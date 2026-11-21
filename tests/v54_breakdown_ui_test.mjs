// v5.4.0 拆书工作台 UI 测试（node 直跑，jsdom）。
// 覆盖：
//   1. UI 状态机（list → create → preview → work，nextScreen 纯函数）；
//   2. 段落级高亮映射（paraRange 归一化 + highlightParas 真实高亮 + startPara/endPara 锚定）；
//   3. 分析卡锚定（点击节奏点/大纲节点 → onAnchor 收到正确区间、卡片标记 active）；
//   4. 手法入账本 payload 结构（buildAdoptPayload：越界/非法下标过滤、projectKey 透传）；
//   5. 切章预览行 / 进度文案 / 章节徽标等纯函数。
// 不发真实请求（fetch 回空），不碰后端。
import { JSDOM } from 'jsdom';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';

const WATCHDOG = setTimeout(() => {
  console.log('❌ 用例超时未结束');
  process.exit(1);
}, 60000);
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
dom.window.HTMLElement.prototype.scrollIntoView = function scrollIntoView() { /* jsdom 无布局，吞掉 */ };

global.fetch = async () => ({
  ok: true,
  status: 200,
  headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'application/json' : '') },
  json: async () => ({}),
  text: async () => '{}'
});
dom.window.fetch = global.fetch;

const mod = await import(pathToFileURL(path.join(root, 'static/v2/js/views/breakdown.js')).href);

const doc = dom.window.document;
let failed = 0;
function check(name, cond, extra = '') {
  console.log(`${cond ? '✅' : '❌'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed++;
}

// ---- 1. UI 状态机 ----
{
  check('初始 list + new → create', mod.nextScreen('list', { type: 'new' }) === 'create');
  check('create + created → preview', mod.nextScreen('create', { type: 'created' }) === 'preview');
  check('preview + resplit → 留在 preview', mod.nextScreen('preview', { type: 'resplit' }) === 'preview');
  check('preview + confirm → work', mod.nextScreen('preview', { type: 'confirm' }) === 'work');
  check('任意 + open → work', mod.nextScreen('create', { type: 'open' }) === 'work');
  check('work + back → list', mod.nextScreen('work', { type: 'back' }) === 'list');
  check('未知事件不换屏', mod.nextScreen('work', { type: 'nonsense' }) === 'work');
  check('空事件不换屏', mod.nextScreen('work', null) === 'work');
}

// ---- 2. 段落区间归一化（startPara/endPara 高亮映射）----
{
  const [s, e] = mod.paraRange(3, 6, 10);
  check('正常区间原样', s === 3 && e === 6, `${s}-${e}`);
  const [s2, e2] = mod.paraRange(6, 3, 10);
  check('倒挂区间自动交换', s2 === 3 && e2 === 6, `${s2}-${e2}`);
  const [s3, e3] = mod.paraRange(-4, 99, 5);
  check('越界 clamp 到 [1,total]', s3 === 1 && e3 === 5, `${s3}-${e3}`);
  const [s4] = mod.paraRange('x', null, 5);
  check('非法输入回退到 1', s4 === 1, String(s4));
  const [s5, e5] = mod.paraRange(1, 1, 0);
  check('total=0 回退到 1', s5 === 1 && e5 === 1, `${s5}-${e5}`);
  const [s6, e6] = mod.paraRange(2.6, 5.4, 10);
  check('小数四舍五入', s6 === 3 && e6 === 5, `${s6}-${e6}`);
}

// ---- 3. 原文渲染 + highlightParas ----
{
  const orig = mod.renderOriginalText(['第一段。', '第二段。', '第三段。', '第四段。']);
  doc.body.append(orig);   // 高亮映射只对已挂载的容器生效（工作台内必然已挂载）
  check('原文段落数与 data-para 编号', orig.querySelectorAll('[data-para]').length === 4
    && orig.querySelector('[data-para="2"]').textContent.includes('第二段'));
  check('data-para 带 total 供高亮映射', orig.querySelector('[data-para="1"]').dataset.total === '4');

  const hit = mod.highlightParas(orig, 2, 3);
  const hl = Array.from(orig.querySelectorAll('[data-para].bd-hl'));
  check('区间 2-3 高亮两段', hl.length === 2 && hit.length === 2
    && hl[0].dataset.para === '2' && hl[1].dataset.para === '3', hit.join(','));
  check('区间外段落无高亮', !orig.querySelector('[data-para="1"]').classList.contains('bd-hl')
    && !orig.querySelector('[data-para="4"]').classList.contains('bd-hl'));

  mod.highlightParas(orig, 1, 99);   // 越界 → clamp 到全文
  check('越界区间 clamp 到全文高亮', orig.querySelectorAll('[data-para].bd-hl').length === 4);
  mod.highlightParas(orig, 6, 3);    // 倒挂 → 交换后高亮 3-6 → clamp 3-4
  const hl2 = Array.from(orig.querySelectorAll('[data-para].bd-hl')).map((el) => el.dataset.para);
  check('倒挂区间 clamp 后高亮 3-4', hl2.join(',') === '3,4', hl2.join(','));
  check('空容器安全', mod.highlightParas(null, 1, 2).length === 0);

  // 再次高亮先清旧高亮
  mod.highlightParas(orig, 1, 1);
  const hl3 = Array.from(orig.querySelectorAll('[data-para].bd-hl')).map((el) => el.dataset.para);
  check('重复锚定先清旧高亮', hl3.join(',') === '1', hl3.join(','));
}

// ---- 4. 分析卡锚定（点击 → onAnchor + active）----
{
  const analysis = {
    brief: '梗概文本',
    rhythm: [{ para: 2, type: 'twist', note: '反转' }, { para: 4, type: 'hook', note: '' }],
    conflict: '甲 vs 乙',
    value_shift: '安全→危险',
    hook: { type: '危机钩', text: '门外有脚步声' },
    outline: [{ startPara: 1, endPara: 2, title: '开端', summary: '醒来' }],
    techniques: ['章末反转钩'],
    characters: ['林然'],
    golden: { opening_hook: '第一段下钩', immersion: '身份困境', info_pacing: '设定缓给' },
    para_count: 4
  };
  let anchored = null;
  const cards = mod.renderAnalysisCards(analysis, { onAnchor: (s, e) => { anchored = [s, e]; } });

  const chips = Array.from(cards.querySelectorAll('.bd-chip'));
  check('节奏点 chip 数量', chips.length === 2, String(chips.length));
  chips[0].click();
  check('点击节奏点 chip → onAnchor(段,段)', anchored && anchored[0] === 2 && anchored[1] === 2, JSON.stringify(anchors(anchored)));
  check('点击后卡片标记 active', cards.querySelectorAll('.bd-card.active').length === 1);

  const outlineBtn = cards.querySelector('.bd-outline-item');
  outlineBtn.click();
  check('点击大纲节点 → onAnchor(startPara,endPara)', anchored[0] === 1 && anchored[1] === 2, JSON.stringify(anchors(anchored)));

  const miniBtn = cards.querySelector('.bd-mini');
  miniBtn.click();
  check('章末钩子卡定位到 para_count 末段', anchored[0] === 4 && anchored[1] === 4, JSON.stringify(anchors(anchored)));

  check('黄金三章深拆卡渲染', !!cards.querySelector('.bd-golden'));
  check('手法标签渲染', cards.querySelector('.bd-tag').textContent === '章末反转钩');
  check('梗概渲染', cards.querySelector('.bd-brief').textContent === '梗概文本');

  function anchors(v) { return Array.isArray(v) ? v : []; }
}

// ---- 5. 手法入账本 payload 结构 ----
{
  const report = { techniques: [{ name: 'a' }, { name: 'b' }, { name: 'c' }] };
  const p1 = mod.buildAdoptPayload(report, [0, 2], 'novel_demo');
  check('payload 带合法下标与 projectKey', p1.indices.length === 2 && p1.indices[0] === 0
    && p1.indices[1] === 2 && p1.projectKey === 'novel_demo', JSON.stringify(p1));
  const p2 = mod.buildAdoptPayload(report, [0, 3, -1, 1.5, 'x']);
  check('越界/负数/小数/非数字下标全部过滤', p2.indices.length === 1 && p2.indices[0] === 0, JSON.stringify(p2));
  const p3 = mod.buildAdoptPayload(null, [0]);
  check('无报告 → 空下标', p3.indices.length === 0);
  const p4 = mod.buildAdoptPayload(report, [0, 1]);
  check('默认 projectKey 为空串（入拆书项目账本）', p4.projectKey === '');
}

// ---- 6. 预览行 / 进度文案 / 徽标 ----
{
  const rows = mod.splitPreviewRows([
    { no: 1, title: '第一章 起', chars: 100, volume: '第一卷', golden: true },
    { no: 2, title: '第二章 承', chars: 200 }
  ], 12);
  check('预览行包含卷与黄金标记', rows.length === 2 && rows[0].volume === '第一卷' && rows[0].golden === true
    && rows[1].volume === '' && rows[1].golden === false);
  check('预览行 limit 生效', mod.splitPreviewRows(
    Array.from({ length: 20 }, (_, i) => ({ no: i + 1, title: `t${i}`, chars: 1 })), 5).length === 5);

  check('进度文案（含失败与当前章）',
    mod.progressText({ total: 10, done: 4, failed: 1, currentTitle: '第三章 转' })
      .includes('4/10') && mod.progressText({ total: 10, done: 4, failed: 1, currentTitle: '第三章 转' }).includes('第三章'));
  check('进度文案（零进度）', mod.progressText({ total: 3, done: 0, failed: 0 }) === '0/3 章已拆');

  check('徽标：待拆/拆解中/完成/失败/部分失败', mod.chapterBadge('pending').label === '待拆'
    && mod.chapterBadge('running').label === '拆解中'
    && mod.chapterBadge('done').label === '完成'
    && mod.chapterBadge('failed').label === '失败'
    && mod.chapterBadge('partial').label === '部分失败'
    && mod.chapterBadge('draft').tone === 'dim'
    && mod.chapterBadge('unknown').label === '待拆');
}

// ---- 7. 真渲染：列表屏 + 工作台骨架（fetch 回空 → 空态）----
{
  const container = doc.createElement('div');
  doc.body.append(container);
  const view = mod.renderBreakdown(container, {});
  check('renderBreakdown 返回视图句柄（el/refresh/destroy）', !!view && typeof view.refresh === 'function'
    && typeof view.destroy === 'function');
  await new Promise((r) => setTimeout(r, 20));   // 等 loadList 的 fetch 回调
  check('列表空态渲染（还没有拆书项目）', view.el.textContent.includes('还没有拆书项目'));
  view.destroy();
  check('destroy 后视图摘除', !view.el.isConnected);
}

console.log(failed ? `\n❌ ${failed} 项未通过` : '\n✅ 拆书工作台 UI 测试全部通过');
process.exit(failed ? 1 : 0);
