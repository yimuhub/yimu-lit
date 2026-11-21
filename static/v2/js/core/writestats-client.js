/* 码字统计客户端（v5.4.0 监听侧，自安装副作用模块）。
 *
 * 设计（docs/系统化升级方案-v5.1 第 6.1 节，全部自研）：
 *   · 监听 document 级 `yimu-editor-session`（{phase:'begin'|'update'|'commit', key, text}）：
 *       begin  = 记会话起点基线（当前字数）；
 *       update = 计算手写净增 = max(0, 当前字数 − 基线)，幂等覆盖写本地缓存并节流上报会话槽位；
 *       commit = 会话定稿：槽位 + 并入（/session → /commit）。
 *   · 监听 document 级 `yimu-ai-insert`（{chars, key}）：AI 字数并入当日 AI 槽，
 *       并调后端抬升该章基线——基线抬高后，后续 update 的手写净增里就不再包含 AI 字数（不双记）。
 *   · 删字不回扣已入账：会话槽位取「历史最大净增」，只增不减。
 *   · 崩溃兜底：beforeunload / visibilitychange / pagehide 时把未提交会话与待报 AI 用
 *       keepalive fetch 冲到后端；下次打开时结算本地遗留缓存（服务端另有跨天槽位自愈）。
 *   · 统计是附加能力：这里任何失败都只 try/catch 静默，绝不影响写作主链路。
 *
 * 挂载：主会话在 app.js 里 `import './core/writestats-client.js'` 一行即可（副作用自安装）。
 */

import { IS_MOCK } from './api.js';
import { store } from './state.js';

const LS_SESSION = 'ew-writestats-session';      // 当前会话本地缓存（幂等覆盖写）
const LS_AI_PENDING = 'ew-writestats-ai-pending'; // AI 入账待报队列（上报失败重试）
const FLUSH_DEBOUNCE_MS = 1600;                  // update 节流上报间隔
const CJK_RE = /[\u4e00-\u9fff]/g;

function countChars(text) {
  /* 字数口径与后端 bot/wordcount.py 一致：正文汉字数（标点/英文不计）。 */
  const m = String(text || '').match(CJK_RE);
  return m ? m.length : 0;
}

function todayStr() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function uid() {
  return `ws-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function lsGet(key) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function lsSet(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* 本地缓存失败不影响主链路 */
  }
}

function lsDel(key) {
  try {
    localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

/* 上报统一走 keepalive fetch：页面关闭时也能把最后一批数据冲出去。 */
function postJson(path, body, { keepalive = false } = {}) {
  return fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
    keepalive,
    credentials: 'same-origin'
  }).then((res) => {
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json().catch(() => ({}));
  });
}

/* 当前书与章：bench 编辑只发生在 #/work/<key>/bench/<n>；取不到就不记账（保守）。 */
function currentBook() {
  try {
    const m = String(location.hash || '').match(/^#\/work\/([^/]+)(?:\/bench\/(\d+))?/);
    if (m && m[1]) return { key: decodeURIComponent(m[1]), chapter: Number(m[2]) || 1 };
  } catch {
    /* ignore */
  }
  try {
    const key = store.get().currentKey;
    if (key) return { key, chapter: 1 };
  } catch {
    /* ignore */
  }
  return null;
}

/* ---------------------------------------------------------------- 会话与队列状态 */

let sess = null;          // {session, date, key, chapter, baseline, chars}
let aiPending = [];       // [{date, key, chapter, chars}]
let flushTimer = 0;
let committing = false;

function loadAiPending() {
  const v = lsGet(LS_AI_PENDING);
  aiPending = Array.isArray(v) ? v.filter((x) => x && x.key && x.chars > 0).slice(0, 200) : [];
}

function saveAiPending() {
  lsSet(LS_AI_PENDING, aiPending.slice(0, 200));
}

function cacheSession() {
  /* 幂等覆盖写本地缓存：崩溃后下次打开按这份结算遗留会话。 */
  if (sess) lsSet(LS_SESSION, { ...sess });
}

/* ---------------------------------------------------------------- 上报动作 */

function scheduleFlush() {
  if (flushTimer) return;
  flushTimer = setTimeout(() => {
    flushTimer = 0;
    flushSession();
    flushAi();
  }, FLUSH_DEBOUNCE_MS);
}

async function flushSession({ keepalive = false } = {}) {
  if (!sess || committing) return;
  try {
    await postJson('/api/writestats/session', {
      date: sess.date,
      key: sess.key,
      session: sess.session,
      chars: sess.chars
    }, { keepalive });
  } catch {
    /* 失败留待下次触发；本地缓存仍在 */
  }
}

async function flushAi() {
  if (!aiPending.length) return;
  const batch = aiPending.slice();
  for (const item of batch) {
    try {
      await postJson('/api/writestats/baseline', item);
      aiPending = aiPending.filter((x) => x !== item);
      saveAiPending();
    } catch {
      break; // 后端不可达：保留队列，下次再试
    }
  }
}

async function finalizeSession({ keepalive = false } = {}) {
  /* 会话定稿：先覆盖写槽位，再并入 manual；两步都成功才清本地缓存。
     commit 服务端幂等（墓碑表），重放不会双记。 */
  if (!sess || committing) return;
  committing = true;
  const done = sess;
  try {
    await postJson('/api/writestats/session', {
      date: done.date, key: done.key, session: done.session, chars: done.chars
    }, { keepalive });
    await postJson('/api/writestats/commit', {
      date: done.date, key: done.key, session: done.session
    }, { keepalive });
    if (sess === done) {
      sess = null;
      lsDel(LS_SESSION);
    }
  } catch {
    /* 留着本地缓存与槽位，下次打开结算 */
  }
  committing = false;
}

/* ---------------------------------------------------------------- 事件处理 */

function newSession(book, nowCount) {
  return {
    session: uid(),
    date: todayStr(),
    key: book.key,
    chapter: book.chapter,
    baseline: nowCount,   // 会话起点基线（当前字数），AI 插入时抬升
    chars: 0,             // 手写累计（删字不回扣；AI 归账修正除外）
    pendingAi: 0,         // 已插入但尚未随 update/commit 归账的 AI 字数（原始长度）
    lastCount: nowCount   // 最近一次看到的编辑器字数
  };
}

/* AI 归账：AI 文本可能先于 ai-insert 事件随 update 进过手写累计（编辑器 change 与
   ai-insert 同一任务先后派发，流式续写更是多次进账）。抬基线后，把「手写累计 −
   真实净增」里属于这批 AI 的部分退回；退回量以 pendingAi（本批 AI 原始长度）封顶，
   并保证不缩到当前真实净增以下——AI 槽同时 +chars，手写+AI 总量不降，只是口径换位。 */
function reconcileAi(sessObj, net) {
  if (!(sessObj.pendingAi > 0)) return false;
  const clean = Math.max(net, Math.max(0, sessObj.chars - sessObj.pendingAi));
  if (clean < sessObj.chars) sessObj.chars = clean;
  sessObj.pendingAi = 0;
  return true;
}

function onEditorSession(detail) {
  const phase = detail && detail.phase;
  const text = String((detail && detail.text) || '');
  const book = currentBook();
  if (!book) return;
  const nowCount = countChars(text);

  if (phase === 'begin') {
    // 上一段会话还在（切章/换书没 commit）→ 先并入，再开新会话
    if (sess) void finalizeSession();
    sess = newSession(book, nowCount);
    cacheSession();
    scheduleFlush();
    return;
  }

  if (phase === 'update') {
    if (!sess) return; // 没有 begin 就不记账（保守，防重复事件源）
    // 换了书：旧会话并入，本书从头记（基线=当前字数，净增 0 起步）
    if (sess.key !== book.key) {
      void finalizeSession();
      sess = newSession(book, nowCount);
      cacheSession();
      scheduleFlush();
      return;
    }
    /* 手写净增 = max(0, 当前字数 − 基线)；基线已被 AI 插入抬高，AI 字数不会进手写（不双记）。
       删字不回扣已入账：净增变小不缩会话累计（AI 归账修正除外）。 */
    sess.lastCount = nowCount;
    const net = Math.max(0, nowCount - sess.baseline);
    const reconciled = reconcileAi(sess, net);
    if (net > sess.chars) {
      sess.chars = net;
      cacheSession();
      scheduleFlush();
    } else if (reconciled) {
      cacheSession();
      scheduleFlush();
    }
    return;
  }

  if (phase === 'commit') {
    if (!sess) return;
    sess.lastCount = nowCount;
    const net = Math.max(0, nowCount - sess.baseline);
    if (!reconcileAi(sess, net) && net > sess.chars) {
      sess.chars = net;
    }
    cacheSession();
    void finalizeSession();
  }
}

function onAiInsert(detail) {
  const chars = Math.max(0, Math.floor(Number(detail && detail.chars) || 0));
  if (!chars) return;
  const book = currentBook();
  if (!book) return;
  // 抬基线：这批 AI 字数从手写净增里排除（后续 update 的手写净增 = max(0, 当前字数 − 基线)）
  if (sess && sess.key === book.key) {
    sess.baseline += chars;
    sess.pendingAi += chars; // 是否已随 update 进过手写，留到下一次 update/commit 归账
    cacheSession();
  }
  aiPending.push({ date: todayStr(), key: book.key, chapter: book.chapter, chars });
  if (aiPending.length > 200) aiPending = aiPending.slice(-200);
  saveAiPending();
  void flushAi();
}

/* 崩溃兜底：页面隐藏/关闭时把未提交会话与待报 AI 冲到后端。 */
function onPageHide() {
  flushSession({ keepalive: true });
  void flushAi();
}

/* ---------------------------------------------------------------- 启动安装 */

function settleLeftover() {
  /* 上次崩溃/强退遗留的本地会话缓存：结算进后端（upsert → commit，幂等可重放）。 */
  const c = lsGet(LS_SESSION);
  if (!c || !c.session || !c.key || !(Number(c.chars) > 0)) {
    if (c) lsDel(LS_SESSION);
    return;
  }
  const date = typeof c.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(c.date) ? c.date : todayStr();
  try {
    void postJson('/api/writestats/session', { date, key: c.key, session: c.session, chars: Number(c.chars) || 0 })
      .then(() => postJson('/api/writestats/commit', { date, key: c.key, session: c.session }))
      .then(() => {
        if (lsGet(LS_SESSION) && lsGet(LS_SESSION).session === c.session) lsDel(LS_SESSION);
      })
      .catch(() => {
        /* 服务端不可达：缓存保留，下次再结算 */
      });
  } catch {
    /* ignore */
  }
}

function install() {
  if (IS_MOCK) return; // 演示模式绝不穿透真实后端（功能表开发规范 7）
  if (typeof document === 'undefined') return; // node 环境（测试/工具）不安装
  try {
    loadAiPending();
    settleLeftover();
    document.addEventListener('yimu-editor-session', (e) => {
      try {
        onEditorSession(e.detail);
      } catch {
        /* 统计失败不影响写作 */
      }
    });
    document.addEventListener('yimu-ai-insert', (e) => {
      try {
        onAiInsert(e.detail);
      } catch {
        /* ignore */
      }
    });
    window.addEventListener('pagehide', onPageHide);
    window.addEventListener('beforeunload', onPageHide);
    document.addEventListener('visibilitychange', () => {
      try {
        if (document.visibilityState === 'hidden') onPageHide();
      } catch {
        /* ignore */
      }
    });
  } catch {
    /* 安装失败也不影响主链路 */
  }
}

install();

/* 调试 API（仅诊断用；生产代码不依赖）。 */
export function writestatsDebug() {
  return {
    session: sess ? { ...sess } : null,
    aiPending: aiPending.slice(),
    countChars,
    flush: () => Promise.allSettled([flushSession(), flushAi()])
  };
}
