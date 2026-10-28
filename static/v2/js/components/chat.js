/* 会话视图：消息列表、流式增量更新、审批卡、队列、滚动跟随。 */

import { h, icon } from '../core/dom.js';
import { store } from '../core/state.js';
import { renderMarkdownInto } from '../core/markdown.js';
import {
  renderUserMessage,
  renderAssistantMessage,
  renderApprovalCard,
  renderQueueRow,
  processCard
} from './message.js';

function messageSignature(chat) {
  /* v2.9.0：章节卡的折叠/展开要触发整行重渲染（正文在 card 内按状态渲染），
     所以签名里带上章节卡状态位。 */
  return (chat.messages || [])
    .map((m) => `${m.id}${m.chapterMeta ? (m.collapsed ? '~c' : '~o') : ''}`)
    .join('|');
}

export function createChatView({ ctx, composer, handlers = {} } = {}) {
  const el = h('div', { class: 'chat-view' });
  const scroll = h('div', { class: 'chat-scroll' });
  const column = h('div', { class: 'chat-column' });
  const flow = h('div', { class: 'chat-flow' });
  const extras = h('div', { class: 'chat-extras' });
  column.append(flow, extras);
  scroll.append(column);
  el.append(scroll);

  let currentSig = '';
  let currentNodeMap = new Map();
  let lastStreaming = false;
  let pendingFrame = 0;
  let disposed = false;

  function nearBottom() {
    return scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 140;
  }

  function scrollToBottom(smooth = false) {
    scroll.scrollTo({
      top: scroll.scrollHeight,
      behavior: smooth ? 'smooth' : 'auto'
    });
  }

  function buildEmpty() {
    const state = store.get();
    const work = state.work;
    const empty = h('div', { class: 'chat-empty' });
    empty.append(
      h('div', { class: 'hero-mark', html: icon('quill', 20) }),
      h('h2', { text: work ? `继续写《${work.name}》` : '开始一段新的创作' }),
      h('p', {
        text: work
          ? `当前进度 ${work.done}/${work.total} 章。直接说下一步想做什么，或让我按规划继续。`
          : '描述一个念头、一个人物或一场戏，我会先和你对齐方向。'
      })
    );
    const suggestions = work
      ? ['继续写下一章，先给我看场景节拍', '检查最近三章的伏笔与知情范围', '把第 47 章的开场写得更冷一些']
      : ['我想写一个港城悬疑故事，主角是被停职的刑警', '帮我把这个念头拆成三幕结构', '我想要冷硬、短句、少形容词的文风'];
    const list = h('div', { class: 'suggest-list', style: { display: 'grid', gap: '6px', width: '100%', maxWidth: '520px', marginTop: '12px' } });
    for (const text of suggestions) {
      list.append(
        h(
          'button',
          {
            class: 'suggestion',
            type: 'button',
            style: {
              padding: '8px 11px',
              border: '0.5px solid var(--line-2)',
              borderRadius: 'var(--radius-sm)',
              background: 'var(--bg-base)',
              color: 'var(--ink-secondary)',
              fontSize: '12px',
              textAlign: 'left'
            },
            on: {
              click: () => {
                if (composer) {
                  composer.setText(text);
                  composer.focus();
                }
              }
            }
          },
          text
        )
      );
    }
    empty.append(list);
    return empty;
  }

  function renderAll() {
    const state = store.get();
    const chat = state.chat;
    const messages = chat.messages || [];
    const wasNear = nearBottom();
    flow.textContent = '';
    currentNodeMap = new Map();

    if (!messages.length) {
      flow.append(buildEmpty());
    } else {
      for (const msg of messages) {
        const node =
          msg.role === 'user'
            ? renderUserMessage(msg)
            : renderAssistantMessage(msg, {
                onRetry: handlers.onRetry,
                workKey: state.currentKey,
                chapter: state.route.chapter,
                onBranch: handlers.onBranch,
                onDismissError: () => handlers.onDismissError && handlers.onDismissError(msg)
              });
        currentNodeMap.set(msg.id, node);
        flow.append(node);
      }
    }

    renderExtras();
    currentSig = messageSignature(chat);
    lastStreaming = messages.some((m) => m.streaming);
    if (wasNear || lastStreaming) requestAnimationFrame(() => scrollToBottom(false));
    updateComposerStreaming(chat);
  }

  function renderExtras() {
    const state = store.get();
    const chat = state.chat;
    extras.textContent = '';
    /* v2.9.0：上下文自动压缩提示（仅展示对话被压缩；点开可看摘要） */
    if (chat.compressed && chat.compressed.count) {
      const details = h('details', { class: 'compress-chip' });
      details.append(
        h('summary', {},
          h('span', { html: icon('database', 12) }),
          h('span', { text: `已压缩 ${chat.compressed.count} 条早期对话为创作进度摘要` })
        ),
        h('div', { class: 'compress-summary', text: chat.compressed.summary || '（摘要生成中）' })
      );
      extras.append(details);
    }
    const queueRow = renderQueueRow(chat.queue, { onRemove: handlers.onRemoveQueue });
    if (queueRow) extras.append(queueRow);
    if (chat.approval) {
      const card = renderApprovalCard(chat.approval, {
        onApply: handlers.onApplyApproval,
        onReject: handlers.onRejectApproval,
        onViewPlan: handlers.onViewPlan
      });
      if (card) {
        const wrap = h('div', { style: { paddingLeft: '35px' } }, card);
        extras.append(wrap);
      }
    }
  }

  function syncHints(node, msg) {
    let hints = node.querySelector('.msg-hints');
    if (!hints) {
      const main = node.querySelector('.msg-main');
      if (!main) return;
      hints = h('div', { class: 'msg-hints' });
      main.append(hints);
    }
    const status = msg.status || '';
    const notice = msg.notice || '';
    const sig = `${status}|${notice}`;
    // 整块重建，避免状态清空后旧提示行永久留在气泡里
    if (hints.dataset.sig === sig) return;
    hints.dataset.sig = sig;
    hints.textContent = '';
    if (status) hints.append(h('div', { class: 'hint-text msg-status', text: status }));
    if (notice) hints.append(h('div', { class: 'hint-text msg-notice', text: notice }));
  }

  function updateStreamingNode(msg) {
    const node = currentNodeMap.get(msg.id);
    if (!node) {
      renderAll();
      return;
    }
    const body = node.querySelector('.msg-body');
    if (body) {
      const next = msg.content || '';
      if (body.dataset.raw !== next) {
        body.dataset.raw = next;
        body.innerHTML = '';
        if (next) renderMarkdownInto(body, next);
        else body.append(h('span', { class: 'thinking' }, h('span', { class: 'tdot' }), h('span', { class: 'tdot' }), h('span', { class: 'tdot' })));
        body.classList.toggle('is-streaming', Boolean(msg.streaming));
      }
    }
    const oldCard = node.querySelector('.process-card');
    if (oldCard && msg.process) {
      const nextSig = `${msg.process.title}|${(msg.process.steps || []).map((s) => s.state).join(',')}`;
      if (oldCard.dataset.sig !== nextSig) {
        const fresh = processCard(msg.process, { live: true });
        if (fresh) {
          fresh.dataset.sig = nextSig;
          oldCard.replaceWith(fresh);
        }
      }
    }
    syncHints(node, msg);
    const phaseEl = node.querySelector('.msg-phase');
    if (phaseEl) {
      const nextPhase = msg.phase || '生成中';
      if (phaseEl.textContent !== nextPhase) phaseEl.textContent = nextPhase;
    }
  }

  function updateComposerStreaming(chat) {
    if (!composer) return;
    // 以消息自身的 streaming 为准，避免会话级标志被其他更新覆盖时输入器提前回到发送态
    composer.setStreaming((chat.messages || []).some((m) => m.streaming));
  }

  function scheduleFrame() {
    if (pendingFrame) return;
    pendingFrame = requestAnimationFrame(() => {
      pendingFrame = 0;
      if (disposed) return;
      const state = store.get();
      const chat = state.chat;
      const sig = messageSignature(chat);
      const wasNear = nearBottom();
      if (sig !== currentSig) {
        renderAll();
        return;
      }
      const streamingMsg = (chat.messages || []).find((m) => m.streaming);
      if (streamingMsg) {
        updateStreamingNode(streamingMsg);
      }
      if (lastStreaming && !streamingMsg) {
        renderAll();
        return;
      }
      lastStreaming = Boolean(streamingMsg);
      renderExtras();
      updateComposerStreaming(chat);
      if (lastStreaming || wasNear) scrollToBottom(false);
    });
  }

  let lastExtrasKey = '';
  const unsubscribe = store.subscribe((state) => {
    if (disposed) return;
    const chat = state.chat;
    const extrasKey = JSON.stringify({
      q: (chat.queue || []).map((x) => x.id),
      a: chat.approval && chat.approval.id,
      s: chat.streaming,
      c: chat.compressed && chat.compressed.count
    });
    const sig = messageSignature(chat);
    if (sig !== currentSig || extrasKey !== lastExtrasKey) {
      lastExtrasKey = extrasKey;
      scheduleFrame();
    } else {
      const streamingMsg = (chat.messages || []).find((m) => m.streaming);
      // 流式结束的那一帧也要排，否则收尾文案/内联动作会停在旧状态
      if (streamingMsg || lastStreaming) scheduleFrame();
    }
  });

  // 流式文本变化频繁，订阅里的 scheduleFrame 已用 rAF 合并。
  renderAll();
  lastExtrasKey = '';

  scroll.addEventListener('scroll', () => {
    el.dataset.pinned = nearBottom() ? 'true' : 'false';
  });

  return {
    el,
    scroll,
    scrollToBottom,
    refresh: renderAll,
    destroy() {
      disposed = true;
      unsubscribe();
      if (pendingFrame) cancelAnimationFrame(pendingFrame);
    }
  };
}
