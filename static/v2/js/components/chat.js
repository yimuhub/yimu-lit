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
    /* v4.14.0：空状态文案与建议按钮按文体定制，不再全部套用小说的「章/伏笔」口径。 */
    const medium = store.get().workMedium || 'novel';
    const MEDIUM_GUIDE = {
      novel: {
        title: work ? `继续写《${work.name}》` : '开始一段新的创作',
        hint: work
          ? `当前进度 ${work.done}/${work.total} 章。直接说下一步想做什么，或让我按规划继续。`
          : '描述一个念头、一个人物或一场戏，我会先和你对齐方向。',
        suggest: work
          ? ['继续写下一章，先给我看场景节拍', '检查最近三章的伏笔与知情范围', '把下一章的开场写得更冷一些']
          : ['我想写一个港城悬疑故事，主角是被停职的刑警', '帮我把这个念头拆成三幕结构', '我想要冷硬、短句、少形容词的文风']
      },
      script: {
        title: work ? `继续写《${work.name}》` : '写一部剧本',
        hint: work
          ? `当前进度 ${work.done}/${work.total} 场。直接说下一步要写哪场，或让我按场次清单继续。`
          : '给一个故事雏形，我会先搭人物与场次结构，再逐场写。',
        suggest: work
          ? ['继续写下一场，先给我看本场节拍', '检查场次的人物在场与道具连贯', '把下一场的对白写得更口语一些']
          : ['我想写一个夜班保安发现凶案的故事，电影长片', '帮我把这个念头拆成 20 场的结构', '我想要冷峻、克制的长镜头风格']
      },
      poetry: {
        title: work ? `继续改《${work.name}》` : '写一首诗',
        hint: work
          ? `当前改了 ${work.done}/${work.total} 轮。直接说下一首想写什么，或让我继续改。`
          : '给一个意象或情绪，我会先定体式与韵脚，再成稿。',
        suggest: work
          ? ['再写一首七绝，主题不变', '复核上一首的格律与用韵', '把上一首的第二节改得更含蓄']
          : ['写一首关于江边秋夜的七律', '帮我把这意象扩成组诗（三首）', '我想要克制、少形容词的风格']
      },
      prose: {
        title: work ? `继续写《${work.name}》` : '写一篇散文',
        hint: work
          ? `当前已完成 ${work.done}/${work.total} 个阶段。直接说下一步要写什么，或让我继续。`
          : '给一个想写的人/事/物，我会先定结构与细节密度，再动笔。',
        suggest: work
          ? ['继续写下一篇，先给我看结构骨架', '审一下最近一篇的 AI 腔与节奏', '把下一段的细节密度提高一些']
          : ['写一篇关于巷口早餐摊的随笔', '帮我把这个念头拆成三段式结构', '我想要克制、口语、有体温的风格']
      },
      thesis: {
        title: work ? `继续写《${work.name}》` : '写一篇论文',
        hint: work
          ? `当前进度 ${work.done}/${work.total} 章。直接说下一章要写什么，或让我按提纲继续。`
          : '给一个研究题目，我会先建提纲与引用池，再逐章撰写。',
        suggest: work
          ? ['继续写下一章，先给我看论证链条', '检查最近几章的引用格式', '把下一章的降 AIGC 再润一遍']
          : ['帮我写一篇关于短视频对大学生阅读习惯影响的论文', '帮我把这个选题拆成五章提纲', '我要学术严谨、引用规范']
      },
      children: {
        title: work ? `继续写《${work.name}》` : '写一篇儿童故事',
        hint: work
          ? `当前进度 ${work.done}/${work.total} 篇。直接说下一步要写什么，或让我继续。`
          : '给一个主角和想讲的道理，我会先定分级与价值观红线。',
        suggest: work
          ? ['继续写下一篇，先给我看故事节拍', '检查最近几篇的语言难度', '把下一段的互动问题改得更有趣']
          : ['写一个 3-6 岁孩子怕黑的睡前故事', '帮我把这个念头拆成三幕结构', '我要简短、重复句式、有安全感']
      },
      news: {
        title: work ? `继续写《${work.name}》` : '写一篇新闻稿',
        hint: work
          ? `当前已完成 ${work.done}/${work.total} 个阶段。直接说下一步要写什么，或让我继续。`
          : '给一个事件或产品，我会先核事实、定倒金字塔结构。',
        suggest: work
          ? ['继续写下一篇，先给我看导语', '检查事实与数字一致性', '把社媒短版再精简一些']
          : ['帮我写一篇新品发布的新闻稿', '帮我把这个事件拆成导语+主体+背景', '我要官方正式、客观严谨']
      },
      copy: {
        title: work ? `继续写《${work.name}》` : '写一篇文案',
        hint: work
          ? `当前进度 ${work.done}/${work.total} 篇。直接说下一步要写什么，或让我继续。`
          : '给一个产品或主题，我会先出钩子库，再按框架写正文。',
        suggest: work
          ? ['继续写下一篇，先给我看钩子候选', '检查上一篇的转化力', '把下一段改得更口语一些']
          : ['帮我写一篇抖音口播稿，新品上市', '帮我把这个卖点拆成 10 个钩子', '我要活力种草、节奏感强']
      },
      other: {
        title: work ? `继续写《${work.name}》` : '写一篇内容',
        hint: work
          ? `当前进度 ${work.done}/${work.total} 节。直接说下一步要写什么，或让我继续。`
          : '给一个主题和模板，我会先定结构与分节大纲，再逐节写。',
        suggest: work
          ? ['继续写下一节，先给我看大纲', '检查上一节的模板规范', '把下一节的节奏改得更顺']
          : ['帮我写一篇关于这部电影的影评', '帮我把这个话题拆成播客脚本大纲', '我要轻松口语、有观点']
      }
    };
    const guide = MEDIUM_GUIDE[medium] || MEDIUM_GUIDE.novel;
    empty.append(
      h('div', { class: 'hero-mark', html: icon('quill', 20) }),
      h('h2', { text: guide.title }),
      h('p', { text: guide.hint })
    );
    const suggestions = guide.suggest;
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
