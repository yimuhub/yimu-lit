// v4.24.12：极速模式入口可见性验证（jsdom 真渲染）。
// 直接加载向导模块，mock /api/onboarding，断言「极速模式」卡片真的进了 DOM。
import { JSDOM } from 'jsdom';
import fs from 'fs';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';

// 同 planbar_express_test.mjs：结尾显式退出 + unref 看门狗，防止定时器把
// 事件循环撑住导致 CI 判超时（2026-11-09 实测）。
const WATCHDOG = setTimeout(() => {
  console.log('❌ 用例超时未结束（疑似定时器未清理）');
  process.exit(1);
}, 90000);
WATCHDOG.unref();

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

// ---- 真实后端 schema（避免测试里手抄一份漂移）----
// 直接调 /api/onboarding 的组装逻辑：by_medium[medium].{brief,basic,style}
const { execFileSync } = await import('child_process');

/* 解释器探测：与 tools/test.sh 用同一批候选路径（多工作树并存时根目录常常没有 .venv）。
   PY 环境变量优先；带路径的候选要真实存在；裸命令名交给系统 PATH 解析。 */
function pickPython() {
  const cands = [
    process.env.PY,
    '.venv/bin/python',
    '../.venv/bin/python',
    '/Users/shanziya/Documents/yimu-lit/.venv/bin/python',
    'python3',
    'python'
  ];
  for (const c of cands) {
    if (!c) continue;
    if (c.includes('/')) {
      try { if (fs.existsSync(c)) return c; } catch { continue; }
    } else {
      return c;
    }
  }
  return 'python3';
}
const py = pickPython();
const raw = execFileSync(py, ['-c', `
import sys, json, os
os.environ["SHENBI_AUTH"] = "0"
sys.path.insert(0, ${JSON.stringify(root)})
import app as appmod
from fastapi.testclient import TestClient
d = TestClient(appmod.app).get("/api/onboarding").json()
print(json.dumps(d, ensure_ascii=False))
`], { cwd: root, encoding: 'utf8' });
const ONB = JSON.parse(raw);

// 页面骨架要跟真实 index.html 对齐：弹窗挂在 #overlays，toast 挂在 #toasts
const dom = new JSDOM(
  '<!doctype html><html><body><div id="root"></div><div id="overlays"></div><div id="toasts"></div></body></html>',
  { url: 'http://127.0.0.1:8848/', pretendToBeVisual: true }
);
global.window = dom.window;
global.document = dom.window.document;
global.Node = dom.window.Node;
global.Element = dom.window.Element;
global.HTMLElement = dom.window.HTMLElement;
// Node 22 起 navigator 是只读 getter，必须 defineProperty 覆盖
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true, writable: true });
Object.defineProperty(global, 'location', { value: dom.window.location, configurable: true, writable: true });
global.getComputedStyle = dom.window.getComputedStyle;
global.CustomEvent = dom.window.CustomEvent;
global.Event = dom.window.Event;
global.localStorage = dom.window.localStorage;
global.sessionStorage = dom.window.sessionStorage;

// ---- mock fetch：只回 /api/onboarding ----
// 注意：api.js 会读 res.headers.get('content-type') 决定走 json() 还是 text()，
// mock 必须补上 headers，否则抛 TypeError → 被 ensureOnboarding 的 catch 吃掉 →
// 静默回落到本地兜底 schema（字段顺序就是旧的），测出来的 DOM 是假的。
const calls = [];
const jsonResponse = (payload) => ({
  ok: true,
  status: 200,
  headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'application/json' : '') },
  json: async () => payload,
  text: async () => JSON.stringify(payload)
});
global.fetch = async (url) => {
  calls.push(String(url));
  if (String(url).includes('/api/onboarding')) return jsonResponse(ONB);
  return jsonResponse({});
};
global.requestAnimationFrame = (cb) => setTimeout(cb, 0);
global.cancelAnimationFrame = (id) => clearTimeout(id);

// 让 api.js 走真实 fetch 分支
dom.window.fetch = global.fetch;

const mod = await import(pathToFileURL(path.join(root, 'static/v2/js/views/wizard.js')).href);

const ctx = {
  openRight: () => {}, refreshWork: () => {}, refreshPlan: () => {}, refreshLibrary: () => {},
  navigate: () => {}, toast: () => {},
};
const modal = mod.openWizard(ctx, { medium: 'novel' });

// 等 schema 拉取与首帧渲染落定
await new Promise((r) => setTimeout(r, 400));

console.log();
console.log('fetch 调用:', calls);
console.log('ONB.by_medium.novel.brief 长度:',
  (ONB.by_medium?.novel?.brief || []).length);

const html = dom.window.document.body.innerHTML;
const cards = [...dom.window.document.querySelectorAll('.mode-card')];
console.log('=== 向导第一步渲染出的创作模式卡片 ===');
for (const c of cards) {
  console.log('  •', (c.textContent || '').replace(/\s+/g, ' ').trim());
}

const hasExpress = cards.some((c) => (c.textContent || '').includes('极速模式'));
console.log();
console.log('卡片总数:', cards.length);
console.log('包含「极速模式」:', hasExpress);

if (!hasExpress) {
  console.log();
  console.log('=== 调试：页面片段 ===');
  console.log(html.slice(0, 2000));
  console.log();
  console.log('fetch 调用:', calls);
  process.exit(1);
}

// ---- 入口必须「一眼可见」，不能埋在第一屏的最后一项 ----
// v4.24.12：创作模式卡片原来排在 brief 段第 5 位，要滚过题材/基调/视角才看得到，
// 实际使用中等��「没有入口」。现在它必须是第一个可交互的字段。
const groups = [...dom.window.document.querySelectorAll('.wiz-stack .wiz-group')];
const labels = groups.map((g) => {
  const lb = g.querySelector('.wiz-label');
  return lb ? (lb.textContent || '').replace(/\s+/g, ' ').trim() : '';
});
console.log();
console.log('=== 第一步字段顺序（.wiz-stack .wiz-group）===');
labels.forEach((l, i) => console.log(`  ${i}. ${l}`));
// 调试：把 brief 段的原始顺序也打出来对照
console.log();
console.log('=== schema brief 顺序（对照）===');
const briefFields = (ONB.by_medium?.novel?.brief || []).map((f) => f.key);
console.log('  ', briefFields.join(' → '));

const modeIdx = labels.findIndex((l) => l.includes('创作模式'));
console.log();
console.log('「创作模式」位置:', modeIdx, modeIdx === 0 ? '(首位 ✅)' : '(不是首位 ❌)');
if (modeIdx !== 0) {
  console.log('❌ 创作模式卡片必须排在第一步第一个字段，否则极速入口等于看不见');
  process.exit(1);
}

// ---- 所有 9 种文体都必须有极速入口 ----
console.log();
console.log('=== 各文体极速入口 ===');
const medSchemas = ONB.by_medium || {};
for (const [med, sch] of Object.entries(medSchemas)) {
  const all = [...(sch.brief || []), ...(sch.basic || []), ...(sch.style || [])];
  const f = all.find((x) => (x.options || []).some((o) => o.value === 'express'));
  const opts = f ? f.options.map((o) => o.value) : [];
  console.log(`  ${med.padEnd(9)} ${f ? `✅ key=${f.key} options=[${opts}]` : '❌ 无极速入口'}`);
  if (!f) {
    console.log('❌ 该文体缺少极速模式入口');
    process.exit(1);
  }
}

console.log();
console.log('✅ 极速模式入口确实渲染在向导第一步首位，且 9 种文体全部覆盖');
clearTimeout(WATCHDOG);
process.exit(0);   // 显式收尾，别让 jsdom 的定时器把 CI 挂到超时
