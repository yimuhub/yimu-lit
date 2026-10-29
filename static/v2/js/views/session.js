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
    title: '神笔 · 本轮处理',
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
          const msgs = res.items.map((item, i) => {
            const base = {
              id: `hist_${i}_${item.ts || 0}`,
              role: item.role || 'assistant',
              content: item.content || '',
              ts: item.ts || 0,
              historical: true,
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
    const sig = JSON.stringify({
      k: state.currentKey,
      r: work && work.running,
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
        reason: reason || '神笔建议调整后续几章的规划安排。',
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
    // 追章模式（v2.2.2）：仅小说且当前打开了作品时携带 key——后端检测到这本书
    // 正在后台写作，就会在回复后继续把新完成的章节实时推进对话。
    const medium = payload.medium || state.workMedium || state.ui.medium || 'novel';
    const withWork = Boolean(state.currentKey) && medium === 'novel';
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
      el.remove();
    }
  };
}
