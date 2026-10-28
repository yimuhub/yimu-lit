/* 左侧作品栏：品牌、新建、搜索、按时间分组、状态与进度、底部入口。 */

import { h, icon, fmtWords, groupByRecency, initials, mediumGlyph, statusMeta, debounce } from '../core/dom.js';
import { store } from '../core/state.js';
import { navigate, openProject } from '../core/router.js';
import { toast } from '../core/ui.js';
import { api, errorText } from '../core/api.js';

let localQuery = '';

function rowMeta(item) {
  const pct = item.total ? Math.round((item.done / item.total) * 100) : 0;
  return { pct, words: item.total_words || 0 };
}

function renderRow(item, ctx, { isActive }) {
  /* 乐观条目：pending=正在规划，failed=创建失败；两类都不隐藏，点开由作品页处理。 */
  const pending = Boolean(item.pending);
  const failed = Boolean(item.failed);
  const meta = failed
    ? { tone: 'danger', label: '创建失败' }
    : pending
      ? { tone: 'warn', label: '规划中' }
      : statusMeta(item.state, item.status_label);
  const { pct, words } = rowMeta(item);
  const btn = h('button', {
    class: `side-row${isActive ? ' is-active' : ''}${pending ? ' is-pending' : ''}${failed ? ' is-failed' : ''}`,
    type: 'button',
    dataset: { key: item.key },
    'data-tip': `${item.name} · ${meta.label}`,
    'aria-current': isActive ? 'page' : 'false'
  });
  /* v4.2.0：按文体显示单字头像（小/剧/散/诗/词…），一眼识别体裁 */
  btn.append(h('span', { class: 'rail-avatar', 'aria-hidden': 'true',
    text: mediumGlyph(item) || initials(item.name) }));
  const main = h('span', { class: 'side-row-main' });
  const top = h(
    'span',
    { class: 'side-row-top' },
    h('span', { class: `dot${meta.tone === 'muted' ? '' : ' dot-' + meta.tone}`, title: meta.label }),
    h('span', { class: 'side-row-name', text: item.name })
  );
  if (failed) {
    top.append(h('span', { class: 'badge', dataset: { tone: 'danger' }, text: '创建失败' }));
  } else if (pending) {
    top.append(h('span', { class: 'badge badge-accent', text: '规划中' }));
  }
  const bottom = h('span', { class: 'side-row-meta' });
  if (pending) {
    /* 乐观条目没有章节进度，不显示 0/0。 */
    bottom.append(h('span', { text: '正在规划' }));
  } else if (failed) {
    bottom.append(h('span', { text: '尚未创建' }));
  } else {
    bottom.append(
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
    openProject(item);
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
  let items = state.library.items.filter((i) => !i.archived && !i.trashed);
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
      label: '技能库',
      icon: 'wand',
      trail: '',
      active: store.get().route.name === 'skills',
      onClick: () => {
        navigate('/skills');
        ctx.closeDrawer();
      }
    },
    {
      label: '技能进化',
      icon: 'sparkles',
      trail: '',
      active: store.get().route.name === 'evolution',
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
    /* 版本号：显示当前版本，点击检查 GitHub 最新 release */
    {
      label: '',
      icon: 'tag',
      trail: '',
      custom: true,
      render: () => {
        const row = h('div', {
          class: 'side-foot-version',
          title: '点击检查更新',
          role: 'button',
          tabindex: '0',
          style: { cursor: 'pointer' }
        });
        const verSpan = h('span', { class: 'sfv-label', text: '版本 ' });
        const verNum = h('span', { class: 'sfv-num', text: '…' });
        verSpan.append(verNum);
        row.append(h('span', { html: icon('tag', 14), class: 'sfv-icon' }), verSpan);

        /* 加载本地版本 */
        api.version().then((res) => {
          if (res && res.version) verNum.textContent = 'v' + res.version;
        }).catch(() => { verNum.textContent = 'v?'; });

        /* 点击检查更新 */
        const checkUpdate = async () => {
          if (row.dataset.checking === 'true') return;
          row.dataset.checking = 'true';
          verNum.textContent = '检查中…';
          try {
            const res = await api.checkUpdate();
            if (!res || !res.ok) {
              verNum.textContent = res && res.error ? '⚠' : 'v?';
              toast((res && res.error) || '检查更新失败', 'error');
              return;
            }
            const local = res.local_version || '?';
            const latest = res.latest_version || '?';
            if (res.has_update) {
              verNum.textContent = `v${local} → v${latest}`;
              verNum.style.color = 'var(--accent, #4ecb89)';
              toast(`发现新版本 v${latest}，点击前往更新`, 'info');
              row.onclick = () => {
                if (res.release_url) window.open(res.release_url, '_blank');
              };
              /* 闪烁提示 */
              row.dataset.update = 'true';
            } else {
              verNum.textContent = `v${local}`;
              verNum.style.color = '';
              toast('已是最新版本', 'success');
            }
          } catch (e) {
            verNum.textContent = '⚠';
            toast('检查更新失败：网络错误', 'error');
          } finally {
            row.dataset.checking = 'false';
          }
        };
        row.addEventListener('click', checkUpdate);
        row.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); checkUpdate(); }
        });
        return row;
      }
    },
    /* 经典版入口默认隐藏，在「设置 → 高级」里打开 */
    ...(store.get().ui.showClassic
      ? [
          {
            label: '经典版',
            icon: 'history',
            trail: '/classic',
            href: '/classic',
            title: '回到旧版单栏界面'
          }
        ]
      : [])
  ];
  for (const item of footItems) {
    if (item.custom && typeof item.render === 'function') {
      const row = item.render();
      if (row) foot.append(row);
      continue;
    }
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
    if (item.active) row.dataset.active = 'true';
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
