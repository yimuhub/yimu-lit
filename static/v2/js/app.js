/* 应用装配：外壳渲染、路由、数据加载、右栏与抽屉、拖拽调宽、快捷键与实时日志。 */

import {
  store,
  patch,
  patchUi,
  resetChat
} from './core/state.js';
import { initTheme, setTheme, toggleTheme } from './core/theme.js';
import { loadPrefs, persistLayout } from './core/prefs.js';
import { startRouter, navigate, routePath } from './core/router.js';
import { api, errorText } from './core/api.js';
import { installHotkeys } from './core/hotkeys.js';
import { createLiveWatcher, pollBookLive } from './core/sse.js';
import { toast, closeTopOverlay, closePopover } from './core/ui.js';
import { clamp, h, debounce } from './core/dom.js';
import { isMediumBench } from './core/constants.js';
import { loadCss } from './core/loadcss.js';
import { renderSidebar } from './components/sidebar.js';
import { renderTopbar } from './components/topbar.js';
import { renderRightPanel } from './components/rightpanel.js';
/* 命令面板（Cmd+K）按需加载；视图模块在 renderView 里按路由动态 import，
   首屏只保留 welcome/session（对话主界面）及其组件链。 */
import { renderWelcome } from './views/welcome.js';
import { renderSession } from './views/session.js';

/* 路由级 CSS 与 JS 共用同一份版本号（与 index.html 的 ?v= 保持一致）。 */
const CSS_V = '290';

const els = {
  app: document.getElementById('app'),
  sidebar: document.getElementById('sidebar'),
  topbar: document.getElementById('topbar'),
  view: document.getElementById('view'),
  rightbar: document.getElementById('rightbar'),
  handleLeft: document.getElementById('handleLeft'),
  handleRight: document.getElementById('handleRight'),
  rightBackdrop: document.getElementById('rightBackdrop'),
  drawerBackdrop: document.getElementById('drawerBackdrop')
};

let currentView = null;
let wizardModal = null;
/* 命令面板模块（Cmd+K 才加载）：commandModP 是加载 promise，commandMod 只在
   加载完成后才有值——Escape 判断 isCommandOpen 时模块没加载就等于没打开。 */
let commandMod = null;
let commandModPromise = null;
function loadCommand() {
  if (!commandModPromise) {
    commandModPromise = import('./components/command.js').then((m) => {
      commandMod = m;
      return m;
    });
  }
  return commandModPromise;
}
let liveStop = null;
let pollStop = null;
let progressTimer = null;
let loadedKey = '';
const inflight = new Set();
const mediumCache = new Map();

/* 非小说创建是后台线程，目录落盘前作品库还查不到——先用乐观条目占住侧栏。 */
const PENDING_TTL = 120000;
let pendingPoll = 0;

function mergePending(fetched) {
  const now = Date.now();
  const keep = [];
  for (const it of store.get().library.items) {
    if (!(it.pending || it.failed)) continue;
    if (fetched.some((f) => f.key === it.key)) continue;
    if (it.pending && now - (it.pendingSince || 0) > PENDING_TTL) {
      keep.push({ ...it, pending: false, failed: true, error: '创建超时，请重试或移除' });
    } else {
      keep.push(it);
    }
  }
  return [...keep, ...fetched];
}

function schedulePendingPoll() {
  if (pendingPoll) return;
  pendingPoll = setInterval(async () => {
    const pending = store.get().library.items.filter((i) => i.pending);
    if (!pending.length) {
      clearInterval(pendingPoll);
      pendingPoll = 0;
      return;
    }
    let changed = false;
    for (const it of pending) {
      try {
        const info = await api.project(it.key);
        if (info && info.key) {
          patch('library', {
            items: store.get().library.items.map((x) => (x.key === it.key ? { ...x, ...info, pending: false, failed: false } : x))
          });
          mediumCache.set(it.key, info.medium || it.medium || 'novel');
          changed = true;
        }
      } catch {
        /* 404 表示规划线程还没落盘，继续等；超时由 mergePending 兜底 */
      }
    }
    if (changed) syncShell(store.get(), true);
  }, 2500);
}

/* 视图按文体分岔，而路由只带 key：文体先解析出来，才谈得上渲染哪套工作台。 */
function mediumOf(key) {
  if (!key) return '';
  if (mediumCache.has(key)) return mediumCache.get(key);
  const item = store.get().library.items.find((i) => i.key === key);
  if (!item) return '';
  const medium = item.medium || 'novel';
  mediumCache.set(key, medium);
  return medium;
}

async function resolveMedium(key) {
  const res = await api.project(key);
  const medium = (res && res.medium) || 'novel';
  mediumCache.set(key, medium);
  return medium;
}


/* ---------------------------------------------------------------- ctx */

/* 五份 refresh（work/plan/state/refs/skills）的同构骨架：
   inflight 防重入 → loader 拉数 → 成功 onOk / 404 降级 onMissing / 报错 errorKey。
   各自的差异全部收敛进回调——refreshWork 的 merge 也只是自定义 onOk。 */
async function guardedRefresh(name, key, { requireKey = true, loader, onOk, onMissing = null, errorKey, errorFallback }) {
  if (inflight.has(name)) return;
  if (requireKey && !key) return;
  inflight.add(name);
  try {
    const res = await loader(key);
    onOk(res);
  } catch (err) {
    if (err && err.isMissing && onMissing) onMissing();
    else patch(errorKey, errorText(err, errorFallback));
  } finally {
    inflight.delete(name);
  }
  syncShell(store.get());
}

const ctx = {
  navigate,
  /* 刚创建的作品还没落进作品库，先告诉路由它是什么文体，省一次"作品不存在"的竞态。 */
  rememberMedium(key, medium) {
    if (key) mediumCache.set(key, medium || 'novel');
  },
  /* 向导模块按需加载（views/wizard.js 不属于首屏闭包）。打开中的窗口直接复用；
     await 之后再查一次 wizardModal，双击/连点不会开出两个。 */
  async openWizard(preset = {}) {
    if (wizardModal) return wizardModal;
    const mod = await import('./views/wizard.js');
    if (wizardModal) return wizardModal;
    wizardModal = mod.openWizard(ctx, preset);
    const inner = wizardModal.close;
    wizardModal.close = () => {
      wizardModal = null;
      inner();
    };
    return wizardModal;
  },
  openSettings(tab = '') {
    if (tab) patchUi({ settingsTab: tab });
    navigate('/settings');
    ctx.closeDrawer();
  },
  openRight(tab = '') {
    patchUi({ rightOpen: true, ...(tab ? { rightTab: tab } : {}) });
    applyLayout();
    if (tab) ctx.ensureTab(tab);
    syncShell(store.get(), true);
  },
  openPlan() {
    ctx.openRight('plan');
  },
  closeRight() {
    patchUi({ rightOpen: false });
    applyLayout();
    syncShell(store.get(), true);
  },
  toggleRight() {
    if (store.get().ui.rightOpen) ctx.closeRight();
    else ctx.openRight(store.get().ui.rightTab || 'work');
  },
  ensureTab(tab) {
    const state = store.get();
    if (!state.currentKey) return;
    if (tab === 'work' && !state.work) ctx.refreshWork();
    else if (tab === 'plan' && !state.plan && !inflight.has('plan')) ctx.refreshPlan();
    else if (tab === 'state' && !state.lore && !inflight.has('state')) ctx.refreshState();
    else if (tab === 'refs' && !state.refs.length && !inflight.has('refs')) ctx.refreshRefs();
    else if (tab === 'skills' && !state.skills && !inflight.has('skills')) ctx.refreshSkills();
    syncShell(store.get());
  },
  rerenderPanel() {
    syncShell(store.get(), true);
  },
  toggleSidebar() {
    patchUi({ sidebarExpanded: !store.get().ui.sidebarExpanded });
    applyLayout();
    syncShell(store.get(), true);
  },
  toggleDrawer() {
    patchUi({ drawerOpen: !store.get().ui.drawerOpen });
    applyLayout();
  },
  closeDrawer() {
    if (!store.get().ui.drawerOpen) return;
    patchUi({ drawerOpen: false });
    applyLayout();
  },
  toggleTheme() {
    toggleTheme();
    applyLayout();
    syncShell(store.get(), true);
  },
  setTheme(pref) {
    setTheme(pref);
    syncShell(store.get(), true);
  },
  setModel(providerId, model) {
    patchUi({ providerId: providerId || null, model: model || null });
    syncShell(store.get(), true);
  },
  onMediumChange(medium) {
    if (medium && medium !== store.get().ui.medium) patchUi({ medium });
  },
  openBench(n) {
    const key = store.get().currentKey;
    if (!key) return;
    patchUi({ benchChapter: Number(n) || 1 });
    navigate(routePath('bench', { key, chapter: n }));
  },
  chapterList() {
    const plan = (store.get().plan && store.get().plan.plan) || {};
    if (Array.isArray(plan.chapters) && plan.chapters.length) {
      return plan.chapters.map((c) => ({ n: c.n || c.chapterNumber, title: c.title || '' }));
    }
    const list = [];
    for (const vol of plan.volumes || []) {
      for (const arc of vol.arcs || []) {
        const cards = arc.chapter_cards || [];
        if (cards.length) {
          for (const c of cards) list.push({ n: c.n || c.chapterNumber, title: c.title || '' });
        } else {
          for (const n of arc.chapters || []) list.push({ n, title: `第${n}章` });
        }
      }
    }
    return list.sort((a, b) => a.n - b.n);
  },

  /* 创建接口一返回就插入侧栏，别等磁盘落盘；之后由轮询把占位项转正。 */
  addPendingWork({ key, name, medium } = {}) {
    if (!key) return null;
    const now = new Date().toISOString();
    const item = {
      key,
      name: (name || '').trim() || '新作品',
      medium: medium || 'novel',
      state: 'planning',
      status_label: '规划中',
      done: 0,
      total: 0,
      total_words: 0,
      pending: true,
      pendingSince: Date.now(),
      created_at: now,
      updated_at: now
    };
    patch('library', { items: [item, ...store.get().library.items.filter((i) => i.key !== key)] });
    mediumCache.set(key, item.medium);
    syncShell(store.get(), true);
    schedulePendingPoll();
    return item;
  },

  async refreshLibrary() {
    try {
      const res = await api.library();
      const items = mergePending((res && res.items) || []);
      /* v4.12.2：指纹对比——只有数据变化才 patch（避免轮询反复重建 68+ 张卡 DOM） */
      const fp = items.map(i => i.key + ':' + (i.updated_at || '')).join('|');
      const prev = store.get().library;
      const prevFp = (prev.items || []).map(i => i.key + ':' + (i.updated_at || '')).join('|');
      if (fp !== prevFp || !prev.loaded) {
        patch('library', {
          items,
          stats: (res && res.stats) || {},
          facets: (res && res.facets) || {},
          counts: (res && res.counts) || {},
          loading: false,
          loaded: true,
          error: null
        });
        if (res && res.items) patch('projects', items);
      } else {
        patch('library', { loading: false, loaded: true, error: null });
      }
      if (items.some((i) => i.pending)) schedulePendingPoll();
    } catch (err) {
      patch('library', { loading: false, error: errorText(err, '作品库加载失败') });
    }
    syncShell(store.get());
  },

  async refreshWork() {
    const key = store.get().currentKey;
    await guardedRefresh('work', key, {
      loader: () => api.status(key),
      onOk: (status) => {
        /* status 只带进度；侧栏条目带收藏/标签/字数——两者合并成一份 work。 */
        const item = store.get().library.items.find((i) => i.key === key) || null;
        const merged = {
          ...(item || {}),
          ...(status || {}),
          total_words: (item && item.total_words) || (status && status.words) || 0,
          status_label: (status && status.status_label) || (item && item.status_label) || '',
          quality: item ? item.quality : null,
          favorite: item ? item.favorite : false,
          tags: (item && item.tags) || []
        };
        patch('work', merged);
        patch('workError', null);
      },
      errorKey: 'workError',
      errorFallback: '作品状态加载失败'
    });
  },

  async refreshPlan() {
    const key = store.get().currentKey;
    await guardedRefresh('plan', key, {
      loader: () => api.plan(key),
      onOk: (res) => {
        patch('plan', res || null);
        patch('planError', null);
        patch('features', { plan: true });
      },
      onMissing: () => {
        patch('plan', null);
        patch('features', { plan: false });
      },
      errorKey: 'planError',
      errorFallback: '规划加载失败'
    });
  },

  async refreshState() {
    const key = store.get().currentKey;
    await guardedRefresh('state', key, {
      loader: () => api.state(key),
      onOk: (res) => {
        patch('lore', res || null);
        patch('loreError', null);
        patch('features', { state: true });
      },
      onMissing: () => {
        patch('lore', null);
        patch('features', { state: false });
      },
      errorKey: 'loreError',
      errorFallback: '状态库加载失败'
    });
  },

  async refreshRefs() {
    const key = store.get().currentKey;
    await guardedRefresh('refs', key, {
      loader: () => api.references(key),
      onOk: (res) => {
        patch('refs', Array.isArray(res) ? res : (res && res.items) || []);
        patch('refsError', null);
        patch('features', { references: true });
      },
      onMissing: () => {
        patch('refs', []);
        patch('features', { references: false });
      },
      errorKey: 'refsError',
      errorFallback: '参考资料加载失败'
    });
  },

  async refreshSkills() {
    await guardedRefresh('skills', null, {
      requireKey: false,
      loader: () => api.skillsEvolution(),
      onOk: (res) => {
        patch('skills', res || null);
        patch('skillsError', null);
        patch('features', { skills: true });
      },
      onMissing: () => {
        patch('skills', null);
        patch('features', { skills: false });
      },
      errorKey: 'skillsError',
      errorFallback: '技能数据加载失败'
    });
  }
};

/* ------------------------------------------------------------ layout */

const CENTER_MIN = 480;

/* 三栏尺寸的单一事实源：先保住中栏 480px，再分配左右。 */
function layoutMetrics() {
  const ui = store.get().ui;
  const vw = window.innerWidth;
  const expandedMax = Math.max(200, Math.min(560, vw - CENTER_MIN - 320));
  const sbCol = ui.sidebarExpanded ? clamp(ui.sidebarWidth || 280, 200, expandedMax) : 56;
  const dockMax = Math.max(320, Math.min(900, Math.round(vw * 0.62), vw - CENTER_MIN - sbCol));
  return { vw, sbCol, expandedMax, dockMax };
}

function applyLayout() {
  const app = els.app;
  const ui = store.get().ui;
  const narrow = window.innerWidth < 760;
  const m = layoutMetrics();
  const canDock = window.innerWidth >= 1180 && m.vw - m.sbCol - 320 >= CENTER_MIN;

  app.dataset.sidebar = ui.sidebarExpanded ? 'expanded' : 'rail';
  app.dataset.drawer = narrow && ui.drawerOpen ? 'open' : 'closed';
  els.drawerBackdrop.hidden = !(narrow && ui.drawerOpen);

  let mode = 'closed';
  if (ui.rightOpen) mode = canDock ? 'docked' : 'overlay';
  app.dataset.right = mode;

  /* 拖拽中由 pointermove 直接写 CSS 变量，这里不能回写，否则每帧被打回旧宽度。 */
  if (app.dataset.dragging !== 'true') {
    app.style.setProperty('--sidebar-w', `${m.sbCol}px`);
    const dockW = clamp(ui.rightWidth || Math.round(window.innerWidth * 0.45), 320, m.dockMax);
    app.style.setProperty('--right-col', mode === 'docked' ? `${dockW}px` : '0px');
    const overlayMax = Math.max(360, Math.min(720, window.innerWidth - 32));
    app.style.setProperty('--overlay-w', `${clamp(ui.rightWidth || 440, 360, overlayMax)}px`);
  }

  els.rightBackdrop.hidden = mode !== 'overlay';
}

function bindResizers() {
  const app = els.app;

  function varPx(name, fallback) {
    const raw = parseFloat(app.style.getPropertyValue(name));
    return Number.isFinite(raw) ? raw : fallback;
  }

  function track(handle, { move, commit }) {
    let start = null;

    const finish = () => {
      if (!start) return;
      const s = start;
      start = null;
      app.dataset.dragging = 'false';
      handle.dataset.active = 'false';
      commit(s);
      applyLayout();
      syncShell(store.get(), true);
    };

    handle.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      const ui = store.get().ui;
      start = {
        x: event.clientX,
        rail: !ui.sidebarExpanded,
        sidebarW: varPx('--sidebar-w', ui.sidebarWidth || 280),
        rightW:
          app.dataset.right === 'overlay'
            ? varPx('--overlay-w', ui.rightWidth || 440)
            : varPx('--right-col', ui.rightWidth || Math.round(window.innerWidth * 0.45) || 440),
        moved: false
      };
      try {
        handle.setPointerCapture(event.pointerId);
      } catch {
        /* ignore */
      }
      app.dataset.dragging = 'true';
      handle.dataset.active = 'true';
      event.preventDefault();
    });

    handle.addEventListener('pointermove', (event) => {
      if (!start) return;
      const dx = event.clientX - start.x;
      if (Math.abs(dx) > 2) start.moved = true;
      move(dx, start);
    });

    handle.addEventListener('pointerup', finish);
    handle.addEventListener('pointercancel', finish);

    handle.addEventListener('dblclick', (event) => {
      event.preventDefault();
      commit({ reset: true, moved: true });
      applyLayout();
      syncShell(store.get(), true);
    });

    handle.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const step = (event.shiftKey ? 32 : 8) * (event.key === 'ArrowRight' ? 1 : -1);
      const s = {
        x: 0,
        rail: false,
        sidebarW: varPx('--sidebar-w', store.get().ui.sidebarWidth || 280),
        rightW:
          app.dataset.right === 'overlay'
            ? varPx('--overlay-w', store.get().ui.rightWidth || 440)
            : varPx('--right-col', store.get().ui.rightWidth || 520),
        moved: true
      };
      move(step, s);
      commit(s);
      applyLayout();
      syncShell(store.get(), true);
    });
  }

  track(els.handleLeft, {
    move(dx, s) {
      if (!s.moved) return;
      const max = layoutMetrics().expandedMax;
      let w;
      if (s.rail) {
        if (dx < 8) return;
        app.dataset.sidebar = 'expanded';
        w = clamp(200 + (dx - 8), 200, max);
      } else {
        w = clamp(s.sidebarW + dx, 200, max);
      }
      s.w = w;
      app.style.setProperty('--sidebar-w', `${w}px`);
    },
    commit(s) {
      if (s.reset) {
        patchUi({ sidebarWidth: 280, sidebarExpanded: true });
        persistLayout({ sidebarWidth: 280, sidebarExpanded: true });
        return;
      }
      if (!s.w) return;
      const w = Math.round(s.w);
      patchUi({ sidebarWidth: w, sidebarExpanded: true });
      persistLayout({ sidebarWidth: w, sidebarExpanded: true });
    }
  });

  track(els.handleRight, {
    move(dx, s) {
      if (!s.moved) return;
      if (app.dataset.right === 'overlay') {
        const max = Math.max(360, Math.min(720, window.innerWidth - 32));
        const w = clamp(s.rightW - dx, 360, max);
        s.w = w;
        app.style.setProperty('--overlay-w', `${w}px`);
        return;
      }
      const w = clamp(s.rightW - dx, 320, layoutMetrics().dockMax);
      s.w = w;
      app.style.setProperty('--right-col', `${w}px`);
    },
    commit(s) {
      const w = s.reset ? 520 : Math.round(s.w || 0);
      if (!w) return;
      patchUi({ rightWidth: w, rightOpen: true });
      persistLayout({ rightWidth: w });
    }
  });
}

/* ------------------------------------------------------------- shell */

const sigs = { sidebar: '', topbar: '', right: '' };

function syncShell(state, force = false) {
  if (!els.app) return;
  const itemsSig = state.library.items
    .map((i) => [i.key, i.name, i.state, i.status_label, i.done, i.total, i.total_words, i.favorite, i.archived, i.trashed, i.pending, i.failed, i.genre, (i.tags || []).join('|'), i.updated_at].join('~'))
    .join(',');
  const sidebarSig = [
    itemsSig,
    state.library.loading,
    state.library.loaded,
    state.route.key,
    state.route.name,
    state.ui.sidebarExpanded,
    state.ui.showClassic
  ].join('|');
  if (force || sidebarSig !== sigs.sidebar) {
    sigs.sidebar = sidebarSig;
    renderSidebar(els.sidebar, ctx);
  }

  const topbarSig = [
    state.route.name,
    state.route.key,
    state.route.chapter,
    state.work ? [state.work.name, state.work.running, state.work.done, state.work.total, state.work.status_label].join('~') : '',
    state.ui.model,
    state.ui.providerId,
    state.ui.rightOpen,
    state.resolvedTheme,
    state.models.default_provider,
    (state.models.options || []).length
  ].join('|');
  if (force || topbarSig !== sigs.topbar) {
    sigs.topbar = topbarSig;
    renderTopbar(els.topbar, ctx);
  }

  const plan = (state.plan && state.plan.plan) || {};
  const rightSig = [
    state.currentKey,
    state.ui.rightTab,
    state.ui.rightOpen,
    /* v4.21.0：对话产出卡「查看本章过程」→ 右栏档案页签联动 */
    (state.ui.chapterArchive && state.ui.chapterArchive.ts) || 0,
    JSON.stringify(state.features),
    state.work ? [state.work.running, state.work.done, state.work.total, state.work.state].join('~') : '',
    state.workError || '',
    plan.version || 0,
    Array.isArray(plan.chapters) ? plan.chapters.length : 0,
    ((state.plan && state.plan.proposals) || []).map((p) => `${p.id}:${p.status}`).join(','),
    (state.refs || []).map((r) => `${r.id}:${r.usage_mode}:${r.analyzed ? 1 : 0}`).join(','),
    state.skills ? [(state.skills.candidates || []).length, (state.skills.history || []).length, JSON.stringify(state.skills.stats || {})].join('~') : '',
    state.lore ? Object.keys(state.lore).length : 0,
    state.liveConnected ? 1 : 0
  ].join('|');
  if (force || rightSig !== sigs.right) {
    sigs.right = rightSig;
    renderRightPanel(els.rightbar, ctx);
  }
}

/* -------------------------------------------------------------- view */

/* 渲染序号：renderView 变 async 后，动态 import 的 await 窗口里用户可能已切
   到别的路由——旧渲染完成后 seq 对不上就直接丢弃，绝不覆盖新路由的视图。 */
let renderSeq = 0;

function spinnerViewEl(text) {
  return h('div', { class: 'view-scroll', style: { display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: '60vh' } },
    h('div', { style: { textAlign: 'center' } },
      h('div', { class: 'spinner', style: { width: '24px', height: '24px', margin: '0 auto 12px', border: '3px solid var(--line-2)', borderTopColor: 'var(--accent)', borderRadius: '50%', animation: 'spin 0.8s linear infinite' } }),
      h('p', { class: 'hint-text', text })));
}

function destroyCurrentView() {
  if (currentView && currentView.destroy) {
    try {
      currentView.destroy();
    } catch (err) {
      console.error('[view] destroy failed', err);
    }
  }
  currentView = null;
}

async function renderView(route) {
  const seq = ++renderSeq;
  destroyCurrentView();

  /* medium 在任何 await 之前读定：模块加载期间 store 再怎么变，本次渲染口径不变。 */
  const medium = route.key ? store.get().workMedium : '';
  /* v4.12.15：首次加载 library 未到 → mediumOf(key) 返回 '' → 不渲染错误视图，
     改为渲染加载态，library 到位后由 store 订阅触发重渲染（替代原 setTimeout hack，
     避免深链首屏渲染两次与 currentView=null 导致的订阅泄漏） */
  if (!medium && (route.name === 'work' || route.name === 'bench')) {
    const loadingEl = spinnerViewEl('正在加载作品…');
    els.view.textContent = '';
    els.view.append(loadingEl);
    const unsub = store.subscribe((s2) => {
      if (s2.library.loaded && s2.library.items.length) {
        unsub();
        renderView(route);
      }
    });
    currentView = { el: loadingEl, destroy() { unsub(); } };
    return;
  }

  /* render: (container) => view handle。welcome/session 是首屏静态模块；
     其余路由视图按需 import，同时补上各自的按需 CSS。 */
  let render = null;
  if (route.name === 'work') render = renderSession;
  else if (route.name === 'welcome') render = renderWelcome;
  else {
    els.view.textContent = '';
    els.view.append(spinnerViewEl('正在加载…'));
    try {
      if (route.name === 'library') render = (await import('./views/library.js')).renderLibrary;
      else if (route.name === 'settings') {
        loadCss(`/v2/css/settings.css?v=${CSS_V}`);
        render = (await import('./views/settings.js')).renderSettings;
      }
      else if (route.name === 'evolution') render = (await import('./views/evolution.js')).renderEvolution;
      else if (route.name === 'skills') {
        loadCss(`/v2/css/skills.css?v=${CSS_V}`);
        render = (await import('./views/skilllib.js')).renderSkillLib;
      }
      /* v4.12.1：全部文体默认走对话界面；工作台通过 #/work/<key>/bench/<n> 路由或右栏按钮进入 */
      else if (isMediumBench(medium) && route.name === 'bench') {
        loadCss(`/v2/css/chatfloat.css?v=${CSS_V}`);
        render = (await import('./views/mediumbench.js')).renderMediumBench;
      }
      else if (route.name === 'bench') {
        /* 章节工作台同样挂对话浮窗（views/bench.js 内 mountChatFloat）。 */
        loadCss(`/v2/css/chatfloat.css?v=${CSS_V}`);
        render = (await import('./views/bench.js')).renderBench;
      }
      if (seq !== renderSeq) return;
    } catch (err) {
      if (seq !== renderSeq) return;
      console.error('[view] load failed', err);
      els.view.textContent = '';
      els.view.append(spinnerViewEl('视图加载失败，请刷新重试'));
      return;
    }
  }

  if (seq !== renderSeq) return;
  els.view.textContent = '';
  currentView = render(els.view, ctx);

  const state = store.get();
  const item = state.library.items.find((i) => i.key === state.currentKey);
  const title =
    route.name === 'work' || route.name === 'bench'
      ? (state.work && state.work.name) || (item && item.name) || '作品'
      : { welcome: '新会话', library: '作品库', skills: '技能库', evolution: '技能进化', settings: '设置' }[route.name] || '一幕·YIMU 神笔';
  document.title = `${title} · 一幕·YIMU 神笔创作台`;
}

/* 冷启动深链只带 key，文体要问一次后端；这期间先占位，避免把剧本当小说渲染。 */
function renderPendingView() {
  /* 占位也是一次"渲染"：作废仍在 await 模块的旧渲染，防止它完成时覆盖占位。 */
  renderSeq++;
  destroyCurrentView();
  els.view.textContent = '';
  const box = h('div', { class: 'view-scroll' }, h('div', { class: 'session-loading' }, h('span', { class: 'skeleton skeleton-line', style: { width: '40%' } })));
  els.view.append(box);
}

function stopLive() {
  if (liveStop) {
    liveStop.close();
    liveStop = null;
  }
  if (pollStop) {
    pollStop();
    pollStop = null;
  }
  if (progressTimer) {
    clearInterval(progressTimer);
    progressTimer = null;
  }
}

function startLive(key) {
  stopLive();
  liveStop = createLiveWatcher(key, {
    onLog(entry) {
      patch('live', (arr) => {
        const next = [...arr, { message: entry.message, level: entry.level || 'info', at: entry.at || Date.now() }];
        return next.slice(-200);
      });
      sigs.right = '';
      syncShell(store.get());
    },
    onStatus(data) {
      patch('liveConnected', Boolean(data && data.connected));
    },
    onDone() {
      ctx.refreshWork();
      ctx.refreshLibrary();
      /* v4.16.6：分章齐写结束 → 对话框汇报（之前启动过并行时） */
      try {
        const st = store.get();
        const wasParallel = (st.work && st.work.parallel) || window.__parallelJustStarted;
        if (wasParallel) {
          window.__parallelJustStarted = false;
          const w = st.work || {};
          store.set((s2) => {
            const msgs = [...((s2.chat && s2.chat.messages) || [])];
            msgs.push({ id: `par_done_${Date.now()}`, role: 'assistant',
                        content: `⚡ 分章齐写完成：当前 ${w.done || 0}/${w.total || 0} 章、约 ${(w.total_words || 0).toLocaleString()} 字。已停在本轮进度，说「再写 N 章」或「写完全部」继续，或逐章检查后进入审稿验收。` });
            return { ...s2, chat: { ...(s2.chat || {}), messages: msgs } };
          });
        }
      } catch { /* 不影响主流程 */ }
    }
  });
  /* v4.21 创作活动流：book-live 轮询内联化——同一份返回体既喂原有进度逻辑，
     也喂对话里的步骤卡（steps）与实时输出（stream）。请求带上 state 里的
     steps_cursor / stream_offset 续传游标；节奏与原 pollBookLive 一致：
     2.5s 间隔、页面隐藏暂停、防重入、错误静默（404/501 旧后端优雅降级）。
     mock 模式继续走原 pollBookLive（api.bookLive → mock 数据，无 steps/stream）。 */
  const applyLive = (data) => {
    patch('work', (w) => {
      if (!w) return w;
      const prog = data.progress || {};
      return {
        ...w,
        running: Boolean(data.running),
        paused: Boolean(data.paused),
        done: prog.done != null ? prog.done : w.done,
        total: prog.total != null ? prog.total : w.total,
        phase: data.phase || w.phase
      };
    });
    const lines = (data && data.live_log) || [];
    if (lines.length) {
      patch('live', (arr) => (arr.length >= lines.length ? arr : lines.map((m) => ({ message: m, level: 'info', at: Date.now() }))));
    }
  };

  /* steps 增量合并：首个返回批只作基线推进游标（进入书页不回放历史，
     历史在右栏「本章档案」查），之后按 no 去重追加，cap 300 条防内存。 */
  const mergeSteps = (bookKey, data) => {
    if (bookKey !== store.get().currentKey) return;
    const steps = data && data.steps;
    if (!steps || !Array.isArray(steps.items)) return;
    patch('steps', (prev) => {
      const incoming = steps.items.filter(Boolean);
      const maxNo = incoming.reduce((acc, it) => Math.max(acc, Number(it.no) || 0), 0);
      const srvCursor = Number(steps.cursor) || 0;
      if (!prev.baselined) {
        return { baselined: true, cursor: srvCursor || maxNo || prev.cursor || 0, items: prev.items };
      }
      const seen = new Set(prev.items.map((it) => Number(it.no) || 0));
      const fresh = incoming.filter((it) => {
        const no = Number(it.no) || 0;
        return no > 0 && !seen.has(no);
      });
      const nextCursor = srvCursor || Math.max(prev.cursor || 0, maxNo);
      if (!fresh.length && nextCursor === prev.cursor) return prev;
      return {
        baselined: true,
        cursor: nextCursor,
        items: [...prev.items, ...fresh].slice(-300)
      };
    });
  };

  /* stream 合并：text 按 offset 增量累加到本地缓冲；active=false 时 offset 归零
     （缓冲停止增长，文本保留给完成卡），下一轮流式开始（epoch+1）时重置缓冲。 */
  const mergeStream = (bookKey, data) => {
    if (bookKey !== store.get().currentKey) return;
    const stream = data && data.stream;
    if (!stream || typeof stream !== 'object') return;
    const piece = typeof stream.text === 'string' ? stream.text : '';
    const active = Boolean(stream.active) && !stream.done;
    patch('stream', (prev) => {
      if (active) {
        const starting = !prev.active;
        const text = (starting ? '' : prev.text || '') + piece;
        const srvOffset = Number(stream.offset);
        const offset =
          Number.isFinite(srvOffset) && srvOffset > 0
            ? srvOffset
            : (starting ? 0 : prev.offset || 0) + Array.from(piece).length;
        return {
          key: bookKey,
          active: true,
          doneAt: 0,
          epoch: starting ? (prev.epoch || 0) + 1 : prev.epoch || 1,
          agent: stream.agent || prev.agent || '',
          agent_label: stream.agent_label || prev.agent_label || '',
          title: stream.title || prev.title || '',
          chapter: Number(stream.chapter) || 0,
          offset,
          text
        };
      }
      if (!prev.active) {
        return prev.offset === 0 ? prev : { ...prev, offset: 0 };
      }
      return { ...prev, active: false, offset: 0, doneAt: Date.now() };
    });
  };

  if (store.get().mock) {
    pollStop = pollBookLive(key, applyLive, () => {}, 2500);
  } else {
    let liveTimer = 0;
    let liveStopped = false;
    let liveBusy = false;

    const liveTick = async () => {
      if (liveStopped || liveBusy || document.hidden) return;
      if (store.get().currentKey !== key) return;
      liveBusy = true;
      try {
        const s = store.get();
        const qs = new URLSearchParams({
          key,
          steps_cursor: String((s.steps && s.steps.cursor) || 0),
          stream_offset: String((s.stream && s.stream.offset) || 0)
        });
        const ctrl = new AbortController();
        const kill = setTimeout(() => ctrl.abort(), 12000);
        let data = null;
        try {
          const res = await fetch(`/api/book-live?${qs.toString()}`, {
            credentials: 'same-origin',
            headers: { Accept: 'application/json' },
            signal: ctrl.signal
          });
          if (res.ok) data = await res.json().catch(() => null);
          /* 404/501（旧后端）等非 2xx：静默降级，与原 pollBookLive 的 isMissing 一致 */
        } catch {
          /* 网络抖动 / 超时：静默，下个周期重试 */
        } finally {
          clearTimeout(kill);
        }
        if (liveStopped || document.hidden || !data) return;
        applyLive(data);
        mergeSteps(key, data);
        mergeStream(key, data);
      } finally {
        liveBusy = false;
      }
    };

    const liveLoop = () => {
      if (liveStopped) return;
      liveTick();
      liveTimer = setTimeout(liveLoop, 2500);
    };
    const liveOnVisibility = () => {
      if (!document.hidden) liveTick();
    };
    document.addEventListener('visibilitychange', liveOnVisibility);
    liveLoop();
    pollStop = () => {
      liveStopped = true;
      clearTimeout(liveTimer);
      document.removeEventListener('visibilitychange', liveOnVisibility);
    };
  }

  /* 全界面进度自动刷新：每 10 秒拉取一次规划状态、作品库列表与状态库，
     让左侧菜单栏进度、对话界面上方整体进度、右侧面板进度都实时更新，
     无需手动按刷新。仅在写作中或规划非 completed 时轮询。 */
  progressTimer = setInterval(async () => {
    const s = store.get();
    if (!s.currentKey) return;
    const w = s.work;
    if (w && (w.running || w.phase === 'planning' || (w.done != null && w.total != null && w.done < w.total))) {
      try {
        await Promise.allSettled([
          ctx.refreshPlan(),
          ctx.refreshState(),
          ctx.refreshLibrary(),
        ]);
      } catch (e) {
        /* 静默失败，下次 tick 再试 */
      }
    }
  }, 30000);   // v4.12.2：10s→30s
}

async function ensureWork(key) {
  if (!key || loadedKey === key) return;
  loadedKey = key;
  patch('work', null);
  patch('workError', null);
  /* v4.3.0：非小说也 refreshWork（/api/status 已全文体）+ startLive（创作过程实时可视）。
     规划/状态/参考仍按 features 门控，只有小说真正拉取。 */
  const mediumBench = isMediumBench(store.get().workMedium);
  await Promise.allSettled([
    ctx.refreshWork(),
    !mediumBench && ctx.refreshPlan(),
    !mediumBench && ctx.refreshState(),
    !mediumBench && ctx.refreshRefs(),
    ctx.refreshSkills()
  ]);
  const state = store.get();
  if (state.currentKey === key) {
    startLive(key);
    syncShell(state, true);
  }
}

function onRoute(route) {
  const prev = store.get();
  const key = route.name === 'work' || route.name === 'bench' ? route.key : '';

  if (key !== prev.currentKey) {
    loadedKey = '';
    stopLive();
    resetChat();
    store.set({
      currentKey: key,
      workMedium: mediumOf(key),
      work: null,
      workError: null,
      plan: null,
      planError: null,
      lore: null,
      loreError: null,
      refs: [],
      refsError: null,
      live: [],
      liveConnected: false
    });
    patchUi({ rightTab: 'work' });
  }
  store.set({ route });
  applyLayout();

  if (key && !store.get().workMedium) {
    /* 冷启动深链只有一个 key，作品库还没回来：先问清文体，否则会把剧本当小说渲染。 */
    renderPendingView();
    syncShell(store.get(), true);
    resolveMedium(key)
      .then((medium) => {
        if (store.get().currentKey !== key) return;
        patch('workMedium', medium);
        renderView(store.get().route);
        syncShell(store.get(), true);
        ensureWork(key);
      })
      .catch((err) => {
        if (store.get().currentKey !== key) return;
        renderView(store.get().route);
        toast(errorText(err, '作品加载失败'), { tone: 'danger', duration: 5000 });
      });
    return;
  }

  renderView(route);
  syncShell(store.get(), true);

  if (key) ensureWork(key);
  else stopLive();

  if (route.name === 'library' && !store.get().library.loaded) ctx.refreshLibrary();
  if (route.name === 'skills' && !store.get().library.loaded) ctx.refreshLibrary();
  if (route.name === 'evolution') {
    ctx.refreshSkills();
    if (window.innerWidth < 1200) els.view.focus?.();
  }
}

/* ------------------------------------------------------------- boot */

function installHotkeyHandlers() {
  installHotkeys({
    onPalette: () => {
      loadCommand().then((m) => m.openCommand(ctx));
    },
    onToggleSidebar: () => ctx.toggleSidebar(),
    onToggleRight: () => ctx.toggleRight(),
    onNewWork: () => ctx.openWizard(),
    onFocusComposer: () => {
      if (currentView && currentView.focusComposer) currentView.focusComposer();
    },
    onEscape: () => {
      if (closeTopOverlay()) return;
      /* 面板模块没加载过就等于没打开过，不必为 Escape 事件预加载它。 */
      if (commandMod) {
        if (commandMod.isCommandOpen()) {
          commandMod.closeCommand();
          return;
        }
      }
      closePopover();
      const ui = store.get().ui;
      if (ui.drawerOpen) ctx.closeDrawer();
      else if (ui.rightOpen) ctx.closeRight();
    }
  });
}

function installGlobalListeners() {
  window.addEventListener('shenbi:unauthorized', () => {
    toast('登录状态已失效，正在自动刷新…', { tone: 'warning', duration: 3000 });
    setTimeout(() => { location.href = '/'; }, 1500);
  });

  window.addEventListener(
    'resize',
    debounce(() => {
      applyLayout();
      sigs.topbar = '';
      syncShell(store.get(), true);
    }, 140)
  );

  els.rightBackdrop.addEventListener('click', () => ctx.closeRight());
  els.drawerBackdrop.addEventListener('click', () => ctx.closeDrawer());
}

async function loadBootstrap() {
  const [lib, models, mediums, styles] = await Promise.allSettled([
    api.library(),
    api.modelOptions(),
    api.mediums(),
    api.styles()
  ]);

  if (lib.status === 'fulfilled' && lib.value) {
    patch('library', {
      items: lib.value.items || [],
      stats: lib.value.stats || {},
      facets: lib.value.facets || {},
      loading: false,
      loaded: true,
      error: null
    });
    if (lib.value.items) patch('projects', lib.value.items);
  } else {
    const err = lib.status === 'rejected' ? lib.reason : null;
    patch('library', { loading: false, loaded: true, error: errorText(err, '作品库加载失败') });
    if (err && !err.isMissing) toast(errorText(err, '作品库加载失败'), { tone: 'danger' });
  }

  if (models.status === 'fulfilled' && models.value) patch('models', models.value);
  if (mediums.status === 'fulfilled' && mediums.value) {
    const list = Array.isArray(mediums.value) ? mediums.value : mediums.value.mediums || [];
    if (list.length) patch('mediums', list);
  }
  // v4.12.15 → v4.22.0：library 到位后不再用 setTimeout(50ms) 强制重渲染。
  // 旧 hack 有两个问题：(1) 把 currentView 置 null，renderView 不再走 destroy，
  // 旧视图的 store.subscribe/定时器全部泄漏；(2) 深链首屏渲染两次。
  // 现在 work/bench 的 library 未到分支本身就挂着 store 订阅，library patch
  // 一到就触发一次干净的 renderView（含 destroy），双渲染竞态随之消除。
  if (styles.status === 'fulfilled' && styles.value) {
    /* /api/styles 回的是 {items:[...]}，不是 {styles:[...]} */
    const list = Array.isArray(styles.value)
      ? styles.value
      : styles.value.items || styles.value.styles || [];
    patch('styles', list);
  }

  patch('booted', true);
  syncShell(store.get(), true);
}

async function boot() {
  initTheme();
  loadPrefs();
  applyLayout();
  syncShell(store.get(), true);
  bindResizers();
  installHotkeyHandlers();
  installGlobalListeners();
  startRouter(onRoute);
  loadBootstrap();
  try {
    const caps = await api.capabilities();
    if (caps && typeof caps === 'object') {
      patch('features', {
        plan: caps.plan !== false,
        state: caps.state !== false,
        references: caps.references !== false,
        skills: caps.skills !== false
      });
    }
  } catch {
    /* capabilities 缺失时按探测结果降级 */
  }
}

boot();
