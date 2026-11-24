/* v5.9.0 模型供应商文本/图像双区：node 直跑回归（jsdom 仿真，不开浏览器）。
   覆盖面：
     1) 真渲染 settings.js 供应商区：每条供应商带类型徽标（文本/图像，data-kind）；
     2) 双默认槽：全局默认模型（只含文本类候选）+ 图像默认模型（只含图像类候选，
        保存走 POST /api/library/cover/ai/config {provider_id, model}，webcover 兼容入口）；
     3) 行为：图像行「测出图」→ /api/library/cover/ai/test {provider_id}；
        「设为图像默认」→ /api/providers/{id}/default（后端按 kind 归槽）；
     4) 编辑弹窗：新建选类型（文本/图像 二选一），图像类型显示 图像模型名+出图尺寸、
        不拉 /models；编辑图像条目标题带（图像）、保存 payload 带 kind/models/size；
     5) v5.7.0 尾部独立「图像模型（封面生成）」小节已移除（imageProvBody / 旧 aria-label
        不复存在），契约字符串仍在（v57_aicover_test 钉死的那批）；
     6) aicover.js 无配置引导指向新入口（设置→模型与供应商，类型选「图像」）；
     7) 文本/图像分区与默认槽隔离：图像设默认不清文本最近选择，文本设默认清选择；
     8) settings.css v5.9.0 增量段零硬编码色（全部 tokens 变量）。
   fetch 全部桩死，不打后端。 */
import fs from 'fs';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';
import { JSDOM } from 'jsdom';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const failed = [];
function check(name, cond, extra = '') {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed.push(name);
}
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------------------------------------------------------------- jsdom 环境 */
const dom = new JSDOM(
  '<!doctype html><html><body><div id="host"></div><div id="overlays"></div><div id="toasts"></div></body></html>',
  { url: 'http://127.0.0.1:8848/', pretendToBeVisual: true }
);
global.window = dom.window;
global.document = dom.window.document;
global.Node = dom.window.Node;
global.Element = dom.window.Element;
global.HTMLElement = dom.window.HTMLElement;
global.localStorage = dom.window.localStorage;
global.sessionStorage = dom.window.sessionStorage;
global.location = dom.window.location;
global.getComputedStyle = dom.window.getComputedStyle;
global.AbortController = dom.window.AbortController;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true, writable: true });
if (!global.CSS || !global.CSS.escape) {
  global.CSS = { ...(global.CSS || {}), escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => '\\' + c) };
}

/* ---------------------------------------------------------------- fetch 桩 */
const PROVIDERS = {
  default_provider: 'prov-text-a',
  default_image_provider: 'prov-img-a',
  key_ok: true,
  presets: [],
  providers: [
    { id: 'prov-text-a', name: '硅基流动-文本', kind: 'text', active: true, preset: false,
      base_url: 'https://api.siliconflow.cn/v1', models: ['deepseek-chat'],
      default_model: 'deepseek-chat', has_key: true, key_display: '已设置（sk-…ab）' },
    { id: 'prov-text-b', name: 'DeepSeek', kind: 'text', active: true, preset: false,
      base_url: 'https://api.deepseek.com/v1', models: ['deepseek-chat'],
      default_model: 'deepseek-chat', has_key: false },
    { id: 'prov-img-a', name: '硅基流动-图像', kind: 'image', active: true, preset: false,
      base_url: 'https://api.siliconflow.cn/v1', models: ['Kwai-Kolors/Kolors'],
      default_model: 'Kwai-Kolors/Kolors', model: 'Kwai-Kolors/Kolors',
      size: '1024x1536', has_key: true, key_display: '已设置（sk-…cd）' },
    { id: 'prov-img-b', name: '本地出图', kind: 'image', active: false, preset: false,
      base_url: 'http://127.0.0.1:9/v1', models: ['img-b'], default_model: 'img-b',
      model: 'img-b', size: '768x1152', has_key: false }
  ]
};

const calls = [];
global.fetch = async (url, init) => {
  const bare = String(url).split('?')[0];
  const method = (init && init.method) || 'GET';
  let body = null;
  if (init && typeof init.body === 'string') {
    try { body = JSON.parse(init.body); } catch { body = init.body; }
  }
  calls.push({ url: bare, method, body });
  let payload = { ok: true };
  if (bare.endsWith('/api/providers')) payload = PROVIDERS;
  else if (bare.endsWith('/api/settings')) {
    payload = { ok: true, quality_mode: 'report', token_budget: 90000,
                score_provider_id: '', score_model: '' };
  } else if (bare.endsWith('/api/agents')) {
    payload = { ok: true, options: [], agents: [] };
  } else if (bare.endsWith('/api/pennames')) {
    payload = { ok: true, list: [] };
  } else if (bare.endsWith('/api/syslog')) {
    payload = { ok: true, items: [], total: 0 };
  } else if (bare.endsWith('/api/collab-review')) {
    payload = { ok: true, enabled: false, awaiting: 0, loaded: true };
  } else if (bare.endsWith('/api/library/cover/ai/config') && method === 'POST') {
    payload = { ok: true, configured: true, base_url: '', model: '', size: '1024x1536',
                has_key: true, key_display: '已设置' };
  } else if (bare.endsWith('/api/library/cover/ai/test')) {
    payload = { ok: true, detail: '已连通，返回 1 张测试图' };
  }
  return {
    ok: true, status: 200,
    headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: async () => payload,
    text: async () => JSON.stringify(payload)
  };
};
dom.window.fetch = global.fetch;

/* ---------------------------------------------------------------- 渲染设置页 */
const prefs = await import(pathToFileURL(path.join(ROOT, 'static/v2/js/core/prefs.js')).href);
const stateMod = await import(pathToFileURL(path.join(ROOT, 'static/v2/js/core/state.js')).href);
stateMod.store.set({ models: {
  default_provider: 'prov-text-a',
  options: PROVIDERS.providers.filter((p) => p.kind === 'text').map((p) => ({ ...p }))
} });
prefs.persistModelChoice('prov-text-b', 'deepseek-chat');
stateMod.patchUi({ providerId: 'prov-text-b', model: 'deepseek-chat' });

const settingsUrl = pathToFileURL(path.join(ROOT, 'static/v2/js/views/settings.js')).href;
const settings = await import(settingsUrl);
const { renderSettings } = settings;
const host = document.getElementById('host');
let refreshModelsCalls = 0;
const settingsView = renderSettings(host, { refreshModels: async () => { refreshModelsCalls++; } });
await sleep(80);

const q = (sel, root = document) => root.querySelector(sel);
const qa = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const pane = () => q('section[data-group="providers"]');
const bodyOf = (c) => (typeof c.body === 'string' ? c.body : JSON.stringify(c.body || {}));
const lastCall = (url, method) => [...calls].reverse()
  .find((c) => c.url === url && c.method === method);

/* ================================================================ 1. 类型徽标 */
{
  const rows = qa('.provider-row', pane());
  check('供应商区渲染 4 条条目（2 文本 + 2 图像）', rows.length === 4,
    `got ${rows.length}`);
  const kinds = rows.map((r) => r.dataset.kind);
  check('每条带 data-kind（text/image）且与数据一致',
    JSON.stringify(kinds) === JSON.stringify(['text', 'text', 'image', 'image']),
    JSON.stringify(kinds));
  const badges = qa('.provider-row .prov-kind', pane());
  check('类型徽标齐全：文本 ×2 / 图像 ×2',
    badges.filter((b) => b.textContent === '文本').length === 2
    && badges.filter((b) => b.textContent === '图像').length === 2);
  check('图像徽标 data-kind=image（样式钩子）',
    badges.filter((b) => b.textContent === '图像').every((b) => b.dataset.kind === 'image'));
  const parts = qa('.provider-part', pane());
  check('文本/图像供应商分为两个独立分区',
    parts.length === 2 && parts[0].dataset.kind === 'text' && parts[1].dataset.kind === 'image'
    && qa('.provider-row', parts[0]).length === 2 && qa('.provider-row', parts[1]).length === 2);
  const imgRow = rows.find((r) => r.dataset.id === 'prov-img-a');
  check('图像条目 meta 显示模型与出图尺寸',
    imgRow && imgRow.textContent.includes('Kwai-Kolors/Kolors')
    && imgRow.textContent.includes('1024x1536'));
  check('文本条目 meta 显示模型个数（现状不变）',
    rows.find((r) => r.dataset.id === 'prov-text-a').textContent.includes('1 个模型'));
}

/* ================================================================ 2. 双默认槽 */
{
  const globalSel = q('select[aria-label="全局默认模型"]', pane());
  check('全局默认模型槽存在（文本默认）', !!globalSel);
  check('全局默认候选只含文本类供应商（图像条目不进创作选择器）',
    qa('option', globalSel).every((o) => o.value.startsWith('prov-text-')),
    qa('option', globalSel).map((o) => o.value).join(','));
  const imgSel = q('select[aria-label="图像默认模型"]', pane());
  check('图像默认模型槽存在（新）', !!imgSel);
  check('图像默认候选只含 active 图像供应商',
    qa('option', imgSel).every((o) => o.value.startsWith('prov-img-a')),
    qa('option', imgSel).map((o) => o.value).join(','));
  check('图像默认当前值 = default_image_provider + 生效模型',
    imgSel.value === 'prov-img-a::Kwai-Kolors/Kolors', imgSel.value);
  check('图像默认槽文案指向「AI 生成封面」场景',
    pane().textContent.includes('AI 生成封面'));

  /* 保存图像默认 → webcover 兼容入口（显式 provider_id = 保存并设为图像默认） */
  const saveBtn = [...qa('button', pane())].find((b) => b.textContent === '保存'
    && b.closest('.set-row')?.querySelector('select[aria-label="图像默认模型"]'));
  saveBtn.click();
  await sleep(30);
  const c = lastCall('/api/library/cover/ai/config', 'POST');
  check('图像默认保存 → POST /api/library/cover/ai/config {provider_id, model}',
    !!c && c.body.provider_id === 'prov-img-a' && c.body.model === 'Kwai-Kolors/Kolors',
    c ? JSON.stringify(c.body) : 'no call');
  const imageSavedChoice = JSON.parse(localStorage.getItem('shenbi.v2.ui') || '{}');
  check('保存图像默认不刷新文本模型、不清文本 UI/localStorage 选择',
    refreshModelsCalls === 0
    && stateMod.store.get().ui.providerId === 'prov-text-b'
    && stateMod.store.get().ui.model === 'deepseek-chat'
    && imageSavedChoice.providerId === 'prov-text-b'
    && imageSavedChoice.model === 'deepseek-chat');
}

/* ================================================================ 3. 行操作（按类型分流） */
{
  const imgRow = qa('.provider-row', pane()).find((r) => r.dataset.id === 'prov-img-a');
  const testBtn = [...qa('button', imgRow)].find((b) => b.textContent === '测出图');
  testBtn.click();
  await sleep(30);
  const c = lastCall('/api/library/cover/ai/test', 'POST');
  check('图像条目「测出图」→ /ai/test 按条目探活 {provider_id}',
    !!c && c.body.provider_id === 'prov-img-a', c ? JSON.stringify(c.body) : 'no call');

  const bRow = qa('.provider-row', pane()).find((r) => r.dataset.id === 'prov-img-b');
  const dftBtn = [...qa('button', bRow)].find((b) => b.textContent === '设为图像默认');
  dftBtn.click();
  await sleep(30);
  const c2 = lastCall('/api/providers/prov-img-b/default', 'POST');
  check('图像条目设默认 → /api/providers/{id}/default（后端按 kind 归槽）', !!c2);
  const imageDefaultChoice = JSON.parse(localStorage.getItem('shenbi.v2.ui') || '{}');
  check('图像行设默认同样不污染文本最近选择',
    refreshModelsCalls === 0
    && stateMod.store.get().ui.providerId === 'prov-text-b'
    && imageDefaultChoice.providerId === 'prov-text-b'
    && imageDefaultChoice.model === 'deepseek-chat');

  const tRow = qa('.provider-row', pane()).find((r) => r.dataset.id === 'prov-text-b');
  check('文本条目仍是「测连接」+「设为默认」',
    [...qa('button', tRow)].some((b) => b.textContent === '测连接')
    && [...qa('button', tRow)].some((b) => b.textContent === '设为默认'));
  [...qa('button', tRow)].find((b) => b.textContent === '设为默认').click();
  await sleep(40);
  const textDefaultChoice = JSON.parse(localStorage.getItem('shenbi.v2.ui') || '{}');
  check('文本条目设默认刷新文本模型并清 UI/localStorage 最近选择',
    refreshModelsCalls === 1
    && !stateMod.store.get().ui.providerId && !stateMod.store.get().ui.model
    && !textDefaultChoice.providerId && !textDefaultChoice.model);

  prefs.persistModelChoice('prov-text-b', 'deepseek-chat');
  stateMod.patchUi({ providerId: 'prov-text-b', model: 'deepseek-chat' });
  const globalSel = q('select[aria-label="全局默认模型"]', pane());
  globalSel.value = 'prov-text-b::deepseek-chat';
  const globalSave = [...qa('button', pane())].find((b) => b.textContent === '保存'
    && b.closest('.set-row')?.querySelector('select[aria-label="全局默认模型"]'));
  globalSave.click();
  await sleep(40);
  const globalSavedChoice = JSON.parse(localStorage.getItem('shenbi.v2.ui') || '{}');
  check('保存全局文本默认同样清 UI/localStorage 最近选择',
    refreshModelsCalls === 2
    && !stateMod.store.get().ui.providerId && !stateMod.store.get().ui.model
    && !globalSavedChoice.providerId && !globalSavedChoice.model);
}

/* ================================================================ 4. 编辑弹窗：新建选类型 */
{
  calls.length = 0;
  const addBtn = [...qa('button', pane())].find((b) => b.textContent === '添加供应商'
    && b.closest('.set-actions'));
  addBtn.click();
  await sleep(20);
  const modal = q('.modal');
  check('添加弹窗打开', !!modal);
  const kindBtns = qa('.pe-kind-btn', modal);
  check('类型选择：文本模型 / 图像模型 二选一',
    kindBtns.length === 2 && kindBtns[0].textContent === '文本模型'
    && kindBtns[1].textContent === '图像模型');
  check('默认选中文本类型', kindBtns[0].classList.contains('is-active')
    && !kindBtns[1].classList.contains('is-active'));
  check('文本类型：模型选择区可见、图像字段隐藏',
    !!q('.mp-picker, .pe-fetch-row', modal)
    && q('input[aria-label="图像模型名"]', modal).closest('.pe-section').hidden === true);

  kindBtns[1].click();
  await sleep(10);
  check('切到图像类型：图像字段可见、模型拉取区隐藏',
    q('input[aria-label="图像模型名"]', modal).closest('.pe-section').hidden === false
    && [...qa('.pe-section', modal)].find((s) => s.querySelector('.pe-fetch-row')).hidden === true);

  q('input[aria-label="名称"]', modal).value = '硅基流动-图像';
  q('input[aria-label="接口地址"]', modal).value = 'https://api.siliconflow.cn/v1';
  q('input[aria-label="API 密钥"]', modal).value = 'sk-new-1234567890';
  q('input[aria-label="图像模型名"]', modal).value = 'Kwai-Kolors/Kolors';
  q('input[aria-label="出图尺寸"]', modal).value = '832x1248';
  /* 页脚提交按钮（「手动添加模型」行也有一个「添加」，取最后一个） */
  [...qa('button', modal)].filter((b) => b.textContent === '添加').pop().click();
  await sleep(30);
  const c = lastCall('/api/providers', 'POST');
  check('图像条目保存 → POST /api/providers（kind/image + models + size）',
    !!c && c.body.kind === 'image' && Array.isArray(c.body.models)
    && c.body.models[0] === 'Kwai-Kolors/Kolors' && c.body.size === '832x1248',
    c ? JSON.stringify(c.body) : 'no call');
  check('图像条目不触发 /models 拉取',
    !calls.some((x) => x.url.endsWith('/api/providers/fetch-models')));
  check('弹窗已关', !q('.modal'));
}

/* ================================================================ 5. 编辑既有条目 */
{
  const imgRow = qa('.provider-row', pane()).find((r) => r.dataset.id === 'prov-img-a');
  [...qa('button', imgRow)].find((b) => b.getAttribute('aria-label') === '编辑供应商').click();
  await sleep(220);   // 文本条目会自动拉模型；图像条目不应有 fetch-models
  const modal = q('.modal');
  check('编辑图像条目：标题带（图像）', q('.modal-title').textContent.includes('（图像）'));
  check('编辑态类型固定不可切换',
    qa('.pe-kind-btn', modal).every((b) => b.disabled === true));
  check('编辑图像条目：图像字段就位并回填',
    q('input[aria-label="图像模型名"]', modal).value === 'Kwai-Kolors/Kolors'
    && q('input[aria-label="出图尺寸"]', modal).value === '1024x1536');
  check('编辑图像条目不自动拉 /models',
    !calls.some((x) => x.url.endsWith('/api/providers/fetch-models')));
  const keyIn = q('input[aria-label="API 密钥"]', modal);
  check('图像条目密钥框「留空 = 保留已存值」口径', keyIn.placeholder.includes('留空则不修改'));
  [...qa('button', modal)].find((b) => b.textContent === '保存').click();
  await sleep(30);
  const c = lastCall('/api/providers/prov-img-a', 'PUT');
  check('图像条目保存 → PUT /api/providers/{id}（models/size 回传）',
    !!c && c.body.kind === 'image' && c.body.size === '1024x1536',
    c ? JSON.stringify(c.body) : 'no call');
  check('弹窗已关', !q('.modal'));

  /* 文本条目编辑：仍自动拉模型（现状不变） */
  calls.length = 0;
  const tRow = qa('.provider-row', pane()).find((r) => r.dataset.id === 'prov-text-a');
  [...qa('button', tRow)].find((b) => b.getAttribute('aria-label') === '编辑供应商').click();
  await sleep(220);
  check('编辑文本条目仍自动拉取 /models（现状不变）',
    calls.some((x) => x.url.endsWith('/api/providers/fetch-models')));
  [...qa('button', q('.modal'))].find((b) => b.textContent === '取消').click();
  await sleep(10);
}

/* ================================================================ 6. 默认槽失效时与后端解析口径一致 */
{
  const oldImageDefault = PROVIDERS.default_image_provider;
  const oldTextModel = PROVIDERS.providers[0].default_model;
  PROVIDERS.default_image_provider = 'prov-image-deleted';
  PROVIDERS.providers[0].default_model = 'deleted-text-model';
  const fallbackHost = document.createElement('div');
  fallbackHost.id = 'fallback-host';
  document.body.append(fallbackHost);
  const fallbackView = renderSettings(fallbackHost, { refreshModels: async () => {} });
  await sleep(80);
  const fallbackPane = q('section[data-group="providers"]', fallbackHost);
  check('图像默认槽失效时显示第一条 active 图像供应商（与 image_resolve 一致）',
    q('select[aria-label="图像默认模型"]', fallbackPane).value === 'prov-img-a::Kwai-Kolors/Kolors');
  check('图像默认徽标跟随有效回落项，不标记失效 ID',
    qa('.provider-row[data-kind="image"]', fallbackPane)
      .filter((r) => r.querySelector('.prov-name .badge-accent')?.textContent === '默认').length === 1
      && q('.provider-row[data-id="prov-img-a"]', fallbackPane).textContent.includes('默认'));
  check('文本 default_model 失效时下拉框取首个有效模型',
    q('select[aria-label="全局默认模型"]', fallbackPane).value === 'prov-text-a::deepseek-chat');
  fallbackView.destroy();
  fallbackHost.remove();
  PROVIDERS.default_image_provider = oldImageDefault;
  PROVIDERS.providers[0].default_model = oldTextModel;
}

/* ================================================================ 7. 旧小节移除 + 契约钉死（源码级） */
{
  const setSrc = read('static/v2/js/views/settings.js');
  check('v5.7.0 尾部独立图像小节已移除（imageProvBody 不复存在）',
    !setSrc.includes('imageProvBody'));
  check('旧小节专属字段（图像接口地址/图像接口密钥 aria-label）不复存在',
    !setSrc.includes('aria-label\': \'图像接口地址') && !setSrc.includes('图像接口密钥'));
  check('契约字符串仍在（v57_aicover_test 钉死）：图像模型（封面生成）/ config / test',
    setSrc.includes('图像模型（封面生成）') && setSrc.includes('/api/library/cover/ai/config')
    && setSrc.includes('/api/library/cover/ai/test'));
  check('双默认槽与 kind 体系落位（default_image_provider / kind 图标 / 类型按钮）',
    setSrc.includes('default_image_provider') && setSrc.includes("dataset: { kind: isImage ? 'image' : 'text' }")
    && setSrc.includes('pe-kind-btn'));

  const aicvSrc = read('static/v2/js/core/aicover.js');
  check('aicover.js 无配置引导指向新入口（设置→模型与供应商，类型选「图像」）',
    aicvSrc.includes('模型与供应商') && aicvSrc.includes('类型「图像」')
    && aicvSrc.includes('图像模型（封面生成）'));

  const cssSrc = read('static/v2/css/settings.css');
  const seg = cssSrc.slice(cssSrc.indexOf('v5.9.0 模型供应商文本/图像双区'));
  const colorLeak = seg.match(/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsl\(/g) || [];
  check('settings.css v5.9 增量段零硬编码色（全部 tokens）',
    colorLeak.length === 0 && seg.includes('var(--accent-soft)') && seg.includes('var(--bg-chip)'),
    colorLeak.join(','));
}

/* ---------------------------------------------------------------- 收尾 */
if (failed.length) {
  console.error(`\nv59_providers_test: ${failed.length} 项失败`);
  process.exit(1);
}
console.log('\nv59_providers_test: 全部通过');
process.exit(0);
