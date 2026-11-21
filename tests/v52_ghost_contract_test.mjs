// v5.2.0 AI 辅助写作 · 事件契约测试（node 直跑，jsdom 仿真，不开浏览器）。
//
// 跨 Agent 事件契约（另一 Agent 并行开发统计功能，依赖这两条，不许破坏）：
//   1) yimu-ai-insert   —— AI 生成文本插入编辑器后派发 detail { chars, key }；
//      本文件覆盖两条插入路径：幽灵字 Tab 采纳、快捷续写（Cmd/Ctrl+/）SSE 流入结束。
//   2) yimu-editor-session —— bench 会话上报 begin/update/commit 时序：
//      begin=章节装载完成；update=防抖落盘变化（节流 ≤1 次/秒 + 内容去重）；
//      commit=切章/卸载；commit 后不再派发 update。
//
// 顺带守住：幽灵字是「渲染层存在、模型层不存在」的虚拟节点（显示期间 getText
// 不含灰字）；Tab 采纳才入模；Esc/任何输入清除；续写运行期 pluginLock 开合。
import { JSDOM } from 'jsdom';
import path from 'path';
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
function check(name, cond, extra = '') {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed += 1;
}

/* ------------------------------------------------ fetch 桩：/api/ghost 与 /api/quickwrite */

const GHOST_TEXT = '他抬起头，目光落在窗外的雨幕上。';
const QUICK_TEXT = '夜色沉了下来。他数着自己的脚步。';

function jsonResponse(obj, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: async () => obj,
    text: async () => JSON.stringify(obj)
  };
}

function sseResponse(frames, { signal = null, gate = null } = {}) {
  const enc = new TextEncoder();
  let i = 0;
  return {
    ok: true,
    status: 200,
    headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'text/event-stream' : null) },
    body: {
      getReader: () => ({
        read: async () => {
          if (signal && signal.aborted) {
            const e = new Error('aborted');
            e.name = 'AbortError';
            throw e;
          }
          if (gate && i === gate.atIndex) await gate.promise;
          if (i < frames.length) return { value: enc.encode(frames[i++]), done: false };
          return { done: true };
        },
        releaseLock() { /* 桩无需释放 */ }
      })
    }
  };
}

function sse(event, data) {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

const fetchCalls = [];
global.fetch = async (url, init = {}) => {
  fetchCalls.push({ url: String(url), init });
  const u = String(url);
  if (u.includes('/api/ghost')) {
    return jsonResponse({ ok: true, text: GHOST_TEXT, mode: 'beat', chars: GHOST_TEXT.length });
  }
  throw new Error('unexpected fetch: ' + u);
};

/* ------------------------------------------------ 模块装载（全局桩就位之后） */

const { createEditor } = await import(pathToFileURL(path.join(root, 'static/v2/js/core/editor.js')).href);
const { mountGhost } = await import(pathToFileURL(path.join(root, 'static/v2/js/core/ghost.js')).href);
const { mountQuickWrite } = await import(pathToFileURL(path.join(root, 'static/v2/js/core/quickwrite.js')).href);
const { createSessionReporter, emitAiInsert } = await import(pathToFileURL(path.join(root, 'static/v2/js/views/bench.js')).href);

const host = document.getElementById('host');
const sleepEditorCaret = (ed) => {
  ed.focusEnd();
  ed.setCaretOffset(ed.getText().length);   // jsdom 的 focus() 会重置选区：补一刀（真实浏览器不需要）
};

/* ================================================================
   契约 2：bench 会话事件 begin / update / commit 时序
   ================================================================ */
{
  const seen = [];
  const onSession = (e) => seen.push({ ...e.detail });
  document.addEventListener('yimu-editor-session', onSession);

  const rep = createSessionReporter('k1');
  rep.begin('第一段的正文。');
  rep.update('第一段的正文。改了。');        // 首个 update：派发
  rep.update('第一段的正文。又改了。');      // 1s 内：节流抑制
  await sleep(1100);
  rep.update('第一段的正文。第三次改。');    // 过 1s：派发
  rep.update('第一段的正文。第四次改。');    // 1s 内：抑制
  rep.commitIfActive('第一段的正文。第三次改。');   // 切章结算
  rep.update('不该再派发。');                // commit 后：静默
  rep.commitIfActive('重复 commit 不得派发');
  await sleep(1100);
  rep.update('commit 后延迟输入也不派发');

  check('会话事件：begin→update→update→commit 时序', seen.map((x) => x.phase).join(',') === 'begin,update,update,commit',
    JSON.stringify(seen.map((x) => x.phase)));
  check('会话事件：detail 携带 key 与全文 text',
    seen[0].key === 'k1' && seen[0].text === '第一段的正文。' && seen[1].text === '第一段的正文。改了。',
    JSON.stringify(seen[0]));
  check('会话事件：update 节流 ≤1 次/秒', seen.length === 4, `共 ${seen.length} 条`);
  document.removeEventListener('yimu-editor-session', onSession);
}

/* ================================================================
   契约 1：幽灵字 Tab 采纳 → yimu-ai-insert 派发字符数
   ================================================================ */
{
  const baseText = '雨停之前，他把伞收了起来。';
  const insertEvents = [];
  const onInsert = (e) => insertEvents.push({ ...e.detail });
  document.addEventListener('yimu-ai-insert', onInsert);

  host.textContent = '';
  const ed = createEditor({ host });
  /* 先挂幽灵字（基线为空），再装正文——模拟 bench 真实时序：挂载 → 章节装载 → 手打 */
  const ghostCtl = mountGhost({
    editor: ed,
    getContext: () => ({ key: 'k1', n: 3, title: '第3章 试探', summary: '' }),
    onHint: () => {},
    onInsert: ({ chars, key } = {}) => emitAiInsert(chars, key),   // bench 侧接线同款
    options: { debounceMin: 30, debounceMax: 60, minInterval: 0, minTyped: 3, requestTimeout: 4000 }
  });
  ed.setText(baseText, { silent: true });

  /* 模拟本轮手打 ≥3 字：末尾停顿后触发请求 */
  sleepEditorCaret(ed);
  ghostCtl.notifyText(ed.getText(), {});
  await sleep(220);

  check('幽灵字：请求命中 /api/ghost 且带 key', fetchCalls.some((c) => c.url.includes('/api/ghost')), '');
  check('幽灵字：光标末尾停顿后出现灰字建议', ed.ghostActive() === true);
  const span = host.querySelector('.ghost-text');
  check('幽灵字：灰字 span 渲染（.ghost-text）且内容正确', !!span && span.textContent === GHOST_TEXT, span && span.textContent);
  check('幽灵字：「Tab 采纳」键帽提示在位', !!host.querySelector('.ghost-key') &&
    host.querySelector('.ghost-key').textContent.includes('Tab'));
  check('幽灵字：显示期间不进正文模型（纯净模型口径）', ed.getText() === baseText);

  /* Tab 采纳：灰字入模 + 契约事件派发 */
  const tabEvent = new dom.window.KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
  host.querySelector('.edp').dispatchEvent(tabEvent);
  check('幽灵字：Tab 采纳后建议写入正文模型', ed.getText() === baseText + GHOST_TEXT,
    JSON.stringify(ed.getText().slice(baseText.length)));
  check('幽灵字：采纳后建议摘除', ed.ghostActive() === false && !host.querySelector('.ghost-text'));
  check('契约 1：yimu-ai-insert 派发 chars=灰字长度、key=书 key',
    insertEvents.length === 1 && insertEvents[0].chars === GHOST_TEXT.length && insertEvents[0].key === 'k1',
    JSON.stringify(insertEvents));

  /* Esc 清除：再起一条建议，Esc 后消失且不派发插入事件 */
  sleepEditorCaret(ed);
  ed.setGhost('第二条建议。');
  ed.el.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
  check('幽灵字：Esc 清除建议', ed.ghostActive() === false && ed.getGhost() === '');
  check('契约 1：Esc 取消不派发插入事件', insertEvents.length === 1);

  /* 任何输入清除：再起建议，模拟键入（DOM 文本变化 + input 事件） */
  sleepEditorCaret(ed);
  ed.setGhost('第三条建议。');
  const p = host.querySelector('.edp');
  p.textContent = p.textContent.replace(/。$/, '') + '用户打了新字。';
  p.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  check('幽灵字：任何输入清除建议', ed.ghostActive() === false);
  check('幽灵字：输入本身不受影响（模型含新字）', ed.getText().includes('用户打了新字。'));

  ghostCtl.destroy();
  ed.destroy();
  document.removeEventListener('yimu-ai-insert', onInsert);
}

/* ================================================================
   契约 1：快捷续写（Cmd/Ctrl+/）SSE 流入结束 → yimu-ai-insert 派发
   ================================================================ */
{
  const baseText = '巷子深处没有灯。';
  const insertEvents = [];
  const hints = [];
  const onInsert = (e) => insertEvents.push({ ...e.detail });
  document.addEventListener('yimu-ai-insert', onInsert);

  /* 重挂 fetch：/api/quickwrite 返回 SSE 帧 */
  const frames = [
    sse('start', { ts: 1 }),
    sse('delta', { text: '夜色' }),
    sse('delta', { text: '沉了下来。' }),
    sse('delta', { text: '他数着' }),
    sse('delta', { text: '自己的脚步。' }),
    sse('done', { ok: true, text: QUICK_TEXT, chars: QUICK_TEXT.length, elapsed: 0.1 })
  ];
  const signals = [];
  global.fetch = async (url, init = {}) => {
    fetchCalls.push({ url: String(url), init });
    if (String(url).includes('/api/quickwrite')) {
      signals.push(init.signal || null);
      return sseResponse(frames, { signal: init.signal || null });
    }
    throw new Error('unexpected fetch: ' + url);
  };

  host.textContent = '';
  const ed = createEditor({ host });
  ed.setText(baseText, { silent: true });
  const qw = mountQuickWrite({
    editor: ed,
    getContext: () => ({ key: 'k1', n: 3, title: '第3章 试探', summary: '' }),
    onHint: (m) => hints.push(m),
    onInsert: ({ chars, key } = {}) => emitAiInsert(chars, key)
  });

  sleepEditorCaret(ed);
  /* Cmd/Ctrl+/ 触发：目标在编辑器内 */
  ed.el.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: '/', metaKey: true, bubbles: true, cancelable: true }));
  check('快捷续写：流入期间 pluginLock 打开（划词冻结/幽灵字悬挂）', ed.isPluginLocked() === true);
  await sleep(400);

  check('快捷续写：SSE 逐字插入正文模型', ed.getText() === baseText + QUICK_TEXT,
    JSON.stringify(ed.getText()));
  check('契约 1：续写流入结束派发 chars=插入字数、key=书 key',
    insertEvents.length === 1 && insertEvents[0].chars === QUICK_TEXT.length && insertEvents[0].key === 'k1',
    JSON.stringify(insertEvents));
  check('快捷续写：结束后 pluginLock 复位', ed.isPluginLocked() === false);
  check('快捷续写：有完成轻提示', hints.some((m) => m.includes('续写完成')), hints.join('|'));
  qw.destroy();
  ed.destroy();
  document.removeEventListener('yimu-ai-insert', onInsert);
}

/* ================================================================
   快捷续写：流入期间用户改动 → 停在当前进度，不派发插入事件
   ================================================================ */
{
  const baseText = '雨是从傍晚开始下的。';
  const insertEvents = [];
  const hints = [];
  const onInsert = (e) => insertEvents.push({ ...e.detail });
  document.addEventListener('yimu-ai-insert', onInsert);

  /* gate：第 2 个 delta 帧等待测试放行，期间模拟用户改动正文 */
  let releaseGate;
  const gatePromise = new Promise((r) => { releaseGate = r; });
  const gate = { atIndex: 2, promise: gatePromise };
  const frames = [
    sse('start', { ts: 1 }),
    sse('delta', { text: '他抬起头' }),
    sse('delta', { text: '，雨幕里有人影。' }),
    sse('delta', { text: '后半段不该流入。' }),
    sse('done', { ok: true, text: '他抬起头，雨幕里有人影。后半段不该流入。', chars: 20, elapsed: 0.1 })
  ];
  global.fetch = async (url, init = {}) => {
    if (String(url).includes('/api/quickwrite')) return sseResponse(frames, { signal: init.signal || null, gate });
    throw new Error('unexpected fetch: ' + url);
  };

  host.textContent = '';
  const ed = createEditor({ host });
  ed.setText(baseText, { silent: true });
  const qw = mountQuickWrite({
    editor: ed,
    getContext: () => ({ key: 'k2', n: 1, title: '', summary: '' }),
    onHint: (m) => hints.push(m),
    onInsert: ({ chars, key } = {}) => emitAiInsert(chars, key)
  });
  sleepEditorCaret(ed);
  ed.el.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: '/', ctrlKey: true, bubbles: true, cancelable: true }));

  /* 等第一片流入完成 */
  for (let i = 0; i < 50 && !ed.getText().endsWith('他抬起头'); i += 1) await sleep(10);
  check('快捷续写：第一片流入到位', ed.getText() === baseText + '他抬起头', JSON.stringify(ed.getText()));

  /* 用户在流入期间改动正文（在开头插字，破坏偏移差前缀） */
  ed.setCaretOffset(0);
  ed.insertAtCaret('（旁白）');
  releaseGate();
  await sleep(300);

  check('快捷续写：流入期间用户改动 → 停在当前进度（后半段不流入）',
    !ed.getText().includes('后半段不该流入。'), JSON.stringify(ed.getText()));
  check('快捷续写：已流入部分保留', ed.getText().includes('他抬起头'));
  check('契约 1：中途停止不派发插入事件', insertEvents.length === 0, JSON.stringify(insertEvents));
  check('快捷续写：停止提示给出', hints.some((m) => m.includes('停止')), hints.join('|'));
  check('快捷续写：停止后 pluginLock 复位', ed.isPluginLocked() === false);
  qw.destroy();
  ed.destroy();
  document.removeEventListener('yimu-ai-insert', onInsert);
}

/* ================================================================
   汇总
   ================================================================ */
console.log(failed ? `\n${failed} 项失败` : '\n全部通过');
process.exit(failed ? 1 : 0);
