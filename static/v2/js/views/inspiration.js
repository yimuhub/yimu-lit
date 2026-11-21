/* 灵感库全页（v5.4.x，docs/系统化升级方案-v5.1 第 8.4 节）：
 * 瀑布流卡片（置顶 / 展开 / 复制 / 删除 / 行内编辑）+ 热门标签侧栏（从现有条目聚合）+
 * 新建速记输入（#标签 语法与首页速记卡、后端 bot/inspirations.py 同口径）+ 滚动加载。
 * 数据走既有 /api/inspirations（GET/POST/DELETE），置顶与行内编辑走 webrank.py 补充端点。
 * 纯函数（editReducer / aggregateTags / extractTags）导出供 node 直跑测试。 */

import { h, icon, relTime, copyText } from '../core/dom.js';
import { request, errorText } from '../core/api.js';
import { toast, confirmDialog } from '../core/ui.js';

/* ---------------------------------------------------------------- 纯函数（导出供测试） */

/** 单条灵感字数上限（与后端 bot/inspirations.py::MAX_LEN 同口径）。 */
export const INSP_MAX_LEN = 300;

/* ``#标签`` 语法提取：与首页速记卡（views/welcome.js）、后端 bot/inspirations.py 三方同口径：
   # 后跟非空白/非常见标点的 1-20 字，后面必须紧跟边界（空白/#/标点/串尾）；
   超长标签整体不收；去重、最多 8 个。 */
const TAG_CHARS = '\\s#，。；！？、：\u201c\u201d\u2018\u2019（）【】《》…!?.,:;()';
const TAG_RE = new RegExp(`#([^${TAG_CHARS}]{1,20})(?=$|[${TAG_CHARS}])`, 'g');

export function extractTags(text) {
  const out = [];
  TAG_RE.lastIndex = 0;
  let m;
  while ((m = TAG_RE.exec(String(text || '')))) {
    const t = m[1].trim();
    if (t && !out.includes(t)) out.push(t);
    if (out.length >= 8) break;
  }
  return out;
}

/**
 * 行内编辑状态机（纯函数）。
 * state = { id, draft, saving, error }；id 为 null 表示没有卡片处于编辑态。
 * 动作：start{id,text} / input{value} / cancel / save-begin / save-done / save-error{message}。
 * 非法动作（无编辑对象时 input / save-begin / save-error）原样返回，绝不越界。
 */
export const EDIT_IDLE = { id: null, draft: '', saving: false, error: '' };

export function editReducer(state, action) {
  const s = state && typeof state === 'object' ? state : EDIT_IDLE;
  const type = action && action.type;
  switch (type) {
    case 'start':       /* 进入编辑：换 draft、清错误；再次 start 切换编辑对象 */
      return { id: String((action && action.id) || ''), draft: String((action && action.text) ?? ''), saving: false, error: '' };
    case 'input':       /* 输入：只改 draft 并清错误 */
      if (!s.id) return s;
      return { ...s, draft: String((action && action.value) ?? ''), error: '' };
    case 'cancel':      /* 取消：回空闲 */
      return EDIT_IDLE;
    case 'save-begin':  /* 提交中：锁保存键（防双击重复提交） */
      if (!s.id || s.saving) return s;
      return { ...s, saving: true, error: '' };
    case 'save-done':   /* 保存成功：回空闲 */
      return EDIT_IDLE;
    case 'save-error':  /* 保存失败：退回编辑态并留错误（草稿不丢） */
      if (!s.id) return s;
      return { ...s, saving: false, error: String((action && action.message) || '保存失败，请重试') };
    default:
      return s;
  }
}

/**
 * 热门标签聚合（纯函数）：条目 → [{tag, count}] 按次数降序（同数次按 zh 拼音序），取前 topN。
 */
export function aggregateTags(items, topN = 20) {
  const counts = new Map();
  for (const it of (Array.isArray(items) ? items : [])) {
    const tags = Array.isArray(it && it.tags) ? it.tags : [];
    for (const t of tags) {
      const key = String(t || '').trim();
      if (!key) continue;
      counts.set(key, (counts.get(key) || 0) + 1);
    }
  }
  return Array.from(counts.entries())
    .map(([tag, count]) => ({ tag, count }))
    .sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag, 'zh-Hans-CN'))
    .slice(0, Math.max(1, Number(topN) || 20));
}

/* ---------------------------------------------------------------- 视图 */

const PAGE_SIZE = 30;    // 滚动加载步长
const LOAD_LIMIT = 200;  // 服务端 /api/inspirations 单次上限（list_items 钳制）

export function renderInspiration(container) {
  const view = h('div', { class: 'insp-view' });
  container.append(view);

  const model = {
    items: [],          // 全量（≤200，服务端已按 置顶优先+创建倒序 排好）
    rendered: PAGE_SIZE,// 已渲染条数（滚动加载推进）
    activeTag: '',      // 标签筛选（空=全部）
    loading: false,
    loaded: false
  };
  let edit = { ...EDIT_IDLE };   // 行内编辑状态机当前状态

  /* ------------------------------------------------------------- 新建速记 */

  const newInput = h('textarea', {
    class: 'textarea insp-new-input',
    rows: '3',
    maxlength: String(INSP_MAX_LEN),
    placeholder: '记一条灵感…… 支持标签语法：#修仙 #灵感（与首页速记卡同口径）',
    'aria-label': '新建灵感'
  });
  const newCount = h('span', { class: 'insp-count', text: `0 / ${INSP_MAX_LEN}` });
  const newBtn = h('button', {
    class: 'btn btn-primary btn-sm', type: 'button', text: '记下',
    on: { click: () => addNew().catch(() => {}) }
  });

  newInput.addEventListener('input', () => paintNewCount());
  newInput.addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      event.preventDefault();
      addNew().catch(() => {});
    }
  });

  function paintNewCount() {
    const n = [...newInput.value].length;
    newCount.textContent = `${n} / ${INSP_MAX_LEN}`;
    newCount.classList.toggle('over', n > INSP_MAX_LEN);
  }

  async function addNew() {
    const text = newInput.value.trim();
    if (!text) { toast('先写一句灵感再记', { tone: 'danger' }); return; }
    if (text.length > INSP_MAX_LEN) {
      toast(`灵感最多 ${INSP_MAX_LEN} 字，当前 ${text.length} 字`, { tone: 'danger' });
      return;
    }
    newBtn.disabled = true;
    try {
      await request('/api/inspirations', { method: 'POST', body: { text }, timeout: 15000 });
      newInput.value = '';
      paintNewCount();
      await reload();
      toast('已记入灵感库', { tone: 'success' });
    } catch (err) {
      toast(errorText(err, '灵感保存失败'), { tone: 'danger' });
    }
    newBtn.disabled = false;
  }

  view.append(
    h('div', { class: 'insp-head' },
      h('h1', { class: 'insp-title', text: '灵感库' }),
      h('p', { class: 'insp-desc', text: '首页速记卡的全页版：瀑布流管理全部灵感，支持置顶、行内编辑与标签检索。' })
    ),
    h('div', { class: 'insp-new card-base' },
      newInput,
      h('div', { class: 'insp-new-foot' },
        h('span', { class: 'hint-text', text: '失焦不保存：点「记下」或 Cmd/Ctrl+Enter 提交；#标签 自动入账。' }),
        h('span', { style: { flex: '1' } }),
        newCount,
        newBtn
      )
    )
  );

  /* ------------------------------------------------------------- 主体（侧栏 + 瀑布流） */

  const tagListEl = h('div', { class: 'insp-tag-list' });
  const masonry = h('div', { class: 'insp-masonry' });
  const moreBtn = h('button', {
    class: 'btn btn-sm', type: 'button', text: '加载更多',
    on: { click: () => loadMore() }
  });
  const moreWrap = h('div', { class: 'insp-more', style: { display: 'none' } }, moreBtn);
  const sentinel = h('div', { class: 'insp-sentinel' });
  const emptyEl = h('div', { class: 'insp-empty', style: { display: 'none' } },
    h('div', { class: 'insp-empty-icon', html: icon('sparkles', 22) }),
    h('div', { class: 'insp-empty-text', text: '还没有灵感。写下一句，它会在你翻开的那天派上用场。' })
  );

  view.append(
    h('div', { class: 'insp-body' },
      h('div', { class: 'insp-side' },
        h('div', { class: 'insp-side-title', text: '热门标签' }),
        tagListEl
      ),
      h('div', { class: 'insp-main' }, masonry, moreWrap, sentinel, emptyEl)
    )
  );

  /* 滚动加载：哨兵进入视口就推进一页（观察器不可用时仍有「加载更多」按钮兜底）。 */
  let observer = null;
  try {
    if (typeof IntersectionObserver === 'function') {
      observer = new IntersectionObserver((entries) => {
        if (entries.some((e) => e.isIntersecting)) loadMore();
      }, { rootMargin: '240px' });
      observer.observe(sentinel);
    }
  } catch {
    observer = null;
  }

  function loadMore() {
    if (model.rendered >= filteredItems().length) return;
    model.rendered += PAGE_SIZE;
    paintCards();
  }

  function filteredItems() {
    if (!model.activeTag) return model.items;
    return model.items.filter((it) => (it.tags || []).includes(model.activeTag));
  }

  /* ------------------------------------------------------------- 标签侧栏 */

  function paintTags() {
    tagListEl.textContent = '';
    const tags = aggregateTags(model.items, 20);
    const all = h('button', {
      class: `insp-tag${model.activeTag === '' ? ' is-on' : ''}`,
      type: 'button', text: '全部',
      on: { click: () => { model.activeTag = ''; model.rendered = PAGE_SIZE; paintTags(); paintCards(); } }
    });
    tagListEl.append(all);
    for (const { tag, count } of tags) {
      tagListEl.append(h('button', {
        class: `insp-tag${model.activeTag === tag ? ' is-on' : ''}`,
        type: 'button',
        text: `#${tag} · ${count}`,
        on: {
          click: () => {
            model.activeTag = model.activeTag === tag ? '' : tag;
            model.rendered = PAGE_SIZE;
            paintTags();
            paintCards();
          }
        }
      }));
    }
    if (!tags.length) {
      tagListEl.append(h('div', { class: 'hint-text', text: '写几条带 #标签 的灵感后，这里会聚出风向。' }));
    }
  }

  /* ------------------------------------------------------------- 瀑布流卡片 */

  function paintCards() {
    masonry.textContent = '';
    const items = filteredItems();
    const shown = items.slice(0, model.rendered);
    for (const it of shown) masonry.append(inspCard(it));
    moreWrap.style.display = items.length > model.rendered ? '' : 'none';
    emptyEl.style.display = model.loaded && !items.length ? '' : 'none';
    if (model.activeTag && items.length) {
      masonry.append(h('div', { class: 'insp-filter-note', text: `按 #${model.activeTag} 筛选：${items.length} 条（点侧栏标签取消）` }));
    }
  }

  function inspCard(it) {
    const card = h('div', { class: `card-base insp-card${it.pinned ? ' is-pinned' : ''}`, dataset: { id: it.id } });

    /* 行内编辑态：textarea + 计数 + 保存/取消（状态机 edit 驱动） */
    if (edit.id === it.id) {
      const ta = h('textarea', {
        class: 'textarea insp-edit-input', rows: '4', maxlength: String(INSP_MAX_LEN),
        'aria-label': '编辑灵感'
      });
      ta.value = edit.draft;
      const cnt = h('span', { class: 'insp-count' });
      const syncCnt = () => {
        cnt.textContent = `${[...ta.value].length} / ${INSP_MAX_LEN}`;
        cnt.classList.toggle('over', [...ta.value].length > INSP_MAX_LEN);
      };
      syncCnt();
      ta.addEventListener('input', () => {
        edit = editReducer(edit, { type: 'input', value: ta.value });
        syncCnt();
      });
      const saveEl = h('button', {
        class: 'btn btn-primary btn-sm', type: 'button', text: '保存',
        on: { click: () => saveEdit(it, ta.value).catch(() => {}) }
      });
      const cancelEl = h('button', {
        class: 'btn btn-sm', type: 'button', text: '取消',
        on: { click: () => { edit = editReducer(edit, { type: 'cancel' }); paintCards(); } }
      });
      card.append(
        ta,
        h('div', { class: 'insp-edit-foot' },
          h('span', { class: 'insp-edit-error', text: edit.error || '' }),
          h('span', { style: { flex: '1' } }),
          cnt, saveEl, cancelEl
        )
      );
      if (edit.error) saveEl.disabled = false;
      if (edit.saving) saveEl.disabled = true;
      requestAnimationFrame(() => { try { ta.focus(); } catch { /* jsdom 无 focus */ } });
      return card;
    }

    /* 阅读态 */
    const textEl = h('div', {
      class: `insp-text${it.expanded ? ' is-expanded' : ''}`,
      on: { click: () => { it.expanded = !it.expanded; paintCards(); } },
      title: '点击展开/收起'
    });
    textEl.textContent = it.text;

    const tagRow = h('div', { class: 'insp-tags' });
    for (const t of (it.tags || [])) {
      tagRow.append(h('button', {
        class: 'insp-tag insp-tag-mini', type: 'button', text: `#${t}`,
        on: { click: (ev) => { ev.stopPropagation(); model.activeTag = model.activeTag === t ? '' : t; model.rendered = PAGE_SIZE; paintTags(); paintCards(); } }
      }));
    }

    async function act(fn) {
      try {
        await fn();
        await reload();
      } catch (err) {
        toast(errorText(err, '操作失败'), { tone: 'danger' });
      }
    }

    const metaEl = h('div', { class: 'insp-meta' },
      h('span', { text: relTime(it.created) }),
      it.updated && it.updated !== it.created ? h('span', { class: 'sep', text: '已编辑' }) : null
    );
    /* 原生 append 不吃 null（会渲染成 "null" 文本）：先收集再过滤 */
    const kids = [
      h('div', { class: 'insp-card-top' },
        it.pinned ? h('span', { class: 'insp-pin-flag', html: `${icon('pin', 12)}`, title: '置顶' }) : null,
        h('span', { style: { flex: '1' } }),
        /* 动作条：置顶 / 编辑 / 复制 / 删除 */
        h('button', {
          class: 'icon-btn insp-act', type: 'button', title: it.pinned ? '取消置顶' : '置顶',
          html: icon('pin', 14),
          on: { click: () => act(async () => { await request(`/api/inspirations/${it.id}/pin`, { method: 'POST', body: { pinned: !it.pinned }, timeout: 15000 }); }) }
        }),
        h('button', {
          class: 'icon-btn insp-act', type: 'button', title: '编辑',
          html: icon('edit', 14),
          on: { click: () => { edit = editReducer(edit, { type: 'start', id: it.id, text: it.text }); paintCards(); } }
        }),
        h('button', {
          class: 'icon-btn insp-act', type: 'button', title: '复制',
          html: icon('copy', 14),
          on: {
            click: async () => {
              try {
                await copyText(it.text);
                toast('已复制到剪贴板', { tone: 'success', duration: 1800 });
              } catch {
                toast('复制失败，请手动选择文本', { tone: 'danger' });
              }
            }
          }
        }),
        h('button', {
          class: 'icon-btn insp-act insp-act-danger', type: 'button', title: '删除',
          html: icon('trash', 14),
          on: {
            click: async () => {
              const ok = await confirmDialog({
                title: '删除这条灵感？',
                message: it.text.slice(0, 40) + (it.text.length > 40 ? '…' : ''),
                confirmText: '删除',
                danger: true
              });
              if (!ok) return;
              await act(async () => { await request(`/api/inspirations/${it.id}`, { method: 'DELETE', timeout: 15000 }); });
            }
          }
        })
      ),
      textEl,
      (it.tags || []).length ? tagRow : null,
      metaEl
    ];
    for (const k of kids.flat()) {
      if (k) card.append(k);
    }
    return card;
  }

  async function saveEdit(it, draft) {
    const text = String(draft || '').trim();
    if (!text) {
      edit = editReducer(edit, { type: 'save-error', message: '内容不能为空' });
      paintCards();
      return;
    }
    edit = editReducer(edit, { type: 'save-begin' });
    paintCards();
    try {
      const r = await request(`/api/inspirations/${it.id}`, {
        method: 'PUT', body: { text }, timeout: 15000
      });
      /* 保存成功：服务端重提取了 #标签，用返回条目原位替换 */
      const idx = model.items.findIndex((x) => x.id === it.id);
      if (idx >= 0 && r && r.item) model.items[idx] = { ...model.items[idx], ...r.item };
      edit = editReducer(edit, { type: 'save-done' });
      paintTags();
      paintCards();
      toast('已保存修改', { tone: 'success', duration: 1800 });
    } catch (err) {
      edit = editReducer(edit, { type: 'save-error', message: errorText(err, '保存失败，请重试') });
      paintCards();
    }
  }

  /* ------------------------------------------------------------- 数据装载 */

  async function reload() {
    model.loading = true;
    try {
      const r = await request('/api/inspirations', { query: { limit: LOAD_LIMIT }, timeout: 15000 });
      model.items = (r && r.items) || [];
      model.loaded = true;
    } catch (err) {
      if (!model.loaded) {
        model.items = [];
        emptyEl.style.display = '';
        emptyEl.querySelector('.insp-empty-text').textContent =
          `灵感库读取失败：${errorText(err, '请确认服务端已挂载 webrank 路由')}`;
      }
      model.loading = false;
      paintTags();
      paintCards();
      return;
    }
    model.loading = false;
    paintTags();
    paintCards();
  }

  /* ------------------------------------------------------------ start */

  paintTags();
  paintCards();
  reload();

  return {
    el: view,
    destroy() {
      if (observer) { try { observer.disconnect(); } catch { /* ignore */ } }
      view.remove();
    }
  };
}
