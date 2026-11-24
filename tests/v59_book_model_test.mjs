// v5.9.0 每本书绑定创作大模型（死规律）前端回归（node 直跑，jsdom）。
// 覆盖：
//   1) prefs.js：模型选择持久化（persistModelChoice → localStorage shenbi.v2.ui）与刷新恢复（loadPrefs → ui）；
//   2) app.js 真启动 E2E：顶栏芯片在「已绑书」时显示书级绑定值 + 「本书绑定」标记（work.model 优先于 ui.model）；
//   3) setModel 分流（死规律）：有打开的书 → POST /api/book-model（写书级绑定），成功 toast「本书已绑定…创作全程使用」
//      并持久化选择；失败（400 中文报错）→ toast danger 且选中态与 localStorage 不变；
//   4) 无书（welcome/全局态）→ POST /api/global-model（全局默认），toast「全局默认模型已设为」；
//   5) 顶栏菜单：绑定行带「本书绑定」标记并打勾、菜单标题切换、「解除本书绑定（回落全局默认）」项 → POST 空 provider_id；
//   6) 严格绑定失效：model_bound=true + model_unavailable=true → 顶栏明确报错，不伪装全局默认；
//   7) prefs 校验/默认槽隔离 + 会话载荷与 REST 回退契约（session.js）。
import fs from 'fs';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';
import { JSDOM } from 'jsdom';

const WATCHDOG = setTimeout(() => {
  console.log('❌ 用例超时未结束');
  process.exit(1);
}, 90000);
WATCHDOG.unref();

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const V2 = path.join(root, 'static', 'v2');
const read = (p) => fs.readFileSync(path.join(root, p), 'utf8');

let failed = 0;
function check(name, cond, extra = '') {
  console.log(`${cond ? '✅' : '❌'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed++;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 8000, step = 40) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    try { if (fn()) return true; } catch { /* 未就绪继续等 */ }
    await sleep(step);
  }
  return false;
}

/* ---------------------------------------------------------------- jsdom 环境 */
const dom = new JSDOM(
  '<!doctype html><html><body>'
  + '<div id="app" data-sidebar="expanded" data-right="closed" data-drawer="closed" data-dragging="false">'
  + '<div id="sidebar"></div><div id="topbar"></div><div id="view"></div><div id="rightbar"></div>'
  + '<div id="handleLeft"></div><div id="handleRight"></div>'
  + '<div id="rightBackdrop" hidden></div><div id="drawerBackdrop" hidden></div>'
  + '</div>'
  + '<div id="overlays"></div><div id="toasts"></div>'
  + '</body></html>',
  { url: 'http://127.0.0.1:8848/', pretendToBeVisual: true }
);
global.window = dom.window;
global.document = dom.window.document;
global.Node = dom.window.Node;
global.Element = dom.window.Element;
global.HTMLElement = dom.window.HTMLElement;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true, writable: true });
Object.defineProperty(global, 'location', { value: dom.window.location, configurable: true, writable: true });
global.getComputedStyle = dom.window.getComputedStyle;
global.CustomEvent = dom.window.CustomEvent;
global.Event = dom.window.Event;
global.MouseEvent = dom.window.MouseEvent;
global.KeyboardEvent = dom.window.KeyboardEvent;
global.localStorage = dom.window.localStorage;
global.sessionStorage = dom.window.sessionStorage;
global.history = dom.window.history;
global.requestAnimationFrame = (cb) => setTimeout(cb, 0);
global.cancelAnimationFrame = (id) => clearTimeout(id);
global.AbortController = dom.window.AbortController;
/* jsdom 无 CSS 接口：loadcss.js 用 CSS.escape 防选择器注入，这里给最小垫片 */
global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_\-\u00A0-\uFFFF]/g, (c) => `\\${c}`) };
/* jsdom 元素无 scrollTo（对话区滚动到底），补空实现 */
dom.window.Element.prototype.scrollTo = function () {};
global.scrollTo = () => {};

/* ---------------------------------------------------------------- fetch mock */
const calls = [];
let statusFixture = {
  key: 'k1', name: '绑定书', state: 'in_progress', done: 1, total: 3, words: 100,
  running: false, paused: false, events: [], path: '/x', has_book: false,
  model: 'model-b1', model_provider_id: 'prov_b', model_provider_name: '供应商B',
  model_bound: true, model_source: 'book'
};
let bookModelBehavior = 'ok';   // ok | bad
let lastBookModelPost = null;
let lastGlobalModelPost = null;

function jsonResp(obj, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'application/json' : '') },
    json: async () => obj
  };
}

const FIXTURES = {
  '/api/library': { items: [{ key: 'k1', name: '绑定书', medium: 'novel', state: 'in_progress',
                             status_label: '创作中', done: 1, total: 3, total_words: 100,
                             updated_at: '2026-01-01T00:00:00' }], stats: {}, facets: {}, counts: {} },
  '/api/model_options': {
    default_provider: 'prov_a',
    options: [
      { id: 'prov_a', name: '供应商A', models: ['model-a1', 'model-a2'], default_model: 'model-a1' },
      { id: 'prov_b', name: '供应商B', models: ['model-b1'], default_model: 'model-b1' }
    ]
  },
  '/api/mediums': [],
  '/api/styles': { items: [] },
  '/api/capabilities': { plan: true, state: true, references: true, skills: true },
  '/api/writestats': { days: [], total: { words: 0 } },
  '/api/inspirations': { items: [] },
  '/api/skills/evolution': { candidates: [], history: [], stats: {} },
  '/api/book-live': { running: false, paused: false, progress: { done: 1, total: 3 },
                      live_log: [], steps: { items: [], cursor: 0 }, stream: { active: false } }
};

global.fetch = async (url, init = {}) => {
  const u = String(url);
  const method = String(init.method || 'GET').toUpperCase();
  const bare = u.split('?')[0];
  let body = null;
  try { body = init.body ? JSON.parse(init.body) : null; } catch { body = null; }
  calls.push({ method, url: u, body });
  if (method === 'POST' && bare.endsWith('/api/book-model')) {
    lastBookModelPost = body || {};
    if (bookModelBehavior === 'bad') {
      return jsonResp({ ok: false, error: '模型 model-a1 不在供应商「供应商B」的模型列表里' }, 400);
    }
    // 绑定成功 = 服务端事实变化（后续 /api/status 反映新绑定）
    statusFixture = { ...statusFixture, model: body.model || '', model_provider_id: body.provider_id || '',
                      model_provider_name: (body.provider_id || '').endsWith('a') ? '供应商A' : '供应商B',
                      model_bound: Boolean(body.provider_id), model_source: body.provider_id ? 'book' : 'global',
                      model_fallback: false };
    return jsonResp({ ok: true, key: body.key, bound: Boolean(body.provider_id),
                       source: body.provider_id ? 'book' : 'global',
                       provider_id: body.provider_id || '', model: body.model || '',
                       msg: body.provider_id
                         ? `本书已绑定 供应商A · ${body.model}，创作全程使用`
                         : '已解除本书绑定，创作回落全局默认模型' });
  }
  if (method === 'POST' && bare.endsWith('/api/global-model')) {
    lastGlobalModelPost = body || {};
    return jsonResp({ ok: true, provider_id: body.provider_id || '', model: body.model || '' });
  }
  if (method === 'GET' && bare.endsWith('/api/status')) {
    return jsonResp({ ...statusFixture });
  }
  if (method === 'GET' && (bare.endsWith('/api/plan') || bare.endsWith('/api/state')
      || bare.endsWith('/api/references'))) {
    return jsonResp({ detail: 'not found' }, 404);
  }
  for (const [p, data] of Object.entries(FIXTURES)) {
    if (bare.endsWith(p)) return jsonResp(data);
  }
  return jsonResp({}, 200);
};

/* ---------------------------------------------------------------- Part A：prefs */
const prefs = await import(pathToFileURL(path.join(V2, 'js/core/prefs.js')).href);
const stateMod = await import(pathToFileURL(path.join(V2, 'js/core/state.js')).href);
const modelFixture = FIXTURES['/api/model_options'];
// 模拟 boot 的异步 model_options 批次；没有可验证选项时不允许显式持久化选择。
stateMod.store.set({ models: { ...modelFixture, options: modelFixture.options.map((p) => ({ ...p })) } });

prefs.persistModelChoice('prov_b', 'model-b1');
const saved = JSON.parse(localStorage.getItem('shenbi.v2.ui') || '{}');
check('persistModelChoice 落 localStorage（providerId/model）',
      saved.providerId === 'prov_b' && saved.model === 'model-b1');
prefs.persistModelChoice('', '');
check('persistModelChoice 空值=清除本机选择',
      JSON.parse(localStorage.getItem('shenbi.v2.ui')).providerId === '');
prefs.persistModelChoice('prov_b', 'model-b1');
prefs.loadPrefs();
check('loadPrefs 恢复上次选择进 ui（刷新后仍显示）',
      stateMod.store.get().ui.providerId === 'prov_b' && stateMod.store.get().ui.model === 'model-b1');

const guardedOptions = [
  ...modelFixture.options,
  { id: 'prov_img', name: '图像供应商', kind: 'image', active: true, models: ['image-1'], default_model: 'image-1' },
  { id: 'prov_off', name: '停用供应商', kind: 'text', active: false, models: ['off-1'], default_model: 'off-1' },
  { id: 'prov_other', name: '其他类型', kind: 'audio', active: true, models: ['audio-1'], default_model: 'audio-1' }
];
stateMod.store.set({ models: { ...modelFixture, options: guardedOptions } });
for (const [pid, mid, label] of [
  ['prov_img', 'image-1', '图像供应商'],
  ['prov_off', 'off-1', '停用供应商'],
  ['prov_other', 'audio-1', '非文本类型'],
  ['prov_a', 'model-missing', '不存在的模型'],
  ['prov_missing', 'model-a1', '不存在的供应商']
]) {
  prefs.persistModelChoice('prov_b', 'model-b1');
  const ok = prefs.persistModelChoice(pid, mid);
  const ls = JSON.parse(localStorage.getItem('shenbi.v2.ui') || '{}');
  check(`拒绝持久化${label}`, ok === false && !ls.providerId && !ls.model);
}

// 全局文本默认变化清旧选择；图像默认变化不参与文本默认签名。
stateMod.store.set({ models: { ...modelFixture, default_provider: 'prov_b', options: guardedOptions } });
let lsNow = JSON.parse(localStorage.getItem('shenbi.v2.ui') || '{}');
check('全局文本默认变化清 UI 与 localStorage 最近选择',
      !lsNow.providerId && !lsNow.model
      && !stateMod.store.get().ui.providerId && !stateMod.store.get().ui.model);
stateMod.store.set({ models: { ...modelFixture, options: guardedOptions } });
prefs.persistModelChoice('prov_b', 'model-b1');
stateMod.store.set({ models: { ...modelFixture, default_image_provider: 'prov_img', options: guardedOptions } });
lsNow = JSON.parse(localStorage.getItem('shenbi.v2.ui') || '{}');
check('图像默认变化不清文本最近选择',
      lsNow.providerId === 'prov_b' && lsNow.model === 'model-b1');

// 旧 localStorage 在异步选项到达后校验：失效项清除，有效项保留。
localStorage.setItem('shenbi.v2.ui', JSON.stringify({ providerId: 'prov_dead', model: 'dead-1' }));
prefs.loadPrefs();
stateMod.store.set({ models: { ...modelFixture, options: guardedOptions.map((p) => ({ ...p })) } });
lsNow = JSON.parse(localStorage.getItem('shenbi.v2.ui') || '{}');
check('启动时清除失效 localStorage 模型选择',
      !lsNow.providerId && !lsNow.model && !stateMod.store.get().ui.providerId && !stateMod.store.get().ui.model);
localStorage.setItem('shenbi.v2.ui', JSON.stringify({ providerId: 'prov_b', model: 'model-b1' }));
prefs.loadPrefs();
stateMod.store.set({ models: { ...modelFixture, options: modelFixture.options.map((p) => ({ ...p })) } });
check('启动时保留有效 localStorage 模型选择',
      stateMod.store.get().ui.providerId === 'prov_b' && stateMod.store.get().ui.model === 'model-b1');

/* ---------------------------------------------------------------- Part B：app.js 真启动 */
await import(pathToFileURL(path.join(V2, 'js/app.js')).href);
const store = () => window.__store.get();

check('app 启动完成（booted）', await until(() => store().booted && store().library.loaded));
check('启动后 ui 恢复持久选择', store().ui.model === 'model-b1' && store().ui.providerId === 'prov_b');

/* 进入绑定书的工作路由 */
window.location.hash = '#/work/k1';
check('进入作品路由且 work 状态到位', await until(() => store().currentKey === 'k1' && store().work && store().work.key === 'k1'));

const modelChip = () => Array.from(document.querySelectorAll('#topbar .tb-chip'))
  .find((b) => b.querySelector('.tb-label.mono'));

check('顶栏芯片显示书级绑定值 model-b1（work.model 优先于 ui.model）',
      await until(() => modelChip() && modelChip().querySelector('.tb-label.mono').textContent === 'model-b1'),
      `实际=${modelChip() && modelChip().querySelector('.tb-label.mono').textContent}`);
check('已绑书时芯片带「本书绑定」标记',
      Boolean(modelChip() && Array.from(modelChip().children).some((c) => c.textContent === '本书绑定')));

async function openModelMenu() {
  modelChip().dispatchEvent(new MouseEvent('click', { bubbles: true }));
  await until(() => document.querySelector('.popover'));
  return document.querySelector('.popover');
}
function popoverItems(pop) {
  return Array.from(pop.querySelectorAll('[role="menuitem"]'));
}

let pop = await openModelMenu();
check('菜单标题=本书创作模型（绑定后全程使用）',
      (pop.querySelector('.popover-label') || {}).textContent === '本书创作模型（绑定后全程使用）');
const boundRow = popoverItems(pop).find((b) => b.textContent.includes('model-b1'));
check('绑定行带「本书绑定」标记', Boolean(boundRow && boundRow.textContent.includes('本书绑定')));
check('绑定行打勾（aria-checked=true）', boundRow && boundRow.getAttribute('aria-checked') === 'true');
check('菜单含「解除本书绑定（回落全局默认）」项',
      popoverItems(pop).some((b) => b.textContent.includes('解除本书绑定')));
check('未绑定行不打勾',
      popoverItems(pop).filter((b) => b.textContent.includes('model-a2'))
        .every((b) => b.getAttribute('aria-checked') !== 'true'));

/* 有书选择 → POST /api/book-model（死规律） */
bookModelBehavior = 'ok';
const toastTexts = () => Array.from(document.querySelectorAll('#toasts .toast-msg')).map((t) => t.textContent);
const beforeToasts = toastTexts().length;
const a2row = popoverItems(pop).find((b) => b.textContent.includes('model-a2'));
a2row.dispatchEvent(new MouseEvent('click', { bubbles: true }));
check('有书时选择 → POST /api/book-model（书级绑定端点）',
      await until(() => lastBookModelPost && lastBookModelPost.provider_id === 'prov_a'
        && lastBookModelPost.model === 'model-a2' && lastBookModelPost.key === 'k1'));
check('绑定成功 toast「本书已绑定 … 创作全程使用」',
      await until(() => toastTexts().slice(beforeToasts).some((t) => t.includes('本书已绑定') && t.includes('创作全程使用'))),
      toastTexts().slice(beforeToasts).join(' | '));
check('成功后选择持久化进 localStorage',
      await until(() => {
        const p = JSON.parse(localStorage.getItem('shenbi.v2.ui') || '{}');
        return p.providerId === 'prov_a' && p.model === 'model-a2';
      }));
check('成功后 ui 选中态更新（芯片显示新绑定值）',
      await until(() => modelChip() && modelChip().querySelector('.tb-label.mono').textContent === 'model-a2'));

/* 绑定失败（400 中文报错）→ 不改变选中态 */
bookModelBehavior = 'bad';
lastBookModelPost = null;
const uiBefore = { model: store().ui.model, providerId: store().ui.providerId };
const lsBefore = localStorage.getItem('shenbi.v2.ui');
pop = await openModelMenu();
const b1row = popoverItems(pop).find((b) => b.textContent.includes('model-b1'));
b1row.dispatchEvent(new MouseEvent('click', { bubbles: true }));
check('失败请求仍打到绑定端点',
      await until(() => lastBookModelPost && lastBookModelPost.provider_id === 'prov_b'));
const t0 = toastTexts().length;
check('失败 toast 中文报错（400 文案透传）',
      await until(() => toastTexts().slice(t0).some((t) => t.includes('不在供应商') && t.includes('模型列表'))),
      toastTexts().slice(t0).join(' | '));
check('失败不改变 ui 选中态（死规律：失败绝不偷改 UI）',
      store().ui.model === uiBefore.model && store().ui.providerId === uiBefore.providerId);
check('失败不写 localStorage', localStorage.getItem('shenbi.v2.ui') === lsBefore);
bookModelBehavior = 'ok';

/* 解绑失败不得偷改 UI/localStorage；成功后的状态收敛才清选择。 */
bookModelBehavior = 'bad';
lastBookModelPost = null;
pop = await openModelMenu();
let unbindRow = popoverItems(pop).find((b) => b.textContent.includes('解除本书绑定'));
const uiBeforeUnbind = { model: store().ui.model, providerId: store().ui.providerId };
const lsBeforeUnbind = localStorage.getItem('shenbi.v2.ui');
const tUnbindFail = toastTexts().length;
unbindRow.dispatchEvent(new MouseEvent('click', { bubbles: true }));
check('解绑失败 toast 透传端点中文错误',
      await until(() => toastTexts().slice(tUnbindFail).some((t) => t.includes('不在供应商'))));
check('解绑失败不改变 UI 与持久选择',
      store().ui.model === uiBeforeUnbind.model && store().ui.providerId === uiBeforeUnbind.providerId
      && localStorage.getItem('shenbi.v2.ui') === lsBeforeUnbind);

/* 解除本书绑定 */
bookModelBehavior = 'ok';
lastBookModelPost = null;
pop = await openModelMenu();
unbindRow = popoverItems(pop).find((b) => b.textContent.includes('解除本书绑定'));
const tUnbind = toastTexts().length;
unbindRow.dispatchEvent(new MouseEvent('click', { bubbles: true }));
check('解除绑定 → POST /api/book-model 空 provider_id',
      await until(() => lastBookModelPost && !lastBookModelPost.provider_id && lastBookModelPost.key === 'k1'));
check('解除绑定 toast 说明回落全局',
      await until(() => toastTexts().slice(tUnbind).some((t) => t.includes('回落全局默认'))));
check('解绑状态收敛后重置 UI 与持久选择',
      await until(() => {
        const p = JSON.parse(localStorage.getItem('shenbi.v2.ui') || '{}');
        return store().work && store().work.model_bound === false
          && !store().ui.providerId && !store().ui.model && !p.providerId && !p.model;
      }));

/* 无书（welcome）→ 明确显示全局默认 */
window.location.hash = '#/welcome';
check('回到首页', await until(() => store().route.name === 'welcome'));
check('首页保持解绑后的空选择（不再伪装最近选择）',
      !store().ui.model && !store().ui.providerId);
check('无书顶栏明确显示全局默认模型-a1',
      await until(() => modelChip() && modelChip().querySelector('.tb-label.mono').textContent === '全局默认 · model-a1'),
      `实际=${modelChip() && modelChip().querySelector('.tb-label.mono').textContent}`);
lastGlobalModelPost = null;
pop = await openModelMenu();
check('无书菜单标题=全局默认模型',
      (pop.querySelector('.popover-label') || {}).textContent === '全局默认模型（新作品默认使用）');
check('无书菜单没有「解除本书绑定」项',
      popoverItems(pop).every((b) => !b.textContent.includes('解除本书绑定')));
const tG = toastTexts().length;
const a1row = popoverItems(pop).find((b) => b.textContent.includes('model-a1'));
a1row.dispatchEvent(new MouseEvent('click', { bubbles: true }));
check('无书选择 → POST /api/global-model（全局默认端点）',
      await until(() => lastGlobalModelPost && lastGlobalModelPost.provider_id === 'prov_a'
        && lastGlobalModelPost.model === 'model-a1'));
check('无书成功不写书级绑定端点', lastBookModelPost === null || !calls.some(
  (c) => c.method === 'POST' && c.url.includes('book-model') && c.body && c.body.key === ''
    && c.body.provider_id === 'prov_a'));
check('无书 toast「全局默认模型已设为」',
      await until(() => toastTexts().slice(tG).some((t) => t.includes('全局默认模型已设为'))));

/* 严格绑定失效：保留 model_bound=true + model_unavailable=true，绝不伪装全局默认。 */
statusFixture = {
  ...statusFixture, model_bound: true, model_source: 'book',
  model: 'model-deleted', model_provider_id: 'prov_dead', model_provider_name: '已删除供应商',
  model_fallback: false, model_unavailable: true,
  model_note: '本书绑定的供应商/模型已不可用，请更换'
};
window.location.hash = '#/library';
check('离开作品路由', await until(() => store().route.name === 'library'));
window.location.hash = '#/work/k1';
check('回到绑定书路由', await until(() => store().route.name === 'work' && store().work && store().work.key === 'k1'));
// 不切路由，直接验证 topbar 的局部 model_unavailable 订阅与菜单现场读 store。
stateMod.store.set((s) => ({ ...s, work: { ...s.work, model: 'model-live', model_unavailable: false } }));
check('绑定模型恢复可用时芯片无需整栏重绘即可更新',
      await until(() => modelChip() && modelChip().querySelector('.tb-label.mono').textContent === 'model-live'));
stateMod.store.set((s) => ({ ...s, work: {
  ...s.work, model: 'model-deleted', model_unavailable: true,
  model_note: '本书绑定的供应商/模型已不可用，请更换'
} }));
check('严格绑定不可用时芯片显示明确错误态',
      await until(() => modelChip() && modelChip().querySelector('.tb-label.mono').textContent === '绑定模型不可用，请更换'),
      `实际=${modelChip() && modelChip().querySelector('.tb-label.mono').textContent}`);
check('绑定不可用仍标记「本书绑定」而非全局默认',
      Boolean(modelChip() && Array.from(modelChip().children).some((c) => c.textContent === '本书绑定')));
pop = await openModelMenu();
check('绑定不可用菜单首项提示更换',
      popoverItems(pop)[0] && popoverItems(pop)[0].textContent.includes('绑定模型不可用，请更换')
      && popoverItems(pop)[0].disabled === true);
check('严格绑定不可用不再 toast「已回落全局默认」',
      !toastTexts().some((t) => t.includes('已回落全局默认') && t.includes('prov_dead')));

/* ---------------------------------------------------------------- session 纯函数契约 */
const session = await import(pathToFileURL(path.join(V2, 'js/views/session.js')).href);
const sessionModels = {
  default_provider: 'prov_a',
  options: [
    { id: 'prov_a', name: '供应商A', kind: 'text', active: true, models: ['model-a1', 'model-a2'], default_model: 'model-a1' },
    { id: 'prov_img', name: '图像', kind: 'image', active: true, models: ['image-1'], default_model: 'image-1' }
  ]
};
const unboundState = {
  currentKey: '', work: null, workMedium: '', chat: { sessionId: 'sid-1' },
  ui: { medium: 'novel', providerId: 'prov_img', model: 'image-1' },
  models: sessionModels
};
const fallbackPayload = session.buildStreamPayload({
  text: '   ', attachments: [{ id: 'att-1', title: '资料', usage_mode: 'facts' }], mode: 'draft'
}, unboundState);
check('session 无有效最近选择时回落到完整全局文本默认 provider/model',
      fallbackPayload.provider_id === 'prov_a' && fallbackPayload.model === 'model-a1');
check('session 图像最近选择不会进入文本请求',
      fallbackPayload.provider_id !== 'prov_img' && fallbackPayload.model !== 'image-1');
check('session 附件-only 自动提示与附件结构完整保留',
      fallbackPayload.message === '请参考我附上的资料继续。'
      && fallbackPayload.attachments[0].id === 'att-1'
      && fallbackPayload.attachments[0].usage_mode === 'facts');
const recentPayload = session.buildStreamPayload({ text: '继续' }, {
  ...unboundState, ui: { medium: 'novel', providerId: 'prov_a', model: 'model-a2' }
});
check('session 最近有效选择优先于全局默认',
      recentPayload.provider_id === 'prov_a' && recentPayload.model === 'model-a2');
const boundPayload = session.buildStreamPayload({ text: '继续', medium: 'novel' }, {
  ...unboundState, currentKey: 'k1', work: { model_bound: true, model: 'book-only' },
  ui: { medium: 'novel', providerId: 'prov_a', model: 'model-a2' }
});
check('session 已绑书不带单轮 provider/model，但保留完整作品标识',
      !('provider_id' in boundPayload) && !('model' in boundPayload)
      && boundPayload.key === 'k1' && boundPayload.project_key === 'k1');
const statusA = session.workStatusSig({ paused: false, status: 'writing' });
const statusB = session.workStatusSig({ paused: true, pausedReason: 'collab_review', status: 'paused' });
const statusC = session.workStatusSig({ paused: true, pausedReason: 'collab_review', status: 'paused', model_unavailable: true });
check('session 状态签名覆盖 pausedReason/status/model_unavailable',
      statusA !== statusB && statusB !== statusC);

/* ---------------------------------------------------------------- 源码契约 */
const appSrc = read('static/v2/js/app.js');
check('app.js setModel 分流：有书走 /api/book-model', appSrc.includes("request('/api/book-model'"));
check('app.js setModel 分流：无书走 api.globalModelSet', appSrc.includes('api.globalModelSet'));
check('app.js 绑定失败不偷改选中态（catch 分支先 return）',
      /catch \(err\) \{[\s\S]{0,400}?本书模型绑定失败[\s\S]{0,400}?return;/.test(appSrc));
check('app.js 回首页不再清除模型选择（选择持久）',
      !/route\.name === 'welcome'[\s\S]{0,200}providerId: null/.test(appSrc));
check('app.js 顶栏签名含书级绑定值（绑定变化即重绘）',
      appSrc.includes('state.work && state.work.model_bound ? 1 : 0'));
check('app.js 右栏签名复用暂停/状态/绑定不可用签名',
      appSrc.includes('workStatusSig(state.work)'));
check('app.js 解绑成功立即清理选择，不依赖异步状态响应',
      /async unbindBookModel\(\)[\s\S]{0,700}?clearModelChoice\(\);[\s\S]{0,300}?return true;/.test(appSrc));
const wizardSrc = read('static/v2/js/views/wizard.js');
check('向导创建载荷使用统一模型选择，缺最近选择时带全局文本默认',
      wizardSrc.includes('resolveModelChoice()')
      && wizardSrc.includes('payload.provider_id = choice.providerId')
      && wizardSrc.includes('payload.model = choice.model'));
check('app.js 绑定失效回落 toast（sessionStorage 去重）',
      appSrc.includes('shenbi_bm_fallback_') && appSrc.includes('model_fallback'));
const sessionSrc = read('static/v2/js/views/session.js');
check('session.js 载荷：已绑书不回传 provider/model（显示值不当选择值）',
      sessionSrc.includes('bookBound') && sessionSrc.includes('state.work && state.work.model_bound'));
check('session.js 载荷：不再把 work.model 塞进 model 字段',
      !/model: state\.ui\.model \|\| \(state\.work && state\.work\.model\)/.test(sessionSrc));
check('session.js REST 回退发送完整 streamPayload',
      sessionSrc.includes('const res = await api.chat(streamPayload);'));
check('session.js 状态签名/收尾刷新覆盖 pausedReason、status、model_unavailable',
      sessionSrc.includes('work.pausedReason')
      && sessionSrc.includes('work.model_unavailable')
      && sessionSrc.includes('scheduleWorkRefresh();'));
const topbarSrc = read('static/v2/js/components/topbar.js');
check('topbar.js 芯片显示书级绑定值优先', topbarSrc.includes('state.work && state.work.model'));
check('topbar.js 绑定行带「本书绑定」标记', topbarSrc.includes('本书绑定'));
check('topbar.js 乐观 toast 已移除（成败由端点结果决定）',
      !topbarSrc.includes("toast(`已切换到"));
const prefsSrc = read('static/v2/js/core/prefs.js');
check('prefs.js 持久化 providerId/model', prefsSrc.includes('providerId') && prefsSrc.includes('persistModelChoice'));

/* ---------------------------------------------------------------- 收尾 */
console.log(failed ? `\n❌ ${failed} 项未通过` : '\n✅ 全部通过');
process.exit(failed ? 1 : 0);
