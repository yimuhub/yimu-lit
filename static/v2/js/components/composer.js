/* 输入器：自增高、附件与用途、体裁/模式、拖拽粘贴、IME 安全发送与停止。 */

import { h, icon, validateFile, ATTACH_ACCEPT, fmtWords } from '../core/dom.js';
import { store, patchUi } from '../core/state.js';
import { USAGE_MODES, usageLabel, MEDIUM_FALLBACK, mediumIcon } from '../core/constants.js';
import { toast, popoverMenu } from '../core/ui.js';
import { api, errorText, IS_MOCK } from '../core/api.js';

const MODES = [
  { value: 'chat', label: '讨论', icon: 'message', hint: '只讨论，不直接改稿' },
  { value: 'draft', label: '写作', icon: 'quill', hint: '按当前规划推进正文' },
  { value: 'rewrite', label: '重写', icon: 'rotate-ccw', hint: '重写指定段落或章节' }
];

function mediumOptions() {
  const loaded = store.get().mediums;
  const list = loaded && loaded.length ? loaded : MEDIUM_FALLBACK;
  return list.map((m) => ({ key: m.key, label: m.label, icon: mediumIcon(m), desc: m.desc || '' }));
}

export function createComposer({ variant = 'docked', ctx, onSend, onQueue, onStop, placeholder } = {}) {
  const startState = store.get();
  let mode = 'draft';
  let attachments = [];
  let streaming = false;
  let composing = false;
  let dragDepth = 0;

  const el = h('div', { class: 'composer', dataset: { variant } });
  const card = h('div', { class: 'composer-card' });
  const attachRow = h('div', { class: 'composer-attachments' });
  const textarea = h('textarea', {
    class: 'composer-input',
    rows: 1,
    'aria-label': '给神笔发消息',
    placeholder: placeholder || '描述你想写的故事，或直接说「继续第 47 章」…'
  });
  const inputWrap = h('div', { class: 'grow-wrap' }, textarea);
  const addBtn = h('button', {
    class: 'add-btn',
    type: 'button',
    'aria-label': '添加参考资料或附件',
    title: '添加参考资料 / 附件',
    html: icon('plus', 15)
  });
  const mediumChip = h('button', { class: 'composer-chip', type: 'button', 'aria-haspopup': 'menu' });
  const modeChip = h('button', { class: 'composer-chip', type: 'button', 'aria-haspopup': 'menu' });
  const hint = h('span', { class: 'composer-hint', text: 'Enter 发送 · Shift+Enter 换行' });
  const sendBtn = h('button', {
    class: 'send-btn',
    type: 'button',
    'data-mode': 'send',
    'aria-label': '发送',
    title: '发送（Enter）',
    html: icon('arrow-up', 17)
  });
  const tools = h('div', { class: 'composer-tools' }, addBtn, mediumChip, modeChip);
  const row = h('div', { class: 'composer-row' }, tools, h('div', { class: 'composer-trailing' }, hint, sendBtn));
  card.append(attachRow, inputWrap, row);
  el.append(card);

  const foot = h(
    'div',
    { class: 'composer-foot' },
    h(
      'div',
      { class: 'foot-left' },
      h('span', { html: icon('info', 12) }),
      h('span', { text: '附件与参考只用于本轮；模型输出需你确认后入库。' })
    ),
    h(
      'div',
      { class: 'foot-right' },
      h('span', { class: 'dot-sep' }),
      h('span', { text: 'Ctrl/⌘ K 命令' })
    )
  );
  if (variant === 'hero') el.append(foot);

  const drop = h(
    'div',
    { class: 'composer-drop', style: { display: 'none' } },
    h('span', { html: icon('upload', 16) }),
    h('span', { text: '松开以添加参考文件（txt / md / docx / pdf / epub）' })
  );
  el.append(drop);

  const fileInput = h('input', {
    type: 'file',
    accept: ATTACH_ACCEPT,
    multiple: true,
    style: { display: 'none' }
  });
  el.append(fileInput);

  function currentMedium() {
    return mediumOptions().find((m) => m.key === store.get().ui.medium) || mediumOptions()[0];
  }

  function refreshChips() {
    const medium = currentMedium();
    mediumChip.textContent = '';
    mediumChip.append(
      h('span', { html: icon(medium.icon, 13) }),
      h('span', { class: 'label', text: medium.label }),
      h('span', { html: icon('chevron-down', 11) })
    );
    mediumChip.title = `体裁：${medium.label}`;
    const m = MODES.find((x) => x.value === mode) || MODES[1];
    modeChip.textContent = '';
    modeChip.append(
      h('span', { html: icon(m.icon, 13) }),
      h('span', { class: 'label', text: m.label }),
      h('span', { html: icon('chevron-down', 11) })
    );
    modeChip.title = m.hint;
    modeChip.dataset.active = mode !== 'chat' ? 'true' : 'false';
    mediumChip.dataset.active = 'true';
  }
  refreshChips();

  function autoGrow() {
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(160, Math.max(0, textarea.scrollHeight))}px`;
  }

  function refreshSend() {
    const has = textarea.value.trim().length > 0 || attachments.length > 0;
    if (streaming) {
      sendBtn.dataset.mode = 'stop';
      sendBtn.disabled = false;
      sendBtn.innerHTML = icon('stop', 14);
      sendBtn.setAttribute('aria-label', '停止生成');
      sendBtn.title = '停止生成';
      hint.textContent = textarea.value.trim() ? 'Enter 加入队列' : '正在生成…';
    } else {
      sendBtn.dataset.mode = 'send';
      sendBtn.disabled = !has;
      sendBtn.innerHTML = icon('arrow-up', 17);
      sendBtn.setAttribute('aria-label', '发送');
      sendBtn.title = '发送（Enter）';
      hint.textContent = 'Enter 发送 · Shift+Enter 换行';
    }
    el.dataset.streaming = streaming ? 'true' : 'false';
  }

  function renderAttachments() {
    attachRow.textContent = '';
    for (const att of attachments) {
      const chip = h('span', {
        class: 'attach-chip',
        dataset: { state: att.state || 'ready' },
        title: att.chars ? `${att.title} · ${fmtWords(att.chars)}` : att.title
      });
      chip.append(h('span', { class: 'ac-icon', html: icon(att.state === 'uploading' ? 'clock' : 'file-text', 13) }));
      chip.append(h('span', { class: 'ac-name', text: att.title }));
      if (att.state !== 'uploading') {
        const select = h('select', { class: 'ac-usage', 'aria-label': `用途：${att.title}` });
        for (const usage of USAGE_MODES) {
          select.append(h('option', { value: usage.value, text: usage.label, selected: usage.value === att.usage_mode }));
        }
        select.addEventListener('change', () => {
          att.usage_mode = select.value;
        });
        chip.append(select);
      }
      chip.append(
        h('button', {
          class: 'ac-x',
          type: 'button',
          'aria-label': `移除 ${att.title}`,
          html: icon('x', 11),
          on: {
            click: () => {
              attachments = attachments.filter((x) => x.id !== att.id);
              renderAttachments();
              refreshSend();
            }
          }
        })
      );
      attachRow.append(chip);
    }
  }

  async function attachFile(file) {
    const check = validateFile(file);
    if (!check.ok) {
      toast(check.message, { tone: 'danger' });
      return;
    }
    const localId = `local-${Date.now()}-${Math.random().toString(36).slice(2, 5)}`;
    const att = {
      id: localId,
      title: file.name,
      usage_mode: 'facts',
      state: 'uploading',
      file
    };
    attachments.push(att);
    renderAttachments();
    refreshSend();

    if (IS_MOCK) {
      setTimeout(() => {
        att.id = `ref-${Date.now()}`;
        att.state = 'ready';
        att.chars = Math.max(1200, Math.round(file.size / 2));
        renderAttachments();
        toast(`${file.name} 已加入本轮（演示）`, { tone: 'success', duration: 1800 });
      }, 420);
      return;
    }

    const key = store.get().currentKey;
    if (!key) {
      att.state = 'ready';
      att.chars = Math.round(file.size / 2);
      renderAttachments();
      return;
    }
    try {
      const form = new FormData();
      form.append('file', file, file.name);
      form.append('filename', file.name);
      form.append('title', file.name.replace(/\.[^.]+$/, ''));
      form.append('usage_mode', att.usage_mode);
      form.append('project_key', key);
      form.append('copyright_confirmed', 'true');
      const res = await api.uploadRef(form);
      const item = (res && res.item) || {};
      att.id = item.id || localId;
      att.title = item.title || att.title;
      att.chars = item.chars || att.chars;
      att.state = 'ready';
      renderAttachments();
      if (ctx && ctx.refreshRefs) ctx.refreshRefs();
      toast(`${att.title} 已入库并加入本轮`, { tone: 'success', duration: 2200 });
    } catch (err) {
      att.state = 'error';
      renderAttachments();
      toast(errorText(err, '上传失败'), {
        tone: 'danger',
        action: { label: '移除', onClick: () => {
          attachments = attachments.filter((x) => x.id !== att.id);
          renderAttachments();
        } }
      });
    }
    refreshSend();
  }

  function attachExisting(ref) {
    if (attachments.some((a) => a.id === ref.id)) return;
    attachments.push({
      id: ref.id,
      title: ref.title,
      usage_mode: ref.usage_mode || 'facts',
      chars: ref.chars,
      state: 'ready'
    });
    renderAttachments();
    refreshSend();
  }

  addBtn.addEventListener('click', (event) => {
    const refs = store.get().refs || [];
    const items = [
      { label: '上传文件…', icon: 'upload', onSelect: () => fileInput.click() },
      { separator: true },
      { label: '已有参考资料', icon: 'folder', disabled: true }
    ];
    if (refs.length) {
      for (const ref of refs.slice(0, 12)) {
        items.push({
          label: ref.title,
          icon: ref.kind === 'url' ? 'link' : 'file-text',
          sub: usageLabel(ref.usage_mode),
          checked: attachments.some((a) => a.id === ref.id),
          onSelect: () => {
            const hit = attachments.find((a) => a.id === ref.id);
            if (hit) attachments = attachments.filter((a) => a.id !== ref.id);
            else attachExisting(ref);
            renderAttachments();
            refreshSend();
          }
        });
      }
    } else {
      items.push({ label: '还没有参考资料，先上传', icon: 'info', disabled: true });
    }
    popoverMenu(event.currentTarget, items, { align: 'start', label: '本轮引用' });
  });

  mediumChip.addEventListener('click', (event) => {
    const items = mediumOptions().map((m) => ({
      label: m.label,
      icon: m.icon,
      sub: m.desc,
      checked: m.key === currentMedium().key,
      onSelect: () => {
        patchUi({ medium: m.key });
        refreshChips();
        if (ctx && ctx.onMediumChange) ctx.onMediumChange(m.key);
      }
    }));
    popoverMenu(event.currentTarget, items, { align: 'start', label: '体裁' });
  });

  modeChip.addEventListener('click', (event) => {
    const items = MODES.map((m) => ({
      label: m.label,
      icon: m.icon,
      sub: m.hint,
      checked: m.value === mode,
      onSelect: () => {
        mode = m.value;
        refreshChips();
      }
    }));
    popoverMenu(event.currentTarget, items, { align: 'start', label: '本轮模式' });
  });

  fileInput.addEventListener('change', () => {
    const files = Array.from(fileInput.files || []);
    files.forEach((f) => attachFile(f));
    fileInput.value = '';
  });

  textarea.addEventListener('input', () => {
    autoGrow();
    refreshSend();
  });

  textarea.addEventListener('compositionstart', () => {
    composing = true;
  });
  textarea.addEventListener('compositionend', () => {
    composing = false;
    refreshSend();
  });

  textarea.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      const ime = composing || event.isComposing || event.keyCode === 229;
      if (ime) return;
      event.preventDefault();
      submit();
    }
  });

  textarea.addEventListener('paste', (event) => {
    const files = Array.from((event.clipboardData && event.clipboardData.files) || []);
    if (files.length) {
      event.preventDefault();
      files.forEach((f) => attachFile(f));
    }
  });

  ['dragenter', 'dragover'].forEach((type) => {
    el.addEventListener(type, (event) => {
      if (!event.dataTransfer || !Array.from(event.dataTransfer.types || []).includes('Files')) return;
      event.preventDefault();
      event.stopPropagation();
      if (type === 'dragenter') dragDepth += 1;
      drop.style.display = 'flex';
      el.dataset.dragging = 'true';
    });
  });
  el.addEventListener('dragleave', (event) => {
    event.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) {
      drop.style.display = 'none';
      el.dataset.dragging = 'false';
    }
  });
  el.addEventListener('drop', (event) => {
    event.preventDefault();
    event.stopPropagation();
    dragDepth = 0;
    drop.style.display = 'none';
    el.dataset.dragging = 'false';
    const files = Array.from((event.dataTransfer && event.dataTransfer.files) || []);
    if (!files.length) return;
    files.forEach((f) => attachFile(f));
  });

  function submit() {
    const text = textarea.value.trim();
    if (attachments.some((a) => a.state === 'uploading')) {
      toast('附件还在解析，请稍候再发送', { tone: 'danger' });
      return;
    }
    if (!text && !attachments.length) return;
    const payload = {
      text,
      attachments: attachments.filter((a) => a.state !== 'error').map((a) => ({ ...a })),
      mode,
      medium: currentMedium().key,
      genre: currentMedium().label
    };
    if (streaming) {
      if (!text) return;
      if (onQueue) onQueue(payload);
      reset();
      return;
    }
    if (onSend) onSend(payload);
    reset();
  }

  function reset() {
    textarea.value = '';
    attachments = [];
    renderAttachments();
    autoGrow();
    refreshSend();
    textarea.focus();
  }

  sendBtn.addEventListener('click', () => {
    if (streaming) {
      if (onStop) onStop();
      return;
    }
    submit();
  });

  refreshSend();
  renderAttachments();
  requestAnimationFrame(autoGrow);

  return {
    el,
    textarea,
    focus() {
      textarea.focus();
      const len = textarea.value.length;
      try {
        textarea.setSelectionRange(len, len);
      } catch {
        /* ignore */
      }
    },
    setText(text) {
      textarea.value = text;
      autoGrow();
      refreshSend();
    },
    setStreaming(value) {
      streaming = Boolean(value);
      refreshSend();
    },
    addReference(ref) {
      attachExisting(ref);
      refreshSend();
    },
    reset,
    getValue: () => textarea.value,
    getAttachments: () => attachments.slice()
  };
}
