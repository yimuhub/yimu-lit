/* 蒸馏风格弹窗：把目标作者的文本（或网址）交给后端蒸馏成一张命名风格卡片。
   同一套组件给技能库和新建向导复用，差别只有「蒸馏完要不要顺手选中」。 */

import { h, icon } from '../core/dom.js';
import { openModal, toast } from '../core/ui.js';
import { api, errorText } from '../core/api.js';

/* 四体裁的蒸馏维度由后端决定，这里只负责让用户选一个。 */
const MEDIUMS = [
  { value: 'novel', label: '小说' },
  { value: 'script', label: '剧本' },
  { value: 'poetry', label: '诗词' },
  { value: 'prose', label: '散文' }
];

const MAX_URLS = 5;
const TEXT_SUGGEST = 300;
const TEXT_MIN = 120;

function mediumLabel(value) {
  const hit = MEDIUMS.find((m) => m.value === value);
  return hit ? hit.label : '小说';
}

export function openDistillModal({ medium = '', genre = '', onDone } = {}) {
  let picked = MEDIUMS.some((m) => m.value === medium) ? medium : 'novel';
  let busy = false;

  const nameInput = h('input', {
    class: 'input',
    placeholder: '冷硬侦探风',
    maxlength: '40',
    'aria-label': '风格名'
  });
  const genreInput = h('input', {
    class: 'input',
    placeholder: '悬疑',
    maxlength: '20',
    'aria-label': '题材'
  });
  const textInput = h('textarea', {
    class: 'textarea',
    rows: '8',
    placeholder: '粘贴目标作者的典型段落…',
    'aria-label': '目标文本'
  });
  const urlsInput = h('textarea', {
    class: 'textarea',
    rows: '3',
    placeholder: 'https://… 可留空',
    'aria-label': '目标网址'
  });

  const mediumRow = h('div', { class: 'distill-mediums', role: 'group', 'aria-label': '体裁' });
  for (const m of MEDIUMS) {
    const btn = h('button', {
      class: 'distill-medium',
      type: 'button',
      'aria-pressed': picked === m.value ? 'true' : 'false',
      text: m.label
    });
    btn.addEventListener('click', () => {
      picked = m.value;
      mediumRow.querySelectorAll('.distill-medium').forEach((x) => {
        x.setAttribute('aria-pressed', x === btn ? 'true' : 'false');
      });
    });
    mediumRow.append(btn);
  }

  const counter = h('span', { class: 'hint-text distill-count', text: '文本 0 字，建议 300 字以上' });
  textInput.addEventListener('input', () => {
    const n = textInput.value.trim().length;
    counter.textContent = n >= TEXT_SUGGEST ? `文本 ${n} 字` : `文本 ${n} 字，建议 300 字以上`;
  });

  const body = h(
    'div',
    { class: 'distill-form' },
    h('div', { class: 'field' }, h('span', { class: 'field-label', text: '风格名' }), nameInput),
    h('div', { class: 'field' }, h('span', { class: 'field-label', text: '体裁' }), mediumRow),
    h('div', { class: 'field' }, h('span', { class: 'field-label', text: '题材（可选）' }), genreInput),
    h(
      'div',
      { class: 'field' },
      h('span', { class: 'field-label', text: '目标文本（≥300 字）' }),
      textInput,
      counter
    ),
    h(
      'div',
      { class: 'field' },
      h('span', { class: 'field-label', text: `网址（每行一个，最多 ${MAX_URLS} 个）` }),
      urlsInput
    ),
    h('p', {
      class: 'hint-text',
      text: '只分析写法不复述原文：蒸馏结果是风格技法说明与指纹词，不会搬走原句。'
    })
  );

  const submitBtn = h('button', {
    class: 'btn btn-primary',
    type: 'button',
    html: `${icon('sparkles', 14)}<span>开始蒸馏</span>`,
    on: { click: submit }
  });
  const footer = h(
    'div',
    { style: { display: 'flex', gap: '8px', width: '100%', justifyContent: 'flex-end' } },
    h('button', { class: 'btn', type: 'button', text: '取消', on: { click: () => modal.close() } }),
    submitBtn
  );

  const modal = openModal({
    title: '✨ 蒸馏新风格',
    subtitle: '粘贴目标作者的段落，蒸馏成可复用的写法卡片。',
    body,
    footer,
    size: 'md',
    className: 'distill-modal'
  });

  async function submit() {
    if (busy) return;
    const name = nameInput.value.trim();
    if (!name) {
      toast('请给这个风格起个名字', { tone: 'danger' });
      nameInput.focus();
      return;
    }
    const text = textInput.value.trim();
    const urls = urlsInput.value
      .split('\n')
      .map((x) => x.trim())
      .filter(Boolean)
      .slice(0, MAX_URLS);
    if (!text && !urls.length) {
      toast('粘贴一段目标文本，或给至少一个网址', { tone: 'danger' });
      return;
    }
    if (!urls.length && text.length < TEXT_MIN) {
      toast(`文本只有 ${text.length} 字，至少给 ${TEXT_MIN} 字以上才能蒸馏`, { tone: 'danger' });
      return;
    }
    if (!urls.length && text.length < TEXT_SUGGEST) {
      toast(`文本只有 ${text.length} 字，建议 300 字以上，蒸馏出的风格更准`, { tone: 'default' });
    }

    busy = true;
    submitBtn.disabled = true;
    setSubmitLabel('蒸馏中…');
    try {
      const res = await api.distill({
        name,
        text,
        urls,
        genre: genreInput.value.trim() || genre || '',
        medium: picked
      });
      if (!res || res.ok === false) {
        toast((res && res.note) || errorText(null, '蒸馏失败'), { tone: 'danger' });
        return;
      }
      toast('✨ 风格已生成', { tone: 'success' });
      modal.close();
      if (onDone) {
        try {
          onDone(res);
        } catch (err) {
          console.error('[distill] onDone failed', err);
        }
      }
    } catch (err) {
      toast(errorText(err, '蒸馏失败'), { tone: 'danger' });
    } finally {
      busy = false;
      submitBtn.disabled = false;
      setSubmitLabel('开始蒸馏');
    }
  }

  function setSubmitLabel(text) {
    const span = submitBtn.querySelector('span');
    if (span) span.textContent = text;
  }

  return modal;
}

export { MEDIUMS as DISTILL_MEDIUMS, mediumLabel as distillMediumLabel };
