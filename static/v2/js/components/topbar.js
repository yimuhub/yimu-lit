/* 顶栏：面包屑、实时状态、模型选择、主题、右栏开关。 */

import { h, icon } from '../core/dom.js';
import { store } from '../core/state.js';
import { popoverMenu, toast } from '../core/ui.js';

function currentTitle(state) {
  if (state.route.name === 'welcome') return '新会话';
  if (state.route.name === 'library') return '作品库';
  if (state.route.name === 'evolution') return '技能进化';
  if (state.route.name === 'settings') return '设置';
  const item = state.work || state.library.items.find((i) => i.key === state.route.key);
  return item ? item.name : '作品';
}

export function renderTopbar(container, ctx) {
  const state = store.get();
  const route = state.route;
  const small = window.innerWidth < 760;
  container.textContent = '';

  const left = h('div', { class: 'topbar-left' });
  const menuBtn = h('button', {
    class: 'icon-btn',
    type: 'button',
    'aria-label': small ? '打开作品导航' : state.ui.sidebarExpanded ? '收起侧栏' : '展开侧栏',
    title: '侧栏（Ctrl/⌘ B）',
    html: icon('panel-left', 17),
    on: {
      click: () => {
        if (small) ctx.toggleDrawer();
        else ctx.toggleSidebar();
      }
    }
  });
  left.append(menuBtn);

  const crumbs = h('div', { class: 'topbar-crumbs' });
  if (route.name === 'welcome') {
    crumbs.append(h('span', { class: 'crumb-current', text: '新会话' }));
  } else if (route.name === 'library') {
    crumbs.append(h('span', { class: 'crumb', text: '作品库' }));
  } else if (route.name === 'evolution') {
    crumbs.append(h('span', { class: 'crumb', text: '技能进化' }));
  } else if (route.name === 'settings') {
    crumbs.append(h('span', { class: 'crumb', text: '设置' }));
  } else {
    crumbs.append(
      h('span', { class: 'crumb', text: '作品' }),
      h('span', { class: 'slash', text: '/' }),
      h('span', { class: 'crumb-current', text: currentTitle(state), title: currentTitle(state) })
    );
    if (route.name === 'bench') {
      crumbs.append(
        h('span', { class: 'slash', text: '/' }),
        h('span', { class: 'crumb-current', text: `第 ${route.chapter || 1} 章` })
      );
    }
  }
  left.append(crumbs);

  const right = h('div', { class: 'topbar-right' });

  // work live chip
  if (route.name === 'work' || route.name === 'bench') {
    const work = state.work;
    if (work) {
      const running = Boolean(work.running);
      const pct = work.total ? Math.round((work.done / work.total) * 100) : 0;
      const chip = h(
        'button',
        {
          class: 'tb-chip',
          type: 'button',
          title: running ? '正在创作，点击查看实时状态' : '查看创作状态',
          on: {
            click: () => {
              ctx.openRight('work');
            }
          }
        },
        h('span', { class: `dot${running ? ' dot-running' : ''}` }),
        h('span', {
          class: 'tb-label',
          text: running ? `写作中 · ${work.done}/${work.total}` : `${work.status_label || '空闲'} · ${pct}%`
        })
      );
      right.append(chip);
    }
  }

  // model chip
  const modelName = state.ui.model || defaultModel(state);
  const modelChip = h(
    'button',
    {
      class: 'tb-chip',
      type: 'button',
      'aria-haspopup': 'menu',
      title: `当前模型：${modelName}`,
      on: {
        click: (event) => {
          const options = (state.models.options || []).filter((o) => o.models && o.models.length);
          const items = [];
          for (const provider of options) {
            items.push({ label: provider.name, icon: 'cpu', disabled: true });
            for (const m of provider.models) {
              items.push({
                label: m,
                icon: 'dot',
                checked: m === modelName,
                onSelect: () => {
                  ctx.setModel(provider.id, m);
                  toast(`已切换到 ${provider.name} · ${m}`, { tone: 'success', duration: 1800 });
                }
              });
            }
          }
          if (!items.length) {
            items.push({
              label: '还没有可用模型',
              icon: 'plug',
              onSelect: () => ctx.openSettings('providers')
            });
          }
          items.push(
            { separator: true },
            { label: '管理模型与密钥…', icon: 'settings', onSelect: () => ctx.openSettings('providers') }
          );
          popoverMenu(event.currentTarget, items, { label: '本轮使用的模型' });
        }
      }
    },
    h('span', { html: icon('cpu', 14) }),
    h('span', { class: 'tb-label mono', text: modelName }),
    h('span', { class: 'chev', html: icon('chevron-down', 12) })
  );
  right.append(modelChip);

  right.append(h('div', { class: 'divider-v' }));

  const themeBtn = h('button', {
    class: 'icon-btn',
    type: 'button',
    'aria-label': `主题：${state.resolvedTheme === 'dark' ? '深色' : '浅色'}，点击切换`,
    title: '切换主题（浅色 / 深色）',
    html: icon(state.resolvedTheme === 'dark' ? 'moon' : 'sun', 16),
    on: { click: () => ctx.toggleTheme() }
  });
  right.append(themeBtn);

  const rightBtn = h('button', {
    class: 'icon-btn',
    type: 'button',
    'aria-label': state.ui.rightOpen ? '关闭工作台' : '打开工作台',
    'aria-pressed': state.ui.rightOpen ? 'true' : 'false',
    title: '工作台（Ctrl/⌘ J）',
    html: icon('panel-right', 17),
    on: { click: () => ctx.toggleRight() }
  });
  right.append(rightBtn);

  const settingsBtn = h('button', {
    class: 'icon-btn',
    type: 'button',
    'aria-label': '设置',
    title: '设置',
    html: icon('settings', 16),
    on: { click: () => ctx.openSettings() }
  });
  right.append(settingsBtn);

  container.append(left, right);
}

export function defaultModel(state) {
  const options = state.models.options || [];
  const provider =
    options.find((o) => o.id === state.models.default_provider) || options[0] || null;
  return (provider && (provider.default_model || (provider.models || [])[0])) || '默认模型';
}

export function modelLabel(state) {
  return state.ui.model || defaultModel(state);
}

