/* 应用装配：外壳渲染、路由、数据加载、右栏与抽屉、拖拽调宽、快捷键与实时日志。 */

import {
  store,
  patch,
  patchUi,
  resetChat
} from './core/state.js';
import { initTheme, setTheme, toggleTheme } from './core/theme.js';
import { loadPrefs, persistLayout } from './core/prefs.js';
import { startRouter, navigate, routePath, parseHash } from './core/router.js';
import { api, errorText } from './core/api.js';
import { installHotkeys } from './core/hotkeys.js';
import { createLiveWatcher, pollBookLive } from './core/sse.js';
import { toast, closeTopOverlay, closePopover } from './core/ui.js';
import { clamp, h } from './core/dom.js';
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
import { renderSkillLib } from './views/skilllib.js';
import { renderBench } from './views/bench.js';
import { renderMediumBench, isMediumBench } from './views/mediumbench.js';

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

const ctx = {
  navigate,
  /* 刚创建的作品还没落进作品库，先告诉路由它是什么文体，省一次"作品不存在"的竞态。 */
  rememberMedium(key, medium) {
    if (key) mediumCache.set(key, medium || 'novel');
  },
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

  const medium = route.key ? store.get().workMedium : '';
  /* v4.12.15：首次加载 library 未到 → mediumOf(key) 返回 '' → 不渲染错误视图，
     改为渲染加载态，library 到位后 store 触发重渲染 */
  if (!medium && (route.name === 'work' || route.name === 'bench')) {
    if (currentView && currentView.destroy) currentView.destroy();
    const loadingEl = h('div', { class: 'view-scroll', style: { display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: '60vh' } },
      h('div', { style: { textAlign: 'center' } },
        h('div', { class: 'spinner', style: { width: '24px', height: '24px', margin: '0 auto 12px', border: '3px solid var(--line-2)', borderTopColor: 'var(--accent)', borderRadius: '50%', animation: 'spin 0.8s linear infinite' } }),
        h('p', { class: 'hint-text', text: '正在加载作品…' })));
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
  let handle = null;
  if (route.name === 'library') handle = renderLibrary(els.view, ctx);
  else if (route.name === 'settings') handle = renderSettings(els.view, ctx);
  else if (route.name === 'evolution') handle = renderEvolution(els.view, ctx);
  else if (route.name === 'skills') handle = renderSkillLib(els.view, ctx);
  /* v4.12.1：全部文体默认走对话界面；工作台通过 #/work/<key>/bench/<n> 路由或右栏按钮进入 */
  else if (isMediumBench(medium) && route.name === 'bench') handle = renderMediumBench(els.view, ctx);
  else if (route.name === 'bench') handle = renderBench(els.view, ctx);
  else if (route.name === 'work') handle = renderSession(els.view, ctx);
  else handle = renderWelcome(els.view, ctx);
  currentView = handle;

  const state = store.get();
  const item = state.library.items.find((i) => i.key === state.currentKey);
  const title =
    route.name === 'work' || route.name === 'bench'
      ? (state.work && state.work.name) || (item && item.name) || '作品'
      : { welcome: '新会话', library: '作品库', skills: '技能库', evolution: '技能进化', settings: '设置' }[route.name] || '神笔';
  document.title = `${title} · 神笔创作台`;
}

/* 冷启动深链只带 key，文体要问一次后端；这期间先占位，避免把剧本当小说渲染。 */
function renderPendingView() {
  if (currentView && currentView.destroy) {
    try {
      currentView.destroy();
    } catch (err) {
      console.error('[view] destroy failed', err);
    }
  }
  currentView = null;
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
    toast('登录状态已失效，正在自动刷新…', { tone: 'warning', duration: 3000 });
    setTimeout(() => { location.href = '/'; }, 1500);
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
  // v4.12.15：library 到位后重新渲染当前路由——首次加载时 mediumOf(key) 因
  // library 为空返回 ''，导致剧本/诗词等走不到 mediumbench（白屏）。
  // 必须延迟到下一个 tick 才能等 applyLayout 完成后再触发。
  setTimeout(() => {
    const cr = parseHash();
    if (cr.name === 'work' || cr.name === 'bench') {
      // 强制重渲染（清除 currentView 缓存）
      currentView = null;
      onRoute(cr);
    }
  }, 50);
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
