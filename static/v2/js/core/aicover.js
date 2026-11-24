/* AI 生成封面（v5.7.0）：书架卡片 ⋯ 菜单的弹窗 + OpenAI 兼容 images 通道客户端。
   后端 bot/webcover.py：POST /api/library/cover/ai 生成即落盘（projects/<key>/cover.*），
   「采用」只是关弹窗 + 通知 library 刷新封面；「放弃」关闭不刷新；「重新生成」再发一次。

   状态机（reduceAiCover 纯函数，tests/v57_aicover_test.mjs 直测）：
     input    --generate--> generating --generated--> preview
     generating --failed--> input（错误文案回显）
     preview  --regenerate--> generating
     preview  --adopt--> closed（onApplied）；input/generating/preview --cancel--> closed
     input/generating/preview --setStyle/setPrompt--> 原状态更新（不迁移）

   mock 模式（?mock=1，IS_MOCK）不穿透：本地 canvas 画渐变占位当预览
   （jsdom/无 canvas 环境回退内联 SVG），「采用」只做本地预览不落盘——
   与书架既有 mockCovers 机制对齐；后端 mock=true 的内置占位 PNG 链路给 pytest 用。 */

import { h, icon } from './dom.js';
import { request, optional, errorText, IS_MOCK } from './api.js';
import { openModal, toast } from './ui.js';

/* 预设风格 chips（点击回填风格描述输入框；label 发后端 {{基调}}/{{风格}}）。 */
export const STYLE_PRESETS = [
  { label: '水墨', hint: '留白构图，晕染笔触，淡彩山水意象' },
  { label: '国风插画', hint: '工笔重彩，祥云纹样，古典人物场景' },
  { label: '都市光影', hint: '霓虹夜景，玻璃幕墙反光，都市剪影' },
  { label: '暗黑悬疑', hint: '低明度冷色调，浓雾与剪影，压迫式构图' },
  { label: '清新文艺', hint: '柔和日光，低饱和暖色，生活化静物' }
];

export const PROMPT_MAX = 180;

/* ---------------------------------------------------------------- 纯函数（直测） */

export function reduceAiCover(state, action) {
  const act = action || {};
  switch (act.type) {
    case 'setStyle':
      return { ...state, style: String(act.value || ''), error: '' };
    case 'setPrompt':
      return { ...state, prompt: String(act.value || '').slice(0, PROMPT_MAX), error: '' };
    case 'generate':
    case 'regenerate':
      return { ...state, phase: 'generating', error: '' };
    case 'generated':
      return { ...state, phase: 'preview', previewUrl: String(act.url || ''), mock: Boolean(act.mock) };
    case 'failed':
      return { ...state, phase: 'input', error: String(act.message || '生成失败，请稍后重试') };
    case 'adopt':
    case 'cancel':
      return { ...state, phase: 'closed' };
    default:
      return state;
  }
}

export function initialAiCoverState(extra = {}) {
  return { phase: 'input', style: '', prompt: '', error: '', previewUrl: '', mock: false, ...extra };
}

export function buildPayload(key, prompt, style) {
  return {
    key: String(key || ''),
    prompt: String(prompt || '').trim().slice(0, PROMPT_MAX),
    style: String(style || '').trim().slice(0, 60)
  };
}

/* mock 占位：canvas 渐变 → dataURL；无 canvas（jsdom）回退内联 SVG dataURL。
   颜色读 tokens 语义变量（读不到就是透明渐变），不写死色值。 */
export function localPlaceholderDataUrl(title) {
  const css = (name, fallback) => {
    try {
      const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch {
      return fallback;
    }
  };
  try {
    const canvas = document.createElement('canvas');
    canvas.width = 64;
    canvas.height = 96;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('no canvas');
    const grad = ctx.createLinearGradient(0, 0, 0, 96);
    grad.addColorStop(0, css('--accent-soft', 'transparent'));
    grad.addColorStop(1, css('--bg-chip', 'transparent'));
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, 64, 96);
    ctx.fillStyle = css('--ink-secondary', 'currentColor');
    ctx.font = '10px sans-serif';
    ctx.fillText('AI 封面', 8, 50);
    return canvas.toDataURL('image/png');
  } catch {
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="96"><defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="currentColor" stop-opacity="0.12"/><stop offset="1" stop-color="currentColor" stop-opacity="0.04"/></linearGradient></defs><rect width="64" height="96" fill="url(#g)"/><text x="32" y="50" text-anchor="middle" font-size="9" fill="currentColor" opacity="0.6">AI</text></svg>`;
    return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  }
}

/* ---------------------------------------------------------------- 弹窗（状态机挂载） */

/* v5.9.0：图像供应商并入供应商列表（kind=image 条目）——引导指向设置→模型与供应商，
   新建供应商时类型选「图像」；文案仍保留「图像模型（封面生成）」关键词供旧文档/检索。 */
const NO_CONFIG_HINT = '还没有配置图像模型：请先到「设置 → 模型与供应商」点「添加供应商」并选类型「图像」（图像模型（封面生成）），填写接口地址与图像模型名。';

export async function openAiCoverDialog({ item, onApplied } = {}) {
  const it = item || {};
  let state = initialAiCoverState();
  let configured = false;      // 后端已配置图像供应商（config 探测结果）
  let configKnown = IS_MOCK;   // 探测未回来前不闪「无配置」引导
  let busy = false;

  const promptInput = h('textarea', {
    class: 'input lib-aicv-prompt',
    rows: '3',
    maxlength: String(PROMPT_MAX),
    placeholder: '想画什么？（≤180 字）如：雨夜当铺门口，一盏暖灯照着湿漉漉的青石台阶…',
    'aria-label': '画面风格描述'
  });
  const chipsRow = h('div', { class: 'lib-aicv-chips', role: 'group', 'aria-label': '预设风格' });
  const previewBox = h('div', { class: 'lib-aicv-preview', 'aria-live': 'polite' });
  const guide = h('div', { class: 'lib-aicv-guide', role: 'note', hidden: true }, NO_CONFIG_HINT);
  const errorLine = h('div', { class: 'error-text', role: 'alert', text: '' });
  const genBtn = h('button', {
    class: 'btn btn-primary',
    type: 'button',
    text: '生成封面',
    disabled: true,
    on: { click: () => generate(false) }
  });
  const redoBtn = h('button', {
    class: 'btn',
    type: 'button',
    text: '重新生成',
    hidden: true,
    on: { click: () => generate(true) }
  });
  const adoptBtn = h('button', {
    class: 'btn btn-primary',
    type: 'button',
    text: '采用',
    hidden: true,
    disabled: true,
    on: { click: () => adopt() }
  });
  const cancelBtn = h('button', {
    class: 'btn',
    type: 'button',
    text: '放弃',
    on: { click: () => { dispatch({ type: 'cancel' }); } }
  });

  const modal = openModal({
    title: `AI 生成封面 ·《${String(it.name || '')}》`,
    subtitle: '生成即自动保存为本书封面；不满意可以重新生成或放弃。',
    body: h('div', { class: 'lib-aicv-body' },
      guide,
      h('div', { class: 'field' },
        h('span', { class: 'field-label', text: '风格描述' }),
        promptInput,
        chipsRow),
      previewBox,
      errorLine),
    footer: h('div', { class: 'lib-dialog-foot lib-aicv-foot' }, cancelBtn, redoBtn, adoptBtn, genBtn),
    size: 'sm',
    initialFocus: promptInput,
    onClose: () => { state = { ...state, phase: 'closed' }; }
  });

  function dispatch(action) {
    state = reduceAiCover(state, action);
    paint();
    if (state.phase === 'closed') modal.close();
    if (action.type === 'generated' || action.type === 'failed' || action.type === 'cancel') syncAfter();
  }

  function syncAfter() {
    /* generated 后把预览图 src 真正挂上（reducer 保持纯函数，副作用放这里）。 */
    if (state.phase === 'preview' && state.previewUrl) {
      const img = h('img', { class: 'lib-aicv-img', alt: '封面预览', src: state.previewUrl });
      previewBox.append(img);
    }
  }

  function paint() {
    /* chips 选中态 */
    for (const chip of chipsRow.children) {
      chip.classList.toggle('is-active', chip.dataset.label === state.style);
      chip.setAttribute('aria-pressed', chip.dataset.label === state.style ? 'true' : 'false');
    }
    /* 生成中转圈 */
    previewBox.textContent = '';
    if (state.phase === 'generating') {
      previewBox.append(h('div', { class: 'lib-aicv-loading' },
        h('span', { class: 'lib-aicv-spin', 'aria-hidden': 'true', html: icon('refresh', 16) }),
        h('span', { text: '正在生成封面…通常需要十几秒' })));
    }
    /* 错误/引导文案（mock 走本地占位链路，不需要图像供应商引导） */
    errorLine.textContent = state.error || '';
    guide.hidden = IS_MOCK || !(!configured && configKnown && state.phase === 'input');
    /* 按钮可见性：input→[生成]；generating→[生成中…]；preview→[重新生成][采用] */
    genBtn.hidden = state.phase !== 'input';
    genBtn.textContent = state.phase === 'input' ? '生成封面' : '生成中…';
    genBtn.disabled = busy || state.phase !== 'input' || (!configured && !IS_MOCK);
    promptInput.disabled = state.phase === 'generating';
    redoBtn.hidden = state.phase !== 'preview';
    redoBtn.disabled = busy;
    adoptBtn.hidden = state.phase !== 'preview';
    adoptBtn.disabled = busy || !state.previewUrl;
    cancelBtn.disabled = busy;
  }

  async function generate(isRedo) {
    if (busy) return;
    dispatch({ type: isRedo ? 'regenerate' : 'generate' });
    if (IS_MOCK) {
      /* mock：不穿透，本地占位 + 一小段生成等待感 */
      busy = true;
      paint();
      await new Promise((r) => setTimeout(r, 300));
      busy = false;
      dispatch({ type: 'generated', url: localPlaceholderDataUrl(String(it.name || '')), mock: true });
      return;
    }
    busy = true;
    paint();
    try {
      const res = await request('/api/library/cover/ai', {
        method: 'POST',
        body: buildPayload(it.key, state.prompt, state.style),
        timeout: 90000
      });
      busy = false;
      dispatch({ type: 'generated', url: (res && res.url) || '', mock: Boolean(res && res.mock) });
    } catch (err) {
      busy = false;
      dispatch({ type: 'failed', message: errorText(err, '生成失败，请稍后重试') });
    }
  }

  function adopt() {
    const url = state.previewUrl;
    dispatch({ type: 'adopt' });
    if (IS_MOCK) toast('演示模式：封面仅本地预览，不落盘', { duration: 2400 });
    else toast('封面已更新', { tone: 'success', duration: 1800 });
    if (typeof onApplied === 'function') onApplied(state.mock ? url : null);
  }

  /* chips：点击回填风格描述并标记选中 */
  for (const preset of STYLE_PRESETS) {
    const chip = h('button', {
      class: 'lib-aicv-chip',
      type: 'button',
      dataset: { label: preset.label },
      'aria-pressed': 'false',
      title: preset.hint,
      on: {
        click: () => {
          const next = state.style === preset.label ? '' : preset.label;
          dispatch({ type: 'setStyle', value: next });
        }
      }
    }, h('span', { text: preset.label }));
    chipsRow.append(chip);
  }
  promptInput.addEventListener('input', () => {
    state = reduceAiCover(state, { type: 'setPrompt', value: promptInput.value });
    errorLine.textContent = state.error || '';
  });

  paint();

  /* 无配置探测：设置页保存后重开弹窗即生效；后端未挂载（旧版本服务）按未配置引导。 */
  if (!IS_MOCK) {
    try {
      const cfg = await optional(request('/api/library/cover/ai/config'));
      configured = Boolean(cfg && cfg.configured);
    } catch {
      configured = false;
    }
    configKnown = true;
    paint();
  }
  return { el: modal.el, close: modal.close };
}
