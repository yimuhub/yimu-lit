/* 资料画布核心（v5.5.2，docs/易创对齐清单 第二节 A2）——自研 SVG 画布，零依赖。
 *
 * 能力：
 *   · 节点 = 圆角卡片（名称 + 小标签徽记），pointer events 拖拽；
 *   · 连线 = 两节点间的二次贝塞尔曲线，端口按两节点相对方位自动选最顺的一侧，
 *     连线中点挂关系标签（可点击编辑，由视图层处理）；
 *   · 画布平移（空白处拖动）与缩放（滚轮 + 按钮，0.4–2.5，以光标为中心）；
 *   · 命中检测 = 简单包围盒（hitTest）；
 *   · 位置持久化 = localStorage `ew-canvas-pos:<bookKey>:<tab>`（{nodeId:{x,y}}）；
 *   · 自动布局 = 同心圆（角色关系图）/ 层级列（思维导图），重置布局并清记忆；
 *   · 导出 PNG = SVG serialize → canvas → toDataURL 下载。
 *
 * 纯函数全部导出（tests/v55_canvas_test.mjs 直测）：命中检测、端点方位、
 * 同心圆布局、位置记忆键、SVG 序列化、内容包围盒、思维导图展平等。
 * 所有颜色走 CSS 类（canvas.css 读 tokens 语义变量），JS 里零硬编码主题色。
 */

const SVG_NS = 'http://www.w3.org/2000/svg';

export const ZOOM_MIN = 0.4;
export const ZOOM_MAX = 2.5;
export const ZOOM_STEP = 1.15;

/* -------------------------------------------------------------- 位置记忆 */

export function posStoreKey(bookKey, tab) {
  return `ew-canvas-pos:${String(bookKey || '')}:${String(tab || '')}`;
}

export function loadPositions(bookKey, tab) {
  try {
    const raw = localStorage.getItem(posStoreKey(bookKey, tab));
    const obj = raw ? JSON.parse(raw) : null;
    if (!obj || typeof obj !== 'object') return null;
    const out = {};
    for (const [id, p] of Object.entries(obj)) {
      if (p && typeof p.x === 'number' && typeof p.y === 'number') out[id] = { x: p.x, y: p.y };
    }
    return Object.keys(out).length ? out : null;
  } catch {
    return null;
  }
}

export function savePositions(bookKey, tab, pos) {
  try {
    if (!pos || !Object.keys(pos).length) {
      localStorage.removeItem(posStoreKey(bookKey, tab));
      return true;
    }
    localStorage.setItem(posStoreKey(bookKey, tab), JSON.stringify(pos));
    return true;
  } catch {
    return false;
  }
}

export function clearPositions(bookKey, tab) {
  try {
    localStorage.removeItem(posStoreKey(bookKey, tab));
  } catch {
    /* 隐私模式等：清不掉就清不掉，布局仍会在内存里重排 */
  }
}

/* -------------------------------------------------------------- 几何纯函数 */

export function clampZoom(z) {
  const n = Number(z);
  if (!Number.isFinite(n)) return 1;
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, n));
}

/**
 * 节点卡片尺寸：宽度随名称字数（最多按 9 字计），组/根节点稍大；
 * 出场权重（后端按档案/账本出现次数或图谱自带 weight）让卡片适度变大（上限 +24px）。
 */
export function nodeSize(node) {
  const label = String((node && node.label) || '');
  const chars = Math.min(9, Array.from(label).length || 1);
  const big = node && (node.kind === 'group' || node.kind === 'root');
  const weight = Number(node && node.weight);
  const bump = Number.isFinite(weight) && weight > 1
    ? Math.min(24, 8 + Math.log2(weight) * 4)
    : 0;
  const base = Math.max(big ? 96 : 84, Math.min(196, 34 + chars * 15));
  return { w: Math.min(220, base + bump), h: big ? 54 : 46 };
}

/**
 * 命中检测（简单包围盒）。pos: {id:{x,y}}（节点中心，世界坐标）。
 * 从后往前（最上层优先）找第一个命中的节点；无命中返回 null。
 */
export function hitTest(x, y, nodes, pos, pad = 2) {
  const px = Number(x);
  const py = Number(y);
  if (!Number.isFinite(px) || !Number.isFinite(py) || !Array.isArray(nodes)) return null;
  for (let i = nodes.length - 1; i >= 0; i--) {
    const n = nodes[i];
    const p = (pos && pos[n.id]) || (n.x != null ? { x: n.x, y: n.y } : null);
    if (!p) continue;
    const { w, h } = nodeSize(n);
    if (px >= p.x - w / 2 - pad && px <= p.x + w / 2 + pad
      && py >= p.y - h / 2 - pad && py <= p.y + h / 2 + pad) {
      return n.id;
    }
  }
  return null;
}

/**
 * 端点方位选择：按两节点中心相对方向（dx/dy 主导侧）各取朝向对方的一侧中点。
 * 返回 {ax, ay, bx, by}（曲线起止锚点）。
 */
export function pickPorts(aRect, bRect) {
  const a = aRect || {};
  const b = bRect || {};
  const acx = a.x + (a.w || 0) / 2;
  const acy = a.y + (a.h || 0) / 2;
  const bcx = b.x + (b.w || 0) / 2;
  const bcy = b.y + (b.h || 0) / 2;
  const dx = bcx - acx;
  const dy = bcy - acy;
  let ax;
  let ay;
  let bx;
  let by;
  if (Math.abs(dx) >= Math.abs(dy)) {
    const s = dx >= 0 ? 1 : -1;
    ax = acx + s * (a.w || 0) / 2;
    ay = acy;
    bx = bcx - s * (b.w || 0) / 2;
    by = bcy;
  } else {
    const s = dy >= 0 ? 1 : -1;
    ax = acx;
    ay = acy + s * (a.h || 0) / 2;
    bx = bcx;
    by = bcy - s * (b.h || 0) / 2;
  }
  return { ax, ay, bx, by };
}

/** 边几何：起止锚点 + 贝塞尔控制点 + 标签锚点（真中点 = (P0 + 2C + P2)/4）。 */
export function edgeGeom(aPos, aSize, bPos, bSize, bow = 0.12) {
  const aRect = { x: aPos.x - aSize.w / 2, y: aPos.y - aSize.h / 2, w: aSize.w, h: aSize.h };
  const bRect = { x: bPos.x - bSize.w / 2, y: bPos.y - bSize.h / 2, w: bSize.w, h: bSize.h };
  const { ax, ay, bx, by } = pickPorts(aRect, bRect);
  const dx = bx - ax;
  const dy = by - ay;
  const len = Math.hypot(dx, dy) || 1;
  const k = Math.min(30, len * bow);
  const cx = (ax + bx) / 2 + (-dy / len) * k;
  const cy = (ay + by) / 2 + (dx / len) * k;
  const lx = (ax + 2 * cx + bx) / 4;
  const ly = (ay + 2 * cy + by) / 4;
  return { ax, ay, cx, cy, bx, by, lx, ly };
}

export function edgePath(geom) {
  const g = geom || {};
  return `M ${g.ax} ${g.ay} Q ${g.cx} ${g.cy} ${g.bx} ${g.by}`;
}

/* -------------------------------------------------------------- 自动布局 */

/** 同心圆布局：权重最高者在圆心，其余按权重降序逐环铺开（每环容量随周长走）。 */
export function layoutConcentric(nodes, opts = {}) {
  const cx = opts.cx != null ? opts.cx : 0;
  const cy = opts.cy != null ? opts.cy : 0;
  const startR = opts.startRadius != null ? opts.startRadius : 170;
  const ringGap = opts.ringGap != null ? opts.ringGap : 190;
  const spacing = opts.spacing != null ? opts.spacing : 132;
  const capFor = (r) => Math.max(6, Math.floor((2 * Math.PI * r) / spacing));
  const sorted = [...(nodes || [])].sort((a, b) => (b.weight || 0) - (a.weight || 0)
    || String(a.id).localeCompare(String(b.id)));
  const pos = {};
  let centerDone = false;
  let ring = 0;
  let slot = 0;
  let r = startR;
  let cap = capFor(r);
  for (const n of sorted) {
    if (!centerDone) {
      pos[n.id] = { x: cx, y: cy };
      centerDone = true;
      continue;
    }
    if (slot >= cap) {
      ring += 1;
      slot = 0;
      r = startR + ring * ringGap;
      cap = capFor(r);
    }
    const angle = (slot / cap) * 2 * Math.PI - Math.PI / 2 + (ring % 2) * (Math.PI / cap);
    pos[n.id] = { x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) };
    slot += 1;
  }
  return pos;
}

/**
 * 层级列布局（思维导图）：root 在最左列，组在第二列，条目在第三列；
 * 各组条目纵向依次排开，组节点 y = 子条目均值，root y = 组均值。
 */
export function layoutTree(nodes, opts = {}) {
  const colGap = opts.colGap != null ? opts.colGap : 260;
  const rowGap = opts.rowGap != null ? opts.rowGap : 66;
  const x0 = opts.x0 != null ? opts.x0 : 0;
  const list = nodes || [];
  const byId = new Map(list.map((n) => [n.id, n]));
  const pos = {};
  const roots = list.filter((n) => !n.parentId && byId.has(n.id));
  if (!roots.length) {
    return layoutConcentric(list, opts);
  }
  let cursorY = 0;
  const groupMeans = [];
  for (const root of roots) {
    const groups = list.filter((n) => n.parentId === root.id);
    const gMeans = [];
    for (const g of groups) {
      const items = list.filter((n) => n.parentId === g.id);
      if (!items.length) {
        pos[g.id] = { x: x0 + colGap, y: cursorY };
        cursorY += rowGap;
        gMeans.push(pos[g.id].y);
        continue;
      }
      const startY = cursorY;
      for (const it of items) {
        pos[it.id] = { x: x0 + colGap * 2, y: cursorY };
        cursorY += rowGap;
      }
      pos[g.id] = { x: x0 + colGap, y: startY + ((cursorY - rowGap) - startY) / 2 };
      gMeans.push(pos[g.id].y);
    }
    if (!groups.length) {
      pos[root.id] = { x: x0, y: cursorY };
      cursorY += rowGap;
    } else {
      pos[root.id] = { x: x0, y: gMeans.reduce((s, v) => s + v, 0) / gMeans.length };
    }
    groupMeans.push(pos[root.id].y);
  }
  return pos;
}

/** 内容包围盒（导出/fitView 用）：全部节点覆盖范围 + 内边距。 */
export function contentBounds(nodes, pos, opts = {}) {
  const pad = opts.pad != null ? opts.pad : 40;
  const pts = [];
  for (const n of nodes || []) {
    const p = (pos && pos[n.id]) || (n.x != null ? { x: n.x, y: n.y } : null);
    if (!p) continue;
    const { w, h } = nodeSize(n);
    pts.push({ x: p.x - w / 2, y: p.y - h / 2 }, { x: p.x + w / 2, y: p.y + h / 2 });
  }
  if (!pts.length) return { x: 0, y: 0, w: 640, h: 400 };
  const xs = pts.map((p) => p.x);
  const ys = pts.map((p) => p.y);
  const x = Math.min(...xs) - pad;
  const y = Math.min(...ys) - pad;
  return { x, y, w: Math.max(...xs) + pad - x, h: Math.max(...ys) + pad - y };
}

/* -------------------------------------------------------------- 思维导图展平 */

/**
 * 故事线树 → 画布图。父→子连边 kind='parent'；seq（前后置）连边 kind='seq'。
 * 节点带 parentId（组条目挂组、组挂 root），配合 visibleSubgraph 做展开层级。
 */
export function mindmapToGraph(story, seq) {
  const nodes = [];
  const edges = [];
  const root = story && story.root ? story.root : null;
  if (!root) return { nodes, edges };
  nodes.push({ id: root.id || 'root', label: root.label || '主线', kind: root.kind || 'root', parentId: null });
  for (const group of root.children || []) {
    const gid = group.id;
    nodes.push({ id: gid, label: group.label, kind: group.kind || 'group', parentId: root.id || 'root' });
    edges.push({ from: root.id || 'root', to: gid, label: '', kind: 'parent' });
    for (const item of group.children || []) {
      nodes.push({
        id: item.id,
        label: item.label,
        kind: item.kind || 'item',
        parentId: gid,
        badge: itemBadge(item)
      });
      edges.push({ from: gid, to: item.id, label: '', kind: 'parent' });
    }
  }
  for (const pair of seq || []) {
    if (Array.isArray(pair) && pair.length >= 2) {
      edges.push({ from: pair[0], to: pair[1], label: '前后置', kind: 'seq' });
    }
  }
  return { nodes, edges };
}

function itemBadge(item) {
  if (!item) return '';
  if (item.kind === 'foreshadow' || item.kind === 'branch') {
    if (item.ch != null) return `第${item.ch}章`;
  }
  if (item.status && item.kind === 'foreshadow') return String(item.status);
  return '';
}

/** 展开层级过滤：parentId ∈ expanded 的节点可见；边要求两端都可见。 */
export function visibleSubgraph(graph, expanded) {
  const g = graph || { nodes: [], edges: [] };
  const ex = expanded instanceof Set ? expanded : new Set(expanded || []);
  const visible = (g.nodes || []).filter((n) => !n.parentId || ex.has(n.parentId));
  const ids = new Set(visible.map((n) => n.id));
  const edges = (g.edges || []).filter((e) => ids.has(e.from) && ids.has(e.to));
  return { nodes: visible, edges };
}

/* -------------------------------------------------------------- SVG 序列化 */

const XMLSerializerImpl = () => globalThis.XMLSerializer
  || (typeof XMLSerializer !== 'undefined' ? XMLSerializer : null);

/** SVG → 可独立打开的字符串（补 xmlns、尺寸、背景色；bg 缺省读 --bg-base）。 */
export function serializeSvg(svg, opts = {}) {
  if (!svg) return '';
  const w = Math.max(1, Math.round(opts.w || 960));
  const h = Math.max(1, Math.round(opts.h || 600));
  const Ser = XMLSerializerImpl();
  if (!Ser) return '';
  const clone = svg.cloneNode(true);
  clone.setAttribute('xmlns', SVG_NS);
  clone.setAttribute('width', String(w));
  clone.setAttribute('height', String(h));
  const bg = document.createElementNS(SVG_NS, 'rect');
  bg.setAttribute('x', '0');
  bg.setAttribute('y', '0');
  bg.setAttribute('width', String(w));
  bg.setAttribute('height', String(h));
  bg.setAttribute('class', 'gc-export-bg');
  if (opts.bg) bg.setAttribute('fill', opts.bg);
  clone.insertBefore(bg, clone.firstChild);
  return new Ser().serializeToString(clone);
}

/* -------------------------------------------------------------- 画布实例 */

function svgEl(tag, attrs) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'text') el.textContent = String(v);
    else el.setAttribute(k, String(v));
  }
  return el;
}

function esc(s) {
  return String(s == null ? '' : s);
}

function truncateLabel(label, max = 9) {
  const chars = Array.from(esc(label));
  return chars.length > max ? `${chars.slice(0, max).join('')}…` : chars.join('');
}

/**
 * 创建画布实例。
 * opts:
 *   nodes / edges      初始图数据
 *   positions          初始位置（缺省走 opts.layout 自动布局）
 *   layout(nodes)      自动布局纯函数（同心圆 / 层级列，视图按 tab 传）
 *   onPositionsChange(pos, persist)   拖拽结束（persist=true）与拖拽中（false）
 *   onNodeClick(node)  原位点击（位移 < 4px）
 *   onEdgeClick(edge)  连线/关系标签点击
 *   onZoomChange(zoom)
 *   onResetLayout()    「重置布局」按钮回调（视图负责清记忆 + 传回新布局）
 *   onExportError(err) 导出失败回调
 */
export function createGraphCanvas(container, opts = {}) {
  const state = {
    nodes: [],
    edges: [],
    pos: {},
    zoom: 1,
    panX: 0,
    panY: 0,
    drag: null,       // {type:'node', id, startWorld, nodeStart, moved}
    panning: null,    // {type:'pan', startScreen, startPanX, startPanY, moved}
    destroyed: false
  };

  const wrap = document.createElement('div');
  wrap.className = 'gc-wrap';
  const svg = svgEl('svg', { class: 'gc-svg', role: 'img', 'aria-label': '资料画布' });
  const viewport = svgEl('g', { class: 'gc-viewport' });
  const edgesLayer = svgEl('g', { class: 'gc-edges' });
  const nodesLayer = svgEl('g', { class: 'gc-nodes' });
  viewport.append(edgesLayer, nodesLayer);
  svg.append(viewport);
  wrap.append(svg);

  /* 工具条：缩放 −/+/百分比、重置布局、导出 PNG */
  const toolbar = document.createElement('div');
  toolbar.className = 'gc-toolbar';
  const zoomLabel = document.createElement('span');
  zoomLabel.className = 'gc-zoom-label';
  zoomLabel.textContent = '100%';
  const btnOut = document.createElement('button');
  btnOut.type = 'button';
  btnOut.className = 'gc-btn';
  btnOut.textContent = '−';
  btnOut.setAttribute('aria-label', '缩小');
  const btnIn = document.createElement('button');
  btnIn.type = 'button';
  btnIn.className = 'gc-btn';
  btnIn.textContent = '+';
  btnIn.setAttribute('aria-label', '放大');
  const btnReset = document.createElement('button');
  btnReset.type = 'button';
  btnReset.className = 'gc-btn gc-btn-wide';
  btnReset.textContent = '重置布局';
  const btnExport = document.createElement('button');
  btnExport.type = 'button';
  btnExport.className = 'gc-btn gc-btn-wide';
  btnExport.textContent = '导出 PNG';
  const sep1 = document.createElement('span');
  sep1.className = 'gc-sep';
  const sep2 = document.createElement('span');
  sep2.className = 'gc-sep';
  toolbar.append(btnOut, zoomLabel, btnIn, sep1, btnReset, sep2, btnExport);
  wrap.append(toolbar);
  container.append(wrap);

  /* ------------------------------------------------------------ 视图换算 */

  function screenPoint(event) {
    const rect = svg.getBoundingClientRect
      ? svg.getBoundingClientRect()
      : { left: 0, top: 0 };
    return {
      x: (event.clientX || 0) - (rect.left || 0),
      y: (event.clientY || 0) - (rect.top || 0)
    };
  }

  function worldPoint(event) {
    const s = screenPoint(event);
    return { x: (s.x - state.panX) / state.zoom, y: (s.y - state.panY) / state.zoom };
  }

  function applyTransform() {
    viewport.setAttribute('transform',
      `translate(${state.panX} ${state.panY}) scale(${state.zoom})`);
    zoomLabel.textContent = `${Math.round(state.zoom * 100)}%`;
    if (opts.onZoomChange) opts.onZoomChange(state.zoom);
  }

  /* ------------------------------------------------------------ 渲染 */

  function posOf(node) {
    return state.pos[node.id] || { x: node.x != null ? node.x : 0, y: node.y != null ? node.y : 0 };
  }

  function renderNode(node) {
    const p = posOf(node);
    const { w, h } = nodeSize(node);
    const g = svgEl('g', {
      class: `gc-node gc-kind-${esc(node.kind || 'item')}`,
      'data-id': esc(node.id),
      transform: `translate(${p.x} ${p.y})`
    });
    const rect = svgEl('rect', { class: 'gc-node-box', x: -w / 2, y: -h / 2, width: w, height: h, rx: 12 });
    g.append(rect);
    const hasBadge = node.badge != null && String(node.badge) !== '';
    const label = svgEl('text', {
      class: 'gc-node-label',
      x: 0,
      y: hasBadge ? -4 : 5,
      'text-anchor': 'middle'
    });
    label.textContent = truncateLabel(node.label);
    g.append(label);
    if (hasBadge) {
      const badge = svgEl('text', { class: 'gc-node-badge', x: 0, y: 14, 'text-anchor': 'middle' });
      badge.textContent = truncateLabel(String(node.badge), 12);
      g.append(badge);
    }
    return g;
  }

  function renderEdge(edge) {
    const a = state.nodes.find((n) => n.id === edge.from);
    const b = state.nodes.find((n) => n.id === edge.to);
    if (!a || !b) return null;
    const geom = edgeGeom(posOf(a), nodeSize(a), posOf(b), nodeSize(b));
    const g = svgEl('g', {
      class: `gc-edge gc-edge-${esc(edge.kind || 'relation')}`,
      'data-from': esc(edge.from),
      'data-to': esc(edge.to)
    });
    const hit = svgEl('path', {
      class: 'gc-edge-hit',
      d: edgePath(geom),
      fill: 'none'
    });
    const line = svgEl('path', {
      class: 'gc-edge-line',
      d: edgePath(geom),
      fill: 'none'
    });
    g.append(hit, line);
    if (edge.label) {
      const gl = svgEl('g', { class: 'gc-edge-label' });
      const text = svgEl('text', { class: 'gc-edge-label-text', x: geom.lx, y: geom.ly, 'text-anchor': 'middle' });
      text.textContent = truncateLabel(edge.label, 6);
      const tw = Math.min(72, 12 + Array.from(String(edge.label)).length * 12);
      const rect = svgEl('rect', {
        class: 'gc-edge-label-box',
        x: geom.lx - tw / 2,
        y: geom.ly - 11,
        width: tw,
        height: 20,
        rx: 10
      });
      gl.append(rect, text);
      g.append(gl);
    }
    g.addEventListener('click', (event) => {
      if (suppressClick) return;
      event.stopPropagation();
      if (opts.onEdgeClick) opts.onEdgeClick(edge, event);
    });
    return g;
  }

  function render() {
    edgesLayer.textContent = '';
    nodesLayer.textContent = '';
    for (const e of state.edges) {
      const el = renderEdge(e);
      if (el) edgesLayer.append(el);
    }
    for (const n of state.nodes) {
      nodesLayer.append(renderNode(n));
    }
    applyTransform();
  }

  /* ------------------------------------------------------------ 交互 */

  let suppressClick = false;

  svg.addEventListener('pointerdown', (event) => {
    if (state.destroyed || event.button !== 0) return;
    const target = event.target;
    const nodeG = target && target.closest ? target.closest('.gc-node') : null;
    const world = worldPoint(event);
    if (nodeG) {
      const id = nodeG.getAttribute('data-id');
      const node = state.nodes.find((n) => n.id === id);
      if (node && state.pos[id]) {
        state.drag = {
          type: 'node',
          id,
          startWorld: world,
          nodeStart: { ...state.pos[id] },
          moved: false
        };
      }
    } else {
      const s = screenPoint(event);
      state.panning = {
        type: 'pan',
        startScreen: s,
        startPanX: state.panX,
        startPanY: state.panY,
        moved: false
      };
    }
    try {
      svg.setPointerCapture(event.pointerId);
    } catch {
      /* jsdom 等无实现：吞掉 */
    }
    event.preventDefault();
  });

  svg.addEventListener('pointermove', (event) => {
    if (state.destroyed) return;
    if (state.drag) {
      const world = worldPoint(event);
      const dx = world.x - state.drag.startWorld.x;
      const dy = world.y - state.drag.startWorld.y;
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) state.drag.moved = true;
      state.pos[state.drag.id] = {
        x: state.drag.nodeStart.x + dx,
        y: state.drag.nodeStart.y + dy
      };
      render();
      if (opts.onPositionsChange) opts.onPositionsChange({ ...state.pos }, false);
    } else if (state.panning) {
      const s = screenPoint(event);
      const dx = s.x - state.panning.startScreen.x;
      const dy = s.y - state.panning.startScreen.y;
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) state.panning.moved = true;
      state.panX = state.panning.startPanX + dx;
      state.panY = state.panning.startPanY + dy;
      applyTransform();
    }
  });

  function endPointer(event) {
    if (state.drag) {
      suppressClick = state.drag.moved;
      if (opts.onPositionsChange) opts.onPositionsChange({ ...state.pos }, true);
      state.drag = null;
    } else if (state.panning) {
      suppressClick = state.panning.moved;
      state.panning = null;
    }
    try {
      svg.releasePointerCapture(event.pointerId);
    } catch {
      /* 吞掉 */
    }
    setTimeout(() => {
      suppressClick = false;
    }, 0);
  }

  svg.addEventListener('pointerup', endPointer);
  svg.addEventListener('pointercancel', endPointer);

  svg.addEventListener('click', (event) => {
    if (state.destroyed || suppressClick) return;
    const nodeG = event.target && event.target.closest ? event.target.closest('.gc-node') : null;
    if (nodeG && opts.onNodeClick) {
      const node = state.nodes.find((n) => n.id === nodeG.getAttribute('data-id'));
      if (node) opts.onNodeClick(node);
    }
  });

  svg.addEventListener('wheel', (event) => {
    if (state.destroyed) return;
    event.preventDefault();
    const before = worldPoint(event);
    const factor = event.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP;
    state.zoom = clampZoom(state.zoom * factor);
    const after = worldPoint(event);
    state.panX += (after.x - before.x) * state.zoom;
    state.panY += (after.y - before.y) * state.zoom;
    applyTransform();
  }, { passive: false });

  btnIn.addEventListener('click', () => zoomBy(ZOOM_STEP));
  btnOut.addEventListener('click', () => zoomBy(1 / ZOOM_STEP));
  btnReset.addEventListener('click', () => {
    if (opts.onResetLayout) opts.onResetLayout();
  });
  btnExport.addEventListener('click', () => {
    try {
      exportPng();
    } catch (err) {
      if (opts.onExportError) opts.onExportError(err);
    }
  });

  /* ------------------------------------------------------------ 对外 API */

  function zoomBy(factor) {
    state.zoom = clampZoom(state.zoom * factor);
    applyTransform();
  }

  function fitView() {
    const rect = svg.getBoundingClientRect ? svg.getBoundingClientRect() : null;
    const vw = (rect && rect.width) || container.clientWidth || 960;
    const vh = (rect && rect.height) || container.clientHeight || 600;
    const b = contentBounds(state.nodes, state.pos);
    const zoom = clampZoom(Math.min(vw / (b.w || 1), vh / (b.h || 1), 1.4));
    state.zoom = zoom;
    state.panX = vw / 2 - (b.x + b.w / 2) * zoom;
    state.panY = vh / 2 - (b.y + b.h / 2) * zoom;
    applyTransform();
  }

  function ensurePositions() {
    const missing = state.nodes.filter((n) => !state.pos[n.id]);
    if (!missing.length) return false;
    const layout = opts.layout || ((list) => layoutConcentric(list, { cx: 0, cy: 0 }));
    const auto = layout(state.nodes);
    for (const n of missing) {
      if (auto[n.id]) state.pos[n.id] = auto[n.id];
    }
    return true;
  }

  function setGraph(nodes, edges, positions) {
    state.nodes = Array.isArray(nodes) ? nodes.slice() : [];
    state.edges = Array.isArray(edges) ? edges.slice() : [];
    state.pos = {};
    const incoming = positions || {};
    for (const n of state.nodes) {
      if (incoming[n.id] && typeof incoming[n.id].x === 'number') {
        state.pos[n.id] = { ...incoming[n.id] };
      }
    }
    ensurePositions();
    render();
    fitView();
  }

  function applyPositions(pos, { fit = true } = {}) {
    state.pos = {};
    for (const [id, p] of Object.entries(pos || {})) {
      if (p && typeof p.x === 'number') state.pos[id] = { x: p.x, y: p.y };
    }
    ensurePositions();
    render();
    if (fit) fitView();
  }

  function getPositions() {
    return { ...state.pos };
  }

  function getZoom() {
    return state.zoom;
  }

  function setZoom(z) {
    state.zoom = clampZoom(z);
    applyTransform();
  }

  /**
   * 导出 PNG：按内容包围盒重排导出视图 → serialize → Image → canvas → toDataURL 下载。
   * 浏览器外（jsdom）没有 Image/canvas：抛错交 onExportError。
   */
  function exportPng() {
    const b = contentBounds(state.nodes, state.pos, { pad: 48 });
    const scale = 2;
    const w = Math.max(1, Math.round(b.w));
    const h = Math.max(1, Math.round(b.h));
    const svgStr = exportSvgString({ w, h });
    const ImageCls = globalThis.Image || (typeof Image !== 'undefined' ? Image : null);
    if (!ImageCls || typeof document === 'undefined') {
      throw new Error('当前环境不支持导出 PNG');
    }
    const img = new ImageCls();
    img.onload = () => {
      try {
        const canvas = document.createElement('canvas');
        canvas.width = w * scale;
        canvas.height = h * scale;
        const ctx2d = canvas.getContext('2d');
        if (!ctx2d) throw new Error('无法创建画布上下文');
        ctx2d.scale(scale, scale);
        ctx2d.drawImage(img, 0, 0, w, h);
        const url = canvas.toDataURL('image/png');
        const a = document.createElement('a');
        a.href = url;
        a.download = `资料画布-${new Date().toISOString().slice(0, 10)}.png`;
        document.body.append(a);
        a.click();
        a.remove();
      } catch (err) {
        if (opts.onExportError) opts.onExportError(err);
      }
    };
    img.onerror = () => {
      if (opts.onExportError) opts.onExportError(new Error('图像渲染失败'));
    };
    img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svgStr)}`;
  }

  /**
   * 导出用 SVG 字符串：以内容包围盒为准生成独立视图（不依赖当前平移缩放），
   * 背景色读主题 --bg-base（读不到回退白）。
   */
  function exportSvgString({ w, h } = {}) {
    const bb = contentBounds(state.nodes, state.pos, { pad: 48 });
    const width = Math.max(1, Math.round(w || bb.w));
    const height = Math.max(1, Math.round(h || bb.h));
    const oldTx = viewport.getAttribute('transform');
    viewport.setAttribute('transform',
      `translate(${-bb.x} ${-bb.y}) scale(1)`);
    let bg = '#ffffff';
    try {
      const cs = getComputedStyle(svg);
      const v = cs && cs.getPropertyValue('--bg-base');
      if (v && v.trim()) bg = v.trim();
    } catch {
      /* jsdom：保持回退色 */
    }
    const str = serializeSvg(svg, { w: width, h: height, bg });
    if (oldTx != null) viewport.setAttribute('transform', oldTx);
    else viewport.removeAttribute('transform');
    return str;
  }

  function destroy() {
    state.destroyed = true;
    try {
      wrap.remove();
    } catch {
      /* 吞掉 */
    }
  }

  render();

  return {
    setGraph,
    applyPositions,
    getPositions,
    getZoom,
    setZoom,
    zoomBy,
    fitView,
    exportPng,
    exportSvgString,
    destroy,
    el: wrap
  };
}
