// v5.6.0 编辑器内人物名高亮（core/namehl.js，node 直跑，jsdom 仿真）。
//
// 覆盖（用户点名功能，机制自研，全部零正文污染）：
// · Range 计算纯函数：多人物 / 长名优先（张三丰 vs 张三）/ 重叠名 / 无匹配 /
//   空文本 / 跨段不标 / 软换行段内 / 区间封顶 / 正则特殊字符字面匹配 / 排序；
// · 名单来源与缓存：/api/canvas/graph characters.nodes[].label 解析、模块级缓存
//   （同 key 二次挂载不再发请求）、失败静默降级空名单；
// · 状态机：总开关（localStorage 记忆 + 损坏值兜底）、自定义词增删（清洗 + 持久化）、
//   单词单独开关（ew-name-hl-off:<bookKey>）、拼音组词期间跳过重建；
// · Highlight 注册：CSS.highlights.set('character-name') / 组词跳过 / 关开关清除 /
//   不支持环境（Safari<17.0 等）静默降级不注册；
// · 纯净域红线：开高亮前后 editor.getText() 逐字一致 + 段落 DOM 零改动
//   （除既有 BR 外零新增元素）+ namehl.js 源码零 innerHTML（纯 Range 无 HTML 注入面）；
// · 管理面板 DOM：下拉面板 / 总开关 / 人物清单 ≤30 + 搜索过滤 / 点击人物
//   revealOffset 定位第一处 / 自定义词回车添加与删除 / 单词开关 / 外点关闭 / XSS 词纯文本；
// · 结构契约：bench-editor.css ::highlight(character-name) 区块 + 零硬编码色 +
//   bench.js 挂载点（import/openPanel/notifyText/destroy）在位；
// · 性能：6000 字 × 50 名 Range 构建 < 200ms（目标 <50ms，断言放宽）。
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
if (!global.CSS || !global.CSS.escape) {
  global.CSS = { ...(global.CSS || {}), escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => '\\' + c) };
}
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true, writable: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let failed = 0;
let passed = 0;
function check(name, cond, extra = '') {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
  if (cond) passed += 1;
  else failed += 1;
}

/* ------------------------------------------------ fetch 桩：/api/canvas/graph */

const GRAPH_PAYLOADS = {};   // bookKey → 人物名数组
const fetchCalls = [];
function jsonResponse(obj, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: async () => obj,
    text: async () => JSON.stringify(obj)
  };
}
global.fetch = async (url) => {
  fetchCalls.push(String(url));
  const u = String(url);
  if (u.includes('/api/canvas/graph')) {
    const key = decodeURIComponent((u.match(/key=([^&]+)/) || [])[1] || '');
    if (key in GRAPH_PAYLOADS) {
      return jsonResponse({
        ok: true,
        key,
        characters: { nodes: GRAPH_PAYLOADS[key].map((label) => ({ id: label, label, kind: 'person', weight: 1 })), edges: [] }
      });
    }
    return jsonResponse({ ok: true, key, characters: { nodes: [], edges: [] } });
  }
  throw new Error('unexpected fetch: ' + url);
};

/* ------------------------------------------------ Highlight 注桩（模拟 CSS Custom Highlight API） */

class MockHighlight {
  constructor(...ranges) { this.ranges = ranges; }
}
globalThis.Highlight = MockHighlight;
if (!global.CSS.highlights) global.CSS.highlights = new Map();
function registered() {
  try { return global.CSS.highlights.get('character-name') || null; } catch { return null; }
}
function rangeTexts(hl) {
  if (!hl || !Array.isArray(hl.ranges)) return [];
  return hl.ranges.map((r) => {
    try { return String(r.toString()); } catch { return ''; }
  });
}

/* ------------------------------------------------ 模块装载 */

const {
  parseGraphNames,
  paragraphSpans,
  computeNameRanges,
  filterNameList,
  loadCustomWords,
  saveCustomWords,
  loadNameOff,
  saveNameOff,
  loadMasterEnabled,
  saveMasterEnabled,
  isHighlightSupported,
  clearNameCache,
  mountNameHighlight,
  NAMEHL_HL_NAME,
  NAMEHL_MASTER_KEY,
  NAMEHL_STORE_PREFIX,
  NAMEHL_OFF_PREFIX,
  NAMEHL_DEBOUNCE_MS,
  NAMEHL_PANEL_MAX,
  NAMEHL_MAX_NODES,
  NAMEHL_MAX_WORD_LEN,
  NAMEHL_MAX_RANGES
} = await import(pathToFileURL(path.join(root, 'static/v2/js/core/namehl.js')).href);

const { createEditor } = await import(pathToFileURL(path.join(root, 'static/v2/js/core/editor.js')).href);

/* bench.js 与 namehl.js 同链路可装载（本测试不触发 renderBench，fetch 桩兜底） */
await import(pathToFileURL(path.join(root, 'static/v2/js/views/bench.js')).href);

const host = document.getElementById('host');

function mountEditor(placeholder = '') {
  host.textContent = '';
  const ed = createEditor({ host, placeholder });
  return ed;
}

function clearBookKeys(bk) {
  localStorage.removeItem(NAMEHL_STORE_PREFIX + bk);
  localStorage.removeItem(NAMEHL_OFF_PREFIX + bk);
  localStorage.removeItem(NAMEHL_MASTER_KEY);
}

/* ================================================================
   1. computeNameRanges：Range 计算纯函数
   ================================================================ */
{
  const text = [
    '张三丰在山上练剑。张三在山下烧饭。',
    '李四说：张三丰师弟好厉害。',
    '',
    '王五路过，看见了张三。'
  ].join('\n\n');
  const names = ['张三丰', '张三', '李四', '王五'];
  const ranges = computeNameRanges(text, names);

  check('纯函数：多人物全部命中（张三丰/张三/李四/王五 → 6 条）', ranges.length === 6,
    JSON.stringify(ranges));
  check('纯函数：区间切片与词名逐一相等',
    ranges.every((r) => text.slice(r.start, r.end) === r.name));
  check('纯函数：区间按 start 升序', ranges.every((r, i) => i === 0 || ranges[i - 1].start <= r.start));
  const zh = ranges.filter((r) => r.name === '张三丰').map((r) => [r.start, r.end]);
  check('纯函数：长名优先（张三丰 吃掉 [0,3)，张三 不再标同一处）',
    JSON.stringify(zh) === '[[0,3],[23,26]]', JSON.stringify(zh));
  const zs = ranges.filter((r) => r.name === '张三').map((r) => [r.start, r.end]);
  check('纯函数：短名只在未占用处标记（张三 → [9,11) 与 [44,46)）',
    JSON.stringify(zs) === '[[9,11],[44,46]]', JSON.stringify(zs));

  /* 重叠名：等长名按声明顺序占领 */
  const r2 = computeNameRanges('李波波来了', ['李波', '波波']);
  check('纯函数：等长重叠名先声明者占领（李波 → 1 条，波波让位）',
    r2.length === 1 && r2[0].name === '李波' && text_slice(r2[0]) === '李波',
    JSON.stringify(r2));
  function text_slice(r) { return '李波波来了'.slice(r.start, r.end); }

  /* 相邻自重叠：indexOf 游标推进，同一词相邻出现不互相吃 */
  const r3 = computeNameRanges('生生生生', ['生生']);
  check('纯函数：相邻出现双双命中（生生生生 → [0,2)+[2,4)）',
    r3.length === 2 && r3[0].start === 0 && r3[1].start === 2, JSON.stringify(r3));

  /* 跨段不标 */
  const r4 = computeNameRanges('上段张\n\n下段三', ['张三']);
  check('纯函数：名字跨段（张\\n\\n三）不标', r4.length === 0, JSON.stringify(r4));
  const r5 = computeNameRanges('张三在A段\n\n张三在B段', ['张三']);
  check('纯函数：两段各出现各标（2 条，段落内定位）', r5.length === 2, JSON.stringify(r5));

  /* 无匹配 / 空文本 */
  check('纯函数：无匹配 → 空区间', computeNameRanges('完全没有人物出场', ['张三']).length === 0);
  check('纯函数：空文本 / 空名单 → 空区间',
    computeNameRanges('', ['张三']).length === 0 && computeNameRanges('张三', []).length === 0);

  /* 非法词清洗 */
  const r6 = computeNameRanges('张三来了 \n 换行词不标', ['张三', '', '  ', '换\n行词', '张三', 'x'.repeat(NAMEHL_MAX_WORD_LEN + 5)]);
  check('纯函数：非法词过滤（空/空白/含换行/超长/重复）后只标有效词',
    r6.length === 1 && r6[0].name === '张三', JSON.stringify(r6));

  /* 软换行（单 \n 不分段）：同段内两行各出现各标 */
  const r7 = computeNameRanges('李四在第一行\n李四在第二行', ['李四']);
  check('纯函数：软换行段内两行各标（单 \\n 不切断段落）', r7.length === 2, JSON.stringify(r7));

  /* 正则特殊字符：字面匹配（无 escapeHtml/regex 注入面） */
  const r8 = computeNameRanges('编号张三(1)出场', ['张三(1)']);
  check('纯函数：正则特殊字符词字面匹配（张三(1)）',
    r8.length === 1 && text_slice_of('编号张三(1)出场', r8[0]) === '张三(1)', JSON.stringify(r8));
  function text_slice_of(t, r) { return t.slice(r.start, r.end); }

  /* 封顶 */
  const many = Array.from({ length: 40 }, (_, i) => `词${i}`);
  const bigText = Array.from({ length: 200 }, (_, i) => `词${i % 40}铺满`).join('\n\n');
  const r9 = computeNameRanges(bigText, many, { maxRanges: 50 });
  check('纯函数：maxRanges 封顶（200 段命中远超 50 → 只留 50 条）', r9.length === 50, `got ${r9.length}`);
}

/* ================================================================
   2. paragraphSpans：段落切分口径
   ================================================================ */
{
  const s1 = paragraphSpans('');
  check('段落切分：空文本 → 单空段', s1.length === 1 && s1[0].text === '' && s1[0].start === 0);
  const s2 = paragraphSpans('甲\n\n\n\n乙');
  check('段落切分：多余空行按 \\n{2,} 折叠，start 精确',
    s2.length === 2 && s2[0].text === '甲' && s2[1].text === '乙' && s2[1].start === 5,
    JSON.stringify(s2));
  const s3 = paragraphSpans('甲\n\n乙\n\n丙');
  check('段落切分：三段 start 累计（0/3/6）',
    s3.map((s) => s.start).join(',') === '0,3,6' && s3[2].text === '丙');
}

/* ================================================================
   3. parseGraphNames：名单解析
   ================================================================ */
{
  const payload = { characters: { nodes: [{ label: '张三丰 ' }, { label: '张三丰' }, { label: '李四' }, { id: 'idonly' }, {}, null], edges: [] } };
  const list = parseGraphNames(payload);
  check('名单解析：label 提取 + trim + 去重（id 兜底）',
    JSON.stringify(list) === JSON.stringify(['张三丰', '李四', 'idonly']), JSON.stringify(list));
  check('名单解析：非法结构 → 空名单',
    parseGraphNames(null).length === 0 && parseGraphNames({}).length === 0
    && parseGraphNames({ characters: { nodes: 'bad' } }).length === 0);
  const many = Array.from({ length: NAMEHL_MAX_NODES + 20 }, (_, i) => `人${i}`);
  check('名单解析：封顶 NAMEHL_MAX_NODES', parseGraphNames({ characters: { nodes: many.map((label) => ({ label })) } }).length === NAMEHL_MAX_NODES);
}

/* ================================================================
   4. filterNameList：面板过滤
   ================================================================ */
{
  const src = Array.from({ length: 40 }, (_, i) => `人物${String(i + 1).padStart(2, '0')}`);
  check('面板过滤：默认封顶 30', filterNameList(src, '').length === NAMEHL_PANEL_MAX);
  const hit = filterNameList(src, '人物0');
  check('面板过滤：搜索词过滤（人物0 → 9 条：01–09，不含 10–40）', hit.length === 9 && hit[0] === '人物01', JSON.stringify(hit));
  check('面板过滤：不分大小写 + 自定义 limit',
    filterNameList(['ABC', 'abc', 'bcd'], 'abc', 5).length === 2
    && filterNameList(src, '', 5).length === 5);
}

/* ================================================================
   5. 存取与状态机（storage 读写 + 损坏兜底）
   ================================================================ */
{
  const bk = 'bk-store';
  clearBookKeys(bk);
  saveCustomWords(bk, ['张三', ' 张三 ', '李四', '', 'x'.repeat(30), '带\n词'], localStorage);
  check('存储：自定义词清洗后持久化（去重/去空白/截断/滤换行）',
    JSON.stringify(loadCustomWords(bk, localStorage)) === JSON.stringify(['张三', '李四', 'x'.repeat(NAMEHL_MAX_WORD_LEN)]),
    localStorage.getItem(NAMEHL_STORE_PREFIX + bk));
  saveNameOff(bk, ['张三', '张三', '李四'], localStorage);
  check('存储：单词关闭名单去重持久化', JSON.stringify(loadNameOff(bk, localStorage)) === JSON.stringify(['张三', '李四']));
  check('存储：总开关默认开 → 写 0 → false', loadMasterEnabled(localStorage) === true
    && (saveMasterEnabled(false, localStorage), loadMasterEnabled(localStorage) === false));
  localStorage.setItem(NAMEHL_MASTER_KEY, 'yes');
  check('存储：总开关损坏值兜底为关（非 "1" 一律视为关）', loadMasterEnabled(localStorage) === false);
  localStorage.setItem(NAMEHL_STORE_PREFIX + bk, '{bad json!!');
  check('存储：损坏 JSON → 空自定义词（不抛错）', JSON.stringify(loadCustomWords(bk, localStorage)) === '[]');
  clearBookKeys(bk);
}

/* ================================================================
   6. 挂载 + Highlight 注册（注桩环境）
   ================================================================ */
const SAMPLE = [
  '张三丰在山上练剑。张三在山下烧饭。',
  '李四说：张三丰师弟好厉害。',
  '',
  '王五路过，看见了张三。'
].join('\n\n');
let ctlF = null;
let edF = null;
{
  const bk = 'bk-f';
  clearBookKeys(bk);
  GRAPH_PAYLOADS[bk] = ['张三丰', '张三', '李四', '王五'];
  clearNameCache();
  fetchCalls.length = 0;
  check('能力检测：注桩后 isHighlightSupported()=true', isHighlightSupported() === true);

  edF = mountEditor();
  edF.setText(SAMPLE, { silent: true });
  ctlF = mountNameHighlight({ editor: edF, getKey: () => bk, storage: localStorage, onHint: () => {} });
  await sleep(60);   /* 初始名单拉取 + 重建 */

  check('挂载：自动拉取画布名单（1 次 /api/canvas/graph 请求）',
    ctlF.names().join(',') === '张三丰,张三,李四,王五'
    && fetchCalls.filter((u) => u.includes('/api/canvas/graph')).length === 1,
    JSON.stringify(fetchCalls));
  const hl = registered();
  check('注册：装载后自动注册 character-name（Highlight 实例）', !!hl && hl instanceof MockHighlight);
  check('注册：Range 数与纯函数口径一致（6 条）', hl && hl.ranges.length === 6, `${hl && hl.ranges.length}`);
  const texts = rangeTexts(hl);
  check('注册：Range 覆盖文本 = 词名（张三丰×2 + 张三×2 + 李四 + 王五）',
    JSON.stringify(texts) === JSON.stringify(['张三丰', '张三', '李四', '张三丰', '王五', '张三']),
    JSON.stringify(texts));

  /* 防抖重建：输入 → 120ms 防抖 → 新实例替换旧实例 */
  const before = registered();
  edF.setText(SAMPLE + '\n\n新段落张三出场。', { silent: true });
  ctlF.notifyText(edF.getText(), {});
  check('防抖：notifyText 后 60ms 内尚未重建（120ms 防抖）', registered() === before);
  await sleep(NAMEHL_DEBOUNCE_MS + 140);
  const after = registered();
  check('防抖：120ms 后重建为新 Highlight（张三 新增命中）',
    after !== before && after && after.ranges.length === 7, `${after && after.ranges.length}`);
  check('防抖：新增段落的张三被标记',
    rangeTexts(after).includes('张三') && after.ranges.some((r) => r.toString() === '张三' && r.startContainer.textContent.includes('新段落')));

  /* 组词期间跳过 */
  const frozen = registered();
  ctlF.notifyText(edF.getText(), { composing: true });
  await sleep(NAMEHL_DEBOUNCE_MS + 140);
  check('组词跳过：composing=true 期间不重建（实例身份不变）', registered() === frozen);

  /* 会话契约钩子：yimu-editor-session begin（切章装载）触发重算 */
  edF.setText('王五出现了。', { silent: true });
  document.dispatchEvent(new CustomEvent('yimu-editor-session', { detail: { phase: 'begin', key: bk, text: edF.getText() } }));
  await sleep(40);
  const beg = registered();
  check('会话钩子：begin 事件后按新章文本重算（只剩 王五）',
    beg && beg.ranges.length === 1 && rangeTexts(beg)[0] === '王五', JSON.stringify(rangeTexts(beg)));

  /* 单词开关 */
  ctlF.setWordEnabled('王五', false);
  check('单词开关：关闭后 activeWords 排除 + 持久化',
    !ctlF.activeWords().includes('王五')
    && JSON.stringify(loadNameOff(bk, localStorage)) === JSON.stringify(['王五']));
  check('单词开关：注册随之更新（0 条）', registered() === null || registered().ranges.length === 0);
  ctlF.setWordEnabled('王五', true);
  check('单词开关：重新开启恢复标记', registered() && registered().ranges.length === 1);

  /* 总开关 */
  ctlF.setEnabled(false);
  check('总开关：关闭 → 注册清除（CSS.highlights 无 character-name）', !registered());
  check('总开关：记忆 localStorage（ew-name-hl-enabled=0）', localStorage.getItem(NAMEHL_MASTER_KEY) === '0');
  ctlF.setEnabled(true);
  check('总开关：重开 → 重新注册', registered() && registered().ranges.length === 1);

  /* refresh（正文/工作文件切换口径） */
  edF.setText('李四在别处。', { silent: true });
  ctlF.refresh();
  check('refresh：立即按新文本重算（李四 1 条）', registered() && registered().ranges.length === 1 && rangeTexts(registered())[0] === '李四');

  /* 自定义词（API 路径） */
  check('自定义词：addCustomWord 去重拒绝', ctlF.addCustomWord('李四') === false);
  check('自定义词：新增生效 + 持久化',
    ctlF.addCustomWord('剑客甲') === true
    && ctlF.customWords().includes('剑客甲')
    && JSON.stringify(loadCustomWords(bk, localStorage)).includes('剑客甲'));
  edF.setText('剑客甲在山道。', { silent: true });
  ctlF.refresh();
  check('自定义词：注册覆盖自定义词', registered() && rangeTexts(registered())[0] === '剑客甲');
  ctlF.removeCustomWord('剑客甲');
  check('自定义词：删除生效', !ctlF.customWords().includes('剑客甲'));
}

/* 名单缓存：同 key 二次挂载不再发请求；失败静默降级空名单 */
{
  clearNameCache();
  fetchCalls.length = 0;
  const bk = 'bk-cache';
  GRAPH_PAYLOADS[bk] = ['张三丰', '张三'];
  edF.setText('张三在。', { silent: true });
  const c1 = mountNameHighlight({ editor: edF, getKey: () => bk, storage: localStorage, onHint: () => {} });
  await sleep(50);
  const c2 = mountNameHighlight({ editor: edF, getKey: () => bk, storage: localStorage, onHint: () => {} });
  await sleep(50);
  check('名单缓存：两个挂载实例共享缓存（画布请求只发 1 次）',
    c1.names().length === 2 && c2.names().length === 2
    && fetchCalls.filter((u) => u.includes('/api/canvas/graph')).length === 1,
    JSON.stringify(fetchCalls));
  c1.destroy();
  c2.destroy();
  check('destroy：清除注册（CSS.highlights 无 character-name）', !registered());

  /* 失败降级：payload 不存在 key → 后端返回空名单（也走缓存口径）；请求异常 → 空名单 */
  global.fetch = async (url) => {
    const u = String(url);
    if (u.includes('/api/canvas/graph') && u.includes('bk-fail')) throw new Error('boom');
    return jsonResponse({ ok: false, error: 'x' });
  };
  const bkFail = 'bk-fail';
  const c3 = mountNameHighlight({ editor: edF, getKey: () => bkFail, storage: localStorage, onHint: () => {} });
  await sleep(60);
  check('名单失败：静默降级空名单（自定义词仍可高亮）',
    c3.names().length === 0 && c3.addCustomWord('张三') === true && c3.activeWords().join(',') === '张三');
  c3.destroy();
  global.fetch = async (url) => {
    fetchCalls.push(String(url));
    const u = String(url);
    if (u.includes('/api/canvas/graph')) {
      const key = decodeURIComponent((u.match(/key=([^&]+)/) || [])[1] || '');
      if (key in GRAPH_PAYLOADS) {
        return jsonResponse({
          ok: true,
          key,
          characters: { nodes: GRAPH_PAYLOADS[key].map((label) => ({ id: label, label, kind: 'person', weight: 1 })), edges: [] }
        });
      }
      return jsonResponse({ ok: true, key, characters: { nodes: [], edges: [] } });
    }
    throw new Error('unexpected fetch: ' + url);
  };
}

/* ================================================================
   7. 纯净域红线：getText 不变性 + 段落 DOM 零改动
   ================================================================ */
{
  const bk = 'bk-purity';
  clearBookKeys(bk);
  GRAPH_PAYLOADS[bk] = ['张三丰', '张三', '李四', '王五', '韩梅梅'];
  clearNameCache();
  const ed = mountEditor();
  ed.setText(SAMPLE, { silent: true });
  const beforeText = ed.getText();
  const beforeParas = Array.from(ed.el.querySelectorAll('p.edp')).map((p) => ({
    text: p.textContent,
    extraEls: Array.from(p.querySelectorAll('*')).filter((el) => el.tagName !== 'BR').length
  }));
  const ctl = mountNameHighlight({ editor: ed, getKey: () => bk, storage: localStorage, onHint: () => {} });
  await sleep(60);
  ctl.notifyText(ed.getText(), {});
  await sleep(NAMEHL_DEBOUNCE_MS + 140);
  check('注册：高亮链路已生效（≥4 条 Range，先证明开着）', !!registered() && registered().ranges.length >= 4,
    `${registered() && registered().ranges.length}`);
  check('红线：开高亮前后 getText() 逐字一致', ed.getText() === beforeText);
  const afterParas = Array.from(ed.el.querySelectorAll('p.edp')).map((p) => ({
    text: p.textContent,
    extraEls: Array.from(p.querySelectorAll('*')).filter((el) => el.tagName !== 'BR').length
  }));
  check('红线：段落 textContent 逐段一致（零文本污染）',
    JSON.stringify(afterParas.map((p) => p.text)) === JSON.stringify(beforeParas.map((p) => p.text)));
  check('红线：段落内零新增元素节点（除既有 BR 外无 span/mark/b）',
    afterParas.every((p) => p.extraEls === 0) && beforeParas.every((p) => p.extraEls === 0));
  const namehlSrc = fs.readFileSync(path.join(root, 'static/v2/js/core/namehl.js'), 'utf-8');
  check('红线：namehl.js 零 innerHTML / 零 insertAdjacentHTML（纯 Range 无 HTML 注入面）',
    !/innerHTML|insertAdjacentHTML|outerHTML/.test(namehlSrc));
  /* XSS 词也走字面匹配 */
  const r = computeNameRanges('正文里出现<img src=x>标签', ['<img src=x>']);
  check('红线：XSS 词纯字面匹配（不解析 HTML）',
    r.length === 1 && '正文里出现<img src=x>标签'.slice(r[0].start, r[0].end) === '<img src=x>');
  ctl.destroy();
  ed.destroy();
}

/* ================================================================
   8. 不支持环境（Safari<17.0 等）：静默降级不注册
   ================================================================ */
{
  const bk = 'bk-unsup';
  clearBookKeys(bk);
  GRAPH_PAYLOADS[bk] = ['张三丰'];
  clearNameCache();
  const hadHighlight = globalThis.Highlight;
  delete globalThis.Highlight;   /* 模拟 Chrome<105 / Safari<17.0 */
  check('能力检测：无 Highlight 构造器 → isHighlightSupported()=false', isHighlightSupported() === false);
  const before = registered();
  const ed = mountEditor();
  ed.setText('张三丰来了。', { silent: true });
  const ctl = mountNameHighlight({ editor: ed, getKey: () => bk, storage: localStorage, onHint: () => {} });
  await sleep(50);
  ctl.notifyText(ed.getText(), {});
  await sleep(NAMEHL_DEBOUNCE_MS + 120);
  check('降级：不支持环境不注册（注册表维持原状）', registered() === before);
  ctl.openPanel(h('anchor'));
  function h(tag) { return document.createElement(tag); }
  const panel = document.querySelector('.namehl-panel');
  check('降级：面板给出浏览器兼容提示（.nh-unsupport）',
    !!panel && !!panel.querySelector('.nh-unsupport'));
  check('降级：面板仍可维护词表（人物清单 + 自定义词区块在位）',
    !!panel && panel.querySelectorAll('.nh-item').length >= 1 && !!panel.querySelector('.nh-add-input'));
  ctl.destroy();
  ed.destroy();
  globalThis.Highlight = hadHighlight;
}

/* ================================================================
   9. 管理面板 DOM
   ================================================================ */
{
  const bk = 'bk-g';
  clearBookKeys(bk);
  GRAPH_PAYLOADS[bk] = ['李雷', '韩梅梅'];
  clearNameCache();
  fetchCalls.length = 0;
  saveMasterEnabled(true, localStorage);
  const hints = [];
  const ed = mountEditor();
  ed.setText('李雷叫住了韩梅梅。韩梅梅没有回头。', { silent: true });
  const ctl = mountNameHighlight({ editor: ed, getKey: () => bk, storage: localStorage, onHint: (m) => hints.push(String(m)) });
  await sleep(60);

  ctl.openPanel(document.createElement('button'));
  const panel = document.querySelector('.namehl-panel');
  check('面板：openPanel 后 .namehl-panel 挂 body（aria-label）',
    !!panel && panel.getAttribute('aria-label') === '人物名高亮管理');
  check('面板：总开关开关态与 isEnabled 同步（data-on=true）',
    panel.querySelector('.nh-master').dataset.on === 'true');
  check('面板：人物清单渲染（李雷/韩梅梅 2 条）',
    panel.querySelectorAll('.nh-item').length === 2
    && [...panel.querySelectorAll('.nh-item')].some((it) => it.dataset.name === '李雷'));

  /* 点击人物 → revealOffset 到第一处（spy 断言偏移正确；editor.focus 的选区行为
     属 editor.js 既有域，jsdom focus 会重置选区，这里只钉 namehl 的定位口径） */
  const revealCalls = [];
  const origReveal = ed.revealOffset.bind(ed);
  ed.revealOffset = (off) => { revealCalls.push(off); return origReveal(off); };
  const wantIdx = ed.getText().indexOf('韩梅梅');
  const itemHan = [...panel.querySelectorAll('.nh-item')].find((it) => it.dataset.name === '韩梅梅');
  itemHan.querySelector('.nh-item-main').dispatchEvent(new dom.window.Event('click', { bubbles: true }));
  check('面板：点击人物定位第一处（revealOffset 收到 indexOf 首现偏移）',
    revealCalls.length === 1 && revealCalls[0] === wantIdx,
    `got ${JSON.stringify(revealCalls)} want ${wantIdx}`);

  /* 搜索过滤 */
  const search = panel.querySelector('.nh-search-input');
  search.value = '韩';
  search.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  check('面板：搜索过滤（韩 → 1 条）', panel.querySelectorAll('.nh-item').length === 1
    && panel.querySelector('.nh-item').dataset.name === '韩梅梅');
  search.value = '';
  search.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  check('面板：清空搜索恢复全列表', panel.querySelectorAll('.nh-item').length === 2);

  /* 定位失败 → onHint：正文没有的词（自定义词路径），点击后不乱定位只提示 */
  const addPre = panel.querySelector('.nh-add-input');
  addPre.value = '张三丰';
  addPre.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  const itemMiss = [...panel.querySelectorAll('.nh-item')].find((it) => it.dataset.name === '张三丰');
  itemMiss.querySelector('.nh-item-main').dispatchEvent(new dom.window.Event('click', { bubbles: true }));
  check('面板：正文没有该人物 → onHint 提示（不乱定位）',
    hints.some((m) => m.includes('张三丰') && m.includes('没有出现')) && revealCalls.length === 1,
    JSON.stringify(hints));
  const delMiss = [...panel.querySelectorAll('.nh-item')].find((it) => it.dataset.name === '张三丰').querySelector('.nh-del');
  delMiss.dispatchEvent(new dom.window.Event('click', { bubbles: true }));

  /* 自定义词回车添加 */
  search.value = '';
  search.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  const addInput = panel.querySelector('.nh-add-input');
  addInput.value = '回头客';
  addInput.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  check('面板：回车添加自定义词（状态 + 持久化 + 输入框清空）',
    ctl.customWords().includes('回头客')
    && loadCustomWords(bk, localStorage).includes('回头客')
    && addInput.value === '');
  check('面板：自定义词区块出现新条目（带删除按钮）',
    [...panel.querySelectorAll('.nh-item')].some((it) => it.dataset.name === '回头客'
      && it.querySelector('.nh-del')));

  /* XSS 词：纯文本呈现，零元素注入 */
  addInput.value = '<img src=x>';
  addInput.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  check('红线：XSS 词零元素注入（无 img/script，仅文本）',
    panel.querySelectorAll('img').length === 0 && panel.querySelectorAll('script').length === 0
    && [...panel.querySelectorAll('.nh-item-main')].some((b) => b.textContent === '<img src=x>'));
  const delBtn = [...panel.querySelectorAll('.nh-item')].find((it) => it.dataset.name === '<img src=x>').querySelector('.nh-del');
  delBtn.dispatchEvent(new dom.window.Event('click', { bubbles: true }));
  check('面板：删除自定义词（XSS 词移除 + 持久化同步）',
    !ctl.customWords().includes('<img src=x>')
    && !loadCustomWords(bk, localStorage).includes('<img src=x>'));

  /* 单词开关（面板路径）：面板每次变更后重建列表 DOM，断言前重新查询节点 */
  const findItem = (name) => [...document.querySelector('.namehl-panel').querySelectorAll('.nh-item')]
    .find((it) => it.dataset.name === name);
  findItem('李雷').querySelector('.nh-toggle').dispatchEvent(new dom.window.Event('click', { bubbles: true }));
  check('面板：单词开关（aria-checked=false + off 名单持久化）',
    findItem('李雷').querySelector('.nh-toggle').getAttribute('aria-checked') === 'false'
    && loadNameOff(bk, localStorage).includes('李雷'));
  findItem('李雷').querySelector('.nh-toggle').dispatchEvent(new dom.window.Event('click', { bubbles: true }));
  check('面板：再点恢复开启（activeWords 收回）', ctl.activeWords().includes('李雷'));

  /* 总开关（面板路径） */
  panel.querySelector('.nh-master').dispatchEvent(new dom.window.Event('click', { bubbles: true }));
  check('面板：总开关切换（data-on=false + localStorage 记忆）',
    !ctl.isEnabled() && panel.querySelector('.nh-master').dataset.on === 'false'
    && localStorage.getItem(NAMEHL_MASTER_KEY) === '0');
  panel.querySelector('.nh-master').dispatchEvent(new dom.window.Event('click', { bubbles: true }));
  check('面板：总开关切回开（注册恢复）', ctl.isEnabled() && !!registered());

  /* 外点关闭 */
  document.body.dispatchEvent(new dom.window.Event('pointerdown', { bubbles: true }));
  check('面板：面板外 pointerdown 关闭', !document.querySelector('.namehl-panel'));

  ctl.destroy();
  ed.destroy();
}

/* 人物清单 30 条封顶 + 计数文案 */
{
  const bk = 'bk-caps';
  clearBookKeys(bk);
  GRAPH_PAYLOADS[bk] = Array.from({ length: 40 }, (_, i) => `人物${String(i + 1).padStart(2, '0')}`);
  clearNameCache();
  const ed = mountEditor();
  const ctl = mountNameHighlight({ editor: ed, getKey: () => bk, storage: localStorage, onHint: () => {} });
  await sleep(60);
  ctl.openPanel(document.createElement('button'));
  const panel = document.querySelector('.namehl-panel');
  check('面板：人物清单封顶 30 条（接口给 40）', panel.querySelectorAll('.nh-item').length === NAMEHL_PANEL_MAX);
  check('面板：计数文案（共 40 · 显示 30）',
    panel.querySelector('.nh-section .nh-count').textContent.includes('共 40')
    && panel.querySelector('.nh-section .nh-count').textContent.includes('显示 30'));
  ctl.closePanel();
  check('面板：closePanel 摘除面板', !document.querySelector('.namehl-panel'));
  ctl.destroy();
  ed.destroy();
}

/* ================================================================
   10. 性能：6000 字 × 50 名 Range 构建
   ================================================================ */
{
  const NAMES = Array.from({ length: 50 }, (_, i) => `人物${String(i + 1).padStart(2, '0')}号`);
  const paras = [];
  for (let i = 0; i < 40; i += 1) {
    let seg = '';
    while (seg.length < 150) {
      seg += `第${i}段写着${NAMES[(seg.length + i) % NAMES.length]}路过，又写了些别的凑字数。`;
    }
    paras.push(seg.slice(0, 150));
  }
  const bigText = paras.join('\n\n');
  if (bigText.length < 6000) paras.push('补'.repeat(6000 - bigText.length));
  const text6k = paras.join('\n\n');
  const t0 = process.hrtime.bigint();
  const ranges = computeNameRanges(text6k, NAMES);
  const t1 = process.hrtime.bigint();
  const ms = Number(t1 - t0) / 1e6;
  check('性能：6000 字 × 50 名 Range 构建 < 200ms（目标 <50ms）', ms < 200, `${ms.toFixed(2)}ms`);
  check('性能：结果正确性抽查（全部命中切片=词名 且 封顶内）',
    ranges.length > 0 && ranges.length <= NAMEHL_MAX_RANGES
    && ranges.every((r) => text6k.slice(r.start, r.end) === r.name),
    `ranges=${ranges.length} text=${text6k.length}`);
}

/* ================================================================
   11. 结构契约：CSS / bench.js 挂载点
   ================================================================ */
{
  const css = fs.readFileSync(path.join(root, 'static/v2/css/bench-editor.css'), 'utf-8');
  const namehlCss = css.slice(css.indexOf('v5.6.0 编辑器内人物名高亮'));
  check('样式契约：::highlight(character-name) 规则在 bench-editor.css 尾部就位',
    namehlCss.includes('::highlight(character-name)') && namehlCss.includes('.namehl-panel'));
  const colorDecls = namehlCss.match(/(?:^|[;{\n])\s*(?:color|background(?:-color)?|border(?:-color)?|outline(?:-color)?|fill)\s*:[^;}]+/g) || [];
  const varOnly = colorDecls.length > 0
    && colorDecls.every((d) => !/#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/.test(d));
  check('样式契约：namehl 区块颜色全部走语义变量（无 hex/rgb 字面量，四主题自适应）', varOnly,
    colorDecls.filter((d) => /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/.test(d)).slice(0, 3).join(' | '));

  const benchSrc = fs.readFileSync(path.join(root, 'static/v2/js/views/bench.js'), 'utf-8');
  check('结构契约：bench.js 挂载点（import + mountNameHighlight + 头部按钮 openPanel）在位',
    benchSrc.includes("import { mountNameHighlight } from '../core/namehl.js'")
    && benchSrc.includes('const namehl = mountNameHighlight({')
    && benchSrc.includes("namehl.openPanel(event.currentTarget)")
    && benchSrc.includes('<span>高亮</span>'));
  check('结构契约：bench.js 接线（onChange notifyText + 模式切换 refresh + destroy 清理）在位',
    benchSrc.includes('namehl.notifyText(text, ev || {});')
    && benchSrc.includes('namehl.refresh();')
    && benchSrc.includes('namehl.destroy();'));

  check('常量契约：注册名 / 键约定与测试钉死一致',
    NAMEHL_HL_NAME === 'character-name'
    && NAMEHL_MASTER_KEY === 'ew-name-hl-enabled'
    && NAMEHL_STORE_PREFIX === 'ew-name-hl:'
    && NAMEHL_OFF_PREFIX === 'ew-name-hl-off:'
    && NAMEHL_DEBOUNCE_MS === 120
    && NAMEHL_PANEL_MAX === 30
    && NAMEHL_MAX_WORD_LEN === 20
    && NAMEHL_MAX_NODES === 300);
}

/* ================================================================
   汇总
   ================================================================ */
console.log(`\n通过 ${passed} / ${passed + failed}`);
console.log(failed ? `${failed} 项失败` : '全部通过');
process.exit(failed ? 1 : 0);
