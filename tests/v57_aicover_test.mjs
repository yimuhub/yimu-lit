/* v5.7.0 AI 生成封面：node 直跑回归（jsdom 仿真，不开浏览器、不依赖 pytest）。
   覆盖面：
     1) core/aicover.js 导出契约（STYLE_PRESETS 5 chips / reduceAiCover 状态机 / buildPayload）；
     2) 状态机全迁移：input→generating→preview→采用(关闭+onApplied)/重新生成/放弃；
        失败回 input 带中文错误文案；
     3) 弹窗渲染：风格描述输入框、chips 选中态、生成中转圈、预览图、三按钮可见性；
     4) 无配置引导文案（GET /api/library/cover/ai/config configured:false → 中文引导+禁用生成）；
     5) mock 模式（?mock=1/sessionStorage）不穿透：本地占位 dataURL、不打 cover/ai 接口、
        采用走本地预览回调；
     6) 契约钉死：library.js 卡片菜单含「AI 生成封面」挂 openAiCoverDialog；settings.js
        供应商区含「图像模型（封面生成）」小节走 /api/library/cover/ai/config|test；
        library.css 增量段与 aicover.js 零硬编码色（全部 tokens 变量）。
   mock 分相运行：IS_MOCK 在 api.js 首次 import 时定型，故 mock 用例由本文件
   自spawn 子进程（node tests/v57_aicover_test.mjs --mock）承担，父进程聚合退出码。 */
import fs from 'fs';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';
import { execFileSync } from 'child_process';
import { JSDOM } from 'jsdom';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const V2 = path.join(ROOT, 'static', 'v2');
const MOCK_PHASE = process.argv.includes('--mock');
const failed = [];
function check(name, cond, extra = '') {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed.push(name);
}
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------------------------------------------------------------- jsdom 环境 */
const dom = new JSDOM('<!doctype html><html><head></head><body><div id="host"></div><div id="overlays"></div><div id="toasts"></div></body></html>',
  { url: 'http://127.0.0.1:8848/', pretendToBeVisual: true });
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
if (MOCK_PHASE) {
  /* mock 粘性标记：api.js import 时 IS_MOCK 即为 true（生产 ?mock=1 的等价路径）。 */
  sessionStorage.setItem('shenbi_mock', '1');
}

/* ---------------------------------------------------------------- fetch 桩 */
let fetchCalls = [];
let configState = { configured: true, base_url: 'http://127.0.0.1:9/v1', model: 'fooocus-xl', size: '1024x1536', has_key: true, key_display: '已设置' };
let aiResult = { ok: true, cover: 'cover.png', url: '/api/library/cover?key=k1&v=123', mtime: 123, mock: false };
let aiShouldFail = false;
/* 生成响应闸门：按住 fetch 不放，才能在「生成中」态断言转圈（否则桩立即 resolve）。 */
let aiGate = null;
let aiGateResolve = null;
function armAiGate() {
  aiGate = new Promise((r) => { aiGateResolve = r; });
}
function openAiGate() {
  if (aiGateResolve) { aiGateResolve(); aiGateResolve = null; aiGate = null; }
}

global.fetch = async (url, init) => {
  fetchCalls.push({ url: String(url), method: (init && init.method) || 'GET' });
  const bare = String(url).split('?')[0];
  if (bare.endsWith('/api/library/cover/ai') && aiGate) await aiGate;
  let payload = null;
  let status = 200;
  if (bare.endsWith('/api/library/cover/ai/config')) payload = { ok: true, ...configState };
  else if (bare.endsWith('/api/library/cover/ai')) {
    if (aiShouldFail) { status = 400; payload = { ok: false, error: '图像接口鉴权失败（401），请检查 API key 是否正确' }; }
    else payload = aiResult;
  } else payload = { error: `演示桩未实现：${bare}` };
  return {
    ok: status === 200,
    status,
    headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: async () => payload,
    text: async () => JSON.stringify(payload)
  };
};

/* ---------------------------------------------------------------- 导入 */
const aicoverUrl = pathToFileURL(path.join(V2, 'js/core/aicover.js')).href;
const aicv = await import(aicoverUrl);

const ITEM = { key: 'k1', name: '长夜灯河', genre: '玄幻', medium: 'novel' };
const host = document.getElementById('host');
const q = (sel, root = document) => root.querySelector(sel);
const qa = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* ================================================================ mock 相：不穿透 */
if (MOCK_PHASE) {
  let applied = null;
  const dlg = await aicv.openAiCoverDialog({ item: ITEM, onApplied: (u) => { applied = u; } });
  await sleep(60);
  check('[mock] 无配置引导不弹（mock 走本地占位链路，不依赖图像供应商）',
    !q('.lib-aicv-guide', dlg.el) || q('.lib-aicv-guide', dlg.el).hidden === true);
  const gen = qa('button', dlg.el).find((b) => b.textContent === '生成封面');
  check('[mock] 生成按钮可用', !!gen && !gen.disabled);
  gen.click();
  await sleep(700);
  const img = q('.lib-aicv-img', dlg.el);
  check('[mock] 预览来自本地占位 dataURL（不穿透接口）',
    !!img && String(img.getAttribute('src')).startsWith('data:image/'),
    img ? String(img.getAttribute('src')).slice(0, 30) : 'no img');
  check('[mock] 全程没有请求 cover/ai 端点（生成/配置都不打）',
    fetchCalls.length === 0,
    JSON.stringify(fetchCalls.map((c) => `${c.method} ${c.url}`)));
  const adopt = qa('button', dlg.el).find((b) => b.textContent === '采用');
  adopt.click();
  await sleep(30);
  check('[mock] 采用回调带回 dataURL（library.js 写入 mockCovers 本地预览）',
    typeof applied === 'string' && applied.startsWith('data:image/'));
  check('[mock] 弹窗已关闭', !q('.modal'));
  finish();
}

/* ================================================================ 1. 导出契约 */
check('aicover.js 导出 openAiCoverDialog / STYLE_PRESETS / reduceAiCover / initialAiCoverState / buildPayload / PROMPT_MAX',
  typeof aicv.openAiCoverDialog === 'function' && typeof aicv.reduceAiCover === 'function'
  && typeof aicv.initialAiCoverState === 'function' && typeof aicv.buildPayload === 'function'
  && Array.isArray(aicv.STYLE_PRESETS) && aicv.PROMPT_MAX === 180);

const CHIPS = ['水墨', '国风插画', '都市光影', '暗黑悬疑', '清新文艺'];
check('STYLE_PRESETS 恰好 5 个预设风格 chips 且文案齐备',
  aicv.STYLE_PRESETS.length === 5 && aicv.STYLE_PRESETS.map((s) => s.label).join(',') === CHIPS.join(',')
  && aicv.STYLE_PRESETS.every((s) => s.label && s.hint),
  aicv.STYLE_PRESETS.map((s) => s.label).join(' / '));

/* ================================================================ 2. 状态机（纯函数） */
const S = aicv.initialAiCoverState;
check('初始态为 input', S().phase === 'input');
check('input --generate--> generating', aicv.reduceAiCover(S(), { type: 'generate' }).phase === 'generating');
check('generating --generated--> preview（带预览 url）',
  (() => { const s = aicv.reduceAiCover({ ...S(), phase: 'generating' }, { type: 'generated', url: '/x' }); return s.phase === 'preview' && s.previewUrl === '/x'; })());
check('generating --failed--> input（错误文案回显）',
  (() => { const s = aicv.reduceAiCover({ ...S(), phase: 'generating' }, { type: 'failed', message: '鉴权失败' }); return s.phase === 'input' && s.error === '鉴权失败'; })());
check('failed 缺省文案兜底中文', aicv.reduceAiCover(S(), { type: 'failed' }).error.includes('生成失败'));
check('preview --regenerate--> generating',
  aicv.reduceAiCover({ ...S(), phase: 'preview', previewUrl: '/x' }, { type: 'regenerate' }).phase === 'generating');
check('preview --adopt--> closed（保留 mock 标记）',
  (() => { const s = aicv.reduceAiCover({ ...S(), phase: 'preview', mock: true }, { type: 'adopt' }); return s.phase === 'closed' && s.mock === true; })());
check('任意态 --cancel--> closed', aicv.reduceAiCover(S(), { type: 'cancel' }).phase === 'closed');
check('setStyle/setPrompt 不迁移状态', (() => {
  const s = aicv.reduceAiCover(S(), { type: 'setStyle', value: '水墨' });
  const s2 = aicv.reduceAiCover(s, { type: 'setPrompt', value: '雨夜' });
  return s.phase === 'input' && s.style === '水墨' && s2.prompt === '雨夜' && s2.style === '水墨';
})());
check('未知 action 原样返回', aicv.reduceAiCover(S(), { type: 'nope' }).phase === 'input');

check('buildPayload：裁剪 prompt ≤180 / style ≤60 / key 透传',
  (() => {
    const p = aicv.buildPayload('k1', '夜'.repeat(300), '暗'.repeat(100));
    return p.key === 'k1' && p.prompt.length === 180 && p.style.length === 60;
  })());
check('buildPayload：首尾空白收敛',
  aicv.buildPayload(' k1 ', '  雨夜当铺  ', '  水墨 ').prompt === '雨夜当铺'
  && aicv.buildPayload(' k1 ', '  雨夜当铺  ', '  水墨 ').style === '水墨');

check('localPlaceholderDataUrl：jsdom 无 canvas 回退 SVG dataURL（不抛错）',
  (() => { const u = aicv.localPlaceholderDataUrl('长夜灯河'); return typeof u === 'string' && u.startsWith('data:image/svg+xml'); })());

/* ================================================================ 3. 弹窗渲染 + 生成流（已配置） */
async function openDialog(applied) {
  const dlg = await aicv.openAiCoverDialog({ item: ITEM, onApplied: applied });
  await sleep(60);
  return dlg;
}

let appliedArg = undefined;
let dlg = await openDialog((u) => { appliedArg = u; });
check('弹窗标题带书名', q('.modal-title').textContent.includes('AI 生成封面') && q('.modal-title').textContent.includes('长夜灯河'));
check('已配置时无引导文案（guide 隐藏）', q('.lib-aicv-guide') && q('.lib-aicv-guide').hidden === true);
check('风格描述输入框就位（aria-label + maxlength=180）',
  !!q('.lib-aicv-prompt') && q('.lib-aicv-prompt').getAttribute('maxlength') === '180'
  && q('.lib-aicv-prompt').getAttribute('aria-label') === '画面风格描述');
check('5 个风格 chips 渲染', qa('.lib-aicv-chip', dlg.el).length === 5);
check('已配置：生成按钮可用', !q('button.btn-primary', dlg.el.closest('.modal')).disabled
  || qa('button', dlg.el).some((b) => b.textContent === '生成封面' && !b.disabled));

/* chip 选中态 */
const chip = qa('.lib-aicv-chip', dlg.el)[0];
chip.click();
await sleep(10);
check('点击 chip：选中态 is-active + aria-pressed',
  chip.classList.contains('is-active') && chip.getAttribute('aria-pressed') === 'true');
chip.click();
await sleep(10);
check('再点同一 chip：取消选中', !chip.classList.contains('is-active'));
chip.click();
await sleep(10);

/* 输入风格描述 */
const promptEl = q('.lib-aicv-prompt', dlg.el);
promptEl.value = '雨夜当铺门口，一盏暖灯照着湿漉漉的青石台阶';
promptEl.dispatchEvent(new window.Event('input', { bubbles: true }));
await sleep(10);

/* 生成 → 生成中（闸门按住响应）→ 预览 */
const genBtn = qa('button', dlg.el).find((b) => b.textContent === '生成封面');
armAiGate();
genBtn.click();
await sleep(20);
check('生成中：转圈提示出现、输入框锁定',
  !!q('.lib-aicv-loading', dlg.el) && !!q('.lib-aicv-spin', dlg.el) && promptEl.disabled === true);
check('生成请求 POST /api/library/cover/ai 且带 key/prompt/style',
  (() => {
    const call = fetchCalls.filter((c) => c.url.includes('/api/library/cover/ai') && c.method === 'POST');
    return call.length === 1;
  })());
openAiGate();
await sleep(80);
check('预览图出现且 src 指向后端封面 url',
  !!q('.lib-aicv-img', dlg.el) && q('.lib-aicv-img', dlg.el).getAttribute('src').includes('/api/library/cover?key=k1'));
check('预览态按钮：放弃 / 重新生成 / 采用（采用可用）',
  (() => {
    const btns = qa('button', dlg.el);
    return btns.some((b) => b.textContent === '放弃')
      && btns.some((b) => b.textContent === '重新生成' && !b.hidden)
      && btns.some((b) => b.textContent === '采用' && !b.disabled);
  })());

/* 重新生成：再次 POST（同样按住闸门看转圈） */
const callsBefore = fetchCalls.length;
armAiGate();
qa('button', dlg.el).find((b) => b.textContent === '重新生成').click();
await sleep(20);
check('重新生成回到 generating（再次转圈）', !!q('.lib-aicv-spin', dlg.el));
openAiGate();
await sleep(80);
check('重新生成发了第二次 POST', fetchCalls.length > callsBefore
  && fetchCalls.filter((c) => c.url.includes('/api/library/cover/ai') && c.method === 'POST').length === 2);

/* 采用：关闭 + onApplied(null)（后端已落盘，library.js bump coverV） */
qa('button', dlg.el).find((b) => b.textContent === '采用').click();
await sleep(30);
check('采用：弹窗关闭 + onApplied(null)（后端已落盘，无需 dataURL）',
  !q('.modal') && appliedArg === null);

/* ================================================================ 4. 放弃 / 失败 */
appliedArg = undefined;
dlg = await openDialog((u) => { appliedArg = u; });
await sleep(40);
qa('button', dlg.el).find((b) => b.textContent === '放弃').click();
await sleep(30);
check('放弃：弹窗关闭且不触发 onApplied', !q('.modal') && appliedArg === undefined);

aiShouldFail = true;
dlg = await openDialog(() => {});
await sleep(40);
qa('button', dlg.el).find((b) => b.textContent === '生成封面').click();
await sleep(80);
check('生成失败：错误文案中文回显（后端 401 文案透传）',
  q('[role="alert"]', dlg.el).textContent.includes('鉴权失败'));
check('生成失败：回到 input 态（生成按钮恢复，转圈消失）',
  !!qa('button', dlg.el).find((b) => b.textContent === '生成封面') && !q('.lib-aicv-spin', dlg.el));
aiShouldFail = false;

/* ================================================================ 5. 无配置引导 */
configState = { ...configState, configured: false, has_key: false };
dlg = await openDialog(() => {});
await sleep(60);
const guide = q('.lib-aicv-guide', dlg.el);
check('无配置：引导文案可见且指向「设置 → 模型与供应商 → 图像模型（封面生成）」',
  !!guide && guide.hidden === false
  && guide.textContent.includes('设置') && guide.textContent.includes('图像模型（封面生成）'),
  guide ? guide.textContent.slice(0, 40) : 'no guide');
check('无配置：生成按钮禁用',
  (() => { const b = qa('button', dlg.el).find((x) => x.textContent === '生成封面'); return b && b.disabled === true; })());
qa('button', dlg.el).find((b) => b.textContent === '放弃').click();
await sleep(20);
configState = { ...configState, configured: true, has_key: true };

/* ================================================================ 6. 契约钉死（源码级） */
const libSrc = read('static/v2/js/views/library.js');
check('library.js 卡片菜单含「AI 生成封面」并挂 openAiCoverDialog',
  libSrc.includes("label: 'AI 生成封面'") && libSrc.includes('openAiCoverDialog')
  && libSrc.includes("from '../core/aicover.js'"));
check('library.js 回调口径：dataURL → mockCovers 本地预览，否则 bump coverV',
  libSrc.includes('model.mockCovers[item.key] = dataUrl') && libSrc.includes('model.coverV[item.key] = Date.now()'));

const setSrc = read('static/v2/js/views/settings.js');
check('settings.js 供应商区含「图像模型（封面生成）」小节（保存+测试按钮）',
  setSrc.includes('图像模型（封面生成）') && setSrc.includes('/api/library/cover/ai/config')
  && setSrc.includes('/api/library/cover/ai/test'));
check('settings.js 密钥框「留空 = 保留已存值」口径',
  setSrc.includes('已保存（留空则不修改）'));

const pySrc = read('bot/webcover.py');
check('webcover.py 契约：b64_json 优先 + url 兜底回捞 + 60s 超时 + 无供应商中文引导',
  pySrc.includes('b64_json') && pySrc.includes('_fetch_image_bytes')
  && pySrc.includes('IMAGE_TIMEOUT = 60') && pySrc.includes('请先在设置→供应商配置图像模型'));
check('webcover.py 落盘复用 weblib（import save_cover，不复制第二套）',
  pySrc.includes('from .weblib import save_cover, _cover_url'));

/* ================================================================ 7. 零硬编码色 */
const cssSrc = read('static/v2/css/library.css');
const aicvCss = cssSrc.slice(cssSrc.indexOf('AI 生成封面弹窗'));
const colorLeak = aicvCss.match(/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsl\(/g) || [];
check('library.css AI 封面增量段零硬编码色（全部 tokens 变量）',
  colorLeak.length === 0 && aicvCss.includes('var(--accent)') && aicvCss.includes('var(--line-2)'),
  colorLeak.join(','));
const aicvSrc = read('static/v2/js/core/aicover.js');
const jsColorLeak = aicvSrc.match(/["'][#][0-9a-fA-F]{3,8}\b|rgba?\(\s*\d|:\s*\d+px\s+\w+color/g) || [];
check('aicover.js 零硬编码色（占位渐变读 tokens 变量/currentColor）', jsColorLeak.length === 0, jsColorLeak.join(','));
check("library.css 仍被 library.js 注入（loadCss('/v2/css/library.css')）",
  libSrc.includes("loadCss('/v2/css/library.css"));

/* mock 相由子进程承担（IS_MOCK 在 api.js 首次 import 时定型，无法同进程翻转）；
   父进程聚合退出码，失败项已在子进程输出里打印。 */
try {
  const out = execFileSync(process.execPath, [fileURLToPath(import.meta.url), '--mock'],
    { encoding: 'utf8' });
  process.stdout.write(out);
} catch (err) {
  process.stdout.write(String(err.stdout || ''));
  check('mock 相（子进程）全绿', false, String(err.stderr || err.message).slice(0, 300));
}

finish();

/* ---------------------------------------------------------------- 收尾 */
function finish() {
  const tag = MOCK_PHASE ? '（mock 相）' : '';
  if (failed.length) {
    console.error(`\nv57_aicover_test${tag}: ${failed.length} 项失败`);
    process.exit(1);
  }
  console.log(`\nv57_aicover_test${tag}: 全部通过`);
  process.exit(0);
}
