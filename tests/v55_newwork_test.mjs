// v5.5.0 新建作品双入口 + 直接开聊（无绑定会话）：node 直跑回归（jsdom，不依赖 pytest / 浏览器）。
// 覆盖面：
//   1) newwork.js 双入口弹窗：两卡渲染（标题/副标题/入口标记）、双开守卫、
//      「按开书流程走」回调原样透传 preset 调 ctx.openWizard、「直接开聊」进入无 key 会话路由；
//   2) 关闭路径齐全：Esc（hotkeys.onEscape → closeTopOverlay → modalStack）与点遮罩关闭；
//   3) 直接开聊会话：#/welcome?chat=1 挂 session 视图（无 key）、顶部提示卡、composer 预填不自动发送；
//   4) 入口接线：侧栏按钮与快捷键 N（捕获拦截）、hero 输入器与工具卡「一键开书」同弹窗、
//      体裁快捷入口保持 ctx.openWizard 直连（现行为不变）；
//   5) 无 key 会话契约：runTurn 放行 directChat、buildStreamPayload 无 key 不带 key/project_key；
//   6) newwork.css 命名空间与无硬编码色（全走 tokens 语义变量）。
import fs from 'fs';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';
import { JSDOM } from 'jsdom';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const V2 = path.join(ROOT, 'static', 'v2');
const failed = [];
function check(name, cond, extra = '') {
  console.log(`${cond ? '✅' : '❌'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed.push(name);
}
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

/* ---------------------------------------------------------------- jsdom 环境 */
const dom = new JSDOM(
  '<!doctype html><html><body><div id="overlays"></div><div id="toasts"></div><div id="view"></div></body></html>',
  { url: 'http://127.0.0.1:8848/', pretendToBeVisual: true });
global.window = dom.window;
global.document = dom.window.document;
global.Node = dom.window.Node;
global.Element = dom.window.Element;
global.HTMLElement = dom.window.HTMLElement;
global.localStorage = dom.window.localStorage;
global.getComputedStyle = dom.window.getComputedStyle;
global.location = dom.window.location;
global.CSS = dom.window.CSS;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true, writable: true });
global.requestAnimationFrame = (f) => setTimeout(() => f(), 0);
global.fetch = async () => ({ ok: true, json: async () => ({}) });
/* jsdom 的 Element 没有 scrollTo（chat.scrollToBottom 用），补齐避免 rAF 回调噪音 */
dom.window.HTMLElement.prototype.scrollTo = function () {};

/* ---------------------------------------------------------------- 1. 弹窗渲染与回调 */

const stateMod = await import(pathToFileURL(path.join(V2, 'js/core/state.js')).href);
const { store, resetChat } = stateMod;
const newwork = await import(pathToFileURL(path.join(V2, 'js/components/newwork.js')).href);

check('newwork.js 导出弹窗/直聊/查询判断 API',
  typeof newwork.openNewWorkDialog === 'function' && typeof newwork.enterDirectChat === 'function'
  && typeof newwork.isDirectChatQuery === 'function' && typeof newwork.newworkDialogOpen() === 'boolean',
  `newworkDialogOpen=${newwork.newworkDialogOpen()}`);

let wizardCalls = [];
const ctx = { openWizard: (preset) => { wizardCalls.push(preset); } };

const modal = newwork.openNewWorkDialog(ctx, { preset: { medium: 'novel' }, source: 'test' });
const cards = [...modal.el.querySelectorAll('.nw-card')];
check('弹窗渲染两张入口卡', cards.length === 2, `cards=${cards.length}`);
check('卡片入口标记齐备（wizard/chat）',
  cards.map((c) => c.dataset.entry).join(',') === 'wizard,chat'
  && cards.every((c) => c.dataset.source === 'test'));
check('卡一「按开书流程走」+ 副标题（完整向导链路）',
  cards[0].textContent.includes('按开书流程走')
  && cards[0].textContent.includes('完整向导：体裁 → 设定 → 大纲 → 规划，适合认真开新书'));
check('卡二「直接开聊」+ 副标题（不建书不定设定）',
  cards[1].textContent.includes('直接开聊')
  && cards[1].textContent.includes('不建书、不定设定，直接跟总编辑说想写什么，边聊边定'));
check('弹窗标题/副标题中文（新建作品 / 选一种开始方式）',
  modal.el.textContent.includes('新建作品') && modal.el.textContent.includes('选一种开始方式'));

/* 卡一：preset 原样透传给 ctx.openWizard（现行为不变） */
cards[0].click();
check('「按开书流程走」→ ctx.openWizard(preset) 原样透传',
  wizardCalls.length === 1 && wizardCalls[0] && wizardCalls[0].medium === 'novel',
  JSON.stringify(wizardCalls[0] || null));
check('选完入口弹窗已关闭', document.querySelectorAll('.modal-backdrop').length === 0
  && newwork.newworkDialogOpen() === false);

/* 卡二：进入无 key 会话（hash 带 chat=1 + nonce；preset.text 成为预填语） */
resetChat();
store.set({ chat: { ...store.get().chat, messages: [{ id: 'm1', role: 'user', content: '旧消息' }] } });
const modal2 = newwork.openNewWorkDialog(ctx, { preset: { text: '一个关于灯塔的故事' } });
modal2.el.querySelector('[data-entry="chat"]').click();
const hash = String(dom.window.location.hash || '');
check('「直接开聊」→ 进入无 key 会话路由 #/welcome?chat=1',
  hash.includes('#/welcome?') && newwork.isDirectChatQuery(Object.fromEntries(new URLSearchParams(hash.split('?')[1] || ''))),
  hash);
check('直接开聊清空旧对话（新会话）', store.get().chat.messages.length === 0);
check('preset.text 成为 composer 预填语',
  store.get().ui.directChatPrefill === '一个关于灯塔的故事',
  JSON.stringify(store.get().ui.directChatPrefill));

/* 无 preset：预填默认引导语 */
const modal3 = newwork.openNewWorkDialog(ctx, null);
modal3.el.querySelector('[data-entry="chat"]').click();
check('无 preset 时预填默认引导语（我想写一本……）',
  String(store.get().ui.directChatPrefill || '').startsWith('我想写一本……'),
  JSON.stringify(store.get().ui.directChatPrefill));

/* ---------------------------------------------------------------- 2. 关闭路径：Esc / 遮罩 / 双开守卫 */

const m1 = newwork.openNewWorkDialog(ctx);
const m2 = newwork.openNewWorkDialog(ctx);
check('双开守卫：返回同一句柄', m1 === m2);

/* Esc 链路：app.js 的 installHotkeys({ onEscape: () => closeTopOverlay() })。
   这里按同一接线安装 hotkeys，派发真实 Escape 键验证 modalStack 规范。 */
const uiMod = await import(pathToFileURL(path.join(V2, 'js/core/ui.js')).href);
const hotkeysMod = await import(pathToFileURL(path.join(V2, 'js/core/hotkeys.js')).href);
hotkeysMod.installHotkeys({ onEscape: () => uiMod.closeTopOverlay() });
dom.window.document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
await new Promise((r) => setTimeout(r, 0));
check('Esc 关闭最顶层弹窗（modalStack 规范）',
  document.querySelectorAll('.modal-backdrop').length === 0 && newwork.newworkDialogOpen() === false);

const m3 = newwork.openNewWorkDialog(ctx);
m3.el.parentElement.dispatchEvent(new dom.window.MouseEvent('mousedown', { bubbles: true }));
await new Promise((r) => setTimeout(r, 0));
check('点遮罩关闭弹窗', document.querySelectorAll('.modal-backdrop').length === 0
  && newwork.newworkDialogOpen() === false);

/* ---------------------------------------------------------------- 3. 侧栏入口 + 快捷键 N 捕获拦截 */

const sidebarMod = await import(pathToFileURL(path.join(V2, 'js/components/sidebar.js')).href);
const sideBox = document.createElement('div');
document.body.append(sideBox);
sidebarMod.renderSidebar(sideBox, ctx);
const newBtn = sideBox.querySelector('.new-work');
check('侧栏「新建作品」按钮存在且带快捷键 N 标记',
  Boolean(newBtn) && (newBtn.getAttribute('aria-label') || '').includes('N'));

newBtn.click();
check('侧栏按钮 → 打开双入口弹窗', document.querySelectorAll('.nw-card').length === 2);
uiMod.closeTopOverlay();

/* 裸按 N（无修饰键、非输入焦点）→ 弹窗；捕获拦截后旧的全局监听不再触发 */
let legacyHotkeyFired = 0;
const legacyListener = () => { legacyHotkeyFired += 1; };
window.addEventListener('keydown', legacyListener);   // 模拟 app.js 旧 onNewWork 的冒泡监听
document.body.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'n', bubbles: true }));
check('快捷键 N → 双入口弹窗（捕获拦截，旧监听不再收到事件）',
  document.querySelectorAll('.nw-card').length === 2 && legacyHotkeyFired === 0,
  `legacyFired=${legacyHotkeyFired}`);
const openCount = document.querySelectorAll('.modal-backdrop').length;
document.body.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'n', bubbles: true }));
check('弹窗开着时再按 N 只吞事件不重复开',
  document.querySelectorAll('.modal-backdrop').length === openCount && legacyHotkeyFired === 0);
uiMod.closeTopOverlay();

/* ⌘/Ctrl+N 不属于我们（浏览器/系统语义） */
document.body.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'n', metaKey: true, bubbles: true }));
check('带修饰键的 N 不开弹窗', document.querySelectorAll('.modal-backdrop').length === 0);

/* 输入焦点（可编辑元素）里按字母 n 是打字，不开弹窗 */
const input = document.createElement('input');
document.body.append(input);
input.focus();
input.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'n', bubbles: true }));
check('输入框聚焦时按 n 不开弹窗（打字优先）', document.querySelectorAll('.modal-backdrop').length === 0);
input.blur();
input.remove();

/* ---------------------------------------------------------------- 4. welcome 入口接线（hero / 工具卡 / 空态） */

const welcomeSrc = read('static/v2/js/views/welcome.js');
check('hero 输入器 onSend 改走双入口弹窗（preset 透传）',
  welcomeSrc.includes("openNewWorkDialog(ctx, { preset: payload, source: 'hero' })"));
check('工具卡「一键开书」接双入口弹窗',
  welcomeSrc.includes("label: '一键开书'") && welcomeSrc.includes("source: 'welcome-tools'"));
check('空作品态「新建作品」按钮接双入口弹窗', welcomeSrc.includes("source: 'welcome-empty'"));
check('体裁快捷入口保持 ctx.openWizard 直连（现行为不变）',
  welcomeSrc.includes("btn.addEventListener('click', () => ctx.openWizard({ medium: medium.key }));"));

/* ---------------------------------------------------------------- 5. 直接开聊会话（无 key）挂载 */

resetChat();
store.set({
  route: { name: 'welcome', key: '', chapter: null, query: { chat: '1', t: 'test' } },
  currentKey: '',
  workMedium: '',
  ui: { ...store.get().ui, directChatPrefill: '我想写一本……（例：一本关于实习生隐藏身份的都市爽文）' }
});
const welcomeMod = await import(pathToFileURL(path.join(V2, 'js/views/welcome.js')).href);
const viewBox = document.getElementById('view');
const handle = welcomeMod.renderWelcome(viewBox, ctx);

check('#/welcome?chat=1 挂的是 session 会话视图（无 key）',
  Boolean(viewBox.querySelector('.session-view')) && !viewBox.querySelector('.welcome'));
check('会话区顶部渲染「直接开聊模式」提示卡',
  Boolean(viewBox.querySelector('.direct-chat-notice'))
  && viewBox.querySelector('.direct-chat-notice').textContent.includes('直接开聊模式')
  && viewBox.querySelector('.direct-chat-notice').textContent.includes('总编辑会帮你定体裁/书名并开写')
  && viewBox.querySelector('.direct-chat-notice').textContent.includes('书名确认后会自动切到新书任务'));
const prefillTa = viewBox.querySelector('.composer-dock textarea');
check('composer 预填引导语（不自动发送：无用户气泡）',
  Boolean(prefillTa) && prefillTa.value.startsWith('我想写一本……')
  && store.get().chat.messages.every((m) => m.role !== 'user'));
check('无 key 会话不渲染「正在加载作品…」占位', !viewBox.querySelector('.session-loading'));
check('预填语取走即清（ui.directChatPrefill 已消费）', !store.get().ui.directChatPrefill);
handle.destroy();
check('直接开聊视图 destroy 干净（无残留 DOM）', viewBox.children.length === 0);

/* 无 key 会话契约（session.js）：buildStreamPayload 不带 key；runTurn 放行 directChat */
const sessionSrc = read('static/v2/js/views/session.js');
check('runTurn 无 key 守卫放行 directChat',
  sessionSrc.includes('if (!state.currentKey && !directChat) return;'));
check('buildStreamPayload 无 key 不带 key/project_key（后端按无绑定会话处理）',
  sessionSrc.includes('const withWork = Boolean(state.currentKey);')
  && sessionSrc.includes('...(withWork ? { key: state.currentKey, project_key: state.currentKey } : {})'));
check('renderSession 第三参 opts（directChat/prefill）',
  sessionSrc.includes('export function renderSession(container, ctx = {}, opts = {})')
  && sessionSrc.includes('const directChat = Boolean(opts.directChat);'));
check('直接开聊不渲染作品加载占位（paintBanner 直聊短路）',
  sessionSrc.includes('if (directChat) return;'));

/* ---------------------------------------------------------------- 6. newwork.css：命名空间 + 无硬编码色 */

const cssSrc = read('static/v2/css/newwork.css');
check('newwork.css 命名空间齐备（弹窗 .nw-* / 提示卡 .dcn-* / direct-chat-notice）',
  ['.nw-entries', '.nw-card', '.nw-card-icon', '.nw-card-title', '.nw-card-sub', '.nw-foot',
    '.direct-chat-notice', '.dcn-icon', '.dcn-body', '.dcn-title', '.dcn-text']
    .every((c) => cssSrc.includes(c)));
const hexColors = cssSrc.match(/#[0-9a-fA-F]{3,8}\b/g) || [];
check('newwork.css 无硬编码颜色（全走 tokens 语义变量）', hexColors.length === 0,
  hexColors.length ? hexColors.join(' ') : '');
check('newwork.css 颜色/圆角/字号全部走 var(--…) tokens',
  (cssSrc.match(/var\(--/g) || []).length >= 15
  && ['--line-2', '--accent', '--accent-soft', '--radius-md', '--fs-small', '--ink-secondary']
    .every((v) => cssSrc.includes(v)));

/* ---------------------------------------------------------------- 7. 接线健康度（改动未破坏既有链路） */

const sidebarSrc = read('static/v2/js/components/sidebar.js');
check('sidebar.js 侧栏按钮与快捷键 N 都指向双入口弹窗',
  sidebarSrc.includes("openNewWorkDialog(ctx, { source: 'sidebar' })")
  && sidebarSrc.includes('installNewWorkHotkey(ctx)')
  && sidebarSrc.includes("openNewWorkDialog(_nwHotkeyCtx, { source: 'hotkey' })"));
const appSrc = read('static/v2/js/app.js');
check('app.js 未被本任务改动（ctx.openWizard 现行为保留）',
  appSrc.includes('onNewWork: () => ctx.openWizard()')
  && appSrc.includes('wizardModal = mod.openWizard(ctx, preset'));
check('index.html 未引 newwork.css（由弹窗按需 loadCss 注入）',
  !read('static/v2/index.html').includes('newwork.css'));

/* ---------------------------------------------------------------- 收尾 */

console.log('');
if (failed.length) {
  console.log(`❌ ${failed.length} 项未通过：`);
  for (const f of failed) console.log(`   - ${f}`);
  process.exit(1);
}
console.log('✅ v5.5.0 新建作品双入口 + 直接开聊回归全绿');
process.exit(0);
