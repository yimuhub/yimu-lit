// v4.35.0：导出被闸门拦下时，前端必须真的把「强制导出 / 重写低评分章节」摆出来。
//
// 背景：《复仇太妃：改写贞观》31 万字写完，整书导出一直失败。后端回了
// 「成书未过放行门禁（35.5 分 < 达标线 80）」，前端把它当普通错误 toast 掉，
// 作者在导出页既拿不到书，也不知道下一步该做什么。
//
// 本用例真加载 api.js 跑一遍：
// v4.41.4：默认导出不再拦低分，只有勾选「强制高分」才拦。
//   1) strict_high=1 + 409 not_released → 弹窗出现；
//   2) 点「取消强制高分，直接导原稿」→ 带 force=1 拿到书稿；
//   3) 点「重写到达标线」→ 调 /api/rewrite-parallel 并自动重试导出。
import { JSDOM, VirtualConsole } from 'jsdom';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';

const WATCHDOG = setTimeout(() => {
  console.log('❌ 用例超时未结束（疑似弹窗/定时器未收口）');
  process.exit(1);
}, 60000);
WATCHDOG.unref();

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const vc = new VirtualConsole();       // jsdom 对 <a download>.click() 会抱怨导航未实现，静音
vc.on('jsdomError', () => {});

const dom = new JSDOM(
  '<!doctype html><html><body><div id="overlays"></div><div id="toasts"></div></body></html>',
  { url: 'http://127.0.0.1:8848/', pretendToBeVisual: true, virtualConsole: vc }
);
global.window = dom.window;
global.document = dom.window.document;
global.Node = dom.window.Node;
global.Element = dom.window.Element;
global.HTMLElement = dom.window.HTMLElement;
global.Blob = dom.window.Blob;
global.CustomEvent = dom.window.CustomEvent;
global.Event = dom.window.Event;
global.localStorage = dom.window.localStorage;
global.sessionStorage = dom.window.sessionStorage;
global.getComputedStyle = dom.window.getComputedStyle;
global.requestAnimationFrame = (cb) => setTimeout(cb, 0);
global.cancelAnimationFrame = (id) => clearTimeout(id);
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true, writable: true });
Object.defineProperty(global, 'location', { value: dom.window.location, configurable: true, writable: true });

// jsdom 没有 createObjectURL，blob 下载要自己兜
dom.window.URL.createObjectURL = () => 'blob:mock';
dom.window.URL.revokeObjectURL = () => {};
global.URL = dom.window.URL;

const calls = [];
let rewrote = false;

const json = (payload, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'application/json' : null) },
  json: async () => payload,
  text: async () => JSON.stringify(payload)
});

const file = (text) => ({
  ok: true,
  status: 200,
  headers: {
    get: (k) => {
      const key = String(k).toLowerCase();
      if (key === 'content-type') return 'text/markdown; charset=utf-8';
      if (key === 'content-disposition') return 'attachment; filename="book.md"';
      return null;
    }
  },
  blob: async () => new dom.window.Blob([text], { type: 'text/markdown' })
});

const NOT_RELEASED = {
  error: '成书未过放行门禁（最终质量 35.5 分 < 达标线 80）。请先跑「放行门禁」至达标，或加 force=1 带「未放行初稿」标注导出。',
  code: 'not_released',
  final_quality: 35.5,
  pass_line: 80,
  below: 92,
  chapters: 100,
  title: '复仇太妃：改写贞观'
};

global.fetch = async (url, opts = {}) => {
  const u = String(url);
  const method = (opts && opts.method) || 'GET';
  calls.push({ url: u, method });
  if (u.includes('/api/version')) return json({ version: '4.35.0', desktop: false });
  if (u.includes('/api/rewrite-parallel/progress')) {
    // v4.41.0：重写分支会等后台任务收口后再自动重试导出；这里模拟任务已结束
    return json({ ok: true, rw_running: false, running: false });
  }
  if (u.includes('/api/rewrite-parallel')) {
    rewrote = true;
    return json({ ok: true, started: true, workers: 3, msg: '⚡ 多岗位并行改写已启动' });
  }
  if (u.includes('/api/export')) {
    if (u.includes('force=1') || rewrote) {
      return file('# 复仇太妃：改写贞观（已修复）\n');
    }
    return json(NOT_RELEASED, 409);
  }
  return json({ ok: true });
};

const waitFor = async (fn, label, timeout = 4000) => {
  const t0 = Date.now();
  for (;;) {
    const v = fn();
    if (v) return v;
    if (Date.now() - t0 > timeout) throw new Error(`等待超时：${label}`);
    await new Promise((r) => setTimeout(r, 20));
  }
};

const modalTitle = () => {
  const el = document.querySelector('#overlays .modal-title');
  return el ? el.textContent : '';
};
const buttons = () => [...document.querySelectorAll('#overlays .modal button')];
const clickByText = (text) => {
  const btn = buttons().find((b) => (b.textContent || '').includes(text));
  if (!btn) throw new Error(`弹窗里没有「${text}」按钮，实际按钮：${buttons().map((b) => b.textContent).join(' / ')}`);
  btn.click();
};

let failed = 0;
const ok = (cond, msg) => {
  if (cond) console.log(`✅ ${msg}`);
  else { console.log(`❌ ${msg}`); failed += 1; }
};

const api = await import(pathToFileURL(path.join(root, 'static/v2/js/core/api.js')).href).then((m) => m.api);

// ---------------------------------------------------------------- 场景一：强制导出
{
  calls.length = 0;
  const pending = api.exportWork('book', 'md', { name: '复仇太妃', strictHigh: true });

  await waitFor(() => modalTitle().includes('强制高分'), '强制高分弹窗出现');
  ok(true, '勾选强制高分且未达线时弹出恢复弹窗');
  ok(modalTitle().includes('尚未达标'), `弹窗标题：${modalTitle()}`);

  const bodyText = document.querySelector('#overlays .modal-body').textContent;
  ok(bodyText.includes('35.5') && bodyText.includes('80'), '弹窗正文写明当前分数与达标线');
  ok(!calls.some((c) => c.url.includes('force=1')), '弹窗出现前没有偷偷带 force 重试');

  clickByText('直接导原稿');
  await pending;

  const forced = calls.filter((c) => c.url.includes('/api/export') && c.url.includes('force=1'));
  ok(forced.length === 1, '点「直接导原稿」后带 force=1 重新请求一次');
  ok(!document.querySelector('#overlays .modal'), '导出成功后弹窗自动关闭');
}

// ---------------------------------------------------------------- 场景二：重写低评分章节
{
  calls.length = 0;
  rewrote = false;
  const pending = api.exportWork('book', 'docx', { name: '复仇太妃', strictHigh: true });
  await waitFor(() => modalTitle().includes('强制高分'), '第二次弹窗');
  clickByText('重写到达标线');
  const r = await pending;

  const rw = calls.filter((c) => c.url.includes('/api/rewrite-parallel') && !c.url.includes('/progress'));
  ok(rw.length === 1 && rw[0].method === 'POST', '点「重写到达标线」调用 /api/rewrite-parallel');
  ok(!calls.some((c) => c.url.includes('/api/export') && c.url.includes('force=1')),
     '重写分支不会顺手带 force 把稿子导出去');
  const exports_ = calls.filter((c) => c.url.includes('/api/export'));
  ok(exports_.length === 2, '后台修复结束后自动重新检查并导出（第二次导出不再是同一份被拦稿）');
  ok(r?.cancelled !== true && !document.querySelector('#overlays .modal'),
     '自动重试成功后弹窗关闭，不叠「导出失败」红字');
}

// ---------------------------------------------------------------- 场景三：取消
{
  calls.length = 0;
  rewrote = false;
  const pending = api.exportWork('book', 'md', { strictHigh: true });
  await waitFor(() => modalTitle().includes('强制高分'), '第三次弹窗');
  clickByText('取消');
  const r = await pending;
  ok(!calls.some((c) => c.url.includes('force=1')), '取消后不发强制导出请求');
  ok(r && r.cancelled === true, '取消返回 cancelled（静默，不报错）');
}

console.log(failed ? `\n❌ 导出恢复弹窗用例失败 ${failed} 项` : '\n✅ 导出恢复弹窗用例全部通过');
clearTimeout(WATCHDOG);
dom.window.close();
process.exit(failed ? 1 : 0);
