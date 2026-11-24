/* 顶栏：面包屑、实时状态、模型选择、主题、右栏开关。 */

import { h, icon } from '../core/dom.js';
import { store } from '../core/state.js';
import { popoverMenu } from '../core/ui.js';
import {
  clearModelChoice,
  globalTextModel,
  selectableTextProviders
} from '../core/prefs.js';

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
    work.status_label || '',
    work.pausedReason || '',
    work.model_unavailable ? 1 : 0
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

/* app.js 的整栏签名覆盖绑定值/全局默认，但 model_unavailable 是严格的书级
   错误态，必须在同一本书内单独刷新芯片，不能等下一次路由或整栏重绘。 */
let modelPainter = null;
let modelSig = '';
let modelUnsub = null;

function modelChipSig(state) {
  const work = state.work || {};
  const global = globalTextModel(state);
  return [
    state.currentKey || '',
    work.model_bound ? 1 : 0,
    work.model || '',
    work.model_provider_id || '',
    work.model_unavailable ? 1 : 0,
    global.providerId,
    global.model
  ].join('~');
}

function ensureModelSubscription() {
  if (modelUnsub) return;
  modelUnsub = store.subscribe((state) => {
    if (!modelPainter) return;
    const sig = modelChipSig(state);
    if (sig === modelSig) return;
    modelSig = sig;
    modelPainter();
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
  // v5.9.0（死规律）：只有书级 model_bound=true 才显示 work.model；未绑定明确显示
  // 全局文本默认，绝不把最近选择或图像默认冒充当前全局模型。
  const bookBound = Boolean(state.currentKey && state.work && state.work.model_bound);
  const modelUnavailable = Boolean(bookBound && state.work.model_unavailable);
  const globalChoice = globalTextModel(state);
  const modelName = modelUnavailable
    ? '绑定模型不可用，请更换'
    : (bookBound
      ? (state.work.model || '绑定模型')
      : `全局默认 · ${globalChoice.model || '未配置'}`);
  const modelLabel = h('span', { class: 'tb-label mono', text: modelName });
  const modelBadge = h('span', {
    style: {
      fontSize: '10px', lineHeight: '1', padding: '3px 6px', borderRadius: '999px',
      border: '1px solid var(--accent)', color: 'var(--accent)', whiteSpace: 'nowrap'
    },
    text: bookBound ? '本书绑定' : '全局默认'
  });
  const modelChip = h(
    'button',
    {
      class: 'tb-chip',
      type: 'button',
      'aria-haspopup': 'menu',
      title: modelUnavailable
        ? (state.work.model_note || '绑定模型不可用，请更换')
        : (bookBound
          ? `本书绑定：${state.work.model_provider_name || state.work.model_provider_id || ''} · ${state.work.model}（创作全程使用）`
          : `全局默认模型：${globalChoice.providerId || '未配置'} · ${globalChoice.model || '未配置'}`),
      on: {
        click: (event) => {
          // 局部订阅只更新芯片文本时不会重建按钮；菜单必须现场读 store，
          // 尤其 model_unavailable 不能被渲染时的旧闭包吞掉。
          const state = store.get();
          const bookBound = Boolean(state.currentKey && state.work && state.work.model_bound);
          const modelUnavailable = Boolean(bookBound && state.work.model_unavailable);
          const globalChoice = globalTextModel(state);
          const options = selectableTextProviders(state.models.options)
            .filter((o) => o.models && o.models.length);
          // 生效口径：已绑书 → 书级绑定；未绑定 → 全局文本默认。
          const effPid = bookBound
            ? (state.work.model_provider_id || '')
            : globalChoice.providerId;
          const effModel = bookBound ? (state.work.model || '') : globalChoice.model;
          const items = [];
          if (modelUnavailable) {
            items.push({ label: '绑定模型不可用，请更换', icon: 'alert', disabled: true });
          }
          for (const provider of options) {
            items.push({ label: provider.name, icon: 'cpu', disabled: true });
            for (const m of provider.models) {
              const isBoundRow = bookBound
                && provider.id === state.work.model_provider_id && m === state.work.model;
              items.push({
                label: m,
                sub: isBoundRow ? '本书绑定' : '',
                icon: 'dot',
                checked: provider.id === effPid && m === effModel,
                onSelect: () => {
                  // v5.9.0：分流在 ctx.setModel 里做（有书→本书绑定端点；无书→全局默认），
                  // 成功/失败 toast 也由它负责——这里的乐观 toast 会把失败说成成功，已删。
                  ctx.setModel(provider.id, m);
                }
              });
            }
          }
          if (!options.length) {
            items.push({
              label: '还没有可用模型',
              icon: 'plug',
              onSelect: () => ctx.openSettings('providers')
            });
          }
          items.push({ separator: true });
          if (bookBound) {
            items.push({
              label: '解除本书绑定（回落全局默认）',
              icon: 'x',
              onSelect: async () => {
                await ctx.unbindBookModel?.();
                /* app.js 解绑成功后异步刷新 work；若已同步落地则立即清，否则由
                   prefs 的 model_bound true→false 订阅在 status 到达时清。 */
                const work = store.get().work;
                if (work && !work.model_bound) clearModelChoice();
              }
            });
          }
          items.push(
            { label: '管理模型与密钥…', icon: 'settings', onSelect: () => ctx.openSettings('providers') }
          );
          popoverMenu(event.currentTarget, items, {
            label: bookBound ? '本书创作模型（绑定后全程使用）' : '全局默认模型（新作品默认使用）'
          });
        }
      }
    },
    h('span', { html: icon('cpu', 14) }),
    modelLabel,
    modelBadge,
    h('span', { class: 'chev', html: icon('chevron-down', 12) })
  );
  modelSig = modelChipSig(state);
  modelPainter = () => {
    const current = store.get();
    const bound = Boolean(current.currentKey && current.work && current.work.model_bound);
    const unavailable = Boolean(bound && current.work.model_unavailable);
    const global = globalTextModel(current);
    modelLabel.textContent = unavailable
      ? '绑定模型不可用，请更换'
      : (bound ? (current.work.model || '绑定模型') : `全局默认 · ${global.model || '未配置'}`);
    modelBadge.textContent = bound ? '本书绑定' : '全局默认';
    modelChip.title = unavailable
      ? (current.work.model_note || '绑定模型不可用，请更换')
      : (bound
        ? `本书绑定：${current.work.model_provider_name || current.work.model_provider_id || ''} · ${current.work.model}（创作全程使用）`
        : `全局默认模型：${global.providerId || '未配置'} · ${global.model || '未配置'}`);
    modelSig = modelChipSig(current);
  };
  right.append(modelChip);
  ensureModelSubscription();

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

/* v4.14.0：顶栏章节标签按文体定制，不用「第 N 章」糊弄所有体裁 */
function chapterLabel(medium, chapter) {
  const n = chapter || 1;
  const units = {
    novel: '章', script: '场', poetry: '首', prose: '阶段',
    thesis: '章', children: '篇', news: '阶段', copy: '阶段', other: '阶段'
  };
  const u = units[medium] || '章';
  return `第 ${n} ${u}`;
}