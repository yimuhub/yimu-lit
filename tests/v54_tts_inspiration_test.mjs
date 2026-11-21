// v5.4.x 听书 + 灵感库：node 直跑回归（jsdom 全局，不进浏览器、不烧 LLM、不依赖后端）。
// 覆盖面：
//   1) core/tts.js splitSentences 中文句读：句末标点归句、闭合引号跟随、换行断句、
//      省略号/连续标点合并、超长无标点软断句、offset 对齐、空文本；
//   2) core/tts.js 位置记忆：键结构 ew-tts-pos:<book>#<chapter>、读写往返、清除、轮转；
//   3) core/tts.js 语速循环：0.8→1.0→1.25→1.5→0.8（RATES 表与 nextRate 回绕）；
//   4) core/tts.js 不支持环境（jsdom 无 speechSynthesis）优雅返回：start()=0、状态 idle、不抛错；
//   5) 打字音静音联动契约：typingsound.js 导出 mute/isMuted（tts.js 静态导入 + try/catch 包裹）；
//   6) views/inspiration.js 行内编辑状态机：start/input/cancel/save-begin/done/error/非法动作；
//   7) views/inspiration.js 热门标签聚合：计数、排序、topN、空集；
//   8) views/inspiration.js #标签 提取与首页速记卡（welcome.js）/后端三方同口径对拍；
//   9) 视图冒烟：renderInspiration / renderRank 可在 jsdom 下渲染出骨架节点；
//  10) 结构契约：bench.js 听书胶囊挂载与销毁清理、纯净域朗读口径、位置记忆询问；
//      rank.css / inspiration.css 语义类齐备（升降幅着色、瀑布流、置顶卡）。
import fs from 'fs';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';
import { JSDOM } from 'jsdom';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const V2 = path.join(ROOT, 'static', 'v2');
const failed = [];
function check(name, cond, extra = '') {
  console.log(`${cond ? '✅' : '❌'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed.push(name);
}

const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

/* ---------------------------------------------------------------- jsdom 环境 */
/* tts.js 静态导入 typingsound.js、inspiration.js 导入 ui/api 链——统一先铺 jsdom
   全局再动态 import（与 v52/v53/v54_home 回归同一姿势；loadCss 需要 CSS.escape polyfill）。 */
const dom = new JSDOM('<!doctype html><html><body></body></html>',
  { url: 'http://127.0.0.1:8848/', pretendToBeVisual: true });
global.window = dom.window;
global.document = dom.window.document;
global.Node = dom.window.Node;
global.Element = dom.window.Element;
global.HTMLElement = dom.window.HTMLElement;
global.localStorage = dom.window.localStorage;
global.getComputedStyle = dom.window.getComputedStyle;
global.location = dom.window.location;
if (!global.CSS || !global.CSS.escape) {
  global.CSS = { ...(global.CSS || {}), escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => '\\' + c) };
}
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true, writable: true });

/* ---------------------------------------------------------------- 1. splitSentences 中文句读 */

const ttsUrl = pathToFileURL(path.join(V2, 'js/core/tts.js')).href;
const tts = await import(ttsUrl);

check('tts.js 导出契约齐备（splitSentences/posKey/savePos/loadPos/clearPos/RATES/nextRate/createTTS/ttsSupported）',
  ['splitSentences', 'posKey', 'savePos', 'loadPos', 'clearPos', 'RATES', 'nextRate', 'createTTS', 'ttsSupported']
    .every((k) => typeof tts[k] !== 'undefined'));

{
  const s = tts.splitSentences('夜色降临。他问：“你是谁？”她没答。');
  check('句末标点归句 + 闭合引号跟随（3 句）',
    s.length === 3
    && s[0].text === '夜色降临。'
    && s[1].text === '他问：“你是谁？”'   // ？” 整体归前句
    && s[2].text === '她没答。',
    JSON.stringify(s.map((x) => x.text)));
}
{
  const s = tts.splitSentences('第一段第一句！第二句？\n\n新段开始；仍算一句。');
  check('换行独立断句 + 感叹/问/分号句读（4 句，；为句末停顿）',
    s.length === 4
    && s[0].text === '第一段第一句！' && s[1].text === '第二句？'
    && s[2].text === '新段开始；' && s[3].text === '仍算一句。',
    JSON.stringify(s.map((x) => x.text)));
}
{
  const s = tts.splitSentences('他走了……她还在等。真的！！');
  check('省略号归前句、句号断句、连续标点合并（3 句）',
    s.length === 3
    && s[0].text === '他走了……'
    && s[1].text === '她还在等。'
    && s[2].text === '真的！！',   // ！！ 连续句末标点合并进同一句
    JSON.stringify(s.map((x) => x.text)));
}
{
  const long = '分句之间只有逗号没有句号，'.repeat(12);   // 156 字，无句末标点只有软停顿
  const s = tts.splitSentences(long);
  check('超长无句末标点文本按软停顿（逗号）断句（句数>1 且拼回原文）',
    s.length > 1 && s.map((x) => x.text).join('') === long, `${s.length} 句`);
}
{
  const raw = '甲。乙！丙？';
  const s = tts.splitSentences(raw);
  check('句偏移对齐原文（slice(start,end) === text，可用于编辑器 revealOffset）',
    s.every((x) => raw.slice(x.start, x.end) === x.text),
    JSON.stringify(s.map((x) => [x.start, x.end])));
}
check('空文本/无标点文本不抛错（无标点整段归一句）',
  tts.splitSentences('').length === 0
  && tts.splitSentences('   ').length === 0
  && tts.splitSentences('没有标点的一整段').length === 1);

/* ---------------------------------------------------------------- 2. 位置记忆 */

check('位置记忆键结构：ew-tts-pos:<book>#<chapter>',
  tts.posKey('p-b1', 7) === 'ew-tts-pos:p-b1#7'
  && tts.posKey('', null) === 'ew-tts-pos:default#1',
  tts.posKey('p-b1', 7));
{
  localStorage.removeItem('ew-tts-pos:p-b1#7');
  check('未听过的章 loadPos 返回 null', tts.loadPos('p-b1', 7) === null);
  tts.savePos('p-b1', 7, 42);
  const pos = tts.loadPos('p-b1', 7);
  check('savePos → loadPos 往返（index/book/chapter/at 结构齐备）',
    pos && pos.index === 42 && pos.book === 'p-b1' && pos.chapter === 7 && typeof pos.at === 'string',
    JSON.stringify(pos));
  tts.clearPos('p-b1', 7);
  check('clearPos 后 loadPos 返回 null', tts.loadPos('p-b1', 7) === null);
  tts.savePos('p-x', 1, 1);
  tts.savePos('p-x', 2, 2);
  tts.prunePos(1);
  const remaining = [];
  for (let i = 0; i < localStorage.length; i += 1) {
    const k = localStorage.key(i);
    if (k && k.startsWith('ew-tts-pos:')) remaining.push(k);
  }
  check('prunePos 轮转：只留最近 1 条',
    remaining.length === 1 && remaining[0] === 'ew-tts-pos:p-x#2',
    JSON.stringify(remaining));
}

/* ---------------------------------------------------------------- 3. 语速循环 */

check('RATES 档位表为 0.8/1.0/1.25/1.5',
  JSON.stringify(tts.RATES) === JSON.stringify([0.8, 1.0, 1.25, 1.5]));
check('语速循环回绕：0.8→1.0→1.25→1.5→0.8（未知档位按 1.0 的下一档）',
  [0.8, 1.0, 1.25, 1.5].every((r) => tts.nextRate(r) === { 0.8: 1.0, 1.0: 1.25, 1.25: 1.5, 1.5: 0.8 }[r])
  && tts.nextRate(999) === 1.25);

/* ---------------------------------------------------------------- 4. 不支持环境优雅返回 */

{
  const ctl = tts.createTTS({ book: 'p-b1', chapter: 3 });
  const states = [];
  const ctl2 = tts.createTTS({
    book: 'p-b1', chapter: 3,
    onState: (s) => states.push(s),
    onSentence: () => { throw new Error('回调异常也不许外抛'); }
  });
  const n = ctl2.start('一句。两句。');
  check('jsdom 无 speechSynthesis：start() 返回 0、状态保持 idle、不抛错',
    tts.ttsSupported() === false && n === 0 && ctl.state === 'idle' && ctl2.state === 'idle');
  ctl2.pause(); ctl2.resume(); ctl2.stop(); ctl2.setRate(1.25); ctl2.cycleRate();
  check('不支持环境下 pause/resume/stop/setRate 均安全空转', ctl2.state === 'idle');
}

/* ---------------------------------------------------------------- 5. 打字音静音联动契约 */

{
  const sound = await import(pathToFileURL(path.join(V2, 'js/core/typingsound.js')).href);
  check('typingsound.js 导出 mute/isMuted（听书联动复用的既有契约）',
    typeof sound.mute === 'function' && typeof sound.isMuted === 'function');
  sound.mute(true);
  const mutedWhilePlaying = sound.isMuted() === true;
  sound.mute(false);
  check('mute(true/false) 运行时静音可复位', mutedWhilePlaying && sound.isMuted() === false);
  const ttsSrc = read('static/v2/js/core/tts.js');
  check('tts.js 复用 mute（静态导入）且调用处 try/catch 包裹（联动失败不影响朗读）',
    ttsSrc.includes("import { mute as muteTypingSound } from './typingsound.js'")
    && /try\s*\{\s*muteTypingSound\(on\);\s*\}\s*catch/.test(ttsSrc));
}

/* ---------------------------------------------------------------- 6. 行内编辑状态机 */

const inspUrl = pathToFileURL(path.join(V2, 'js/views/inspiration.js')).href;
const insp = await import(inspUrl);

check('inspiration.js 导出契约齐备（renderInspiration/editReducer/aggregateTags/extractTags）',
  typeof insp.renderInspiration === 'function'
  && typeof insp.editReducer === 'function'
  && typeof insp.aggregateTags === 'function'
  && typeof insp.extractTags === 'function');

{
  const IDLE = insp.EDIT_IDLE;
  let s = IDLE;
  s = insp.editReducer(s, { type: 'start', id: 'a1', text: '原文' });
  check('start：进入编辑态（id/draft 就位、无错误）',
    s.id === 'a1' && s.draft === '原文' && s.saving === false && s.error === '');
  s = insp.editReducer(s, { type: 'input', value: '改了' });
  check('input：更新草稿并清错误', s.draft === '改了' && s.error === '');
  s = insp.editReducer(s, { type: 'save-begin' });
  check('save-begin：进入保存中（防双击重复提交）', s.saving === true);
  s = insp.editReducer(s, { type: 'save-error', message: '保存失败，请重试' });
  check('save-error：退回编辑态、草稿不丢、错误可见',
    s.saving === false && s.error === '保存失败，请重试' && s.draft === '改了');
  s = insp.editReducer(s, { type: 'save-done' });
  check('save-done：回到空闲', s.id === null && s.draft === '' && s.saving === false);
  const same = insp.editReducer(IDLE, { type: 'input', value: '越界' });
  const same2 = insp.editReducer(IDLE, { type: 'save-begin' });
  const same3 = insp.editReducer(IDLE, { type: 'save-error', message: 'x' });
  check('非法动作（无编辑对象）原样返回不越界',
    same === IDLE && same2 === IDLE && same3 === IDLE);
  const switched = insp.editReducer(
    insp.editReducer(IDLE, { type: 'start', id: 'a', text: '甲' }),
    { type: 'start', id: 'b', text: '乙' });
  check('编辑中再次 start：切换编辑对象并重置草稿', switched.id === 'b' && switched.draft === '乙');
  const cancelled = insp.editReducer(
    insp.editReducer(IDLE, { type: 'start', id: 'a', text: '甲' }),
    { type: 'cancel' });
  check('cancel：放弃草稿回空闲', cancelled === IDLE || cancelled.id === null);
}

/* ---------------------------------------------------------------- 7. 热门标签聚合 */

{
  const items = [
    { tags: ['修仙', '都市'] },
    { tags: ['修仙'] },
    { tags: ['都市', '重生'] },
    { tags: [] },
    { tags: ['修仙', '  ', ''] }
  ];
  const tags = insp.aggregateTags(items, 20);
  check('标签聚合计数：出现次数正确',
    tags[0].tag === '修仙' && tags[0].count === 3
    && tags[1].tag === '都市' && tags[1].count === 2
    && tags[2].tag === '重生' && tags[2].count === 1,
    JSON.stringify(tags));
  check('topN 截断', insp.aggregateTags(items, 2).length === 2);
  check('空集/坏条目不抛错', insp.aggregateTags([]).length === 0
    && insp.aggregateTags([null, 42, {}]).length === 0);
}

/* ---------------------------------------------------------------- 8. #标签 三方同口径 */

{
  const welcome = await import(pathToFileURL(path.join(V2, 'js/views/welcome.js')).href);
  const cases = [
    '#灵感 顺手记一条 #人物-林晚',
    '没有标签的普通句子',
    '#重复 #重复',
    '句尾#句尾标签。',
    `#${'x'.repeat(21)}`,
    `#${'x'.repeat(19)} 尾`,
    '#修仙 #都市 #重生 #.System'
  ];
  check('#标签 提取与首页速记卡（welcome.js）同口径',
    cases.every((c) => JSON.stringify(insp.extractTags(c)) === JSON.stringify(welcome.extractTags(c))),
    JSON.stringify(insp.extractTags(cases[0])));
  check('#标签 最多 8 个、超长整体不收（与后端 bot/inspirations.py 同口径）',
    insp.extractTags(Array.from({ length: 10 }, (_, i) => `#标${i}`).join(' ')).length === 8
    && insp.extractTags(`#${'x'.repeat(21)}`).length === 0);
}

/* ---------------------------------------------------------------- 9. 视图冒烟 */

{
  const host = document.createElement('div');
  document.body.append(host);
  let view = null;
  let threw = false;
  try {
    view = insp.renderInspiration(host);
  } catch (e) {
    threw = true;
    console.log(e);
  }
  check('renderInspiration 在 jsdom 下渲染出骨架（无异常）', !threw && !!view
    && !!host.querySelector('.insp-view')
    && !!host.querySelector('.insp-new-input')
    && !!host.querySelector('.insp-masonry')
    && !!host.querySelector('.insp-tag-list'));
  try { view.destroy(); } catch { /* ignore */ }
  host.remove();
}
{
  const rankUrl = pathToFileURL(path.join(V2, 'js/views/rank.js')).href;
  const rank = await import(rankUrl);
  check('rank.js 导出 renderRank + changeLabel',
    typeof rank.renderRank === 'function' && typeof rank.changeLabel === 'function');
  const meta = [
    [{ tag: 'up', delta: 2 }, '↑2', 'rk-up'],
    [{ tag: 'down', delta: -3 }, '↓3', 'rk-down'],
    [{ tag: 'new' }, '新', 'rk-new'],
    [{ tag: 'same', delta: 0 }, '—', 'rk-same'],
    [null, '—', 'rk-same']
  ];
  check('changeLabel 升降幅展示（↑2 红 / ↓3 绿 / 新 / —）',
    meta.every(([c, label, cls]) => {
      const r = rank.changeLabel(c);
      return r.label === label && r.cls === cls;
    }));
  const host = document.createElement('div');
  document.body.append(host);
  let view = null;
  let threw = false;
  try {
    view = rank.renderRank(host);
  } catch (e) {
    threw = true;
    console.log(e);
  }
  check('renderRank 在 jsdom 下渲染出骨架（无异常）', !threw && !!view
    && !!host.querySelector('.rank-view')
    && !!host.querySelector('.rank-collect')
    && !!host.querySelector('.rank-table-wrap')
    && !!host.querySelector('.rank-review'));
  try { view.destroy(); } catch { /* ignore */ }
  host.remove();
}

/* ---------------------------------------------------------------- 10. 结构契约（文本级） */

{
  const benchSrc = read('static/v2/js/views/bench.js');
  check('bench.js 听书胶囊挂载：createTTS（book=作品 key / chapter=章号）+ 胶囊入 head',
    benchSrc.includes("import { createTTS, ttsSupported, hasChineseVoice, loadPos } from '../core/tts.js'")
    && benchSrc.includes('const ttsCtl = createTTS({')
    && benchSrc.includes('ttsCapsule,'));
  check('bench.js 朗读纯净域：文本先过 cleanChapterText（只读正文不读概要备注）',
    benchSrc.includes('return cleanChapterText(editor.getText()) || editor.getText();'));
  check('bench.js 位置记忆：重进本章询问「继续听？」（loadPos + confirmDialog）',
    benchSrc.includes('loadPos(key, model.n)') && benchSrc.includes('继续听'));
  check('bench.js 听书销毁清理：切章与 destroy 均调 ttsStop（恢复打字音）',
    benchSrc.split('ttsStop();   /* v5.4.x').length >= 3);
  check('bench.js 语速循环与静音联动按钮齐备（0.8/1.0/1.25/1.5 提示语 + 切换语速）',
    benchSrc.includes('切换语速（0.8 / 1.0 / 1.25 / 1.5）') && benchSrc.includes('cycleRate()'));
  check('bench.js 降级 toast：SpeechSynthesis 不可用给中文说明',
    benchSrc.includes('当前浏览器不支持语音合成'));
}
{
  const rankCss = read('static/v2/css/rank.css');
  check('rank.css 语义类齐备（采集卡/表格/升降幅着色/解读卡，颜色全走 tokens）',
    ['.rank-collect', '.rank-table', '.rk-up', '.rk-down', '.rk-new', '.rank-review', '.rank-empty']
      .every((c) => rankCss.includes(c))
    && !/#[0-9a-fA-F]{3,8}\b/.test(rankCss.replace(/var\(--[^)]*\)/g, '')));
  const inspCss = read('static/v2/css/inspiration.css');
  check('inspiration.css 语义类齐备（瀑布流 columns / 置顶卡 / 标签 / 行内编辑）',
    ['.insp-masonry', 'columns: 3', '.insp-card.is-pinned', '.insp-tag', '.insp-edit-input', '.insp-text']
      .every((c) => inspCss.includes(c)));
}

/* ---------------------------------------------------------------- 汇总 */

console.log('');
if (failed.length) {
  console.log(`❌ ${failed.length} 项未通过：`);
  for (const f of failed) console.log(`   - ${f}`);
  process.exit(1);
} else {
  console.log('✅ 全部通过');
}
