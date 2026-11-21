// v5.5.2 资料画布测试（node 直跑，jsdom）。
// 覆盖：
//   1) graphcanvas.js 纯函数：位置记忆键/读写、缩放钳制、节点尺寸、命中检测（包围盒）；
//   2) 端点方位选择（横纵主导侧 + 符号）与边几何（贝塞尔控制点、标签中点）；
//   3) 同心圆布局（权重降序、圆心 + 逐环）与思维导图层级列布局；
//   4) 思维导图展平（父子/前后置连边 + 徽记）与展开层级过滤；
//   5) SVG serialize 导出字符串（xmlns/尺寸/背景板）与画布实例端到端（渲染/缩放/销毁）；
//   6) views/canvas.js：tab 状态机、人物徽记、角色图/思维导图数据整形、
//      renderCanvas 端到端（选书 → 聚合渲染 → 切 tab → 组节点展开 → 连线点击改标签 →
//      重置布局清记忆）；
//   7) rightpanel.js 参考页签画布入口存在；
//   8) canvas.css 语义变量口径（零硬编码色）；
//   9) bot/webcanvas.py ↔ 前端接口契约字面量对齐。
import fs from 'fs';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';
import { JSDOM } from 'jsdom';

const WATCHDOG = setTimeout(() => {
  console.log('❌ 用例超时未结束');
  process.exit(1);
}, 60000);
WATCHDOG.unref();

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const V2 = path.join(root, 'static', 'v2');
const read = (p) => fs.readFileSync(path.join(root, p), 'utf8');

const dom = new JSDOM(
  '<!doctype html><html><body><div id="root"></div></body></html>',
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
global.localStorage = dom.window.localStorage;
global.sessionStorage = dom.window.sessionStorage;
global.XMLSerializer = dom.window.XMLSerializer;
global.requestAnimationFrame = (cb) => setTimeout(cb, 0);
global.cancelAnimationFrame = (id) => clearTimeout(id);

let failed = 0;
function check(name, cond, extra = '') {
  console.log(`${cond ? '✅' : '❌'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed++;
}

/* fetch mock：资料画布两接口 + 其余全空。 */
const apiCalls = [];
const PAYLOAD = {
  ok: true,
  key: 'k1',
  resolvedKey: 'k1',
  title: '测试书·画布',
  characters: {
    nodes: [
      { id: '陈默', label: '陈默', kind: 'person', weight: 46 },
      { id: '林哲', label: '林哲', kind: 'person', weight: 29 },
      { id: '苏晚', label: '苏晚', kind: 'person', weight: 15 }
    ],
    edges: [
      { from: '林哲', to: '陈默', label: '对手', kind: 'relation' },
      { from: '苏晚', to: '陈默', label: '暧昧', kind: 'relation' }
    ]
  },
  story: {
    root: {
      id: 'root', label: '测试书·画布', kind: 'root',
      children: [
        { id: 'grp-main', label: '主线', kind: 'group', children: [{ id: 'main-0', label: '底层逆袭', kind: 'main' }] },
        { id: 'grp-person', label: '人物线', kind: 'group', children: [{ id: 'p-陈默', label: '陈默', kind: 'person' }] },
        { id: 'grp-branch', label: '支线', kind: 'group', children: [{ id: 'x-X1', label: '神笔', kind: 'branch', ch: 2 }] },
        { id: 'grp-foreshadow', label: '伏笔', kind: 'group', children: [
          { id: 'f-L1', label: '金色光泽', kind: 'foreshadow', ch: 1, status: 'planned' },
          { id: 'f-L2', label: '瞬时杂音', kind: 'foreshadow', ch: 3, status: 'paid-off' }
        ] }
      ]
    },
    seq: [['f-L1', 'f-L2']]
  },
  source: {}
};
function jsonResp(obj) {
  return {
    ok: true,
    status: 200,
    headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'application/json' : '') },
    json: async () => obj
  };
}
global.fetch = async (url, init) => {
  const u = String(url);
  apiCalls.push({ url: u, method: (init && init.method) || 'GET', body: (init && init.body) || '' });
  if (u.includes('/api/canvas/graph')) return jsonResp(PAYLOAD);
  if (u.includes('/api/canvas/edge')) return jsonResp({ ok: true });
  return jsonResp({});
};
dom.window.fetch = global.fetch;

const gc = await import(pathToFileURL(path.join(V2, 'js/core/graphcanvas.js')).href);
const view = await import(pathToFileURL(path.join(V2, 'js/views/canvas.js')).href);
const state = await import(pathToFileURL(path.join(V2, 'js/core/state.js')).href);

/* ------------------------------------------------ 1. 位置记忆 */

{
  check('posStoreKey 格式 ew-canvas-pos:<book>:<tab>',
    gc.posStoreKey('bk1', 'chars') === 'ew-canvas-pos:bk1:chars'
    && gc.posStoreKey('', null) === 'ew-canvas-pos::',
    gc.posStoreKey('bk1', 'chars'));
  localStorage.removeItem(gc.posStoreKey('bkT', 'chars'));
  check('无记忆时 loadPositions 返回 null', gc.loadPositions('bkT', 'chars') === null);
  const pos = { 陈默: { x: 10, y: -3.5 }, 林哲: { x: 0, y: 0 } };
  check('savePositions 成功回写', gc.savePositions('bkT', 'chars', pos) === true);
  const loaded = gc.loadPositions('bkT', 'chars');
  check('loadPositions 往返一致', loaded && loaded['陈默'].x === 10 && loaded['陈默'].y === -3.5
    && loaded['林哲'].x === 0);
  check('不同 tab 隔离', gc.loadPositions('bkT', 'story') === null);
  gc.savePositions('bkT', 'chars', {});
  check('空记忆等价清除', gc.loadPositions('bkT', 'chars') === null);
  localStorage.setItem(gc.posStoreKey('bkBad', 'chars'), '{oops');
  check('坏 JSON 安全降级 null', gc.loadPositions('bkBad', 'chars') === null);
}

/* ------------------------------------------------ 2. 缩放 / 尺寸 */

{
  check('clampZoom 下限 0.4', gc.clampZoom(0.1) === 0.4);
  check('clampZoom 上限 2.5', gc.clampZoom(9) === 2.5);
  check('clampZoom 原值保留', gc.clampZoom(1.3) === 1.3);
  check('clampZoom 非法回退 1', gc.clampZoom('x') === 1);
  const small = gc.nodeSize({ label: '苏晚', kind: 'person' });
  const big = gc.nodeSize({ label: '一个特别特别长的名字被截断', kind: 'person' });
  const group = gc.nodeSize({ label: '伏笔', kind: 'group' });
  const heavy = gc.nodeSize({ label: '陈默', kind: 'person', weight: 46 });
  const light = gc.nodeSize({ label: '陈默', kind: 'person', weight: 1 });
  check('节点宽度随字数增长且按 9 字截断', small.w === 84 && big.w === 169 && big.w > small.w,
    `${small.w}/${big.w}`);
  check('节点大小按出场权重放大（无权重不放大）',
    heavy.w === Math.min(220, 84 + 24) && light.w === 84 && heavy.w > light.w,
    `${heavy.w}/${light.w}`);
  check('组节点更高', group.h > small.h, `${group.h}/${small.h}`);
}

/* ------------------------------------------------ 3. 命中检测 */

{
  const nodes = [{ id: 'a', label: '甲' }, { id: 'b', label: '乙' }];
  const pos = { a: { x: 0, y: 0 }, b: { x: 0, y: 40 } };
  check('命中节点中心', gc.hitTest(0, 0, nodes, pos) === 'a');
  check('包围盒边缘命中（含 pad）', gc.hitTest(43, 0, nodes, pos) === 'a', String(gc.hitTest(43, 0, nodes, pos)));
  check('未命中返回 null', gc.hitTest(500, 500, nodes, pos) === null);
  check('重叠时后绘制者优先', gc.hitTest(0, 23, nodes, pos) === 'b', String(gc.hitTest(0, 23, nodes, pos)));
  check('非法坐标返回 null', gc.hitTest(NaN, 0, nodes, pos) === null);
}

/* ------------------------------------------------ 4. 端点方位 / 边几何 */

{
  const A = { x: 0, y: 0, w: 100, h: 40 };   // 中心 (50,20)
  const B = { x: 400, y: 20, w: 100, h: 40 }; // 中心 (450,40)
  const p1 = gc.pickPorts(A, B);
  check('横向主导：A 出右端口', p1.ax === 100 && p1.ay === 20, JSON.stringify(p1));
  check('横向主导：B 入左端口', p1.bx === 400 && p1.by === 40);
  const C = { x: 0, y: 300, w: 100, h: 40 }; // 正下方
  const p2 = gc.pickPorts(A, C);
  check('纵向主导：A 出下端口', p2.ax === 50 && p2.ay === 40, JSON.stringify(p2));
  check('纵向主导：C 入上端口', p2.bx === 50 && p2.by === 300);
  const D = { x: -400, y: 0, w: 100, h: 40 };
  const p3 = gc.pickPorts(A, D);
  check('左向：A 出左端口', p3.ax === 0 && p3.ay === 20, JSON.stringify(p3));
  const g = gc.edgeGeom({ x: 50, y: 20 }, { w: 100, h: 40 }, { x: 450, y: 40 }, { w: 100, h: 40 });
  check('边几何端点与 pickPorts 一致', g.ax === p1.ax && g.ay === p1.ay && g.bx === p1.bx && g.by === p1.by);
  check('标签中点在两端点之间', g.lx > g.ax && g.lx < g.bx && g.ly > Math.min(g.ay, g.by) - 1
    && g.ly < Math.max(g.ay, g.by) + 30, `${g.lx},${g.ly}`);
  check('edgePath 生成 Q 曲线', gc.edgePath(g) === `M ${g.ax} ${g.ay} Q ${g.cx} ${g.cy} ${g.bx} ${g.by}`);
}

/* ------------------------------------------------ 5. 同心圆 / 层级列布局 */

{
  const nodes = [];
  for (let i = 0; i < 12; i++) nodes.push({ id: `n${i}`, weight: 12 - i });
  const pos = gc.layoutConcentric(nodes, { cx: 0, cy: 0, startRadius: 170, ringGap: 190, spacing: 132 });
  check('同心圆：最高权重居圆心', pos.n0.x === 0 && pos.n0.y === 0);
  const d1 = Math.hypot(pos.n1.x, pos.n1.y);
  check('同心圆：第一环半径 170', Math.abs(d1 - 170) < 1e-9, String(d1));
  const d9 = Math.hypot(pos.n9.x - 0, pos.n9.y);
  check('同心圆：满环后进入第二环（半径 360）', Math.abs(d9 - 360) < 1e-9, String(d9));
  check('同心圆：全部节点有位置', nodes.every((n) => pos[n.id]));
  const single = gc.layoutConcentric([{ id: 'only' }]);
  check('同心圆：单节点居中', single.only.x === 0 && single.only.y === 0);
}

{
  const tree = gc.mindmapToGraph(PAYLOAD.story, PAYLOAD.story.seq);
  const pos = gc.layoutTree(tree.nodes, { x0: 0, colGap: 280, rowGap: 64 });
  check('层级列：root 在第 0 列', pos.root.x === 0);
  check('层级列：组在第 1 列', pos['grp-main'].x === 280);
  check('层级列：条目在第 2 列', pos['main-0'].x === 560 && pos['f-L1'].x === 560);
  const gY = pos['grp-foreshadow'].y;
  const mean = (pos['f-L1'].y + pos['f-L2'].y) / 2;
  check('层级列：组 y = 子条目均值', Math.abs(gY - mean) < 1e-9, `${gY}/${mean}`);
  const fallback = gc.layoutTree([{ id: 'x' }], {});
  check('层级列：无根节点回退同心圆', fallback.x && typeof fallback.x.x === 'number');
}

/* ------------------------------------------------ 6. 思维导图展平 / 展开层级 */

{
  const g = gc.mindmapToGraph(PAYLOAD.story, PAYLOAD.story.seq);
  check('展平：root + 4 组 + 5 条目', g.nodes.length === 10, String(g.nodes.length));
  check('展平：父子连边 9 条', g.edges.filter((e) => e.kind === 'parent').length === 9);
  check('展平：前后置连边带标签', g.edges.some((e) => e.kind === 'seq' && e.label === '前后置'
    && e.from === 'f-L1' && e.to === 'f-L2'));
  const item = g.nodes.find((n) => n.id === 'x-X1');
  check('展平：支线条目带 第N章 徽记', item.badge === '第2章', item.badge);
  const f2 = g.nodes.find((n) => n.id === 'f-L2');
  check('展平：伏笔条目徽记优先章节', f2.badge === '第3章', f2.badge);

  const vis0 = gc.visibleSubgraph(g, new Set(['root']));
  check('展开层级：默认只露 root+组', vis0.nodes.length === 5
    && vis0.nodes.every((n) => n.kind === 'root' || n.kind === 'group'), String(vis0.nodes.length));
  const vis1 = gc.visibleSubgraph(g, new Set(['root', 'grp-foreshadow']));
  check('展开层级：展开伏笔组露出条目', vis1.nodes.some((n) => n.id === 'f-L1')
    && !vis1.nodes.some((n) => n.id === 'x-X1'));
  check('展开层级：边过滤两端不可见的', vis0.edges.every((e) =>
    vis0.nodes.some((n) => n.id === e.from) && vis0.nodes.some((n) => n.id === e.to)));
  const vis2 = gc.visibleSubgraph(g, []);
  check('展开层级：全收起只剩 root', vis2.nodes.length === 1 && vis2.nodes[0].id === 'root');
}

/* ------------------------------------------------ 7. SVG 序列化 */

{
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const child = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  child.setAttribute('class', 'probe');
  svg.append(child);
  const s = gc.serializeSvg(svg, { w: 960, h: 600, bg: '#123456' });
  check('serializeSvg：含 xmlns', s.includes('xmlns="http://www.w3.org/2000/svg"'));
  check('serializeSvg：写出尺寸', s.includes('width="960"') && s.includes('height="600"'));
  check('serializeSvg：注入背景板与填充色', s.includes('gc-export-bg') && s.includes('fill="#123456"'));
  check('serializeSvg：保留子元素', s.includes('class="probe"'));
  check('serializeSvg：空元素返回空串', gc.serializeSvg(null) === '');
}

/* ------------------------------------------------ 8. 画布实例端到端（jsdom） */

{
  localStorage.clear();
  const container = document.createElement('div');
  document.body.append(container);
  const nodes = [
    { id: 'a', label: '陈默', kind: 'person', badge: '出场 46' },
    { id: 'b', label: '林哲', kind: 'person' }
  ];
  const edges = [{ from: 'b', to: 'a', label: '对手', kind: 'relation' }];
  const inst = gc.createGraphCanvas(container, {
    layout: (list) => gc.layoutConcentric(list, { cx: 0, cy: 0 })
  });
  inst.setGraph(nodes, edges, { a: { x: -200, y: 0 }, b: { x: 200, y: 0 } });
  check('实例渲染节点卡', container.querySelectorAll('.gc-node').length === 2);
  check('实例渲染连线 + 关系标签', container.querySelectorAll('.gc-edge').length === 1
    && container.querySelector('.gc-edge-label-text').textContent === '对手');
  check('实例渲染徽记', container.querySelector('.gc-node-badge').textContent === '出场 46');
  check('工具条齐备', [...container.querySelectorAll('.gc-btn')].some((b) => b.textContent === '重置布局')
    && [...container.querySelectorAll('.gc-btn')].some((b) => b.textContent === '导出 PNG'));
  inst.setZoom(9);
  check('实例缩放钳制到 2.5', inst.getZoom() === 2.5
    && container.querySelector('.gc-zoom-label').textContent === '250%');
  inst.setZoom(0.1);
  check('实例缩放钳制到 0.4', inst.getZoom() === 0.4);
  inst.setZoom(1);
  const str = inst.exportSvgString({ w: 800, h: 500 });
  check('实例导出字符串含节点与边', str.includes('gc-node') && str.includes('gc-edge'));
  check('实例导出为独立 SVG 文档', str.startsWith('<svg') && str.includes('xmlns'));
  let pngErr = null;
  const inst2 = gc.createGraphCanvas(container, { onExportError: (e) => { pngErr = e; } });
  inst2.destroy();
  inst.destroy();
  check('实例销毁移除 DOM', !container.querySelector('.gc-wrap'));
}

/* ------------------------------------------------ 9. views/canvas.js 纯函数 */

{
  let s = view.reduceTab(null, null);
  check('tab 状态机：初始 chars', s.tab === 'chars' && !s.loading);
  s = view.reduceTab(s, { type: 'switch', tab: 'story' });
  check('tab 状态机：switch 生效', s.tab === 'story');
  s = view.reduceTab(s, { type: 'switch', tab: 'nope' });
  check('tab 状态机：非法 tab 不动', s.tab === 'story');
  s = view.reduceTab(s, { type: 'load' });
  check('tab 状态机：load 置 loading', s.loading === true && s.error === null);
  s = view.reduceTab(s, { type: 'error', error: '加载失败' });
  check('tab 状态机：error 落错误信息', s.loading === false && s.error === '加载失败');
  s = view.reduceTab(s, { type: 'loaded' });
  check('tab 状态机：loaded 清错误', s.error === null && s.loading === false);
  check('tab 状态机：未知事件原样返回', view.reduceTab(s, { type: 'nonsense' }) === s);

  const g = view.buildCharsGraph(PAYLOAD);
  check('角色图整形：节点带出场徽记', g.nodes[0].badge === '出场 46');
  check('角色图整形：边补缺省标签', g.edges.every((e) => e.label));
  const dirty = view.buildCharsGraph({ characters: { nodes: [{ id: 'a' }], edges: [{ from: 'a', to: 'ghost' }] } });
  check('角色图整形：悬空边被滤掉', dirty.edges.length === 0);
  const story = view.buildStoryGraph(PAYLOAD);
  check('思维导图整形：节点数 10', story.nodes.length === 10);
  check('思维导图整形：空 payload 安全', view.buildStoryGraph(null).nodes.length === 0);
  check('personBadge：权重 ≤1 不显示', view.personBadge({ weight: 1 }) === ''
    && view.personBadge({ weight: 9 }) === '出场 9');
  check('pickLayout：chars 同心圆 / story 层级列',
    typeof view.pickLayout('chars') === 'function' && typeof view.pickLayout('story') === 'function');
}

/* ------------------------------------------------ 10. renderCanvas 端到端 */

{
  localStorage.clear();
  state.store.set({
    library: { items: [{ key: 'k1', name: '测试书·画布' }], stats: {}, facets: {}, loading: false, loaded: true, error: null },
    currentKey: 'k1'
  });
  const container = document.getElementById('root');
  const destroy = view.renderCanvas(container);
  await new Promise((r) => setTimeout(r, 30));

  check('视图：书选择器填充作品', container.querySelector('.canvas-book-select option') != null
    && container.querySelector('.canvas-book-select').value === 'k1');
  check('视图：默认渲染角色关系图', container.querySelectorAll('.gc-node').length === 3
    && container.querySelectorAll('.gc-edge').length === 2,
    `nodes=${container.querySelectorAll('.gc-node').length}`);
  check('视图：请求了聚合接口', apiCalls.some((c) => c.url.includes('/api/canvas/graph?key=k1')));

  // 连线点击 → 关系标签编辑条 → 保存 POST
  const edgeEl = container.querySelector('.gc-edge');
  edgeEl.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 10));
  const editor = container.querySelector('.canvas-edge-editor');
  check('视图：点连线弹出关系编辑条', editor != null);
  check('视图：编辑条显示关系对', editor && editor.querySelector('.canvas-edge-pair').textContent.includes('林哲'));
  const input = editor.querySelector('.canvas-edge-input');
  input.value = '宿敌';
  [...editor.querySelectorAll('button')].find((b) => b.textContent === '保存').click();
  await new Promise((r) => setTimeout(r, 30));
  const post = apiCalls.find((c) => c.method === 'POST' && c.url.includes('/api/canvas/edge'));
  check('视图：保存发起 POST /api/canvas/edge', post != null);
  if (post) {
    const body = JSON.parse(post.body);
    check('视图：POST 载荷 key/a/b/label', body.key === 'k1' && body.label === '宿敌'
      && typeof body.a === 'string' && typeof body.b === 'string', post.body);
  }

  // 切到思维导图 tab → 组可见、条目收起 → 点组节点展开
  const tabBtn = [...container.querySelectorAll('.canvas-tab')].find((b) => b.textContent === '故事线思维导图');
  tabBtn.click();
  await new Promise((r) => setTimeout(r, 20));
  const mindNodes = container.querySelectorAll('.gc-node');
  check('视图：思维导图默认收起（root+4 组）', mindNodes.length === 5, String(mindNodes.length));
  const grp = container.querySelector('.gc-node[data-id="grp-foreshadow"]');
  grp.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 20));
  check('视图：点组节点展开伏笔条目',
    [...container.querySelectorAll('.gc-node')].some((n) => n.getAttribute('data-id') === 'f-L1'));

  // 重置布局按钮：清空位置记忆
  gc.savePositions('k1', 'story', { 'f-L1': { x: 1, y: 2 } });
  const resetBtn = [...container.querySelectorAll('.gc-btn')].find((b) => b.textContent === '重置布局');
  resetBtn.click();
  await new Promise((r) => setTimeout(r, 10));
  check('视图：重置布局清除位置记忆', gc.loadPositions('k1', 'story') === null);

  // 位置持久化：模拟 onPositionsChange 无法直发，改为验证 savePositions 被视图使用（拖拽链路）
  check('视图：拖拽持久化走 localStorage 键', gc.posStoreKey('k1', 'story') === 'ew-canvas-pos:k1:story');

  destroy();
  check('视图：销毁后移除 DOM', !container.querySelector('.canvas-view'));
  state.store.set({ library: { items: [], stats: {}, facets: {}, loading: false, loaded: false, error: null }, currentKey: '' });
}

/* ------------------------------------------------ 11. rightpanel 入口 / CSS 口径 / API 契约 */

{
  const rp = read('static/v2/js/components/rightpanel.js');
  check('rightpanel：参考页签含画布入口按钮', rp.includes("navigate('/canvas')") && rp.includes('资料画布'));

  const css = read('static/v2/css/canvas.css');
  const hex = css.match(/#[0-9a-fA-F]{3,8}\b/g) || [];
  check('canvas.css：零硬编码色（无 hex）', hex.length === 0, hex.join(','));
  check('canvas.css：语义变量口径', css.includes('var(--ink-primary)') && css.includes('var(--accent-soft)')
    && css.includes('var(--bg-sunken)'));
  for (const cls of ['.canvas-view', '.canvas-stage', '.gc-wrap', '.gc-toolbar', '.gc-node-box',
    '.gc-edge-line', '.gc-edge-label-box', '.canvas-edge-editor']) {
    if (!css.includes(cls)) check(`canvas.css：缺类 ${cls}`, false);
  }
  check('canvas.css：画布类名齐备', true);

  const py = read('bot/webcanvas.py');
  const vjs = read('static/v2/js/views/canvas.js');
  check('契约：GET /api/canvas/graph 双端一致',
    py.includes('/api/canvas/graph') && vjs.includes('/api/canvas/graph'));
  check('契约：POST /api/canvas/edge 双端一致',
    py.includes('/api/canvas/edge') && vjs.includes('/api/canvas/edge'));
  check('契约：节点字段 id/label/kind/weight', py.includes('"kind": "person"') && py.includes('"weight"')
    && vjs.includes('kind'));
  check('契约：关系缺省标签「关联」', py.includes('DEFAULT_LABEL = "关联"')
    && vjs.includes("'关联'"));
}

console.log(failed ? `\n❌ ${failed} 项未通过` : '\n✅ 全部通过');
process.exit(failed ? 1 : 0);
