/* 作品库：视图（全部/归档/回收站）、筛选、排序、批量操作、收藏/归档/恢复/彻底删除/导出。 */

import {
  h,
  icon,
  fmtWords,
  fmtNum,
  fmtDate,
  initials,
  statusMeta,
  mediumChip,
  MEDIUM_LABELS,
  MEDIUM_UNITS,
  debounce,
  downloadUrl
} from '../core/dom.js';
import { store } from '../core/state.js';
import { api, errorText, IS_MOCK } from '../core/api.js';
import { toast, popoverMenu, openModal } from '../core/ui.js';
import { openProject } from '../core/router.js';

const SORTS = [
  { value: 'updated', label: '最近更新' },
  { value: 'created', label: '创建时间' },
  { value: 'words', label: '字数' },
  { value: 'quality', label: '评分' },
  { value: 'progress', label: '进度' }
];

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'planning', label: '规划中' },
  { value: 'in_progress', label: '创作中' },
  { value: 'completed', label: '已完成' },
  { value: 'passing', label: '质量达标' },
  { value: 'unrated', label: '未评分' }
];

/* 三档视图对应后端 view=active|archived|trash，切换时整页重取，避免残留上一视图条目。 */
const VIEWS = [
  { value: 'active', label: '全部' },
  { value: 'archived', label: '归档' },
  { value: 'trash', label: '回收站' }
];

const EMPTY_TEXT = {
  active: {
    title: (q) => (q ? '没有匹配的作品' : '还没有作品'),
    hint: (q) => (q ? '换个关键词，或清空筛选条件。' : '从一句话想法开始，第一本三分钟就能立项。')
  },
  archived: {
    title: (q) => (q ? '没有匹配的归档作品' : '还没有归档作品'),
    hint: (q) => (q ? '换个关键词，或清空筛选条件。' : '归档的作品会出现在这里，随时可以取消归档。')
  },
  trash: {
    title: (q) => (q ? '没有匹配的回收站作品' : '回收站是空的'),
    hint: (q) => (q ? '换个关键词，或清空筛选条件。' : '移入回收站的作品会出现在这里，可以随时恢复。')
  }
};

export function renderLibrary(container, ctx) {
  const view = h('div', { class: 'view-scroll' });
  const pad = h('div', { class: 'view-pad' });
  view.append(pad);
  container.append(view);

  const model = {
    view: 'active',
    q: '',
    genre: '',
    medium: '',
    status: '',
    sort: 'updated',
    order: 'desc',
    mode: localStorage.getItem('shenbi.v2.libView') || 'grid',
    items: [],
    stats: {},
    facets: {},
    counts: {},
    selected: new Set(),
    loading: true,
    error: ''
  };

  /* ------------------------------------------------------------- head */

  const head = h(
    'div',
    { class: 'lib-head' },
    h(
      'div',
      { class: 'lib-head-copy' },
      h('h1', { text: '作品库' }),
      h('p', { class: 'hint-text', text: '全部作品、评分与导出。数据都在本地，随时可以打包带走。' })
    ),
    h(
      'div',
      { class: 'lib-toolbar' },
      h(
        'button',
        {
          class: 'btn btn-primary',
          type: 'button',
          html: `${icon('plus', 14)}<span>新建作品</span>`,
          on: { click: () => ctx.openWizard() }
        }
      )
    )
  );
  pad.append(head);

  const viewsRow = h('div', { class: 'lib-views' });
  pad.append(viewsRow);

  const statsRow = h('div', { class: 'lib-stats' });
  pad.append(statsRow);

  const filterRow = h('div', { class: 'lib-toolbar' });
  pad.append(filterRow);

  const batchBar = h('div', { class: 'lib-batch', hidden: true });
  pad.append(batchBar);

  const listWrap = h('div', { class: model.mode === 'grid' ? 'lib-grid' : 'lib-list' });
  pad.append(listWrap);

  /* ------------------------------------------------------------ logic */

  let loadSeq = 0;

  async function load() {
    const seq = ++loadSeq;
    model.loading = true;
    model.error = '';
    paintList();
    try {
      const res = await api.library({
        view: model.view,
        q: model.q,
        genre: model.genre,
        medium: model.medium,
        status: model.status,
        sort: model.sort,
        order: model.order
      });
      if (seq !== loadSeq) return;
      model.items = (res && res.items) || [];
      model.stats = (res && res.stats) || {};
      model.facets = (res && res.facets) || {};
      /* counts 可能缺省（旧后端），缺省时按当前视图条目数兜底，不阻塞渲染。 */
      model.counts = (res && res.counts) || model.counts || {};
      pruneSelection();
    } catch (err) {
      if (seq !== loadSeq) return;
      model.items = [];
      model.selected.clear();
      model.error = errorText(err, '作品库加载失败');
      paintBatchBar();
    }
    if (seq !== loadSeq) return;
    model.loading = false;
    paintStats();
    paintViews();
    paintFilters();
    paintList();
  }

  function pruneSelection() {
    const keys = new Set(model.items.map((i) => i.key));
    for (const key of Array.from(model.selected)) {
      if (!keys.has(key)) model.selected.delete(key);
    }
    paintBatchBar();
  }

  function viewCount(name) {
    const counts = model.counts || {};
    const raw = counts[name];
    if (raw !== undefined && raw !== null) return Number(raw) || 0;
    if (name === model.view) return model.items.length;
    return null;
  }

  function paintViews() {
    viewsRow.textContent = '';
    const seg = h('div', { class: 'segmented', role: 'tablist', 'aria-label': '作品视图' });
    for (const v of VIEWS) {
      const count = viewCount(v.value);
      const btn = h('button', {
        class: 'seg-btn',
        type: 'button',
        role: 'tab',
        'aria-pressed': model.view === v.value ? 'true' : 'false',
        'aria-selected': model.view === v.value ? 'true' : 'false'
      });
      btn.append(h('span', { text: v.label }));
      if (count !== null) btn.append(h('span', { class: 'lib-tab-count', text: String(count) }));
      btn.addEventListener('click', () => setView(v.value));
      seg.append(btn);
    }
    viewsRow.append(seg);
  }

  function setView(next) {
    if (model.view === next) return;
    model.view = next;
    model.status = '';
    model.items = [];
    model.selected.clear();
    paintViews();
    paintStats();
    paintFilters();
    paintBatchBar();
    listWrap.textContent = '';
    load();
  }

  function paintStats() {
    statsRow.textContent = '';
    const s = model.stats || {};
    const cells = [
      ['作品', fmtNum(s.count ?? model.items.length), '部'],
      ['总字数', fmtWords(s.total_words || 0), ''],
      ['质量达标', fmtNum(s.passing ?? 0), '部'],
      ['平均评分', s.avg_quality != null ? s.avg_quality : '—', s.avg_quality != null ? '分' : '']
    ];
    for (const [k, v, unit] of cells) {
      statsRow.append(
        h(
          'div',
          { class: 'stat-card' },
          h('span', { class: 'stat-k', text: k }),
          h('span', { class: 'stat-v' }, `${v}`, unit ? h('small', { text: unit }) : null)
        )
      );
    }
  }

  function paintFilters() {
    filterRow.textContent = '';
    const searchInput = h('input', {
      class: 'input',
      type: 'search',
      placeholder: '搜索书名或题材…',
      value: model.q,
      'aria-label': '搜索作品'
    });
    searchInput.addEventListener('input', () => {
      model.q = searchInput.value.trim();
      runSearch();
    });
    const search = h('div', { class: 'lib-search', html: icon('search', 14) }, searchInput);

    const mediumSel = h('select', { class: 'select', 'aria-label': '文体筛选' });
    mediumSel.append(h('option', { value: '', text: '全部文体' }));
    for (const m of model.facets.mediums || ['novel']) {
      mediumSel.append(h('option', {
        value: m,
        text: MEDIUM_LABELS[m] || m,
        selected: m === model.medium
      }));
    }
    mediumSel.addEventListener('change', () => {
      model.medium = mediumSel.value;
      load();
    });

    const genreSel = h('select', { class: 'select', 'aria-label': '题材筛选' });
    genreSel.append(h('option', { value: '', text: '全部题材' }));
    for (const g of model.facets.genres || []) {
      genreSel.append(h('option', { value: g, text: g, selected: g === model.genre }));
    }
    genreSel.addEventListener('change', () => {
      model.genre = genreSel.value;
      load();
    });

    const statusSel = h('select', { class: 'select', 'aria-label': '状态筛选' });
    for (const opt of STATUS_OPTIONS) {
      statusSel.append(h('option', { value: opt.value, text: opt.label, selected: opt.value === model.status }));
    }
    statusSel.addEventListener('change', () => {
      model.status = statusSel.value;
      load();
    });

    const sortSel = h('select', { class: 'select', 'aria-label': '排序' });
    for (const opt of SORTS) {
      sortSel.append(h('option', { value: opt.value, text: `按${opt.label}`, selected: opt.value === model.sort }));
    }
    sortSel.addEventListener('change', () => {
      model.sort = sortSel.value;
      load();
    });

    const orderBtn = h('button', {
      class: 'btn btn-sm',
      type: 'button',
      title: model.order === 'desc' ? '降序，点击切换升序' : '升序，点击切换降序',
      html: icon(model.order === 'desc' ? 'arrow-down' : 'arrow-up', 14),
      on: {
        click: () => {
          model.order = model.order === 'desc' ? 'asc' : 'desc';
          load();
        }
      }
    });

    const seg = h(
      'div',
      { class: 'segmented', role: 'group', 'aria-label': '视图切换' },
      h('button', {
        class: 'seg-btn',
        type: 'button',
        'aria-pressed': model.mode === 'grid' ? 'true' : 'false',
        title: '网格视图',
        html: icon('grid', 14),
        on: { click: () => setMode('grid') }
      }),
      h('button', {
        class: 'seg-btn',
        type: 'button',
        'aria-pressed': model.mode === 'list' ? 'true' : 'false',
        title: '列表视图',
        html: icon('list', 14),
        on: { click: () => setMode('list') }
      })
    );

    filterRow.append(search, mediumSel, genreSel, statusSel, sortSel, orderBtn, h('span', { class: 'lib-spacer' }), seg);
  }

  function setMode(mode) {
    model.mode = mode;
    try {
      localStorage.setItem('shenbi.v2.libView', mode);
    } catch {
      /* storage may be unavailable */
    }
    listWrap.className = mode === 'grid' ? 'lib-grid' : 'lib-list';
    paintFilters();
    paintList();
  }

  const runSearch = debounce(() => load(), 280);

  /* ------------------------------------------------------------ cards */

  /* 章节工作台与导出走的是小说接口（02 计划 + /api/export），非小说作品不显示。 */
  function rowActions(item) {
    const isTrash = model.view === 'trash' || item.trashed;
    const novelOnly = !isTrash && (!item.medium || item.medium === 'novel');
    const viewActions =
      model.view === 'archived'
        ? [
            {
              label: '取消归档',
              icon: 'archive',
              onSelect: () => archiveItem(item, false)
            },
            {
              label: '移到回收站',
              icon: 'trash',
              onSelect: () => trashItem(item)
            }
          ]
        : model.view === 'trash'
          ? [
              {
                label: '恢复',
                icon: 'refresh',
                onSelect: () => restoreItem(item)
              },
              {
                label: '彻底删除',
                icon: 'trash',
                danger: true,
                onSelect: () => purgeDialog(item)
              }
            ]
          : [
              {
                label: '归档',
                icon: 'archive',
                onSelect: () => archiveItem(item, true)
              },
              {
                label: '移到回收站',
                icon: 'trash',
                onSelect: () => trashItem(item)
              }
            ];
    return [
      ...(isTrash
        ? []
        : [
            {
              label: '打开作品',
              icon: 'book-open',
              onSelect: () => openProject(item)
            }
          ]),
      ...(novelOnly
        ? [
            {
              label: '章节工作台',
              icon: 'edit',
              onSelect: () => ctx.openBench(Math.min((item.done || 0) + 1, item.total || 1))
            }
          ]
        : []),
      { separator: true },
      {
        label: item.favorite ? '取消收藏' : '收藏',
        icon: 'star',
        onSelect: () => toggleFavorite(item)
      },
      /* 回收站里不允许改名/导出，作品已不可编辑。 */
      ...(!isTrash
        ? [
            {
              label: '重命名',
              icon: 'edit',
              onSelect: () => renameDialog(item)
            }
          ]
        : []),
      ...(novelOnly
        ? [
            {
              label: '导出',
              icon: 'download',
              onSelect: (event) => exportMenu(event, item)
            }
          ]
        : []),
      { separator: true },
      ...viewActions
    ];
  }

  function exportMenu(anchor, item) {
    const items = ['md', 'docx', 'epub'].map((fmt) => ({
      label: `导出 ${fmt.toUpperCase()}`,
      icon: 'download',
      onSelect: () => {
        if (IS_MOCK) {
          const blob = new Blob([`《${item.name}》（${fmt.toUpperCase()} 导出演示内容）`], { type: 'text/plain;charset=utf-8' });
          const url = URL.createObjectURL(blob);
          downloadUrl(url);
          setTimeout(() => URL.revokeObjectURL(url), 4000);
        } else {
          downloadUrl(api.exportUrl(item.key, fmt));
        }
        toast(`正在导出 ${fmt.toUpperCase()}…`, { duration: 2000 });
      }
    }));
    popoverMenu(anchor, items, { label: `导出《${item.name}》` });
  }

  async function toggleFavorite(item) {
    try {
      await api.favorite(item.key, !item.favorite);
      item.favorite = !item.favorite;
      toast(item.favorite ? '已收藏' : '已取消收藏', { duration: 1600 });
      paintList();
      ctx.refreshLibrary();
    } catch (err) {
      toast(errorText(err, '操作失败'), { tone: 'danger' });
    }
  }

  async function archiveItem(item, on) {
    try {
      await api.archive(item.key, on);
      model.selected.delete(item.key);
      toast(on ? '已归档，可在作品库的「归档」页找到' : '已取消归档，作品已回到作品库', {
        tone: 'success',
        duration: 2400
      });
      await load();
      ctx.refreshLibrary();
    } catch (err) {
      toast(errorText(err, on ? '归档失败' : '取消归档失败'), { tone: 'danger' });
    }
  }

  async function trashItem(item) {
    try {
      await api.trash(item.key);
      model.selected.delete(item.key);
      toast('已移入回收站，可在作品库的回收站找回', { tone: 'success', duration: 2600 });
      await load();
      ctx.refreshLibrary();
    } catch (err) {
      toast(errorText(err, '移入回收站失败'), { tone: 'danger' });
    }
  }

  async function restoreItem(item) {
    try {
      await api.restore(item.key);
      model.selected.delete(item.key);
      toast('已恢复，作品已回到作品库', { tone: 'success', duration: 2400 });
      await load();
      ctx.refreshLibrary();
    } catch (err) {
      toast(errorText(err, '恢复失败'), { tone: 'danger' });
    }
  }

  /* ------------------------------------------------------- batch bar */

  const BATCH_OPS = {
    archive: { label: '归档', text: (n) => `已归档 ${n} 部作品，可在作品库的「归档」页找到` },
    unarchive: { label: '取消归档', text: (n) => `已取消归档 ${n} 部作品，作品已回到作品库` },
    trash: { label: '移到回收站', text: (n) => `已将 ${n} 部作品移入回收站，可在作品库的回收站找回` },
    restore: { label: '恢复', text: (n) => `已恢复 ${n} 部作品，作品已回到作品库` }
  };

  function batchOpIds() {
    if (model.view === 'archived') return ['unarchive', 'trash'];
    if (model.view === 'trash') return ['restore'];
    return ['archive', 'trash'];
  }

  function paintBatchBar() {
    batchBar.textContent = '';
    const keys = Array.from(model.selected);
    if (!keys.length) {
      batchBar.hidden = true;
      return;
    }
    batchBar.hidden = false;
    const allSelected = model.items.length > 0 && model.items.every((i) => model.selected.has(i.key));
    batchBar.append(
      h('span', { class: 'lib-batch-count', text: `已选 ${keys.length} 部` }),
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        text: allSelected ? '取消全选' : '全选',
        on: {
          click: () => {
            if (allSelected) model.selected.clear();
            else for (const item of model.items) model.selected.add(item.key);
            paintList();
            paintBatchBar();
          }
        }
      }),
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        text: '清空选择',
        on: {
          click: () => {
            model.selected.clear();
            paintList();
            paintBatchBar();
          }
        }
      }),
      h('span', { class: 'lib-spacer' })
    );
    for (const id of batchOpIds()) {
      const op = BATCH_OPS[id];
      batchBar.append(
        h('button', {
          class: 'btn btn-sm',
          type: 'button',
          text: op.label,
          on: { click: () => runBatch(id) }
        })
      );
    }
  }

  async function runBatch(action) {
    const keys = Array.from(model.selected);
    if (!keys.length) return;
    const op = BATCH_OPS[action] || { label: '操作', text: (n) => `已处理 ${n} 部作品` };
    try {
      await api.batch(keys, action);
      model.selected.clear();
      toast(op.text(keys.length), { tone: 'success', duration: 2800 });
      await load();
      ctx.refreshLibrary();
    } catch (err) {
      toast(errorText(err, `${op.label}失败`), { tone: 'danger' });
    }
  }

  function renameDialog(item) {
    const input = h('input', { class: 'input', value: item.name || '', maxlength: '60', 'aria-label': '新书名' });
    const body = h('div', {}, input);
    const footer = h(
      'div',
      { class: 'lib-dialog-foot' },
      h('button', { class: 'btn', type: 'button', text: '取消', on: { click: () => modal.close() } }),
      h('button', {
        class: 'btn btn-primary',
        type: 'button',
        text: '保存',
        dataset: { autofocus: '' },
        on: {
          click: async () => {
            const name = input.value.trim();
            if (!name) {
              toast('书名不能为空', { tone: 'danger' });
              return;
            }
            try {
              await api.rename(item.key, name);
              toast('已重命名', { tone: 'success', duration: 1600 });
              modal.close();
              load();
              ctx.refreshLibrary();
            } catch (err) {
              toast(errorText(err, '重命名失败'), { tone: 'danger' });
            }
          }
        }
      })
    );
    const modal = openModal({ title: '重命名作品', body, footer, size: 'sm' });
  }

  /* 彻底删除：二次确认 + 手动输入完整作品名，名字不匹配时按钮禁用、不发请求。 */
  function purgeDialog(item) {
    const expected = String(item.name || '').trim();
    const input = h('input', {
      class: 'input',
      type: 'text',
      autocomplete: 'off',
      spellcheck: 'false',
      placeholder: `请输入：${expected}`,
      'aria-label': '输入完整作品名以确认彻底删除'
    });
    const error = h('div', { class: 'error-text', role: 'alert' });
    const confirmBtn = h('button', {
      class: 'btn btn-danger',
      type: 'button',
      text: '彻底删除',
      disabled: true,
      on: { click: () => submit() }
    });
    const modal = openModal({
      title: '彻底删除作品',
      body: h(
        'div',
        { class: 'confirm-body' },
        h('div', { html: `确定要彻底删除 <strong>《${escapeText(expected)}》</strong> 吗？` }),
        h('div', { class: 'warn-note', text: '删除后作品目录与全部章节都会被移除，无法恢复。' }),
        h('div', { class: 'lib-confirm-label' }, h('span', { class: 'field-label', text: '输入完整作品名以确认：' })),
        h('div', { class: 'lib-confirm-name', text: expected }),
        input,
        error
      ),
      footer: h(
        'div',
        { class: 'lib-dialog-foot' },
        h('button', { class: 'btn', type: 'button', text: '取消', on: { click: () => modal.close() } }),
        confirmBtn
      ),
      size: 'sm',
      initialFocus: input
    });

    let busy = false;
    function sync() {
      const typed = input.value.trim();
      const matched = typed === expected;
      confirmBtn.disabled = busy || !matched;
      error.textContent = typed && !matched ? '作品名不一致，请完整输入后重试' : '';
    }
    input.addEventListener('input', sync);
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !confirmBtn.disabled) {
        event.preventDefault();
        submit();
      }
    });

    async function submit() {
      const typed = input.value.trim();
      if (busy || typed !== expected) return;
      busy = true;
      confirmBtn.disabled = true;
      confirmBtn.textContent = '删除中…';
      try {
        await api.purge(item.key, typed);
        modal.close();
        model.selected.delete(item.key);
        toast('已彻底删除，无法恢复', { duration: 2200 });
        await load();
        ctx.refreshLibrary();
      } catch (err) {
        busy = false;
        confirmBtn.textContent = '彻底删除';
        sync();
        toast(errorText(err, '删除失败'), { tone: 'danger' });
      }
    }
  }

  function escapeText(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function paintList() {
    listWrap.textContent = '';
    if (model.loading) {
      const wrap = h('div', { class: 'lib-loading' });
      const n = model.mode === 'grid' ? 6 : 5;
      for (let i = 0; i < n; i += 1) {
        wrap.append(
          h(
            'div',
            { class: model.mode === 'grid' ? 'lib-card' : 'lib-row' },
            h('div', { class: 'skeleton lib-skel-cover' }),
            h(
              'div',
              { class: 'lib-skel-body' },
              h('div', { class: 'skeleton skeleton-line', style: { width: `${45 + ((i * 13) % 35)}%` } }),
              h('div', { class: 'skeleton skeleton-line w-40' })
            )
          )
        );
      }
      listWrap.append(wrap);
      return;
    }
    if (model.error) {
      listWrap.append(
        h(
          'div',
          { class: 'lib-empty empty-state', role: 'alert' },
          h('div', { class: 'empty-icon', html: icon('alert', 18) }),
          h('h3', { text: '作品库没有加载出来' }),
          h('p', { text: model.error }),
          h('button', { class: 'btn btn-sm', type: 'button', text: '重试', on: { click: load } })
        )
      );
      return;
    }
    if (!model.items.length) {
      const copy = EMPTY_TEXT[model.view] || EMPTY_TEXT.active;
      listWrap.append(
        h(
          'div',
          { class: 'lib-empty empty-state' },
          h('div', { class: 'empty-icon', html: icon(model.view === 'trash' ? 'trash' : 'library', 18) }),
          h('h3', { text: copy.title(model.q) }),
          h('p', { text: copy.hint(model.q) }),
          model.view === 'active'
            ? h('button', {
                class: 'btn btn-primary btn-sm',
                type: 'button',
                text: model.q ? '新建作品' : '新建第一部作品',
                on: { click: () => ctx.openWizard() }
              })
            : null
        )
      );
      return;
    }

    for (const item of model.items) {
      listWrap.append(model.mode === 'grid' ? gridCard(item) : listRow(item));
    }
  }

  function qualityChip(item) {
    if (item.quality == null) return null;
    const passing = item.passing === true;
    return h('span', {
      class: 'badge',
      dataset: { tone: passing ? 'success' : 'muted' },
      text: `${item.quality} 分${passing ? ' · 达标' : ''}`
    });
  }

  function progressOf(item) {
    return item.total ? Math.round((item.done / item.total) * 100) : 0;
  }

  function selectBox(item) {
    const box = h('input', {
      class: 'lib-check',
      type: 'checkbox',
      'aria-label': `选择《${item.name}》`,
      checked: model.selected.has(item.key)
    });
    box.addEventListener('click', (event) => event.stopPropagation());
    box.addEventListener('change', () => {
      if (box.checked) model.selected.add(item.key);
      else model.selected.delete(item.key);
      paintBatchBar();
    });
    return box;
  }

  function gridCard(item) {
    const meta = statusMeta(item.state, item.status_label);
    const pct = progressOf(item);
    const unit = MEDIUM_UNITS[item.medium || 'novel'] || '章';
    const card = h('div', { class: 'lib-card', dataset: { key: item.key } });
    card.append(
      h(
        'div',
        { class: 'lib-card-top' },
        h(
          'div',
          { class: 'lib-cover' },
          h('span', { text: initials(item.name) }),
          item.favorite
            ? h('span', { class: 'lib-star', html: icon('star', 13), title: '已收藏' })
            : null
        ),
        h(
          'div',
          { class: 'lib-main' },
          h(
            'div',
            { class: 'lib-name-line' },
            h('span', { class: 'lib-name', text: item.name }),
            mediumChip(item.medium),
            qualityChip(item),
            selectBox(item)
          ),
          h(
            'div',
            { class: 'lib-meta' },
            h('span', { text: item.genre || '未分类' }),
            h('span', { class: 'sep' }),
            h('span', { class: `dot dot-${meta.tone}` }),
            h('span', { text: meta.label }),
            h('span', { class: 'sep' }),
            h('span', { text: `${item.done}/${item.total} ${unit}` })
          ),
          h(
            'div',
            { class: 'lib-progress' },
            h('span', { class: 'progress-track', html: `<span style="width:${pct}%"></span>` }),
            h('span', { class: 'pct', text: `${pct}%` })
          ),
          h(
            'div',
            { class: 'lib-updated' },
            h('span', { text: fmtWords(item.total_words) }),
            h('span', { class: 'sep' }),
            h('span', { text: fmtDate(item.updated_at) })
          )
        )
      ),
      h(
        'div',
        { class: 'row-actions' },
        h('button', {
          class: 'icon-btn icon-btn-sm',
          type: 'button',
          'aria-label': '更多操作',
          html: icon('more', 15),
          on: {
            click: (event) => {
              event.stopPropagation();
              popoverMenu(event.currentTarget, rowActions(item), { align: 'right' });
            }
          }
        })
      )
    );
    card.addEventListener('click', () => {
      if (model.view !== 'trash') openProject(item);
    });
    return card;
  }

  function listRow(item) {
    const meta = statusMeta(item.state, item.status_label);
    const pct = progressOf(item);
    const unit = MEDIUM_UNITS[item.medium || 'novel'] || '章';
    const row = h('div', { class: 'lib-row', dataset: { key: item.key } });
    row.append(
      selectBox(item),
      h('span', { class: 'lib-cover', text: initials(item.name) }),
      h(
        'div',
        { class: 'lib-main' },
        h(
          'div',
          { class: 'lib-name-line' },
          h('span', { class: 'lib-name', text: item.name }),
          mediumChip(item.medium),
          item.favorite ? h('span', { class: 'lib-star', html: icon('star', 12) }) : null
        ),
        h(
          'div',
          { class: 'lib-meta' },
          h('span', { text: item.genre || '未分类' }),
          h('span', { class: `dot dot-${meta.tone}` }),
          h('span', { text: meta.label }),
          h('span', { text: `${item.done}/${item.total} ${unit} · ${fmtWords(item.total_words)}` }),
          qualityChip(item)
        )
      ),
      h(
        'div',
        { class: 'lib-progress' },
        h('span', { class: 'progress-track', html: `<span style="width:${pct}%"></span>` })
      ),
      h(
        'div',
        { class: 'row-actions' },
        h('button', {
          class: 'icon-btn icon-btn-sm',
          type: 'button',
          'aria-label': '更多操作',
          html: icon('more', 15),
          on: {
            click: (event) => {
              event.stopPropagation();
              popoverMenu(event.currentTarget, rowActions(item), { align: 'right' });
            }
          }
        })
      )
    );
    row.addEventListener('click', () => {
      if (model.view !== 'trash') openProject(item);
    });
    return row;
  }

  /* ------------------------------------------------------------ start */

  paintStats();
  paintViews();
  paintFilters();
  paintBatchBar();
  load();

  return {
    el: view,
    refresh: load,
    destroy() {
      view.remove();
    }
  };
}
