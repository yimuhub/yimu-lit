/* 新建作品向导：三屏字段全部由 /api/onboarding.by_medium 的 schema 驱动。
   切换体裁即整屏换字段——引擎读什么，这里就问什么；不再按 medium 硬编码分支。 */

import {
  h,
  icon,
  validateFile,
  ATTACH_ACCEPT
} from '../core/dom.js';
import { store, patchUi } from '../core/state.js';
import { openModal, toast, popoverMenu } from '../core/ui.js';
import { GENRE_OPTIONS, SCALE_OPTIONS, WRITING_MODES, USAGE_MODES, MEDIUM_FALLBACK, mediumIcon } from '../core/constants.js';
import { api, errorText } from '../core/api.js';
import { openDistillModal } from '../components/distill.js';

/* /api/onboarding 是向导的唯一词表源（题材/基调/视角/卖点/风格预设 + 平台 + 引擎词表）。
   只在第一次打开向导时取一次，失败则回落到本地最小 schema，不阻塞创建。 */
let ONB = null;
let ONB_PROMISE = null;

function onb() {
  return ONB || {};
}

function ensureOnboarding() {
  if (!ONB_PROMISE) {
    ONB_PROMISE = api
      .onboarding()
      .then((data) => {
        ONB = data || {};
        return ONB;
      })
      .catch(() => {
        ONB = {};
        return ONB;
      });
  }
  return ONB_PROMISE;
}

function platformList() {
  return onb().platforms || [];
}

function styleDims() {
  return onb().style || {};
}

function mediumList() {
  const loaded = store.get().mediums || [];
  return loaded.length ? loaded : MEDIUM_FALLBACK;
}

function mediumOf(key) {
  return mediumList().find((m) => m.key === key) || mediumList()[0] || MEDIUM_FALLBACK[0];
}

/* 词表没到位时的本地兜底：小说仍能选齐题材/规模，其余文体至少给出必填起点。 */
function localSchema(medium) {
  if (medium !== 'novel') {
    return {
      brief: [],
      basic: [
        { key: 'title', label: '标题', type: 'text', max_length: 40 },
        { key: 'idea', label: '一句话起点', type: 'textarea', rows: 4, required: true }
      ],
      style: []
    };
  }
  return {
    brief: [
      { key: 'genre', label: '题材', type: 'select', required: true, default: '悬疑',
        options: GENRE_OPTIONS.map((g) => ({ value: g, label: g })) },
      { key: 'mode', label: '创作模式', type: 'cards', default: 'rolling',
        options: WRITING_MODES.map((m) => ({ value: m.value, label: m.label, desc: m.desc, icon: m.icon })) }
    ],
    basic: [
      { key: 'title', label: '书名', type: 'text', max_length: 40 },
      { key: 'idea', label: '一句话 premise', type: 'textarea', rows: 4, required: true },
      { key: 'scale', label: '规模', type: 'scale', default: '1m', custom_key: 'total_chapters',
        min: 1, max: 2000, unit: '章',
        options: SCALE_OPTIONS.map((s) => ({ value: s.value, label: s.label, desc: s.desc, fill: s.chapters })) }
    ],
    style: []
  };
}

function sectionsFor(medium) {
  const by = onb().by_medium;
  return (by && by[medium]) || localSchema(medium);
}

function fieldsOf(medium, group) {
  return sectionsFor(medium)[group] || [];
}

function optionsOf(field) {
  return (field.options || []).map((o) => (typeof o === 'string' ? { value: o, label: o } : o));
}

/* 规模字段的「自定义」圆点是渲染器统一补的，schema 只声明快捷档 */
const CUSTOM_TOKEN = 'custom';

function scaleOptions(field) {
  return [...optionsOf(field), { value: CUSTOM_TOKEN, label: '自定义' }];
}

function fillOf(field, token) {
  const hit = optionsOf(field).find((o) => String(o.value) === String(token));
  return hit && Number.isFinite(Number(hit.fill)) ? Number(hit.fill) : null;
}

function sameToken(a, b) {
  return String(a) === String(b);
}

function humanValue(field, token) {
  const hit = optionsOf(field).find((o) => sameToken(o.value, token));
  return hit ? hit.label : String(token ?? '');
}

function isVisible(field, values) {
  const rule = field.when;
  if (!rule) return true;
  return (rule.in || []).some((x) => sameToken(values[rule.field], x));
}

function stepsBar(step) {
  const labels = ['体裁与模式', '基本信息', '参考与风格'];
  const bar = h('div', { class: 'wiz-steps', role: 'list', 'aria-label': '新建作品步骤' });
  labels.forEach((label, index) => {
    const n = index + 1;
    const stateName = step === n ? 'active' : step > n ? 'done' : 'todo';
    if (index) bar.append(h('span', { class: 'wiz-step-line' }));
    bar.append(
      h(
        'div',
        { class: 'wiz-step', dataset: { state: stateName }, role: 'listitem', 'aria-current': step === n ? 'step' : 'false' },
        h('span', { class: 'num', html: step > n ? icon('check', 12) : String(n) }),
        h('span', { class: 'step-label', text: label })
      )
    );
  });
  return bar;
}

function field(label, control, { hint = '', optionalText = '' } = {}) {
  const wrap = h('div', { class: 'wiz-group' });
  wrap.append(
    h(
      'span',
      { class: 'wiz-label' },
      h('span', { text: label }),
      optionalText ? h('span', { class: 'opt', text: optionalText }) : null
    ),
    control
  );
  if (hint) wrap.append(h('span', { class: 'hint-text', text: hint }));
  return wrap;
}

function chip(label, active, onClick, { title = '' } = {}) {
  const btn = h('button', { class: 'wiz-chip', type: 'button', 'aria-pressed': active ? 'true' : 'false' });
  if (title) btn.setAttribute('title', title);
  btn.append(h('span', { text: label }));
  btn.addEventListener('click', onClick);
  return btn;
}

export function openWizard(ctx, preset = {}) {
  const state = store.get();
  const model = {
    step: 1,
    medium: preset.medium || state.ui.medium || 'novel',
    values: {},
    refs: [],
    existing: new Set(),
    styleId: preset.styleId || '',
    /* 蒸馏出来的风格当场就能选中，不必等下次启动再从 store 取。 */
    styles: (state.styles || []).slice(),
    copyright: true,
    submitting: false,
    error: '',
    status: ''
  };
  let stylesFetched = false;

  /* store 里的风格列表可能是旧的（首页启动时才拉过一次），这里自己再拉一次兜底。 */
  async function ensureStyles() {
    if (stylesFetched) return;
    stylesFetched = true;
    let items = [];
    try {
      const res = await api.styles();
      items = Array.isArray(res) ? res : (res && (res.items || res.styles)) || [];
    } catch {
      /* 拉不到就先用 store 里那份，不挡开书 */
      return;
    }
    if (destroyed) return;
    const list = items.filter(Boolean);
    if (!list.length) return;
    const known = new Set(model.styles.map((s) => s.name || s.id));
    if (model.styles.length && list.every((s) => known.has(s.name || s.id))) return;
    model.styles = list;
    render();
  }

  /* 每种体裁一套值，来回切体裁不丢已填内容。 */
  function values() {
    return valuesOf(model.medium);
  }

  /* 逐字段补齐缺失的默认值：/api/onboarding 晚于首帧到达时，本地兜底 schema 建的值袋
     会少了真 schema 才有的字段（如每章字数），不补就会渲染成空并误报越界。 */
  function valuesOf(medium) {
    const bag = model.values[medium] || (model.values[medium] = {});
    ['brief', 'basic', 'style'].forEach((name) => {
      for (const f of fieldsOf(medium, name)) {
        if (f.type === 'note' || f.type === 'preview') continue;
        if (f.type === 'scale') {
          if (bag[f.key] === undefined) bag[f.key] = f.default || (optionsOf(f)[0] || {}).value || '';
          if (bag[f.custom_key] === undefined) {
            const n = fillOf(f, bag[f.key]);
            bag[f.custom_key] = n === null ? f.min || 1 : n;
          }
        } else if (bag[f.key] === undefined) {
          bag[f.key] = f.default !== undefined ? f.default : f.multi ? [] : '';
        }
      }
    });
    return bag;
  }

  for (const item of preset.attachments || []) {
    if (item.file) {
      model.refs.push({
        id: `local-${Math.random().toString(36).slice(2, 8)}`,
        kind: 'file',
        title: item.title || item.file.name,
        file: item.file,
        usage_mode: item.usage_mode || 'facts'
      });
    } else if (item.id) {
      model.existing.add(item.id);
    }
  }

  /* 平台只作用于小说：切走体裁时清掉平台维度，避免把网文分类带进诗词/剧本。 */
  function platformOf(key) {
    return platformList().find((p) => p.key === key) || null;
  }

  function applyPlatform(plat) {
    const v = values();
    v.platform = plat ? plat.key : '';
    v.sells = [];
    if (!plat) {
      v.style_preset = '';
      const choices = optionsOf(genreField());
      if (!choices.some((o) => sameToken(o.value, v.genre))) {
        v.genre = (genreField().default || choices[0] || {}).value || '';
      }
      return;
    }
    v.style_preset = plat.style_preset || '';
    const allowed = (plat.genres || []).length ? plat.genres : null;
    const choices = optionsOf(genreField());
    if (allowed && !allowed.includes(v.genre)) v.genre = allowed[0];
    else if (!choices.some((o) => sameToken(o.value, v.genre))) v.genre = (choices[0] || {}).value || '';
    v.sells = (plat.hot_tags || []).slice(0, 3);
  }

  function genreField() {
    const found = fieldsOf(model.medium, 'brief').find((f) => f.key === 'genre');
    return found || { key: 'genre', type: 'select', options: [], default: '' };
  }

  /* v3.1.0：用户对风格维度的逐项覆盖（点「本次生效」里的值 chip 修改） */
  const styleOverrides = {};
  function resolvedStyle() {
    const v = values();
    const presets = styleDims().preset || {};
    const base = presets[v.genre] || presets[v.style_preset] || null;
    if (!base) return null;
    const merged = { ...base };
    for (const [k, val] of Object.entries(styleOverrides)) {
      if (val) merged[k] = val;
    }
    return merged;
  }

  /* 字段语义效果登记表：schema 用 effect 声明，渲染器只认这个名字，不含体裁分支。 */
  const EFFECTS = {
    platform: (token) => {
      applyPlatform(platformOf(token));
      render();
    }
  };

  const body = h('div', { class: 'wiz-body' });
  const footer = h('div', { style: { display: 'flex', gap: '8px', alignItems: 'center', width: '100%' } });

  const modal = openModal({
    title: '新建作品',
    subtitle: '先对齐体裁与规模，再把参考与文风交给神笔。创建后可以随时调整规划。',
    body,
    footer,
    size: 'lg',
    className: 'wizard',
    initialFocus: null
  });

  let destroyed = false;
  const origClose = modal.close;
  modal.close = () => {
    destroyed = true;
    origClose();
  };

  /* 空态返回 hidden 元素而不是 null：Element.append(null) 会把 "null" 当文本渲染。 */
  function errorBanner() {
    const box = h('div', { class: 'wiz-error', role: 'alert' });
    if (!model.error) box.hidden = true;
    else box.append(h('span', { html: icon('alert', 14) }), h('div', { text: model.error }));
    return box;
  }

  function statusLine() {
    const line = h('div', { class: 'hint-text', style: { marginTop: '6px' } });
    if (!model.status) line.hidden = true;
    else line.textContent = model.status;
    return line;
  }

  /* ---------------------------------------------------------- 通用字段渲染 */
  function set(fieldDef, next) {
    const v = values();
    v[fieldDef.key] = next;
    if (fieldDef.effect && EFFECTS[fieldDef.effect]) EFFECTS[fieldDef.effect](next);
  }

  function controlFor(fieldDef) {
    const v = values();
    switch (fieldDef.type) {
      case 'note':
        return h('p', { class: 'wiz-note', text: fieldDef.text || '' });
      case 'preview':
        return previewControl();
      case 'select': {
        const sel = h('select', { class: 'select', 'aria-label': fieldDef.label });
        if (!fieldDef.required) sel.append(h('option', { value: '', text: `不限${fieldDef.label}` }));
        for (const o of optionsOf(fieldDef)) {
          sel.append(h('option', { value: String(o.value), text: o.label, selected: sameToken(v[fieldDef.key], o.value) }));
        }
        sel.addEventListener('change', () => {
          const hit = optionsOf(fieldDef).find((o) => sameToken(o.value, sel.value));
          set(fieldDef, sel.value);
          /* 题材带 craft key（skillkit 归一结果），后端按 key 注入，不再靠中文字面匹配。 */
          if (hit && hit.key) v[`${fieldDef.key}_key`] = hit.key;
          render();
        });
        const hit = optionsOf(fieldDef).find((o) => sameToken(o.value, v[fieldDef.key]));
        if (hit && hit.key) v[`${fieldDef.key}_key`] = hit.key;
        return sel;
      }
      case 'number': {
        const input = h('input', {
          class: 'input wiz-number',
          type: 'number',
          min: String(fieldDef.min ?? 1),
          max: String(fieldDef.max ?? 100000),
          value: String(v[fieldDef.key] ?? ''),
          'aria-label': fieldDef.label
        });
        input.addEventListener('input', () => {
          v[fieldDef.key] = input.value === '' ? '' : Number(input.value) || 0;
        });
        return h('div', { class: 'wiz-with-unit' }, input, h('span', { class: 'scale-unit', text: fieldDef.unit || '' }));
      }
      case 'text': {
        const input = h('input', {
          class: 'input',
          maxlength: String(fieldDef.max_length || 80),
          placeholder: fieldDef.placeholder || '',
          value: v[fieldDef.key] || '',
          'aria-label': fieldDef.label
        });
        input.addEventListener('input', () => {
          v[fieldDef.key] = input.value;
        });
        return input;
      }
      case 'textarea': {
        const area = h('textarea', {
          class: 'textarea',
          rows: String(fieldDef.rows || 4),
          maxlength: String(fieldDef.max_length || 4000),
          placeholder: fieldDef.placeholder || '',
          'aria-label': fieldDef.label
        });
        area.value = v[fieldDef.key] || '';
        area.addEventListener('input', () => {
          v[fieldDef.key] = area.value;
        });
        return area;
      }
      case 'toggle': {
        const box = h('input', { type: 'checkbox', checked: v[fieldDef.key] !== false });
        box.addEventListener('change', () => {
          v[fieldDef.key] = box.checked;
        });
        return h('label', { class: 'wiz-toggle' }, box, h('span', { text: v[fieldDef.key] !== false ? '开启' : '关闭' }));
      }
      case 'chips': {
        const row = h('div', { class: 'wiz-chips' });
        const single = (key) => !fieldDef.multi && sameToken(v[fieldDef.key], key);
        if (!fieldDef.multi && !fieldDef.required) {
          row.append(chip(fieldDef.none_label || '不限', single(''), () => {
            set(fieldDef, '');
            render();
          }));
        }
        for (const o of optionsOf(fieldDef)) {
          const on = fieldDef.multi ? (v[fieldDef.key] || []).includes(o.value) : single(o.value);
          row.append(
            chip(o.label, on, () => {
              if (fieldDef.multi) {
                const list = v[fieldDef.key] || [];
                v[fieldDef.key] = list.includes(o.value) ? list.filter((x) => x !== o.value) : [...list, o.value];
              } else {
                set(fieldDef, on ? '' : o.value);
              }
              render();
            }, { title: o.desc || '' })
          );
        }
        if (!optionsOf(fieldDef).length && fieldDef.multi) row.append(h('span', { class: 'hint-text', text: '暂无可选项。' }));
        return row;
      }
      case 'cards': {
        const cards = h('div', { class: 'mode-cards' });
        for (const o of optionsOf(fieldDef)) {
          const on = sameToken(v[fieldDef.key], o.value);
          const btn = h(
            'button',
            { class: 'mode-card', type: 'button', 'aria-pressed': on ? 'true' : 'false' },
            h('span', { class: 'mc-name' }, h('span', { html: icon(o.icon || 'book', 13) }), h('span', { text: o.label })),
            h('span', { class: 'mc-desc', text: o.desc || '' })
          );
          btn.addEventListener('click', () => {
            set(fieldDef, o.value);
            render();
          });
          cards.append(btn);
        }
        return cards;
      }
      case 'scale':
        return scaleControl(fieldDef);
      case 'radio':
      default:
        return radioControl(fieldDef);
    }
  }

  /* 规模 = 小圆点快捷档 + 末位「自定义」；选中自定义才露出数字框。 */
  function scaleControl(fieldDef) {
    const v = values();
    const name = `${model.medium}-${fieldDef.key}`;
    const box = h('div', { class: 'wiz-radios wiz-scale', role: 'radiogroup', 'aria-label': fieldDef.label });
    for (const o of scaleOptions(fieldDef)) {
      const on = sameToken(v[fieldDef.key], o.value);
      const radio = h('input', { type: 'radio', name, value: String(o.value), checked: on });
      radio.addEventListener('change', () => {
        v[fieldDef.key] = o.value;
        if (o.value !== CUSTOM_TOKEN) {
          const n = fillOf(fieldDef, o.value);
          if (n !== null) v[fieldDef.custom_key] = n;
        }
        render();
      });
      box.append(
        h('label', { class: 'opt-radio' }, radio, h('span', { text: o.label }), o.desc ? h('span', { class: 'muted', text: o.desc }) : null)
      );
    }
    if (sameToken(v[fieldDef.key], CUSTOM_TOKEN)) {
      const input = h('input', {
        class: 'input wiz-number',
        type: 'number',
        min: String(fieldDef.min || 1),
        max: String(fieldDef.max || 2000),
        value: String(v[fieldDef.custom_key] || ''),
        'aria-label': `${fieldDef.label}自定义`
      });
      input.addEventListener('input', () => {
        v[fieldDef.custom_key] = Number(input.value) || 0;
      });
      box.append(h('div', { class: 'wiz-with-unit' }, input, h('span', { class: 'scale-unit', text: fieldDef.unit || '' })));
    }
    return box;
  }

  function radioControl(fieldDef) {
    const v = values();
    const name = `${model.medium}-${fieldDef.key}`;
    const box = h('div', { class: 'wiz-radios', role: 'radiogroup', 'aria-label': fieldDef.label });
    for (const o of optionsOf(fieldDef)) {
      const on = sameToken(v[fieldDef.key], o.value);
      const radio = h('input', { type: 'radio', name, value: String(o.value), checked: on });
      radio.addEventListener('change', () => {
        set(fieldDef, o.value);
        render();
      });
      box.append(
        h('label', { class: 'opt-radio' }, radio, h('span', { text: o.label }), o.desc ? h('span', { class: 'muted', text: o.desc }) : null)
      );
    }
    return box;
  }

  function previewControl() {
    /* v3.1.0：「本次生效」每个维度值可点击 → 弹出该维度候选（含自定义输入），改完本次创建生效 */
    const row = h('div', { class: 'wiz-chips' });
    const resolved = resolvedStyle();
    if (!resolved) {
      row.append(h('span', { class: 'hint-text', text: ONB === null ? '维度数据尚未加载完，创建后仍可在作品规划里调整。' : '暂无可用预设。' }));
      return row;
    }
    const dims = styleDims() || {};
    for (const [k, val] of Object.entries(resolved)) {
      const chip = h('button', {
        class: 'wiz-chip' + (styleOverrides[k] ? ' is-overridden' : ''),
        type: 'button',
        title: '点击更换该维度（本次创建生效）',
        'aria-haspopup': 'menu'
      });
      chip.append(h('span', { text: `${k}：${val}` }));
      chip.addEventListener('click', (event) => {
        const options = (Array.isArray(dims[k]) ? dims[k] : []).filter((x) => x !== '自定义');
        const items = options.map((opt) => ({
          label: opt,
          icon: 'dot',
          checked: opt === resolved[k],
          onSelect: () => { styleOverrides[k] = opt; render(); }
        }));
        items.push({ separator: true });
        items.push({
          label: '恢复题材预设',
          icon: 'refresh',
          onSelect: () => { delete styleOverrides[k]; render(); }
        });
        popoverMenu(event.currentTarget, items, { label: `${k} · 选择` });
      });
      row.append(chip);
    }
    row.append(h('span', { class: 'hint-text', text: '点值可改，本次创建生效；规模与章数由第二步决定' }));
    return row;
  }

  /* v4.12.1：AI 自动生成按钮——点击后根据文体和已有信息生成标题/方向 */
  function addAiGenerateBtn(pane, targetField) {
    const btn = h('button', {
      class: 'btn btn-sm wiz-ai-btn',
      type: 'button',
      html: `${icon('wand', 13)}<span>AI 自动生成</span>`,
      on: {
        click: async (event) => {
          btn.disabled = true;
          const span = btn.querySelector('span');
          if (span) span.textContent = '生成中…';
          try {
            const hint = values()[targetField] || values().idea || '';
            const res = await api.aiGenerate({ medium: model.medium, field: targetField, hint });
            if (targetField === 'title' && res.title) {
              model.values[model.medium].title = res.title;
            } else if (res.idea) {
              model.values[model.medium].idea = res.idea;
            }
            render();
            toast('已生成，可继续编辑', { tone: 'success', duration: 1800 });
          } catch (err) {
            toast(errorText(err, '生成失败'), { tone: 'danger' });
          }
          btn.disabled = false;
          if (span) span.textContent = 'AI 自动生成';
        }
      }
    });
    pane.append(btn);
  }

  function renderGroup(name, { span = false } = {}) {
    const pane = h('div', { class: span ? 'wiz-grid' : 'wiz-stack' });
    for (const f of fieldsOf(model.medium, name)) {
      if (!isVisible(f, values())) continue;
      if (f.type === 'note') {
        pane.append(h('p', { class: 'wiz-note', text: f.text || '' }));
        continue;
      }
      const ctrl = controlFor(f);
      /* v4.12.9：title/idea 字段旁加「AI 随机生成」按钮 */
      if (name === 'basic' && (f.key === 'title' || f.key === 'idea')) {
        const aiBtn = h('button', {
          class: 'btn btn-sm wiz-ai-inline', type: 'button',
          html: `${icon('wand', 12)}<span>AI 生成</span>`,
          title: 'AI 根据文体自动生成内容（每次不同）',
          on: {
            click: async (event) => {
              event.preventDefault();
              const btn = event.currentTarget;
              btn.disabled = true;
              const sp = btn.querySelector('span');
              if (sp) sp.textContent = '生成中…';
              try {
                const hint = values().idea || values().title || '';
                const res = await api.aiGenerate({ medium: model.medium, field: f.key, hint });
                if (f.key === 'title' && res.title) {
                  model.values[model.medium].title = res.title;
                } else if (res.idea) {
                  model.values[model.medium].idea = res.idea;
                }
                render();
                toast('已生成，可继续编辑', { tone: 'success', duration: 1500 });
              } catch (err) {
                toast(errorText(err, '生成失败'), { tone: 'danger' });
              }
              btn.disabled = false;
              if (sp) sp.textContent = 'AI 生成';
            }
          }
        });
        const row = h('div', { class: 'wiz-field-ai-row' }, ctrl, aiBtn);
        const wrap2 = field(f.label, row, {
          hint: f.hint || '',
          optionalText: f.required ? '' : '可选'
        });
        if (span && (f.type === 'textarea' || f.type === 'scale' || f.type === 'chips' || f.type === 'preview')) {
          wrap2.classList.add('span-2');
        }
        pane.append(wrap2);
        continue;
      }
      const wrap = field(f.label, ctrl, {
        hint: f.hint || '',
        optionalText: f.required ? '' : '可选'
      });
      if (span && (f.type === 'textarea' || f.type === 'scale' || f.type === 'chips' || f.type === 'preview')) {
        wrap.classList.add('span-2');
      }
      pane.append(wrap);
    }
    return pane;
  }

  /* ---------------------------------------------------------- step 1 */
  function stepOne() {
    const pane = h('div', { class: 'wiz-pane' });
    pane.append(
      h(
        'div',
        { class: 'wiz-pane-title' },
        h('h3', { text: '这次想写什么？' }),
        h('p', { text: '体裁决定要填哪些维度：小说问题材与规模，剧本问形式与入口，诗词问体式与韵书，散文问文体与操作，论文问层次与引用格式。' })
      )
    );

    const cards = h('div', { class: 'genre-cards' });
    for (const medium of mediumList()) {   // v4.6.2：论文等全部体裁都出现在向导第一步
      const active = model.medium === medium.key;
      const card = h(
        'button',
        { class: 'genre-card', type: 'button', 'aria-pressed': active ? 'true' : 'false', dataset: { medium: medium.key } },
        h('span', { class: 'gc-icon', html: icon(mediumIcon(medium), 17) }),
        h('span', { class: 'gc-name', text: medium.label }),
        h('span', { class: 'gc-desc', text: medium.desc || '' }),
        h('span', { class: 'gc-check', html: icon('check', 12) })
      );
      card.addEventListener('click', () => {
        model.medium = medium.key;
        valuesOf(medium.key);
        if (medium.key !== 'novel') {
          const v = valuesOf(medium.key);
          if ('platform' in v) v.platform = '';
        }
        render();
      });
      cards.append(card);
    }
    pane.append(field('体裁', cards));
    pane.append(renderGroup('brief'));
    pane.append(errorBanner(), statusLine());
    return pane;
  }

  /* ---------------------------------------------------------- step 2 */
  function stepTwo() {
    const pane = h('div', { class: 'wiz-pane' });
    pane.append(
      h(
        'div',
        { class: 'wiz-pane-title' },
        h('h3', { text: `给它一个起点` }),
        h('p', { text: `${mediumOf(model.medium).label}需要知道的规模与素材。留空的项由系统按体裁默认。` })
      )
    );
    pane.append(renderGroup('basic', { span: true }));
    pane.append(errorBanner(), statusLine());
    return pane;
  }

  /* ---------------------------------------------------------- step 3 */
  function stageRef(ref) {
    model.refs.push(ref);
    render();
  }

  function addFiles(files) {
    let added = 0;
    for (const file of files) {
      const check = validateFile(file);
      if (!check.ok) {
        toast(check.message, { tone: 'danger' });
        continue;
      }
      stageRef({
        id: `local-${Math.random().toString(36).slice(2, 8)}`,
        kind: 'file',
        title: file.name,
        file,
        usage_mode: 'facts'
      });
      added += 1;
    }
    return added;
  }

  function refsPane() {
    const wrap = h('div', { class: 'ref-step' });
    const head = h('div', { class: 'ref-step-head' });
    head.append(
      h('span', { class: 'rsh-title', html: `${icon('folder', 14)}<span>参考资料</span>` }),
      h('span', { class: 'badge', text: `${model.refs.length} 份待上传` })
    );
    wrap.append(head);
    wrap.append(
      h('div', { class: 'hint-text', text: '可以先放资料，再开始规划。资料只在创建后关联到这本作品。' })
    );

    const actions = h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '6px', margin: '8px 0' } });
    const fileInput = h('input', {
      type: 'file',
      accept: ATTACH_ACCEPT,
      multiple: true,
      style: { display: 'none' }
    });
    fileInput.addEventListener('change', () => {
      addFiles(Array.from(fileInput.files || []));
      fileInput.value = '';
    });
    actions.append(
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        html: `${icon('upload', 13)}<span>选择文件</span>`,
        on: { click: () => fileInput.click() }
      }),
      fileInput,
      h('span', { class: 'hint-text', text: 'txt / md / docx / pdf / epub，单份 ≤20 MB' })
    );

    const textTitle = h('input', { class: 'input', placeholder: '资料标题' });
    const textBody = h('textarea', { class: 'textarea', rows: 2, placeholder: '粘贴正文或摘录…' });
    const textUsage = h(
      'select',
      { class: 'select' },
      ...USAGE_MODES.map((u) => h('option', { value: u.value, text: u.label }))
    );
    const textBtn = h('button', {
      class: 'btn btn-sm',
      type: 'button',
      text: '加入',
      on: {
        click: () => {
          if (!textBody.value.trim()) {
            toast('请先粘贴资料正文', { tone: 'danger' });
            return;
          }
          stageRef({
            id: `local-${Math.random().toString(36).slice(2, 8)}`,
            kind: 'text',
            title: textTitle.value.trim() || '未命名资料',
            text: textBody.value.trim(),
            usage_mode: textUsage.value
          });
        }
      }
    });
    actions.append(
      h(
        'div',
        { class: 'ref-form', style: { width: '100%' } },
        h('div', { class: 'form-row' }, textTitle, textUsage, textBtn),
        textBody
      )
    );
    wrap.append(actions);

    const list = h('div', { class: 'staged-refs' });
    if (!model.refs.length) {
      list.append(h('div', { class: 'hint-text', style: { padding: '4px 2px' }, text: '还没有待上传资料。也可以直接创建，之后再补充。' }));
    }
    for (const ref of model.refs) {
      const row = h('div', { class: 'staged-ref' });
      row.append(
        h('span', { class: 'sr-name', title: ref.title, text: ref.title }),
        h('span', { class: 'sr-mode', text: ref.file ? '文件' : ref.kind === 'text' ? '文本' : '链接' })
      );
      const usage = h('select', { class: 'select', 'aria-label': `用途：${ref.title}` });
      for (const u of USAGE_MODES) {
        usage.append(h('option', { value: u.value, text: u.label, selected: u.value === ref.usage_mode }));
      }
      usage.addEventListener('change', () => {
        ref.usage_mode = usage.value;
      });
      row.append(
        usage,
        h('button', {
          class: 'sr-x',
          type: 'button',
          'aria-label': `移除 ${ref.title}`,
          html: icon('x', 11),
          on: {
            click: () => {
              model.refs = model.refs.filter((x) => x.id !== ref.id);
              render();
            }
          }
        })
      );
      list.append(row);
    }
    wrap.append(list);

    const existing = (store.get().refs || []).slice(0, 8);
    if (existing.length) {
      wrap.append(h('div', { class: 'ref-or', text: '或使用已有资料' }));
      const existWrap = h('div', { class: 'staged-refs' });
      for (const ref of existing) {
        const checked = model.existing.has(ref.id);
        const row = h('label', { class: 'staged-ref', style: { cursor: 'pointer' } });
        const box = h('input', { type: 'checkbox', checked });
        box.addEventListener('change', () => {
          if (box.checked) model.existing.add(ref.id);
          else model.existing.delete(ref.id);
        });
        row.append(box, h('span', { class: 'sr-name', text: ref.title }), h('span', { class: 'sr-mode', text: ref.usage_mode === 'style' ? '文风' : '事实' }));
        existWrap.append(row);
      }
      wrap.append(existWrap);
    }

    const copyright = h('input', { type: 'checkbox', checked: model.copyright });
    copyright.addEventListener('change', () => {
      model.copyright = copyright.checked;
    });
    wrap.append(
      h(
        'label',
        { class: 'copyright', style: { display: 'flex', gap: '8px', alignItems: 'flex-start', marginTop: '8px', fontSize: '12px', color: 'var(--ink-tertiary)' } },
        copyright,
        h('span', { text: '我确认上传的资料为自己创作、拥有权利或已获授权；参考资料只用于辅助创作。' })
      )
    );
    return wrap;
  }

  function stylePane() {
    const wrap = h('div', { class: 'ref-step' });
    const head = h('div', { class: 'ref-step-head' });
    head.append(
      h('span', { class: 'rsh-title', html: `${icon('palette', 14)}<span>蒸馏风格</span>` }),
      h('span', { class: 'badge', text: '可选' }),
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        html: `${icon('sparkles', 13)}<span>蒸馏新风格</span>`,
        on: { click: () => distillHere() }
      })
    );
    wrap.append(head);
    const styles = model.styles;
    const grid = h('div', { class: 'style-grid' });
    const none = h(
      'button',
      { class: 'style-opt', type: 'button', 'aria-pressed': model.styleId === '' ? 'true' : 'false' },
      h('span', { class: 'so-name', text: '不指定' }),
      h('span', { class: 'so-sub', text: '由系统按题材与规划自动选择' })
    );
    none.addEventListener('click', () => {
      model.styleId = '';
      render();
    });
    grid.append(none);
    for (const style of styles) {
      const btn = h(
        'button',
        { class: 'style-opt', type: 'button', 'aria-pressed': model.styleId === style.id || model.styleId === style.name ? 'true' : 'false' },
        h('span', { class: 'so-name', text: style.name }),
        h('span', { class: 'so-sub', text: style.summary || style.genre || '' })
      );
      btn.addEventListener('click', () => {
        model.styleId = style.id || style.name;
        render();
      });
      grid.append(btn);
    }
    wrap.append(grid);
    if (!styles.length) {
      wrap.append(h('div', { class: 'hint-text', text: '还没有蒸馏风格。点上面的「蒸馏新风格」粘一段目标文本就能现场生成，本次先自动选择。' }));
    }
    return wrap;
  }

  /* 创作流程里就地蒸馏：不用跳去别的页面，蒸馏完顺手把新风格选中。 */
  function distillHere() {
    const genre = String((values() || {}).genre || '');
    openDistillModal({
      medium: model.medium,
      genre,
      onDone: (card) => {
        const style = (card && card.style) || {};
        const name = (card && card.name) || style.name || '';
        if (!name) return;
        const item = {
          id: name,
          name,
          genre: style.genre || genre || '',
          summary: style.summary || (card && card.summary) || '',
          medium: style.medium || model.medium,
          fingerprint_words: style.fingerprint_words || []
        };
        model.styles = [item, ...model.styles.filter((s) => (s.name || s.id) !== name)];
        model.styleId = name;
        render();
      }
    });
  }

  /* 摘要只列有值的可见字段，长文本单独成段。 */
  function summaryBox() {
    const v = values();
    const box = h('div', { class: 'wiz-summary' });
    const rows = [['体裁', mediumOf(model.medium).label]];
    const longTexts = [];
    ['brief', 'basic', 'style'].forEach((name) => {
      for (const f of fieldsOf(model.medium, name)) {
        if (!isVisible(f, v) || f.type === 'note' || f.type === 'preview') continue;
        const raw = v[f.key];
        if (f.type === 'textarea') {
          if (String(raw || '').trim()) longTexts.push(`${f.label}：${String(raw).trim()}`);
          continue;
        }
        if (f.type === 'scale') {
          const n = v[f.custom_key];
          rows.push([f.label, sameToken(v[f.key], CUSTOM_TOKEN) ? `自定义 ${n} ${f.unit || ''}` : `${humanValue(f, v[f.key])} · ${n} ${f.unit || ''}`]);
          continue;
        }
        if (raw === '' || raw === null || raw === undefined) continue;
        if (Array.isArray(raw)) {
          if (raw.length) rows.push([f.label, raw.join('、')]);
          continue;
        }
        if (typeof raw === 'boolean') {
          rows.push([f.label, raw ? '开启' : '关闭']);
          continue;
        }
        const shown = optionsOf(f).length ? humanValue(f, raw) : `${raw}${f.unit || ''}`;
        rows.push([f.label, f.type === 'number' ? `${raw} ${f.unit || ''}` : shown]);
      }
    });
    if (model.styleId) rows.push(['蒸馏风格', model.styleId]);
    rows.push(['参考', `${model.refs.length + model.existing.size} 份`]);
    for (const [k, val] of rows) box.append(h('div', {}, h('span', { text: k }), h('b', { text: String(val) })));
    for (const line of longTexts) box.append(h('p', { text: line }));
    return box;
  }

  function stepThree() {
    const pane = h('div', { class: 'wiz-pane' });
    ensureStyles();
    pane.append(
      h(
        'div',
        { class: 'wiz-pane-title' },
        h('h3', { text: '把参考与文风交给它' }),
        h('p', { text: '创建前先放好资料和风格，第一版规划会更贴近你想要的东西。' })
      )
    );
    const extra = renderGroup('style');
    pane.append(refsPane(), stylePane());
    if (extra.childElementCount) {
      const step = h('div', { class: 'ref-step' });
      step.append(
        h(
          'div',
          { class: 'ref-step-head' },
          h('span', { class: 'rsh-title', html: `${icon('sliders', 14)}<span>${sectionsFor(model.medium).style_title || '本体裁附加项'}</span>` }),
          h('span', { class: 'badge', text: mediumOf(model.medium).label })
        ),
        extra
      );
      pane.append(step);
    }
    pane.append(summaryBox(), errorBanner(), statusLine());
    return pane;
  }

  function renderFooter() {
    footer.textContent = '';
    if (model.step > 1) {
      footer.append(
        h('button', {
          class: 'btn',
          type: 'button',
          text: '上一步',
          disabled: model.submitting,
          on: { click: () => goBack() }
        })
      );
    } else {
      footer.append(
        h('button', {
          class: 'btn',
          type: 'button',
          text: '取消',
          disabled: model.submitting,
          on: { click: () => modal.close() }
        })
      );
    }
    footer.append(h('span', { style: { flex: '1' } }));
    if (model.step < 3) {
      footer.append(
        h('button', {
          class: 'btn btn-primary',
          type: 'button',
          html: `<span>下一步</span>${icon('arrow-right', 14)}`,
          on: { click: () => goNext() }
        })
      );
    } else {
      const v = values();
      footer.append(
        h('button', {
          class: 'btn btn-primary',
          type: 'button',
          html: model.submitting
            ? '<span class="wiz-loading"><span class="spinner"></span>创建中…</span>'
            : `<span>${String(v.title || '').trim() ? '创建并开始' : '创建作品'}</span>${icon('arrow-right', 14)}`,
          disabled: model.submitting,
          on: { click: () => submit() }
        })
      );
    }
  }

  /* 词表到位后，本地兜底 schema 播下的值可能不在真选项里（如「悬疑」vs「悬疑/刑侦」）：
     <select> 会显示首项却提交旧值。渲染前先归一，保证所见即所提交。 */
  function normalizeValues() {
    const v = values();
    for (const group of ['brief', 'basic', 'style']) {
      for (const f of fieldsOf(model.medium, group)) {
        if (f.type === 'note' || f.type === 'preview' || f.type === 'scale') continue;
        const opts = optionsOf(f);
        if (!opts.length) continue;
        const known = (token) => opts.some((o) => sameToken(o.value, token));
        if (f.multi) {
          v[f.key] = (v[f.key] || []).filter(known);
          continue;
        }
        if (v[f.key] === '' || v[f.key] === undefined || known(v[f.key])) continue;
        const fallback = known(f.default) ? f.default : opts[0].value;
        v[f.key] = fallback;
        const hit = opts.find((o) => sameToken(o.value, fallback));
        if (hit && hit.key) v[`${f.key}_key`] = hit.key;
      }
    }
  }

  function render() {
    if (destroyed) return;
    normalizeValues();
    body.textContent = '';
    body.append(stepsBar(model.step));
    if (model.step === 1) body.append(stepOne());
    else if (model.step === 2) body.append(stepTwo());
    else body.append(stepThree());
    renderFooter();
  }

  ensureOnboarding().then(() => {
    if (destroyed) return;
    const v = values();
    if ('platform' in v && !platformOf(v.platform)) v.platform = '';
    if ('platform' in v && v.platform) applyPlatform(platformOf(v.platform));
    render();
  });

  function visibleFields() {
    const out = [];
    ['brief', 'basic', 'style'].forEach((name) => {
      for (const f of fieldsOf(model.medium, name)) if (isVisible(f, values())) out.push(f);
    });
    return out;
  }

  function validateStep() {
    const v = values();
    model.error = '';
    if (model.step === 1 && !model.medium) {
      model.error = '请选择一种体裁';
      return false;
    }
    const stepGroup = model.step === 1 ? 'brief' : model.step === 2 ? 'basic' : null;
    if (!stepGroup) return true;
    for (const f of fieldsOf(model.medium, stepGroup)) {
      if (!isVisible(f, v) || f.type === 'note' || f.type === 'preview') continue;
      const raw = v[f.key];
      if (f.required) {
        const empty = Array.isArray(raw) ? !raw.length : raw === '' || raw === null || raw === undefined;
        if (empty) {
          model.error = `请填写「${f.label}」`;
          return false;
        }
      }
      if (f.type === 'text' && String(raw || '').length > (f.max_length || 80)) {
        model.error = `「${f.label}」请控制在 ${f.max_length || 80} 字以内`;
        return false;
      }
      if (f.type === 'scale') {
        const n = Number(v[f.custom_key]);
        if (!Number.isInteger(n) || n < (f.min || 1) || n > (f.max || 2000)) {
          model.error = `「${f.label}」需在 ${f.min || 1}–${f.max || 2000} 之间`;
          return false;
        }
      }
      if (f.type === 'number' && raw !== '' && raw !== null && raw !== undefined) {
        const n = Number(raw);
        if (!Number.isInteger(n) || n < (f.min ?? 0) || n > (f.max ?? 100000)) {
          model.error = `「${f.label}」需在 ${f.min ?? 0}–${f.max ?? 100000} 之间`;
          return false;
        }
      }
    }
    /* 第 2 屏兜底：书名与起点至少要有一个，否则引擎无从规划。 */
    if (model.step === 2) {
      const hasTitle = fieldsOf(model.medium, 'basic').some((f) => f.key === 'title' && String(v[f.key] || '').trim());
      const hasIdea = fieldsOf(model.medium, 'basic').some((f) => f.key === 'idea' && String(v[f.key] || '').trim());
      if (!hasTitle && !hasIdea) {
        model.error = '至少填写标题或起点素材，系统才能开始规划';
        return false;
      }
    }
    return true;
  }

  function goNext() {
    if (!validateStep()) {
      render();
      return;
    }
    model.step = Math.min(3, model.step + 1);
    render();
  }

  function goBack() {
    model.error = '';
    model.step = Math.max(1, model.step - 1);
    render();
  }

  async function uploadStaged() {
    const ids = [];
    const failures = [];
    const total = model.refs.length;
    for (let i = 0; i < model.refs.length; i += 1) {
      const ref = model.refs[i];
      model.status = `正在上传参考资料 ${i + 1}/${total}…`;
      render();
      try {
        if (ref.kind === 'file' && ref.file) {
          const form = new FormData();
          form.append('file', ref.file, ref.file.name);
          form.append('filename', ref.file.name);
          form.append('title', ref.title.replace(/\.[^.]+$/, ''));
          form.append('usage_mode', ref.usage_mode);
          form.append('copyright_confirmed', 'true');
          const res = await api.uploadRef(form);
          const item = (res && (res.item || res)) || {};
          if (item.id) ids.push(item.id);
        } else if (ref.kind === 'text') {
          const res = await api.addRefText({
            title: ref.title,
            text: ref.text,
            usage_mode: ref.usage_mode,
            copyright_confirmed: true
          });
          const item = (res && (res.item || res)) || {};
          if (item.id) ids.push(item.id);
        } else if (ref.kind === 'url') {
          const res = await api.addRefUrl({
            url: ref.url,
            usage_mode: ref.usage_mode,
            copyright_confirmed: true
          });
          const item = (res && (res.item || res)) || {};
          if (item.id) ids.push(item.id);
        }
      } catch (err) {
        if (err && err.isMissing) {
          failures.push(`${ref.title}：当前后端未启用参考资料`);
        } else {
          failures.push(`${ref.title}：${errorText(err, '上传失败')}`);
        }
      }
    }
    return { ids, failures };
  }

  function buildPayload() {
    const v = values();
    const payload = { medium: model.medium, copyright_confirmed: true };
    for (const f of visibleFields()) {
      if (f.type === 'note' || f.type === 'preview') continue;
      const raw = v[f.key];
      if (raw === '' || raw === null || raw === undefined) continue;
      if (Array.isArray(raw) && !raw.length) continue;
      payload[f.key] = typeof raw === 'string' ? raw.trim() : raw;
      if (f.type === 'scale') payload[f.custom_key] = v[f.custom_key];
      if (f.key === 'genre' && v.genre_key) payload.genre_key = v.genre_key;
    }
    if (model.styleId) payload.style_id = model.styleId;
    const resolved = resolvedStyle();
    if (resolved) payload.style = resolved;
    const ui = store.get().ui;
    if (ui.providerId) payload.provider_id = ui.providerId;
    if (ui.model) payload.model = ui.model;
    return payload;
  }

  /* v3.1.0：创建后自动以用户消息形式把配置发给对话 Agent（消除"创建完毫无反应"断层） */
  function buildFirstMessage(payload, awaitingTitle) {
    const v = values();
    if (model.medium === 'novel') {
      const lines = [];
      if (awaitingTitle) {
        lines.push(`开新书：${payload.idea || '（详见参考设定）'}`);
        lines.push('请先生成几个书名候选给我挑选。');
      } else {
        lines.push(`开新书《${payload.title || v.title || ''}》，请确认设定后直接开始规划并创作。`);
      }
      const bits = [];
      if (payload.genre) bits.push(`题材：${payload.genre}`);
      if (payload.tone) bits.push(`基调：${payload.tone}`);
      if (payload.perspective) bits.push(`视角：${payload.perspective}`);
      if (payload.total_chapters) bits.push(`规模：${payload.total_chapters} 章`);
      if (payload.min_words_per_chapter) bits.push(`每章约 ${payload.min_words_per_chapter} 字`);
      if (payload.style && typeof payload.style === 'object') {
        bits.push('风格：' + Object.entries(payload.style).map(([k, val]) => `${k}=${val}`).join('、'));
      }
      if (bits.length) lines.push(bits.join('；') + '。');
      return lines.join('\n');
    }
    if (model.medium === 'script') {
      return `开新剧本：${payload.title || payload.idea || ''}\n形式：${payload.form || 'film'}；入口：${payload.entryPath || 'idea'}；题材：${payload.genre || ''}。请开始规划。`;
    }
    if (model.medium === 'poetry') {
      return `创作诗词：${payload.title || payload.idea || ''}\n体式：${payload.form || 'modern'}；改稿轮数：${payload.maxRounds || 3}。请开始。`;
    }
    if (model.medium === 'prose') {
      return `创作散文：${payload.title || payload.idea || ''}\n文体：${payload.genre || 'suibi'}；操作：${payload.operation || 'write'}${payload.target_words ? `；目标 ${payload.target_words} 字` : ''}。请开始。`;
    }
    return `开始创作：${payload.title || payload.idea || ''}`;
  }

  async function submit() {
    if (model.submitting) return;
    if (!model.copyright) {
      model.error = '请先确认参考资料版权，再创建作品';
      render();
      return;
    }
    model.error = '';
    model.submitting = true;
    model.status = '';
    render();

    const { ids, failures } = await uploadStaged();
    if (destroyed) return;

    const payload = buildPayload();
    payload.reference_ids = [...ids, ...model.existing];
    model.status = '正在创建作品并启动规划…';
    render();

    try {
      const res = await api.create(payload);
      const key = res && (res.key || res.project || res.pid);
      if (!key) throw new Error('创建接口没有返回作品 key，请刷新后重试');

      const med = res.medium || model.medium || 'novel';
      if (failures.length) {
        toast(`作品已创建，但有 ${failures.length} 份资料未入库`, { tone: 'danger', duration: 5000 });
      }
      /* 作品落盘到磁盘上还要一点时间，先把文体告诉路由，免得打开时被当成小说。 */
      if (ctx.rememberMedium) ctx.rememberMedium(key, med);
      /* 侧栏先出现占位项：后台规划要几次 LLM 调用，不能让人以为没创建成功。 */
      if (ctx.addPendingWork) ctx.addPendingWork({ key, name: res.name, medium: med });
      if (ctx.refreshLibrary) await ctx.refreshLibrary().catch(() => {});
      patchUi({ medium: model.medium, rightTab: 'work' });
      // v3.1.0：把所选配置合成首条用户消息，进入对话后由 session 自动发出
      patchUi({ pendingFirstMessage: { key, text: buildFirstMessage(payload, Boolean(res.awaiting_title)) } });
      modal.close();
      ctx.navigate(`/work/${encodeURIComponent(key)}`);
      if (med === 'novel') {
        if (!failures.length) toast('作品已创建，正在生成规划与书名', { tone: 'success' });
        if (res.awaiting_title) {
          toast('书名候选生成中，可在作品页继续处理', { duration: 3000 });
        }
      } else if (!failures.length) {
        toast('作品已创建，引擎正在后台逐阶段生成，工作台会自动刷新', { tone: 'success', duration: 4000 });
      }
    } catch (err) {
      if (destroyed) return;
      model.submitting = false;
      model.status = failures.length ? `部分资料未入库：${failures[0]}` : '';
      model.error = errorText(err, '创建失败，请检查配置后重试');
      render();
    }
  }

  render();
  setTimeout(() => {
    if (destroyed) return;
    const target = body.querySelector('.genre-card[aria-pressed="true"], input, button');
    if (target) target.focus();
  }, 30);
  return modal;
}
