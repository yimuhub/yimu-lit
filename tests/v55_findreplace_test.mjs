// v5.5.x 全书查找替换（core/findreplace.js）+ A4 结算卡文案库/账本 + 零硬编码色（node 直跑，jsdom 仿真）。
//
// 覆盖（易创对齐清单 A1/A4，机制自研）：
// · 纯函数：不区分大小写默认 / 英文精确 / 全量替换（replacement 里的 $& 不被二次解释）/ 替换前原文校验；
// · 浮层状态机：输入防抖重扫 → position/total 导航 → 回车从光标处跳下一处（回卷全文）；
//   编辑器 docChanged → 结果标 stale + 自动重扫当前章；章节 begin 事件 → 立即重扫；
// · 三级替换回调：替换（当前一处，走编辑器 replaceRange + flushSave）/ 本章替换（全部替换 +
//   flushSave）/ 全书替换（POST /api/book/replace，raw 锁定章进 skip；确认文案注明原文可从
//   章节历史版本恢复；替换后全书结果自动重搜）；
// · A4：结算文案库全中文（14 条）+ 随机性；会话净增账本（begin/update/commit 事件流口径，
//   删字不回扣、跨书事件忽略）；
// · 零硬编码色：findreplace.js 源码里不出现 hex/rgb/hsl 颜色（颜色全部走主题变量）。
import { JSDOM } from 'jsdom';
import path from 'path';
import fs from 'fs';
import { fileURLToPath, pathToFileURL } from 'url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const dom = new JSDOM(
  '<!doctype html><html><body><div id="host"></div></body></html>',
  { url: 'http://127.0.0.1:8848/', pretendToBeVisual: true }
);
global.window = dom.window;
global.document = dom.window.document;
global.Node = dom.window.Node;
global.Element = dom.window.Element;
global.HTMLElement = dom.window.HTMLElement;
global.Event = dom.window.Event;
global.KeyboardEvent = dom.window.KeyboardEvent;
global.CustomEvent = dom.window.CustomEvent;
global.Range = dom.window.Range;
global.getComputedStyle = dom.window.getComputedStyle;
global.requestAnimationFrame = (cb) => setTimeout(cb, 0);
global.sessionStorage = dom.window.sessionStorage;
global.localStorage = dom.window.localStorage;
if (!global.CSS && dom.window.CSS) global.CSS = dom.window.CSS;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true, writable: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let failed = 0;
function check(name, cond, extra = '') {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed += 1;
}

/* ------------------------------------------------ fetch 桩：/api/book/find 与 /api/book/replace */

const BOOK_GROUPS = [
  { n: 1, title: '雨夜', count: 3, snippets: [{ before: '他握着', match: '雨', after: '停在屋檐下…' }] },
  { n: 3, title: '山涧', count: 1, snippets: [{ before: '山涧里的', match: '雨', after: '声很密。' }] }
];
const apiCalls = [];
global.fetch = async (url, init = {}) => {
  const u = String(url);
  apiCalls.push({ url: u, init, body: init.body ? JSON.parse(init.body) : null });
  if (u.includes('/api/book/find')) {
    return {
      ok: true, status: 200,
      headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'application/json' : null) },
      json: async () => ({ ok: true, key: 'k1', q: '雨', groups: BOOK_GROUPS, total: 4, chapters: 2 })
    };
  }
  if (u.includes('/api/book/replace')) {
    return {
      ok: true, status: 200,
      headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'application/json' : null) },
      json: async () => ({ ok: true, changed: [{ n: 3, title: '山涧', count: 1, words: 10 }], skipped: [], total: 1 })
    };
  }
  throw new Error('unexpected fetch: ' + u);
};

/* ------------------------------------------------ 模块装载（全局桩就位之后） */

const { createEditor } = await import(pathToFileURL(path.join(root, 'static/v2/js/core/editor.js')).href);
const { mountFindReplace, findReplaceInternals } =
  await import(pathToFileURL(path.join(root, 'static/v2/js/core/findreplace.js')).href);
const benchMod = await import(pathToFileURL(path.join(root, 'static/v2/js/views/bench.js')).href);

const host = document.getElementById('host');

/* ================================================================
   1. 纯函数：大小写口径 / 全量替换 / 原文校验 / 确认文案红线
   ================================================================ */
{
  const { findMatches, replaceAllMatches, matchStillEqual, bookReplaceConfirmText } = findReplaceInternals;
  const text = 'His LOCK glows. A small lock sits below.';
  check('纯函数：默认不区分大小写（LOCK+lock 全命中）', findMatches(text, 'lock', false).length === 2);
  check('纯函数：英文精确口径只命中小写', findMatches(text, 'lock', true).length === 1
    && findMatches(text, 'LOCK', true).length === 1);
  check('纯函数：中文查找不受大小写开关影响', findMatches('雨停了。雨又下起来。', '雨', true).length === 2);
  check('纯函数：全量替换 replacement 的 $& 不被二次解释',
    replaceAllMatches('雨停了', '雨', '$&雪', false) === '$&雪停了');
  check('纯函数：正则元字符按字面匹配', findMatches('a.b.c', '.').length === 2);
  check('纯函数：替换前原文校验（大小写口径一致才放行）',
    matchStillEqual(text, { start: 4, end: 8 }, 'LOCK', true) === true
    && matchStillEqual(text, { start: 4, end: 8 }, 'lock', true) === false
    && matchStillEqual(text, { start: 4, end: 8 }, 'lock', false) === true);

  const msg = bookReplaceConfirmText('雨', '雪', 4, [3]);
  check('确认文案：注明原文可在章节历史版本中恢复（可恢复承诺红线）',
    msg.includes('章节历史版本') && msg.includes('恢复'));
  check('确认文案：注明只作用正文域、概要/备注不动（纯净域红线）',
    msg.includes('正文') && msg.includes('概要'));
  check('确认文案：raw 锁定章的跳过说明在位', msg.includes('工作文件') && msg.includes('3'));
  check('确认文案：空替换说明删除语义', bookReplaceConfirmText('雨', '', 1).includes('删除'));
}

/* ================================================================
   2. 浮层状态机：防抖重扫 / position-total 导航 / 回车跳下一处 / stale
   ================================================================ */
{
  host.textContent = '';
  const ed = createEditor({ host });
  const flushCalls = [];
  ed.setText('雨停了。雨又下起来。雨幕很厚。', { silent: true });

  const fr = mountFindReplace({
    editor: ed,
    getContext: () => ({ key: 'k1', n: 1 }),
    isLocked: () => false,
    flushSave: () => { flushCalls.push(1); },
    onGotoChapter: () => {},
    onChapterReplaced: () => {},
    confirm: async () => true,
    debounceMs: 30
  });
  const panel = fr.el;
  const [qInput, replInput] = panel.querySelectorAll('.fr-input');
  const nav = panel.querySelector('.fr-nav');
  const btnOne = [...panel.querySelectorAll('button')].find((b) => b.textContent === '替换');
  const btnChapter = [...panel.querySelectorAll('button')].find((b) => b.textContent === '本章替换');

  fr.open();
  check('浮层：打开可见', fr.isOpen() === true && !panel.hidden);
  check('浮层：未输入时导航为空', nav.textContent === '');

  qInput.value = '雨';
  qInput.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  replInput.value = '雪';
  replInput.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  await sleep(60);   // 防抖 30ms + 余量
  const dbg1 = fr.debug();
  check('状态机：输入防抖后即时重扫当前章（3 处）',
    dbg1.matches.length === 3 && dbg1.stale === false && nav.textContent === '1/3',
    `nav=${nav.textContent}`);

  /* 回车跳下一处：光标在第 1 处内（offset 1）→ 跳到第 2 处（导航先算好，reveal 定位在后）。
     每次回车前显式落光标：jsdom 的 focus() 会动选区，导航判定本身只依赖回车瞬间的光标。 */
  ed.setCaretOffset(1);
  qInput.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  check('状态机：回车从光标处跳下一处（2/3）', nav.textContent === '2/3', nav.textContent);
  ed.setCaretOffset(4);
  qInput.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  check('状态机：回车连跳（3/3）', nav.textContent === '3/3', nav.textContent);
  ed.setCaretOffset(10);   // 末处（第三处「雨」）之内：其后无匹配 → 回卷全文
  qInput.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  check('状态机：末处回车回卷全文（1/3）', nav.textContent === '1/3', nav.textContent);

  /* 替换当前一处：编辑器内容更新 + flushSave 回调 + 导航刷新 */
  btnOne.click();
  check('三级替换：替换当前一处生效（雨→雪）', ed.getText().startsWith('雪停了。'), ed.getText().slice(0, 8));
  check('三级替换：替换后 flushSave 被回调（落盘链路）', flushCalls.length === 1);
  check('三级替换：替换后导航指向下一处（剩两处）', nav.textContent === '1/2', nav.textContent);

  /* 本章替换：全部替换 + flushSave */
  btnChapter.click();
  check('三级替换：本章替换全量生效（雨→雪 ×2）',
    !ed.getText().includes('雨') && ed.getText().includes('雪又下起来'), ed.getText());
  check('三级替换：本章替换后导航归零', nav.textContent === '0/0', nav.textContent);
  check('三级替换：本章替换回调 flushSave（自动落盘）', flushCalls.length === 2);

  /* 编辑器 docChanged → stale + 自动重扫（当前章内，不抢光标） */
  qInput.value = '雪';
  qInput.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  await sleep(60);
  const p0 = host.querySelector('.edp');
  p0.textContent = '雪停了。雪又下起来。雪幕很厚。又落了一场雪。';
  p0.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  check('状态机：编辑器 docChanged 立即标 stale',
    fr.debug().stale === true && !panel.querySelector('.fr-stale').hidden);
  await sleep(70);
  check('状态机：stale 自动重扫当前章后复位（4 处）',
    fr.debug().stale === false && fr.debug().matches.length === 4 && nav.textContent === '1/4',
    `nav=${nav.textContent}`);

  /* 章节装载完成（yimu-editor-session begin）→ 立即重扫新章（此时查找词是「雪」，
     新章『山里没有雪。』恰好 1 处 → 导航从 1/4 刷新为 1/1，证明重扫已发生） */
  ed.setText('山里没有雪。', { silent: true });
  document.dispatchEvent(new CustomEvent('yimu-editor-session', {
    detail: { phase: 'begin', key: 'k1', text: '山里没有雪。' }
  }));
  check('状态机：章节 begin 事件后立即重扫新章（1/1）',
    fr.debug().matches.length === 1 && nav.textContent === '1/1', nav.textContent);

  /* 区分大小写开关：英文精确口径即时生效 */
  ed.setText('Lock lock LOCK.', { silent: true });
  document.dispatchEvent(new CustomEvent('yimu-editor-session', {
    detail: { phase: 'begin', key: 'k1', text: 'Lock lock LOCK.' }
  }));
  qInput.value = 'lock';
  qInput.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  await sleep(60);
  check('状态机：默认不区分大小写（3 处）', nav.textContent === '1/3', nav.textContent);
  const caseChk = panel.querySelector('.fr-case input');
  caseChk.checked = true;
  caseChk.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  await sleep(10);
  check('状态机：勾选区分大小写后按精确口径（1 处）', nav.textContent === '1/1', nav.textContent);
  caseChk.checked = false;
  caseChk.dispatchEvent(new dom.window.Event('change', { bubbles: true }));

  fr.destroy();
  ed.destroy();
  check('浮层：destroy 后面板摘除', !panel.parentNode);
}

/* ================================================================
   3. 全书替换：后端契约 / raw 锁定章进 skip / 替换后自动重搜
   ================================================================ */
{
  apiCalls.length = 0;
  host.textContent = '';
  const ed = createEditor({ host });
  ed.setText('雨停了。', { silent: true });
  const flushed = [];
  const reloaded = [];
  let locked = false;

  const fr = mountFindReplace({
    editor: ed,
    getContext: () => ({ key: 'k1', n: 1 }),
    isLocked: () => locked,
    flushSave: () => { flushed.push(1); },
    onGotoChapter: () => {},
    onChapterReplaced: () => { reloaded.push(1); },
    confirm: async ({ message } = {}) => { fr._confirmMsg = message; return true; },
    debounceMs: 30
  });
  const panel = fr.el;
  const [qInput, replInput] = panel.querySelectorAll('.fr-input');
  const btnBook = [...panel.querySelectorAll('button')].find((b) => b.textContent === '全书替换');

  qInput.value = '雨';
  qInput.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  replInput.value = '雪';
  replInput.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  await sleep(40);

  await fr.searchBook();
  check('搜索全书：请求命中 /api/book/find', apiCalls.some((c) => c.url.includes('/api/book/find')));
  check('搜索全书：分组结果渲染（按章分组 snippet 高亮）',
    panel.querySelectorAll('.fr-group').length === 2
    && panel.querySelectorAll('.fr-hit').length >= 2);
  check('搜索全书：汇总行给出全书处数', panel.querySelector('.fr-summary').textContent.includes('4 处'));

  await btnBook.click();
  await sleep(20);
  const replCall = apiCalls.find((c) => c.url.includes('/api/book/replace'));
  check('全书替换：POST /api/book/replace 走后端 saveChapter 链路', !!replCall);
  check('全书替换：请求体带 key/q/replacement/scope=book',
    replCall.body.key === 'k1' && replCall.body.q === '雨' && replCall.body.replacement === '雪'
    && replCall.body.scope === 'book', JSON.stringify(replCall.body));
  check('全书替换：确认文案注明可从章节历史版本恢复',
    (fr._confirmMsg || '').includes('章节历史版本'));
  check('全书替换：替换前先 flushSave 当前章（防脏改被覆盖）', flushed.length === 1);
  check('全书替换：当前章不在 changed → 不重载编辑器', reloaded.length === 0);
  check('全书替换：替换成功后全书结果自动重搜',
    apiCalls.filter((c) => c.url.includes('/api/book/find')).length >= 2);

  /* raw 锁定章：skip=[n] + 确认文案提示 */
  apiCalls.length = 0;
  locked = true;
  fr._confirmMsg = '';
  await btnBook.click();
  await sleep(20);
  const replCall2 = apiCalls.find((c) => c.url.includes('/api/book/replace'));
  check('全书替换：raw 锁定章经 skip 交给后端跳过',
    Array.isArray(replCall2.body.skip) && replCall2.body.skip[0] === 1, JSON.stringify(replCall2.body));
  check('全书替换：raw 锁定章在确认文案中说明',
    (fr._confirmMsg || '').includes('工作文件'));

  fr.destroy();
  ed.destroy();
}

/* ================================================================
   4. A4 结算：文案库全中文 + 随机性；会话净增账本口径
   ================================================================ */
{
  const { SETTLE_PHRASES, pickSettlePhrase, createSettleLedger } = benchMod;
  check('结算文案库：12-16 条', SETTLE_PHRASES.length >= 12 && SETTLE_PHRASES.length <= 16,
    `共 ${SETTLE_PHRASES.length} 条`);
  const okCJK = SETTLE_PHRASES.every((s) =>
    /^[\u4e00-\u9fff][\u4e00-\u9fff0-9，。、！？·…—]*[\u4e00-\u9fff。]$/.test(s) && !/[A-Za-z]/.test(s));
  check('结算文案库：全中文（无英文字母，中文标点收尾）', okCJK,
    JSON.stringify(SETTLE_PHRASES.filter((s) => !/^[\u4e00-\u9fff]/.test(s))));
  const picks = new Set(Array.from({ length: 40 }, () => pickSettlePhrase()));
  check('结算文案库：随机性（40 次取样 ≥5 种）', picks.size >= 5, `取样 ${picks.size} 种`);

  const led = createSettleLedger('k1');
  led.onDetail({ phase: 'begin', key: 'k1', text: '雨停了。雨又下起来。' });               // 8 个汉字
  led.onDetail({ phase: 'update', key: 'k1', text: '雨停了。雨又下起来。又写了六字。' });  // 13 个汉字（峰值）
  led.onDetail({ phase: 'commit', key: 'k1', text: '雨停了。雨又下起来。又写' });          // 11 个汉字（删字不回扣）
  check('结算账本：逐章净增 = 峰值 − 进入基线（删字不回扣）', led.chars() === 5, `chars=${led.chars()}`);

  led.onDetail({ phase: 'begin', key: 'k2', text: '一二三四五' });
  led.onDetail({ phase: 'update', key: 'k2', text: '一二三四五写了两段。' });
  led.onDetail({ phase: 'commit', key: 'k2', text: '一二三四五写了两段。' });
  check('结算账本：跨书事件忽略（只记本 bench 的 key）', led.chars() === 5, `chars=${led.chars()}`);

  led.onDetail({ phase: 'begin', key: 'k1', text: '一二三' });
  led.onDetail({ phase: 'commit', key: 'k1', text: '' });
  check('结算账本：净减章不回扣总额（max(0, 退出-进入)）', led.chars() === 5, `chars=${led.chars()}`);

  led.onDetail({ phase: 'begin', key: 'k1', text: '一二三' });
  led.onDetail({ phase: 'update', key: 'k1', text: '一二三四五六七' });
  led.onDetail({ phase: 'commit', key: 'k1', text: '一二三四五六七' });
  check('结算账本：多章会话累计（+4）', led.chars() === 9, `chars=${led.chars()}`);
}

/* ================================================================
   5. 零硬编码色：findreplace.js 颜色全部走主题变量
   ================================================================ */
{
  const src = fs.readFileSync(path.join(root, 'static/v2/js/core/findreplace.js'), 'utf-8');
  const hits = [];
  if (/#[0-9a-fA-F]{3,8}\b/.test(src)) hits.push('hex');
  if (/\brgba?\(/.test(src)) hits.push('rgb()');
  if (/\bhsla?\(/.test(src)) hits.push('hsl()');
  check('零硬编码色：findreplace.js 无 hex/rgb/hsl 字面量', hits.length === 0, hits.join(','));
  const css = fs.readFileSync(path.join(root, 'static/v2/css/bench-editor.css'), 'utf-8');
  const frCss = css.slice(css.indexOf('A1 查找替换浮层'));
  check('样式命名空间：.fr-* 与 .settle-* 区块在 bench-editor.css 尾部就位',
    frCss.includes('.fr-panel') && frCss.includes('.settle-card') && frCss.includes('.settle-bar-fill'));
  /* 颜色承载声明里不许出现 hex/rgb/hsl 字面量（取值一律 var(--token)/transparent/none）；
     盒阴影按既有区块惯例允许 rgba（阴影不算主题色承载面） */
  const colorDecls = frCss.match(/(?:^|[;{\n])\s*(?:color|background(?:-color)?|border(?:-color)?|outline(?:-color)?|fill)\s*:[^;}]+/g) || [];
  const varOnly = colorDecls.length > 0
    && colorDecls.every((d) => !/#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/.test(d));
  check('样式命名空间：fr/settle 区块颜色全部走主题变量（无 hex/rgb 字面量）', varOnly,
    colorDecls.filter((d) => /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/.test(d)).slice(0, 3).join(' | '));
}

/* ================================================================
   汇总
   ================================================================ */
console.log(failed ? `\n${failed} 项失败` : '\n全部通过');
process.exit(failed ? 1 : 0);
