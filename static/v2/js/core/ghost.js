/* 幽灵字触发节制（v5.2.0 AI 辅助写作）：光标后的灰色建议字，Tab 一键采纳。
 *
 * 触发门（全部满足才请求，任何一项不满足绝不打扰输入）：
 * · 光标停在正文末尾（模型偏移 === 全文长度）；
 * · 距上次输入停顿 1200–1800ms（随机防抖，输入即重排）；
 * · 距上次请求 ≥8s；
 * · 本轮新输入 ≥12 字（采纳进来的 AI 字不计数）；
 * · 429 / 连续失败 → 冷却（服务端给了 cooldown 秒数用服务端的，否则 60s）；
 * · 流式续写等插件运行期（editor.isPluginLocked()）整体悬挂。
 *
 * 请求路径：POST /api/ghost（bot/webai.py → bot/ghost.py，产出已过 strip_fragment
 * 纯净域口径）。失败静默 + onHint 轻提示（状态栏），绝不影响正常输入。
 * mock 模式（?mock=1）：本地演示建议（不走网络），交互可验收。
 */

import { request, IS_MOCK } from './api.js';

/* 触发参数：与 bot/ghost.py 的服务端间隔门口径一致（MIN_INTERVAL_SECS）。 */
const DEFAULTS = {
  debounceMin: 1200,      // 随机防抖下限 ms
  debounceMax: 1800,      // 随机防抖上限 ms
  minInterval: 8000,      // 距上次请求最小间隔 ms
  minTyped: 12,           // 本轮新输入门槛（字）
  cooldownMs: 60000,      // 限流冷却默认 60s
  requestTimeout: 12000
};

/* mock 演示建议：按模式自判挑一句（与 bot/ghost.py 的两种模式呼应）。 */
const MOCK_BEAT = [
  '他抬起头，目光越过窗棂落在院里那棵老槐树上。',
  '屋里的灯忽然闪了一下，像是谁在暗处轻轻叹了口气。',
  '她把茶盏放下，指尖在桌沿轻轻敲了两下。',
  '风从半开的窗缝里钻进来，吹得烛火一矮。'
];
const MOCK_HALF = [
  '声音忽然低了下去，',
  '像是在斟酌接下来的字句，',
  '手里的动作却没停，',
  '眼神里透出一点不易察觉的疲惫，'
];

function mockPick(list) {
  return list[Math.floor(Math.random() * list.length)] || list[0];
}

async function mockGhost(text, { signal } = {}) {
  const tail = (text || '').trim().slice(-2);
  const mode = /[。！？…!?”』」》—]$/.test(tail) ? 'beat' : 'half';
  const body = mode === 'beat' ? mockPick(MOCK_BEAT) : mockPick(MOCK_HALF);
  await new Promise((resolve) => {
    const t = setTimeout(resolve, 320);
    if (signal) signal.addEventListener('abort', () => { clearTimeout(t); resolve(); }, { once: true });
  });
  if (signal && signal.aborted) {
    const e = new Error('已取消');
    e.code = 'aborted';
    throw e;
  }
  return { ok: true, text: body, mode, label: mode === 'beat' ? '续一拍' : '补半句', chars: body.length };
}

export function mountGhost({
  editor,
  getContext = null,
  onHint = null,
  onInsert = null,
  options = {}
} = {}) {
  const opts = { ...DEFAULTS, ...(options || {}) };
  let destroyed = false;
  let timer = 0;              // 随机防抖计时器
  let pending = false;        // 请求在途
  let abort = null;
  let lastRequestTs = 0;      // 距上次请求 ≥8s
  let typedSince = 0;         // 本轮新输入（采纳的 AI 字不计数）
  let lastLen = 0;            // 输入差值基准（正文模型长度）
  let cooldownUntil = 0;      // 限流冷却截止

  const ctx = () => {
    try { return (typeof getContext === 'function' && getContext()) || {}; }
    catch { return {}; }
  };
  const hint = (msg) => { try { if (onHint) onHint(msg); } catch { /* 提示绝不打断输入 */ } };

  /* ---------------------------------------------------------- 基线同步 */

  function resync() {
    /* 正文被整批替换（切章/版本恢复/模式切换/续写流入）后调用：差值基准重置。 */
    lastLen = editor.getText().length;
    typedSince = 0;
    clearTimeout(timer);
    timer = 0;
  }

  /* ---------------------------------------------------------- 防抖调度 */

  function schedule() {
    if (destroyed) return;
    clearTimeout(timer);
    const wait = opts.debounceMin + Math.random() * (opts.debounceMax - opts.debounceMin);
    timer = setTimeout(() => { timer = 0; tryRequest(); }, wait);
  }

  function tryRequest() {
    if (destroyed || pending) return;
    if (editor.isPluginLocked()) return;               // 续写流入期整体悬挂
    if (editor.isComposing() || editor.ghostActive()) return;
    const text = editor.getText();
    if (!text.trim()) return;                          // 空章不起幽灵字（起笔交给作者）
    const off = editor.getCaretOffset();
    if (off < 0 || off !== text.length) return;        // 光标必须在正文末尾
    const now = Date.now();
    if (now < cooldownUntil) return;
    if (now - lastRequestTs < opts.minInterval) return;
    if (typedSince < opts.minTyped) return;            // 本轮新输入不足，先不动
    requestSuggestion(text);
  }

  /* ---------------------------------------------------------- 请求 */

  async function requestSuggestion(text) {
    pending = true;
    abort = new AbortController();
    lastRequestTs = Date.now();
    const c = ctx();
    const body = {
      key: c.key || '',
      text,
      title: c.title || '',
      summary: c.summary || '',
      n: c.n || 0
    };
    try {
      const r = IS_MOCK
        ? await mockGhost(text, { signal: abort.signal })
        : await request('/api/ghost', { method: 'POST', body, signal: abort.signal, timeout: opts.requestTimeout });
      if (destroyed || (abort && abort.signal.aborted)) return;
      /* 请求期间正文/光标动了 → 本次建议作废（绝不把灰字接到过期位置上） */
      const cur = editor.getText();
      const off = editor.getCaretOffset();
      if (cur !== text || off < 0 || off !== cur.length || editor.isComposing()) return;
      const t = r && typeof r.text === 'string' ? r.text : '';
      if (!t.trim()) { hint('幽灵字：模型这次没给出建议'); return; }
      editor.setGhost(t);
    } catch (err) {
      if (err && (err.isAbort || err.code === 'aborted')) return;
      const status = err && err.status;
      const cd = Number(err && err.payload && err.payload.cooldown);
      if (status === 429) {
        const secs = cd > 0 ? cd : opts.cooldownMs / 1000;
        cooldownUntil = Date.now() + secs * 1000;
        hint(`幽灵字休息一下：${Math.max(1, Math.round(secs))}s 后再试`);
      } else if (status === 404 || status === 501) {
        hint('幽灵字需要新版本后端（/api/ghost 未挂载），已静默跳过');
      } else {
        hint('幽灵字暂不可用，已静默跳过');
      }
    } finally {
      pending = false;
      abort = null;
      typedSince = 0;    // 一轮结束（无论成败），本轮输入计数清零
    }
  }

  /* ---------------------------------------------------------- 编辑器联动 */

  /* bench 的 onChange 转发进来（text, {composing, adopting}）：
     输入差值计数 + 防抖重排 + 在途请求即时作废。 */
  function notifyText(text, ev = {}) {
    if (destroyed) return;
    if (ev.adopting) {
      /* Tab 采纳：灰字转正，AI 字不计入手打；基线对齐新长度后重新轮询。 */
      lastLen = editor.getText().length;
      typedSince = 0;
      lastRequestTs = Date.now();
      clearTimeout(timer);
      schedule();
      return;
    }
    if (editor.isPluginLocked()) {
      lastLen = editor.getText().length;   // 续写流入期：只跟基线，不触发
      return;
    }
    if (ev.composing) return;              // 组词中不重排（compositionend 会再进）
    if (typeof text === 'string') {
      const len = text.length;
      typedSince += Math.max(0, len - lastLen);
      lastLen = len;
    }
    if (pending && abort) { try { abort.abort(); } catch { /* 已结束 */ } }
    schedule();
  }

  /* Tab 采纳回调（editor 内部触发）：通知 bench 派发 AI 插入事件 + 立即落盘。 */
  editor.setGhostAdoptListener((result) => {
    if (!result) return;
    lastRequestTs = Date.now();
    lastLen = editor.getText().length;
    typedSince = 0;
    try { if (onInsert) onInsert({ chars: result.chars, key: ctx().key || '' }); }
    catch { /* 统计上报失败不影响采纳 */ }
    clearTimeout(timer);
    schedule();
  });
  /* 建议消失（Esc/方向键/输入/光标移走）：清挂起标记，防抖随后自然重排。 */
  editor.setGhostDismissListener(() => { /* 建议已摘除；输入路径会重排 */ });

  resync();

  return {
    notifyText,
    resync,
    suspend() { clearTimeout(timer); if (pending && abort) { try { abort.abort(); } catch { /* 已结束 */ } } },
    stats: () => ({ typedSince, cooling: Math.max(0, cooldownUntil - Date.now()) }),
    destroy() {
      destroyed = true;
      clearTimeout(timer);
      if (pending && abort) { try { abort.abort(); } catch { /* 已结束 */ } }
      editor.setGhostAdoptListener(null);
      editor.setGhostDismissListener(null);
    }
  };
}
