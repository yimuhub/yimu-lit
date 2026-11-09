// v4.24.12：极速续写入口的前端真渲染验证（jsdom）。
// 直接加载 planbar.js 渲染目标条，断言「极速续写」按钮真的挂进了 .goal-actions，
// 且在「没有待写章节 / 正在写」时确实不出现。
import { JSDOM } from 'jsdom';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';

// 2026-11-09：CI 上这条用例断言全绿后进程仍不退出，step 挂满 8 分钟被判超时。
// 原因：点「继续」会启动 jobfeed 的轮询链（setTimeout → fetch → setTimeout），
// 卡片一直等不到 finished，定时器就把 node 的事件循环撑住了。
// 兜底：90 秒看门狗（unref，不自己撑住事件循环）+ 结尾显式 process.exit。
const WATCHDOG = setTimeout(() => {
  console.log('❌ 用例超时未结束（疑似定时器未清理）');
  process.exit(1);
}, 90000);
WATCHDOG.unref();

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const dom = new JSDOM(
  '<!doctype html><html><body><div id="root"></div><div id="overlays"></div><div id="toasts"></div></body></html>',
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
global.localStorage = dom.window.localStorage;
global.sessionStorage = dom.window.sessionStorage;
global.requestAnimationFrame = (cb) => setTimeout(cb, 0);
global.cancelAnimationFrame = (id) => clearTimeout(id);

// fetch 一律回空——本测试只关心渲染，不发真实请求
const calls = [];
global.fetch = async (url, init) => {
  calls.push({ url: String(url), body: init && init.body });
  return {
    ok: true,
    status: 200,
    headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'application/json' : '') },
    json: async () => ({}),
    text: async () => '{}'
  };
};
dom.window.fetch = global.fetch;

const mod = await import(pathToFileURL(path.join(root, 'static/v2/js/components/planbar.js')).href);

const doc = dom.window.document;
let failed = 0;
function check(name, cond, extra = '') {
  console.log(`${cond ? '✅' : '❌'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed++;
}

const baseWork = {
  key: 'bk-demo',
  medium: 'novel',
  total: 100,
  done: 50,
  running: false,
  paused: false,
  state: 'in_progress'
};

function render(overrides = {}, plan = null) {
  doc.body.innerHTML = '';
  const work = { ...baseWork, ...overrides };
  const bar = mod.renderPlanBar({ work, plan, ctx: { refreshWork: () => {}, refreshPlan: () => {} } });
  doc.body.append(bar);
  const actions = doc.querySelector('.goal-actions');
  return {
    actions,
    buttons: actions ? [...actions.querySelectorAll('button')] : [],
    zapBtn: actions
      ? [...actions.querySelectorAll('button')].find((b) => (b.getAttribute('aria-label') || '') === '创作速度')
      : null
  };
}

// ---- 0. 刷新现场：work 还没并进作品库（没有 key），按钮也必须渲染「继续」 ----
// 用户报的就是这一幕：刷新后 /api/status 先回、/api/library 后回，
// 合并出的 work 没有 key，按钮工厂首行就 return null → 按钮整颗消失。
{
  doc.body.innerHTML = '';
  const key = 'bk-refresh';
  const workNoKey = { medium: 'novel', total: 100, done: 57, running: false, paused: false, state: 'in_progress' };
  const btn = mod.createCreativeToggle({ work: workNoKey, ctx: { currentKey: key, refreshWork: () => {} } });
  const text = btn ? btn.textContent.replace(/\s+/g, '') : '(null)';
  check('work 缺 key 时仍渲染创作按钮（刷新场景）', !!btn, text);
  check('刷新后按钮显示「继续」而不是空白', text.includes('继续'), text);
  check('刷新后按钮未被 hidden', !!btn && btn.hidden === false);
}

// ---- 0b. 状态字段也脏（state=unknown）但还有没写完的章节 → 仍是「继续」 ----
{
  doc.body.innerHTML = '';
  const key = 'bk-refresh2';
  const dirty = { medium: 'novel', total: 100, done: 57 }; // 无 running/paused/state
  const btn = mod.createCreativeToggle({ work: dirty, ctx: { currentKey: key, refreshWork: () => {} } });
  const text = btn ? btn.textContent.replace(/\s+/g, '') : '(null)';
  check('状态字段缺失但 chapters 没写完 → 仍给「继续」入口', text.includes('继续'), text);
}

// ---- 0c. 点「继续」要真的打 /api/resume；点「创作中」要真的打 /api/pause ----
{
  doc.body.innerHTML = '';
  calls.length = 0;
  const key = 'bk-click';
  const btn = mod.createCreativeToggle({
    work: { key, medium: 'novel', total: 100, done: 57, running: false, paused: false, state: 'in_progress' },
    ctx: { currentKey: key, refreshWork: () => {}, refreshPlan: () => {} }
  });
  doc.body.append(btn);
  btn.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  await new Promise((res) => setTimeout(res, 60));
  check('点「继续」调用了 /api/resume', calls.some((c) => c.url.includes('/api/resume')),
    calls.map((c) => c.url).join(' / '));
}
{
  doc.body.innerHTML = '';
  calls.length = 0;
  const key = 'bk-click2';
  const btn = mod.createCreativeToggle({
    work: { key, medium: 'novel', total: 100, done: 57, running: true, paused: false, state: 'in_progress' },
    ctx: { currentKey: key, refreshWork: () => {}, refreshPlan: () => {} }
  });
  doc.body.append(btn);
  check('运行中按钮显示「创作中」', btn.textContent.includes('创作中'), btn.textContent.trim());
  btn.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  await new Promise((res) => setTimeout(res, 60));
  check('点「创作中」调用了 /api/pause', calls.some((c) => c.url.includes('/api/pause')),
    calls.map((c) => c.url).join(' / '));
}

// ---- 1. 有未完成章节 → 极速入口必须出现 ----
let r = render({});
check('有未完成章节时挂出极速入口', !!r.zapBtn,
  r.buttons.map((b) => b.getAttribute('aria-label') || b.textContent.trim()).join(' / '));

// ---- 2. 全部写完 → 不该有入口（没有「续写」可言） ----
r = render({ done: 100, total: 100 });
check('全部写完时不挂极速入口', !r.zapBtn);

//---- 3. 正在写 → 不让切速度 ----
r = render({ running: true });
check('正在写时不挂极速入口', !r.zapBtn);

// ---- 4. 点击 → 弹出「创作速度」菜单，含极速续写与切回标准 ----
r = render({}, { plan: { express: false } });
check('有未完成章节时挂出极速入口（普通模式）', !!r.zapBtn);
if (r.zapBtn) {
  r.zapBtn.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  await new Promise((res) => setTimeout(res, 30));
  const pop = doc.querySelector('.popover');
  const items = pop ? [...pop.querySelectorAll('.popover-item')] : [];
  const labels = items.map((i) => i.textContent.replace(/\s+/g, ' ').trim());
  console.log('   菜单项：', labels);
  check('弹出「创作速度」菜单', !!pop);
  check('菜单里有「极速续写」', labels.some((l) => l.includes('极速续写')));
  check('菜单里有「切回标准」', labels.some((l) => l.includes('切回标准')));
  check('已是标准模式时「切回标准」置灰', items.length >= 2 && items[1].disabled === true);

  // 选「极速续写」→ 真的打接口
  items[0].dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  await new Promise((res) => setTimeout(res, 80));
  const hit = calls.find((c) => c.url.includes('/api/express-write'));
  check('点「极速续写」调用了 /api/express-write', !!hit, hit ? hit.body : '没发出请求');
  check('请求体带 express 模式', !!hit && String(hit.body).includes('express'));
}

// ---- 5. 已是极速模式的书：「切回标准」可点 ----
calls.length = 0;
r = render({}, { plan: { express: true } });
if (r.zapBtn) {
  r.zapBtn.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  await new Promise((res) => setTimeout(res, 30));
  const items = [...doc.querySelectorAll('.popover .popover-item')];
  check('已是极速模式时「切回标准」可点', items.length >= 2 && items[1].disabled === false);
  if (items.length >= 2) {
    items[1].dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
    await new Promise((res) => setTimeout(res, 80));
    const hit = calls.find((c) => c.url.includes('/api/express-write'));
    check('点「切回标准」调用了 /api/express-write', !!hit);
    check('请求体带 standard 模式', !!hit && String(hit.body).includes('standard'));
  }
}

// ---- 6. 极速开关：与「极速续写」共用后端；v4.24.15 起运行中也可切（下一章生效） ----
doc.body.innerHTML = '';
const sw = mod.createExpressSwitch({
  work: { ...baseWork },
  plan: { plan: { express: false } },
  ctx: { refreshWork: () => {}, refreshPlan: () => {}, refreshState: () => {} }
});
doc.body.append(sw);
check('极速开关渲染在标准态', !!sw && sw.textContent.includes('极速'));
check('标准态不显示「· 开」', !!sw && !sw.textContent.includes('· 开'));
calls.length = 0;
sw.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
await new Promise((res) => setTimeout(res, 80));
const swHit = calls.find((c) => c.url.includes('/api/express-toggle'));
check('点极速开关调用了 /api/express-toggle', !!swHit, swHit ? swHit.body : '没发出请求');
check('请求体带 on=true', !!swHit && String(swHit.body).includes('"on":true'));

doc.body.innerHTML = '';
const swOn = mod.createExpressSwitch({
  work: { ...baseWork },
  plan: { plan: { express: true } },
  ctx: {}
});
doc.body.append(swOn);
check('极速态按钮带「· 开」', swOn.textContent.includes('· 开'));

doc.body.innerHTML = '';
const swRunning = mod.createExpressSwitch({
  work: { ...baseWork, running: true },
  plan: { plan: { express: false } },
  ctx: {}
});
doc.body.append(swRunning);
check('运行中极速开关仍可点', swRunning.disabled === false);
check('运行中提示「写完即生效」', String(swRunning.title || '').includes('写完即生效'));
calls.length = 0;
swRunning.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
await new Promise((res) => setTimeout(res, 80));
const runHit = calls.find((c) => c.url.includes('/api/express-toggle'));
check('运行中点开关真的发请求', !!runHit, runHit ? runHit.body : '没发出请求');

console.log();
if (failed) {
  console.log(`❌ ${failed} 项未通过`);
  process.exit(1);
}
console.log('✅ 极速续写入口在目标条真实渲染且行为正确');
clearTimeout(WATCHDOG);
process.exit(0);   // 显式收尾：jsdom + jobfeed 定时器不会自己让事件循环空出来
