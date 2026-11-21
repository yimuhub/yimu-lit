/* 无限画布页（v5.8.0，#/flow 创作工作流白板）：
 *   · 自由白板本体在 core/whiteboard.js（DOM 世界层 + 无限平移缩放 + 自由连线），
 *     本文件只做页面编排：书选择器、工具条、章卡导入、AI 扩展、整板持久化；
 *   · 与资料画布（#/canvas，数据驱动的图谱视图）定位不同：这里是想法→大纲→
 *     章节卡的工作流工具，节点自由摆放、自由连线、AI 发散；
 *   · 持久化 = GET/POST /api/flow/board 整板读写（前端 700ms 防抖，简单覆盖）；
 *   · AI 扩展 = POST /api/flow/expand（scene「画布扩展」）；mock 模式本地假数据不穿透；
 *   · 章卡导入 = GET /api/flow/chapters（本地轻读 02d-章卡.jsonl），节点只读、
 *     双击跳 bench——章卡是项目数据，画布草稿是作者工作区（纯净域，无出口）。
 * 接口失败给中文可操作提示，不影响写作主链路。 */

import { h, icon } from '../core/dom.js';
import { request, errorText, IS_MOCK } from '../core/api.js';
import { store } from '../core/state.js';
import { toast, confirmDialog, openModal } from '../core/ui.js';
import { navigate } from '../core/router.js';
import {
  createWhiteboard,
  scatterAround,
  defaultSize,
  exportBoardJSON,
  NODE_COLORS
} from '../core/whiteboard.js';

/* -------------------------------------------------------------- 纯函数（导出供测试） */

export const BOOK_MEM_KEY = 'ew-flow-book';
export const SAVE_DEBOUNCE_MS = 700;

/** 页面级状态机：{key, loading, error, busy} + switchBook/load/loaded/error/busy。 */
export function reduceFlow(state, event) {
  const s = state || { key: '', loading: false, error: null, busy: false };
  const ev = event || {};
  switch (ev.type) {
    case 'switchBook':
      return { key: String(ev.key || ''), loading: true, error: null, busy: false };
    case 'load':
      return { ...s, loading: true, error: null };
    case 'loaded':
      return { ...s, loading: false, error: null };
    case 'error':
      return { ...s, loading: false, error: String(ev.error || '加载失败') };
    case 'busy':
      return { ...s, busy: Boolean(ev.busy) };
    default:
      return s;
  }
}

/** 章卡 → 白板节点（kind=chapter 只读灰底；3 列网格摆在 origin 右下）。 */
export const CHAPTER_GRID = { cols: 3, gapX: 252, gapY: 132 };

export function chaptersToNodes(chapters, origin = { x: 0, y: 0 }) {
  const list = (Array.isArray(chapters) ? chapters : [])
    .filter((c) => c && Number.isFinite(Number(c.n)));
  return list.map((c, i) => {
    const n = Math.floor(Number(c.n));
    const col = i % CHAPTER_GRID.cols;
    const row = Math.floor(i / CHAPTER_GRID.cols);
    const title = String(c.title || '').trim();
    return {
      kind: 'chapter',
      n,
      title: `第 ${n} 章 · ${title || ''}`.trim(),
      text: String(c.summary || ''),
      color: 'plain',
      x: Math.round((origin.x || 0) + col * CHAPTER_GRID.gapX),
      y: Math.round((origin.y || 0) + row * CHAPTER_GRID.gapY)
    };
  });
}

/** mock 演示模式（?mock=1）的扩展假数据：绝不发请求穿透到真实后端。 */
export function expandMockItems(text, count = 4) {
  const seed = String(text || '').trim();
  const pool = [
    { title: '反转视角', text: `换到对手或旁观者的眼睛再看「${seed.slice(0, 12) || '这个想法'}」，结论会怎么变？` },
    { title: '极端化推演', text: '把核心冲突放大到极限：被逼到墙角时，角色会做出什么意料之外的选择？' },
    { title: '代价清单', text: '如果这件事要成，主角必须付出什么？列出三条具体代价，挑最疼的一条展开。' },
    { title: '隐藏关联', text: '这个点能和已有的一条伏笔或次要人物挂上钩吗？钩上之后谁受益、谁受损？' },
    { title: '下一小步', text: '拆成一个明天就能动笔的最小场景：谁、在哪、要什么、被什么拦住。' }
  ];
  const n = Math.max(3, Math.min(5, Math.floor(Number(count) || 4)));
  return pool.slice(0, n);
}

/** AI 扩展结果 → 新增子节点 + 父→子连线（环形散布在父节点周围）。 */
export function buildExpandNodes(parent, items, opts = {}) {
  const p = parent || { x: 0, y: 0, w: 230, h: 156 };
  const size = defaultSize('ai');
  const center = { x: (p.x || 0) + (p.w || size.w) / 2, y: (p.y || 0) + (p.h || size.h) / 2 };
  const angle0 = Number.isFinite(opts.angle0) ? opts.angle0 : undefined;
  const pts = scatterAround(center, (items || []).length, angle0 === undefined ? {} : { angle0 });
  const nodes = [];
  const edges = [];
  (items || []).forEach((it, i) => {
    const pt = pts[i] || center;
    const id = (opts.newId || (() => `ai-${Date.now().toString(36)}-${i}`))();
    nodes.push({
      id,
      kind: 'ai',
      title: String((it && it.title) || '子想法').slice(0, 80),
      text: String((it && it.text) || '').slice(0, 4000),
      color: 'accent',
      x: Math.round(pt.x - size.w / 2),
      y: Math.round(pt.y - size.h / 2),
      w: size.w,
      h: size.h
    });
    edges.push({ from: p.id || '', to: id });
  });
  return { nodes, edges: edges.filter((e) => e.from) };
}

/** 导出 JSON 文件名：无限画布-YYYYMMDD.json（全局板加 -global 后缀）。 */
export function exportFileName(key) {
  const d = new Date();
  const ymd = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`;
  return `无限画布-${key ? String(key).slice(0, 40) : 'global'}-${ymd}.json`;
}

/* -------------------------------------------------------------- 视图 */

export const EMPTY_STEPS = [
  '双击空白处，就地新建一张便签；拖住卡片边缘可自由摆放。',
  '拖住节点右缘的小圆点，拉到另一节点松手，连成有向箭头；点连线可删除。',
  '选中一张便签，点工具条「✦ 扩展」，AI 帮你发散 3-5 个子想法。'
];

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

export function renderFlow(container) {
  const view = h('div', { class: 'flow-view' });
  container.append(view);

  const model = {
    key: '',
    destroyed: false,
    wb: null,
    expandBusy: false,
    saveTimer: 0,
    saveWarned: false
  };

  /* ------------------------------------------------------------- 头部 */

  const titleEl = h('h1', { class: 'flow-title' },
    '无限画布',
    h('span', { class: 'flow-title-badge', text: '自由白板' }));
  const descEl = h('p', {
    class: 'flow-desc',
    text: '想法 → 大纲 → 章节卡的工作流白板：无限平移缩放、节点自由连线、AI 发散；与「资料画布」的书目图谱是两回事。'
  });

  const bookSelect = h('select', { class: 'select flow-book-select', 'aria-label': '选择白板归属的作品' });
  const btnNote = h('button', { class: 'btn btn-sm', type: 'button', html: `${icon('plus', 14)}<span>新建便签</span>` });
  const btnExpand = h('button', { class: 'btn btn-sm', type: 'button', html: `${icon('sparkles', 14)}<span>✦ 扩展</span>` });
  const btnImport = h('button', { class: 'btn btn-sm', type: 'button', html: `${icon('file-text', 14)}<span>导入章卡</span>` });
  const btnReset = h('button', { class: 'btn btn-sm', type: 'button', html: `${icon('target', 14)}<span>重置视图</span>` });
  const btnExport = h('button', { class: 'btn btn-sm', type: 'button', html: `${icon('download', 14)}<span>导出 JSON</span>` });
  const countEl = h('span', { class: 'flow-count', text: '节点 0 · 连线 0' });
  const hintEl = h('span', { class: 'flow-hint', text: '拖空白平移 · 滚轮缩放 · Delete 删除选中' });

  view.append(
    h('div', { class: 'flow-head' },
      h('div', {}, titleEl, descEl),
      h('div', { class: 'flow-head-controls' },
        bookSelect,
        h('div', { class: 'flow-toolbar' }, btnNote, btnExpand, btnImport, btnReset, btnExport, countEl)
      )
    ),
    h('div', { class: 'flow-error', hidden: true })
  );

  const errorBox = view.querySelector('.flow-error');
  const stage = h('div', { class: 'flow-stage' });
  view.append(stage);
  view.append(h('div', { class: 'flow-toolbar', style: { marginTop: '2px' } }, hintEl));

  /* ------------------------------------------------------------- 白板挂载 */

  const scheduleSave = debounceSave();

  function debounceSave() {
    return () => {
      clearTimeout(model.saveTimer);
      model.saveTimer = setTimeout(saveNow, SAVE_DEBOUNCE_MS);
    };
  }

  function saveNow() {
    clearTimeout(model.saveTimer);
    model.saveTimer = 0;
    if (model.destroyed || !model.wb) return;
    if (IS_MOCK) return;   // 演示模式不穿透
    const board = model.wb.getBoard();
    request('/api/flow/board', {
      method: 'POST',
      body: { key: model.key, nodes: board.nodes, edges: board.edges },
      timeout: 15000
    }).then(() => {
      model.saveWarned = false;
    }).catch((err) => {
      if (model.destroyed) return;
      if (!model.saveWarned) {
        model.saveWarned = true;
        toast(errorText(err, '白板保存失败：改动还在页面上，稍后会自动重试'), { tone: 'danger' });
      }
      scheduleSave();
    });
  }

  function updateCounts() {
    if (!model.wb) return;
    const board = model.wb.getBoard();
    countEl.textContent = `节点 ${board.nodes.length} · 连线 ${board.edges.length}`;
  }

  function renderEmptyState() {
    const old = stage.querySelector('.flow-empty');
    if (old) old.remove();
    if (!model.wb) return;
    const board = model.wb.getBoard();
    if (board.nodes.length) return;
    stage.append(
      h('div', { class: 'flow-empty' },
        h('div', { class: 'flow-empty-card' },
          h('h3', { text: '从一张便签开始' }),
          h('p', { class: 'flow-empty-sub', text: '这是你的自由草稿区，内容只保存在本机。三步上手：' }),
          h('div', { class: 'flow-empty-steps' },
            EMPTY_STEPS.map((t, i) => h('div', { class: 'flow-empty-step' },
              h('span', { class: 'flow-step-no', text: String(i + 1) }),
              h('span', { text: t })
            ))
          )
        )
      )
    );
  }

  function renderBusy() {
    const old = view.querySelector('.flow-expand-busy');
    if (old) old.remove();
    if (model.expandBusy) {
      view.querySelector('.flow-toolbar').append(
        h('span', { class: 'flow-expand-busy', text: '✦ AI 正在发散想法…' })
      );
    }
    btnExpand.disabled = model.expandBusy;
  }

  function renderError(err) {
    if (!err) {
      errorBox.hidden = true;
      errorBox.textContent = '';
      return;
    }
    errorBox.hidden = false;
    errorBox.textContent = '';
    errorBox.append(
      h('span', { text: err }),
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        text: '重试',
        on: { click: () => loadBoard() }
      })
    );
  }

  function updateExpandButton() {
    const node = model.wb ? model.wb.getSelected() : null;
    const ok = node && (node.kind === 'note' || node.kind === 'ai');
    btnExpand.disabled = !ok || model.expandBusy;
    btnExpand.title = ok
      ? `围绕「${(node.title || '未命名').slice(0, 12)}」发散子想法`
      : '先选中一张便签（AI 节点也可以）';
  }

  model.wb = createWhiteboard(stage, {
    onChange: ({ persist }) => {
      if (!persist) return;
      updateCounts();
      renderEmptyState();
      scheduleSave();
    },
    onSelectionChange: () => updateExpandButton(),
    onChapterOpen: (node) => {
      if (!model.key) {
        toast('章卡引用需要归属一本书：先在右上角选书，再双击章卡跳转', { tone: 'warn' });
        return;
      }
      navigate(`/work/${encodeURIComponent(model.key)}/bench/${node.n || 1}`);
    },
    confirmNodeDelete: async (node, attached) => {
      const ok = await confirmDialog({
        title: '删除节点',
        message: `「${node.title || node.id}」上连着 ${attached} 条线，会一并删除。`,
        confirmText: '删除',
        danger: true
      });
      return Boolean(ok);
    }
  });

  /* ------------------------------------------------------------- 书选择 */

  function renderBookOptions() {
    const items = (store.get().library && store.get().library.items) || [];
    bookSelect.textContent = '';
    bookSelect.append(h('option', { value: '', text: '（全局白板，不归属作品）' }));
    for (const it of items) {
      bookSelect.append(h('option', { value: it.key, text: it.name || it.key, selected: it.key === model.key }));
    }
  }

  bookSelect.addEventListener('change', () => {
    switchBook(bookSelect.value);
  });

  function switchBook(key) {
    if (model.key === key) return;
    saveNow();   // 先把上一块板的改动落盘
    const next = reduceFlow({ key: model.key, loading: false, error: null, busy: false },
      { type: 'switchBook', key });
    model.key = next.key;
    model.expandBusy = false;
    rememberBook(model.key);
    renderBookOptions();
    renderBusy();
    loadBoard();
  }

  function resolveInitialKey() {
    const remembered = recallBook();
    const items = (store.get().library && store.get().library.items) || [];
    if (remembered && (remembered === '' || items.some((i) => i.key === remembered))) {
      return remembered;
    }
    const cur = store.get().currentKey;
    if (cur && items.some((i) => i.key === cur)) return cur;
    return '';
  }

  /* ------------------------------------------------------------- 数据加载 */

  async function loadBoard() {
    if (model.destroyed) return;
    const key = model.key;
    renderError(null);
    if (IS_MOCK) {
      model.wb.setBoard({ nodes: [], edges: [] }, { fit: true });
      updateCounts();
      renderEmptyState();
      updateExpandButton();
      return;
    }
    try {
      const res = await request('/api/flow/board', { query: { key }, timeout: 15000 });
      if (model.destroyed || model.key !== key) return;
      model.wb.setBoard({ nodes: res.nodes || [], edges: res.edges || [] }, { fit: true });
      updateCounts();
      renderEmptyState();
      updateExpandButton();
    } catch (err) {
      if (model.destroyed || model.key !== key) return;
      renderError(errorText(err, '白板加载失败'));
    }
  }

  /* ------------------------------------------------------------- 新建便签 */

  btnNote.addEventListener('click', () => {
    if (!model.wb) return;
    model.wb.addNode({ kind: 'note', title: '新便签', text: '' });
    const node = model.wb.getSelected();
    if (node) model.wb.beginEdit(node.id);
  });

  /* ------------------------------------------------------------- AI 扩展 */

  async function expandSelected() {
    const node = model.wb ? model.wb.getSelected() : null;
    if (!node || (node.kind !== 'note' && node.kind !== 'ai')) {
      toast('先选中一张便签或 AI 节点，再点扩展', { tone: 'warn' });
      return;
    }
    if (model.expandBusy) return;
    model.expandBusy = true;
    renderBusy();
    try {
      const text = `${node.title || ''}\n${node.text || ''}`.trim() || node.title || '这个想法';
      const items = IS_MOCK
        ? expandMockItems(text, 4)
        : (await request('/api/flow/expand', {
            method: 'POST',
            body: { key: model.key, text, kind: 'note', count: 4 },
            timeout: 120000
          })).items || [];
      const added = applyExpand(node, items);
      toast(added ? `✦ 已发散 ${added} 个子想法` : 'AI 没有返回新的想法，稍后再试');
    } catch (err) {
      toast(errorText(err, 'AI 扩展失败'), { tone: 'danger', duration: 6000 });
    } finally {
      model.expandBusy = false;
      if (!model.destroyed) renderBusy();
    }
  }

  function applyExpand(parent, items) {
    /* buildExpandNodes 生成占位 id；落板时交给白板重新发号（避免撞号），
       连线按 idMap 重映射到真实节点 id。 */
    const additions = buildExpandNodes(parent, items);
    const idMap = new Map();
    let count = 0;
    for (const n of additions.nodes) {
      const { id, ...node } = n;
      const created = model.wb.addNode(node);
      idMap.set(id, (created && created.id) || id);
      count += 1;
    }
    for (const e of additions.edges) {
      const to = idMap.get(e.to) || e.to;
      if (parent.id && to && to !== parent.id) model.wb.addEdge(parent.id, to);
    }
    return count;
  }

  btnExpand.addEventListener('click', expandSelected);

  /* ------------------------------------------------------------- 导入章卡 */

  async function importChapters() {
    if (!model.wb) return;
    if (!model.key) {
      toast('章卡从作品里来：先在右上角选一本书，再导入', { tone: 'warn' });
      return;
    }
    let chapters = [];
    try {
      if (IS_MOCK) {
        chapters = [
          { n: 1, title: '雨夜裂痕', status: 'done', summary: '（演示）第一张章卡。' },
          { n: 2, title: '回溯', status: 'writing', summary: '（演示）第二张章卡。' }
        ];
      } else {
        const res = await request('/api/flow/chapters', { query: { key: model.key }, timeout: 15000 });
        chapters = res.chapters || [];
      }
    } catch (err) {
      toast(errorText(err, '章卡读取失败'), { tone: 'danger' });
      return;
    }
    if (!chapters.length) {
      toast('这本书还没有章卡：先在创作流程里规划章节', { tone: 'warn' });
      return;
    }
    openChapterPicker(chapters);
  }

  function openChapterPicker(chapters) {
    const picked = new Set(chapters.map((c) => c.n));
    const rows = chapters.map((c) => {
      const cb = h('input', { type: 'checkbox' });
      cb.checked = true;
      cb.addEventListener('change', () => {
        if (cb.checked) picked.add(c.n);
        else picked.delete(c.n);
        confirmBtn.disabled = picked.size === 0;
      });
      return h('label', { class: 'flow-import-row' },
        cb,
        h('span', { class: 'flow-import-no', text: `第${c.n}章` }),
        h('span', { class: 'flow-import-main' },
          h('div', { class: 'flow-import-title', text: c.title || '' }),
          (c.summary ? h('div', { class: 'flow-import-summary', text: c.summary }) : null)
        )
      );
    });
    const list = h('div', { class: 'flow-import-list' }, rows);
    const confirmBtn = h('button', { class: 'btn btn-primary', type: 'button', text: '导入到画布' });
    const modal = openModal({
      title: '导入章卡（只读引用）',
      subtitle: '勾选要摆上画布的章节；章卡内容只读，双击画布上的章卡可跳到对应章节工作台。',
      body: list,
      footer: h('div', { style: { display: 'flex', gap: '8px', justifyContent: 'flex-end', width: '100%' } },
        h('button', { class: 'btn', type: 'button', text: '取消', on: { click: () => modal.close() } }),
        confirmBtn),
      size: 'md'
    });
    confirmBtn.addEventListener('click', () => {
      const chosen = chapters.filter((c) => picked.has(c.n));
      const center = model.wb.centerWorld();
      const size = defaultSize('chapter');
      const origin = {
        x: Math.round(center.x - (CHAPTER_GRID.cols * CHAPTER_GRID.gapX) / 2 + size.w / 2),
        y: Math.round(center.y - (Math.ceil(chosen.length / CHAPTER_GRID.cols) * CHAPTER_GRID.gapY) / 2)
      };
      const nodes = chaptersToNodes(chosen, origin);
      for (const n of nodes) model.wb.addNode(n);
      modal.close();
      toast(`已导入 ${nodes.length} 张章卡到画布（只读）`);
    });
  }

  btnImport.addEventListener('click', importChapters);

  /* ------------------------------------------------------------- 重置视图 / 导出 */

  btnReset.addEventListener('click', () => {
    if (model.wb) model.wb.resetView();
  });

  btnExport.addEventListener('click', () => {
    if (!model.wb) return;
    const board = model.wb.getBoard();
    const payload = exportBoardJSON(board.nodes, board.edges, { key: model.key });
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = exportFileName(model.key);
    a.rel = 'noopener';
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast(`已导出 ${board.nodes.length} 个节点 / ${board.edges.length} 条连线`);
  });

  /* ------------------------------------------------------------- 装配 + 卸载 */

  renderBookOptions();
  model.key = resolveInitialKey();
  renderBookOptions();

  const unsub = store.subscribe((state) => {
    if (model.destroyed) return;
    const items = (state.library && state.library.items) || [];
    if (model.key && !items.some((i) => i.key === model.key)) {
      // 当前书被删/改名后回退全局板（改名后 key 由后端别名解析兜底，这里只处理删除）
      model.key = '';
      rememberBook('');
      renderBookOptions();
      loadBoard();
    } else {
      renderBookOptions();
    }
  });

  loadBoard();

  return function destroy() {
    model.destroyed = true;
    try {
      if (typeof unsub === 'function') unsub();
    } catch {
      /* 吞掉 */
    }
    saveNow();   // 卸载前把未落盘的改动冲一次（失败静默）
    clearTimeout(model.saveTimer);
    if (model.wb) {
      model.wb.destroy();
      model.wb = null;
    }
    view.remove();
  };
}

/* NODE_COLORS 保留在导出面：色板口径由 whiteboard.js 单一定义，视图/测试共用。 */
export { NODE_COLORS };
