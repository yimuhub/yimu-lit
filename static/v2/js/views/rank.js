/* 榜单风向（v5.4.x，docs/系统化升级方案-v5.1 第 8.2 节）：轻量「手动采风 + AI 解读」页。
 * 采集：粘贴榜单 URL（服务端抓一次，UA+10s 超时+2MB 上限，SSR 文本解析）或直接粘贴榜单文本；
 * 快照：data/rank-snapshots/<源>/<YYYY-MM-DD>.json（每源每日一份、120 天保留、重抓覆盖当日）；
 * 升降幅：相对上一份快照自动计算（↑n 红 / ↓n 绿 / 新入榜）；
 * AI 解读：只发送榜单数据（名次/书名/作者/公开数据列），不含任何创作内容；解读随快照缓存。
 * 页面是附加能力：接口失败给中文可操作提示，不影响写作主链路。 */

import { h, icon, fmtDate } from '../core/dom.js';
import { request, errorText } from '../core/api.js';
import { toast } from '../core/ui.js';

/* ---------------------------------------------------------------- 纯函数（导出供测试） */

/**
 * 升降幅展示元数据（纯函数）：{delta, tag} → {label, cls}。
 * tag: up=上升 / down=下降 / new=新入榜 / same=持平。
 */
export function changeLabel(change) {
  const ch = change || {};
  if (ch.tag === 'new') return { label: '新', cls: 'rk-new' };
  if (ch.tag === 'up') return { label: `↑${ch.delta}`, cls: 'rk-up' };
  if (ch.tag === 'down') return { label: `↓${Math.abs(ch.delta)}`, cls: 'rk-down' };
  return { label: '—', cls: 'rk-same' };
}

/* ---------------------------------------------------------------- 视图 */

export function renderRank(container) {
  const view = h('div', { class: 'rank-view' });
  container.append(view);

  const model = {
    sources: [],      // [{source, dates, latest}]
    source: '',       // 当前源
    date: '',         // 当前快照日期
    against: '',      // 历史对比基准日期（空=自动取上一份）
    snapshot: null,   // 当前快照
    changes: [],      // 带 change 的条目
    comparedWith: '', // 实际对比基准（服务端回传）
    loading: false,
    reviewing: false
  };

  /* ------------------------------------------------------------- 头部 */

  view.append(
    h('div', { class: 'rank-head' },
      h('h1', { class: 'rank-title', text: '榜单风向' }),
      h('p', {
        class: 'rank-desc',
        text: '手动采风 + AI 解读：粘贴榜单页面链接或榜单文本，按日留快照并自动计算名次升降。' +
          '不做自动抓取与反爬对抗——站点拦截时请复制榜单内容改用「粘贴文本」。'
      })
    )
  );

  /* ------------------------------------------------------------- 采集卡 */

  const sourceInput = h('input', {
    class: 'input rank-source-input',
    type: 'text',
    placeholder: '榜单源名称（留空则用域名，如 qidian.com）',
    maxlength: '40',
    'aria-label': '榜单源名称'
  });

  const urlInput = h('input', {
    class: 'input',
    type: 'url',
    placeholder: '粘贴榜单页面链接（http/https）',
    'aria-label': '榜单页面链接'
  });

  const textInput = h('textarea', {
    class: 'textarea rank-text-input',
    rows: '7',
    placeholder: '或直接粘贴榜单文本，每行一本：名次 书名 作者 数据\n支持 1. / 1、 / TOP3 / 第4名 / [5] 等名次写法，| 与多空格分隔均可',
    'aria-label': '榜单文本'
  });

  let collectMode = 'url';
  const urlPane = h('div', { class: 'rank-pane' }, urlInput);
  const textPane = h('div', { class: 'rank-pane', style: { display: 'none' } }, textInput);

  const tabUrl = h('button', {
    class: 'rank-tab is-on', type: 'button', text: '粘贴链接',
    on: { click: () => setMode('url') }
  });
  const tabText = h('button', {
    class: 'rank-tab', type: 'button', text: '粘贴文本',
    on: { click: () => setMode('text') }
  });

  function setMode(m) {
    collectMode = m;
    tabUrl.classList.toggle('is-on', m === 'url');
    tabText.classList.toggle('is-on', m === 'text');
    urlPane.style.display = m === 'url' ? '' : 'none';
    textPane.style.display = m === 'text' ? '' : 'none';
  }

  const collectBtn = h('button', {
    class: 'btn btn-primary', type: 'button', text: '抓取榜单',
    on: { click: () => collect().catch(() => {}) }
  });

  view.append(
    h('div', { class: 'rank-card rank-collect' },
      h('div', { class: 'rank-collect-head' },
        h('span', { class: 'rank-card-title', text: '采集榜单' }),
        sourceInput,
        h('div', { class: 'rank-collect-tabs' }, tabUrl, tabText)
      ),
      urlPane,
      textPane,
      h('div', { class: 'rank-collect-foot' },
        h('span', { class: 'hint-text', text: '抓取仅在点击时进行一次（10 秒超时 / 2MB 上限）；解析失败会如实提示，可改用粘贴文本。' }),
        collectBtn
      )
    )
  );

  async function collect() {
    if (model.loading) return;
    if (collectMode === 'url' && !urlInput.value.trim()) {
      toast('请先粘贴榜单页面链接', { tone: 'danger' });
      return;
    }
    if (collectMode === 'text' && !textInput.value.trim()) {
      toast('请先粘贴榜单文本', { tone: 'danger' });
      return;
    }
    model.loading = true;
    collectBtn.disabled = true;
    collectBtn.textContent = collectMode === 'url' ? '抓取中…' : '解析中…';
    try {
      const body = collectMode === 'url'
        ? { url: urlInput.value.trim(), source: sourceInput.value.trim() }
        : { text: textInput.value.trim(), source: sourceInput.value.trim() };
      const r = await request(collectMode === 'url' ? '/api/rank/fetch' : '/api/rank/text', {
        method: 'POST',
        body,
        timeout: 30000
      });
      await loadSources(r.source, r.date);
      toast(`已存入快照：${r.source} · ${r.date}（${r.count} 条）`, { tone: 'success' });
      if (r.compared_with) {
        toast(`已与 ${r.compared_with} 的快照对比名次升降`, { duration: 2600 });
      } else {
        toast('这是该源第一份快照：下次采集将自动对比名次升降', { duration: 3000 });
      }
    } catch (err) {
      toast(errorText(err, '采集失败'), { tone: 'danger', duration: 5200 });
    }
    model.loading = false;
    collectBtn.disabled = false;
    collectBtn.textContent = '抓取榜单';
  }

  /* ------------------------------------------------------------- 工具栏（源/日期/对比基准） */

  const sourceSelect = h('select', {
    class: 'input rank-select',
    'aria-label': '选择榜单源',
    on: {
      change: () => {
        model.source = sourceSelect.value;
        fillDateOptions('');
        loadSnapshot().catch(() => {});
      }
    }
  });
  const dateSelect = h('select', {
    class: 'input rank-select',
    'aria-label': '选择快照日期',
    on: {
      change: () => {
        model.date = dateSelect.value;
        loadSnapshot().catch(() => {});
      }
    }
  });
  const againstSelect = h('select', {
    class: 'input rank-select',
    'aria-label': '历史对比基准',
    on: {
      change: () => {
        model.against = againstSelect.value;
        loadSnapshot().catch(() => {});
      }
    }
  });

  const toolbar = h('div', { class: 'rank-card rank-toolbar' },
    h('div', { class: 'rank-field' }, h('span', { class: 'rank-field-label', text: '榜单源' }), sourceSelect),
    h('div', { class: 'rank-field' }, h('span', { class: 'rank-field-label', text: '快照日期' }), dateSelect),
    h('div', { class: 'rank-field' }, h('span', { class: 'rank-field-label', text: '对比基准' }), againstSelect),
    h('div', { class: 'rank-meta', text: '' })
  );
  view.append(toolbar);

  function fillOptions(sel, items, pick) {
    sel.textContent = '';
    for (const [value, label] of items) {
      sel.append(h('option', { value, text: label }));
    }
    sel.value = pick;
    if (sel.value !== pick) sel.value = items.length ? items[0][0] : '';
  }

  function fillDateOptions(preferDate) {
    const src = model.sources.find((s) => s.source === model.source);
    const dates = src ? src.dates : [];
    model.date = preferDate && dates.includes(preferDate) ? preferDate : (dates[0] || '');
    fillOptions(dateSelect, dates.map((d) => [d, d]), model.date);
    /* 对比基准：除当前日期外的其余快照（「自动」= 服务端取上一份） */
    const againstItems = [['', `自动${model.date ? `（上一份快照）` : ''}`]]
      .concat(dates.filter((d) => d !== model.date).map((d) => [d, d]));
    fillOptions(againstSelect, againstItems, model.against);
    model.against = againstSelect.value;   /* 源/日期切换后基准回落到「自动」，防陈旧值 */
  }

  async function loadSources(preferSource, preferDate) {
    try {
      const r = await request('/api/rank/sources', { timeout: 15000 });
      model.sources = (r && r.sources) || [];
    } catch (err) {
      model.sources = [];
      paintMeta(`榜单源读取失败：${errorText(err, '请确认服务端已挂载 webrank 路由')}`);
      return;
    }
    model.source = preferSource && model.sources.some((s) => s.source === preferSource)
      ? preferSource
      : (model.sources[0] ? model.sources[0].source : '');
    fillOptions(sourceSelect, model.sources.map((s) => [s.source, `${s.source}（${s.dates.length} 份快照）`]), model.source);
    fillDateOptions(preferDate || '');
    await loadSnapshot().catch(() => {});
  }

  /* ------------------------------------------------------------- 榜单表格 */

  const tableCard = h('div', { class: 'rank-card' },
    h('div', { class: 'rank-card-title', text: '榜单明细' }),
    h('div', { class: 'rank-table-wrap' })
  );
  const tableWrap = tableCard.querySelector('.rank-table-wrap');
  view.append(tableCard);

  function paintTable() {
    tableWrap.textContent = '';
    if (!model.changes.length) {
      tableWrap.append(h('div', { class: 'rank-empty' },
        h('div', { class: 'rank-empty-icon', html: icon('list', 22) }),
        h('div', { class: 'rank-empty-text', text: '还没有快照。先在上方粘贴榜单链接或榜单文本，采集第一份。' })
      ));
      return;
    }
    const thead = h('thead', {},
      h('tr', {},
        h('th', { text: '名次' }),
        h('th', { text: '变化' }),
        h('th', { text: '书名' }),
        h('th', { text: '作者' }),
        h('th', { text: '数据' })
      )
    );
    const tbody = h('tbody', {});
    for (const c of model.changes) {
      const ch = changeLabel(c.change);
      tbody.append(
        h('tr', {},
          h('td', { class: 'rk-rank', text: String(c.rank) }),
          h('td', { class: `rk-change ${ch.cls}`, text: ch.label }),
          h('td', { class: 'rk-title', text: c.title }),
          h('td', { class: 'rk-author', text: c.author || '佚名' }),
          h('td', { class: 'rk-metric', text: c.metric || '—' })
        )
      );
    }
    tableWrap.append(h('table', { class: 'rank-table' }, thead, tbody));
  }

  function paintMeta(extra) {
    const snap = model.snapshot;
    const bits = [];
    if (snap) {
      bits.push(`${snap.source} · ${snap.date} · ${snap.entries.length} 条`);
      bits.push(snap.url ? `来自链接` : '来自手动粘贴');
      if (snap.review_at) bits.push(`解读于 ${fmtDate(snap.review_at, true)}`);
      if (model.comparedWith) bits.push(`对比基准：${model.comparedWith}`);
    }
    if (extra) bits.push(extra);
    toolbar.querySelector('.rank-meta').textContent = bits.join(' ｜ ');
  }

  /* ------------------------------------------------------------- AI 解读卡 */

  const reviewBody = h('div', { class: 'rank-review-body' });
  const reviewBtn = h('button', {
    class: 'btn btn-primary btn-sm', type: 'button', text: '生成 AI 解读',
    on: { click: () => review(false).catch(() => {}) }
  });
  const reviewRefreshBtn = h('button', {
    class: 'btn btn-sm', type: 'button', text: '重新解读',
    on: { click: () => review(true).catch(() => {}) }
  });

  const reviewCard = h('div', { class: 'rank-card rank-review' },
    h('div', { class: 'rank-review-head' },
      h('span', { class: 'rank-card-title', text: 'AI 解读' }),
      h('span', { style: { flex: '1' } }),
      reviewRefreshBtn,
      reviewBtn
    ),
    reviewBody,
    h('div', {
      class: 'rank-review-note',
      text: '解读只发送榜单数据（名次/书名/作者/公开数据列/名次变化），不会包含你的任何创作内容；结果随快照缓存，换日期需重新生成。'
    })
  );
  view.append(reviewCard);

  function paintReview() {
    reviewBody.textContent = '';
    const snap = model.snapshot;
    if (!snap) {
      reviewBtn.disabled = true;
      reviewRefreshBtn.hidden = true;
      reviewBody.append(h('div', { class: 'hint-text', text: '采集快照后，可让模型基于榜单与名次变化做风向解读。' }));
      return;
    }
    reviewBtn.disabled = false;
    reviewRefreshBtn.hidden = !snap.review;
    if (snap.review) {
      for (const para of String(snap.review).split(/\n{2,}|\n/)) {
        if (para.trim()) reviewBody.append(h('p', { class: 'rank-review-para', text: para.trim() }));
      }
    } else {
      reviewBody.append(h('div', { class: 'hint-text', text: '还没有解读。点击「生成 AI 解读」，模型会基于当前榜单与升降幅给出分类分布 / 标签风向 / 竞品提示。' }));
    }
  }

  async function review(refresh) {
    if (model.reviewing || !model.source || !model.date) return;
    model.reviewing = true;
    reviewBtn.disabled = true;
    reviewRefreshBtn.disabled = true;
    reviewBtn.textContent = refresh ? '重新解读中…' : '解读中…';
    try {
      const r = await request('/api/rank/review', {
        method: 'POST',
        body: { source: model.source, date: model.date, refresh },
        timeout: 120000
      });
      model.snapshot.review = r.review;
      model.snapshot.review_at = r.review_at || '';
      paintReview();
      paintMeta(r.cached ? '（本次为缓存解读）' : '（已生成新解读并缓存）');
      toast(r.cached ? '已读取缓存解读（点「重新解读」可刷新）' : 'AI 解读已生成', { tone: 'success' });
    } catch (err) {
      toast(errorText(err, 'AI 解读失败'), { tone: 'danger', duration: 5200 });
    }
    model.reviewing = false;
    reviewBtn.disabled = false;
    reviewRefreshBtn.disabled = false;
    reviewBtn.textContent = '生成 AI 解读';
  }

  /* ------------------------------------------------------------- 快照装载 */

  async function loadSnapshot() {
    if (!model.source) {
      model.snapshot = null;
      model.changes = [];
      model.comparedWith = '';
      paintTable();
      paintReview();
      paintMeta('');
      return;
    }
    try {
      const r = await request('/api/rank/snapshot', {
        query: { source: model.source, date: model.date, against: model.against },
        timeout: 15000
      });
      model.snapshot = r.snapshot;
      model.changes = r.changes || [];
      model.comparedWith = r.compared_with || '';
      model.date = r.snapshot.date;
      dateSelect.value = model.date;
    } catch (err) {
      model.snapshot = null;
      model.changes = [];
      model.comparedWith = '';
      paintTable();
      paintReview();
      paintMeta(`快照读取失败：${errorText(err, '请重试')}`);
      return;
    }
    paintTable();
    paintReview();
    paintMeta('');
  }

  /* ------------------------------------------------------------ start */

  paintTable();
  paintReview();
  loadSources();

  return {
    el: view,
    destroy() {
      view.remove();
    }
  };
}
