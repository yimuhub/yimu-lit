/* 资料画布页（v5.5.2，docs/易创对齐清单 第二节 A2）：
 *   · 角色关系图 tab —— 数据源 /api/canvas/graph（15-故事图谱.json + 00-人物档案.md 聚合），
 *     节点拖拽 / 自动同心圆布局 / 位置持久化 / 点连线编辑关系标签（写回后端覆盖层）；
 *   · 故事线思维导图 tab —— 主线/人物线/支线/伏笔四组层级树，点组节点展开层级，
 *     连线 = 父子 + 伏笔前后置；
 *   · 书选择器复用作品库数据（store.get().library.items），两个 tab 独立记位置；
 *   · 画布本体在 core/graphcanvas.js（自研 SVG，零依赖），本文件只做页面编排。
 * 接口失败给中文可操作提示，不影响写作主链路。 */

import { h } from '../core/dom.js';
import { request, errorText } from '../core/api.js';
import { store } from '../core/state.js';
import { toast } from '../core/ui.js';
import {
  createGraphCanvas,
  layoutConcentric,
  layoutTree,
  mindmapToGraph,
  visibleSubgraph,
  loadPositions,
  savePositions,
  clearPositions,
  nodeSize
} from '../core/graphcanvas.js';

/* -------------------------------------------------------------- 纯函数（导出供测试） */

export const CANVAS_TABS = ['chars', 'story'];
export const DEFAULT_TAB = 'chars';
export const TAB_LABELS = { chars: '角色关系图', story: '故事线思维导图' };

/** tab 状态机：{tab, loading, error} + switch/load/loaded/error 事件。 */
export function reduceTab(state, event) {
  const s = state || { tab: DEFAULT_TAB, loading: false, error: null };
  const ev = event || {};
  switch (ev.type) {
    case 'switch':
      if (!CANVAS_TABS.includes(ev.tab) || ev.tab === s.tab) return s;
      return { ...s, tab: ev.tab, loading: false, error: null };
    case 'load':
      return { ...s, loading: true, error: null };
    case 'loaded':
      return { ...s, loading: false, error: null };
    case 'error':
      return { ...s, loading: false, error: String(ev.error || '加载失败') };
    default:
      return s;
  }
}

/** 人物节点徽记：有权重才显示「出场 N」。 */
export function personBadge(node) {
  const w = node && Number(node.weight);
  return Number.isFinite(w) && w > 1 ? `出场 ${w}` : '';
}

/** 角色关系图数据 → 画布图（节点带徽记；缺边缺点都容错）。 */
export function buildCharsGraph(payload) {
  const chars = (payload && payload.characters) || {};
  const nodes = (Array.isArray(chars.nodes) ? chars.nodes : [])
    .filter((n) => n && n.id)
    .map((n) => ({ ...n, kind: n.kind || 'person', label: String(n.label || n.id), badge: personBadge(n) }));
  const edges = (Array.isArray(chars.edges) ? chars.edges : [])
    .filter((e) => e && e.from && e.to && nodes.some((n) => n.id === e.from) && nodes.some((n) => n.id === e.to))
    .map((e) => ({ ...e, label: String(e.label || '关联'), kind: e.kind || 'relation' }));
  return { nodes, edges };
}

/** 故事线树 → 画布图（mindmapToGraph 展平 + 父子/前后置连边）。 */
export function buildStoryGraph(payload) {
  const story = (payload && payload.story) || null;
  if (!story || !story.root) return { nodes: [], edges: [] };
  return mindmapToGraph(story, story.seq || []);
}

/** 按 tab 选自动布局：关系图同心圆，思维导图层级列。 */
export function pickLayout(tab) {
  if (tab === 'story') {
    return (nodes) => layoutTree(nodes, { x0: 0, colGap: 280, rowGap: 64 });
  }
  return (nodes) => layoutConcentric(nodes, { cx: 0, cy: 0 });
}

const BOOK_MEM_KEY = 'ew-canvas-book';

function rememberBook(key) {
  try {
    localStorage.setItem(BOOK_MEM_KEY, String(key || ''));
  } catch {
    /* 隐私模式：记住失败无碍 */
  }
}

function recallBook() {
  try {
    return localStorage.getItem(BOOK_MEM_KEY) || '';
  } catch {
    return '';
  }
}

/* -------------------------------------------------------------- 视图 */

export function renderCanvas(container, ctx) {
  const view = h('div', { class: 'canvas-view' });
  container.append(view);

  const model = {
    tab: DEFAULT_TAB,
    loading: false,
    error: null,
    key: '',
    payload: null,
    graph: { nodes: [], edges: [] },
    expanded: new Set(['root']),
    editing: null,
    canvas: null,
    destroyed: false
  };

  /* ------------------------------------------------------------- 头部：书选择 + tab */

  const bookSelect = h('select', { class: 'select canvas-book-select', 'aria-label': '选择作品' });
  const tabsRow = h('div', { class: 'canvas-tabs', role: 'tablist' });

  view.append(
    h('div', { class: 'canvas-head' },
      h('div', {},
        h('h1', { class: 'canvas-title', text: '资料画布' }),
        h('p', {
          class: 'canvas-desc',
          text: '角色关系与故事线的可视化草稿台：拖拽节点、缩放平移，点连线可改关系标签；布局自动记忆，可一键导出 PNG。'
        })
      ),
      h('div', { class: 'canvas-head-controls' }, bookSelect, tabsRow)
    ),
    h('div', { class: 'canvas-error', hidden: true })
  );

  const errorBox = view.querySelector('.canvas-error');
  const stage = h('div', { class: 'canvas-stage' });
  view.append(stage);

  /* ------------------------------------------------------------- 空态 */

  function emptyState(title, desc) {
    stage.textContent = '';
    stage.append(
      h('div', { class: 'canvas-empty' },
        h('div', { class: 'canvas-empty-icon', 'aria-hidden': 'true', text: ' ◈ ' }),
        h('h3', { text: title }),
        h('p', { text: desc })
      )
    );
  }

  /* ------------------------------------------------------------- tab 渲染 */

  function renderTabs() {
    tabsRow.textContent = '';
    for (const t of CANVAS_TABS) {
      tabsRow.append(
        h('button', {
          class: `canvas-tab${model.tab === t ? ' is-active' : ''}`,
          type: 'button',
          role: 'tab',
          'aria-selected': model.tab === t ? 'true' : 'false',
          text: TAB_LABELS[t],
          on: {
            click: () => {
              const next = reduceTab(model, { type: 'switch', tab: t });
              if (next !== model) {
                model.tab = next.tab;
                model.loading = next.loading;
                model.error = next.error;
                model.editing = null;
                renderTabs();
                rebuildGraph();
              }
            }
          }
        })
      );
    }
  }

  /* ------------------------------------------------------------- 书选择 */

  function renderBookOptions() {
    const items = (store.get().library && store.get().library.items) || [];
    bookSelect.textContent = '';
    if (!items.length) {
      bookSelect.append(h('option', { value: '', text: '（作品库为空）' }));
      return;
    }
    for (const it of items) {
      bookSelect.append(h('option', { value: it.key, text: it.name || it.key, selected: it.key === model.key }));
    }
  }

  bookSelect.addEventListener('change', () => {
    model.key = bookSelect.value;
    rememberBook(model.key);
    model.expanded = new Set(['root']);
    model.editing = null;
    fetchGraph();
  });

  function resolveInitialKey() {
    const items = (store.get().library && store.get().library.items) || [];
    if (!items.length) return '';
    const remembered = recallBook();
    if (remembered && items.some((i) => i.key === remembered)) return remembered;
    if (store.get().currentKey && items.some((i) => i.key === store.get().currentKey)) {
      return store.get().currentKey;
    }
    return items[0].key;
  }

  /* ------------------------------------------------------------- 数据加载 */

  async function fetchGraph() {
    if (model.destroyed) return;
    model.loading = true;
    model.error = null;
    renderStatus();
    const key = model.key;
    if (!key) {
      model.loading = false;
      model.payload = null;
      model.graph = { nodes: [], edges: [] };
      renderEmptyOrCanvas();
      return;
    }
    try {
      const res = await request('/api/canvas/graph', { query: { key }, timeout: 30000 });
      if (model.destroyed || model.key !== key) return;
      model.payload = res && res.ok ? res : null;
      model.loading = false;
      rebuildGraph({ fresh: true });
    } catch (err) {
      if (model.destroyed || model.key !== key) return;
      model.loading = false;
      model.error = errorText(err, '画布数据加载失败');
      renderStatus();
      renderEmptyOrCanvas();
    }
  }

  function rebuildGraph({ fresh = false } = {}) {
    if (model.destroyed) return;
    if (fresh) {
      model.expanded = new Set(['root']);
      model.editing = null;
    }
    model.graph = model.tab === 'story' ? buildStoryGraph(model.payload) : buildCharsGraph(model.payload);
    renderStatus();
    renderEmptyOrCanvas({ keepPositions: !fresh });
  }

  /* ------------------------------------------------------------- 画布挂载 */

  function destroyCanvas() {
    if (model.canvas) {
      model.canvas.destroy();
      model.canvas = null;
    }
    stage.textContent = '';
  }

  function renderStatus() {
    if (model.error) {
      errorBox.hidden = false;
      errorBox.textContent = '';
      errorBox.append(
        h('span', { text: model.error }),
        h('button', {
          class: 'btn btn-sm',
          type: 'button',
          text: '重试',
          on: { click: () => fetchGraph() }
        })
      );
    } else {
      errorBox.hidden = true;
      errorBox.textContent = '';
    }
  }

  function positionsFor(key, tab) {
    return loadPositions(key, tab);
  }

  function renderEmptyOrCanvas({ keepPositions = false } = {}) {
    destroyCanvas();
    if (model.loading) {
      stage.append(h('div', { class: 'canvas-loading', text: '正在聚合图谱数据…' }));
      return;
    }
    if (!model.key) {
      emptyState('还没有选择作品', '在上方选择一本书，即可把人物档案、故事图谱与线索账本聚合到画布上。');
      return;
    }
    if (!model.graph.nodes.length) {
      if (model.tab === 'chars') {
        emptyState('暂无可视化的人物',
          '这本书还没有「00-人物档案.md」（或档案里没有「## 人物：X」标题）。先在创作流程里规划人物，画布会自动出现。');
      } else {
        emptyState('暂无可视化的故事线',
          '这本书还没有「10-线索账本.md」与人物档案。登记线索或规划人物后，故事线思维导图会自动生成。');
      }
      return;
    }

    const key = model.key;
    const tab = model.tab;
    const saved = keepPositions ? positionsFor(key, tab) : null;
    model.canvas = createGraphCanvas(stage, {
      layout: pickLayout(tab),
      positions: saved,
      onPositionsChange: (pos, persist) => {
        if (persist) savePositions(key, tab, pos);
      },
      onNodeClick: (node) => {
        if (model.tab === 'story' && (node.kind === 'group' || node.kind === 'root')) {
          toggleExpand(node.id);
        }
      },
      onEdgeClick: (edge) => {
        if (model.tab === 'chars' && edge.kind !== 'seq') openEdgeEditor(edge);
      },
      onResetLayout: () => resetLayout(),
      onExportError: (err) => {
        toast(errorText(err, '导出 PNG 失败：当前环境不支持画布渲染'), { tone: 'danger' });
      },
      onZoomChange: () => {
        /* 缩放百分比由画布工具条自显示，这里无需处理 */
      }
    });
    canvasSetGraph();
    renderEdgeEditor();
  }

  function canvasSetGraph() {
    if (!model.canvas) return;
    const pos = positionsFor(model.key, model.tab) || undefined;
    /* 思维导图按展开层级过滤（默认只露 root + 组节点） */
    const g = model.tab === 'story' ? visibleSubgraph(model.graph, model.expanded) : model.graph;
    model.canvas.setGraph(g.nodes, g.edges, pos);
  }

  /* ------------------------------------------------------------- 展开层级（思维导图） */

  function toggleExpand(id) {
    if (model.expanded.has(id)) model.expanded.delete(id);
    else model.expanded.add(id);
    canvasSetGraph();
  }

  /* ------------------------------------------------------------- 重置布局 */

  function resetLayout() {
    clearPositions(model.key, model.tab);
    const layout = pickLayout(model.tab);
    const nodes = model.canvas ? currentNodes() : model.graph.nodes;
    const pos = layout(nodes);
    if (model.canvas) {
      model.canvas.applyPositions(pos, { fit: true });
    }
    toast('已按自动布局重排，并清除位置记忆');
  }

  function currentNodes() {
    if (model.tab === 'story') return visibleSubgraph(model.graph, model.expanded).nodes;
    return model.graph.nodes;
  }

  /* ------------------------------------------------------------- 关系标签编辑（角色图） */

  let editorBox = null;

  function closeEdgeEditor() {
    model.editing = null;
    renderEdgeEditor();
  }

  function renderEdgeEditor() {
    if (editorBox) {
      editorBox.remove();
      editorBox = null;
    }
    if (!model.editing || model.tab !== 'chars') return;
    const e = model.editing;
    const input = h('input', {
      class: 'input canvas-edge-input',
      type: 'text',
      value: String(e.label || ''),
      maxlength: '12',
      placeholder: '盟友 / 对手 / 亲人 / 暧昧 / 上下级…',
      'aria-label': `编辑 ${e.from} 与 ${e.to} 的关系标签`
    });
    const save = async () => {
      const label = String(input.value || '').trim();
      try {
        await request('/api/canvas/edge', {
          method: 'POST',
          body: { key: model.key, a: e.from, b: e.to, label },
          timeout: 15000
        });
        toast(label ? `关系标签已保存：${label}` : '已恢复自动标签');
        closeEdgeEditor();
        await fetchGraph();
      } catch (err) {
        toast(errorText(err, '保存关系标签失败'), { tone: 'danger' });
      }
    };
    editorBox = h('div', { class: 'canvas-edge-editor' },
      h('span', { class: 'canvas-edge-pair', text: `${e.from} ↔ ${e.to}` }),
      input,
      h('button', { class: 'btn btn-sm', type: 'button', text: '保存', on: { click: save } }),
      h('button', { class: 'btn btn-sm', type: 'button', text: '关闭', on: { click: closeEdgeEditor } }),
      h('span', { class: 'hint-text', text: '留空保存 = 恢复自动标签；标签最长 12 字。' })
    );
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') save();
      if (event.key === 'Escape') closeEdgeEditor();
    });
    stage.append(editorBox);
    input.focus();
  }

  function openEdgeEditor(edge) {
    model.editing = edge;
    renderEdgeEditor();
  }

  /* ------------------------------------------------------------- 装配 + 卸载 */

  renderTabs();
  renderBookOptions();
  model.key = resolveInitialKey();
  renderBookOptions();

  const unsub = store.subscribe((state) => {
    if (model.destroyed) return;
    /* 作品库刷新后选项要跟上（改名/删除后 key 失效时回退到第一本） */
    const items = (state.library && state.library.items) || [];
    if (items.length && !items.some((i) => i.key === model.key)) {
      model.key = items[0].key;
      rememberBook(model.key);
      renderBookOptions();
      fetchGraph();
    } else {
      renderBookOptions();
    }
  });

  fetchGraph();

  return function destroy() {
    model.destroyed = true;
    try {
      if (typeof unsub === 'function') unsub();
    } catch {
      /* 吞掉 */
    }
    destroyCanvas();
    view.remove();
  };
}

/* nodeSize 保留在导出面，供测试核对卡片尺寸口径。 */
export { nodeSize };
