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

/* v4.20.0：状态胶囊的局部重绘。app.js 的 topbarSig 不含 paused（本线程只改组件层），
   顶栏在这里自行订阅 store：running/paused/进度/状态标签变化时只重绘胶囊本身，
   不整栏重绘；路由离开作品页或无 work 时置空 painter，订阅保持空转。 */
let chipPainter = null;
let chipSig = '';
let chipUnsub = null;

function workChipSig(work) {
  if (!work) return '';
  return [
    Boolean(work.running),
    Boolean(work.paused),
    work.done,
    work.total,
    work.state || '',
    work.status_label || ''
  ].join('~');
}

/* v4.20.0：胶囊四态——暂停请求已发出（running&&paused）、已暂停、写作中、空闲/完成 */
function paintWorkChip(chip, work) {
  const running = Boolean(work && work.running);
  const paused = Boolean(work && work.paused);
  const pct = work && work.total ? Math.round((work.done / work.total) * 100) : 0;
  let dotClass = 'dot';
  let label = '';
  let title = '查看创作状态';
  if (running && paused) {
    dotClass = 'dot dot-warn';
    label = `暂停中… · ${work.done}/${work.total}`;
    title = '暂停请求已发出，当前章节完成后停止';
  } else if (paused) {
    dotClass = 'dot dot-warn';
    label = `已暂停 · ${work.done}/${work.total}`;
    title = '已人工暂停，点击查看实时状态';
  } else if (running) {
    dotClass = 'dot dot-running';
    label = `写作中 · ${work.done}/${work.total}`;
    title = '正在创作，点击查看实时状态';
  } else {
    label = `${(work && work.status_label) || '空闲'} · ${pct}%`;
  }
  chip.title = title;
  chip.textContent = '';
  chip.append(h('span', { class: dotClass }), h('span', { class: 'tb-label', text: label }));
}

function ensureChipSubscription() {
  if (chipUnsub) return;
  chipUnsub = store.subscribe((state) => {
    /* work 短暂为空（切书间隙）不重绘，保持旧文案等整栏重渲染接管 */
    if (!chipPainter || !state.work) return;
    const sig = workChipSig(state.work);
    if (sig === chipSig) return;
    chipSig = sig;
    chipPainter();
  });
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
        h('span', { class: 'crumb-current', text: chapterLabel(store.get().workMedium, route.chapter) })
      );
    }
  }
  left.append(crumbs);

  const right = h('div', { class: 'topbar-right' });

  // work live chip（v4.20.0：增加「暂停中…/已暂停」态，warn 色 dot；paused 变化由自订阅收敛）
  if (route.name === 'work' || route.name === 'bench') {
    const work = state.work;
    if (work) {
      const chip = h(
        'button',
        {
          class: 'tb-chip',
          type: 'button',
          on: {
            click: () => {
              ctx.openRight('work');
            }
          }
        }
      );
      paintWorkChip(chip, work);
      chipSig = workChipSig(work);
      chipPainter = () => {
        chipSig = workChipSig(store.get().work);
        paintWorkChip(chip, store.get().work);
      };
      right.append(chip);
      ensureChipSubscription();
    } else {
      chipPainter = null;
    }
  } else {
    chipPainter = null;
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

function defaultModel(state) {
  const options = state.models.options || [];
  const provider =
    options.find((o) => o.id === state.models.default_provider) || options[0] || null;
  return (provider && (provider.default_model || (provider.models || [])[0])) || '默认模型';
}

/* v4.14.0：顶栏章节标签按文体定制，不用「第 N 章」糊弄所有体裁 */
function chapterLabel(medium, chapter) {
  const n = chapter || 1;
  const units = {
    novel: '章', script: '场', poetry: '首', prose: '阶段',
    thesis: '章', children: '篇', news: '篇', copy: '篇', other: '节'
  };
  const u = units[medium] || '章';
  return `第 ${n} ${u}`;
}