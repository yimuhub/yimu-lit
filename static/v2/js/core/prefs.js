/* 本机界面偏好（不落后端配置）：localStorage 读写 + store 同步。 */

import { store, patchUi } from './state.js';

const KEY = 'shenbi.v2.ui';
const DEFAULTS = { showClassic: false, sidebarWidth: 280, rightWidth: 0, sidebarExpanded: true };

function read() {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || '{}');
    return raw && typeof raw === 'object' ? raw : {};
  } catch {
    return {};
  }
}

function num(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

/* /api/model_options 已由后端过滤为 active 文本条目；兼容旧响应/测试桩里省略
   kind/active 的情况，但显式 image 或 inactive 一律不得成为创作模型选择。 */
export function isSelectableTextProvider(provider) {
  return Boolean(provider && provider.id
    && (!provider.kind || provider.kind === 'text')
    && provider.active !== false);
}

export function selectableTextProviders(providers = []) {
  return (Array.isArray(providers) ? providers : []).filter(isSelectableTextProvider);
}

export function providerDefaultModel(provider) {
  const models = (provider && Array.isArray(provider.models) ? provider.models : []).map(String);
  const preferred = String((provider && provider.default_model) || '');
  return models.includes(preferred) ? preferred : (models[0] || '');
}

/* 全局文本默认：只认 default_provider 对应的 active 文本条目。模型选项为空时
   保留空值，绝不拿图像默认或最近选择冒充全局默认。 */
export function globalTextModel(state = store.get()) {
  const models = state.models || {};
  const options = selectableTextProviders(models.options);
  const provider = options.find((p) => p.id === models.default_provider) || null;
  return {
    providerId: provider ? provider.id : '',
    model: provider ? providerDefaultModel(provider) : ''
  };
}

export function isValidModelChoice(state, providerId, model) {
  const pid = String(providerId || '').trim();
  const mid = String(model || '').trim();
  if (!pid || !mid) return false;
  const options = selectableTextProviders(state.models && state.models.options);
  const provider = options.find((p) => p.id === pid);
  const models = provider && Array.isArray(provider.models) ? provider.models : [];
  return Boolean(provider && models.map(String).includes(mid));
}

/* 创建/会话载荷的统一口径：最近选择有效则沿用；否则完整回落到全局文本默认。
   返回的 providerId/model 要么同时有值，要么同时为空，避免半对选择落进请求。 */
export function resolveModelChoice(state = store.get()) {
  if (isValidModelChoice(state, state.ui && state.ui.providerId, state.ui && state.ui.model)) {
    return {
      providerId: String(state.ui.providerId).trim(),
      model: String(state.ui.model).trim()
    };
  }
  return globalTextModel(state);
}

function write(state) {
  try {
    const options = store.get().models && store.get().models.options;
    const choiceKnown = Array.isArray(options) && options.length > 0;
    const validChoice = !choiceKnown || isValidModelChoice(store.get(), state.providerId, state.model);
    localStorage.setItem(
      KEY,
      JSON.stringify({
        showClassic: !!state.showClassic,
        sidebarWidth: num(state.sidebarWidth, DEFAULTS.sidebarWidth),
        rightWidth: num(state.rightWidth, DEFAULTS.rightWidth),
        sidebarExpanded: state.sidebarExpanded !== false,
        /* v5.9.0：模型选择持久化（顶栏最近一次选择的供应商/模型）——
           刷新后顶栏芯片与新建作品的创建载荷仍沿用这次选择（死规律的选择持久）。 */
        providerId: validChoice ? (state.providerId || '') : '',
        model: validChoice ? (state.model || '') : ''
      })
    );
  } catch {
    /* 隐私模式或存储不可用：偏好只活在本次会话 */
  }
}

export function clearModelChoice() {
  write({ ...read(), providerId: '', model: '' });
  patchUi({ providerId: null, model: null });
}

/* 启动时调用一次，把本机偏好灌进 store。模型选项尚未回来时先恢复原值，
   下方的 store 订阅在 /api/model_options 到位后立即校验并清除失效选择。 */
export function loadPrefs() {
  const saved = read();
  patchUi({
    showClassic: saved.showClassic === true,
    sidebarWidth: num(saved.sidebarWidth, DEFAULTS.sidebarWidth),
    rightWidth: num(saved.rightWidth, DEFAULTS.rightWidth),
    sidebarExpanded: saved.sidebarExpanded !== false,
    providerId: saved.providerId || null,
    model: saved.model || null
  });
  /* 若模型批次已经到过（例如测试/热重载再次 loadPrefs），立即校验；首次
     boot 时 options 还是初始空数组，不能区分「尚未加载」与「确实无可用供应商」，
     必须等下方订阅收到新 models 批次后再判断。 */
  if (modelBatchSeen) reconcileModelChoice(store.get());
}

/* v5.9.0：把顶栏最近一次模型选择落 localStorage（供应商/模型对）。
   传空 = 清除本机选择；模型选项已加载时，非法/图像/停用选择拒绝落盘。 */
export function persistModelChoice(providerId, model) {
  const pid = String(providerId || '').trim();
  const mid = String(model || '').trim();
  if (!pid || !mid) {
    write({ ...read(), providerId: '', model: '' });
    return false;
  }
  const options = store.get().models && store.get().models.options;
  // 显式保存选择必须当下可验证；空选项集同样不能落盘。只有 loadPrefs
  // 恢复旧值时才允许等待异步 model_options 回来再校验。
  if (!Array.isArray(options) || !isValidModelChoice(store.get(), pid, mid)) {
    write({ ...read(), providerId: '', model: '' });
    return false;
  }
  write({ ...read(), providerId: pid, model: mid });
  return true;
}

let lastDefaultSig = '';

/* 模型选项是启动后异步到达的，因此校验不能只写在 loadPrefs 里。这里同时处理：
   ① 失效持久选择（供应商删除/停用/变图像/模型下架）；
   ② 全局文本默认变化（清旧选择）；图像默认变化不参与签名，绝不会误清。 */
export function reconcileModelChoice(state = store.get()) {
  const rawOptions = state.models && state.models.options;
  if (!Array.isArray(rawOptions)) return false;
  const options = selectableTextProviders(rawOptions);

  const global = globalTextModel(state);
  const defaultSig = `${global.providerId}\u0000${global.model}`;
  const changed = Boolean(lastDefaultSig) && defaultSig !== lastDefaultSig;
  lastDefaultSig = defaultSig;
  if (changed) {
    clearModelChoice();
    return true;
  }

  const saved = read();
  const savedPid = String(saved.providerId || '').trim();
  const savedModel = String(saved.model || '').trim();
  const uiPid = String(state.ui && state.ui.providerId || '').trim();
  const uiModel = String(state.ui && state.ui.model || '').trim();
  // 已加载但没有可选文本供应商：任何旧选择都必然失效，立即清理。
  if (!options.length) {
    if (savedPid || savedModel || uiPid || uiModel) clearModelChoice();
    return false;
  }
  const savedValid = isValidModelChoice(state, savedPid, savedModel);
  if (!savedValid && (savedPid || savedModel)) {
    write({ ...saved, providerId: '', model: '' });
  }

  if ((uiPid || uiModel) && !isValidModelChoice(state, uiPid, uiModel)) {
    patchUi({ providerId: null, model: null });
  }
  return savedValid;
}

/* 分栏宽度与折叠态整体落盘；拖拽只在 pointerup 调一次，避免每帧写 localStorage。 */
export function persistLayout(ui) {
  write({ ...read(), ...ui });
}

export function setPref(name, value) {
  const next = { ...DEFAULTS, ...read(), [name]: !!value };
  write(next);
  patchUi({ [name]: next[name] });
}

/* app.js 先 loadPrefs、后拉 model_options；在模块装载时订阅即可覆盖这个时间差。
   书级绑定从 true → false（/api/book-model 解绑成功后的 status 收敛）也在这里
   清掉本机选择，失败时 work.model_bound 不变，因此不会误清。 */
// 初始 models 对象不是一次服务端批次；loadPrefs 的同步 patchUi 不应提前清掉旧值。
let lastModels = store.get().models;
let modelBatchSeen = false;
const boundByKey = new Map();
store.subscribe((state) => {
  if (state.models !== lastModels) {
    lastModels = state.models;
    modelBatchSeen = true;
    reconcileModelChoice(state);
  }
  const key = state.currentKey || '';
  const work = key ? state.work : null;
  if (!key || !work) return;
  const bound = Boolean(work.model_bound);
  const wasBound = boundByKey.get(key);
  boundByKey.set(key, bound);
  if (wasBound && !bound) clearModelChoice();
});
