/* 浮层基础件：toast、modal、确认框、弹出菜单、反馈表单。 */

import { h, trapFocus, uid } from './dom.js';
import { icon } from './icons.js';

const overlayRoot = () => document.getElementById('overlays');
const toastRoot = () => document.getElementById('toasts');

export function toast(message, { tone = 'default', duration = 3200, action = null } = {}) {
  const root = toastRoot();
  if (!root) return () => {};
  const el = h(
    'div',
    { class: 'toast', dataset: { tone }, role: 'status' },
    h('span', { class: 'toast-icon', html: icon(tone === 'danger' ? 'alert' : tone === 'success' ? 'check' : 'info', 14) }),
    h('div', { class: 'toast-msg', text: message })
  );
  if (action && action.label) {
    el.append(
      h('button', {
        class: 'toast-action',
        type: 'button',
        text: action.label,
        on: {
          click: () => {
            try {
              action.onClick && action.onClick();
            } finally {
              close();
            }
          }
        }
      })
    );
  }
  const closeBtn = h('button', {
    class: 'toast-close',
    type: 'button',
    'aria-label': '关闭提示',
    html: icon('x', 12),
    on: { click: () => close() }
  });
  el.append(closeBtn);
  root.append(el);
  const timer = duration > 0 ? setTimeout(close, duration) : 0;
  function close() {
    clearTimeout(timer);
    el.remove();
  }
  return close;
}

const modalStack = [];

export function closeTopOverlay() {
  const top = modalStack[modalStack.length - 1];
  if (top) {
    top.close();
    return true;
  }
  return false;
}

export function openModal({
  title,
  subtitle = '',
  body,
  footer = null,
  size = 'md',
  className = '',
  closable = true,
  onClose = null,
  initialFocus = null
}) {
  const root = overlayRoot();
  const titleId = uid('modal-title');
  const backdrop = h('div', { class: 'modal-backdrop' });
  const modal = h('div', {
    class: `modal ${className}`.trim(),
    dataset: { size },
    role: 'dialog',
    'aria-modal': 'true',
    'aria-labelledby': titleId
  });

  const head = h('div', { class: 'modal-header' });
  const copy = h(
    'div',
    { style: { flex: '1', minWidth: '0' } },
    h('div', { class: 'modal-title', id: titleId, text: title || '' })
  );
  if (subtitle) copy.append(h('div', { class: 'modal-subtitle', text: subtitle }));
  head.append(copy);
  if (closable) {
    head.append(
      h('button', {
        class: 'icon-btn icon-btn-sm',
        type: 'button',
        'aria-label': '关闭',
        html: icon('x', 16),
        on: { click: () => close() }
      })
    );
  }
  modal.append(head);

  const bodyEl = h('div', { class: 'modal-body' });
  if (body instanceof Node) bodyEl.append(body);
  else if (typeof body === 'string') bodyEl.innerHTML = body;
  modal.append(bodyEl);

  if (footer) {
    const foot = h('div', { class: 'modal-footer' });
    if (footer instanceof Node) foot.append(footer);
    else if (Array.isArray(footer)) foot.append(...footer);
    modal.append(foot);
  }

  backdrop.append(modal);
  root.append(backdrop);

  const releaseTrap = trapFocus(modal);
  const entry = {
    el: modal,
    close
  };

  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    releaseTrap();
    backdrop.remove();
    const idx = modalStack.indexOf(entry);
    if (idx >= 0) modalStack.splice(idx, 1);
    if (onClose) onClose();
  }
  entry.close = close;
  modalStack.push(entry);

  backdrop.addEventListener('mousedown', (event) => {
    if (event.target === backdrop && closable) close();
  });

  const focusTarget =
    initialFocus ||
    modal.querySelector('[data-autofocus]') ||
    modal.querySelector('input, textarea, select, button:not(.icon-btn)');
  if (focusTarget) {
    setTimeout(() => {
      try {
        focusTarget.focus();
        if (focusTarget.select && focusTarget.tagName === 'INPUT') focusTarget.select();
      } catch {
        /* ignore */
      }
    }, 20);
  }

  return { el: modal, body: bodyEl, close };
}

export function confirmDialog({
  title = '确认操作',
  message = '',
  detail = '',
  confirmText = '确认',
  cancelText = '取消',
  danger = false
}) {
  return new Promise((resolve) => {
    let done = false;
    const body = h('div', { class: 'confirm-body' });
    body.append(h('div', { text: message }));
    if (detail) body.append(h('div', { class: 'warn-note', text: detail }));

    const settle = (value) => {
      if (done) return;
      done = true;
      resolve(value);
      modal.close();
    };

    const footer = h(
      'div',
      { style: { display: 'flex', gap: '8px', width: '100%', justifyContent: 'flex-end' } },
      h('button', { class: 'btn', type: 'button', text: cancelText, on: { click: () => settle(false) } }),
      h('button', {
        class: `btn ${danger ? 'btn-danger' : 'btn-primary'}`,
        type: 'button',
        text: confirmText,
        dataset: { autofocus: '' },
        on: { click: () => settle(true) }
      })
    );

    const modal = openModal({
      title,
      body,
      footer,
      size: 'sm',
      onClose: () => {
        if (!done) {
          done = true;
          resolve(false);
        }
      }
    });
  });
}

let currentPopover = null;

export function closePopover() {
  if (currentPopover) {
    currentPopover.remove();
    currentPopover = null;
  }
}

export function popoverMenu(anchor, items, { align = 'end', label = '' } = {}) {
  closePopover();
  const pop = h('div', { class: 'popover', role: 'menu' });
  if (label) pop.append(h('div', { class: 'popover-label', text: label }));
  for (const item of items) {
    if (!item) continue;
    if (item.separator) {
      pop.append(h('div', { class: 'divider' }));
      continue;
    }
    const btn = h(
      'button',
      {
        class: 'popover-item',
        type: 'button',
        role: 'menuitem',
        'aria-checked': item.checked ? 'true' : 'false',
        disabled: item.disabled || false
      },
      h('span', { html: icon(item.icon || 'dot', 14) }),
      h(
        'span',
        { style: { flex: '1', minWidth: '0' } },
        h('span', { text: item.label }),
        item.sub ? h('span', { class: 'item-sub', text: ` ${item.sub}` }) : null
      ),
      item.checked ? h('span', { class: 'check', html: icon('check', 13) }) : null
    );
    btn.addEventListener('click', () => {
      closePopover();
      if (item.onSelect) item.onSelect();
    });
    pop.append(btn);
  }
  document.body.append(pop);
  const rect = anchor.getBoundingClientRect();
  const pw = pop.offsetWidth;
  const ph = pop.offsetHeight;
  let left = align === 'start' ? rect.left : rect.right - pw;
  left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
  let top = rect.bottom + 6;
  if (top + ph > window.innerHeight - 8) top = Math.max(8, rect.top - ph - 6);
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
  currentPopover = pop;

  const onDoc = (event) => {
    if (!pop.contains(event.target) && !anchor.contains(event.target)) {
      closePopover();
      document.removeEventListener('mousedown', onDoc, true);
    }
  };
  setTimeout(() => document.addEventListener('mousedown', onDoc, true), 0);
  return pop;
}

export function openFeedback({ title = '这条回复哪里不满意？', onSubmit } = {}) {
  const ratings = [
    { v: 1, label: '很差' },
    { v: 2, label: '较差' },
    { v: 3, label: '一般' },
    { v: 4, label: '不错' },
    { v: 5, label: '很好' }
  ];
  const tags = ['剧情拖沓', '人物失真', '文风不对', '信息错误', '没按规划', '太像 AI'];
  let rating = 0;
  let picked = new Set();
  const ratingRow = h('div', { class: 'rating-row' });
  const tagRow = h('div', { class: 'tag-row' });
  const note = h('textarea', { class: 'textarea', rows: 3, placeholder: '补充说明（可选）' });

  for (const r of ratings) {
    const b = h(
      'button',
      { class: 'rating-btn', type: 'button', 'aria-pressed': 'false' },
      h('span', { html: icon('star', 15) }),
      h('span', { text: r.label })
    );
    b.addEventListener('click', () => {
      rating = r.v;
      ratingRow.querySelectorAll('.rating-btn').forEach((x) => x.setAttribute('aria-pressed', 'false'));
      b.setAttribute('aria-pressed', 'true');
    });
    ratingRow.append(b);
  }

  for (const tag of tags) {
    const b = h('button', { class: 'tag-opt', type: 'button', 'aria-pressed': 'false', text: tag });
    b.addEventListener('click', () => {
      if (picked.has(tag)) picked.delete(tag);
      else picked.add(tag);
      b.setAttribute('aria-pressed', picked.has(tag) ? 'true' : 'false');
    });
    tagRow.append(b);
  }

  const body = h(
    'div',
    { class: 'feedback-form' },
    h('div', { class: 'field' }, h('span', { class: 'field-label', text: '整体评价' }), ratingRow),
    h('div', { class: 'field' }, h('span', { class: 'field-label', text: '具体问题' }), tagRow),
    h('div', { class: 'field' }, h('span', { class: 'field-label', text: '补充说明' }), note)
  );

  const footer = h(
    'div',
    { style: { display: 'flex', gap: '8px', width: '100%', justifyContent: 'flex-end' } },
    h('button', { class: 'btn', type: 'button', text: '取消', on: { click: () => modal.close() } }),
    h('button', {
      class: 'btn btn-primary',
      type: 'button',
      text: '提交反馈',
      on: {
        click: async () => {
          if (!rating) {
            toast('请先选择一个评价', { tone: 'danger' });
            return;
          }
          if (onSubmit) {
            try {
              await onSubmit({ rating, tags: Array.from(picked), note: note.value.trim() });
              toast('反馈已记录，会用于调整后续生成', { tone: 'success' });
              modal.close();
            } catch (err) {
              toast(err && err.message ? err.message : '提交失败，请重试', { tone: 'danger' });
            }
          } else {
            modal.close();
          }
        }
      }
    })
  );

  const modal = openModal({ title, body, footer, size: 'sm' });
  return modal;
}

export function skeletonBlock(lines = 3) {
  const wrap = h('div', { class: 'sidebar-skeleton' });
  for (let i = 0; i < lines; i += 1) {
    wrap.append(
      h('div', {
        class: 'skeleton skeleton-line',
        style: { width: `${70 + ((i * 13) % 25)}%` }
      })
    );
  }
  return wrap;
}
