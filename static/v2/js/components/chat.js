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
  return (chat.messages || []).map((m) => m.id).join('|');
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
    const wasNear = nearBottom();
    flow.textContent = '';
    currentNodeMap = new Map();

    if (!chat.messages.length) {
      flow.append(buildEmpty());
    } else {
      for (const msg of chat.messages) {
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
    lastStreaming = Boolean(chat.messages.find((m) => m.streaming));
    if (wasNear || lastStreaming) requestAnimationFrame(() => scrollToBottom(false));
    updateComposerStreaming(chat);
  }

  function renderExtras() {
    const state = store.get();
    const chat = state.chat;
    extras.textContent = '';
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
    const statusEl = node.querySelector('.hint-text');
    if (msg.status) {
      if (statusEl) statusEl.textContent = msg.status;
      else {
        const line = h('div', { class: 'hint-text', style: { marginTop: '4px' }, text: msg.status });
        const main = node.querySelector('.msg-main');
        if (main) main.append(line);
      }
    }
  }

  function updateComposerStreaming(chat) {
    if (!composer) return;
    composer.setStreaming(chat.streaming);
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
      const streamingMsg = chat.messages.find((m) => m.streaming);
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
      q: chat.queue.map((x) => x.id),
      a: chat.approval && chat.approval.id,
      s: chat.streaming
    });
    const sig = messageSignature(chat);
    if (sig !== currentSig || extrasKey !== lastExtrasKey) {
      lastExtrasKey = extrasKey;
      scheduleFrame();
    } else {
      const streamingMsg = chat.messages.find((m) => m.streaming);
      if (streamingMsg) scheduleFrame();
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
