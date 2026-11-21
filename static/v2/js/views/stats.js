/* 码字统计（v5.4.0）：今日概览双卡、SVG 自绘双折线趋势、码字日历、
   连续天数、目标进度环与「设置计划」弹窗。颜色读 tokens 语义变量，随主题重绘，不引图表库。 */

import { h, icon, fmtNum } from '../core/dom.js';
import { store } from '../core/state.js';
import { request, errorText } from '../core/api.js';
import { toast, openModal } from '../core/ui.js';

/* ---------------------------------------------------------------- 纯函数（导出供 node 测试） */

/**
 * 给一组数值计算 SVG 折线点位（纯函数：不碰 DOM、不依赖外部状态）。
 * 返回 [{x, y, v}]；x 随下标递增，y 落在 [pad, height-pad] 区间（值越大 y 越小，即画在上方）。
 */
export function linePoints(values, { width = 640, height = 180, pad = 10, min = 0, max = 0 } = {}) {
  const vals = (Array.isArray(values) ? values : []).map((v) => Math.max(0, Number(v) || 0));
  if (!vals.length) return [];
  const w = Math.max(1, Number(width) || 1);
  const hh = Math.max(1, Number(height) || 1);
  const p = Math.max(0, Number(pad) || 0);
  const lo = Math.max(0, Number(min) || 0);
  const hi = Math.max(lo + 1, ...vals, Number(max) || 0);
  const n = vals.length;
  const innerW = Math.max(1, w - p * 2);
  const innerH = Math.max(1, hh - p * 2);
  return vals.map((v, i) => ({
    x: p + (n === 1 ? innerW / 2 : (i * innerW) / (n - 1)),
    y: p + innerH - ((v - lo) / (hi - lo)) * innerH,
    v
  }));
}

/**
 * 某月的日历格子（纯函数）：周一开头，首尾用 null 补齐整周。
 * 返回 [{date:'YYYY-MM-DD', day:n} | null, ...]。
 */
export function monthGrid(year, month) {
  const y = Number(year) || 1970;
  const m = Number(month) || 1;
  const dayCount = new Date(y, m, 0).getDate();
  // JS getDay() 周日=0；转成周一=0 的列号
  const firstDow = (new Date(y, m - 1, 1).getDay() + 6) % 7;
  const cells = [];
  for (let i = 0; i < firstDow; i += 1) cells.push(null);
  const p2 = (n) => String(n).padStart(2, '0');
  for (let d = 1; d <= dayCount; d += 1) {
    cells.push({ date: `${y}-${p2(m)}-${p2(d)}`, day: d });
  }
  while (cells.length % 7 !== 0) cells.push(null);
  return cells;
}

/** 主题语义变量 → 折线/环颜色（进入页面与切主题时都重读，保证四主题可看）。 */
function cssVar(name, fallback) {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  } catch {
    return fallback;
  }
}

/* SVG 元素必须走 createElementNS（h() 的 createElement 出来是 HTMLUnknownElement，不渲染矢量）。 */
const SVG_NS = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    el.setAttribute(k, String(v));
  }
  return el;
}

/* ---------------------------------------------------------------- 视图 */

export function renderStats(container, ctx) {
  const view = h('div', { class: 'view-scroll' });
  const pad = h('div', { class: 'view-pad' });
  view.append(pad);
  container.append(view);

  const model = {
    data: null,          // GET /api/writestats?days=30 的完整返回
    monthCache: {},      // 'YYYY-MM' -> 月视图（翻历史月时按需拉取）
    viewMonth: null,     // 日历当前展示月 'YYYY-MM'
    book: '',            // 书筛选（'' = 全部作品）
    range: 7,            // 趋势窗口 7/15/30
    error: '',
    libraryItems: []
  };

  /* 头部：标题 + 书筛选 + 设置计划 */
  const bookSel = h(
    'select',
    { class: 'input stats-book-sel', 'aria-label': '按作品筛选' },
    h('option', { value: '', text: '全部作品' })
  );
  bookSel.addEventListener('change', () => {
    model.book = bookSel.value || '';
    paint();
  });

  pad.append(
    h(
      'div',
      { class: 'evo-head' },
      h(
        'div',
        { class: 'evo-head-copy' },
        h('h1', { text: '码字统计' }),
        h('p', { class: 'hint-text', text: '手写与 AI 分账记账：手写按编辑会话净增累计，AI 插字抬基线单独入账，落盘求差不双记。' })
      ),
      bookSel,
      h(
        'button',
        { class: 'btn btn-primary', type: 'button', html: `${icon('target', 14)}<span>设置计划</span>`, on: { click: openGoalModal } }
      )
    )
  );

  const body = h('div', { class: 'stats-view' });
  pad.append(body);

  /* 主题切换时重绘（折线/环颜色读 CSS 变量） */
  let themeObs = null;
  try {
    themeObs = new MutationObserver(() => paint());
    themeObs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  } catch {
    /* ignore */
  }

  async function load() {
    model.error = '';
    paint();
    try {
      const d = await request(`/api/writestats?days=30&month=${encodeURIComponent(currentMonthKey())}`);
      model.data = d && d.ok ? d : null;
      if (d && d.month && d.month.key) model.monthCache[d.month.key] = d.month;
      if (!model.viewMonth && d && d.month) model.viewMonth = d.month.key;
      model.error = model.data ? '' : '统计返回为空';
    } catch (err) {
      model.data = null;
      model.error = errorText(err, '统计加载失败');
    }
    paint();
  }

  /* 刷新书筛选下拉（作品库到位后调用一次） */
  function refreshBookOptions() {
    const items = (store.get().library.items || []).filter((i) => !i.archived);
    model.libraryItems = items;
    const cur = model.book;
    bookSel.textContent = '';
    bookSel.append(h('option', { value: '', text: '全部作品' }));
    for (const it of items) bookSel.append(h('option', { value: it.key, text: it.name || it.key }));
    if (cur && items.some((i) => i.key === cur)) bookSel.value = cur;
    else {
      model.book = '';
      bookSel.value = '';
    }
  }

  const unsubLibrary = store.subscribe((s) => {
    if (!view.isConnected) return;
    if (s.library.loaded) refreshBookOptions();
  });

  /* --------------------------------------------- 取数辅助（含书筛选口径） */

  function bookOf(row, key) {
    return (row && row.books && row.books[key]) || null;
  }

  function rowVals(row, kind) {
    /* kind: 'manual' | 'ai'；选了书就只看这本书，否则取全日合计。 */
    if (!row) return 0;
    if (model.book) return Number((bookOf(row, model.book) || {})[kind]) || 0;
    return Math.max(0, Number(row[kind]) || 0);
  }

  function todayRow() {
    const d = model.data;
    if (!d) return null;
    if (model.book) {
      const b = bookOf(d.today, model.book);
      return {
        date: d.today.date,
        manual: Number((b || {}).manual) || 0,
        ai: Number((b || {}).ai) || 0,
        session: Number((b || {}).session) || 0
      };
    }
    return d.today;
  }

  function goalOf(kind) {
    const g = (model.data && model.data.goals) || {};
    return Math.max(0, Number(g[kind]) || 0);
  }

  function currentMonthKey() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
  }

  /* --------------------------------------------- 渲染 */

  function paint() {
    body.textContent = '';
    if (model.error) {
      body.append(
        h(
          'div',
          { class: 'lib-empty empty-state', role: 'alert' },
          h('div', { class: 'empty-icon', html: icon('alert', 18) }),
          h('h3', { text: '统计数据没有加载出来' }),
          h('p', { text: model.error }),
          h('button', { class: 'btn btn-sm', type: 'button', text: '重试', on: { click: load } })
        )
      );
      return;
    }
    if (!model.data) {
      for (let i = 0; i < 3; i += 1) {
        body.append(
          h(
            'div',
            { class: 'stat-card' },
            h('div', { class: 'skeleton skeleton-line', style: { width: `${36 + i * 18}%` } }),
            h('div', { class: 'skeleton skeleton-line w-40' })
          )
        );
      }
      return;
    }
    paintToday();
    paintTrend();
    paintGoals();
    paintCalendar();
  }

  /* 今日概览：手写/AI 双卡 + 连续天数卡 */
  function paintToday() {
    const t = todayRow() || { manual: 0, ai: 0, session: 0 };
    const all = t.manual + t.ai;
    const pct = (part) => (all > 0 ? Math.round((part / all) * 100) : 0);
    const grid = h('div', { class: 'stats-top' });

    grid.append(
      h(
        'div',
        { class: 'stat-card stat-today stat-today-manual' },
        h('span', { class: 'stat-k' }, h('span', { html: icon('pen', 13) }), h('span', { text: '今日手写' })),
        h('span', { class: 'stat-v' }, fmtNum(t.manual), h('small', { text: ' 字' })),
        h('span', { class: 'stat-sub' }, `占今日总码字 ${pct(t.manual)}%`),
        t.session > 0 ? h('span', { class: 'stat-hint', text: `进行中会话未定稿 ${fmtNum(t.session)} 字` }) : null
      ),
      h(
        'div',
        { class: 'stat-card stat-today stat-today-ai' },
        h('span', { class: 'stat-k' }, h('span', { html: icon('wand', 13) }), h('span', { text: '今日 AI' })),
        h('span', { class: 'stat-v' }, fmtNum(t.ai), h('small', { text: ' 字' })),
        h('span', { class: 'stat-sub' }, `占今日总码字 ${pct(t.ai)}%`),
        h('span', { class: 'stat-hint', text: '幽灵字 / 划词 / 续写采纳入账' })
      ),
      h(
        'div',
        { class: 'stat-card stat-today stat-today-streak' },
        h('span', { class: 'stat-k' }, h('span', { html: icon('zap', 13) }), h('span', { text: '连续码字' })),
        h(
          'span',
          { class: 'stats-streak-pill' },
          `${((model.data.streak || {}).days) || 0}`,
          h('small', { text: ' 天' })
        ),
        h(
          'span',
          { class: 'stat-sub' },
          `本月累计 ${fmtNum(((model.data.month || {}).manual) || 0)} + ${fmtNum(((model.data.month || {}).ai) || 0)} 字`
        ),
        h('span', { class: 'stat-hint', text: ((model.data.streak || {}).wrote_today) ? '今天已经动笔，继续保持' : '今天还没写——从昨天连数的火种还热着' })
      )
    );
    body.append(grid);
  }

  /* 趋势：SVG 自绘双折线（手写 / AI），窗口 7/15/30 天 */
  function paintTrend() {
    const card = h('div', { class: 'stat-card stats-trend' });
    const head = h(
      'div',
      { class: 'stats-card-head' },
      h('h2', {}, h('span', { html: icon('activity', 14) }), h('span', { text: '码字趋势' })),
      h('span', { class: 'stats-legend' },
        h('span', { class: 'stats-legend-item' }, h('i', { class: 'stats-swatch stats-swatch-manual' }), h('span', { text: '手写' })),
        h('span', { class: 'stats-legend-item' }, h('i', { class: 'stats-swatch stats-swatch-ai' }), h('span', { text: 'AI' }))
      ),
      h(
        'div',
        { class: 'stats-range' },
        ...[7, 15, 30].map((n) =>
          h('button', {
            class: `stats-range-btn${model.range === n ? ' on' : ''}`,
            type: 'button',
            text: `${n} 天`,
            on: { click: () => { model.range = n; paint(); } }
          })
        )
      )
    );
    card.append(head);

    const rows = (model.data.days || []).slice(-model.range);
    const W = 720;
    const H = 210;
    const PADX = 14;
    const PADY = 18;
    const manualPts = linePoints(rows.map((r) => rowVals(r, 'manual')), { width: W, height: H, pad: PADY });
    const aiPts = linePoints(rows.map((r) => rowVals(r, 'ai')), { width: W, height: H, pad: PADY });
    const hi = Math.max(1, ...rows.map((r) => Math.max(rowVals(r, 'manual'), rowVals(r, 'ai'))));

    const svg = svgEl('svg', {
      viewBox: `0 0 ${W} ${H}`,
      preserveAspectRatio: 'xMidYMid meet',
      class: 'stats-trend-svg',
      role: 'img',
      'aria-label': `最近 ${model.range} 天手写与 AI 码字趋势`
    });
    // 横向参考线（3 档）+ y 轴刻度文字
    for (let i = 0; i <= 2; i += 1) {
      const v = (hi / 2) * i;
      const y = PADY + (H - PADY * 2) - ((H - PADY * 2) * i) / 2;
      svg.append(svgEl('line', { x1: PADX, y1: y.toFixed(1), x2: W - PADX, y2: y.toFixed(1), class: 'stats-grid-line' }));
      const txt = svgEl('text', { x: PADX + 2, y: (y - 4).toFixed(1), class: 'stats-axis-txt' });
      txt.textContent = fmtNum(Math.round(v));
      svg.append(txt);
    }
    // 双折线（颜色挂 CSS 变量，主题切换时 MutationObserver 重绘）
    const poly = (pts, cls) => svgEl('polyline', { points: pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' '), class: cls });
    svg.append(poly(aiPts, 'stats-line stats-line-ai'));
    svg.append(poly(manualPts, 'stats-line stats-line-manual'));
    for (const [pts, cls] of [[manualPts, 'stats-dot stats-dot-manual'], [aiPts, 'stats-dot stats-dot-ai']]) {
      for (const p of pts) svg.append(svgEl('circle', { cx: p.x.toFixed(1), cy: p.y.toFixed(1), r: 2.4, class: cls }));
    }
    // x 轴首中尾日期
    const labelAt = (idx) => {
      const r = rows[idx];
      if (!r) return '';
      const parts = String(r.date).split('-');
      return `${Number(parts[1])}/${Number(parts[2])}`;
    };
    const xlab = h('div', { class: 'stats-xlab' },
      h('span', { text: labelAt(0) }),
      h('span', { text: labelAt(Math.floor((rows.length - 1) / 2)) }),
      h('span', { text: labelAt(rows.length - 1) })
    );
    card.append(svg, xlab);
    body.append(card);
  }

  /* 目标进度环（conic-gradient）+ 月目标进度 */
  function paintGoals() {
    const card = h('div', { class: 'stat-card stats-goals' });
    card.append(h('div', { class: 'stats-card-head' }, h('h2', {}, h('span', { html: icon('target', 14) }), h('span', { text: '码字计划' }))));
    const t = todayRow() || { manual: 0, ai: 0 };
    const rings = h('div', { class: 'stats-rings' });
    for (const [kind, label, val, cls] of [
      ['manual', '手写', t.manual, 'ring-manual'],
      ['ai', 'AI', t.ai, 'ring-ai']
    ]) {
      const goal = goalOf(kind);
      const pct = goal > 0 ? Math.min(100, Math.round((val / goal) * 100)) : 0;
      const color = cssVar(cls === 'ring-manual' ? '--accent' : '--success', '#4176e6');
      const track = cssVar('--bg-chip', '#f1f3f5');
      const ring = h(
        'div',
        { class: `stats-ring ${cls}`, title: goal > 0 ? `${label} ${fmtNum(val)} / ${fmtNum(goal)} 字` : `${label}未设目标` },
        h('div', {
          class: 'stats-ring-face',
          style: goal > 0
            ? { background: `conic-gradient(${color} ${(pct / 100) * 360}deg, ${track} 0)` }
            : { background: track }
        }),
        h('div', { class: 'stats-ring-hole' },
          h('span', { class: 'stats-ring-pct', text: goal > 0 ? `${pct}%` : '未设' }),
          h('span', { class: 'stats-ring-label', text: label })
        )
      );
      rings.append(ring);
    }
    card.append(rings);

    const goals = (model.data.goals) || {};
    const month = (model.data.month) || { manual: 0, ai: 0 };
    card.append(
      h(
        'div',
        { class: 'stats-goal-lines' },
        goalRow('手写月目标', month.manual, goals.monthly_manual),
        goalRow('AI 月目标', month.ai, goals.monthly_ai)
      ),
      h('div', { class: 'hint-text stats-goal-note', text: '月目标 = 日目标 × 当月天数；点右上「设置计划」调整。' })
    );
    body.append(card);
  }

  function goalRow(label, val, target) {
    const pct = target > 0 ? Math.min(100, Math.round((val / target) * 100)) : 0;
    return h(
      'div',
      { class: 'stats-goal-row' },
      h('span', { class: 'stats-goal-name', text: label }),
      h('div', { class: 'stats-goal-track' }, h('div', { class: 'stats-goal-fill', style: { width: `${pct}%` } })),
      h('span', { class: 'stats-goal-val', text: `${fmtNum(val)} / ${fmtNum(target || 0)}` })
    );
  }

  /* 码字日历：月格子双行双口径 + 月切换 */
  function paintCalendar() {
    const card = h('div', { class: 'stat-card stats-cal' });
    const mkey = model.viewMonth || currentMonthKey();
    const month = model.monthCache[mkey] || null;
    const [y, m] = mkey.split('-').map(Number);
    const isThisMonth = mkey === currentMonthKey();
    const header = h(
      'div',
      { class: 'stats-card-head' },
      h('h2', {}, h('span', { html: icon('grid', 14) }), h('span', { text: '码字日历' })),
      h(
        'div',
        { class: 'stats-cal-nav' },
        h('button', { class: 'icon-btn icon-btn-sm stats-cal-btn', type: 'button', 'aria-label': '上个月', text: '‹', on: { click: () => shiftMonth(-1) } }),
        h('span', { class: 'stats-cal-title', text: `${y} 年 ${m} 月` }),
        h('button', {
          class: 'icon-btn icon-btn-sm stats-cal-btn', type: 'button', 'aria-label': '下个月',
          text: '›', disabled: isThisMonth, on: { click: () => shiftMonth(1) }
        })
      )
    );
    card.append(header);

    if (!month) {
      card.append(h('div', { class: 'hint-text', text: '月视图加载中…' }));
      body.append(card);
      loadMonth(mkey);
      return;
    }

    const sum = {
      manual: model.book ? (month.rows || []).reduce((a, r) => a + rowVals(r, 'manual'), 0) : month.manual,
      ai: model.book ? (month.rows || []).reduce((a, r) => a + rowVals(r, 'ai'), 0) : month.ai
    };
    card.append(h('div', { class: 'stats-cal-sum' },
      h('span', {}, h('i', { class: 'stats-swatch stats-swatch-manual' }), h('span', { text: `本月手写 ${fmtNum(sum.manual)} 字` })),
      h('span', {}, h('i', { class: 'stats-swatch stats-swatch-ai' }), h('span', { text: `本月 AI ${fmtNum(sum.ai)} 字` }))
    ));

    const todayStr = (() => {
      const d = new Date();
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    })();

    const cal = h('div', { class: 'stats-cal-grid' });
    for (const wd of ['一', '二', '三', '四', '五', '六', '日']) cal.append(h('span', { class: 'stats-cal-wd', text: wd }));
    const rowsByDate = {};
    for (const r of month.rows || []) rowsByDate[r.date] = r;
    for (const cell of monthGrid(y, m)) {
      if (!cell) {
        cal.append(h('span', { class: 'stats-cal-cell stats-cal-empty' }));
        continue;
      }
      const r = rowsByDate[cell.date];
      const manual = rowVals(r, 'manual');
      const ai = rowVals(r, 'ai');
      const wrote = manual + ai > 0;
      cal.append(
        h(
          'div',
          {
            class: `stats-cal-cell${cell.date === todayStr ? ' today' : ''}${wrote ? ' wrote' : ''}`,
            title: `${cell.date} · 手写 ${fmtNum(manual)} 字 / AI ${fmtNum(ai)} 字`
          },
          h('span', { class: 'stats-cal-day', text: String(cell.day) }),
          h('span', { class: 'stats-cal-line stats-cal-manual', text: manual > 0 ? fmtNum(manual) : '—' }),
          h('span', { class: 'stats-cal-line stats-cal-ai', text: ai > 0 ? `+${fmtNum(ai)}` : '—' })
        )
      );
    }
    card.append(cal);
    body.append(card);
  }

  function shiftMonth(delta) {
    const [y, m] = (model.viewMonth || currentMonthKey()).split('-').map(Number);
    const d = new Date(y, m - 1 + delta, 1);
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
    // 不允许翻到未来
    if (key > currentMonthKey()) return;
    model.viewMonth = key;
    paint();
    if (!model.monthCache[key]) loadMonth(key);
  }

  async function loadMonth(mkey) {
    try {
      const d = await request(`/api/writestats?days=1&month=${encodeURIComponent(mkey)}`);
      if (d && d.month && d.month.key) {
        model.monthCache[d.month.key] = d.month;
        if (view.isConnected && model.viewMonth === d.month.key) paint();
      }
    } catch {
      /* 月视图拉取失败：格子里显示已有数据即可 */
    }
  }

  /* 「设置计划」弹窗：手写/AI 双目标 */
  function openGoalModal() {
    const goals = (model.data && model.data.goals) || {};
    const manualIn = h('input', { class: 'input', type: 'number', min: '0', step: '100', value: String(Number(goals.manual) || 0), 'aria-label': '手写日目标' });
    const aiIn = h('input', { class: 'input', type: 'number', min: '0', step: '100', value: String(Number(goals.ai) || 0), 'aria-label': 'AI 日目标' });
    const body = h(
      'div',
      { class: 'stats-goal-form' },
      h('label', { class: 'stats-goal-field' }, h('span', { class: 'stats-goal-name', text: '手写日目标（字）' }), manualIn),
      h('label', { class: 'stats-goal-field' }, h('span', { class: 'stats-goal-name', text: 'AI 日目标（字）' }), aiIn),
      h('p', { class: 'hint-text', text: '填 0 表示不设目标；月目标 = 日目标 × 当月天数，自动随月份换算。' })
    );
    const saveBtn = h('button', { class: 'btn btn-primary', type: 'button', text: '保存计划' });
    const modal = openModal({
      title: '设置码字计划',
      subtitle: '手写与 AI 分别设日目标，统计页展示进度环与月进度',
      body,
      footer: saveBtn,
      size: 'sm'
    });
    saveBtn.addEventListener('click', async () => {
      saveBtn.disabled = true;
      saveBtn.textContent = '保存中…';
      try {
        await request('/api/writestats/goals', {
          method: 'POST',
          body: {
            manual: Math.max(0, Number(manualIn.value) || 0),
            ai: Math.max(0, Number(aiIn.value) || 0)
          }
        });
        toast('码字计划已保存', { tone: 'success' });
        modal.close();
        await load();
      } catch (err) {
        toast(errorText(err, '保存失败'), { tone: 'danger' });
        saveBtn.disabled = false;
        saveBtn.textContent = '保存计划';
      }
    });
  }

  refreshBookOptions();
  load();

  return {
    el: view,
    refresh: load,
    destroy() {
      unsubLibrary();
      if (themeObs) {
        try {
          themeObs.disconnect();
        } catch {
          /* ignore */
        }
      }
      view.remove();
    }
  };
}
