/* 作品库（书架）：视图（全部/归档/回收站）、筛选、排序 6 种、本地搜索（书名/分类/简介）、
   分组 tab 条（全部/未分组/自定义分组带计数）、移动分组、封面上传（即时预览）、
   批量操作、收藏/归档/恢复/彻底删除/导出。
   v5.3.0：排序/搜索/分组/封面全部在前端完成（bot/weblib.py 提供分组与封面端点，
   后端没挂载或 mock 模式时 optional() 优雅降级，不影响作品列表主链路）。
   回收站沿用既有 trashed_at 生命周期与 /api/library/{key}/restore|purge 端点，不另起炉灶。 */

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
  downloadUrl,
  mediumGlyph } from '../core/dom.js';
import { store } from '../core/state.js';
import { api, errorText, IS_MOCK, request, optional } from '../core/api.js';
import { toast, popoverMenu, openModal, confirmDialog } from '../core/ui.js';
import { openProject } from '../core/router.js';
import { openAiCoverDialog } from '../core/aicover.js';
import { loadCss } from '../core/loadcss.js';

/* 书架新样式（分组 tab / 封面图 / 管理弹窗）：library.css 按需注入，panels.css 不动。 */
loadCss('/v2/css/library.css?v=530');

/* 排序 6 种：方向内建在选项里（最近更新/最早更新是一对，字数最多/最少是一对），
   排序在前端完成（sortItems 纯函数），切换时不再回服务端。 */
export const SORTS = [
  { value: 'updated', order: 'desc', label: '最近更新' },
  { value: 'updated', order: 'asc', label: '最早更新' },
  { value: 'created', order: 'desc', label: '最新创建' },
  { value: 'words', order: 'desc', label: '字数最多' },
  { value: 'words', order: 'asc', label: '字数最少' },
  { value: 'name', order: 'asc', label: '书名' }
];

const GROUP_NAME_MAX = 30;
const COVER_MAX_MB = 5;

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

/* 中文书名排序用本地化比较器（node/浏览器都有 Intl；缺了就退回逐字符比较）。 */
const NAME_COLLATOR = (typeof Intl !== 'undefined' && Intl.Collator)
  ? new Intl.Collator(['zh-Hans-CN', 'zh'], { numeric: true })
  : null;

/* ---------------------------------------------------------------- 纯函数（tests/v53_library_ui_test.mjs 直测） */

/* 排序：置顶（pinned）永远在前（与 /api/library 的服务端口径一致），其余按 key/order。
   稳定排序：key 相同保持原有相对顺序。 */
export function sortItems(items, sort = 'updated', order = 'desc') {
  const dir = order === 'asc' ? 1 : -1;
  const cmp = (a, b) => {
    let d = 0;
    if (sort === 'name') {
      const an = String(a.name || '');
      const bn = String(b.name || '');
      d = NAME_COLLATOR ? NAME_COLLATOR.compare(an, bn) : (an < bn ? -1 : an > bn ? 1 : 0);
    } else {
      const keyOf = (it) => {
        if (sort === 'created') return String(it.created_at || '');
        if (sort === 'words') return Number(it.total_words) || 0;
        return String(it.updated_at || it.created_at || '');   // updated
      };
      const ka = keyOf(a);
      const kb = keyOf(b);
      d = ka < kb ? -1 : ka > kb ? 1 : 0;
    }
    return d * dir;
  };
  const pinned = items.filter((it) => it && it.pinned).sort(cmp);
  const rest = items.filter((it) => it && !it.pinned).sort(cmp);
  return pinned.concat(rest);
}

/* 本地搜索：书名 / 分类（题材）/ 简介（bot/weblib.py /api/library/intros 提供，
   拿不到就跳过该字段，不阻塞搜索）+ 自定义标签。全部大小写不敏感的包含匹配。 */
export function filterItems(items, q, intros = {}) {
  const kw = String(q || '').trim().toLowerCase();
  if (!kw) return items.slice();
  return items.filter((it) => {
    const hay = [
      it.name,
      it.genre,
      Array.isArray(it.tags) ? it.tags.join(' ') : '',
      intros[it.key]
    ].filter(Boolean).join('\n').toLowerCase();
    return hay.includes(kw);
  });
}

/* 分组计数：基于已搜索的条目集（tab 上的数字随搜索收缩，所见即所搜）。 */
export function groupCounts(items, assign = {}) {
  const byGroup = {};
  let none = 0;
  for (const it of items) {
    const g = assign[it && it.key];
    if (g) byGroup[g] = (byGroup[g] || 0) + 1;
    else none += 1;
  }
  return { all: items.length, none, byGroup };
}

/* 按分组筛选：'all'=全部，'none'=未分组，其余为分组 id。 */
export function applyGroup(items, assign = {}, group = 'all') {
  if (!group || group === 'all') return items.slice();
  if (group === 'none') return items.filter((it) => !assign[it.key]);
  return items.filter((it) => assign[it.key] === group);
}

/* ---------------------------------------------------------------- 视图主入口 */

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
    group: 'all',                  // 'all' | 'none' | 分组 id
    mode: localStorage.getItem('shenbi.v2.libView') || 'grid',
    items: [],
    stats: {},
    facets: {},
    counts: {},
    selected: new Set(),
    groups: [],                    // 自定义分组 [{id,name,order}]
    assign: {},                    // 作品 key -> 分组 id（缺席 = 未分组）
    intros: {},                    // 作品 key -> 简介（本地搜索字段，可选）
    coverV: {},                    // 作品 key -> 封面版本（上传/移除后 bump，防缓存）
    mockCovers: {},                // mock 模式的本地预览 dataURL
    hasCover: new Set(),           // 已确认有封面的 key（img onload / 上传成功后登记）
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
      h('p', { class: 'hint-text', text: '全部作品、分组、封面与导出。数据都在本地，随时可以打包带走。' })
    ),
    h(
      'div',
      { class: 'lib-toolbar page-card' },
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

  /* 分组 tab 条：只在「全部」视图出现（归档/回收站不参与分组）。 */
  const groupRow = h('div', { class: 'lib-groups', hidden: true });
  pad.append(groupRow);

  const statsRow = h('div', { class: 'lib-stats page-card' });
  pad.append(statsRow);

  const filterRow = h('div', { class: 'lib-toolbar page-card' });
  pad.append(filterRow);

  const batchBar = h('div', { class: 'lib-batch', hidden: true });
  pad.append(batchBar);

  const listWrap = h('div', { class: model.mode === 'grid' ? 'lib-grid' : 'lib-list' });
  pad.append(listWrap);

  /* ------------------------------------------------------------ logic */

  let loadSeq = 0;

  /* 当前视图 + 搜索 + 分组筛选后的条目（不含排序）。 */
  function visibleItems() {
    return applyGroup(filterItems(model.items, model.q, model.intros), model.assign, model.group);
  }

  async function load() {
    const seq = ++loadSeq;
    model.loading = true;
    model.error = '';
    paintList();
    try {
      const res = await api.library({
        view: model.view,
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
    paintGroupTabs();
    paintList();
  }

  /* 分组与简介：附加能力，拿不到（后端未挂载 / mock 模式）就静默降级为无分组。 */
  async function refreshExtras() {
    try {
      const [g, it] = await Promise.all([
        optional(request('/api/library/groups')),
        optional(request('/api/library/intros'))
      ]);
      model.groups = (g && g.groups) || [];
      model.assign = (g && g.assign) || {};
      model.intros = (it && it.intros) || {};
    } catch {
      /* 分组/简介不可用不影响作品列表主链路 */
    }
    paintGroupTabs();
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
    model.group = 'all';          // 分组只作用于「全部」视图
    model.items = [];
    model.selected.clear();
    paintViews();
    paintStats();
    paintFilters();
    paintGroupTabs();
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

  /* ------------------------------------------------------------ 分组 tab 条 */

  function paintGroupTabs() {
    groupRow.hidden = model.view !== 'active';
    if (groupRow.hidden) return;
    groupRow.textContent = '';
    const counts = groupCounts(filterItems(model.items, model.q, model.intros), model.assign);
    const tabs = [
      { value: 'all', label: '全部', count: counts.all },
      { value: 'none', label: '未分组', count: counts.none },
      ...model.groups.map((g) => ({ value: g.id, label: g.name, count: counts.byGroup[g.id] || 0 }))
    ];
    const strip = h('div', { class: 'lib-gtabs', role: 'tablist', 'aria-label': '作品分组' });
    for (const t of tabs) {
      strip.append(h('button', {
        class: `lib-gtab${model.group === t.value ? ' is-active' : ''}`,
        type: 'button',
        role: 'tab',
        'aria-pressed': model.group === t.value ? 'true' : 'false',
        title: t.label,
        on: { click: () => setGroup(t.value) }
      },
        h('span', { class: 'lib-gtab-label', text: t.label }),
        h('span', { class: 'lib-gtab-count', text: String(t.count) })));
    }
    groupRow.append(
      strip,
      h('span', { class: 'lib-spacer' }),
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        html: `${icon('plus', 13)}<span>新建分组</span>`,
        on: { click: () => groupDialog('create') }
      }),
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        text: '管理分组',
        disabled: !model.groups.length,
        on: { click: manageGroupsDialog }
      })
    );
  }

  function setGroup(next) {
    if (model.group === next) return;
    model.group = next;
    paintGroupTabs();
    paintList();
  }

  /* 新建/重命名分组弹窗。onDone 供「管理分组」弹窗刷新行内列表。 */
  function groupDialog(mode, group = null, onDone = null) {
    const isRename = mode === 'rename';
    const input = h('input', {
      class: 'input',
      value: group ? group.name : '',
      maxlength: String(GROUP_NAME_MAX),
      placeholder: '如：玄幻 / 已完本 / 冲榜中',
      'aria-label': '分组名'
    });
    const error = h('div', { class: 'error-text', role: 'alert' });
    const saveBtn = h('button', {
      class: 'btn btn-primary',
      type: 'button',
      text: isRename ? '保存' : '创建',
      dataset: { autofocus: '' },
      on: { click: () => submit() }
    });
    const modal = openModal({
      title: isRename ? '重命名分组' : '新建分组',
      body: h('div', { class: 'confirm-body' },
        h('div', { class: 'field' }, h('span', { class: 'field-label', text: '分组名' }), input),
        error),
      footer: h('div', { class: 'lib-dialog-foot' },
        h('button', { class: 'btn', type: 'button', text: '取消', on: { click: () => modal.close() } }),
        saveBtn),
      size: 'sm',
      initialFocus: input
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        submit();
      }
    });

    async function submit() {
      const name = input.value.trim();
      if (!name) {
        error.textContent = '分组名不能为空';
        return;
      }
      if (IS_MOCK) {
        toast('演示模式：分组功能需要连接本地服务', { duration: 2400 });
        modal.close();
        return;
      }
      saveBtn.disabled = true;
      try {
        await request('/api/library/groups', {
          method: 'POST',
          body: isRename ? { action: 'rename', id: group.id, name } : { action: 'create', name }
        });
        modal.close();
        await refreshExtras();
        toast(isRename ? '分组已重命名' : `分组「${name}」已创建`, { tone: 'success', duration: 1800 });
        if (onDone) onDone();
      } catch (err) {
        saveBtn.disabled = false;
        error.textContent = errorText(err, isRename ? '重命名失败' : '创建失败');
      }
    }
  }

  /* 管理分组：行内重命名 / 删除（删除只是解散分组，组内作品回「未分组」）。 */
  function manageGroupsDialog() {
    const listWrap = h('div', { class: 'lib-gmgmt-list' });
    const paint = () => {
      listWrap.textContent = '';
      const counts = groupCounts(model.items, model.assign);
      if (!model.groups.length) {
        listWrap.append(h('div', { class: 'hint-text', text: '还没有分组，关闭后点「新建分组」创建第一个。' }));
        return;
      }
      for (const g of model.groups) {
        listWrap.append(
          h('div', { class: 'lib-gmgmt-row', dataset: { id: g.id } },
            h('span', { class: 'lib-gmgmt-name', text: g.name }),
            h('span', { class: 'lib-gmgmt-count', text: `${counts.byGroup[g.id] || 0} 部` }),
            h('button', {
              class: 'btn btn-sm', type: 'button', text: '重命名',
              on: { click: () => groupDialog('rename', g, paint) }
            }),
            h('button', {
              class: 'btn btn-sm lib-gmgmt-del', type: 'button', text: '删除',
              on: { click: () => deleteGroupFlow(g, paint) }
            }))
        );
      }
    };
    paint();
    openModal({
      title: '管理分组',
      subtitle: '删除分组不会删作品，组内作品回到「未分组」。',
      body: listWrap,
      size: 'sm'
    });
  }

  async function deleteGroupFlow(group, onDone = null) {
    const ok = await confirmDialog({
      title: '删除分组',
      message: `确定删除分组「${group.name}」吗？`,
      detail: '组内作品不会被删除，会回到「未分组」。',
      confirmText: '删除分组',
      danger: true
    });
    if (!ok) return;
    if (IS_MOCK) {
      toast('演示模式：分组功能需要连接本地服务', { duration: 2400 });
      return;
    }
    try {
      await request('/api/library/groups', { method: 'POST', body: { action: 'delete', id: group.id } });
      if (model.group === group.id) model.group = 'all';
      await refreshExtras();
      toast(`分组「${group.name}」已删除，组内作品回到未分组`, { tone: 'success', duration: 2200 });
      if (onDone) onDone();
    } catch (err) {
      toast(errorText(err, '删除分组失败'), { tone: 'danger' });
    }
  }

  /* 移动分组菜单：卡片 ⋯ → 移动分组 → 未分组 / 各分组（带当前勾选）/ 新建分组。 */
  function moveGroupMenu(anchor, item) {
    const current = model.assign[item.key] || null;
    const items = [
      { label: '未分组', icon: 'folder', checked: !current, onSelect: () => moveToGroup(item, null) },
      ...model.groups.map((g) => ({
        label: g.name,
        icon: 'folder',
        checked: current === g.id,
        onSelect: () => moveToGroup(item, g.id)
      })),
      { separator: true },
      { label: '新建分组', icon: 'plus', onSelect: () => groupDialog('create') }
    ];
    popoverMenu(anchor, items, { label: `《${item.name}》移动到` });
  }

  async function moveToGroup(item, gid) {
    if (IS_MOCK) {
      toast('演示模式：分组功能需要连接本地服务', { duration: 2400 });
      return;
    }
    try {
      await request('/api/library/assign-group', { method: 'POST', body: { key: item.key, groupId: gid } });
      if (gid) model.assign[item.key] = gid;
      else delete model.assign[item.key];
      toast(gid ? '已移动到分组' : '已移出分组，回到未分组', { tone: 'success', duration: 1600 });
      paintGroupTabs();
      paintList();
    } catch (err) {
      toast(errorText(err, '移动分组失败'), { tone: 'danger' });
    }
  }

  /* ------------------------------------------------------------ 封面 */

  function coverSrc(item) {
    if (model.mockCovers[item.key]) return model.mockCovers[item.key];
    const v = model.coverV[item.key] || 0;
    return `/api/library/cover?key=${encodeURIComponent(item.key)}&v=${v}`;
  }

  /* 封面节点：无封面 = 主题渐变底 + 文体字形 + 竖排书名（≤8 字取前 4 字，复用 mediumGlyph 口径）；
     有封面 = 图（load 后盖住占位，error 回退占位）。mock 模式只有本地预览图，不发探测请求。 */
  function coverNode(item, extraClass = '') {
    const wrap = h('span', { class: `lib-cover ${extraClass}`.trim() });
    const title = String(item.name || '').trim();
    const vert = title.length > 1 && title.length <= 8 ? title.slice(0, 3) : '';
    wrap.append(
      h('span', { class: 'lib-cover-glyph' },
        h('span', { class: 'lib-cover-glyph-char', text: (mediumGlyph(item) || initials(item.name)) }),
        vert ? h('span', { class: 'lib-cover-title', text: vert }) : null)
    );
    if (!IS_MOCK || model.mockCovers[item.key]) {
      const img = h('img', {
        class: 'lib-cover-img',
        alt: '',
        loading: 'lazy',
        src: coverSrc(item)
      });
      img.addEventListener('load', () => {
        model.hasCover.add(item.key);
        wrap.classList.add('has-img');
      });
      img.addEventListener('error', () => {
        model.hasCover.delete(item.key);
        img.remove();
      });
      wrap.append(img);
    }
    return wrap;
  }

  function validateCoverFile(file) {
    if (!file) return '没有读取到图片';
    const lower = String(file.name || '').toLowerCase();
    const okType = /^image\/(png|jpeg|webp)$/.test(file.type || '')
      || /\.(png|jpe?g|webp)$/.test(lower);
    if (!okType) return '封面只支持 PNG / JPG / WebP 图片';
    if (file.size > COVER_MAX_MB * 1024 * 1024) return `封面图片超过 ${COVER_MAX_MB}MB，请压缩后再上传`;
    if (file.size === 0) return '这张图片是空的';
    return '';
  }

  /* 上传封面：input file 选中 → FileReader 即时预览 → 再发请求落盘；
     失败回退预览并 toast，成功后 bump 版本号刷新封面。 */
  function pickCover(item) {
    const input = h('input', {
      type: 'file',
      accept: 'image/png,image/jpeg,image/webp',
      'aria-label': '选择封面图片'
    });
    input.addEventListener('change', () => {
      const file = input.files && input.files[0];
      if (!file) return;
      const bad = validateCoverFile(file);
      if (bad) {
        toast(bad, { tone: 'danger' });
        return;
      }
      const reader = new FileReader();
      reader.onload = () => applyCover(item, String(reader.result || ''), file);
      reader.onerror = () => toast('读取图片失败，请重试', { tone: 'danger' });
      reader.readAsDataURL(file);
    });
    input.click();
  }

  function applyCover(item, dataUrl, file) {
    model.mockCovers[item.key] = dataUrl;   // 即时预览（请求失败会回退）
    paintList();
    if (IS_MOCK) {
      toast('演示模式：封面仅本地预览，不落盘', { duration: 2400 });
      return;
    }
    const form = new FormData();
    form.append('key', item.key);
    form.append('file', file);
    request('/api/library/cover', { method: 'POST', form, timeout: 30000 })
      .then(() => {
        delete model.mockCovers[item.key];
        model.coverV[item.key] = Date.now();
        model.hasCover.add(item.key);
        toast('封面已更新', { tone: 'success', duration: 2000 });
        paintList();
      })
      .catch((err) => {
        delete model.mockCovers[item.key];
        paintList();
        toast(errorText(err, '封面上传失败'), { tone: 'danger' });
      });
  }

  async function removeCover(item) {
    if (IS_MOCK) {
      toast('演示模式：封面仅本地预览，不落盘', { duration: 2400 });
      return;
    }
    try {
      const res = await request(`/api/library/cover?key=${encodeURIComponent(item.key)}`, { method: 'DELETE' });
      model.coverV[item.key] = Date.now();
      model.hasCover.delete(item.key);
      toast(res && res.removed ? '已移除封面' : '该作品还没有封面', { duration: 1800 });
      paintList();
    } catch (err) {
      toast(errorText(err, '移除封面失败'), { tone: 'danger' });
    }
  }

  /* ------------------------------------------------------------ 逻辑辅助 */

  function paintFilters() {
    filterRow.textContent = '';
    const searchInput = h('input', {
      class: 'input',
      type: 'search',
      placeholder: '搜索书名 / 分类 / 简介…',
      value: model.q,
      'aria-label': '搜索作品'
    });
    /* 本地搜索：书名/分类/简介（+标签），不再回服务端，输入即时生效。 */
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
      sortSel.append(h('option', {
        value: opt.value + ':' + opt.order,
        text: opt.label,
        selected: opt.value === model.sort && opt.order === model.order
      }));
    }
    /* 排序 6 种方向内建，前端 sortItems 纯函数完成，切换不回服务端。 */
    sortSel.addEventListener('change', () => {
      const [sv, so] = sortSel.value.split(':');
      model.sort = sv;
      model.order = so;
      paintList();
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

    filterRow.append(search, mediumSel, genreSel, statusSel, sortSel, h('span', { class: 'lib-spacer' }), seg);
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

  /* 本地搜索防抖：只重画，不发请求。 */
  const runSearch = debounce(() => {
    paintGroupTabs();
    paintList();
  }, 140);

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
      /* 回收站里不允许改名/移动分组/换封面/导出，作品已不可编辑。 */
      ...(!isTrash
        ? [
            {
              label: '重命名',
              icon: 'edit',
              onSelect: () => renameDialog(item)
            },
            {
              label: '移动分组',
              icon: 'folder',
              onSelect: (anchor) => moveGroupMenu(anchor, item)
            },
            {
              label: '上传封面',
              icon: 'upload',
              onSelect: () => pickCover(item)
            },
            {
              /* v5.7.0：AI 生成封面——弹窗状态机在 core/aicover.js；
                 后端生成即落盘，采用后 bump 版本号刷新封面；mock 模式回传本地占位 dataURL。 */
              label: 'AI 生成封面',
              icon: 'sparkles',
              onSelect: () => openAiCoverDialog({
                item,
                onApplied: (dataUrl) => {
                  if (dataUrl) {
                    model.mockCovers[item.key] = dataUrl;
                  } else {
                    model.coverV[item.key] = Date.now();
                    model.hasCover.add(item.key);
                  }
                  paintList();
                }
              })
            },
            ...(model.hasCover.has(item.key)
              ? [
                  {
                    label: '移除封面',
                    icon: 'x',
                    onSelect: () => removeCover(item)
                  }
                ]
              : [])
          ]
        : []),
      /* v4.31.0：导出开放给全部文体（此前只有小说能导出） */
      ...(!isTrash
        ? [
            {
              label: '导出',
              icon: 'download',
              /* v4.31.0：popoverMenu 现在把锚点元素传给 onSelect；导出菜单据它定位 */
              onSelect: (anchor) => exportMenu(anchor, item)
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
          /* v4.31.0：fetch+blob 下载，报错走 toast 而不是把窗口导航到错误页 */
          safeExport(() => api.exportWork(item.key, fmt, { name: item.name }), fmt.toUpperCase());
        }
      }
    }));
    popoverMenu(anchor, items, { label: `导出《${item.name}》` });
  }

  /* v4.31.0：统一导出反馈——成功 toast，失败 toast 错误信息 */
  async function safeExport(run, label) {
    try {
      toast(`正在导出 ${label}…`, { duration: 2000 });
      const r = await run();
      /* v4.32.0：桌面端用户取消保存对话框 → 不提示 */
      if (r && r.cancelled) return;
      const where = r && r.saved ? `已保存到 ${r.saved}` : `${label} 已开始下载`;
      toast(where, { tone: 'success', duration: 3200 });
    } catch (err) {
      toast(errorText(err, '导出失败，请稍后重试'), { tone: 'danger', duration: 4000 });
    }
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

  /* 恢复沿用既有 /api/library/{key}/restore（restore_book 同时清归档与回收站标记的
     既有生命周期语义），不另造第二套恢复。 */
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

  /* 彻底删除：二次确认 + 手动输入完整作品名，名字不匹配时按钮禁用、不发请求。
     v5.3.0：明确列出将删数据范围（正文/版本/账本/参考/封面），删除的是整个作品目录。 */
  const PURGE_SCOPE = [
    '正文与成稿（各章正文、定稿文件）',
    '章节版本快照（全部历史版本）',
    '大纲、人物档案、故事圣经等设定资料',
    '线索账本、伏笔账本与 AI 调用账本',
    '参考资料与作品封面'
  ];

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
        h('div', { class: 'lib-purge-scope' },
          h('div', { class: 'lib-purge-scope-title', text: '将删除以下全部数据，删除后无法恢复：' }),
          h('ul', { class: 'lib-purge-list' }, PURGE_SCOPE.map((s) => h('li', { text: s })))
        ),
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
    const shown = sortItems(visibleItems(), model.sort, model.order);
    if (!shown.length) {
      const inGroup = model.view === 'active' && model.group !== 'all';
      const copy = EMPTY_TEXT[model.view] || EMPTY_TEXT.active;
      const title = inGroup && !model.q ? '这个分组还没有作品' : copy.title(model.q);
      const hint = inGroup && !model.q ? '点作品卡上的「⋯」→「移动分组」，把它移进来。' : copy.hint(model.q);
      listWrap.append(
        h(
          'div',
          { class: 'lib-empty empty-state' },
          h('div', { class: 'empty-icon', html: icon(model.view === 'trash' ? 'trash' : 'library', 18) }),
          h('h3', { text: title }),
          h('p', { text: hint }),
          model.view === 'active' && !inGroup
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

    for (const item of shown) {
      listWrap.append(model.mode === 'grid' ? gridCard(item) : listRow(item));
    }
  }

  function qualityChip(item) {
    /* v4.2.0：星级评分（★ + 分数 + 达标徽章），无分不显示 */
    if (item.quality == null) return null;
    const passing = item.passing === true;
    return h('span', {
      class: 'lib-quality',
      dataset: { tone: passing ? 'success' : 'muted' },
      title: passing ? '质量达标' : '尚未达标'
    },
      h('span', { class: 'lib-star-score', html: icon('star', 12) }),
      h('span', { class: 'lib-quality-num', text: String(item.quality) }),
      passing ? h('span', { class: 'lib-quality-pass', text: '达标' }) : null
    );
  }

  /* v4.2.0：题材标签 chip + 用户自定义 tags + 所在分组 */
  function genreTags(item) {
    const row = h('span', { class: 'lib-tags' });
    if (item.genre) row.append(h('span', { class: 'chip chip-muted lib-genre', text: item.genre }));
    for (const t of (item.tags || []).slice(0, 3)) {
      row.append(h('span', { class: 'chip chip-muted lib-tag', text: String(t) }));
    }
    const gid = model.assign[item.key];
    if (gid) {
      const g = model.groups.find((x) => x.id === gid);
      if (g) row.append(h('span', { class: 'chip chip-muted lib-group-chip', title: `分组：${g.name}`, text: g.name }));
    }
    return row;
  }

  /* 归档/回收站条目的时间戳：归档页显示归档时间，回收站显示移入时间；其余显示更新时间。 */
  function lifecycleStamp(item) {
    if (model.view === 'trash') return item.trashed_at ? `移入 ${fmtDate(item.trashed_at)}` : '';
    if (model.view === 'archived') return item.archived_at ? `归档 ${fmtDate(item.archived_at)}` : '';
    return '';
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
    const meta = statusMeta(item.state, item.status_label, item.running);   // v4.34.0：补 running 参数（v4.33.6 漏改的两处，缺参会误显示创作中）
    const pct = progressOf(item);
    const unit = MEDIUM_UNITS[item.medium || 'novel'] || '章';
    const card = h('div', { class: 'lib-card', dataset: { key: item.key } });
    card.append(
      h(
        'div',
        { class: 'lib-card-top' },
        coverNode(item),
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
            genreTags(item),
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
            h('span', { text: lifecycleStamp(item) || fmtDate(item.updated_at) })
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
    const meta = statusMeta(item.state, item.status_label, item.running);   // v4.34.0：补 running 参数（v4.33.6 漏改的两处，缺参会误显示创作中）
    const pct = progressOf(item);
    const unit = MEDIUM_UNITS[item.medium || 'novel'] || '章';
    const row = h('div', { class: 'lib-row', dataset: { key: item.key } });
    row.append(
      selectBox(item),
      coverNode(item),
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
          genreTags(item),
          h('span', { class: `dot dot-${meta.tone}` }),
          h('span', { text: meta.label }),
          h('span', { text: `${item.done}/${item.total} ${unit} · ${fmtWords(item.total_words)}` }),
          h('span', { class: 'lib-row-stamp', text: lifecycleStamp(item) }),
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
  paintGroupTabs();
  paintBatchBar();
  refreshExtras();
  load();

  return {
    el: view,
    refresh: load,
    destroy() {
      view.remove();
    }
  };
}
