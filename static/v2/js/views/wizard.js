/* 新建作品向导：体裁与模式 → 基本信息 → 参考资料与风格。三步内可返回，创建中有 loading 与可读错误。 */

import {
  h,
  icon,
  validateFile,
  ATTACH_ACCEPT
} from '../core/dom.js';
import { store, patchUi } from '../core/state.js';
import { openModal, toast } from '../core/ui.js';
import { GENRE_OPTIONS, SCALE_OPTIONS, WRITING_MODES, USAGE_MODES, MEDIUM_FALLBACK, scaleLabel } from '../core/constants.js';
import { api, errorText } from '../core/api.js';

const DEFAULT_CHAPTERS = { short: 100, '1m': 300, '3m': 900, '5m': 1500 };

function mediumList() {
  const loaded = store.get().mediums || [];
  return loaded.length ? loaded : MEDIUM_FALLBACK;
}

function mediumOf(key) {
  return mediumList().find((m) => m.key === key) || mediumList()[0] || MEDIUM_FALLBACK[0];
}

function defaultGenre(mediumKey) {
  if (mediumKey === 'script') return '剧本';
  if (mediumKey === 'poetry') return '诗词';
  if (mediumKey === 'prose') return '散文';
  return '悬疑';
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

export function openWizard(ctx, preset = {}) {
  const state = store.get();
  const initialMedium = preset.medium || state.ui.medium || 'novel';
  const model = {
    step: 1,
    medium: initialMedium,
    genre: preset.genre || defaultGenre(initialMedium),
    title: preset.title || '',
    idea: preset.idea || '',
    scale: preset.scale || '1m',
    chapters: Number(preset.chapters) || DEFAULT_CHAPTERS[preset.scale || '1m'] || 300,
    mode: preset.mode || 'rolling',
    styleId: preset.styleId || '',
    refs: [],
    existing: new Set(),
    copyright: true,
    submitting: false,
    error: '',
    status: ''
  };

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

  function errorBanner() {
    if (!model.error) return null;
    const box = h('div', { class: 'wiz-error', role: 'alert' });
    box.append(h('span', { html: icon('alert', 14) }), h('div', { text: model.error }));
    return box;
  }

  function statusLine() {
    if (!model.status) return null;
    return h('div', { class: 'hint-text', style: { marginTop: '6px' }, text: model.status });
  }

  /* ---------------------------------------------------------- step 1 */
  function stepOne() {
    const pane = h('div', { class: 'wiz-pane' });
    const title = h(
      'div',
      { class: 'wiz-pane-title' },
      h('h3', { text: '这次想写什么？' }),
      h('p', { text: '体裁决定规划深度与交付形态；创作模式决定先规划还是边写边看。' })
    );
    pane.append(title);

    const cards = h('div', { class: 'genre-cards' });
    for (const medium of mediumList().slice(0, 4)) {
      const active = model.medium === medium.key;
      const card = h(
        'button',
        {
          class: 'genre-card',
          type: 'button',
          'aria-pressed': active ? 'true' : 'false',
          dataset: { medium: medium.key }
        },
        h('span', { class: 'gc-icon', html: icon(medium.icon || 'book', 17) }),
        h('span', { class: 'gc-name', text: medium.label }),
        h('span', { class: 'gc-desc', text: medium.desc || '' }),
        h('span', { class: 'gc-check', html: icon('check', 12) })
      );
      card.addEventListener('click', () => {
        model.medium = medium.key;
        model.genre = defaultGenre(medium.key);
        render();
      });
      cards.append(card);
    }
    pane.append(field('体裁', cards));

    const genreSelect = h('select', { class: 'select', 'aria-label': '题材' });
    for (const g of GENRE_OPTIONS) {
      genreSelect.append(h('option', { value: g, text: g, selected: g === model.genre }));
    }
    genreSelect.addEventListener('change', () => {
      model.genre = genreSelect.value;
    });
    pane.append(field('题材', genreSelect, { hint: '题材会作为规划与文风的第一层约束，之后仍可微调。' }));

    const modes = h('div', { class: 'mode-cards' });
    for (const mode of WRITING_MODES) {
      const btn = h(
        'button',
        {
          class: 'mode-card',
          type: 'button',
          'aria-pressed': model.mode === mode.value ? 'true' : 'false'
        },
        h('span', { class: 'mc-name' }, h('span', { html: icon(mode.icon, 13) }), h('span', { text: mode.label })),
        h('span', { class: 'mc-desc', text: mode.desc })
      );
      btn.addEventListener('click', () => {
        model.mode = mode.value;
        render();
      });
      modes.append(btn);
    }
    pane.append(field('创作模式', modes));
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
        h('h3', { text: '给它一个起点' }),
        h('p', { text: '一句话说清主角、困境或核心场面即可。书名可以留空，由系统生成候选。' })
      )
    );

    const grid = h('div', { class: 'wiz-grid' });
    const titleInput = h('input', {
      class: 'input',
      maxlength: '40',
      placeholder: '例如：长夜灯河',
      value: model.title,
      'aria-label': '书名'
    });
    titleInput.addEventListener('input', () => {
      model.title = titleInput.value;
    });
    grid.append(field('书名', titleInput, { optionalText: '可选' }));

    const idea = h('textarea', {
      class: 'textarea',
      rows: 4,
      maxlength: '2000',
      placeholder: '例如：一名被停职的港城刑警，在追查守灯人失踪案时发现一份跨越二十年的失踪者名单。',
      'aria-label': '一句话 premise'
    });
    idea.value = model.idea;
    idea.addEventListener('input', () => {
      model.idea = idea.value;
    });
    const ideaWrap = field('一句话 premise', idea, { hint: '写清「谁 / 想要什么 / 被什么挡住」最容易得到可用的规划。' });
    ideaWrap.classList.add('span-2');
    grid.append(ideaWrap);

    const scaleRow = h('div', { class: 'scale-row' });
    const presets = h('div', { class: 'scale-presets' });
    for (const option of SCALE_OPTIONS) {
      const btn = h('button', {
        class: 'scale-opt',
        type: 'button',
        'aria-pressed': model.scale === option.value ? 'true' : 'false'
      });
      btn.append(h('span', { text: option.label }), h('span', { class: 'muted', text: option.desc }));
      btn.addEventListener('click', () => {
        model.scale = option.value;
        model.chapters = DEFAULT_CHAPTERS[option.value] || model.chapters;
        render();
      });
      presets.append(btn);
    }
    const chapters = h('input', {
      class: 'input scale-input',
      type: 'number',
      min: '1',
      max: '2000',
      value: String(model.chapters),
      'aria-label': '章节数'
    });
    chapters.addEventListener('input', () => {
      model.chapters = Number(chapters.value) || 0;
    });
    scaleRow.append(presets, h('div', { style: { display: 'flex', alignItems: 'center', gap: '6px' } }, chapters, h('span', { class: 'scale-unit', text: '章' })));
    const scaleWrap = field('规模与章节数', scaleRow, { hint: '章节数会作为规划目标，写作过程中仍可扩展。' });
    scaleWrap.classList.add('span-2');
    grid.append(scaleWrap);
    pane.append(grid);
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
      h('span', { class: 'rsh-title', html: `${icon('palette', 14)}<span>文风</span>` }),
      h('span', { class: 'badge', text: '可选' })
    );
    wrap.append(head);
    const styles = store.get().styles || [];
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
      wrap.append(h('div', { class: 'hint-text', text: '还没有蒸馏风格。可以在设置或作品里先生成风格，本次先自动选择。' }));
    }
    return wrap;
  }

  function summaryBox() {
    const box = h('div', { class: 'wiz-summary' });
    const rows = [
      ['体裁', `${mediumOf(model.medium).label} · ${model.genre}`],
      ['书名', model.title.trim() || '留空，创建后生成候选'],
      ['规模', `${scaleLabel(model.scale) || model.scale} · 约 ${model.chapters} 章`],
      ['创作模式', (WRITING_MODES.find((m) => m.value === model.mode) || {}).label || model.mode],
      ['文风', model.styleId || '自动选择'],
      ['参考', `${model.refs.length + model.existing.size} 份`]
    ];
    for (const [k, v] of rows) {
      box.append(h('div', {}, h('span', { text: k }), h('b', { text: v })));
    }
    if (model.idea.trim()) box.append(h('p', { text: model.idea.trim() }));
    return box;
  }

  function stepThree() {
    const pane = h('div', { class: 'wiz-pane' });
    pane.append(
      h(
        'div',
        { class: 'wiz-pane-title' },
        h('h3', { text: '把参考与文风交给它' }),
        h('p', { text: '创建前先放好资料和风格，第一版规划会更贴近你想要的东西。' })
      )
    );
    pane.append(refsPane(), stylePane(), summaryBox(), errorBanner(), statusLine());
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
      footer.append(
        h('button', {
          class: 'btn btn-primary',
          type: 'button',
          html: model.submitting
            ? '<span class="wiz-loading"><span class="spinner"></span>创建中…</span>'
            : `<span>${model.title.trim() ? '创建并开始' : '创建作品'}</span>${icon('arrow-right', 14)}`,
          disabled: model.submitting,
          on: { click: () => submit() }
        })
      );
    }
  }

  function render() {
    if (destroyed) return;
    body.textContent = '';
    body.append(stepsBar(model.step));
    if (model.step === 1) body.append(stepOne());
    else if (model.step === 2) body.append(stepTwo());
    else body.append(stepThree());
    renderFooter();
  }

  function validateStep() {
    if (model.step === 1) {
      if (!model.medium) {
        model.error = '请选择一种体裁';
        return false;
      }
      if (!model.genre) {
        model.error = '请选择题材';
        return false;
      }
    }
    if (model.step === 2) {
      if (!model.title.trim() && !model.idea.trim()) {
        model.error = '至少填写书名或一句话 premise，系统才能开始规划';
        return false;
      }
      if (model.title.trim().length > 40) {
        model.error = '书名请控制在 40 字以内';
        return false;
      }
      if (!Number.isInteger(model.chapters) || model.chapters < 1 || model.chapters > 2000) {
        model.error = '章节数需在 1–2000 之间';
        return false;
      }
    }
    model.error = '';
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

    const referenceIds = [...ids, ...model.existing];
    model.status = '正在创建作品并启动规划…';
    render();

    try {
      const payload = {
        title: model.title.trim(),
        idea: model.idea.trim(),
        genre: model.genre,
        medium: model.medium,
        total_chapters: model.chapters,
        scale: model.scale,
        writing_mode: model.mode,
        mode: model.mode,
        style_id: model.styleId || '',
        reference_ids: referenceIds,
        copyright_confirmed: true
      };
      const ui = store.get().ui;
      if (ui.providerId) payload.provider_id = ui.providerId;
      if (ui.model) payload.model = ui.model;

      const res = await api.create(payload);
      const key = res && (res.key || res.project || res.pid);
      if (!key) throw new Error('创建接口没有返回作品 key，请刷新后重试');

      if (failures.length) {
        toast(`作品已创建，但有 ${failures.length} 份资料未入库`, { tone: 'danger', duration: 5000 });
      } else {
        toast('作品已创建，正在生成规划与书名', { tone: 'success' });
      }
      if (ctx.refreshLibrary) await ctx.refreshLibrary().catch(() => {});
      patchUi({ medium: model.medium, rightTab: 'work' });
      modal.close();
      ctx.navigate(`/work/${encodeURIComponent(key)}`);
      if (res.awaiting_title) {
        toast('书名候选生成中，可在作品页继续处理', { duration: 3000 });
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
