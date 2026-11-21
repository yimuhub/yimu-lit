/* 无限画布白板核心（v5.8.0，#/flow 创作工作流白板）——自研 DOM 白板，零依赖。
 *
 * 与 v5.6.0 资料画布（graphcanvas.js，SVG 数据图谱）定位不同：这里是自由白板——
 *   · 无限画布：外层视口 + 内层 `transform: translate(x,y) scale(s)` 世界层；
 *     空白拖 = 平移、滚轮/Ctrl+滚轮 = 以光标为中心缩放（0.25–3）、
 *     双击空白 = 就地新建便签；网格点阵背景随缩放密度自适应；
 *   · 节点 = DOM 卡片（便签/章卡引用/AI 节点三种），pointer events 拖拽
 *     （拖拽中天然禁用平移：平移只在空白处起手）；五种语义色 + 选中描边；
 *   · 连线 = 节点边缘锚点拖出 → 落到另一节点成有向箭头（a→b）；点击连线删除；
 *     节点移动时连线实时重算（直线 + 箭头，矩形边缘裁剪）；
 *   · 持久化 = 全量 {nodes, edges} 由视图层防抖 POST（本文件只管状态与 DOM）。
 *
 * 纯函数全部导出（tests/v58_flow_test.mjs 直测）：坐标换算、光标中心缩放、
 * 矩形边缘裁剪端点、连线命中、节点 CRUD 状态机（reduceBoard）、扩展子节点
 * 环形散布、导出结构、网格密度。所有颜色走 CSS 类（flow.css 读 tokens 语义
 * 变量），JS 里零硬编码色；节点文本一律 textContent（防 XSS）。
 */

export const ZOOM_MIN = 0.25;
export const ZOOM_MAX = 3;
export const ZOOM_STEP = 1.12;
export const GRID = 28;              // 网格点阵世界间距（px）
export const GRID_MIN_SCREEN = 10;   // 缩到很小后点阵不再变密（可读下限）
export const EDGE_HIT_TOL = 9;       // 连线点击命中容差（px）
export const SCATTER_RADIUS = 190;   // AI 扩展子节点环形半径
export const SCATTER_START_ANGLE = -Math.PI / 2;

export const NODE_KINDS = ['note', 'chapter', 'ai'];
export const NODE_COLORS = ['plain', 'accent', 'success', 'warn', 'danger'];

const NODE_FIELDS = ['id', 'kind', 'x', 'y', 'w', 'h', 'title', 'text', 'color', 'n'];

/* -------------------------------------------------------------- 纯函数：视图 */

export function clampZoom(z) {
  const n = Number(z);
  if (!Number.isFinite(n)) return 1;
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, n));
}

/** 屏幕（视口内）坐标 → 世界坐标。view = {x, y, scale}。 */
export function screenToWorld(sx, sy, view) {
  const v = view || { x: 0, y: 0, scale: 1 };
  const s = clampZoom(v.scale);
  return { x: (Number(sx || 0) - (v.x || 0)) / s, y: (Number(sy || 0) - (v.y || 0)) / s };
}

/** 世界坐标 → 屏幕坐标。 */
export function worldToScreen(wx, wy, view) {
  const v = view || { x: 0, y: 0, scale: 1 };
  const s = clampZoom(v.scale);
  return { x: Number(wx || 0) * s + (v.x || 0), y: Number(wy || 0) * s + (v.y || 0) };
}

/**
 * 以屏幕点 (sx, sy) 为中心缩放：光标下的世界点缩放前后保持不动。
 * 返回新 view（不改动入参）。
 */
export function zoomAt(view, sx, sy, factor) {
  const v = view || { x: 0, y: 0, scale: 1 };
  const s0 = clampZoom(v.scale);
  const s1 = clampZoom(s0 * (Number(factor) || 1));
  if (s1 === s0) return { ...v, scale: s1 };
  const px = Number(sx) || 0;
  const py = Number(sy) || 0;
  // wx = (px - x)/s0 固定 → x' = px - wx*s1
  return {
    scale: s1,
    x: px - ((px - (v.x || 0)) / s0) * s1,
    y: py - ((py - (v.y || 0)) / s0) * s1
  };
}

export function panBy(view, dx, dy) {
  const v = view || { x: 0, y: 0, scale: 1 };
  return { scale: clampZoom(v.scale), x: (v.x || 0) + Number(dx || 0), y: (v.y || 0) + Number(dy || 0) };
}

/** 网格点阵屏幕间距：随缩放走，缩太小则守住可读下限。 */
export function gridSize(scale) {
  const s = clampZoom(scale);
  return Math.max(GRID_MIN_SCREEN, Math.round(GRID * s * 100) / 100);
}

/* -------------------------------------------------------------- 纯函数：连线几何 */

function clampToRect(cx, cy, rx, ry, w, h) {
  /* 中心 (rx+w/2, ry+h/2)；返回从矩形中心到 (cx,cy) 方向与边框的交点。 */
  const mx = rx + w / 2;
  const my = ry + h / 2;
  const dx = cx - mx;
  const dy = cy - my;
  if (dx === 0 && dy === 0) return { x: mx, y: my };
  const hw = Math.max(w / 2, 1e-6);
  const hh = Math.max(h / 2, 1e-6);
  const sx = dx !== 0 ? hw / Math.abs(dx) : Infinity;
  const sy = dy !== 0 ? hh / Math.abs(dy) : Infinity;
  const t = Math.min(sx, sy);
  return { x: mx + dx * t, y: my + dy * t };
}

/**
 * 连线端点：a、b 两矩形（{x,y,w,h} 左上角制）中心连线，各自裁剪到矩形边框。
 * 返回 {ax, ay, bx, by}；两点重合时回退到各自中心。
 */
export function edgeEndpoints(aRect, bRect) {
  const a = aRect || { x: 0, y: 0, w: 0, h: 0 };
  const b = bRect || { x: 0, y: 0, w: 0, h: 0 };
  const acx = a.x + a.w / 2;
  const acy = a.y + a.h / 2;
  const bcx = b.x + b.w / 2;
  const bcy = b.y + b.h / 2;
  const p = clampToRect(bcx, bcy, a.x, a.y, a.w, a.h);
  const q = clampToRect(acx, acy, b.x, b.y, b.w, b.h);
  return { ax: p.x, ay: p.y, bx: q.x, by: q.y };
}

/** 箭头三角：tip 为终点，沿 (tip - from) 方向张开的等腰三角。 */
export function arrowHead(ax, ay, bx, by, size = 9) {
  const dx = bx - ax;
  const dy = by - ay;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;
  const back = 0.62 * size;
  const side = 0.42 * size;
  // 翼点：终点往回退一点再向两侧张开（箭头不要盖住终点）
  const tipx = bx - ux * 1.5;
  const tipy = by - uy * 1.5;
  const basex = tipx - ux * back;
  const basey = tipy - uy * back;
  return {
    tipx, tipy,
    x1: basex + -uy * side, y1: basey + ux * side,
    x2: basex - -uy * side, y2: basey - ux * side
  };
}

/** 点到线段距离 ≤ tol（命中检测）。 */
export function hitEdge(px, py, ax, ay, bx, by, tol = EDGE_HIT_TOL) {
  const x = Number(px);
  const y = Number(py);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return false;
  const dx = bx - ax;
  const dy = by - ay;
  const lenSq = dx * dx + dy * dy;
  let t = lenSq ? ((x - ax) * dx + (y - ay) * dy) / lenSq : 0;
  t = Math.max(0, Math.min(1, t));
  const cx = ax + dx * t;
  const cy = ay + dy * t;
  return Math.hypot(x - cx, y - cy) <= tol;
}

/* -------------------------------------------------------------- 纯函数：状态机 */

let uidSeq = 0;
export function uid(prefix = 'fw') {
  uidSeq += 1;
  return `${prefix}-${Date.now().toString(36)}-${uidSeq.toString(36)}`;
}

export function defaultSize(kind) {
  if (kind === 'chapter') return { w: 220, h: 104 };
  return { w: 230, h: 156 };
}

/** 任意来源的节点 → 规范节点（与 bot/webflow.py sanitize_board 同口径）。 */
export function normalizeNode(raw, i = 0) {
  const r = raw && typeof raw === 'object' ? raw : {};
  const id = String(r.id || `n${i + 1}`).slice(0, 64) || `n${i + 1}`;
  const kind = NODE_KINDS.includes(r.kind) ? r.kind : 'note';
  const size = defaultSize(kind);
  const num = (v, d) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : d;
  };
  const nRaw = r.n;
  const nNum = nRaw == null || nRaw === '' ? NaN : Math.floor(Number(nRaw));
  const n = Number.isFinite(nNum) && nNum >= 0 ? nNum : null;
  return {
    id,
    kind,
    x: Math.round(num(r.x, 0) * 10) / 10,
    y: Math.round(num(r.y, 0) * 10) / 10,
    w: Math.max(120, Math.min(900, num(r.w, size.w))),
    h: Math.max(72, Math.min(1600, num(r.h, size.h))),
    title: String(r.title || '').slice(0, 80),
    text: String(r.text || '').slice(0, 4000),
    color: NODE_COLORS.includes(r.color) ? r.color : 'plain',
    n
  };
}

export function normalizeBoard(board) {
  const b = board && typeof board === 'object' ? board : {};
  const nodes = [];
  const seenIds = new Set();
  for (const raw of (Array.isArray(b.nodes) ? b.nodes : [])) {
    if (!raw || typeof raw !== 'object' || nodes.length >= 500) continue;
    const n = normalizeNode(raw, nodes.length);
    if (nodes.some((x) => x.id === n.id)) continue;   // 重复 id 丢弃后至（与后端同口径）
    nodes.push(n);
  }
  const ids = new Set(nodes.map((n) => n.id));
  const seen = new Set();
  const edges = [];
  for (const e of (Array.isArray(b.edges) ? b.edges : []).slice(0, 1200)) {
    if (!e || typeof e !== 'object') continue;
    const from = String(e.from || '').slice(0, 64);
    const to = String(e.to || '').slice(0, 64);
    if (!from || !to || from === to || !ids.has(from) || !ids.has(to)) continue;
    const k = `${from}\u001f${to}`;
    if (seen.has(k)) continue;
    seen.add(k);
    edges.push({ from, to });
  }
  return { nodes, edges };
}

export function edgeKeyOf(e) {
  return `${(e && e.from) || ''}\u001f${(e && e.to) || ''}`;
}

/**
 * 节点 CRUD 状态机。state = {nodes, edges, selectedId}；返回新 state（无变化返回原引用）。
 * 事件：add / update / move / delete / addEdge / deleteEdge / select / setColor / replace。
 */
export function reduceBoard(state, event) {
  const s = state || { nodes: [], edges: [], selectedId: null };
  const ev = event || {};
  switch (ev.type) {
    case 'replace': {
      const b = normalizeBoard(ev.board);
      const sel = ev.keepSelection && s.selectedId && b.nodes.some((n) => n.id === s.selectedId)
        ? s.selectedId : null;
      return { nodes: b.nodes, edges: b.edges, selectedId: sel };
    }
    case 'add': {
      const node = normalizeNode(ev.node, s.nodes.length);
      if (s.nodes.some((n) => n.id === node.id)) {
        node.id = uid('fw');
      }
      return { ...s, nodes: [...s.nodes, node], selectedId: ev.select === false ? s.selectedId : node.id };
    }
    case 'update': {
      const id = String(ev.id || '');
      const i = s.nodes.findIndex((n) => n.id === id);
      if (i < 0) return s;
      const patch = ev.patch && typeof ev.patch === 'object' ? ev.patch : {};
      const cur = s.nodes[i];
      const merged = { ...cur };
      for (const f of NODE_FIELDS) {
        if (f in patch) merged[f] = patch[f];
      }
      const node = normalizeNode(merged, i);
      const nodes = s.nodes.slice();
      nodes[i] = node;
      return { ...s, nodes };
    }
    case 'move': {
      const id = String(ev.id || '');
      const i = s.nodes.findIndex((n) => n.id === id);
      if (i < 0 || !Number.isFinite(Number(ev.x)) || !Number.isFinite(Number(ev.y))) return s;
      const cur = s.nodes[i];
      if (cur.x === ev.x && cur.y === ev.y) return s;
      const node = { ...cur, x: Number(ev.x), y: Number(ev.y) };
      const nodes = s.nodes.slice();
      nodes[i] = node;
      return { ...s, nodes };
    }
    case 'delete': {
      const id = String(ev.id || '');
      if (!s.nodes.some((n) => n.id === id)) return s;
      return {
        nodes: s.nodes.filter((n) => n.id !== id),
        edges: s.edges.filter((e) => e.from !== id && e.to !== id),
        selectedId: s.selectedId === id ? null : s.selectedId
      };
    }
    case 'addEdge': {
      const from = String(ev.from || '');
      const to = String(ev.to || '');
      if (!from || !to || from === to) return s;
      if (!s.nodes.some((n) => n.id === from) || !s.nodes.some((n) => n.id === to)) return s;
      const k = edgeKeyOf({ from, to });
      if (s.edges.some((e) => edgeKeyOf(e) === k)) return s;
      return { ...s, edges: [...s.edges, { from, to }] };
    }
    case 'deleteEdge': {
      let idx = -1;
      if (ev.index != null && Number.isInteger(Number(ev.index))) {
        idx = Number(ev.index);
        if (idx < 0 || idx >= s.edges.length) return s;
      } else {
        const k = edgeKeyOf({ from: ev.from, to: ev.to });
        idx = s.edges.findIndex((e) => edgeKeyOf(e) === k);
        if (idx < 0) return s;
      }
      return { ...s, edges: s.edges.filter((_, i) => i !== idx) };
    }
    case 'select': {
      const id = ev.id == null ? null : String(ev.id);
      if (id && !s.nodes.some((n) => n.id === id)) return s;
      if ((s.selectedId || null) === id) return s;
      return { ...s, selectedId: id };
    }
    case 'setColor': {
      const id = String(ev.id || '');
      if (!NODE_COLORS.includes(ev.color)) return s;
      return reduceBoard(s, { type: 'update', id, patch: { color: ev.color } });
    }
    default:
      return s;
  }
}

/** 附加在节点上的连线数（删除确认用）。 */
export function edgeCountFor(state, id) {
  return (state.edges || []).filter((e) => e.from === id || e.to === id).length;
}

/**
 * AI 扩展子节点环形散布：origin 为父节点中心（世界坐标），count 个子节点
 * 从 startAngle 起顺时针均分一圈；超过单环容量（8）后向外扩一环。
 * 返回 [{x, y}]（节点中心，世界坐标）。angle0 可注入避免测试脆断。
 */
export function scatterAround(origin, count, opts = {}) {
  const n = Math.max(0, Math.floor(Number(count) || 0));
  const out = [];
  if (!n) return out;
  const r0 = Math.max(60, Number(opts.radius) || SCATTER_RADIUS);
  const ringGap = Math.max(80, Number(opts.ringGap) || (r0 + 90));
  const cap = Math.max(1, Math.floor(Number(opts.ringCap) || 8));
  const start = Number.isFinite(opts.angle0) ? opts.angle0 : SCATTER_START_ANGLE;
  const ox = Number(origin && origin.x) || 0;
  const oy = Number(origin && origin.y) || 0;
  let placed = 0;
  for (let ring = 0; placed < n; ring++) {
    const r = ring === 0 ? r0 : r0 + ring * ringGap;
    const inRing = Math.min(cap, n - placed);
    for (let i = 0; i < inRing; i++) {
      const angle = start + (i / inRing) * 2 * Math.PI + (ring % 2) * (Math.PI / inRing);
      out.push({ x: ox + r * Math.cos(angle), y: oy + r * Math.sin(angle) });
      placed++;
    }
  }
  return out;
}

/** 导出 JSON 结构（前端「导出」按钮下载的草稿包）。 */
export function exportBoardJSON(nodes, edges, meta = {}) {
  return {
    app: 'yimu-flow',
    version: 1,
    exportedAt: new Date().toISOString(),
    key: String(meta.key || ''),
    nodes: (nodes || []).map((n) => ({
      id: n.id, kind: n.kind, x: n.x, y: n.y, w: n.w, h: n.h,
      title: n.title, text: n.text, color: n.color, ...(n.n != null ? { n: n.n } : {})
    })),
    edges: (edges || []).map((e) => ({ from: e.from, to: e.to }))
  };
}

/* -------------------------------------------------------------- 实例 */

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function svgEl(tag, attrs) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    node.setAttribute(k, String(v));
  }
  return node;
}

const SVG_NS = 'http://www.w3.org/2000/svg';

/**
 * 创建白板实例。opts：
 *   onChange({persist})           状态变化（persist=true 时视图层应防抖保存）
 *   onSelectionChange(node|null)  选中变化
 *   onZoomChange(scale)
 *   onChapterOpen(node)           双击章卡引用 → 视图层跳 bench
 *   confirmNodeDelete(node, edgesAttached) → boolean|Promise<boolean>（有连线时的删除确认）
 *   onDeleteError(err)
 */
export function createWhiteboard(container, opts = {}) {
  let st = { nodes: [], edges: [], selectedId: null };
  const view = { x: 0, y: 0, scale: 1 };
  let drag = null;        // {id, startWorld, origin, moved}
  let pan = null;         // {startScreen, origin:{x,y}, moved}
  let connecting = null;  // {fromId}
  let editing = null;     // {id, originTitle, originText}
  let destroyed = false;

  const wrap = el('div', 'fw-wrap');
  const viewport = el('div', 'fw-viewport');
  const world = el('div', 'fw-world');
  const edgeSvg = svgEl('svg', { class: 'fw-edges-svg' });
  const edgeLayer = svgEl('g', { class: 'fw-edges' });
  const nodesLayer = el('div', 'fw-nodes');
  edgeSvg.append(edgeLayer);
  world.append(edgeSvg, nodesLayer);
  viewport.append(world);
  wrap.append(viewport);
  container.append(wrap);

  const nodeEls = new Map();
  const edgeEls = [];   // [{g, hit, line, arrow, edge}]

  /* ------------------------------------------------------------ 坐标 */

  function screenPoint(event) {
    const rect = viewport.getBoundingClientRect ? viewport.getBoundingClientRect() : null;
    return {
      x: (event.clientX || 0) - ((rect && rect.left) || 0),
      y: (event.clientY || 0) - ((rect && rect.top) || 0)
    };
  }

  function toWorld(event) {
    const s = screenPoint(event);
    return screenToWorld(s.x, s.y, view);
  }

  function centerWorld() {
    const rect = viewport.getBoundingClientRect ? viewport.getBoundingClientRect() : null;
    const w = (rect && rect.width) || container.clientWidth || 960;
    const h = (rect && rect.height) || container.clientHeight || 600;
    return screenToWorld(w / 2, h / 2, view);
  }

  /* ------------------------------------------------------------ 渲染 */

  function applyTransform() {
    world.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
    const size = gridSize(view.scale);
    viewport.style.backgroundSize = `${size}px ${size}px`;
    viewport.style.backgroundPosition = `${view.x}px ${view.y}px`;
    if (opts.onZoomChange) opts.onZoomChange(view.scale);
  }

  function nodeById(id) {
    return st.nodes.find((n) => n.id === id) || null;
  }

  function nodeRect(n) {
    return { x: n.x, y: n.y, w: n.w, h: n.h };
  }

  function positionNode(node) {
    const eln = nodeEls.get(node.id);
    if (eln) eln.style.transform = `translate(${node.x}px, ${node.y}px)`;
  }

  function renderNode(node) {
    const eln = el('div', `fw-node fw-kind-${node.kind} fw-color-${node.color}`);
    eln.dataset.id = node.id;
    eln.dataset.kind = node.kind;
    eln.style.width = `${node.w}px`;
    eln.style.height = `${node.h}px`;
    eln.style.transform = `translate(${node.x}px, ${node.y}px)`;

    const head = el('div', 'fw-node-head');
    if (node.kind === 'ai') {
      const badge = el('span', 'fw-node-badge', '✦');
      badge.title = 'AI 扩展节点';
      head.append(badge);
    }
    if (node.kind === 'chapter') {
      const badge = el('span', 'fw-node-badge fw-node-chbadge',
        node.n != null ? `第 ${node.n} 章` : '章卡');
      head.append(badge);
    }
    const title = el('div', 'fw-node-title', node.title || (node.kind === 'note' ? '新便签' : ''));
    head.append(title);
    eln.append(head);

    const body = el('div', 'fw-node-text', node.text || '');
    if (node.kind === 'chapter') body.classList.add('fw-node-text-ro');
    eln.append(body);

    /* 色板：选中时显示（flow.css 控制显隐） */
    const colors = el('div', 'fw-node-colors');
    for (const c of NODE_COLORS) {
      const dot = el('button', `fw-dot fw-dot-${c}`);
      dot.type = 'button';
      dot.dataset.color = c;
      dot.title = '节点颜色';
      dot.setAttribute('aria-label', `节点颜色 ${c}`);
      colors.append(dot);
    }
    eln.append(colors);

    const anchor = el('div', 'fw-node-anchor');
    anchor.title = '拖到我身上连线';
    eln.append(anchor);
    return eln;
  }

  function renderEdges() {
    edgeLayer.textContent = '';
    edgeEls.length = 0;
    const byId = new Map(st.nodes.map((n) => [n.id, nodeRect(n)]));
    for (const e of st.edges) {
      const a = byId.get(e.from);
      const b = byId.get(e.to);
      if (!a || !b) continue;
      const g = svgEl('g', { class: 'fw-edge' });
      const hit = svgEl('line', { class: 'fw-edge-hit' });
      const line = svgEl('line', { class: 'fw-edge-line' });
      const arrow = svgEl('path', { class: 'fw-edge-arrow' });
      g.append(hit, line, arrow);
      g.dataset.from = e.from;
      g.dataset.to = e.to;
      edgeLayer.append(g);
      edgeEls.push({ g, hit, line, arrow, edge: e });
    }
    updateEdgeGeometry();
  }

  function updateEdgeGeometry() {
    const byId = new Map(st.nodes.map((n) => [n.id, nodeRect(n)]));
    for (const item of edgeEls) {
      const a = byId.get(item.edge.from);
      const b = byId.get(item.edge.to);
      if (!a || !b) continue;
      const { ax, ay, bx, by } = edgeEndpoints(a, b);
      for (const ln of [item.hit, item.line]) {
        ln.setAttribute('x1', String(ax));
        ln.setAttribute('y1', String(ay));
        ln.setAttribute('x2', String(bx));
        ln.setAttribute('y2', String(by));
      }
      const head = arrowHead(ax, ay, bx, by);
      item.arrow.setAttribute('d',
        `M ${head.tipx} ${head.tipy} L ${head.x1} ${head.y1} L ${head.x2} ${head.y2} Z`);
    }
  }

  function paintSelection() {
    for (const [id, eln] of nodeEls) {
      eln.classList.toggle('is-selected', id === st.selectedId);
    }
  }

  function renderAll() {
    nodesLayer.textContent = '';
    nodeEls.clear();
    for (const n of st.nodes) {
      const eln = renderNode(n);
      nodeEls.set(n.id, eln);
      nodesLayer.append(eln);
    }
    renderEdges();
    paintSelection();
    applyTransform();
  }

  function dispatch(event, { rerender = true } = {}) {
    const next = reduceBoard(st, event);
    if (next === st) return false;
    st = next;
    if (rerender) renderAll();
    if (!event || event.type !== 'select') emitChange(true, event && event.type);
    return true;
  }

  function emitChange(persist, reason) {
    if (opts.onChange) opts.onChange({ persist: !!persist, reason: reason || '' });
  }

  /* ------------------------------------------------------------ 编辑 */

  function beginEdit(id) {
    if (editing) commitEdit();
    const node = nodeById(id);
    const eln = nodeEls.get(id);
    if (!node || !eln || node.kind === 'chapter') return;
    editing = { id, originTitle: node.title, originText: node.text };
    eln.classList.add('is-editing');
    const box = el('div', 'fw-node-editor');
    const titleInput = el('input', 'fw-edit-title');
    titleInput.value = node.title || '';
    titleInput.placeholder = '标题';
    titleInput.maxLength = 80;
    const textArea = el('textarea', 'fw-edit-text');
    textArea.value = node.text || '';
    textArea.placeholder = '写点什么…（Ctrl+Enter 保存，Esc 取消）';
    box.append(titleInput, textArea);
    eln.append(box);
    titleInput.addEventListener('pointerdown', (e) => e.stopPropagation());
    textArea.addEventListener('pointerdown', (e) => e.stopPropagation());
    textArea.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        commitEdit();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        cancelEdit();
      }
      e.stopPropagation();
    });
    titleInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        textArea.focus();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        cancelEdit();
      }
      e.stopPropagation();
    });
    setTimeout(() => {
      try {
        titleInput.focus();
      } catch {
        /* 吞掉 */
      }
    }, 0);
  }

  function commitEdit() {
    if (!editing) return;
    const box = editing.id && nodeEls.get(editing.id);
    const titleInput = box && box.querySelector('.fw-edit-title');
    const textArea = box && box.querySelector('.fw-edit-text');
    const title = titleInput ? String(titleInput.value || '').trim() : editing.originTitle;
    const text = textArea ? String(textArea.value || '') : editing.originText;
    const id = editing.id;
    endEditSession();
    dispatch({ type: 'update', id, patch: { title, text } });
  }

  function cancelEdit() {
    if (!editing) return;
    endEditSession();
    applyTransform();
  }

  function endEditSession() {
    const eln = editing && nodeEls.get(editing.id);
    editing = null;
    if (eln) {
      eln.classList.remove('is-editing');
      const editor = eln.querySelector('.fw-node-editor');
      if (editor) editor.remove();
    }
  }

  /* ------------------------------------------------------------ 连线 */

  const tempLine = svgEl('line', { class: 'fw-edge-temp' });

  function beginConnect(fromId) {
    connecting = { fromId };
    edgeLayer.append(tempLine);
    const node = nodeById(fromId);
    if (node) {
      const r = nodeRect(node);
      tempLine.setAttribute('x1', String(r.x + r.w / 2));
      tempLine.setAttribute('y1', String(r.y + r.h / 2));
    }
  }

  function moveConnect(event) {
    if (!connecting) return;
    const w = toWorld(event);
    tempLine.setAttribute('x2', String(w.x));
    tempLine.setAttribute('y2', String(w.y));
  }

  function endConnect(event) {
    if (!connecting) return;
    const { fromId } = connecting;
    connecting = null;
    if (tempLine.parentNode === edgeLayer) tempLine.remove();
    const w = toWorld(event);
    const target = nodeAtWorld(w.x, w.y);
    if (target && target.id !== fromId) {
      dispatch({ type: 'addEdge', from: fromId, to: target.id });   // 重复连线时 reducer 原样返回，静默
    }
  }

  /* ------------------------------------------------------------ 命中 */

  function nodeAtWorld(wx, wy) {
    const x = Number(wx);
    const y = Number(wy);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    for (let i = st.nodes.length - 1; i >= 0; i--) {
      const n = st.nodes[i];
      if (x >= n.x && x <= n.x + n.w && y >= n.y && y <= n.y + n.h) return n;
    }
    return null;
  }

  function edgeAtWorld(wx, wy) {
    const byId = new Map(st.nodes.map((n) => [n.id, nodeRect(n)]));
    for (let i = edgeEls.length - 1; i >= 0; i--) {
      const item = edgeEls[i];
      const a = byId.get(item.edge.from);
      const b = byId.get(item.edge.to);
      if (!a || !b) continue;
      const { ax, ay, bx, by } = edgeEndpoints(a, b);
      if (hitEdge(wx, wy, ax, ay, bx, by)) return { edge: item.edge, index: st.edges.indexOf(item.edge) };
    }
    return null;
  }

  /* ------------------------------------------------------------ 指针交互 */

  let suppressClick = false;

  viewport.addEventListener('pointerdown', (event) => {
    if (destroyed || event.button !== 0) return;
    const target = event.target;
    const interactive = target && target.closest
      ? target.closest('textarea, input, select, button, .fw-node-editor')
      : null;
    if (interactive) return;   // 编辑器/按钮/色点走原生行为
    const nodeEl = target && target.closest ? target.closest('.fw-node') : null;
    const anchorEl = target && target.closest ? target.closest('.fw-node-anchor') : null;
    const world1 = toWorld(event);
    try {
      viewport.setPointerCapture(event.pointerId);
    } catch {
      /* jsdom 等无实现：吞掉 */
    }

    if (anchorEl && nodeEl) {
      event.stopPropagation();
      beginConnect(nodeEl.dataset.id);
      select(nodeEl.dataset.id, { silent: false });
      return;
    }
    if (nodeEl) {
      const id = nodeEl.dataset.id;
      const node = nodeById(id);
      if (!node) return;
      select(id, { silent: false });
      drag = {
        id,
        startWorld: world1,
        origin: { x: node.x, y: node.y },
        moved: false
      };
      event.preventDefault();
      return;
    }
    // 空白处：清选 + 平移
    select(null, { silent: true });
    pan = {
      startScreen: screenPoint(event),
      origin: { x: view.x, y: view.y },
      moved: false
    };
    event.preventDefault();
  });

  viewport.addEventListener('pointermove', (event) => {
    if (destroyed) return;
    if (drag) {
      const w = toWorld(event);
      const dx = w.x - drag.startWorld.x;
      const dy = w.y - drag.startWorld.y;
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) drag.moved = true;
      const nx = Math.round((drag.origin.x + dx) * 10) / 10;
      const ny = Math.round((drag.origin.y + dy) * 10) / 10;
      const next = reduceBoard(st, { type: 'move', id: drag.id, x: nx, y: ny });
      if (next !== st) {
        st = next;
        const node = nodeById(drag.id);
        if (node) positionNode(node);
        updateEdgeGeometry();
      }
    } else if (pan) {
      const s = screenPoint(event);
      const dx = s.x - pan.startScreen.x;
      const dy = s.y - pan.startScreen.y;
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) pan.moved = true;
      view.x = pan.origin.x + dx;
      view.y = pan.origin.y + dy;
      applyTransform();
    } else if (connecting) {
      moveConnect(event);
    }
  });

  function endPointer(event) {
    if (drag) {
      if (drag.moved) {
        suppressClick = true;
        emitChange(true, 'move');
      }
      drag = null;
    } else if (pan) {
      if (pan.moved) suppressClick = true;
      pan = null;
    }
    if (connecting) endConnect(event);
    try {
      viewport.releasePointerCapture(event.pointerId);
    } catch {
      /* 吞掉 */
    }
    setTimeout(() => {
      suppressClick = false;
    }, 0);
  }

  viewport.addEventListener('pointerup', endPointer);
  viewport.addEventListener('pointercancel', endPointer);

  /* 节点内交互：色点 / 连线点击走坐标命中（跨浏览器稳） */
  nodesLayer.addEventListener('click', (event) => {
    if (destroyed) return;
    const dot = event.target && event.target.closest ? event.target.closest('.fw-dot') : null;
    if (dot) {
      const nodeEl = dot.closest('.fw-node');
      if (nodeEl) dispatch({ type: 'setColor', id: nodeEl.dataset.id, color: dot.dataset.color });
      return;
    }
    /* 其余点击不在这里处理（viewport click 已管连线命中） */
  });

  nodesLayer.addEventListener('dblclick', (event) => {
    if (destroyed) return;
    const nodeEl = event.target && event.target.closest ? event.target.closest('.fw-node') : null;
    if (!nodeEl) return;
    const node = nodeById(nodeEl.dataset.id);
    if (!node) return;
    event.stopPropagation();
    if (node.kind === 'chapter') {
      if (opts.onChapterOpen) opts.onChapterOpen(node);
      return;
    }
    beginEdit(node.id);
  });

  viewport.addEventListener('dblclick', (event) => {
    if (destroyed) return;
    if (event.target && event.target.closest && event.target.closest('.fw-node')) return;
    const w = toWorld(event);
    const size = defaultSize('note');
    const created = dispatch({
      type: 'add',
      node: {
        kind: 'note',
        x: Math.round(w.x - size.w / 2),
        y: Math.round(w.y - size.h / 2),
        title: '新便签',
        text: ''
      }
    });
    if (created && st.selectedId) beginEdit(st.selectedId);
  });

  viewport.addEventListener('click', (event) => {
    if (destroyed || suppressClick) return;
    if (event.target && event.target.closest && event.target.closest('.fw-node, .fw-node-anchor')) {
      return;   // 节点上的点击不是连线
    }
    const w = toWorld(event);
    const hit = edgeAtWorld(w.x, w.y);
    if (hit) dispatch({ type: 'deleteEdge', index: hit.index });
  });

  /* ------------------------------------------------------------ 滚轮缩放 */

  viewport.addEventListener('wheel', (event) => {
    if (destroyed) return;
    event.preventDefault();
    const s = screenPoint(event);
    const factor = event.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP;
    const next = zoomAt(view, s.x, s.y, factor);
    view.scale = next.scale;
    view.x = next.x;
    view.y = next.y;
    applyTransform();
  }, { passive: false });

  /* ------------------------------------------------------------ 键盘 */

  function onKeydown(event) {
    if (destroyed) return;
    const t = event.target;
    const tag = t && t.tagName ? String(t.tagName).toUpperCase() : '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    if (event.key === 'Delete' || event.key === 'Backspace') {
      if (!st.selectedId) return;
      const node = nodeById(st.selectedId);
      if (!node) return;
      const attached = edgeCountFor(st, node.id);
      event.preventDefault();
      if (!attached) {
        dispatch({ type: 'delete', id: node.id });
        notifySelection();
        return;
      }
      Promise.resolve(
        opts.confirmNodeDelete ? opts.confirmNodeDelete(node, attached) : true
      ).then((ok) => {
        if (ok && !destroyed) {
          dispatch({ type: 'delete', id: node.id });
          notifySelection();
        }
      }).catch((err) => {
        if (opts.onDeleteError) opts.onDeleteError(err);
      });
    } else if (event.key === 'Escape') {
      if (connecting) {
        connecting = null;
        if (tempLine.parentNode === edgeLayer) tempLine.remove();
      } else if (editing) {
        cancelEdit();
      } else if (st.selectedId) {
        select(null, { silent: false });
      }
    }
  }

  document.addEventListener('keydown', onKeydown);

  function notifySelection() {
    if (opts.onSelectionChange) opts.onSelectionChange(nodeById(st.selectedId || ''));
  }

  function select(id, { silent = false } = {}) {
    const next = id == null ? null : String(id);
    const changed = dispatch({ type: 'select', id: next }, { rerender: false });
    if (changed) {
      paintSelection();
      if (!silent) notifySelection();
    }
  }

  /* ------------------------------------------------------------ 对外 API */

  function setBoard(board, { fit = false } = {}) {
    if (destroyed) return;
    if (editing) endEditSession();
    st = reduceBoard(st, { type: 'replace', board, keepSelection: true });
    renderAll();
    if (fit) fitView();
    emitChange(false, 'load');
  }

  function getBoard() {
    return JSON.parse(JSON.stringify({ nodes: st.nodes, edges: st.edges }));
  }

  function fitView() {
    if (destroyed) return;
    if (!st.nodes.length) {
      view.scale = 1;
      view.x = 0;
      view.y = 0;
      applyTransform();
      return;
    }
    const rect = viewport.getBoundingClientRect ? viewport.getBoundingClientRect() : null;
    const vw = (rect && rect.width) || container.clientWidth || 960;
    const vh = (rect && rect.height) || container.clientHeight || 600;
    const xs = st.nodes.map((n) => n.x);
    const ys = st.nodes.map((n) => n.y);
    const minX = Math.min(...xs);
    const minY = Math.min(...ys);
    const maxX = Math.max(...st.nodes.map((n) => n.x + n.w));
    const maxY = Math.max(...st.nodes.map((n) => n.y + n.h));
    const bw = Math.max(60, maxX - minX);
    const bh = Math.max(60, maxY - minY);
    const pad = 48;
    const scale = clampZoom(Math.min((vw - pad * 2) / bw, (vh - pad * 2) / bh, 1.2));
    view.scale = scale;
    view.x = vw / 2 - ((minX + maxX) / 2) * scale;
    view.y = vh / 2 - ((minY + maxY) / 2) * scale;
    applyTransform();
  }

  function resetView() {
    view.scale = 1;
    view.x = 0;
    view.y = 0;
    applyTransform();
  }

  function zoomBy(factor) {
    const next = zoomAt(view, 0, 0, factor);
    view.scale = next.scale;
    view.x = next.x;
    view.y = next.y;
    applyTransform();
  }

  function getView() {
    return { ...view };
  }

  function addNode(patch = {}) {
    const size = defaultSize(patch.kind || 'note');
    const c = centerWorld();
    const jitter = (st.nodes.length % 5) * 24;
    const node = normalizeNode({
      ...size,
      title: '',
      text: '',
      color: 'plain',
      x: Math.round(c.x - size.w / 2 + jitter),
      y: Math.round(c.y - size.h / 2 + jitter),
      ...patch
    }, st.nodes.length);
    dispatch({ type: 'add', node });
    return nodeById(st.selectedId || node.id) || node;
  }

  function updateNode(id, patch) {
    return dispatch({ type: 'update', id, patch });
  }

  function deleteNode(id) {
    const changed = dispatch({ type: 'delete', id });
    notifySelection();
    return changed;
  }

  function addEdge(from, to) {
    return dispatch({ type: 'addEdge', from, to });
  }

  function deleteEdge(match) {
    if (match && match.index != null) return dispatch({ type: 'deleteEdge', index: match.index });
    return dispatch({ type: 'deleteEdge', from: match && match.from, to: match && match.to });
  }

  function setColor(id, color) {
    return dispatch({ type: 'setColor', id, color });
  }

  function getSelected() {
    return nodeById(st.selectedId || '');
  }

  function destroy() {
    destroyed = true;
    document.removeEventListener('keydown', onKeydown);
    try {
      wrap.remove();
    } catch {
      /* 吞掉 */
    }
  }

  renderAll();

  return {
    el: wrap,
    destroy,
    setBoard,
    getBoard,
    fitView,
    resetView,
    zoomBy,
    getView,
    centerWorld,
    addNode,
    updateNode,
    deleteNode,
    addEdge,
    deleteEdge,
    setColor,
    select,
    getSelected,
    nodeAtWorld,
    edgeAtWorld,
    beginEdit,
    commitEdit,
    /* 测试钩子：直接拿到当前内部状态快照 */
    _state: () => JSON.parse(JSON.stringify(st))
  };
}

export { SVG_NS };
