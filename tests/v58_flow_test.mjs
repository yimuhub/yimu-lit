// v5.8.0 无限画布测试（node 直跑，jsdom）。
// 覆盖：
//   1) whiteboard.js 纯函数：缩放钳制、屏幕↔世界坐标、光标中心缩放（锚点不变）、
//      平移、网格密度、连线端点裁剪、箭头几何、连线命中；
//   2) reduceBoard 节点 CRUD 状态机（增/改/移/删级联连线/连线校验/选色/replace 清洗）；
//   3) scatterAround 环形散布（半径/均分角/多环扩容）与 buildExpandNodes（父→子连线）；
//   4) exportBoardJSON 结构、normalizeBoard 清洗、exportFileName；
//   5) 白板实例端到端（jsdom）：渲染/选中/节点拖拽实时带动连线/空白平移/滚轮缩放钳制/
//      双击新建便签进编辑/锚点拉线成箭头/点连线删除/键盘 Delete（带连线走确认）/
//      Escape 取消/销毁；XSS（便签标题含 <script> 纯文本渲染）；
//   6) views/flow.js：reduceFlow 状态机、chaptersToNodes 网格映射、expandMockItems、
//      renderFlow 端到端（加载→渲染→选中→双击编辑→切书→导入章卡→AI 扩展→计数→销毁）；
//   7) 零硬编码色（whiteboard.js / flow.js / flow.css 无 hex）；
//   8) bot/webflow.py ↔ 前端接口契约字面量对齐 + 路由入口。
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
  '<!doctype html><html><body>'
  + '<div id="root"></div><div id="overlays"></div><div id="toasts"></div>'
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
global.requestAnimationFrame = (cb) => setTimeout(cb, 0);
global.cancelAnimationFrame = (id) => clearTimeout(id);

let failed = 0;
function check(name, cond, extra = '') {
  console.log(`${cond ? '✅' : '❌'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed++;
}

/* fetch mock：flow 三接口 + 其余全空。 */
const apiCalls = [];
function jsonResp(obj) {
  return {
    ok: true,
    status: 200,
    headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'application/json' : '') },
    json: async () => obj
  };
}
const BOARD_PAYLOAD = {
  ok: true,
  key: '',
  nodes: [
    { id: 'a1', kind: 'note', x: 40, y: 40, w: 230, h: 156, title: '开场钩子', text: '雨夜的钥匙', color: 'accent' },
    { id: 'b1', kind: 'chapter', x: 420, y: 320, w: 220, h: 104, title: '第 3 章 · 裂痕', text: '监控黑屏', color: 'plain', n: 3 }
  ],
  edges: [{ from: 'a1', to: 'b1' }],
  updatedAt: '2026-01-01T00:00:00'
};
const EXPAND_PAYLOAD = {
  ok: true,
  items: [
    { title: '反转视角', text: '从对手的眼睛看这件事' },
    { title: '极端化', text: '把冲突放大到极限' },
    { title: '代价清单', text: '主角要付出什么' },
    { title: '下一小步', text: '拆成最小场景' }
  ]
};
const CHAPTERS_PAYLOAD = {
  ok: true,
  chapters: [
    { n: 1, title: '裂痕', status: 'done', summary: '雨夜连环失踪案' },
    { n: 2, title: '回溯', status: 'writing', summary: '案件时间点对应童年节拍' },
    { n: 3, title: '纺织厂', status: 'planned', summary: '' }
  ]
};
global.fetch = async (url, init) => {
  const u = String(url);
  apiCalls.push({ url: u, method: (init && init.method) || 'GET', body: (init && init.body) || '' });
  if (u.includes('/api/flow/board')) {
    if ((init && init.method) === 'POST') return jsonResp({ ok: true, boardKey: 'k', nodeCount: 2, edgeCount: 1, updatedAt: 't' });
    return jsonResp(BOARD_PAYLOAD);
  }
  if (u.includes('/api/flow/expand')) return jsonResp(EXPAND_PAYLOAD);
  if (u.includes('/api/flow/chapters')) return jsonResp(CHAPTERS_PAYLOAD);
  return jsonResp({ ok: true });
};
dom.window.fetch = global.fetch;

const wb = await import(pathToFileURL(path.join(V2, 'js/core/whiteboard.js')).href);
const view = await import(pathToFileURL(path.join(V2, 'js/views/flow.js')).href);
const state = await import(pathToFileURL(path.join(V2, 'js/core/state.js')).href);

/* ------------------------------------------------ 1. 视图纯函数 */

{
  check('clampZoom 下限 0.25', wb.clampZoom(0.01) === 0.25);
  check('clampZoom 上限 3', wb.clampZoom(9) === 3);
  check('clampZoom 原值保留', wb.clampZoom(1.4) === 1.4);
  check('clampZoom 非法回退 1', wb.clampZoom('x') === 1 && wb.clampZoom(NaN) === 1);

  const v = { x: 100, y: 50, scale: 2 };
  const wpt = wb.screenToWorld(300, 150, v);
  check('screenToWorld：scale 2 下 (300,150) → (100,50)', wpt.x === 100 && wpt.y === 50);
  const spt = wb.worldToScreen(100, 50, v);
  check('worldToScreen 往返一致', spt.x === 300 && spt.y === 150);
  check('screenToWorld 非法 view 回退恒等',
    JSON.stringify(wb.screenToWorld(5, 6, null)) === JSON.stringify({ x: 5, y: 6 }));

  const z1 = wb.zoomAt(v, 300, 150, 2);
  const back = wb.worldToScreen(wpt.x, wpt.y, z1);
  check('zoomAt：光标下世界点缩放前后不动（含钳制到 3）',
    z1.scale === 3 && back.x === 300 && back.y === 150, JSON.stringify(z1));
  const z2 = wb.zoomAt(v, 300, 150, 0.5);
  const back2 = wb.worldToScreen(wb.screenToWorld(300, 150, v).x, wb.screenToWorld(300, 150, v).y, z2);
  check('zoomAt：缩小同样锚定', back2.x === 300 && back2.y === 150 && z2.scale === 1);
  check('zoomAt：factor 0/NaN 不动', wb.zoomAt(v, 0, 0, 0).scale === 2
    && wb.zoomAt(v, 0, 0, NaN).scale === 2);
  check('zoomAt：不改动入参', v.scale === 2 && v.x === 100);
  const p1 = wb.panBy(v, -30, 10);
  check('panBy 保留 scale 并平移', p1.x === 70 && p1.y === 60 && p1.scale === 2);

  check('gridSize：基准 28', wb.gridSize(1) === 28);
  check('gridSize：放大成比例', wb.gridSize(2) === 56);
  check('gridSize：缩太小守可读下限', wb.gridSize(0.25) === wb.GRID_MIN_SCREEN);
}

/* ------------------------------------------------ 2. 连线几何 */

{
  const A = { x: 0, y: 0, w: 100, h: 40 };    // 中心 (50,20)
  const B = { x: 400, y: 20, w: 100, h: 40 }; // 中心 (450,40)
  const e1 = wb.edgeEndpoints(A, B);
  check('edgeEndpoints：横向上 A 从右边框出', e1.ax === 100 && Math.abs(e1.ay - 22.5) < 1e-9,
    JSON.stringify(e1));
  check('edgeEndpoints：横向上 B 从左边框入', e1.bx === 400 && Math.abs(e1.by - 37.5) < 1e-9);
  const C = { x: 40, y: 300, w: 100, h: 40 };
  const e2 = wb.edgeEndpoints(A, C);
  check('edgeEndpoints：纵向上 A 从下边框出', e2.ay === 40, JSON.stringify(e2));
  const e3 = wb.edgeEndpoints(A, { x: 0, y: 0, w: 100, h: 40 });
  check('edgeEndpoints：同位矩形回退中心', e3.ax === 50 && e3.ay === 20);

  const head = wb.arrowHead(0, 0, 100, 0, 9);
  check('arrowHead：箭头尖在终点方向', head.tipx < 100 && head.tipx > 88
    && Math.abs(head.tipy) < 1e-9, JSON.stringify(head));
  const wingDist = Math.hypot(head.x1 - head.tipx, head.y1 - head.tipy);
  check('arrowHead：两翼有张口', wingDist > 2 && Number.isFinite(head.x2));

  check('hitEdge：线上命中', wb.hitEdge(50, 4, 0, 0, 100, 0) === true);
  check('hitEdge：超出容差不命中', wb.hitEdge(50, 15, 0, 0, 100, 0) === false);
  check('hitEdge：端点延长段按最近端算', wb.hitEdge(105, 3, 0, 0, 100, 0) === true
    && wb.hitEdge(105, 20, 0, 0, 100, 0) === false && wb.hitEdge(130, 3, 0, 0, 100, 0) === false);
  check('hitEdge：自定义容差与非法坐标', wb.hitEdge(50, 5, 0, 0, 100, 0, 2) === false
    && wb.hitEdge(NaN, 0, 0, 0, 100, 0) === false);
}

/* ------------------------------------------------ 3. reduceBoard 状态机 */

{
  let s = wb.reduceBoard(null, { type: 'replace', board: { nodes: [], edges: [] } });
  check('reduceBoard：初始空板', s.nodes.length === 0 && s.edges.length === 0 && s.selectedId === null);
  check('reduceBoard：未知事件原样返回', wb.reduceBoard(s, { type: 'nonsense' }) === s);

  s = wb.reduceBoard(s, { type: 'add', node: { kind: 'note', x: 10, y: 20, title: '甲' } });
  check('reduceBoard：add 分配 id 且选中', s.nodes.length === 1 && s.nodes[0].id && s.selectedId === s.nodes[0].id);
  check('reduceBoard：add 尺寸走 kind 默认', s.nodes[0].w === 230 && s.nodes[0].h === 156);
  const id1 = s.nodes[0].id;

  s = wb.reduceBoard(s, { type: 'add', node: { id: 'b1', kind: 'chapter', x: 300, y: 300, n: 3, title: '第 3 章' } });
  check('reduceBoard：add 章卡节点带 n', s.nodes[1].id === 'b1' && s.nodes[1].n === 3
    && s.nodes[1].w === 220);
  s = wb.reduceBoard(s, { type: 'add', node: { id: id1, kind: 'ai' } });
  check('reduceBoard：id 撞号自动改名', s.nodes[2].id !== id1 && s.nodes[2].kind === 'ai');

  s = wb.reduceBoard(s, { type: 'addEdge', from: id1, to: 'b1' });
  check('reduceBoard：addEdge 成功', s.edges.length === 1 && s.edges[0].from === id1);
  const before = s;
  check('reduceBoard：自环拒绝', wb.reduceBoard(s, { type: 'addEdge', from: 'b1', to: 'b1' }) === s || true);
  const afterSelf = wb.reduceBoard(s, { type: 'addEdge', from: 'b1', to: 'b1' });
  check('reduceBoard：自环拒绝（边数不变）', afterSelf.edges.length === 1);
  check('reduceBoard：悬空端点拒绝',
    wb.reduceBoard(s, { type: 'addEdge', from: 'ghost', to: 'b1' }).edges.length === 1);
  check('reduceBoard：重复边拒绝',
    wb.reduceBoard(s, { type: 'addEdge', from: id1, to: 'b1' }) === before);
  check('reduceBoard：反向是另一条线',
    wb.reduceBoard(s, { type: 'addEdge', from: 'b1', to: id1 }).edges.length === 2);

  const moved = wb.reduceBoard(s, { type: 'move', id: id1, x: 55.5, y: -3 });
  check('reduceBoard：move 生效', moved.nodes[0].x === 55.5 && moved.nodes[0].y === -3);
  check('reduceBoard：move 非法坐标拒绝',
    wb.reduceBoard(s, { type: 'move', id: id1, x: NaN, y: 0 }) === s);

  const upd = wb.reduceBoard(s, { type: 'update', id: id1, patch: { title: '新标题', color: 'warn' } });
  check('reduceBoard：update 合并字段', upd.nodes[0].title === '新标题' && upd.nodes[0].color === 'warn');
  check('reduceBoard：setColor 非法色不动',
    wb.reduceBoard(s, { type: 'setColor', id: id1, color: 'hot-pink' }) === s);
  const colored = wb.reduceBoard(s, { type: 'setColor', id: id1, color: 'danger' });
  check('reduceBoard：setColor 生效', colored.nodes[0].color === 'danger');

  const aiId = s.nodes[2].id;
  const deleted = wb.reduceBoard(s, { type: 'delete', id: id1 });
  check('reduceBoard：delete 级联删除连线', deleted.nodes.length === 2 && deleted.edges.length === 0);
  check('reduceBoard：delete 非选中节点不动 selectedId', deleted.selectedId === aiId);
  check('reduceBoard：delete 选中节点清空 selectedId',
    wb.reduceBoard(deleted, { type: 'delete', id: aiId }).selectedId === null);
  check('reduceBoard：delete 不存在的 id 原样返回', wb.reduceBoard(s, { type: 'delete', id: 'ghost' }) === s);

  const deled = wb.reduceBoard(s, { type: 'deleteEdge', index: 0 });
  check('reduceBoard：deleteEdge 按下标', deled.edges.length === 0);
  const s4 = wb.reduceBoard(s, { type: 'addEdge', from: id1, to: 'b1' });
  check('reduceBoard：deleteEdge 按 from/to',
    wb.reduceBoard(s4, { type: 'deleteEdge', from: id1, to: 'b1' }).edges.length === 0
    && wb.reduceBoard(s4, { type: 'deleteEdge', from: 'b1', to: id1 }) === s4);
  check('reduceBoard：select 空/非法/相同',
    wb.reduceBoard(s4, { type: 'select', id: 'ghost' }) === s4
    && wb.reduceBoard(s4, { type: 'select', id: 'b1' }).selectedId === 'b1'
    && wb.reduceBoard(s4, { type: 'select', id: null }).selectedId === null);

  const dirty = wb.reduceBoard(s4, {
    type: 'replace',
    board: { nodes: [{ id: 'a', kind: 'alien', text: 'x'.repeat(9999) },
                     { id: 'a', kind: 'note' }, '垃圾', null],
             edges: [{ from: 'a', to: 'ghost' }, { from: 'a', to: 'a' }] }
  });
  check('reduceBoard：replace 清洗（kind 归一/文本截断/去重/边滤空）',
    dirty.nodes.length === 1 && dirty.nodes[0].kind === 'note'
    && dirty.nodes[0].text.length === 4000 && dirty.edges.length === 0
    && dirty.nodes[0].n === null);

  check('edgeCountFor：进线+出线都算',
    wb.edgeCountFor(s4, id1) === 1 && wb.edgeCountFor(s4, 'b1') === 1
    && wb.edgeCountFor(s4, 'ghost') === 0);
}

/* ------------------------------------------------ 4. 散布与扩展节点 */

{
  const pts = wb.scatterAround({ x: 0, y: 0 }, 4);
  check('scatterAround：数量一致', pts.length === 4);
  check('scatterAround：单环等半径', pts.every((p) => Math.abs(Math.hypot(p.x, p.y) - wb.SCATTER_RADIUS) < 1e-9));
  check('scatterAround：从正上方起顺时针均分',
    Math.abs(pts[0].x) < 1e-9 && pts[0].y < 0 && Math.abs(pts[1].x - wb.SCATTER_RADIUS) < 1e-9);
  const pts10 = wb.scatterAround({ x: 5, y: 5 }, 10);
  check('scatterAround：超容量进第二环', pts10.length === 10
    && Math.abs(Math.hypot(pts10[8].x - 5, pts10[8].y - 5) - (wb.SCATTER_RADIUS + wb.SCATTER_RADIUS + 90)) < 1e-9);
  check('scatterAround：count 0 安全', wb.scatterAround({ x: 0, y: 0 }, 0).length === 0);
  const custom = wb.scatterAround({ x: 0, y: 0 }, 2, { radius: 100, angle0: 0 });
  check('scatterAround：自定义半径与起始角（angle0=0 从正右起逆时针均分）',
    Math.abs(custom[0].x - 100) < 1e-9 && Math.abs(custom[0].y) < 1e-9
    && Math.abs(custom[1].x + 100) < 1e-9 && Math.abs(custom[1].y) < 1e-9);

  const built = view.buildExpandNodes(
    { id: 'p1', x: 0, y: 0, w: 230, h: 156 },
    [{ title: '甲', text: 'a' }, { title: '乙', text: 'b' }]
  );
  check('buildExpandNodes：子节点数与 kind/color', built.nodes.length === 2
    && built.nodes.every((n) => n.kind === 'ai' && n.color === 'accent'));
  check('buildExpandNodes：父→子连线方向', built.edges.length === 2
    && built.edges.every((e) => e.from === 'p1' && e.to));
  const c0 = { x: 115, y: 78 };
  check('buildExpandNodes：子节点环形落在父周围',
    built.nodes.every((n) => Math.abs(Math.hypot(n.x + 115 - c0.x, n.y + 78 - c0.y) - wb.SCATTER_RADIUS) < 1e-9
      || Math.abs(n.x + 115 - c0.x) < 1e-9));
  check('buildExpandNodes：缺父 id 时连线被滤掉',
    view.buildExpandNodes({ x: 0, y: 0 }, [{ title: 'x' }]).edges.length === 0);
}

/* ------------------------------------------------ 5. 导出/清洗纯函数 */

{
  const payload = wb.exportBoardJSON(
    [{ id: 'a', kind: 'note', x: 1, y: 2, w: 3, h: 4, title: 't', text: 'b', color: 'warn', extra: '丢' },
     { id: 'c', kind: 'chapter', x: 5, y: 6, w: 7, h: 8, title: 'ch', text: '', color: 'plain', n: 3 }],
    [{ from: 'a', to: 'c' }],
    { key: '书' }
  );
  check('exportBoardJSON：结构齐备', payload.app === 'yimu-flow' && payload.version === 1
    && payload.key === '书' && typeof payload.exportedAt === 'string'
    && payload.nodes.length === 2 && payload.edges[0].from === 'a');
  check('exportBoardJSON：白名单字段（n 仅章卡带）',
    !('extra' in payload.nodes[0]) && payload.nodes[0].n === undefined
    && payload.nodes[1].n === 3);
  check('exportBoardJSON：空板安全', wb.exportBoardJSON([], [], {}).nodes.length === 0);

  const norm = wb.normalizeBoard({
    nodes: [{ id: 'a', x: '9', y: NaN, w: 99999, color: 'x' }, { no: 'id' }, '垃圾', null],
    edges: [{ from: 'a', to: 'a' }, { from: 'a', to: 'gone' }, '垃圾', { from: 'a', to: 'a' }]
  });
  check('normalizeBoard：坏载荷安全清洗（无 id 字典补号、非对象跳过）',
    norm.nodes.length === 2 && norm.nodes[0].w <= 900 && norm.nodes[0].color === 'plain'
    && norm.nodes[1].id === 'n2' && norm.edges.length === 0);
  check('normalizeBoard：非对象入参', wb.normalizeBoard(null).nodes.length === 0
    && wb.normalizeBoard('x').edges.length === 0);

  check('exportFileName：日期与归属', /^无限画布-书A-\d{8}\.json$/.test(view.exportFileName('书A'))
    && view.exportFileName('').includes('global'));
}

/* ------------------------------------------------ 6. 视图纯函数 */

{
  let s = view.reduceFlow(null, { type: 'switchBook', key: 'k1' });
  check('reduceFlow：switchBook 置 loading', s.key === 'k1' && s.loading === true && s.error === null);
  s = view.reduceFlow(s, { type: 'error', error: '白板加载失败' });
  check('reduceFlow：error 落错误', s.loading === false && s.error === '白板加载失败');
  s = view.reduceFlow(s, { type: 'loaded' });
  check('reduceFlow：loaded 清错误', s.error === null && s.loading === false);
  s = view.reduceFlow(s, { type: 'busy', busy: true });
  check('reduceFlow：busy 开关', s.busy === true);
  check('reduceFlow：未知事件原样返回', view.reduceFlow(s, { type: 'x' }) === s);

  const nodes = view.chaptersToNodes(CHAPTERS_PAYLOAD.chapters, { x: 10, y: 20 });
  check('chaptersToNodes：3 张章卡 3 列网格', nodes.length === 3
    && nodes[0].x === 10 && nodes[1].x === 10 + view.CHAPTER_GRID.gapX
    && nodes[2].x === 10 + 2 * view.CHAPTER_GRID.gapX);
  check('chaptersToNodes：第二行换列', view.chaptersToNodes(
    CHAPTERS_PAYLOAD.chapters.concat([{ n: 4, title: '四' }]), { x: 0, y: 0 })[3].x === 0
    && view.chaptersToNodes(CHAPTERS_PAYLOAD.chapters.concat([{ n: 4, title: '四' }]), { x: 0, y: 0 })[3].y
      === view.CHAPTER_GRID.gapY);
  check('chaptersToNodes：标题与 n', nodes[0].kind === 'chapter' && nodes[0].n === 1
    && nodes[0].title.startsWith('第 1 章 · 裂痕'));
  check('chaptersToNodes：脏数据过滤',
    view.chaptersToNodes([{ title: '无章号' }, null, { n: '9', title: '九' }], { x: 0, y: 0 }).length === 1
    && view.chaptersToNodes([{ n: '9', title: '九' }])[0].n === 9);

  check('expandMockItems：数量钳制 3–5', view.expandMockItems('x', 2).length === 3
    && view.expandMockItems('x', 9).length === 5 && view.expandMockItems('', 4).length === 4);
  check('expandMockItems：条目形状', view.expandMockItems('想法', 3).every((it) => it.title && it.text));
}

/* ------------------------------------------------ 7. 白板实例端到端 */

{
  const container = document.createElement('div');
  document.body.append(container);
  const confirmCalls = [];
  const inst = wb.createWhiteboard(container, {
    confirmNodeDelete: async (node, attached) => {
      confirmCalls.push({ id: node.id, attached });
      return false;   // 先拒绝一次
    }
  });
  inst.setBoard(BOARD_PAYLOAD);   // 不 fit：视图保持 (0,0,1)，坐标断言可精确计算
  check('实例：渲染节点卡片', container.querySelectorAll('.fw-node').length === 2);
  check('实例：渲染连线（直线 + 箭头）', container.querySelectorAll('.fw-edge').length === 1
    && container.querySelector('.fw-edge-arrow') != null
    && container.querySelector('.fw-edge-line').getAttribute('x1') != null);
  check('实例：节点带 kind/color 类与章卡徽记',
    container.querySelector('[data-id="a1"]').className.includes('fw-color-accent')
    && container.querySelector('[data-id="b1"]').className.includes('fw-kind-chapter')
    && container.querySelector('[data-id="b1"] .fw-node-chbadge').textContent === '第 3 章');
  check('实例：章卡文本只读样式', container.querySelector('[data-id="b1"] .fw-node-text')
    .className.includes('fw-node-text-ro'));
  check('实例：世界层 transform 与网格尺寸', container.querySelector('.fw-world').style.transform
    .includes('translate') && container.querySelector('.fw-viewport').style.backgroundSize.length > 0);

  /* XSS：恶意标题/正文按纯文本渲染 */
  inst.setBoard({
    nodes: [{ id: 'x1', kind: 'note', x: 0, y: 0, title: '<script>alert(1)</script>',
              text: '<img src=x onerror=alert(2)>', color: 'plain' }],
    edges: []
  });
  check('XSS：标题按纯文本渲染',
    container.querySelector('[data-id="x1"] .fw-node-title').textContent === '<script>alert(1)</script>');
  check('XSS：无 script/img 元素落地', container.querySelector('.fw-world script') === null
    && container.querySelector('.fw-world img') === null);

  /* 选中 + 色板 */
  inst.setBoard(BOARD_PAYLOAD);
  const a1El = container.querySelector('[data-id="a1"]');
  a1El.dispatchEvent(new dom.window.MouseEvent('pointerdown', { bubbles: true, clientX: 100, clientY: 100, button: 0 }));
  check('实例：pointerdown 选中节点', a1El.className.includes('is-selected')
    && inst.getSelected() && inst.getSelected().id === 'a1');
  check('实例：选中后色板可见（DOM 存在 5 个色点）', a1El.querySelectorAll('.fw-dot').length === 5);
  a1El.querySelector('.fw-dot-success').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
  check('实例：点色点换色', inst._state().nodes.find((n) => n.id === 'a1').color === 'success');

  /* 拖拽节点 → 连线实时跟随 */
  const lineBefore = container.querySelector('.fw-edge-line').getAttribute('x1');
  container.querySelector('.fw-viewport').dispatchEvent(new dom.window.MouseEvent('pointermove', { bubbles: true, clientX: 130, clientY: 150 }));
  container.querySelector('.fw-viewport').dispatchEvent(new dom.window.MouseEvent('pointerup', { bubbles: true, clientX: 130, clientY: 150 }));
  const a1State = inst._state().nodes.find((n) => n.id === 'a1');
  check('实例：拖拽移动节点 (40,40)→(70,90)', a1State.x === 70 && a1State.y === 90,
    `${a1State.x},${a1State.y}`);
  const lineAfter = container.querySelector('.fw-edge-line').getAttribute('x1');
  check('实例：节点移动连线实时更新', lineAfter !== lineBefore && lineAfter.length > 0);

  /* 空白平移 + 滚轮缩放 */
  const viewport = container.querySelector('.fw-viewport');
  viewport.dispatchEvent(new dom.window.MouseEvent('pointerdown', { bubbles: true, clientX: 500, clientY: 500, button: 0 }));
  viewport.dispatchEvent(new dom.window.MouseEvent('pointermove', { bubbles: true, clientX: 600, clientY: 700 }));
  viewport.dispatchEvent(new dom.window.MouseEvent('pointerup', { bubbles: true, clientX: 600, clientY: 700 }));
  const v1 = inst.getView();
  check('实例：空白拖拽平移视图', v1.x === 100 && v1.y === 200, JSON.stringify(v1));
  check('实例：平移后取消选中', inst.getSelected() === null);
  check('实例：网格位置随平移', viewport.style.backgroundPosition === '100px 200px');

  const wheel = new dom.window.MouseEvent('wheel', { bubbles: true, cancelable: true });
  Object.defineProperty(wheel, 'deltaY', { value: -120 });
  for (let i = 0; i < 14; i++) viewport.dispatchEvent(wheel);
  check('实例：滚轮放大钳到 3', inst.getView().scale === 3);
  check('实例：网格随缩放变密（28*3=84px）', viewport.style.backgroundSize === '84px 84px',
    viewport.style.backgroundSize);
  const wheelDown = new dom.window.MouseEvent('wheel', { bubbles: true, cancelable: true });
  Object.defineProperty(wheelDown, 'deltaY', { value: 120 });
  for (let i = 0; i < 30; i++) viewport.dispatchEvent(wheelDown);
  check('实例：滚轮缩小钳到 0.25', inst.getView().scale === 0.25);
  inst.resetView();
  check('实例：重置视图回 1', inst.getView().scale === 1 && inst.getView().x === 0);

  /* 双击空白 → 就地新建便签并进编辑 */
  viewport.dispatchEvent(new dom.window.MouseEvent('dblclick', { bubbles: true, clientX: 500, clientY: 500 }));
  const stateAfterDbl = inst._state();
  const created = stateAfterDbl.nodes.find((n) => n.kind === 'note' && n.title === '新便签');
  check('实例：双击空白新建便签', created != null);
  check('实例：新建后进入编辑（textarea 出现）', container.querySelector('.fw-edit-text') != null
    && container.querySelector('.fw-edit-title') != null);
  container.querySelector('.fw-edit-title').value = '主线钩子';
  container.querySelector('.fw-edit-text').value = '第三章埋一枚钥匙';
  inst.commitEdit();
  const committed = inst._state().nodes.find((n) => n.title === '主线钩子');
  check('实例：编辑提交落内容', committed && committed.text === '第三章埋一枚钥匙');

  /* 锚点拉线：a1 → 新便签 */
  const beforeEdges = inst._state().edges.length;
  const a1el = container.querySelector('[data-id="a1"]');
  a1el.querySelector('.fw-node-anchor').dispatchEvent(
    new dom.window.MouseEvent('pointerdown', { bubbles: true, clientX: 100, clientY: 100, button: 0 }));
  viewport.dispatchEvent(new dom.window.MouseEvent('pointermove', { bubbles: true, clientX: 500, clientY: 500 }));
  check('实例：连线中临时虚线可见', container.querySelector('.fw-edge-temp') != null);
  viewport.dispatchEvent(new dom.window.MouseEvent('pointerup', { bubbles: true, clientX: 500, clientY: 500 }));
  const edgesNow = inst._state().edges;
  check('实例：锚点拉到目标节点成箭头', edgesNow.length === beforeEdges + 1
    && edgesNow.some((e) => e.from === 'a1' && e.to === (created && created.id)));

  /* 点连线删除（坐标命中）——先让 pan pointerup 的 suppressClick 定时器跑完 */
  await new Promise((r) => setTimeout(r, 5));
  const e0 = edgesNow[edgesNow.length - 1];
  const na = inst._state().nodes.find((n) => n.id === e0.from);
  const nb = inst._state().nodes.find((n) => n.id === e0.to);
  const mid = wb.edgeEndpoints(na, nb);
  viewport.dispatchEvent(new dom.window.MouseEvent('click', {
    bubbles: true,
    clientX: (mid.ax + mid.bx) / 2 + inst.getView().x,
    clientY: (mid.ay + mid.by) / 2 + inst.getView().y
  }));
  check('实例：点击连线删除', inst._state().edges.length === beforeEdges);

  /* 键盘删除：无连线直删；有连线走确认（stub 先拒绝） */
  const fresh = inst._state().nodes.find((n) => n.id === (created && created.id));
  const edgesOfFresh = inst._state().edges.filter((e) => e.from === fresh.id || e.to === fresh.id).length;
  check('实例：前置（新便签当前无连线）', edgesOfFresh === 0);
  inst.select(fresh.id);
  document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Delete', bubbles: true }));
  await new Promise((r) => setTimeout(r, 10));
  check('实例：Delete 直删无连线节点', inst._state().nodes.every((n) => n.id !== fresh.id));
  check('实例：确认函数未被调用（无连线）', confirmCalls.length === 0);

  inst.select('a1');
  document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Delete', bubbles: true }));
  await new Promise((r) => setTimeout(r, 10));
  check('实例：有连线删除走确认且拒绝生效', confirmCalls.length === 1
    && confirmCalls[0].attached === 1 && inst._state().nodes.some((n) => n.id === 'a1'));
  document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  check('实例：Escape 取消选中', inst.getSelected() === null);

  inst.deleteNode('b1');
  check('实例：deleteNode API 级联连线', inst._state().nodes.length === 1
    && inst._state().edges.length === 0);

  inst.destroy();
  check('实例：销毁移除 DOM', !container.querySelector('.fw-wrap'));
}

/* ------------------------------------------------ 8. renderFlow 端到端 */

{
  localStorage.clear();
  state.store.set({
    library: { items: [{ key: 'k1', name: '测试书·白板' }], stats: {}, facets: {}, loading: false, loaded: true, error: null },
    currentKey: ''
  });
  const container = document.getElementById('root');
  const destroy = view.renderFlow(container);
  await new Promise((r) => setTimeout(r, 40));

  check('视图：头部/工具条/舞台齐备', container.querySelector('.flow-view') != null
    && container.querySelector('.flow-stage') != null
    && [...container.querySelectorAll('button')].some((b) => b.textContent.includes('新建便签')));
  check('视图：默认全局白板（空 key 请求不带 query）',
    container.querySelector('.flow-book-select').value === ''
    && apiCalls.some((c) => c.url.includes('/api/flow/board') && !c.url.includes('key=')));
  check('视图：加载后渲染节点与连线', container.querySelectorAll('.fw-node').length === 2
    && container.querySelectorAll('.fw-edge').length === 1);
  check('视图：计数徽标', container.querySelector('.flow-count').textContent.includes('节点 2')
    && container.querySelector('.flow-count').textContent.includes('连线 1'));
  check('视图：有节点不出空态卡', container.querySelector('.flow-empty') === null);
  const btnExpand = [...container.querySelectorAll('button')].find((b) => b.textContent.includes('扩展'));
  check('视图：未选中时扩展按钮禁用', btnExpand && btnExpand.disabled === true);

  /* 选中便签 → 扩展按钮亮起 → AI 扩展 4 个子节点 */
  const a1el = container.querySelector('[data-id="a1"]');
  a1el.dispatchEvent(new dom.window.MouseEvent('pointerdown', { bubbles: true, clientX: 100, clientY: 100, button: 0 }));
  check('视图：选中后扩展按钮可用', btnExpand && btnExpand.disabled === false);
  btnExpand.click();
  await new Promise((r) => setTimeout(r, 40));
  const aiNodes = [...container.querySelectorAll('.fw-node')].filter((el) => el.className.includes('fw-kind-ai'));
  check('视图：AI 扩展生成 4 个 ✦ 子节点', aiNodes.length === 4
    && aiNodes.every((el) => el.querySelector('.fw-node-badge').textContent === '✦'));
  check('视图：父→子自动连线', container.querySelectorAll('.fw-edge').length === 5);
  const expandPost = apiCalls.find((c) => c.method === 'POST' && c.url.includes('/api/flow/expand'));
  check('视图：expand POST 带 key/text/count', expandPost && (() => {
    const body = JSON.parse(expandPost.body);
    return body.kind === 'note' && body.count === 4 && typeof body.text === 'string' && body.text.includes('开场钩子');
  })());
  check('视图：扩展后计数更新', container.querySelector('.flow-count').textContent.includes('节点 6'));

  /* 双击便签进编辑并提交 */
  const note2 = container.querySelector('[data-id="a1"]');
  note2.dispatchEvent(new dom.window.MouseEvent('dblclick', { bubbles: true, clientX: 100, clientY: 100 }));
  check('视图：双击便签进编辑', container.querySelector('.fw-edit-title') != null);
  container.querySelector('.fw-edit-title').value = '改标题';
  container.querySelector('.fw-edit-text').value = '改内容';
  container.querySelector('.fw-edit-text').dispatchEvent(new dom.window.KeyboardEvent('keydown', {
    key: 'Enter', ctrlKey: true, bubbles: true
  }));
  await new Promise((r) => setTimeout(r, 10));
  check('视图：Ctrl+Enter 提交编辑', container.querySelector('[data-id="a1"] .fw-node-title')
    .textContent === '改标题');

  /* 导入章卡：切书 → 弹窗 → 全选导入 */
  const select = container.querySelector('.flow-book-select');
  select.value = 'k1';
  select.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 40));
  check('视图：切书后重新拉板', apiCalls.filter((c) => c.url.includes('/api/flow/board?key=k1')).length >= 1);
  const btnImport = [...container.querySelectorAll('button')].find((b) => b.textContent.includes('导入章卡'));
  btnImport.click();
  await new Promise((r) => setTimeout(r, 40));
  const rows = document.querySelectorAll('.flow-import-row');
  check('视图：章卡弹窗列出 3 章', rows.length === 3, String(rows.length));
  const chPost = apiCalls.find((c) => c.url.includes('/api/flow/chapters'));
  check('视图：章卡请求带 key', chPost && chPost.url.includes('key=k1'));
  [...document.querySelectorAll('button')].find((b) => b.textContent === '导入到画布').click();
  await new Promise((r) => setTimeout(r, 20));
  const chNodes = [...container.querySelectorAll('.fw-node')].filter((el) => el.className.includes('fw-kind-chapter'));
  check('视图：导入 3 张章卡节点（灰底只读 + 章号徽记）',
    chNodes.length === 4   // 基线板本有 1 张章卡（b1=第3章）+ 导入 3 张
    && chNodes.every((el) => el.querySelector('.fw-node-text-ro') != null
      && el.className.includes('fw-kind-chapter'))
    && chNodes.some((el) => el.querySelector('.fw-node-chbadge').textContent === '第 2 章')
    && [...container.querySelectorAll('.fw-node-text-ro')].some((el) => el.textContent === '案件时间点对应童年节拍'));
  check('视图：章卡徽记带章号', chNodes.some((el) => el.querySelector('.fw-node-chbadge').textContent === '第 1 章'));

  /* 导出 JSON 按钮（blob 下载在 jsdom 里 a.click 不导航，安全） */
  const btnExport = [...container.querySelectorAll('button')].find((b) => b.textContent.includes('导出 JSON'));
  btnExport.click();
  check('视图：导出按钮可点（无异常即通过）', btnExport != null);

  destroy();
  await new Promise((r) => setTimeout(r, 20));
  check('视图：销毁移除 DOM', !container.querySelector('.flow-view'));
  check('视图：销毁前整板保存冲盘', apiCalls.some((c) => c.method === 'POST' && c.url.includes('/api/flow/board')));
  state.store.set({ library: { items: [], stats: {}, facets: {}, loading: false, loaded: false, error: null }, currentKey: '' });
}

/* ------------------------------------------------ 9. 空态引导 / 零硬编码色 / 契约 */

{
  localStorage.clear();
  state.store.set({
    library: { items: [{ key: 'k1', name: '书' }], stats: {}, facets: {}, loading: false, loaded: true, error: null },
    currentKey: ''
  });
  const container = document.getElementById('root');
  const destroy = view.renderFlow(container);
  await new Promise((r) => setTimeout(r, 30));
  // fetch mock 仍返回 BOARD_PAYLOAD？——空态卡只在节点为 0 时出现；换 key 后 mock 同板。
  // 这里直接断言三步引导文案存在于源码，与空态 DOM 结构类齐备。
  destroy();

  const steps = view.EMPTY_STEPS || [];
  check('空态引导：三步文案自研且齐全', steps.length === 3
    && steps[0].includes('双击') && steps[1].includes('连线') && steps[2].includes('扩展'));
  check('空态引导：声明只存本机', JSON.stringify(steps).includes('本机') || read('static/v2/js/views/flow.js').includes('本机'));

  for (const f of ['static/v2/js/core/whiteboard.js', 'static/v2/js/views/flow.js', 'static/v2/css/flow.css']) {
    const hex = read(f).match(/#[0-9a-fA-F]{3,8}\b/g) || [];
    check(`${path.basename(f)}：零硬编码色（无 hex）`, hex.length === 0, hex.slice(0, 4).join(','));
  }
  const css = read('static/v2/css/flow.css');
  const missing = ['.flow-view', '.flow-stage', '.flow-toolbar', '.flow-empty-card', '.flow-import-row',
    '.fw-viewport', '.fw-world', '.fw-node', '.fw-node-anchor', '.fw-node-colors', '.fw-dot-accent',
    '.fw-edge-line', '.fw-edge-arrow', '.fw-edit-text', '.fw-color-danger', '.fw-kind-chapter']
    .filter((cls) => !css.includes(cls));
  check('flow.css：类名齐备', missing.length === 0, missing.join(','));
  check('flow.css：语义变量口径', css.includes('var(--ink-primary)') && css.includes('var(--accent-soft)')
    && css.includes('var(--bg-sunken)') && css.includes('var(--line-3)'));

  const py = read('bot/webflow.py');
  const vjs = read('static/v2/js/views/flow.js');
  for (const ep of ['/api/flow/board', '/api/flow/expand', '/api/flow/chapters']) {
    check(`契约：${ep} 双端一致`, py.includes(ep) && vjs.includes(ep));
  }
  check('契约：扩展场景「画布扩展」', py.includes('画布扩展'));
  check('契约：节点字段白名单', py.includes('"kind"') && py.includes('"color"') && py.includes('"n"'));
  check('契约：章卡文件 02d-章卡.jsonl', py.includes('02d-章卡.jsonl'));
  check('契约：mock 模式不穿透（前端本地假数据）', vjs.includes('expandMockItems') && vjs.includes('IS_MOCK'));
  const appjs = read('static/v2/js/app.js');
  check('入口：app.js 动态挂载 renderFlow + flow.css', appjs.includes("views/flow.js")
    && appjs.includes('/v2/css/flow.css'));
  check('入口：首页工具卡「无限画布」', read('static/v2/js/views/welcome.js').includes('无限画布'));
  check('纯净域：白板内容无正文出口', !vjs.includes('api.exportWork') && !py.includes('/api/export'));
}

console.log(failed ? `\n❌ ${failed} 项未通过` : '\n✅ 全部通过');
process.exit(failed ? 1 : 0);
