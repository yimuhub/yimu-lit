/* 快捷流式续写（v5.2.0 AI 辅助写作）：Cmd/Ctrl+/ 在光标处流式逐字插入编辑器。
 *
 * 位置追踪——「全文偏移差」：
 * · 触发时快照 basePrefix（光标前全文）与插入点 off；
 * · 每个 delta 校验「basePrefix + 已流入文本」仍是当前全文前缀，
 *   不匹配 = 用户在流入期间改了正文 → 立即停在当前进度（已流入部分保留）；
 * · 插入动作 = 光标定位到 off+已流入长度 → insertAtCaret（单段走原生撤销栈，
 *   跨段走模型级 replaceRange），插入完成后光标自然停在流入前沿。
 *
 * 运行期约束：
 * · editor.setPluginLock(true)：划词气泡冻结（selectionchange 不派发）、幽灵字悬挂；
 * · 批量任务运行中（jobfeed 图层有未完成卡片）拒绝启动，流入途中出现也自动中止；
 * · 切章/卸载由 bench 调 stop() 中止；
 * · 结束后与后端清洗文本（strip_fragment 口径）对账，不一致就地替换流入区段。
 *
 * mock 模式（?mock=1）：本地分片模拟流式输出，交互可验收。
 */

import { request, streamSSE, IS_MOCK } from './api.js';

/* mock 演示续写稿：与幽灵字演示同风格的中性叙述段（演示口径：流式原文与清洗稿一致）。 */
const MOCK_STREAM_TEXT = '夜色一点点沉下来，街角的灯把他的影子拉得很长。他数着自己的脚步，'
  + '像数一段还没写完的句子。风里有点凉，也有点熟悉的味道——那是这条街每个夜晚'
  + '都会升起的人间烟火气。';

const MOCK_CLEAN_TEXT = MOCK_STREAM_TEXT;

/* 批量任务运行检测：core/jobfeed.js 的任务卡片层有未完成卡片即视为忙
   （卡片结束时 dataset.finished='1'）。不引模块依赖，纯 DOM 信号。 */
function batchRunning() {
  try {
    const layer = document.querySelector('[data-yimu-job-layer]');
    if (!layer) return false;
    return Array.from(layer.children).some((el) => el.dataset && el.dataset.finished !== '1');
  } catch {
    return false;
  }
}

async function mockQuickWrite({ onEvent, signal } = {}) {
  const emit = (event, data) => { try { if (onEvent) onEvent(event, data); } catch { /* 帧回调不阻断 */ } };
  emit('start', { ts: Date.now() });
  const text = MOCK_STREAM_TEXT;
  const step = 3;
  for (let i = 0; i < text.length; i += step) {
    if (signal && signal.aborted) {
      const e = new Error('已取消');
      e.code = 'aborted';
      throw e;
    }
    emit('delta', { text: text.slice(i, i + step) });
    await new Promise((resolve) => {
      const t = setTimeout(resolve, 24);
      if (signal) signal.addEventListener('abort', () => { clearTimeout(t); resolve(); }, { once: true });
    });
  }
  emit('done', { ok: true, text: MOCK_CLEAN_TEXT, chars: MOCK_CLEAN_TEXT.length, elapsed: 0.1 });
  return { ok: true, text: MOCK_CLEAN_TEXT };
}

export function mountQuickWrite({
  editor,
  getContext = null,
  onHint = null,
  onInsert = null,
  onStart = null,
  onEnd = null
} = {}) {
  let destroyed = false;
  let active = false;
  let abort = null;

  const ctx = () => {
    try { return (typeof getContext === 'function' && getContext()) || {}; }
    catch { return {}; }
  };
  const hint = (msg) => { try { if (onHint) onHint(msg); } catch { /* 提示绝不打断输入 */ } };

  /* ---------------------------------------------------------- 停止/清理 */

  function stop({ reason = '' } = {}) {
    if (abort) { try { abort.abort(); } catch { /* 已结束 */ } }
    if (active) {
      active = false;
      editor.setPluginLock(false);
      try { if (onEnd) onEnd({ reason }); } catch { /* 通知失败不影响 */ }
      if (reason) hint(reason);
    }
  }

  /* ---------------------------------------------------------- 流入 */

  async function start() {
    if (destroyed || active) return;
    if (editor.isComposing()) return;
    if (batchRunning()) {
      hint('批量任务运行中，续写暂不可用');
      return;
    }
    const c = ctx();
    const text = editor.getText();
    const sel = editor.currentSel();
    let off = sel && sel.start >= 0 ? sel.start : text.length;
    if (off > text.length) off = text.length;
    const basePrefix = text.slice(0, off);

    active = true;
    abort = new AbortController();
    editor.setPluginLock(true);
    try { if (onStart) onStart(); } catch { /* 通知失败不影响 */ }
    hint('续写中…（再次 Cmd/Ctrl+/ 或改动正文可停止）');

    let acc = '';
    let stopped = false;
    let lastErr = '';

    /* 单片流入：校验前缀 intact → 定位插入 → 光标停在流入前沿。 */
    const flowPiece = (piece, prevLen) => {
      const cur = editor.getText();
      const want = basePrefix + acc.slice(0, prevLen);
      if (!cur.startsWith(want)) return false;   // 用户改动：停在当前进度
      editor.setCaretOffset(off + prevLen);
      editor.insertAtCaret(piece);               // 内部 _emitChange → bench 落盘管线
      return true;
    };

    try {
      const payload = { key: c.key || '', text, title: c.title || '', summary: c.summary || '', n: c.n || 0 };
      /* SSE 与本地模拟共用的帧处理：delta 流入正文，error 记录待展示。 */
      const onStreamEvent = (event, data) => {
        if (event === 'delta' && data && data.text) {
          if (batchRunning()) {                // 流入途中批量任务启动：自动中止
            stopped = true;
            stop({ reason: '检测到批量任务启动，续写已自动中止' });
            return;
          }
          const piece = String(data.text);
          const prevLen = acc.length;
          acc += piece;
          if (!flowPiece(piece, prevLen) && !stopped) {
            stopped = true;
            stop({ reason: '续写已停止：正文在流入期间发生了改动（已流入部分保留）' });
          }
        } else if (event === 'error' && data) {
          lastErr = String(data.error || '续写失败');
        }
      };
      const final = IS_MOCK
        ? await mockQuickWrite({ onEvent: onStreamEvent, signal: abort.signal })
        : await streamSSE('/api/quickwrite', payload, {
          signal: abort.signal,
          onEvent: onStreamEvent
        });
      if (destroyed) return;
      if (!active) return;                        // 已被 stop()（切章/用户改动/批量任务）
      /* 结束对账：后端清洗文本（strip_fragment 口径）与流入原文不一致 → 就地替换。 */
      const cleaned = String((final && final.text) || acc || '');
      if (!cleaned.trim()) {
        hint(lastErr || '续写结果为空，已取消本次流入');
        return;
      }
      if (cleaned !== acc) {
        const cur = editor.getText();
        if (cur.startsWith(basePrefix + acc)) {
          editor.replaceRange(off, off + acc.length, cleaned);
          acc = cleaned;
        }
      }
      const cur = editor.getText();
      if (!cur.startsWith(basePrefix + acc)) {
        hint('续写完成，但流入区段已被后续改动覆盖（保留现状）');
        return;
      }
      try { if (onInsert) onInsert({ chars: acc.length, key: c.key || '' }); }
      catch { /* 统计上报失败不影响 */ }
      hint(`续写完成（${acc.length} 字）`);
    } catch (err) {
      if (err && (err.isAbort || err.code === 'aborted')) return;
      const status = err && err.status;
      if (status === 404 || status === 501) hint('快捷续写需要新版本后端（/api/quickwrite 未挂载）');
      else hint(lastErr || '续写失败，已保留已流入部分');
    } finally {
      if (active) {
        active = false;
        editor.setPluginLock(false);
        try { if (onEnd) onEnd({}); } catch { /* 忽略 */ }
      }
      abort = null;
    }
  }

  /* ---------------------------------------------------------- 快捷键 */

  const onKeydown = (event) => {
    if (destroyed) return;
    if (!(event.metaKey || event.ctrlKey) || event.altKey) return;
    if (event.key !== '/' && event.code !== 'Slash') return;
    /* 只在编辑器书写场景触发：事件目标/焦点都在编辑器内，聊天输入框等不受影响。 */
    const target = event.target;
    const focusEl = document.activeElement;
    const inside = (el) => !!(el && editor.el && (el === editor.el || editor.el.contains(el)));
    if (!inside(target) && !inside(focusEl)) return;
    event.preventDefault();
    if (active) {
      /* 流入中再按一次 = 停止（保留已流入部分） */
      stop({ reason: '续写已停止（保留已流入部分）' });
      return;
    }
    start();
  };
  document.addEventListener('keydown', onKeydown);

  return {
    isActive: () => active,
    stop: () => stop({}),
    destroy() {
      destroyed = true;
      document.removeEventListener('keydown', onKeydown);
      stop({});
    }
  };
}
