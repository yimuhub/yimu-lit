/* 作品会话视图：编排对话流、输入器、目标条、队列、审批、停止与重试。
   所有模型输出都经过 markdown 安全渲染；失败保留已生成内容并给出可执行提示。 */

import { h, icon } from '../core/dom.js';
import {
  store,
  patchChat,
  patchUi,
  addMessage,
  updateMessage,
  removeMessage
} from '../core/state.js';
import { api, errorText, ApiError } from '../core/api.js';
import { renderMarkdownInto } from '../core/markdown.js';
import { toast, confirmDialog } from '../core/ui.js';
import { createComposer } from '../components/composer.js';
import { createChatView } from '../components/chat.js';
import { renderPlanBar } from '../components/planbar.js';

const STEPS = [
  { id: 'recall', label: '读取作品记忆与规划' },
  { id: 'align', label: '对齐人物、伏笔与本章目标' },
  { id: 'write', label: '组织本轮回复' }
];

function nowId(prefix) {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
}

function shownChars(text) {
  return Array.from(String(text || '')).length;
}

function modelName(state) {
  const chosen = state.ui.model;
  if (chosen) return chosen;
  const options = (state.models && state.models.options) || [];
  const hit = options.find((o) => o && o.default) || options[0];
  return (hit && (hit.label || hit.name || hit.id)) || '';
}

function stageStates(stage) {
  if (stage === 'writing' || stage === 'done') return ['done', 'done', 'running'];
  if (stage === 'align') return ['done', 'running', 'pending'];
  return ['running', 'pending', 'pending'];
}

function buildProcess(state) {
  return {
    title: '一幕·YIMU 神笔 · 本轮处理',
    stage: '准备中',
    model: modelName(state),
    steps: STEPS.map((step, index) => ({
      label: step.label,
      state: index === 0 ? 'running' : 'pending'
    }))
  };
}

function finishSteps(process, { title, duration, tokens, stage = 'done' } = {}) {
  return {
    ...(process || {}),
    title: title || '本轮处理完成',
    stage: stage === 'stopped' ? '已停止' : '已完成',
    duration: duration || '',
    tokens: tokens || (process && process.tokens) || '',
    steps: STEPS.map((step) => ({ label: step.label, state: 'done' }))
  };
}

/* v4.21.1：创作活动流去重水位（模块级，跨双挂载也生效）——
   mountKey -> Set(事件 no)；creatorAction 用 mountKey -> ts。 */
const _seenSteps = new Map();
const _seenCreatorTs = new Map();

export function renderSession(container, ctx = {}) {
  const mountKey = store.get().currentKey;
  const el = h('div', { class: 'session-view' });
  const planSlot = h('div', { class: 'session-plan' });
  const bannerSlot = h('div', { class: 'session-banner' });

  let disposed = false;
  let epoch = 0;
  let turn = null;
  let planSig = '';
  let bannerSig = '';
  let bindSig = '';
  const bindRow = h('div', { class: 'rp-bind-row' });
  let queueTimer = 0;
  // 追章（v2.2.2）：本轮对话里已开气泡的章节 no → {id, text, words, done, synced}
  let chapterBubbles = new Map();

  const composer = createComposer({
    variant: 'docked',
    ctx,
    placeholder: '继续说下一步，或让我按规划推进…',
    onSend(payload) {
      runTurn(payload, { appendUser: true });
    },
    onQueue(payload) {
      enqueue(payload);
    },
    onStop() {
      stopTurn();
    }
  });

  const chat = createChatView({
    ctx,
    composer,
    handlers: {
      onRetry: retryMessage,
      onBranch: branchFrom,
      onDismissError(msg) {
        updateMessage(msg.id, { error: null });
      },
      onRemoveQueue(id) {
        patchChat((chatState) => ({
          queue: chatState.queue.filter((item) => item.id !== id)
        }));
      },
      onApplyApproval: applyApproval,
      onRejectApproval: rejectApproval,
      onViewPlan() {
        if (ctx.openRight) ctx.openRight('plan');
        else if (ctx.openPlan) ctx.openPlan();
      }
    }
  });

  const dock = h('div', { class: 'composer-dock' }, bindRow, composer.el);
  el.append(planSlot, bannerSlot, chat.el, dock);

  // v2.6.0：切换作品时自动加载聊天历史；v2.8.0：每次挂载都重新拉取（chat_log.jsonl
  // 是权威留档，中途断开的回复 v2.8.0 起也会落盘），只在非流式状态下替换，
  // 保证「所有聊天记录全部保留、可回读」。
  let histLoading = false;
  async function loadChatHistory() {
    if (histLoading) return;
    histLoading = true;
    const key = store.get().currentKey;
    if (!key) { histLoading = false; return; }
    const medium = store.get().workMedium || store.get().ui?.medium || 'novel';
    // v4.12.1：8 文体都拉聊天历史（chat_log.jsonl 全文体留档）
    try {
      const res = await api.chatHistory(key);
      if (res && res.ok && Array.isArray(res.items)) {
        patchChat((cs) => {
          // 正在流式输出的对话不覆盖；其余情况以落盘记录为准全量替换
          if (cs.streaming) return cs;
          // v4.21.6：按时间正序重排（历史章节卡回填的时间戳可能乱序）
          res.items.sort((a, b) => (a.ts || 0) - (b.ts || 0));
          /* v4.21.9 章节卡归位与去重：
             ① 先从全部记录算出每章「首次完成时刻」（同章多记录 ts 最小值，重写不改变它）；
             ② 同章只保留 ts 最大的一条（最新正文/评分/token）；
             ③ 章节卡排序用首次完成时刻 → 重写后永远归位在原序列。 */
          const _chFirst = new Map();
          for (const it of res.items) {
            if (it.type === 'chapter' && it.no != null) {
              const prev = _chFirst.has(it.no) ? _chFirst.get(it.no) : Infinity;
              _chFirst.set(it.no, Math.min(prev, it.ts || 0));
            }
          }
          const _chLatest = new Map();
          res.items = res.items.filter((it) => {
            if (it.type !== 'chapter' || it.no == null) return true;
            const cur = _chLatest.get(it.no);
            if (!cur || (it.ts || 0) > (cur.ts || 0)) _chLatest.set(it.no, it);
            return false;   // 章节记录全部移出，最新一条稍后放回
          });
          for (const it of _chLatest.values()) res.items.push(it);
          const _sortKey = (it) => {
            if (it.type === 'chapter' && it.no != null && _chFirst.has(it.no)) return _chFirst.get(it.no);
            return it.ts || 0;
          };
          res.items.sort((a, b) => _sortKey(a) - _sortKey(b));
          const msgs = res.items.map((item, i) => {
            const base = {
              id: `hist_${i}_${item.ts || 0}`,
              role: item.role || 'assistant',
              content: item.content || '',
              ts: item.ts || 0,
              historical: true,
              // v4.21.3：创作按钮气泡（继续/暂停）的落盘副标注，刷新后原样回放
              ...(item.note ? { creatorNote: item.note } : {}),
            };
            // v2.9.0：章节完成记录 → 章节卡片（默认折叠，可展开/复制/分支）
            if (item.type === 'chapter') {
              return {
                ...base,
                chapter: item.no,
                tag: `第${item.no}章 · ${item.title || ''}`,
                collapsed: true,
                chapterMeta: {
                  no: item.no,
                  title: item.title || `第${item.no}章`,
                  words: item.words,
                  tokens: item.tokens == null ? null : Number(item.tokens),
                  quality: item.quality == null ? null : Number(item.quality),
                  ts: String(item.completedAt || '')
                }
              };
            }
            return base;
          });
          return { ...cs, messages: msgs };
        });
      }
    } catch (e) {
      // 静默失败：历史加载失败不影响正常聊天
    } finally {
      histLoading = false;
    }
  }
  loadChatHistory();

  // v3.1.0：新建作品向导留下的首条引导消息 → 自动以用户身份发出，Agent 开始工作
  // （消除"创建完毫无反应、右侧已完成、章节待规划"的断层体验）
  function maybeSendPendingFirstMessage() {
    const pending = store.get().ui && store.get().ui.pendingFirstMessage;
    if (!pending || pending.key !== store.get().currentKey) return;
    if (store.get().chat.streaming || turn) return;   // 有对话在进行就先不打断
    patchUi({ pendingFirstMessage: null });
    runTurn({ text: String(pending.text || '') }, { appendUser: true });
  }
  setTimeout(maybeSendPendingFirstMessage, 400);

  // v2.3 对话绑定提示：小说且当前打开了作品时，composer 上方挂一枚「已绑定《X》」chip。
  // 纯展示，判断口径与 buildStreamPayload 的 withWork 一致；不碰追章流逻辑。
  function paintBind() {
    const state = store.get();
    if (state.currentKey !== mountKey) return;
    const medium = state.workMedium || state.ui.medium || 'novel';
    const item = state.library.items.find((i) => i.key === state.currentKey);
    const name = (state.work && state.work.name) || (item && item.name) || '';
    const show = Boolean(state.currentKey) && medium === 'novel' && Boolean(name);
    const sig = show ? name : '';
    if (sig === bindSig) return;
    bindSig = sig;
    bindRow.textContent = '';
    if (!show) return;
    bindRow.append(
      h(
        'span',
        {
          class: 'chip chip-accent rp-bind-chip',
          title: '本轮对话会带着这本书的记忆与规划'
        },
        h('span', { html: icon('book-open', 12) }),
        h('span', { text: `已绑定《${name}》` })
      )
    );
  }

  function paintPlan() {
    const state = store.get();
    if (state.currentKey !== mountKey) return;
    const work = state.work;
    /* v4.20.0：签名加入 paused / state——暂停请求发出后 planbar 也要重绘（三态按钮收敛） */
    const sig = JSON.stringify({
      k: state.currentKey,
      r: work && work.running,
      p: work && work.paused,
      s: work && work.state,
      d: work && work.done,
      t: work && work.total,
      v: state.plan && state.plan.plan && state.plan.plan.version,
      c: state.plan && Array.isArray(state.plan.plan && state.plan.plan.chapters)
        ? (state.plan.plan.chapters.find((c) => c.status === 'in_progress') || {}).title
        : ''
    });
    if (sig === planSig) return;
    planSig = sig;
    planSlot.textContent = '';
    if (!work) return;
    const bar = renderPlanBar({
      work,
      plan: state.plan,
      ctx,
      onOpenPlan() {
        if (ctx.openRight) ctx.openRight('plan');
        else if (ctx.openPlan) ctx.openPlan();
      }
    });
    if (bar) planSlot.append(bar);
  }

  function paintBanner() {
    const state = store.get();
    if (state.currentKey !== mountKey) return;
    const sig = `${Boolean(state.work)}|${state.workError || ''}`;
    if (sig === bannerSig) return;
    bannerSig = sig;
    bannerSlot.textContent = '';

    if (state.work) return;
    const row = h('div', { class: state.workError ? 'msg-error' : 'session-loading' });
    if (state.workError) {
      row.setAttribute('role', 'alert');
      row.append(
        h('span', { html: icon('alert', 15) }),
        h(
          'div',
          { class: 'err-main' },
          h('strong', { text: '作品没有加载出来' }),
          h('div', { text: state.workError })
        ),
        h(
          'div',
          { class: 'err-actions' },
          h('button', {
            class: 'err-btn',
            type: 'button',
            text: '重新加载',
            on: {
              click: () => {
                if (ctx.refreshWork) ctx.refreshWork();
                else location.reload();
              }
            }
          })
        )
      );
    } else {
      row.setAttribute('aria-live', 'polite');
      row.append(
        h('span', { class: 'skeleton', style: { width: '18px', height: '18px', borderRadius: '6px' } }),
        h('span', { class: 'skeleton skeleton-line', style: { width: '38%' } }),
        h('span', { class: 'hint-text', text: '正在加载作品…' })
      );
    }
    bannerSlot.append(row);
  }

  function currentAttachments(list = []) {
    return list
      .filter((att) => att && att.id)
      .map((att) => ({
        id: att.id,
        title: att.title || '附件',
        usage_mode: att.usage_mode || 'facts'
      }));
  }

  function deriveApproval() {
    const state = store.get();
    const chatState = state.chat;
    if (chatState.approval) return;
    if (!chatState.messages.some((m) => m.role === 'assistant' && !m.streaming && !m.error && m.content)) return;
    const proposals = (state.plan && state.plan.proposals) || [];
    const pending = proposals.find((p) => p && p.status === 'pending');
    if (!pending) return;
    const title = String(pending.instruction || pending.summary || '规划调整提案').trim();
    const reason = String(pending.reason || '').trim();
    const scope = [pending.scope, '需要确认后才会改动规划'].filter(Boolean);
    patchChat({
      approval: {
        id: pending.id,
        proposal_id: pending.id,
        title: `规划提案：${title.slice(0, 42)}`,
        reason: reason || '一幕·YIMU 神笔建议调整后续几章的规划安排。',
        scope
      }
    });
  }

  function later(fn, ms) {
    const id = setTimeout(() => {
      if (!disposed) fn();
    }, ms);
    return id;
  }

  function setSteps(assistantId, statuses, extra = {}) {
    updateMessage(assistantId, (msg) => ({
      ...msg,
      ...extra,
      process: msg.process
        ? {
            ...msg.process,
            steps: msg.process.steps.map((step, index) => ({
              ...step,
              state: statuses[index] || step.state
            }))
          }
        : msg.process
    }));
  }

  function enqueue(payload) {
    const item = {
      id: nowId('q'),
      text: payload.text || '',
      attachments: payload.attachments || [],
      mode: payload.mode || 'draft',
      medium: payload.medium || store.get().ui.medium,
      genre: payload.genre || '',
      created_at: new Date().toISOString()
    };
    patchChat((chatState) => ({ queue: [...chatState.queue, item] }));
    toast('已加入队列，本轮结束后自动发送', { duration: 2200 });
  }

  function processQueue() {
    const chatState = store.get().chat;
    const queue = chatState.queue || [];
    if (disposed || turn || !queue.length) return;
    const next = queue[0];
    patchChat({ queue: queue.slice(1) });
    runTurn(next, { appendUser: true, fromQueue: true });
  }

  function buildStreamPayload(payload) {
    const state = store.get();
    const attachments = currentAttachments(payload.attachments);
    const text = String(payload.text || '').trim() || (attachments.length ? '请参考我附上的资料继续。' : '');
    // v4.15.6 重大修复：此前仅小说携带 key——其他 8 个文体的对话请求不带 key，
    // 后端当成无绑定新会话 → 散文页问「汇报进度」回复的却是小说开书引导。
    // v4.12.1 起全部文体统一走 session 对话视图（后端 _chat_attached 全文体支持），一律携带 key。
    const medium = payload.medium || state.workMedium || state.ui.medium || 'novel';
    const withWork = Boolean(state.currentKey);
    return {
      message: text,
      ...(withWork ? { key: state.currentKey, project_key: state.currentKey } : {}),
      session_id: state.chat.sessionId || undefined,
      provider_id: state.ui.providerId || (state.models && state.models.default_provider) || undefined,
      model: state.ui.model || (state.work && state.work.model) || undefined,
      mode: payload.mode || 'draft',
      medium,
      genre: payload.genre || '',
      attachments
    };
  }

  function runTurn(payload, { appendUser = true, fromQueue = false, retry = false } = {}) {
    const state = store.get();
    if (!state.currentKey) return;
    // turn 是本视图掌握的“正在跑”事实来源：会话级 streaming 标志可能被其他局部更新覆盖
    if (state.chat.streaming || turn) {
      if (!retry) enqueue(payload);
      return;
    }
    const streamPayload = buildStreamPayload(payload);
    if (!streamPayload.message) return;

    const myEpoch = ++epoch;
    const startedAt = Date.now();
    const controller = new AbortController();
    turn = { controller, assistantId: '', epoch: myEpoch, stopped: false, clockTimer: 0 };
    chapterBubbles = new Map();

    if (appendUser) {
      addMessage({
        role: 'user',
        content: payload.text || '',
        attachments: currentAttachments(payload.attachments),
        created_at: new Date().toISOString()
      });
    }

    const assistant = addMessage({
      role: 'assistant',
      streaming: true,
      content: '',
      phase: '正在思考中',
      status: '',
      process: buildProcess(store.get())
    });
    turn.assistantId = assistant.id;

    patchChat({
      streaming: true,
      error: null,
      stopRequested: false,
      phase: 'thinking',
      lastUserText: payload.text || '',
      approval: retry ? store.get().chat.approval : store.get().chat.approval
    });

    let streamError = '';
    let finalData = null;
    let sawDelta = false;
    let phaseBase = '正在思考中';

    const elapsedSeconds = () => Math.floor((Date.now() - startedAt) / 1000);
    const phaseLabel = () => {
      const seconds = elapsedSeconds();
      // 只有尚未产出内容时才升级为“深入思考”，生成中不制造焦虑
      const thinking = !sawDelta && phaseBase !== '生成中';
      const base = thinking && seconds >= 8 ? '深入思考中' : phaseBase;
      return seconds >= 3 ? `${base}（已等待 ${seconds}s）` : base;
    };

    const stageTimers = [
      later(() => {
        if (myEpoch !== epoch) return;
        phaseBase = '思考中';
        updateMessage(assistant.id, (msg) => ({
          ...msg,
          phase: phaseLabel(),
          status: '正在读取作品记忆…'
        }));
        setSteps(assistant.id, stageStates('thinking'));
      }, 700),
      later(() => {
        if (myEpoch !== epoch) return;
        phaseBase = '对齐中';
        updateMessage(assistant.id, { phase: phaseLabel(), status: '正在对齐人物、伏笔与章节目标…' });
        setSteps(assistant.id, stageStates('align'));
      }, 1700)
    ];

    // 后端心跳可能缺席或延迟，前端自己走秒，保证气泡始终有东西在动
    const clockTimer = setInterval(() => {
      if (disposed || myEpoch !== epoch) return;
      updateMessage(assistant.id, (msg) => ({
        ...msg,
        phase: phaseLabel(),
        notice: !sawDelta && elapsedSeconds() >= 60 ? '网关较慢，可停止后重试' : ''
      }));
    }, 1000);
    turn.clockTimer = clockTimer;

    const handleEvent = (event, data) => {
      if (myEpoch !== epoch) return;
      if (event === 'start' && data && data.session_id) {
        patchChat({ sessionId: data.session_id });
        return;
      }
      if (event === 'status' && data) {
        const stage = data.stage || '';
        const text = data.text || '';
        phaseBase = stage === 'writing' ? '生成中' : '思考中';
        updateMessage(assistant.id, (msg) => ({
          ...msg,
          phase: phaseLabel(),
          status: text
        }));
        setSteps(assistant.id, stageStates(stage));
        return;
      }
      if (event === 'delta' && data) {
        const chunk = typeof data === 'string' ? data : data.text || '';
        if (!chunk) return;
        if (!sawDelta) {
          sawDelta = true;
          setSteps(assistant.id, ['done', 'done', 'running']);
        }
        phaseBase = '生成中';
        updateMessage(assistant.id, (msg) => ({
          ...msg,
          content: (msg.content || '') + chunk,
          phase: phaseLabel(),
          status: '',
          notice: ''
        }));
        return;
      }
      if (event === 'chapter_start' && data) {
        const no = Number(data.no);
        // 防御性去重：同一章已有气泡就忽略重复的 start
        if (!Number.isFinite(no) || chapterBubbles.has(no)) return;
        const title = String(data.title || `第${no}章`);
        const msg = addMessage({
          role: 'assistant',
          tag: `第${no}章 · ${title}`,
          content: '正在落笔…',
          chapter: no,
          phase: '落笔中',
          status: '',
          streaming: true
        });
        chapterBubbles.set(no, { id: msg.id, text: '', words: 0, done: false, synced: '' });
        queueChapterPaint(no);
        return;
      }
      if (event === 'chapter_delta' && data) {
        const no = Number(data.no);
        const b = chapterBubbles.get(no);
        const chunk = typeof data === 'string' ? data : data.text || '';
        if (!b || b.done || !chunk) return;
        b.text += chunk;
        queueChapterPaint(no);
        return;
      }
      if (event === 'chapter_done' && data) {
        const no = Number(data.no);
        const b = chapterBubbles.get(no);
        if (!b) return;
        b.words = Number(data.words) || 0;
        b.done = true;
        paintChapter(no);
        updateMessage(b.id, (m) => ({
          ...m,
          streaming: false,
          phase: '',
          collapsed: true,
          chapterMeta: {
            no,
            title: String(data.title || `第${no}章`),
            words: b.words,
            tokens: data.tokens == null ? null : Number(data.tokens),
            quality: data.quality == null ? null : Number(data.quality),
            ts: String(data.ts || '')
          }
        }));
        scrollIfNear();
        /* v4.12.12：写完一章后立即刷新上方进度条与章节数（此前不刷新→进度0%） */
        if (ctx && ctx.refreshWork) ctx.refreshWork();
        return;
      }
      if (event === 'compressed' && data) {
        // v2.9.0：对话上下文自动压缩提示（仅影响展示对话；后台创作不受影响）
        patchChat((cs) => ({
          ...cs,
          compressed: { count: Number(data.count) || 0, summary: String(data.summary || '') }
        }));
        return;
      }
      if (event === 'error') {
        streamError = (data && (data.error || data.message)) || '本轮生成失败';
        updateMessage(assistant.id, { status: streamError });
        return;
      }
      if (event === 'done') {
        finalData = data || null;
        if (data && data.ok === false && !streamError) {
          streamError = data.error || '本轮没有生成内容';
        }
        /* v4.12.12：整轮完成后刷新工作状态（进度/字数/阶段） */
        if (ctx && ctx.refreshWork) ctx.refreshWork();
      }
    };

    const finish = ({ title, stage = 'done', sessionId } = {}) => {
      if (myEpoch !== epoch) return;
      stageTimers.forEach(clearTimeout);
      clearInterval(clockTimer);
      if (turn) turn.clockTimer = 0;
      closeOpenChapters();
      const chatState = store.get().chat;
      const msg = chatState.messages.find((m) => m.id === assistant.id);
      const duration = `${((Date.now() - startedAt) / 1000).toFixed(1)}s`;
      updateMessage(assistant.id, (current) => ({
        ...current,
        streaming: false,
        phase: '',
        status: '',
        notice: '',
        process: finishSteps(current.process, {
          title: title || '本轮处理完成',
          duration,
          tokens: finalData && finalData.tokens,
          stage
        })
      }));
      patchChat({
        streaming: false,
        phase: '',
        stopRequested: false,
        sessionId: sessionId || chatState.sessionId,
        error: null
      });
      if (!disposed) {
        deriveApproval();
        if (msg && msg.content) {
          queueTimer = later(() => {
            if (!(store.get().chat.queue || []).length) return;
            processQueue();
          }, 420);
        }
      }
      turn = null;
    };

    const fallback = async () => {
      phaseBase = '兼容模式';
      updateMessage(assistant.id, (msg) => ({
        ...msg,
        content: msg.content || '',
        phase: phaseLabel(),
        status: '流式接口不可用，正在用兼容模式生成…'
      }));
      const res = await api.chat({
        message: streamPayload.message,
        session_id: streamPayload.session_id,
        provider_id: streamPayload.provider_id,
        model: streamPayload.model
      });
      if (myEpoch !== epoch) return;
      const text = String((res && (res.reply || res.message || res.content)) || '');
      if (!text) throw new ApiError('兼容接口没有返回内容，请稍后重试');
      phaseBase = '生成中';
      updateMessage(assistant.id, { content: text, phase: phaseLabel(), status: '' });
      finish({ title: '兼容模式完成', sessionId: res && res.session_id });
    };

    api
      .streamChat(streamPayload, {
        signal: controller.signal,
        onEvent: handleEvent
      })
      .then(async (res) => {
        if (myEpoch !== epoch) return;
        finalData = finalData || res || null;
        if (streamError) throw new ApiError(streamError);
        if (finalData && finalData.ok === false) {
          throw new ApiError(finalData.error || '本轮生成失败，请重试');
        }
        const chatState = store.get().chat;
        const msg = chatState.messages.find((m) => m.id === assistant.id);
        const finalText = finalData && typeof finalData.text === 'string' ? finalData.text : '';
        // 以服务端 done.text 为唯一准绳：流式可能缺尾，不能按长度判断
        if (finalText && finalText !== ((msg && msg.content) || '')) {
          updateMessage(assistant.id, { content: finalText });
        }
        if (finalData && finalData.fallback) {
          updateMessage(assistant.id, { action: { text: '去配置模型', path: '/settings' } });
        }
        finish({
          sessionId: finalData && finalData.session_id,
          title: '本轮处理完成'
        });
      })
      .catch(async (err) => {
        if (myEpoch !== epoch) return;
        if (turn && turn.stopped) return;
        if (err && (err.code === 'aborted' || (err instanceof ApiError && err.isAbort))) return;
        const code = err && err.code;
        if (code === 'stream_unavailable' || code === 'stream_unsupported' || err.status === 404 || err.status === 405 || err.status === 501) {
          try {
            await fallback();
            return;
          } catch (fallbackErr) {
            err = fallbackErr;
          }
        }
        if (myEpoch !== epoch) return;
        stageTimers.forEach(clearTimeout);
        clearInterval(clockTimer);
        if (turn) turn.clockTimer = 0;
        const message = errorText(err, '这一轮没有完成，请稍后重试');
        updateMessage(assistant.id, (current) => {
          const partial = current.content || '';
          return {
            ...current,
            streaming: false,
            phase: '',
            status: '',
            notice: '',
            content: partial ? `${partial}\n\n*（回复中断，已显示 ${shownChars(partial)} 字）*` : partial,
            error: message,
            process: finishSteps(current.process, {
              title: '本轮中断',
              duration: `${((Date.now() - startedAt) / 1000).toFixed(1)}s`,
              stage: 'stopped'
            })
          };
        });
        patchChat({ streaming: false, phase: '', error: message, stopRequested: false });
        closeOpenChapters();
        toast(message, { tone: 'danger' });
        turn = null;
      });
  }

  function stopTurn() {
    const chatState = store.get().chat;
    if (!turn || turn.stopped) return;
    const { controller, assistantId } = turn;
    turn.stopped = true;
    epoch += 1;
    if (turn.clockTimer) clearInterval(turn.clockTimer);
    turn.clockTimer = 0;
    try {
      controller.abort();
    } catch {
      /* ignore */
    }
    const current = chatState.messages.find((m) => m.id === assistantId);
    const partial = current && current.content ? current.content : '';
    updateMessage(assistantId, (msg) => ({
      ...msg,
      streaming: false,
      phase: '',
      status: '',
      notice: '',
      content: partial
        ? `${partial}\n\n*（已停止，已显示 ${shownChars(partial)} 字）*`
        : '*（已停止生成）*',
      process: finishSteps(msg.process, {
        title: '已停止生成',
        duration: '',
        stage: 'stopped'
      })
    }));
    patchChat({ streaming: false, phase: '', stopRequested: true });
    toast('已停止生成，保留已输出内容', { duration: 2200 });
    closeOpenChapters();
    turn = null;
  }

  // ---------- 追章渲染（v2.2.2）----------
  // 聊天视图只逐帧刷新「第一条 streaming 消息」（主回复气泡），章节气泡的正文
  // 在这里自绘：store 同步 + 直改 DOM，rAF 合并避免每 ~40 字符一次 reflow。
  function chapterContent(b) {
    if (b.done) {
      if (b.text && b.words) return `${b.text}\n\n*${b.words} 字*`;
      if (b.text) return b.text;
      if (b.words) return `本章 ${b.words} 字`;
      return '本章已完成';
    }
    return b.text || '正在落笔…';
  }

  function paintChapter(no) {
    const b = chapterBubbles.get(no);
    if (!b || disposed || b.stopped) return;
    const raw = chapterContent(b);
    if (b.synced !== raw) {
      b.synced = raw;
      updateMessage(b.id, (m) => ({ ...m, content: raw }));
    }
    const node = chat.el.querySelector(`.msg[data-id="${b.id}"] .msg-body`);
    if (node && node.dataset.raw !== raw) {
      node.dataset.raw = raw;
      node.textContent = '';
      renderMarkdownInto(node, raw);
    }
  }

  const chapterDirty = new Set();
  let chapterPaintQueued = false;

  function queueChapterPaint(no) {
    chapterDirty.add(no);
    if (chapterPaintQueued) return;
    chapterPaintQueued = true;
    requestAnimationFrame(() => {
      chapterPaintQueued = false;
      const batch = Array.from(chapterDirty);
      chapterDirty.clear();
      for (const n of batch) paintChapter(n);
      if (batch.length) scrollIfNear();
    });
  }

  function scrollIfNear() {
    const s = chat.scroll;
    if (!s || disposed) return;
    if (s.scrollHeight - s.scrollTop - s.clientHeight < 160) chat.scrollToBottom(false);
  }

  // 中断/收尾兜底：把没写完的章节气泡关掉，避免 composer 卡在生成态
  function closeOpenChapters() {
    for (const [, b] of chapterBubbles) {
      if (b.done) continue;
      b.done = true;
      b.stopped = true;
      const body = b.text || '';
      updateMessage(b.id, (m) => ({
        ...m,
        streaming: false,
        phase: '',
        content: body ? `${body}\n\n*（已停止）*` : '（已停止）'
      }));
    }
  }

  // ---------- v4.21 创作活动流：步骤卡 / 实时输出 / 创作按钮回显 ----------
  // app.js 的 book-live 轮询把 steps / stream 写进 store；这里只负责把增量转成
  // 对话消息：steps 按 no 水位线追加，stream 维护一条「实时输出」卡片（正文走
  // 直改 DOM，与追章 paintChapter 同款套路）。进入书页不回放历史：水位线初始
  // 取当前 items 的最大 no（重挂载也不回放；历史在右栏「本章档案」查）。
  const maxStepNo = (list) =>
    (list || []).reduce((acc, it) => Math.max(acc, Number(it && it.no) || 0), 0);
  let lastStepNo = maxStepNo(store.get().steps && store.get().steps.items);
  let replayBatch = true;   // v4.21.7：挂载后的第一批为历史回放（章内过程不散落，进章节卡下拉）
  let streamEpoch = 0;
  let streamMsgId = '';
  let streamDoneTimer = 0;
  let creatorTs = (store.get().ui && store.get().ui.creatorAction && store.get().ui.creatorAction.ts) || 0;

  function paintSteps() {
    const state = store.get();
    if (state.currentKey !== mountKey) return;
    const items = (state.steps && state.steps.items) || [];
    if (!items.length) {
      /* 被重置过（切书）：水位线归零，等新书的增量 */
      lastStepNo = 0;
      return;
    }
    /* v4.21.1：模块级已见集合——即使极端场景下出现双订阅/双挂载，
       同一事件也只进一次对话流（此前重复事件成对出现）。 */
    let seen = _seenSteps.get(mountKey);
    if (!seen) {
      seen = new Set();
      _seenSteps.set(mountKey, seen);
    }
    const fresh = items.filter((it) => {
      const no = Number(it && it.no) || 0;
      if (!it || no <= lastStepNo || seen.has(no)) return false;
      /* v4.21.6：作家「（正文全文）」事件不再回放为朴素卡——其内容由章节完成卡承载 */
      if (it.agent === 'writer' && /（正文全文）$/.test(String(it.title || '')) && Number(it.chapter) > 0) {
        return false;
      }
      /* v4.21.7：回放批次跳过「章内过程」事件（chapter>0）——同一章所有 Agent 的
         思考/工作/产出已收进章节完成卡的「本章过程」下拉，不再散落刷屏；
         实时创作批次不受影响（保持当前逐步展示形式）。 */
      if (replayBatch && Number(it.chapter || 0) > 0) {
        return false;
      }
      return true;
    });
    if (!fresh.length) return;
    replayBatch = false;   // 首批之后都是实时增量（逐步展示）
    lastStepNo = maxStepNo(items);
    const addedIds = [];
    for (const item of fresh) {
      const no = Number(item.no) || 0;
      seen.add(no);
      const added = addMessage({ kind: 'step', role: 'assistant', step: { ...item }, content: '' });
      addedIds.push(added.id);
    }
    scrollIfNear();
    /* v4.21.1 渲染看门狗：消息已进 store 但 1.2s 后仍未出现在消息流 DOM
       （极端场景下 chat 视图签名失步）→ 强制整流重渲染一次，保证收敛。 */
    const lastId = addedIds[addedIds.length - 1];
    setTimeout(() => {
      if (disposed || !lastId) return;
      if (!chat.el.querySelector(`.msg-step[data-id="${lastId}"]`)) {
        console.warn('[session] 步骤卡未上屏，强制重渲染消息流');
        try { chat.refresh(); } catch { /* ignore */ }
      }
    }, 1200);
  }

  function paintStreamNode(text, thinking) {
    if (!streamMsgId || disposed) return;
    requestAnimationFrame(() => {
      if (disposed) return;
      const card = chat.el.querySelector(`.msg-step[data-id="${streamMsgId}"]`);
      if (!card) return;
      /* v4.21.4：模型思维链实时流（思考区在正文上方，默认展开随流滚动） */
      const thinkNode = card.querySelector('.step-stream-think-text');
      if (thinkNode) {
        const tk = thinking || '';
        if (thinkNode.dataset.raw !== tk) {
          thinkNode.dataset.raw = tk;
          thinkNode.textContent = tk || '（等待模型思维链…）';
          thinkNode.parentElement.hidden = !tk;
        }
        const tbox = thinkNode.parentElement;
        if (tbox && !tbox.hidden) tbox.scrollTop = tbox.scrollHeight;
      }
      const node = card.querySelector('.step-stream-text');
      if (!node) return;
      if (node.dataset.raw !== text) {
        node.dataset.raw = text;
        node.textContent = text || '正在输出…';
      }
      const box = node.parentElement;
      if (box) box.scrollTop = box.scrollHeight;
    });
  }

  function paintStream() {
    const state = store.get();
    if (state.currentKey !== mountKey) return;
    const st = state.stream || {};
    const epoch = st.epoch || 0;

    if (st.active && st.key === state.currentKey) {
      let msg = streamMsgId ? state.chat.messages.find((m) => m.id === streamMsgId) : null;
      if (epoch !== streamEpoch || !msg) {
        streamEpoch = epoch;
        if (streamDoneTimer) {
          clearTimeout(streamDoneTimer);
          streamDoneTimer = 0;
        }
        const wantedId = `step-live-${epoch}`;
        msg = state.chat.messages.find((m) => m.id === wantedId);
        if (msg) {
          /* 重挂载等场景：同一条流已在对话里，直接接管 */
          streamMsgId = msg.id;
          if ((msg.content || '') !== (st.text || '')) {
            updateMessage(msg.id, { content: st.text || '' });
          }
        } else {
          const created = addMessage({
            id: wantedId,
            kind: 'step',
            role: 'assistant',
            stream: true,
            withThinking: true,   // v4.21.4：实时卡带模型思维链区（renderStepMessage 输出）
            step: {
              agent: st.agent || 'system',
              agent_label: st.agent_label || '实时输出',
              title: st.title || (st.chapter ? `第${st.chapter}章` : '实时输出'),
              chapter: st.chapter || 0,
              phase: 'start',
              streaming: true,
              ts: Math.floor(Date.now() / 1000)
            },
            content: st.text || ''
          });
          streamMsgId = created.id;
          scrollIfNear();
        }
      } else if ((msg.content || '') !== (st.text || '')) {
        /* 只同步 content 供重渲染兜底；视觉更新交给 paintStreamNode 直改 DOM */
        updateMessage(streamMsgId, { content: st.text || '' });
      }
      paintStreamNode(st.text || '', st.thinking || '');
      return;
    }

    /* 一轮输出收尾：转完成态（✓ + 耗时 + 全文），1.5s 后收起为可展开完成卡 */
    if (streamMsgId && epoch === streamEpoch && (st.text || '')) {
      const msg = state.chat.messages.find((m) => m.id === streamMsgId);
      if (msg && msg.step && msg.step.streaming) {
        const durationMs = Math.max(0, Date.now() - (Number(msg.step.ts) || 0) * 1000);
        /* v4.21.6：作家正文流完成 → 直接转回「原版章节完成卡」（renderChapterCard 渲染链：
           默认收缩显示摘录 + 完成时间/字数，展开看全文，复制本章/从此章分支/章节工作台）。
           评分在卡生成之后进行，刷新后由落盘记录补上分数。 */
        if (st.agent === 'writer' && Number(st.chapter) > 0) {
          const _no = Number(st.chapter);
          updateMessage(streamMsgId, (m) => ({
            ...m,
            kind: undefined,
            stream: false,
            chapter: _no,
            tag: `第${_no}章`,
            collapsed: true,
            chapterMeta: {
              no: _no,
              title: String(st.title || `第${_no}章`).replace(/^写第\d+章$/, `第${_no}章`),
              words: Array.from(m.content || '').filter((c) => /[\u4e00-\u9fff]/.test(c)).length,
              tokens: null,
              quality: null,
              ts: String(Math.floor(Date.now() / 1000))
            }
          }));
          streamMsgId = '';
          scrollIfNear();
          return;
        }
        updateMessage(streamMsgId, (m) => ({
          ...m,
          content: st.text || '',
          step: {
            ...(m.step || {}),
            phase: 'done',
            streaming: false,
            ms: durationMs > 500 ? Math.round(durationMs / 100) * 100 : 0
          }
        }));
        if (!streamDoneTimer) {
          streamDoneTimer = setTimeout(() => {
            streamDoneTimer = 0;
            if (!disposed) updateMessage(streamMsgId, { collapsed: true });
          }, 1500);
        }
        scrollIfNear();
      }
    }
  }

  /* 创作按钮（继续/暂停）成功后 planbar 会写 ui.creatorAction；这里转成
     创作者视角的用户气泡，ts 水位线去重（含挂载时跳过旧事件；
     v4.21.1 水位线模块级化——双挂载场景也不重复插气泡）。 */
  function paintCreatorAction() {
    const state = store.get();
    if (state.currentKey !== mountKey) return;
    const act = state.ui && state.ui.creatorAction;
    if (!act || !act.ts || act.ts <= creatorTs) return;
    const seenTs = _seenCreatorTs.get(mountKey);
    if (seenTs && act.ts <= seenTs) return;
    _seenCreatorTs.set(mountKey, act.ts);
    creatorTs = act.ts;
    if (act.key !== mountKey) return;
    const text = String(act.text || '').trim();
    if (!text) return;
    addMessage({
      role: 'user',
      content: text,
      creatorNote: '来自创作按钮',
      created_at: new Date().toISOString()
    });
    scrollIfNear();
  }

  function retryMessage(msg) {
    if (msg && msg.chapter) {
      toast('章节内容是对话生成的，不能单独重试', { duration: 2200 });
      return;
    }
    const chatState = store.get().chat;
    if (chatState.streaming) {
      toast('正在生成，等这一轮结束再重试', { duration: 2000 });
      return;
    }
    const index = chatState.messages.findIndex((m) => m.id === msg.id);
    if (index < 0) return;
    if (chatState.messages.slice(index + 1).some((m) => m.role === 'user')) {
      toast('请重试最近一轮回复，避免覆盖后面的对话', { duration: 2400 });
      return;
    }
    let user = null;
    for (let i = index - 1; i >= 0; i -= 1) {
      if (chatState.messages[i].role === 'user') {
        user = chatState.messages[i];
        break;
      }
    }
    if (!user) {
      toast('找不到这一轮对应的提问', { tone: 'danger' });
      return;
    }
    removeMessage(msg.id);
    runTurn(
      {
        text: user.content || '',
        attachments: user.attachments || [],
        mode: 'draft',
        medium: store.get().ui.medium
      },
      { appendUser: false, retry: true }
    );
  }

  function branchFrom(msg) {
    const excerpt = String(msg.content || '').replace(/\s+/g, ' ').slice(0, 80);
    composer.setText(`基于这段回复换一个方向继续：${excerpt ? `“${excerpt}…”` : ''}\n`);
    composer.focus();
    toast('已把分支起点放进输入框，补充你的新方向即可', { duration: 2400 });
  }

  async function refreshPlanData() {
    if (ctx.refreshPlan) {
      try {
        await ctx.refreshPlan();
      } catch {
        /* 规划刷新失败不影响审批状态 */
      }
    }
  }

  async function applyApproval(approval) {
    const proposalId = approval.proposal_id || approval.id;
    if (!proposalId) return;
    const ok = await confirmDialog({
      title: '应用规划提案',
      message: '将把这份提案写入作品规划，并可能影响尚未写作的章节。',
      detail: '已经完成的章节不会被改动；建议先到「规划」里看差异。',
      confirmText: '应用'
    });
    if (!ok) return;
    try {
      await api.applyPlan(store.get().currentKey, proposalId);
      toast('规划已更新', { tone: 'success' });
      patchChat({ approval: { ...store.get().chat.approval, done: '已应用' } });
      refreshPlanData();
    } catch (err) {
      toast(errorText(err, '应用失败'), { tone: 'danger' });
    }
  }

  async function rejectApproval(approval) {
    const proposalId = approval.proposal_id || approval.id;
    if (!proposalId) return;
    const ok = await confirmDialog({
      title: '驳回提案',
      message: '驳回后这份提案不会再自动应用，规划保持原样。',
      confirmText: '驳回',
      danger: true
    });
    if (!ok) return;
    try {
      await api.rejectPlan(store.get().currentKey, proposalId, '');
      toast('已驳回提案');
      patchChat({ approval: { ...store.get().chat.approval, done: '已驳回' } });
      refreshPlanData();
    } catch (err) {
      toast(errorText(err, '驳回失败'), { tone: 'danger' });
    }
  }

  const unsubscribe = store.subscribe((state) => {
    if (disposed || state.currentKey !== mountKey) return;
    paintPlan();
    paintBanner();
    paintBind();
    paintSteps();
    paintStream();
    paintCreatorAction();
    if (state.plan && !state.chat.approval) deriveApproval();
  });

  function onKeydown(event) {
    if (event.key === 'Escape' && turn && !turn.stopped) {
      const target = event.target;
      const typing = target && (target.tagName === 'TEXTAREA' || target.tagName === 'INPUT');
      if (!typing) stopTurn();
    }
  }
  document.addEventListener('keydown', onKeydown);

  paintPlan();
  paintBanner();
  paintBind();
  container.append(el);
  requestAnimationFrame(() => {
    composer.focus();
    chat.scrollToBottom(false);
  });

  return {
    el,
    composer,
    focusComposer: () => composer.focus(),
    refresh() {
      planSig = '';
      bannerSig = '';
      bindSig = '';
      paintPlan();
      paintBanner();
      paintBind();
      chat.refresh();
    },
    scrollToBottom: chat.scrollToBottom,
    destroy() {
      disposed = true;
      epoch += 1;
      if (streamDoneTimer) {
        clearTimeout(streamDoneTimer);
        streamDoneTimer = 0;
      }
      if (turn && turn.clockTimer) clearInterval(turn.clockTimer);
      if (turn && turn.controller) {
        try {
          turn.controller.abort();
        } catch {
          /* ignore */
        }
      }
      if (queueTimer) clearTimeout(queueTimer);
      document.removeEventListener('keydown', onKeydown);
      unsubscribe();
      if (store.get().chat.streaming) {
        patchChat({ streaming: false, phase: '', stopRequested: false });
      }
      chat.destroy();
      if (composer && typeof composer.destroy === 'function') composer.destroy();
      el.remove();
    }
  };
}
