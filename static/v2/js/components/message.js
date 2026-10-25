/* 消息渲染：安全 Markdown、过程卡、思考态、流式光标、操作与错误。 */

import { h, icon, relTime, fmtNum } from '../core/dom.js';
import { renderMarkdownInto } from '../core/markdown.js';
import { updateMessage } from '../core/state.js';
import { usageLabel } from '../core/constants.js';
import { toast, openFeedback } from '../core/ui.js';
import { api } from '../core/api.js';

function thinkingDots() {
  return h('span', { class: 'thinking', 'aria-label': '正在思考' }, h('span', { class: 'tdot' }), h('span', { class: 'tdot' }), h('span', { class: 'tdot' }));
}

export function processCard(process, { live = false } = {}) {
  if (!process) return null;
  const details = h('details', { class: 'process-card', open: Boolean(live) });
  const summary = h('summary');
  summary.append(
    h('span', { class: 'pc-icon', html: icon(live ? 'activity' : 'cpu', 13) }),
    h('span', { class: 'pc-title', text: process.title || '处理过程' }),
    h('span', { class: 'pc-meta' })
  );
  const meta = summary.querySelector('.pc-meta');
  const parts = [];
  if (process.stage) parts.push(process.stage);
  if (process.model) parts.push(process.model);
  if (process.duration) parts.push(process.duration);
  if (process.tokens) parts.push(`${fmtNum(process.tokens)} tokens`);
  if (process.retries) parts.push(`重试 ${process.retries}`);
  parts.forEach((p, index) => {
    if (index) meta.append(h('span', { class: 'pc-sep' }));
    meta.append(h('span', { text: p }));
  });
  summary.append(h('span', { class: 'pc-chev', html: icon('chevron-right', 12) }));
  details.append(summary);

  const body = h('div', { class: 'pc-body' });
  if (process.detail) body.append(h('div', { text: process.detail }));
  if (Array.isArray(process.steps) && process.steps.length) {
    const steps = h('div', { class: 'pc-steps' });
    for (const step of process.steps) {
      steps.append(
        h(
          'div',
          { class: 'pc-step', dataset: { state: step.state || 'done' } },
          h('span', { class: 'step-dot' }),
          h('span', { text: step.label }),
          step.ms ? h('span', { class: 'muted', style: { marginLeft: 'auto' }, text: step.ms }) : null
        )
      );
    }
    body.append(steps);
  }
  details.append(body);
  return details;
}

function userAttachments(msg) {
  if (!Array.isArray(msg.attachments) || !msg.attachments.length) return null;
  const wrap = h('div', { class: 'attachments' });
  for (const att of msg.attachments) {
    wrap.append(
      h(
        'span',
        { class: 'attach-chip', title: att.title },
        h('span', { class: 'ac-icon', html: icon('file-text', 12) }),
        h('span', { class: 'ac-name', text: att.title || '附件' }),
        h('span', { class: 'sr-only', text: usageLabel(att.usage_mode) })
      )
    );
  }
  return wrap;
}

export function renderUserMessage(msg) {
  const el = h('div', { class: 'msg msg-user', dataset: { id: msg.id } });
  const atts = userAttachments(msg);
  if (atts) el.append(atts);
  el.append(h('div', { class: 'bubble', text: msg.content || '' }));
  const meta = h(
    'div',
    { class: 'user-meta' },
    h('span', { text: msg.created_at ? relTime(msg.created_at) : '刚刚' }),
    h('button', {
      class: 'msg-action',
      type: 'button',
      'aria-label': '复制这条消息',
      html: icon('copy', 12),
      on: {
        click: async () => {
          try {
            await navigator.clipboard.writeText(msg.content || '');
            toast('已复制', { duration: 1400 });
          } catch {
            toast('复制失败，请手动选择文本', { tone: 'danger' });
          }
        }
      }
    })
  );
  el.append(meta);
  return el;
}

export function renderAssistantMessage(msg, handlers = {}) {
  const assistant = h('div', { class: 'msg msg-assistant', dataset: { id: msg.id } });
  assistant.append(h('div', { class: 'msg-avatar', html: icon('quill', 14) }));
  const main = h('div', { class: 'msg-main' });

  const head = h('div', { class: 'msg-head' });
  head.append(h('span', { class: 'msg-author', text: '神笔' }));
  if (msg.created_at) head.append(h('span', { class: 'msg-time', text: relTime(msg.created_at) }));
  if (msg.tag) head.append(h('span', { class: 'msg-tag', text: msg.tag }));
  if (msg.streaming) {
    head.append(h('span', { class: 'msg-tag', text: msg.phase || '生成中' }));
  }
  main.append(head);

  const card = processCard(msg.process, { live: Boolean(msg.streaming) });
  if (card) main.append(card);

  if (msg.error) {
    main.append(renderErrorBanner(msg.error, { onRetry: handlers.onRetry, onDismiss: handlers.onDismissError }));
  }

  const body = h('div', { class: `msg-body md${msg.streaming ? ' is-streaming' : ''}` });
  if (msg.content) renderMarkdownInto(body, msg.content);
  else if (msg.streaming) body.append(thinkingDots());
  main.append(body);

  if (msg.streaming && msg.status) {
    main.append(h('div', { class: 'hint-text', style: { marginTop: '4px' }, text: msg.status }));
  }

  if (!msg.streaming) {
    const actions = h('div', { class: 'msg-actions' });
    const copyBtn = h(
      'button',
      { class: 'msg-action', type: 'button', html: `${icon('copy', 13)}<span>复制</span>` },
      null
    );
    copyBtn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(msg.content || '');
        toast('已复制回复', { duration: 1400 });
      } catch {
        toast('复制失败，请手动选择文本', { tone: 'danger' });
      }
    });
    actions.append(copyBtn);

    if (handlers.onRetry) {
      actions.append(
        h('button', {
          class: 'msg-action',
          type: 'button',
          html: `${icon('refresh', 13)}<span>重试</span>`,
          on: { click: () => handlers.onRetry(msg) }
        })
      );
    }

    const good = h('button', {
      class: `msg-action${msg.rating === 'up' ? ' is-on' : ''}`,
      type: 'button',
      'aria-pressed': msg.rating === 'up' ? 'true' : 'false',
      'aria-label': '这条回复有帮助',
      html: icon('thumbs-up', 13),
      on: {
        click: async () => {
          updateMessage(msg.id, { rating: msg.rating === 'up' ? null : 'up' });
          if (msg.rating !== 'up') {
            try {
              await api.feedback({ project_key: handlers.workKey || '', rating: 5, tags: ['有帮助'], note: msg.content.slice(0, 200) });
            } catch {
              /* 反馈失败不打断阅读 */
            }
            toast('已记为“有帮助”，会强化这类写法', { duration: 1800 });
          }
        }
      }
    });
    const bad = h('button', {
      class: `msg-action${msg.rating === 'down' ? ' is-on' : ''}`,
      type: 'button',
      'aria-label': '这条回复需要改进',
      html: icon('thumbs-down', 13),
      on: {
        click: () => {
          openFeedback({
            onSubmit: async ({ rating, tags, note }) => {
              await api.feedback({
                project_key: handlers.workKey || '',
                chapter_no: handlers.chapter || null,
                rating,
                tags,
                note
              });
              updateMessage(msg.id, { rating: 'down' });
            }
          });
        }
      }
    });
    actions.append(good, bad);

    if (handlers.onBranch) {
      actions.append(
        h('button', {
          class: 'msg-action',
          type: 'button',
          html: `${icon('branch', 13)}<span>从这里分支</span>`,
          on: { click: () => handlers.onBranch(msg) }
        })
      );
    }

    actions.append(h('span', { class: 'actions-spacer' }));
    if (msg.created_at) actions.append(h('span', { class: 'msg-stamp', text: relTime(msg.created_at) }));
    main.append(actions);
  }

  assistant.append(main);
  return assistant;
}

export function renderErrorBanner(error, { onRetry, onDismiss } = {}) {
  const wrap = h('div', { class: 'msg-error', role: 'alert' });
  wrap.append(h('span', { html: icon('alert', 15) }));
  const main = h('div', { class: 'err-main' });
  main.append(h('strong', { text: '这一轮没有完成' }));
  main.append(h('div', { text: error }));
  wrap.append(main);
  const actions = h('div', { class: 'err-actions' });
  if (onRetry) {
    actions.append(h('button', { class: 'err-btn', type: 'button', text: '重试', on: { click: onRetry } }));
  }
  if (onDismiss) {
    actions.append(h('button', { class: 'err-btn', type: 'button', text: '知道了', on: { click: onDismiss } }));
  }
  wrap.append(actions);
  return wrap;
}

export function renderApprovalCard(approval, handlers = {}) {
  if (!approval) return null;
  const wrap = h('div', { class: 'approval-card' });
  wrap.append(
    h(
      'div',
      { class: 'ap-head' },
      h('span', { html: icon('shield', 14) }),
      h('span', { text: approval.done ? '规划提案已处理' : '需要你确认' })
    ),
    h('div', { class: 'ap-title', text: approval.title }),
    h('div', { class: 'ap-reason', text: approval.reason })
  );
  if (Array.isArray(approval.scope) && approval.scope.length) {
    const scope = h('div', { class: 'ap-scope' });
    for (const s of approval.scope) scope.append(h('span', { class: 'chip chip-muted', text: s }));
    wrap.append(scope);
  }
  const actions = h('div', { class: 'ap-actions' });
  if (approval.done) {
    actions.append(h('span', { class: 'status-chip', dataset: { tone: 'success' }, text: approval.done }));
  } else {
    actions.append(
      h('button', {
        class: 'btn btn-primary btn-sm',
        type: 'button',
        text: '应用提案',
        on: { click: () => handlers.onApply && handlers.onApply(approval) }
      }),
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        text: '先看差异',
        on: { click: () => handlers.onViewPlan && handlers.onViewPlan(approval) }
      }),
      h('button', {
        class: 'btn btn-ghost btn-sm',
        type: 'button',
        text: '驳回',
        on: { click: () => handlers.onReject && handlers.onReject(approval) }
      })
    );
  }
  wrap.append(actions);
  return wrap;
}

export function renderQueueRow(queue, { onRemove } = {}) {
  if (!queue || !queue.length) return null;
  const row = h('div', { class: 'queue-row', 'aria-label': '待发送队列' });
  queue.forEach((item) => {
    const chip = h(
      'span',
      { class: 'queue-chip', title: item.text || '' },
      h('span', { html: icon('clock', 11) }),
      h('span', { class: 'q-text', text: item.text || '（附件）' })
    );
    if (onRemove) {
      chip.append(
        h('button', {
          class: 'q-x',
          type: 'button',
          'aria-label': '移出队列',
          html: icon('x', 10),
          on: { click: () => onRemove(item.id) }
        })
      );
    }
    row.append(chip);
  });
  return row;
}

