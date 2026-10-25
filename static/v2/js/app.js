/* 应用装配：外壳渲染、路由、数据加载、右栏与抽屉、拖拽调宽、快捷键与实时日志。 */

import {
  store,
  patch,
  patchUi,
  resetChat
} from './core/state.js';
import { initTheme, setTheme, toggleTheme } from './core/theme.js';
import { startRouter, navigate, routePath } from './core/router.js';
import { api, errorText } from './core/api.js';
import { installHotkeys } from './core/hotkeys.js';
import { createLiveWatcher, pollBookLive } from './core/sse.js';
import { toast, closeTopOverlay, closePopover } from './core/ui.js';
import { clamp } from './core/dom.js';
import { renderSidebar } from './components/sidebar.js';
import { renderTopbar } from './components/topbar.js';
import { renderRightPanel } from './components/rightpanel.js';
import { openCommand, closeCommand, isCommandOpen } from './components/command.js';
import { openWizard as openWizardModal } from './views/wizard.js';
import { renderWelcome } from './views/welcome.js';
import { renderSession } from './views/session.js';
import { renderLibrary } from './views/library.js';
import { renderSettings } from './views/settings.js';
import { renderEvolution } from './views/evolution.js';
import { renderBench } from './views/bench.js';

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
let liveStop = null;
let pollStop = null;
let loadedKey = '';
const inflight = new Set();

/* ---------------------------------------------------------------- ctx */

const ctx = {
  navigate,
  openWizard(preset = {}) {
    if (wizardModal) return wizardModal;
    wizardModal = openWizardModal(ctx, preset);
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

  async refreshLibrary() {
    patch('library', { loading: true });
    try {
      const res = await api.library();
      patch('library', {
        items: (res && res.items) || [],
        stats: (res && res.stats) || {},
        facets: (res && res.facets) || {},
        loading: false,
        loaded: true,
        error: null
      });
      if (res && res.items) patch('projects', res.items);
    } catch (err) {
      patch('library', { loading: false, error: errorText(err, '作品库加载失败') });
    }
    syncShell(store.get());
  },

  async refreshWork() {
    const key = store.get().currentKey;
    if (!key || inflight.has('work')) return;
    inflight.add('work');
    try {
      const status = await api.status(key);
      const item = store.get().library.items.find((i) => i.key === key) || null;
      const merged = {
        ...(item || {}),
        ...(status || {}),
        total_words: (item && item.total_words) || (status && status.words) || 0,
        status_label: (item && item.status_label) || '',
        quality: item ? item.quality : null,
        favorite: item ? item.favorite : false,
        tags: (item && item.tags) || []
      };
      patch('work', merged);
      patch('workError', null);
    } catch (err) {
      patch('workError', errorText(err, '作品状态加载失败'));
    } finally {
      inflight.delete('work');
    }
    syncShell(store.get());
  },

  async refreshPlan() {
    const key = store.get().currentKey;
    if (!key || inflight.has('plan')) return;
    inflight.add('plan');
    try {
      const res = await api.plan(key);
      patch('plan', res || null);
      patch('planError', null);
      patch('features', { plan: true });
    } catch (err) {
      if (err && err.isMissing) {
        patch('plan', null);
        patch('features', { plan: false });
      } else {
        patch('planError', errorText(err, '规划加载失败'));
      }
    } finally {
      inflight.delete('plan');
    }
    syncShell(store.get());
  },

  async refreshState() {
    const key = store.get().currentKey;
    if (!key || inflight.has('state')) return;
    inflight.add('state');
    try {
      const res = await api.state(key);
      patch('lore', res || null);
      patch('loreError', null);
      patch('features', { state: true });
    } catch (err) {
      if (err && err.isMissing) {
        patch('lore', null);
        patch('features', { state: false });
      } else {
        patch('loreError', errorText(err, '状态库加载失败'));
      }
    } finally {
      inflight.delete('state');
    }
    syncShell(store.get());
  },

  async refreshRefs() {
    const key = store.get().currentKey;
    if (!key || inflight.has('refs')) return;
    inflight.add('refs');
    try {
      const res = await api.references(key);
      patch('refs', Array.isArray(res) ? res : (res && res.items) || []);
      patch('refsError', null);
      patch('features', { references: true });
    } catch (err) {
      if (err && err.isMissing) {
        patch('refs', []);
        patch('features', { references: false });
      } else {
        patch('refsError', errorText(err, '参考资料加载失败'));
      }
    } finally {
      inflight.delete('refs');
    }
    syncShell(store.get());
  },

  async refreshSkills() {
    if (inflight.has('skills')) return;
    inflight.add('skills');
    try {
      const res = await api.skillsEvolution();
      patch('skills', res || null);
      patch('skillsError', null);
      patch('features', { skills: true });
    } catch (err) {
      if (err && err.isMissing) {
        patch('skills', null);
        patch('features', { skills: false });
      } else {
        patch('skillsError', errorText(err, '技能数据加载失败'));
      }
    } finally {
      inflight.delete('skills');
    }
    syncShell(store.get());
  }
};

/* ------------------------------------------------------------ layout */

function applyLayout() {
  const app = els.app;
  const ui = store.get().ui;
  const wide = window.innerWidth >= 1200;
  const narrow = window.innerWidth < 760;

  app.dataset.sidebar = ui.sidebarExpanded ? 'expanded' : 'rail';
  app.dataset.drawer = narrow && ui.drawerOpen ? 'open' : 'closed';
  els.drawerBackdrop.hidden = !(narrow && ui.drawerOpen);

  let mode = 'closed';
  if (ui.rightOpen) mode = wide ? 'docked' : 'overlay';
  app.dataset.right = mode;

  const rightW = clamp(ui.rightWidth || Math.round(window.innerWidth * 0.45), 320, Math.min(760, window.innerWidth - 420));
  app.style.setProperty('--right-col', mode === 'docked' ? `${rightW}px` : '0px');
  app.style.setProperty('--sidebar-w', `${clamp(ui.sidebarWidth, 200, 420)}px`);

  els.rightBackdrop.hidden = mode !== 'overlay';
}

function bindResizers() {
  const app = els.app;

  function drag(handle, { getStart, apply, commit }) {
    let start = null;
    handle.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      start = { x: event.clientX, ...getStart() };
      try {
        handle.setPointerCapture(event.pointerId);
      } catch {
        /* ignore */
      }
      app.dataset.dragging = 'true';
      event.preventDefault();
    });
    handle.addEventListener('pointermove', (event) => {
      if (!start) return;
      apply(event.clientX - start.x, start);
    });
    const end = (event) => {
      if (!start) return;
      start = null;
      app.dataset.dragging = 'false';
      commit();
    };
    handle.addEventListener('pointerup', end);
    handle.addEventListener('pointercancel', end);
    handle.addEventListener('keydown', (event) => {
      const step = event.shiftKey ? 48 : 16;
      if (event.key === 'ArrowLeft') {
        event.preventDefault();
        apply(-step, getStart());
        commit();
      } else if (event.key === 'ArrowRight') {
        event.preventDefault();
        apply(step, getStart());
        commit();
      }
    });
  }

  drag(els.handleLeft, {
    getStart: () => ({ w: store.get().ui.sidebarWidth }),
    apply(dx, start) {
      const w = clamp(start.w + dx, 200, 420);
      app.style.setProperty('--sidebar-w', `${w}px`);
      start.w = w;
    },
    commit() {
      const w = parseInt(app.style.getPropertyValue('--sidebar-w'), 10) || store.get().ui.sidebarWidth;
      patchUi({ sidebarWidth: w, sidebarExpanded: true });
      applyLayout();
      syncShell(store.get(), true);
    }
  });

  drag(els.handleRight, {
    getStart: () => ({ w: store.get().ui.rightWidth || Math.round(window.innerWidth * 0.45) }),
    apply(dx, start) {
      const w = clamp(start.w - dx, 320, Math.min(760, window.innerWidth - 420));
      app.style.setProperty('--right-col', `${w}px`);
      start.w = w;
    },
    commit() {
      const w = parseInt(app.style.getPropertyValue('--right-col'), 10) || 0;
      patchUi({ rightWidth: w });
      applyLayout();
      syncShell(store.get(), true);
    }
  });
}

/* ------------------------------------------------------------- shell */

const sigs = { sidebar: '', topbar: '', right: '' };

function syncShell(state, force = false) {
  if (!els.app) return;
  const itemsSig = state.library.items
    .map((i) => [i.key, i.name, i.state, i.status_label, i.done, i.total, i.total_words, i.favorite, i.archived, i.genre, (i.tags || []).join('|'), i.updated_at].join('~'))
    .join(',');
  const sidebarSig = [
    itemsSig,
    state.library.loading,
    state.library.loaded,
    state.route.key,
    state.route.name,
    state.ui.sidebarExpanded
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

function renderView(route) {
  if (currentView && currentView.destroy) {
    try {
      currentView.destroy();
    } catch (err) {
      console.error('[view] destroy failed', err);
    }
  }
  currentView = null;
  els.view.textContent = '';

  let handle = null;
  if (route.name === 'work') handle = renderSession(els.view, ctx);
  else if (route.name === 'bench') handle = renderBench(els.view, ctx);
  else if (route.name === 'library') handle = renderLibrary(els.view, ctx);
  else if (route.name === 'settings') handle = renderSettings(els.view, ctx);
  else if (route.name === 'evolution') handle = renderEvolution(els.view, ctx);
  else handle = renderWelcome(els.view, ctx);
  currentView = handle;

  const state = store.get();
  const item = state.library.items.find((i) => i.key === state.currentKey);
  const title =
    route.name === 'work' || route.name === 'bench'
      ? (state.work && state.work.name) || (item && item.name) || '作品'
      : { welcome: '新会话', library: '作品库', evolution: '技能进化', settings: '设置' }[route.name] || '神笔';
  document.title = `${title} · 神笔创作台`;
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
    }
  });
  pollStop = pollBookLive(
    key,
    (data) => {
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
    },
    () => {},
    2500
  );
}

async function ensureWork(key) {
  if (!key || loadedKey === key) return;
  loadedKey = key;
  patch('work', null);
  patch('workError', null);
  await Promise.allSettled([
    ctx.refreshWork(),
    ctx.refreshPlan(),
    ctx.refreshState(),
    ctx.refreshRefs(),
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
  renderView(route);
  syncShell(store.get(), true);

  if (key) ensureWork(key);
  else stopLive();

  if (route.name === 'library' && !store.get().library.loaded) ctx.refreshLibrary();
  if (route.name === 'evolution') {
    ctx.refreshSkills();
    if (window.innerWidth < 1200) els.view.focus?.();
  }
}

/* ------------------------------------------------------------- boot */

function installHotkeyHandlers() {
  installHotkeys({
    onPalette: () => openCommand(ctx),
    onToggleSidebar: () => ctx.toggleSidebar(),
    onToggleRight: () => ctx.toggleRight(),
    onNewWork: () => ctx.openWizard(),
    onFocusComposer: () => {
      if (currentView && currentView.focusComposer) currentView.focusComposer();
    },
    onEscape: () => {
      if (closeTopOverlay()) return;
      if (isCommandOpen()) {
        closeCommand();
        return;
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
    toast('登录状态已失效，请从 http://127.0.0.1 重新打开创作台', { tone: 'danger', duration: 6000 });
  });

  window.addEventListener(
    'resize',
    debounce_(() => {
      applyLayout();
      sigs.topbar = '';
      syncShell(store.get(), true);
    }, 140)
  );

  els.rightBackdrop.addEventListener('click', () => ctx.closeRight());
  els.drawerBackdrop.addEventListener('click', () => ctx.closeDrawer());
}

function debounce_(fn, wait = 140) {
  let timer = 0;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
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
  if (styles.status === 'fulfilled' && styles.value) {
    const list = Array.isArray(styles.value) ? styles.value : styles.value.styles || [];
    patch('styles', list);
  }

  patch('booted', true);
  syncShell(store.get(), true);
}

async function boot() {
  initTheme();
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
