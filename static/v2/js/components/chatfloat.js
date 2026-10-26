/* 作品工作台的「对话改稿」悬浮窗：收起时是右下角按钮，展开后可拖动，只改指定文档的某一段。
   剧本 / 诗词 / 散文共用一套：doc 定位文档（剧本=场号，诗词散文=定稿/初稿），block 定位段。 */

import { h, icon, uid } from '../core/dom.js';
import { api, errorText } from '../core/api.js';
import { toast } from '../core/ui.js';

const STORE_KEY = 'shenbi.v2.chatfloat';
const MAX_MESSAGES = 40;
const EDGE = 8;
const PANEL_W = 420;
const PANEL_H = 560;

/* 诗词/散文可改的文档只有定稿与草稿两种，后端 doc id 就是这两个词。 */
const DOC_LABELS = { final: '定稿', draft: '草稿' };

function readSaved() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
    return raw && typeof raw === 'object' ? raw : {};
  } catch {
    return {};
  }
}

function numOrNull(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.round(n) : null;
}

function savedNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/* doc 统一成字符串：剧本是场号（"3"），诗词散文是文档 id（"final"/"draft"）。 */
function docId(value) {
  if (value == null || value === '') return '';
  return String(value).trim();
}

function normalizeMedium(value) {
  const m = String(value == null ? '' : value).trim();
  return m === 'poetry' || m === 'prose' ? m : 'script';
}

/* 目标文案：剧本说「第 N 场」，诗词散文说文档名（定稿/草稿）。 */
function docLabel(medium, id, title) {
  const key = docId(id);
  if (!key) return '';
  if (medium === 'script') return `第 ${key} 场`;
  return title ? String(title) : DOC_LABELS[key] || key;
}

function patchTitleOf(patch, medium) {
  const parts = [];
  const label = docLabel(medium, patch.doc, patch.doc_title);
  if (label) parts.push(label);
  if (patch.block) parts.push(`第 ${patch.block} 段`);
  return parts.length ? parts.join(' · ') : '修改建议';
}

/* 留档里的时间戳是 ISO 串（2026-10-26T14:36:12），显示时去掉中间的 T。 */
function fmtTs(value) {
  const s = String(value == null ? '' : value).trim();
  if (!s) return '';
  return s.replace('T', ' ').slice(0, 19);
}

export function mountChatFloat({ getContext, onApplied, root } = {}) {
  const mountRoot =
    root && root.nodeType === 1 ? root : document.getElementById('app') || document.body;
  const panelId = uid('cf-panel');

  let disposed = false;
  let open = false;
  let busy = false;
  let controller = null;
  let dragging = null;
  let focusTimer = 0;
  let sessionId = '';
  let currentKey = '';
  let currentMedium = 'script';
  let manualFocus = false;
  let pos = { x: null, y: null };
  /* 目标：doc 定位文档，block 定位段；剧本的 doc 是场号，诗词散文的是 final/draft。 */
  let focus = { doc: '', block: null, title: '' };
  /* 记录视图：recordsMode 是当前视图，recordsKey 记住已拉取的是哪部作品的哪文体。 */
  let recordsMode = false;
  let recordsKey = '';

  const stored = readSaved();
  const startX = savedNumber(stored.x);
  const startY = savedNumber(stored.y);
  if (startX != null && startY != null) pos = { x: startX, y: startY };

  const messages = [];
  const nodes = new Map();

  /* ------------------------------------------------------------- context */

  function readContext() {
    if (typeof getContext !== 'function') return null;
    try {
      const ctx = getContext();
      return ctx && typeof ctx === 'object' ? ctx : null;
    } catch {
      return null;
    }
  }

  /* 旧的调用方传 scene/sceneTitle，新契约传 doc/docTitle；两个都要认。 */
  function contextTarget(ctx) {
    const rawDoc = ctx.doc == null ? ctx.scene : ctx.doc;
    const rawTitle = ctx.docTitle == null ? ctx.sceneTitle : ctx.docTitle;
    return {
      doc: docId(rawDoc),
      block: numOrNull(ctx.block),
      title: rawTitle ? String(rawTitle) : ''
    };
  }

  function paintFocus() {
    const label = docLabel(currentMedium, focus.doc, focus.title);
    const text = label
      ? `目标：${label}${focus.block ? ` · 第 ${focus.block} 段` : ''}`
      : '未指定目标';
    chipText.textContent = text;
    chip.dataset.empty = label ? 'false' : 'true';
    /* 剧本的 title 是场景头，值得补在气泡后面；诗词散文它就是文档名，重复了反而啰嗦。 */
    const extra = currentMedium === 'script' && focus.title ? ` · ${focus.title}` : '';
    chip.title = label ? `${text}${extra}` : '未指定目标；说清要改哪一段，我只改那一段';
  }

  function paintEmpty() {
    emptyTextEl.textContent =
      currentMedium === 'script'
        ? '说『把第 3 场第 2 段改得更紧张』，我只改那一段；其他内容不动。'
        : '说『把定稿第 2 段改得更凝练』，我只改那一段；其他内容不动。';
    input.placeholder =
      currentMedium === 'script' ? '把第 3 场第 2 段改得更紧张…' : '把定稿第 2 段改得更凝练…';
  }

  function syncContext() {
    const ctx = readContext();
    if (!ctx) return null;
    const nextKey = ctx.key == null ? '' : String(ctx.key);
    if (nextKey && currentKey && nextKey !== currentKey) {
      /* 换了作品，上一轮会话与目标都不该沿用。 */
      manualFocus = false;
      sessionId = '';
    }
    if (nextKey) currentKey = nextKey;
    const medium = normalizeMedium(ctx.medium);
    if (medium !== currentMedium) {
      /* 文体变了，提示文案与记录口径都要跟着变。 */
      currentMedium = medium;
      recordsKey = '';
      paintEmpty();
    }
    if (!manualFocus) {
      focus = contextTarget(ctx);
      paintFocus();
    }
    return ctx;
  }

  function setFocus(next = {}) {
    manualFocus = true;
    const rawDoc = next.doc == null ? next.scene : next.doc;
    const rawTitle = next.docTitle == null ? next.sceneTitle : next.docTitle;
    focus = {
      doc: docId(rawDoc),
      block: numOrNull(next.block),
      title: rawTitle ? String(rawTitle) : ''
    };
    paintFocus();
  }

  /* ---------------------------------------------------------------- DOM */

  const chipText = h('span', { class: 'cf-chip-text', text: '未指定目标' });
  const chip = h(
    'span',
    { class: 'cf-chip', dataset: { empty: 'true' } },
    h('span', { class: 'cf-chip-ico', html: icon('target', 12) }),
    chipText
  );
  const focusbar = h('div', { class: 'cf-focusbar' }, chip);

  const collapseBtn = h(
    'button',
    {
      class: 'cf-collapse',
      type: 'button',
      'aria-label': '收起对话窗',
      title: '收起对话窗',
      on: { click: () => closePanel() }
    },
    h('span', { html: icon('chevron-down', 16) })
  );

  /* 记录视图：对话历史与改稿记录都留档在作品目录里，换浏览器也还在。 */
  const recordsBtn = h(
    'button',
    {
      class: 'cf-tab',
      type: 'button',
      dataset: { active: 'false' },
      'aria-label': '查看改稿记录与对话历史',
      'aria-pressed': 'false',
      title: '查看改稿记录与对话历史',
      on: { click: () => toggleRecords() }
    },
    h('span', { class: 'cf-tab-ico', html: icon('history', 14) }),
    h('span', { class: 'cf-tab-text', text: '记录' })
  );

  const head = h(
    'div',
    { class: 'cf-head', title: '拖动可移动窗口' },
    h(
      'div',
      { class: 'cf-title' },
      h('span', { class: 'cf-title-ico', html: icon('quill', 14) }),
      h('span', { class: 'cf-title-text', text: '对话改稿' })
    ),
    recordsBtn,
    collapseBtn
  );

  const emptyTextEl = h('p', { class: 'cf-empty-text', text: '' });
  const emptyEl = h(
    'div',
    { class: 'cf-empty' },
    h('span', { class: 'cf-empty-ico', html: icon('message', 15) }),
    emptyTextEl
  );

  const thread = h('div', { class: 'cf-thread', role: 'log', 'aria-label': '对话记录' });
  thread.append(emptyEl);

  const input = h('textarea', {
    class: 'cf-input',
    rows: 2,
    placeholder: '把第 3 场第 2 段改得更紧张…',
    'aria-label': '给 Agent 的改稿指令',
    spellcheck: 'false'
  });

  const sendIco = h('span', { class: 'cf-send-ico', html: icon('send', 14) });
  const stopIco = h('span', { class: 'cf-send-ico', html: icon('stop', 13), hidden: true });
  const sendLabel = h('span', { class: 'cf-send-label', text: '发送' });
  const sendBtn = h(
    'button',
    {
      class: 'cf-send',
      type: 'button',
      dataset: { mode: 'send' },
      'aria-label': '发送',
      title: '发送（Enter）',
      on: { click: () => (busy ? stopStream() : send()) }
    },
    sendIco,
    stopIco,
    sendLabel
  );

  const compose = h(
    'div',
    { class: 'cf-compose' },
    input,
    h(
      'div',
      { class: 'cf-compose-foot' },
      h('span', { class: 'cf-kbd-hint', text: 'Enter 发送 · Shift+Enter 换行' }),
      sendBtn
    )
  );

  const recordsList = h('div', { class: 'cf-rec-list' });
  const recordsBody = h(
    'div',
    {
      class: 'cf-records',
      role: 'region',
      'aria-label': '改稿记录与对话历史',
      hidden: true
    },
    h(
      'div',
      { class: 'cf-rec-top' },
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        html: `${icon('chevron-left', 13)}<span>返回对话</span>`,
        on: { click: () => toggleRecords(false) }
      })
    ),
    recordsList
  );

  const panel = h(
    'div',
    {
      class: 'cf-panel',
      id: panelId,
      role: 'dialog',
      'aria-label': '对话改稿',
      tabindex: '-1',
      hidden: true
    },
    head,
    focusbar,
    thread,
    compose,
    recordsBody
  );

  const fab = h(
    'button',
    {
      class: 'cf-fab',
      type: 'button',
      'aria-label': '打开对话改稿',
      title: '对话改稿',
      'aria-haspopup': 'dialog',
      'aria-controls': panelId,
      on: { click: () => openPanel() }
    },
    h('span', { class: 'cf-fab-ico', html: icon('quill', 17) }),
    h('span', { class: 'cf-fab-label', text: '对话改稿' })
  );

  const rootEl = h('div', { class: 'cf-root', dataset: { open: 'false' } }, fab, panel);
  mountRoot.append(rootEl);

  /* ------------------------------------------------------------ messages */

  function stick(force = false) {
    if (force || thread.scrollHeight - thread.scrollTop - thread.clientHeight < 90) {
      thread.scrollTop = thread.scrollHeight;
    }
  }

  function paintMessage(msg) {
    const refs = nodes.get(msg.id);
    if (!refs || msg.role !== 'assistant') return;
    const statusText = msg.streaming ? msg.status || '正在处理…' : '';
    refs.text.textContent = msg.content;
    refs.status.hidden = !statusText;
    refs.statusText.textContent = statusText;
    refs.bubble.hidden = !msg.content && !statusText && !msg.error;
    refs.error.hidden = !msg.error;
    refs.errorText.textContent = msg.error || '';
    refs.notice.hidden = !msg.notice;
    refs.noticeText.textContent = msg.notice || '';
    stick();
  }

  function buildUserMessage(msg) {
    const wrap = h('div', { class: 'cf-msg cf-msg-user', dataset: { id: msg.id } });
    wrap.append(h('div', { class: 'cf-user-bubble', text: msg.content }));
    nodes.set(msg.id, { wrap });
    return wrap;
  }

  function buildAssistantMessage(msg) {
    const statusText = h('span', { class: 'cf-status-text', text: msg.status || '' });
    const status = h(
      'div',
      { class: 'cf-status', role: 'status', hidden: !msg.status },
      h('span', { class: 'cf-spin', 'aria-hidden': 'true' }),
      statusText
    );
    const text = h('div', { class: 'cf-text', text: msg.content || '' });
    const bubble = h('div', { class: 'cf-ai-bubble' }, status, text);
    const errorTextEl = h('span');
    const error = h(
      'div',
      { class: 'cf-note', dataset: { tone: 'danger' }, role: 'alert', hidden: true },
      h('span', { class: 'cf-note-ico', html: icon('alert', 12) }),
      errorTextEl
    );
    const noticeTextEl = h('span');
    const notice = h(
      'div',
      { class: 'cf-note', dataset: { tone: 'muted' }, hidden: true },
      h('span', { class: 'cf-note-ico', html: icon('info', 12) }),
      noticeTextEl
    );
    const main = h('div', { class: 'cf-ai-main' }, bubble, error, notice);
    const wrap = h(
      'div',
      { class: 'cf-msg cf-msg-ai', dataset: { id: msg.id } },
      h('span', { class: 'cf-avatar', html: icon('sparkles', 13) }),
      main
    );
    nodes.set(msg.id, {
      wrap,
      main,
      bubble,
      text,
      status,
      statusText,
      error,
      errorText: errorTextEl,
      notice,
      noticeText: noticeTextEl,
      hint: null
    });
    return wrap;
  }

  function addMessage(msg) {
    messages.push(msg);
    if (messages.length > MAX_MESSAGES) {
      const dropped = messages.shift();
      const refs = nodes.get(dropped.id);
      if (refs) refs.wrap.remove();
      nodes.delete(dropped.id);
    }
    if (emptyEl.parentNode) emptyEl.remove();
    const node = msg.role === 'user' ? buildUserMessage(msg) : buildAssistantMessage(msg);
    thread.append(node);
    return node;
  }

  function historyFor(key) {
    const items = [];
    for (const msg of messages) {
      if (msg.key !== key || msg.streaming || !msg.content) continue;
      if (msg.role !== 'user' && msg.role !== 'assistant') continue;
      items.push({ role: msg.role, content: msg.content });
    }
    return items;
  }

  /* --------------------------------------------------------------- patch */

  function normalizePatch(raw, msg) {
    if (!raw || typeof raw !== 'object') return null;
    const nextText =
      typeof raw.new === 'string' ? raw.new : typeof raw.new_text === 'string' ? raw.new_text : null;
    if (nextText == null) return null;
    /* 后端对剧本会同时给 doc 与 scene，这里以 doc 为准。 */
    const rawDoc = raw.doc == null ? raw.scene : raw.doc;
    const focusDoc = msg.focus ? msg.focus.doc : '';
    const rawBlock = numOrNull(raw.block);
    const focusBlock = msg.focus ? numOrNull(msg.focus.block) : null;
    return {
      state: 'idle',
      doc: docId(rawDoc) || docId(focusDoc),
      block: rawBlock || focusBlock || null,
      base_hash: raw.base_hash ? String(raw.base_hash) : '',
      old: typeof raw.old === 'string' ? raw.old : '',
      new: nextText,
      doc_title: raw.doc_title ? String(raw.doc_title) : '',
      note: ''
    };
  }

  function refreshPatch(msg) {
    const ui = msg.patchUI;
    const patch = msg.patch;
    if (!ui || !patch) return;
    const state = patch.state || 'idle';
    const locked = state !== 'idle';
    ui.card.dataset.state = state;
    ui.applyBtn.disabled = locked;
    ui.retryBtn.disabled = locked;
    ui.discardBtn.disabled = locked;
    ui.applyBtn.textContent = state === 'applying' ? '应用中…' : '应用';
    ui.applyBtn.setAttribute('aria-busy', state === 'applying' ? 'true' : 'false');
    const shown = state === 'applied' || state === 'discarded';
    ui.stateEl.hidden = !shown;
    ui.stateEl.dataset.state = state;
    ui.stateEl.textContent = state === 'applied' ? '已应用' : state === 'discarded' ? '已丢弃' : '';
    ui.noteEl.hidden = !patch.note;
    ui.noteEl.textContent = patch.note || '';
  }

  async function applyPatch(msg) {
    const patch = msg.patch;
    if (!patch || patch.state !== 'idle') return;
    const key = msg.key || currentKey;
    const medium = msg.medium || currentMedium;
    if (!key || !patch.doc || !patch.block) {
      toast('缺少文档或段落信息，无法应用这次修改', { tone: 'danger' });
      return;
    }
    patch.state = 'applying';
    patch.note = '';
    refreshPatch(msg);
    try {
      const res = await api.assistApply({
        key,
        medium,
        doc: patch.doc,
        block: patch.block,
        base_hash: patch.base_hash,
        new_text: patch.new
      });
      patch.state = 'applied';
      refreshPatch(msg);
      toast(`已应用，仅第 ${patch.block} 段发生变化`, { tone: 'success' });
      if (typeof onApplied === 'function') {
        try {
          onApplied({
            doc: patch.doc,
            block: patch.block,
            medium,
            text: res && typeof res.text === 'string' ? res.text : ''
          });
        } catch {
          /* 宿主刷新失败不该把已落盘的结果说成失败 */
        }
      }
    } catch (err) {
      /* 409 等冲突原样展示后端文案（"这段内容已被修改，请刷新后重试"），
         卡片留着让用户决定丢弃还是再改。 */
      patch.state = 'idle';
      patch.note = errorText(err, '应用失败，请重试');
      refreshPatch(msg);
      toast(patch.note, { tone: 'danger' });
    }
  }

  function discardPatch(msg) {
    const patch = msg.patch;
    if (!patch || patch.state !== 'idle') return;
    patch.state = 'discarded';
    patch.note = '';
    refreshPatch(msg);
  }

  function retryPatch(msg) {
    const patch = msg.patch;
    if (!patch || patch.state !== 'idle') return;
    input.value = `不满意，再改一次：${msg.instruction || ''}`;
    autoGrow();
    if (input.disabled) return;
    input.focus();
    const end = input.value.length;
    try {
      input.setSelectionRange(end, end);
    } catch {
      /* ignore */
    }
  }

  function attachPatch(msg) {
    const refs = nodes.get(msg.id);
    const patch = msg.patch;
    if (!refs || !patch || msg.patchUI) return;
    const medium = msg.medium || currentMedium;
    const title = patchTitleOf(patch, medium);
    /* 副标题只在比标题更细时给（剧本的场景头）；诗词散文两者都是文档名，重复了反而啰嗦。 */
    const headLabel = docLabel(medium, patch.doc, '');
    const sub = patch.doc_title && patch.doc_title !== headLabel ? patch.doc_title : '';
    const titleRow = h(
      'div',
      { class: 'cf-patch-head' },
      h('span', { class: 'cf-patch-title', text: title }),
      sub ? h('span', { class: 'cf-patch-scene', text: sub }) : null
    );

    const oldRow = h(
      'div',
      { class: 'cf-diff' },
      h(
        'div',
        { class: 'cf-diff-label' },
        h('span', { class: 'cf-diff-dot', dataset: { kind: 'old' } }),
        h('span', { text: '原文' })
      ),
      h('div', { class: 'cf-diff-old', text: patch.old || '（空）' })
    );
    const newRow = h(
      'div',
      { class: 'cf-diff' },
      h(
        'div',
        { class: 'cf-diff-label' },
        h('span', { class: 'cf-diff-dot', dataset: { kind: 'new' } }),
        h('span', { text: '新文' })
      ),
      h('div', { class: 'cf-diff-new', text: patch.new || '（空）' })
    );

    const noteEl = h('div', { class: 'cf-patch-note', role: 'alert', hidden: true });
    const stateEl = h('span', { class: 'cf-patch-state', hidden: true });
    const applyBtn = h('button', {
      class: 'cf-btn cf-btn-primary',
      type: 'button',
      text: '应用',
      title: '只替换这一段',
      'aria-label': `应用${title}的修改`
    });
    const retryBtn = h('button', {
      class: 'cf-btn cf-btn-ghost',
      type: 'button',
      text: '再改一次',
      title: '把原指令回填到输入框，不自动发送',
      'aria-label': '把原指令回填到输入框，再改一次'
    });
    const discardBtn = h('button', {
      class: 'cf-btn cf-btn-quiet',
      type: 'button',
      text: '丢弃',
      title: '丢弃这次修改',
      'aria-label': '丢弃这次修改'
    });
    const actions = h(
      'div',
      { class: 'cf-patch-actions' },
      applyBtn,
      retryBtn,
      discardBtn,
      stateEl
    );
    const card = h(
      'div',
      { class: 'cf-patch', role: 'group', 'aria-label': `${title}的修改建议` },
      titleRow,
      oldRow,
      newRow,
      noteEl,
      actions
    );
    refs.main.insertBefore(card, refs.error);
    msg.patchUI = { card, applyBtn, retryBtn, discardBtn, stateEl, noteEl };
    applyBtn.addEventListener('click', () => applyPatch(msg));
    retryBtn.addEventListener('click', () => retryPatch(msg));
    discardBtn.addEventListener('click', () => discardPatch(msg));
    refreshPatch(msg);
  }

  function attachClarifyHint(msg) {
    const refs = nodes.get(msg.id);
    if (!refs || refs.hint || !msg.intent || msg.intent.kind !== 'clarify') return;
    const text =
      (msg.medium || currentMedium) === 'script'
        ? '请补充「第几场第几段」，我就能只改那一段。'
        : '请补充「第几段」，我就能只改那一段。';
    refs.hint = h(
      'div',
      { class: 'cf-hint' },
      h('span', { class: 'cf-hint-ico', html: icon('message', 12) }),
      h('span', { text })
    );
    refs.main.insertBefore(refs.hint, refs.error);
  }

  /* -------------------------------------------------------------- stream */

  function paintFocusTargetInPayload() {
    return { doc: focus.doc || null, block: focus.doc ? focus.block : null };
  }

  function finishMessage(msg, data = {}) {
    if (!msg.streaming) return;
    msg.streaming = false;
    const done = data && typeof data === 'object' ? data : {};
    /* done.text 是后端最终稿，优先于累计 delta。 */
    if (typeof done.text === 'string') msg.content = done.text;
    msg.status = '';
    if (done.session_id) sessionId = String(done.session_id);
    if (done.ok === false && !msg.error) {
      msg.error = done.error ? String(done.error) : '这次没有生成成功，请再试一次';
    }
    if (done.intent && typeof done.intent === 'object') msg.intent = done.intent;
    const patch = normalizePatch(done.patch, msg);
    if (patch) {
      msg.patch = patch;
      attachPatch(msg);
    }
    paintMessage(msg);
    if (msg.intent && msg.intent.kind === 'clarify') attachClarifyHint(msg);
    stick();
  }

  function handleEvent(msg, event, data) {
    const payload = data && typeof data === 'object' ? data : {};
    if (event === 'start') {
      if (payload.session_id) sessionId = String(payload.session_id);
      return;
    }
    if (event === 'status') {
      msg.status = payload.text
        ? String(payload.text)
        : payload.stage === 'writing'
          ? '正在改写…'
          : '正在想…';
      paintMessage(msg);
      return;
    }
    if (event === 'delta') {
      const chunk = typeof data === 'string' ? data : typeof payload.text === 'string' ? payload.text : '';
      if (!chunk) return;
      msg.content += chunk;
      paintMessage(msg);
      return;
    }
    if (event === 'error') {
      msg.error = payload.error ? String(payload.error) : '生成失败，请重试';
      paintMessage(msg);
      return;
    }
    if (event === 'done') finishMessage(msg, payload);
  }

  function stopStream() {
    if (controller) {
      try {
        controller.abort();
      } catch {
        /* ignore */
      }
    }
  }

  function autoGrow() {
    input.style.height = 'auto';
    const next = Math.min(116, Math.max(38, input.scrollHeight || 38));
    input.style.height = `${next}px`;
  }

  function setBusy(next) {
    busy = next;
    input.disabled = next;
    sendBtn.dataset.mode = next ? 'stop' : 'send';
    sendIco.hidden = next;
    stopIco.hidden = !next;
    sendLabel.textContent = next ? '停止' : '发送';
    sendBtn.setAttribute('aria-label', next ? '停止生成' : '发送');
    sendBtn.title = next ? '停止生成' : '发送（Enter）';
  }

  async function send() {
    if (busy || disposed) return;
    const message = input.value.trim();
    if (!message) {
      input.focus();
      return;
    }
    const ctx = syncContext();
    if (!currentKey && ctx && ctx.key) currentKey = String(ctx.key);
    if (!currentKey) {
      toast('请先打开一部作品，再对话改稿', { tone: 'danger' });
      input.focus();
      return;
    }
    const key = currentKey;
    const medium = currentMedium;
    const target = paintFocusTargetInPayload();
    const history = historyFor(key);

    const userMsg = { id: uid('cfm'), role: 'user', key, content: message };
    const aiMsg = {
      id: uid('cfm'),
      role: 'assistant',
      key,
      medium,
      content: '',
      focus: { doc: target.doc, block: target.block, title: focus.title },
      instruction: message,
      streaming: true,
      status: '正在想…',
      error: '',
      notice: '',
      intent: null,
      patch: null
    };
    addMessage(userMsg);
    addMessage(aiMsg);
    input.value = '';
    autoGrow();
    setBusy(true);
    stick(true);

    const ctrl = new AbortController();
    controller = ctrl;
    const payload = { key, medium, message, focus: target, history };
    if (sessionId) payload.session_id = sessionId;

    try {
      const finalData = await api.assistStream(payload, {
        signal: ctrl.signal,
        onEvent: (event, data) => {
          if (!disposed) handleEvent(aiMsg, event, data);
        }
      });
      if (aiMsg.streaming) finishMessage(aiMsg, finalData && typeof finalData === 'object' ? finalData : {});
    } catch (err) {
      if (disposed) return;
      if (err && (err.isAbort || err.code === 'aborted' || err.name === 'AbortError')) {
        aiMsg.notice = '已停止生成';
      } else {
        aiMsg.error = errorText(err, '对话失败，请重试');
      }
      if (aiMsg.streaming) finishMessage(aiMsg, {});
      else paintMessage(aiMsg);
    } finally {
      if (!disposed) {
        if (aiMsg.streaming) finishMessage(aiMsg, {});
        controller = null;
        setBusy(false);
      }
    }
  }

  /* ------------------------------------------------------------- records */

  function recordSection(title, count) {
    const body = h('div', { class: 'cf-rec-sec-body' });
    const sec = h(
      'div',
      { class: 'cf-rec-sec' },
      h(
        'div',
        { class: 'cf-rec-sec-head' },
        h('span', { class: 'cf-rec-sec-title', text: title }),
        h('span', { class: 'cf-rec-sec-count', text: String(count) })
      ),
      body
    );
    return { sec, body };
  }

  function recordMessage(item) {
    const isUser = item.role === 'user';
    const content = String(item.content || '');
    return h(
      'div',
      { class: 'cf-rec-item', dataset: { role: isUser ? 'user' : 'ai' } },
      h(
        'div',
        { class: 'cf-rec-head' },
        h('span', { class: 'cf-rec-role', text: isUser ? '用户' : '助手' }),
        h('span', { class: 'cf-rec-time', text: fmtTs(item.ts) })
      ),
      h('div', { class: 'cf-rec-text', text: content })
    );
  }

  function recordEdit(item) {
    const block = Number(item.block) || 0;
    const title = item.doc_title || DOC_LABELS[String(item.doc || '')] || String(item.doc || '');
    /* 回退留档的 block 是 0（整篇替换），这时只说文档名。 */
    const head = block > 0 ? `${title} · 第 ${block} 段` : title;
    const oldText = String(item.old || '');
    const newText = String(item.new || '');
    const words = Number(item.words) || 0;
    const itemEl = h(
      'div',
      { class: 'cf-rec-item', dataset: { role: 'edit' } },
      h(
        'div',
        { class: 'cf-rec-head' },
        h('span', { class: 'cf-rec-role', text: head }),
        h('span', { class: 'cf-rec-time', text: fmtTs(item.ts) })
      ),
      words ? h('div', { class: 'cf-rec-meta', text: `全文字数 ${words}` }) : null,
      h(
        'div',
        { class: 'cf-rec-diff' },
        h('div', { class: 'cf-rec-old', title: oldText, text: oldText || '（空）' }),
        h('span', { class: 'cf-rec-arrow', text: '→' }),
        h('div', { class: 'cf-rec-new', title: newText, text: newText || '（空）' })
      )
    );
    if (item.version) {
      const btn = h('button', {
        class: 'cf-btn cf-btn-ghost',
        type: 'button',
        text: '回退到这版',
        title: '把这一版的内容写回文档',
        on: { click: () => restoreEdit(item, btn) }
      });
      itemEl.append(h('div', { class: 'cf-rec-actions' }, btn));
    }
    return itemEl;
  }

  function renderRecords(data) {
    recordsList.textContent = '';
    const messages = Array.isArray(data.messages) ? data.messages.slice() : [];
    const edits = Array.isArray(data.edits) ? data.edits.slice() : [];
    /* 留档里 messages 是追加顺序，改稿记录已经倒序；这里统一成新的在上。 */
    messages.reverse();
    if (!messages.length && !edits.length) {
      recordsList.append(
        h('div', { class: 'cf-rec-empty', text: '暂无记录：这部作品还没跟副驾说过话，也没有落过改稿。' })
      );
      return;
    }
    if (messages.length) {
      const sec = recordSection('对话历史', messages.length);
      for (const item of messages) sec.body.append(recordMessage(item));
      recordsList.append(sec.sec);
    }
    if (edits.length) {
      const sec = recordSection('改稿记录', edits.length);
      for (const item of edits) sec.body.append(recordEdit(item));
      recordsList.append(sec.sec);
    }
  }

  /* 记录是懒加载的：只有打开记录视图才去问后端要这份作品的留档。 */
  async function loadRecords(force = false) {
    const key = currentKey;
    if (!key) {
      recordsList.textContent = '';
      recordsList.append(h('div', { class: 'cf-rec-empty', text: '先打开一部作品，这里才有记录。' }));
      return;
    }
    const rk = `${key}|${currentMedium}`;
    if (!force && rk === recordsKey) return;
    recordsList.textContent = '';
    recordsList.append(h('div', { class: 'cf-rec-loading', text: '正在读取记录…' }));
    try {
      const res = await api.assistHistory(key, currentMedium);
      recordsKey = rk;
      renderRecords(res || {});
    } catch (err) {
      recordsList.textContent = '';
      recordsList.append(h('div', { class: 'cf-rec-empty', text: errorText(err, '记录读取失败') }));
    }
  }

  async function restoreEdit(item, btn) {
    if (!currentKey) {
      toast('没有作品上下文，无法回退', { tone: 'danger' });
      return;
    }
    btn.disabled = true;
    try {
      const res = await api.assistRestore({
        key: currentKey,
        medium: currentMedium,
        doc: item.doc,
        version: item.version
      });
      toast(`已回退到 ${item.version}`, { tone: 'success' });
      await loadRecords(true);
      if (typeof onApplied === 'function') {
        try {
          onApplied({
            doc: item.doc,
            block: null,
            medium: currentMedium,
            text: res && typeof res.text === 'string' ? res.text : ''
          });
        } catch {
          /* 宿主刷新失败不该把已回退的结果说成失败 */
        }
      }
    } catch (err) {
      toast(errorText(err, '回退失败'), { tone: 'danger' });
      btn.disabled = false;
    }
  }

  /* 切到记录视图时把对话区收起来；切回来时对话原样留着，不清空。 */
  function toggleRecords(force) {
    recordsMode = typeof force === 'boolean' ? force : !recordsMode;
    recordsBtn.dataset.active = recordsMode ? 'true' : 'false';
    recordsBtn.setAttribute('aria-pressed', recordsMode ? 'true' : 'false');
    focusbar.hidden = recordsMode;
    thread.hidden = recordsMode;
    compose.hidden = recordsMode;
    recordsBody.hidden = !recordsMode;
    if (recordsMode) loadRecords();
  }

  /* ------------------------------------------------------------ position */

  function viewport() {
    return {
      width: window.innerWidth || 1024,
      height: window.innerHeight || 768
    };
  }

  function panelBox() {
    const vp = viewport();
    return {
      width: panel.offsetWidth || Math.min(PANEL_W, vp.width * 0.92),
      height: panel.offsetHeight || Math.min(PANEL_H, vp.height * 0.76)
    };
  }

  function cssPx(name, fallback) {
    try {
      const raw = getComputedStyle(rootEl).getPropertyValue(name).trim();
      const n = parseFloat(raw);
      return Number.isFinite(n) ? n : fallback;
    } catch {
      return fallback;
    }
  }

  function clampTo(x, y) {
    const vp = viewport();
    const box = panelBox();
    const maxX = Math.max(0, vp.width - box.width - EDGE);
    const maxY = Math.max(0, vp.height - box.height - EDGE);
    const minX = Math.min(EDGE, maxX);
    const minY = Math.min(EDGE, maxY);
    return {
      x: Math.round(Math.min(Math.max(minX, x), maxX)),
      y: Math.round(Math.min(Math.max(minY, y), maxY))
    };
  }

  function moveTo(x, y) {
    const next = clampTo(x, y);
    pos = next;
    panel.style.left = `${next.x}px`;
    panel.style.top = `${next.y}px`;
  }

  function defaultPosition() {
    const vp = viewport();
    const box = panelBox();
    /* 默认贴着 FAB 上方、右侧面板左侧；--right-col 取不到时按 0 处理。 */
    const right = cssPx('--right-col', 0) + 18;
    return clampTo(vp.width - box.width - right, vp.height - box.height - 72);
  }

  function saveState(nextOpen = open) {
    const payload = {
      x: pos.x == null ? 0 : Math.round(pos.x),
      y: pos.y == null ? 0 : Math.round(pos.y),
      open: Boolean(nextOpen)
    };
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(payload));
    } catch {
      /* 隐私模式或存储不可用：状态只活在本次会话 */
    }
  }

  function openPanel(shouldFocus = true) {
    if (disposed || open) {
      if (open && shouldFocus && !input.disabled) input.focus();
      return;
    }
    syncContext();
    panel.style.visibility = 'hidden';
    panel.hidden = false;
    open = true;
    rootEl.dataset.open = 'true';
    const next = pos.x == null || pos.y == null ? defaultPosition() : clampTo(pos.x, pos.y);
    moveTo(next.x, next.y);
    panel.style.visibility = '';
    saveState(true);
    if (focusTimer) clearTimeout(focusTimer);
    if (shouldFocus) {
      focusTimer = setTimeout(() => {
        focusTimer = 0;
        if (open && !disposed && !input.disabled) input.focus();
      }, 30);
    }
  }

  function closePanel() {
    if (!open) return;
    if (dragging) endDrag();
    open = false;
    rootEl.dataset.open = 'false';
    panel.hidden = true;
    if (focusTimer) {
      clearTimeout(focusTimer);
      focusTimer = 0;
    }
    saveState(false);
    if (panel.contains(document.activeElement)) fab.focus();
  }

  function togglePanel() {
    if (open) closePanel();
    else openPanel();
  }

  /* --------------------------------------------------------------- drag */

  function onHeadDown(event) {
    if (!open) return;
    if (event.button != null && event.button !== 0) return;
    if (event.target instanceof Element && event.target.closest('button, a, input, textarea')) return;
    const rect = panel.getBoundingClientRect();
    dragging = {
      id: event.pointerId == null ? null : event.pointerId,
      dx: event.clientX - rect.left,
      dy: event.clientY - rect.top
    };
    panel.dataset.dragging = 'true';
    if (event.pointerId != null && head.setPointerCapture) {
      try {
        head.setPointerCapture(event.pointerId);
      } catch {
        /* ignore */
      }
    }
    event.preventDefault();
  }

  function onHeadMove(event) {
    if (!dragging) return;
    if (dragging.id != null && event.pointerId != null && event.pointerId !== dragging.id) return;
    event.preventDefault();
    moveTo(event.clientX - dragging.dx, event.clientY - dragging.dy);
  }

  function onHeadUp(event) {
    if (!dragging) return;
    if (dragging.id != null && event.pointerId != null && event.pointerId !== dragging.id) return;
    endDrag();
  }

  function endDrag() {
    const id = dragging ? dragging.id : null;
    dragging = null;
    delete panel.dataset.dragging;
    if (id != null && head.releasePointerCapture) {
      try {
        head.releasePointerCapture(id);
      } catch {
        /* ignore */
      }
    }
    saveState(true);
  }

  function onResize() {
    if (!open || disposed) return;
    const next = clampTo(pos.x == null ? 0 : pos.x, pos.y == null ? 0 : pos.y);
    moveTo(next.x, next.y);
    saveState(true);
  }

  function onInputKey(event) {
    if (event.key === 'Escape') {
      /* 只在输入框里处理 Esc，主应用的全局快捷键不受影响。 */
      if (input.value) {
        input.value = '';
        autoGrow();
      } else {
        closePanel();
      }
      return;
    }
    if (event.key !== 'Enter' || event.shiftKey) return;
    if (event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    if (!input.disabled) send();
  }

  function onInput() {
    autoGrow();
  }

  function destroy() {
    if (disposed) return;
    disposed = true;
    open = false;
    dragging = null;
    panel.hidden = true;
    rootEl.dataset.open = 'false';
    if (controller) {
      try {
        controller.abort();
      } catch {
        /* ignore */
      }
      controller = null;
    }
    if (focusTimer) {
      clearTimeout(focusTimer);
      focusTimer = 0;
    }
    window.removeEventListener('resize', onResize);
    head.removeEventListener('pointerdown', onHeadDown);
    head.removeEventListener('pointermove', onHeadMove);
    head.removeEventListener('pointerup', onHeadUp);
    head.removeEventListener('pointercancel', onHeadUp);
    rootEl.remove();
    messages.length = 0;
    nodes.clear();
  }

  /* ------------------------------------------------------------- wiring */

  head.addEventListener('pointerdown', onHeadDown);
  head.addEventListener('pointermove', onHeadMove);
  head.addEventListener('pointerup', onHeadUp);
  head.addEventListener('pointercancel', onHeadUp);
  head.addEventListener('lostpointercapture', () => {
    if (dragging) endDrag();
  });
  input.addEventListener('keydown', onInputKey);
  input.addEventListener('input', onInput);
  window.addEventListener('resize', onResize);

  /* 文体决定提示文案，先按剧本铺一遍，syncContext 再按实际文体纠正。 */
  paintEmpty();
  syncContext();
  if (stored.open === true) openPanel(false);

  return {
    open: () => openPanel(),
    close: () => closePanel(),
    toggle: () => togglePanel(),
    isOpen: () => open,
    showRecords: () => toggleRecords(true),
    /* 宿主换了目标就叫我一声：chip 上的「第几场第几段」要立刻跟上。 */
    refresh: () => syncContext(),
    setFocus,
    destroy,
    el: rootEl
  };
}
