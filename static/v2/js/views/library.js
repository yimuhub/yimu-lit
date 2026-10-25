/* 作品库：筛选、排序、网格/列表视图、收藏/归档/重命名/删除/导出。 */

import {
  h,
  icon,
  fmtWords,
  fmtNum,
  fmtDate,
  initials,
  statusMeta,
  debounce,
  downloadUrl
} from '../core/dom.js';
import { store } from '../core/state.js';
import { api, errorText, IS_MOCK } from '../core/api.js';
import { toast, confirmDialog, popoverMenu, openModal } from '../core/ui.js';
import { navigate } from '../core/router.js';

const SORTS = [
  { value: 'updated', label: '最近更新' },
  { value: 'created', label: '创建时间' },
  { value: 'words', label: '字数' },
  { value: 'quality', label: '评分' },
  { value: 'progress', label: '进度' }
];

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'in_progress', label: '创作中' },
  { value: 'completed', label: '已出书' },
  { value: 'passing', label: '质量达标' },
  { value: 'unrated', label: '未评分' },
  { value: 'archived', label: '回收站' }
];

export function renderLibrary(container, ctx) {
  const view = h('div', { class: 'view-scroll' });
  const pad = h('div', { class: 'view-pad' });
  view.append(pad);
  container.append(view);

  const model = {
    q: '',
    genre: '',
    status: '',
    sort: 'updated',
    order: 'desc',
    mode: localStorage.getItem('shenbi.v2.libView') || 'grid',
    items: [],
    stats: {},
    facets: {},
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

  const statsRow = h('div', { class: 'lib-stats' });
  pad.append(statsRow);

  const filterRow = h('div', { class: 'lib-toolbar', style: { display: 'flex', flexWrap: 'wrap', gap: '8px', alignItems: 'center', margin: '4px 0 16px' } });
  pad.append(filterRow);

  const listWrap = h('div', { class: model.mode === 'grid' ? 'lib-grid' : 'lib-list' });
  pad.append(listWrap);

  /* ------------------------------------------------------------ logic */

  async function load() {
    model.loading = true;
    paintList();
    try {
      const res = await api.library({
        q: model.q,
        genre: model.genre,
        status: model.status,
        sort: model.sort,
        order: model.order
      });
      model.items = (res && res.items) || [];
      model.stats = (res && res.stats) || {};
      model.facets = (res && res.facets) || {};
      model.error = '';
    } catch (err) {
      model.error = errorText(err, '作品库加载失败');
    }
    model.loading = false;
    paintStats();
    paintFilters();
    paintList();
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
          h('span', { class: 'kv-k', text: k }),
          h(
            'span',
            { class: 'kv-v' },
            `${v}`,
            unit ? h('span', { class: 'kv-unit', text: unit }) : null
          )
        )
      );
    }
  }

  function paintFilters() {
    filterRow.textContent = '';
    const search = h('input', {
      class: 'input lib-search',
      type: 'search',
      placeholder: '搜索书名或题材…',
      value: model.q,
      'aria-label': '搜索作品'
    });
    search.addEventListener('input', () => {
      model.q = search.value.trim();
      runSearch();
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

    filterRow.append(search, genreSel, statusSel, sortSel, orderBtn, h('span', { style: { flex: '1' } }), seg);
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

  function rowActions(item) {
    return [
      {
        label: '打开作品',
        icon: 'book-open',
        onSelect: () => navigate(`/work/${encodeURIComponent(item.key)}`)
      },
      {
        label: '章节工作台',
        icon: 'edit',
        onSelect: () => ctx.openBench(Math.min((item.done || 0) + 1, item.total || 1))
      },
      { separator: true },
      {
        label: item.favorite ? '取消收藏' : '收藏',
        icon: 'star',
        onSelect: () => toggleFavorite(item)
      },
      {
        label: '重命名',
        icon: 'edit',
        onSelect: () => renameDialog(item)
      },
      {
        label: '导出',
        icon: 'download',
        onSelect: (event) => exportMenu(event, item)
      },
      { separator: true },
      {
        label: item.archived ? '从回收站恢复' : '移入回收站',
        icon: 'archive',
        onSelect: () => toggleArchive(item)
      },
      {
        label: '彻底删除',
        icon: 'trash',
        danger: true,
        onSelect: () => removeDialog(item)
      }
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

  async function toggleArchive(item) {
    try {
      await api.archive(item.key, !item.archived);
      toast(item.archived ? '已恢复' : '已移入回收站', { duration: 2000 });
      load();
      ctx.refreshLibrary();
    } catch (err) {
      toast(errorText(err, '操作失败'), { tone: 'danger' });
    }
  }

  function renameDialog(item) {
    const input = h('input', { class: 'input', value: item.name || '', maxlength: '60', 'aria-label': '新书名' });
    const body = h('div', {}, input);
    const footer = h(
      'div',
      { style: { display: 'flex', gap: '8px', justifyContent: 'flex-end', width: '100%' } },
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

  async function removeDialog(item) {
    const ok = await confirmDialog({
      title: `彻底删除《${item.name}》？`,
      message: '删除后作品目录与全部章节都会被移除，无法恢复。',
      detail: '如果只是想暂时收起来，用「移入回收站」即可。',
      confirmText: '彻底删除',
      danger: true
    });
    if (!ok) return;
    try {
      await api.remove(item.key, true);
      toast('已删除', { duration: 1800 });
      load();
      ctx.refreshLibrary();
    } catch (err) {
      toast(errorText(err, '删除失败'), { tone: 'danger' });
    }
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
            h('div', { class: 'skeleton', style: { width: '44px', height: '58px', borderRadius: '8px' } }),
            h(
              'div',
              { style: { flex: '1' } },
              h('div', { class: 'skeleton skeleton-line', style: { width: `${45 + ((i * 13) % 35)}%` } }),
              h('div', { class: 'skeleton skeleton-line w-40', style: { height: '7px', marginTop: '8px' } })
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
          { class: 'lib-empty', role: 'alert' },
          h('div', { class: 'empty-icon', html: icon('alert', 18) }),
          h('h3', { text: '作品库没有加载出来' }),
          h('p', { text: model.error }),
          h('button', { class: 'btn btn-sm', type: 'button', text: '重试', on: { click: load } })
        )
      );
      return;
    }
    if (!model.items.length) {
      const archived = model.status === 'archived';
      listWrap.append(
        h(
          'div',
          { class: 'lib-empty' },
          h('div', { class: 'empty-icon', html: icon('library', 18) }),
          h('h3', { text: archived ? '回收站是空的' : model.q ? '没有匹配的作品' : '还没有作品' }),
          h('p', {
            text: archived
              ? '移入回收站的作品会出现在这里，可以随时恢复。'
              : model.q
                ? '换个关键词，或清空筛选条件。'
                : '从一句话想法开始，第一本三分钟就能立项。'
          }),
          !archived
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

  function gridCard(item) {
    const meta = statusMeta(item.state, item.status_label);
    const pct = progressOf(item);
    const card = h('div', { class: 'lib-card', dataset: { key: item.key } });
    card.append(
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
          qualityChip(item)
        ),
        h(
          'div',
          { class: 'lib-meta' },
          h('span', { text: item.genre || '未分类' }),
          h('span', { class: 'sep' }),
          h('span', { class: `dot dot-${meta.tone}` }),
          h('span', { text: meta.label }),
          h('span', { class: 'sep' }),
          h('span', { text: `${item.done}/${item.total} 章` })
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
    card.addEventListener('click', () => navigate(`/work/${encodeURIComponent(item.key)}`));
    return card;
  }

  function listRow(item) {
    const meta = statusMeta(item.state, item.status_label);
    const pct = progressOf(item);
    const row = h('div', { class: 'lib-row', dataset: { key: item.key } });
    row.append(
      h('span', { class: 'lib-cover', text: initials(item.name) }),
      h(
        'div',
        { class: 'lib-main' },
        h(
          'div',
          { class: 'lib-name-line' },
          h('span', { class: 'lib-name', text: item.name }),
          item.favorite ? h('span', { class: 'lib-star', html: icon('star', 12) }) : null
        ),
        h(
          'div',
          { class: 'lib-meta' },
          h('span', { text: item.genre || '未分类' }),
          h('span', { class: `dot dot-${meta.tone}` }),
          h('span', { text: meta.label }),
          h('span', { text: `${item.done}/${item.total} 章 · ${fmtWords(item.total_words)}` }),
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
    row.addEventListener('click', () => navigate(`/work/${encodeURIComponent(item.key)}`));
    return row;
  }

  /* ------------------------------------------------------------ start */

  paintStats();
  paintFilters();
  load();

  return {
    el: view,
    refresh: load,
    destroy() {
      view.remove();
    }
  };
}
