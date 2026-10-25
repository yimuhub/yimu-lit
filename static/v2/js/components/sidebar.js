/* 左侧作品栏：品牌、新建、搜索、按时间分组、状态与进度、底部入口。 */

import { h, icon, fmtWords, groupByRecency, initials, statusMeta, debounce } from '../core/dom.js';
import { store } from '../core/state.js';
import { navigate } from '../core/router.js';
import { toast } from '../core/ui.js';
import { api, errorText } from '../core/api.js';

let localQuery = '';

function rowMeta(item) {
  const pct = item.total ? Math.round((item.done / item.total) * 100) : 0;
  return { pct, words: item.total_words || 0 };
}

function renderRow(item, ctx, { isActive }) {
  const meta = statusMeta(item.state, item.status_label);
  const { pct, words } = rowMeta(item);
  const btn = h('button', {
    class: `side-row${isActive ? ' is-active' : ''}`,
    type: 'button',
    dataset: { key: item.key },
    'data-tip': `${item.name} · ${meta.label}`,
    'aria-current': isActive ? 'page' : 'false'
  });
  btn.append(h('span', { class: 'rail-avatar', 'aria-hidden': 'true', text: initials(item.name) }));
  const main = h('span', { class: 'side-row-main' });
  const top = h(
    'span',
    { class: 'side-row-top' },
    h('span', { class: `dot${meta.tone === 'muted' ? '' : ' dot-' + meta.tone}`, title: meta.label }),
    h('span', { class: 'side-row-name', text: item.name })
  );
  const bottom = h(
    'span',
    { class: 'side-row-meta' },
    h('span', { text: `${item.done}/${item.total}` }),
    h('span', { class: 'sep' }),
    h('span', { text: fmtWords(words) })
  );
  if (item.state === 'in_progress') {
    bottom.append(
      h('span', { class: 'sep' }),
      h('span', { class: 'mini-progress progress-track', html: `<span style="width:${pct}%"></span>` })
    );
  }
  main.append(top, bottom);
  btn.append(main);

  const actions = h('span', { class: 'side-row-actions' });
  const fav = h('button', {
    class: 'icon-btn icon-btn-sm',
    type: 'button',
    'aria-label': item.favorite ? '取消收藏' : '收藏',
    'aria-pressed': item.favorite ? 'true' : 'false',
    html: icon('star', 13),
    on: {
      click: async (event) => {
        event.stopPropagation();
        try {
          await api.favorite(item.key, !item.favorite);
          ctx.refreshLibrary();
        } catch (err) {
          toast(errorText(err, '收藏失败'), { tone: 'danger' });
        }
      }
    }
  });
  actions.append(fav);
  btn.append(actions);

  btn.addEventListener('click', () => {
    navigate(`/work/${encodeURIComponent(item.key)}`);
    ctx.closeDrawer();
  });
  return btn;
}

function bindSearch(input, listEl, ctx) {
  const run = debounce((value) => {
    localQuery = value.trim();
    paintList(listEl, ctx);
  }, 120);
  input.addEventListener('input', () => run(input.value));
}

function paintList(listEl, ctx) {
  const state = store.get();
  const q = localQuery.toLowerCase();
  let items = state.library.items.filter((i) => !i.archived);
  if (q) {
    items = items.filter(
      (i) =>
        String(i.name || '').toLowerCase().includes(q) ||
        String(i.genre || '').toLowerCase().includes(q) ||
        (i.tags || []).some((t) => String(t).toLowerCase().includes(q))
    );
  }
  listEl.textContent = '';
  if (state.library.loading && !state.library.loaded) {
    for (let i = 0; i < 8; i += 1) {
      listEl.append(
        h(
          'div',
          { class: 'sidebar-skeleton' },
          h('div', { class: 'skeleton skeleton-line', style: { width: `${58 + ((i * 9) % 30)}%` } }),
          h('div', { class: 'skeleton skeleton-line w-40', style: { height: '7px' } })
        )
      );
    }
    return;
  }
  if (!items.length) {
    const empty = h('div', { class: 'empty-state', style: { padding: '28px 8px' } });
    empty.append(
      h('div', { class: 'empty-icon', html: icon('book', 18) }),
      h('h3', { text: q ? '没有匹配的作品' : '还没有作品' }),
      h('p', { text: q ? '换个关键词，或清空搜索。' : '点击上方「新建作品」开始第一本。' })
    );
    if (q) {
      empty.append(
        h('button', {
          class: 'btn btn-sm',
          type: 'button',
          text: '清空搜索',
          on: {
            click: () => {
              localQuery = '';
              const input = document.querySelector('.side-search input');
              if (input) input.value = '';
              paintList(listEl, ctx);
            }
          }
        })
      );
    }
    listEl.append(empty);
    return;
  }

  const groups = groupByRecency(items);
  const currentKey = store.get().route.key;
  for (const group of groups) {
    const section = h('div', { class: 'side-group' });
    section.append(
      h(
        'div',
        { class: 'side-group-label' },
        h('span', { text: group.label }),
        h('span', { class: 'count', text: String(group.items.length) })
      )
    );
    for (const item of group.items) {
      section.append(renderRow(item, ctx, { isActive: item.key === currentKey }));
    }
    listEl.append(section);
  }
}

export function renderSidebar(container, ctx) {
  const state = store.get();
  const focused = document.activeElement;
  const wasSearch = focused && focused.closest && focused.closest('.side-search');
  const selStart = wasSearch ? focused.selectionStart : 0;
  const selEnd = wasSearch ? focused.selectionEnd : 0;

  container.textContent = '';
  const sidebar = h('div', { class: 'sidebar' });

  const brandRow = h('div', { class: 'brand-row' });
  const brand = h(
    'a',
    { class: 'brand', href: '#/welcome', 'aria-label': '神笔创作台首页' },
    h('span', { class: 'brand-mark', html: icon('quill', 15) }),
    h(
      'span',
      { class: 'brand-copy' },
      h('span', { class: 'brand-name', text: '神笔' }),
      h('span', { class: 'brand-sub', text: '创作台' })
    )
  );
  brandRow.append(brand);
  const collapseBtn = h('button', {
    class: 'icon-btn icon-btn-sm',
    type: 'button',
    'aria-label': state.ui.sidebarExpanded ? '收起侧栏' : '展开侧栏',
    title: `${state.ui.sidebarExpanded ? '收起' : '展开'}侧栏（Ctrl/⌘ B）`,
    html: icon(state.ui.sidebarExpanded ? 'panel-left' : 'panel-right', 15),
    on: { click: () => ctx.toggleSidebar() }
  });
  brandRow.append(collapseBtn);
  sidebar.append(brandRow);

  const newBtn = h(
    'button',
    { class: 'new-work', type: 'button', 'aria-label': '新建作品，快捷键 N' },
    h('span', { html: icon('plus', 15) }),
    h('span', { class: 'label', text: '新建作品' }),
    h('kbd', { text: 'N' })
  );
  newBtn.addEventListener('click', () => ctx.openWizard());
  sidebar.append(newBtn);

  const search = h('div', { class: 'side-search' });
  search.append(
    h('span', { html: icon('search', 14) }),
    h('input', {
      type: 'search',
      placeholder: '搜索作品',
      'aria-label': '搜索作品',
      value: localQuery,
      on: {
        keydown: (event) => {
          if (event.key === 'Escape') {
            event.stopPropagation();
            event.target.value = '';
            localQuery = '';
            paintList(listEl, ctx);
            event.target.blur();
          }
        }
      }
    })
  );
  sidebar.append(search);

  const listEl = h('div', { class: 'side-list', 'aria-label': '作品列表' });
  sidebar.append(listEl);
  paintList(listEl, ctx);

  const foot = h('div', { class: 'side-foot' });
  const footItems = [
    {
      label: '作品库',
      icon: 'library',
      trail: '',
      onClick: () => {
        navigate('/library');
        ctx.closeDrawer();
      }
    },
    {
      label: '技能进化',
      icon: 'sparkles',
      trail: '',
      onClick: () => {
        navigate('/evolution');
        ctx.closeDrawer();
      }
    },
    {
      label: '设置',
      icon: 'settings',
      trail: '',
      onClick: () => ctx.openSettings()
    },
    {
      label: '经典版',
      icon: 'history',
      trail: '/classic',
      href: '/classic',
      title: '回到旧版单栏界面'
    }
  ];
  for (const item of footItems) {
    const row = item.href
      ? h(
          'a',
          { class: 'foot-row', href: item.href, 'data-tip': `${item.label}${item.trail}`, title: item.title || '' },
          h('span', { html: icon(item.icon, 15) }),
          h('span', { class: 'label', text: item.label }),
          item.trail ? h('span', { class: 'foot-trail', text: item.trail }) : null
        )
      : h(
          'button',
          { class: 'foot-row', type: 'button', 'data-tip': item.label },
          h('span', { html: icon(item.icon, 15) }),
          h('span', { class: 'label', text: item.label }),
          item.trail ? h('span', { class: 'foot-trail', text: item.trail }) : null
        );
    if (!item.href) row.addEventListener('click', item.onClick);
    foot.append(row);
  }
  sidebar.append(foot);
  container.append(sidebar);

  const input = search.querySelector('input');
  bindSearch(input, listEl, ctx);
  if (wasSearch) {
    requestAnimationFrame(() => {
      input.focus();
      try {
        input.setSelectionRange(selStart, selEnd);
      } catch {
        /* ignore */
      }
    });
  }
}
