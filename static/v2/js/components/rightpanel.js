/* 右侧工作台：作品 / 规划 / 状态 / 参考 / 技能。 */

import {
  h,
  icon,
  fmtWords,
  fmtNum,
  fmtDate,
  relTime,
  statusMeta,
  downloadUrl,
  MEDIUM_LABELS,
  MEDIUM_UNITS
} from '../core/dom.js';
import { store, patchUi, patchChat } from '../core/state.js';
import { navigate } from '../core/router.js';
import { isMediumBench, verdictLabel } from '../views/mediumbench.js';
import { USAGE_MODES, usageLabel } from '../core/constants.js';
import { toast, confirmDialog, openModal, popoverMenu } from '../core/ui.js';
import { api, errorText, IS_MOCK } from '../core/api.js';
import { createCollabPanel } from './collab.js';
import { openDistillModal } from './distill.js';

const TABS = [
  { key: 'work', label: '作品', icon: 'book-open' },
  { key: 'plan', label: '规划', icon: 'list' },
  { key: 'state', label: '状态', icon: 'activity' },
  { key: 'refs', label: '参考', icon: 'folder' },
  { key: 'skills', label: '技能', icon: 'sparkles' },
  { key: 'collab', label: '协作', icon: 'layers' }
];

/* 协作面板带轮询与订阅，跨 tab / 跨书必须显式销毁，否则 interval 会残留。 */
let collabInst = null;

function destroyCollab() {
  if (!collabInst) return;
  try {
    collabInst.destroy();
  } catch (err) {
    console.error('[collab] destroy failed', err);
  }
  collabInst = null;
}

function collabPane(ctx) {
  const key = store.get().currentKey || '';
  if (!collabInst || collabInst.key !== key) {
    destroyCollab();
    collabInst = createCollabPanel(ctx);
    collabInst.key = key;
  }
  return collabInst.el;
}

const panel = {
  key: '',
  chapterLimit: 40,
  chapterQuery: '',
  chapterFilter: 'all',
  refMode: 'upload',
  refQuery: '',
  refHits: null,
  refLoading: false,
  stateQuery: ''
};

function resetPanelFor(key) {
  if (panel.key === key) return;
  panel.key = key || '';
  panel.chapterLimit = 40;
  panel.chapterQuery = '';
  panel.chapterFilter = 'all';
  panel.stateQuery = '';
  panel.refHits = null;
  panel.refQuery = '';
  destroyCollab();
}

function chaptersFrom(planPayload) {
  const plan = (planPayload && planPayload.plan) || {};
  if (Array.isArray(plan.chapters) && plan.chapters.length) {
    return plan.chapters.map((c) => ({
      n: c.n || c.chapterNumber,
      title: c.title || `第${c.n || c.chapterNumber}章`,
      status: c.status || 'planned',
      words: c.words || c.wordCount || 0
    }));
  }
  const list = [];
  for (const vol of plan.volumes || []) {
    for (const arc of vol.arcs || []) {
      const cards = arc.chapter_cards || [];
      if (cards.length) {
        for (const c of cards) {
          list.push({
            n: c.n || c.chapterNumber,
            title: c.title || `第${c.n}章`,
            status: c.status || 'planned',
            words: c.words || c.wordCount || 0
          });
        }
      } else {
        for (const n of arc.chapters || []) {
          list.push({ n, title: `第${n}章`, status: 'planned', words: 0 });
        }
      }
    }
  }
  return list.sort((a, b) => a.n - b.n);
}

function statusLabel(status) {
  const map = {
    completed: '已写',
    in_progress: '进行中',
    failed: '失败',
    pending: '待写',
    planned: '待规划'
  };
  return map[status] || '待写';
}

function sectionHead(title, countText = '', actions = null) {
  const head = h('div', { class: 'rp-section-head' }, h('h3', { text: title }));
  if (countText) head.append(h('span', { class: 'count', text: countText }));
  if (actions) head.append(actions);
  return head;
}

function emptyState(iconName, title, desc, action = null) {
  const box = h('div', { class: 'empty-state' });
  box.append(h('div', { class: 'empty-icon', html: icon(iconName, 18) }), h('h3', { text: title }), h('p', { text: desc }));
  if (action) box.append(action);
  return box;
}

function featureMissing(name) {
  return h(
    'div',
    { class: 'empty-state' },
    h('div', { class: 'empty-icon', html: icon('plug', 18) }),
    h('h3', { text: `${name}尚未接入` }),
    h('p', { text: '当前后端没有启用对应模块（可能返回 404/501）。界面保持可用，升级后端后会自动出现。' })
  );
}

function exportWork(key, fmt, label) {
  if (IS_MOCK) {
    toast(`演示模式不生成文件；正式环境会导出 ${label}`, { duration: 2600 });
    return;
  }
  downloadUrl(api.exportUrl(key, fmt));
  toast(`正在导出 ${label}…`, { duration: 2000 });
}

/* ---------------------------------------------------------------- work */

/* 剧本/诗词/散文：这里的数字单位属于各自引擎，小说的章节列表、导出与暂停/继续都不适用。 */
function mediumWorkPane(work, ctx) {
  const pane = h('div', { class: 'rp-pane' });
  const unit = MEDIUM_UNITS[work.medium || 'novel'] || '';
  const pct = Number(work.progress) || 0;
  const meta = statusMeta(work.state, work.status_label);
  const summary = h('div', { class: 'rp-summary' });
  summary.append(
    h(
      'div',
      { class: 'rp-summary-top' },
      h('div', { class: 'rp-summary-title', text: work.name }),
      h('span', { class: 'status-chip', dataset: { tone: meta.tone }, text: meta.label })
    ),
    h(
      'div',
      { class: 'rp-summary-meta' },
      h('span', { text: `${work.done} / ${work.total} ${unit}` }),
      h('span', { text: `${pct}%` }),
      h('span', { text: fmtWords(work.total_words) })
    ),
    h('div', { class: 'progress-track', html: `<span style="width:${pct}%"></span>` })
  );
  pane.append(summary);

  const kv = h('div', { class: 'kv-grid' });
  const cells = [
    ['已写字数', fmtNum(work.total_words), '字'],
    ['文体', MEDIUM_LABELS[work.medium || 'novel'] || '作品', ''],
    ['形式', work.form || work.genre || '未分类', ''],
    ['状态', meta.label, '']
  ];
  /* 散文的写法、诗词的格律判定各自引擎才有，缺了就不占一格。 */
  if (work.operation) cells.push(['写法', work.operation, '']);
  if (work.verdict) cells.push(['格律', verdictLabel(work.verdict), '']);
  for (const [k, v, extra] of cells) {
    kv.append(
      h(
        'div',
        { class: 'kv-cell' },
        h('span', { class: 'kv-k', text: k }),
        h('span', { class: 'kv-v' }, `${v}`, extra ? h('span', { class: 'kv-unit', text: extra }) : null)
      )
    );
  }
  pane.append(kv);
  /* 创作过程中就地蒸馏：不用跳去技能库，蒸完当场可用。 */
  pane.append(
    h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '6px', padding: '0 2px 12px' } },
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        html: `${icon('sparkles', 13)}<span>蒸馏风格</span>`,
        title: '把目标作者的文本蒸馏成风格卡片',
        on: {
          click: () => {
            openDistillModal({
              medium: work.medium || 'script',
              /* work.form 是"体式/媒介形式"（电影剧本、现代诗），不是题材，别拿来填错字段。 */
              genre: '',
              onDone: () => ctx && ctx.refreshWork && ctx.refreshWork()
            });
          }
        }
      })
    )
  );
  pane.append(
    h('div', { class: 'hint-text', style: { padding: '12px 2px' }, text: '正文与写作动作在中间的工作台里，按文体分页签展开。' })
  );
  return pane;
}

function workPane(ctx) {
  const state = store.get();
  const work = state.work || state.library.items.find((i) => i.key === state.currentKey);
  const pane = h('div', { class: 'rp-pane' });
  if (!work) {
    pane.append(emptyState('book', '还没有打开作品', '从左侧选择一本作品，或新建一本。'));
    return pane;
  }
  if (isMediumBench(state.workMedium)) return mediumWorkPane(work, ctx);
  const pct = work.total ? Math.round((work.done / work.total) * 100) : 0;
  const meta = statusMeta(work.state, work.status_label);

  const summary = h('div', { class: 'rp-summary' });
  const top = h(
    'div',
    { class: 'rp-summary-top' },
    h('div', { class: 'rp-summary-title', text: work.name }),
    h('span', { class: 'status-chip', dataset: { tone: meta.tone }, text: meta.label })
  );
  summary.append(top);
  summary.append(
    h(
      'div',
      { class: 'rp-summary-meta' },
      h('span', { text: `${work.done} / ${work.total} 章` }),
      h('span', { text: `${pct}%` }),
      h('span', { text: fmtWords(work.total_words) })
    ),
    h('div', { class: 'progress-track', html: `<span style="width:${pct}%"></span>` })
  );
  pane.append(summary);

  const kv = h('div', { class: 'kv-grid' });
  const cells = [
    ['已写字数', fmtNum(work.total_words), '字'],
    ['平均质量', work.quality == null ? '未评分' : work.quality, work.quality == null ? '' : '分'],
    ['今日进度', `${work.done}/${work.total}`, '章'],
    ['体裁', work.genre || '小说', '']
  ];
  for (const [k, v, unit] of cells) {
    kv.append(
      h(
        'div',
        { class: 'kv-cell' },
        h('span', { class: 'kv-k', text: k }),
        h('span', { class: 'kv-v' }, `${v}`, unit ? h('span', { class: 'kv-unit', text: unit }) : null)
      )
    );
  }
  pane.append(kv);

  const actions = h(
    'div',
    { style: { display: 'flex', flexWrap: 'wrap', gap: '6px' } },
    h('button', {
      class: 'btn btn-primary btn-sm',
      type: 'button',
      html: `${icon('edit', 13)}<span>章节工作台</span>`,
      on: { click: () => ctx.openBench(Math.min(work.done + 1, work.total || 1)) }
    }),
    h('button', {
      class: 'btn btn-sm',
      type: 'button',
      html: `${icon(work.running ? 'pause' : 'play', 13)}<span>${work.running ? '暂停' : '继续'}</span>`,
      on: {
        click: async () => {
          try {
            if (work.running) await api.pause(work.key);
            else await api.resume(work.key);
            toast(work.running ? '已暂停创作' : '已继续创作', { tone: 'success' });
            ctx.refreshWork();
          } catch (err) {
            toast(errorText(err, '操作失败'), { tone: 'danger' });
          }
        }
      }
    }),
    h('button', {
      class: 'btn btn-sm',
      type: 'button',
      html: `${icon('download', 13)}<span>导出</span>`,
      on: {
        click: (event) => {
          popoverMenu(
            event.currentTarget,
            [
              { label: 'Word 文档（.docx）', icon: 'file-text', onSelect: () => exportWork(work.key, 'docx', 'Word') },
              { label: 'Markdown（.md）', icon: 'file', onSelect: () => exportWork(work.key, 'md', 'Markdown') },
              { label: 'EPUB 电子书', icon: 'book', onSelect: () => exportWork(work.key, 'epub', 'EPUB') },
              { label: '打印版（.html）', icon: 'grid', onSelect: () => exportWork(work.key, 'print', '打印版') }
            ],
            { align: 'start', label: '导出整本' }
          );
        }
      }
    }),
    h('button', {
      class: 'btn btn-sm',
      type: 'button',
      html: `${icon('sparkles', 13)}<span>蒸馏风格</span>`,
      title: '把目标作者的文本蒸馏成风格卡片',
      on: {
        click: () => {
          openDistillModal({
            medium: 'novel',
            genre: work.genre || '',
            onDone: () => ctx.refreshWork()
          });
        }
      }
    })
  );
  pane.append(actions);

  // Chapter list
  const chapterSection = h('div', { class: 'rp-section' });
  const list = chaptersFrom(state.plan);
  const listEl = h('div', { class: 'chapter-list' });
  const countEl = h('span', { class: 'count' });

  const getFiltered = () => {
    const q = panel.chapterQuery.trim().toLowerCase();
    return list.filter((c) => {
      if (panel.chapterFilter === 'written' && c.status !== 'completed') return false;
      if (panel.chapterFilter === 'pending' && !['pending', 'planned'].includes(c.status)) return false;
      if (panel.chapterFilter === 'failed' && c.status !== 'failed') return false;
      if (q && !String(c.n).includes(q) && !String(c.title || '').toLowerCase().includes(q)) return false;
      return true;
    });
  };

  function paintChapters() {
    const filtered = getFiltered();
    countEl.textContent = `${filtered.length} 章`;
    listEl.textContent = '';
    if (!filtered.length) {
      listEl.append(h('div', { style: { padding: '14px' }, class: 'hint-text', text: '没有匹配的章节' }));
      return;
    }
    const shown = filtered.slice(0, panel.chapterLimit);
    for (const ch of shown) {
      const row = h(
        'button',
        {
          class: `chapter-row${ch.status === 'in_progress' ? ' is-current' : ''}`,
          type: 'button',
          dataset: { state: ch.status },
          title: ch.title
        },
        h('span', { class: 'chapter-state' }),
        h('span', { class: 'chapter-no', text: String(ch.n).padStart(3, '0') }),
        h('span', { class: 'chapter-title', text: String(ch.title || '').replace(/^第\d+章\s*/, '') }),
        h(
          'span',
          { class: 'chapter-meta' },
          ch.words ? h('span', { text: fmtWords(ch.words) }) : h('span', { text: statusLabel(ch.status) })
        )
      );
      row.addEventListener('click', () => ctx.openBench(ch.n));
      listEl.append(row);
    }
    if (filtered.length > shown.length) {
      listEl.append(
        h(
          'div',
          { class: 'chapter-more' },
          h('button', {
            class: 'load-more',
            type: 'button',
            text: `加载更多（还有 ${filtered.length - shown.length} 章）`,
            on: {
              click: () => {
                panel.chapterLimit += 40;
                paintChapters();
              }
            }
          })
        )
      );
    }
  }

  const tools = h(
    'div',
    { class: 'chapter-tools' },
    h('input', {
      class: 'input',
      type: 'search',
      placeholder: '章节号或标题',
      'aria-label': '搜索章节',
      value: panel.chapterQuery,
      style: { width: '120px' },
      on: {
        input: (event) => {
          panel.chapterQuery = event.target.value;
          panel.chapterLimit = 40;
          paintChapters();
        }
      }
    }),
    h('select', {
      class: 'select',
      'aria-label': '筛选章节',
      style: { width: '92px', height: '28px', padding: '0 22px 0 6px', fontSize: '11px' },
      on: {
        change: (event) => {
          panel.chapterFilter = event.target.value;
          paintChapters();
        }
      }
    })
  );
  const filterSelect = tools.querySelector('select');
  [
    ['all', '全部'],
    ['written', '已写'],
    ['pending', '待写'],
    ['failed', '失败']
  ].forEach(([v, label]) => filterSelect.append(h('option', { value: v, text: label, selected: panel.chapterFilter === v })));

  const head = h(
    'div',
    { class: 'rp-section-head' },
    h('h3', { text: '章节' }),
    countEl,
    h('div', { class: 'rp-section-actions' }, tools)
  );
  chapterSection.append(head);
  paintChapters();
  chapterSection.append(listEl);
  pane.append(chapterSection);

  if (Array.isArray(state.live) && state.live.length) {
    const liveSec = h('div', { class: 'rp-section' });
    liveSec.append(sectionHead('实时日志', state.liveConnected === false ? '重连中' : '跟随'));
    const log = h('div', { class: 'live-log' });
    for (const line of state.live.slice(-8).reverse()) {
      log.append(h('div', { class: 'live-log-line', text: line && line.message ? line.message : String(line) }));
    }
    liveSec.append(log);
    pane.append(liveSec);
  }
  return pane;
}

/* ---------------------------------------------------------------- plan */

function planPane(ctx) {
  const state = store.get();
  const payload = state.plan;
  const pane = h('div', { class: 'rp-pane' });
  if (!payload) {
    pane.append(
      featureMissing('长篇规划'),
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        text: '重试读取规划',
        on: { click: () => ctx.refreshPlan() }
      })
    );
    return pane;
  }
  const plan = payload.plan || {};
  const proposals = payload.proposals || [];
  const head = h(
    'div',
    { class: 'plan-head' },
    h('span', { class: 'plan-scale', text: `v${plan.version || 1}` }),
    h('span', { class: 'plan-scale', text: plan.scale_label || (plan.target_words ? fmtWords(plan.target_words) : '未设规模') }),
    h('span', { class: 'chip chip-muted', text: plan.mode || '滚动规划' }),
    h('span', { style: { flex: '1' } }),
    h('button', {
      class: 'btn btn-sm',
      type: 'button',
      html: `${icon('refresh', 13)}<span>重规划</span>`,
      on: { click: () => openReplanDialog(ctx) }
    })
  );
  pane.append(head);

  if (plan.premise) pane.append(h('div', { class: 'plan-digest', text: plan.premise }));
  if (payload.digest && payload.digest.summary) {
    pane.append(h('div', { class: 'plan-digest', text: payload.digest.summary }));
  }

  const tree = h('div', { class: 'plan-tree' });
  (plan.volumes || []).forEach((vol, index) => {
    const volEl = h('div', { class: 'plan-volume', dataset: { open: index === 0 ? 'true' : 'false' } });
    const volHead = h(
      'button',
      { class: 'plan-vol-head', type: 'button', 'aria-expanded': index === 0 ? 'true' : 'false' },
      h('span', { class: 'vol-no', text: `卷${vol.no || index + 1}` }),
      h('span', { class: 'vol-title', text: vol.title || '未命名卷' }),
      h('span', { class: 'vol-count', text: `${(vol.arcs || []).reduce((n, a) => n + (a.chapters || []).length, 0)} 章` }),
      h('span', { class: 'chev', html: icon('chevron-right', 12) })
    );
    volHead.addEventListener('click', () => {
      const open = volEl.dataset.open === 'true';
      volEl.dataset.open = open ? 'false' : 'true';
      volHead.setAttribute('aria-expanded', open ? 'false' : 'true');
    });
    volEl.append(volHead);
    const body = h('div', { class: 'plan-vol-body' });
    if (vol.goal) body.append(h('div', { class: 'hint-text', style: { padding: '2px 0 4px' }, text: vol.goal }));
    for (const arc of vol.arcs || []) {
      const arcEl = h('div', { class: 'plan-arc' });
      arcEl.append(
        h(
          'div',
          { class: 'plan-arc-head' },
          h('span', { class: 'arc-title', text: `弧 ${arc.no || ''} · ${arc.title || ''}` }),
          arc.goal ? h('span', { class: 'arc-goal', text: arc.goal }) : null
        )
      );
      const chips = h('div', { class: 'chapter-chips' });
      const cards = arc.chapter_cards || [];
      const nums = arc.chapters || [];
      for (const n of nums) {
        const card = cards.find((c) => (c.n || c.chapterNumber) === n);
        const st = card ? card.status : 'planned';
        chips.append(
          h('button', {
            class: 'chapter-chip',
            type: 'button',
            dataset: { state: st === 'completed' ? 'completed' : st === 'in_progress' ? 'current' : 'planned' },
            title: card && card.title ? card.title : `第 ${n} 章`,
            text: String(n),
            on: { click: () => ctx.openBench(n) }
          })
        );
      }
      arcEl.append(chips);
      body.append(arcEl);
    }
    volEl.append(body);
    tree.append(volEl);
  });
  pane.append(tree);

  const proposalSection = h('div', { class: 'rp-section' });
  proposalSection.append(sectionHead('重规划提案', `${proposals.length} 条`));
  if (!proposals.length) {
    proposalSection.append(h('div', { class: 'hint-text', text: '还没有提案。需要调整结构时，点上方「重规划」。' }));
  }
  /* 多岗位同时规划（v2.1 P2）：带 agent 的提案并排成对比卡，其余按原列表展示。 */
  const roleProps = proposals.filter((p) => p && p.agent);
  const plainProps = proposals.filter((p) => p && !p.agent);
  if (roleProps.length) {
    proposalSection.append(h('div', { class: 'hint-text', text: '同一条指令由两个岗位各出一份提案，对比后选一份应用。' }));
    const compare = h('div', { class: 'proposal-compare' });
    for (const prop of roleProps.slice(0, 4)) compare.append(proposalCard(ctx, prop, true));
    proposalSection.append(compare);
  }
  for (const prop of plainProps.slice(0, 6)) proposalSection.append(proposalCard(ctx, prop, false));
  pane.append(proposalSection);
  return pane;
}

function proposalCard(ctx, prop, wide) {
  const card = h('div', {
    class: `proposal-card${wide ? ' proposal-card-wide' : ''}`,
    dataset: { status: prop.status || 'pending' }
  });
  const stText = prop.status === 'applied' ? '已应用' : prop.status === 'rejected' ? '已驳回' : '待确认';
  const stTone = prop.status === 'applied' ? 'success' : prop.status === 'rejected' ? 'muted' : 'warn';
  /* 多岗位提案的 reason 是内部标记（multi:planner），卡面显示角色名更好读。 */
  const isMulti = String(prop.reason || '').startsWith('multi:');
  const titleText = isMulti
    ? `双岗位同时规划 · ${prop.role || prop.reason.slice(6)}`
    : prop.reason || prop.instruction || '规划重排';
  card.append(
    h(
      'div',
      { class: 'proposal-head' },
      h('span', { html: icon('git-commit', 12) }),
      h('span', { text: prop.role || prop.scope || '规划调整' }),
      prop.role ? h('span', { class: 'chip chip-muted', text: prop.scope || '规划调整' }) : null,
      h('span', { class: 'status-chip', dataset: { tone: stTone }, text: stText })
    ),
    h('div', { class: 'proposal-title', text: titleText }),
    h('div', { class: 'proposal-summary', text: prop.summary || prop.instruction || '' })
  );
  if (Array.isArray(prop.diff) && prop.diff.length) {
    const list = h('ul', { class: 'proposal-diff proposal-diff-list' });
    for (const line of prop.diff.slice(0, 8)) list.append(h('li', { text: String(line) }));
    card.append(list);
    if (prop.diff.length > 8) {
      card.append(h('div', { class: 'hint-text', text: `另有 ${prop.diff.length - 8} 条变更说明` }));
    }
  } else if (prop.diff) {
    card.append(h('div', { class: 'proposal-diff', text: String(prop.diff) }));
  }
  if (!prop.status || prop.status === 'pending') {
    const actions = h(
      'div',
      { class: 'proposal-actions' },
      h('button', {
        class: 'btn btn-primary btn-sm',
        type: 'button',
        text: '应用',
        on: { click: () => applyProposal(ctx, prop) }
      }),
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        text: '驳回',
        on: { click: () => rejectProposal(ctx, prop) }
      })
    );
    card.append(actions);
  }
  return card;
}

function openReplanDialog(ctx) {
  const scope = h(
    'select',
    { class: 'select' },
    h('option', { value: 'window', text: '接下来 10 章' }),
    h('option', { value: 'volume', text: '当前卷' }),
    h('option', { value: 'arc', text: '当前弧' }),
    h('option', { value: 'book', text: '全书' })
  );
  const instruction = h('textarea', {
    class: 'textarea',
    rows: 4,
    placeholder: '例如：第 46 章留存回落，压缩两章调查，把对峙提前到第 48 章。'
  });
  const mode = h(
    'select',
    { class: 'select' },
    h('option', { value: 'single', text: '单个岗位（总编视角）' }),
    h('option', { value: 'multi', text: '双岗位同时规划（规划师 + 总编）' })
  );
  const body = h(
    'div',
    { class: 'feedback-form' },
    h('div', { class: 'field' }, h('span', { class: 'field-label', text: '出提案的岗位' }), mode),
    h('div', { class: 'field' }, h('span', { class: 'field-label', text: '调整范围' }), scope),
    h('div', { class: 'field' }, h('span', { class: 'field-label', text: '调整要求' }), instruction)
  );
  const footer = h(
    'div',
    { style: { display: 'flex', gap: '8px', width: '100%', justifyContent: 'flex-end' } },
    h('button', { class: 'btn', type: 'button', text: '取消', on: { click: () => modal.close() } }),
    h('button', {
      class: 'btn btn-primary',
      type: 'button',
      html: '<span class="wiz-loading"><span class="spinner"></span>生成提案</span>',
      on: {
        click: async (event) => {
          const btn = event.currentTarget;
          if (!instruction.value.trim()) {
            toast('请写清楚要调整什么', { tone: 'danger' });
            instruction.focus();
            return;
          }
          btn.disabled = true;
          const multi = mode.value === 'multi';
          try {
            if (multi) {
              const res = await api.planMulti({
                key: store.get().currentKey,
                scope: scope.value,
                instruction: instruction.value.trim()
              });
              const cards = (res && res.cards) || [];
              const failed = cards.filter((c) => c.ok === false);
              const done = cards.length - failed.length;
              const msg = failed.length
                ? `已出 ${done} 份提案（${failed.map((f) => f.label || f.agent).join('、')}失败）`
                : `已出 ${cards.length} 份提案：对比后选一份应用`;
              toast(msg, { tone: failed.length ? 'warn' : 'success', duration: 3600 });
            } else {
              await api.replan({
                key: store.get().currentKey,
                scope: scope.value,
                instruction: instruction.value.trim(),
                reason: '用户手动重规划'
              });
              toast('提案已生成，确认后才会写入规划', { tone: 'success' });
            }
            modal.close();
            ctx.refreshPlan();
          } catch (err) {
            toast(errorText(err, '生成提案失败'), { tone: 'danger' });
            btn.disabled = false;
            btn.innerHTML = '生成提案';
          }
        }
      }
    })
  );
  const modal = openModal({
    title: '重规划',
    subtitle: '会先生成一份提案与差异，确认后才应用。双岗位时两位各出一份，你选一份。',
    body,
    footer,
    size: 'sm'
  });
}

async function applyProposal(ctx, prop) {
  const ok = await confirmDialog({
    title: '应用规划提案',
    message: `将把「${prop.scope || '规划调整'}」写入作品规划，并可能影响尚未写作的章节。`,
    detail: '已通过的章节不会被改动。应用前建议先看一眼差异。',
    confirmText: '应用'
  });
  if (!ok) return;
  try {
    await api.applyPlan(store.get().currentKey, prop.id);
    toast('规划已更新', { tone: 'success' });
    ctx.refreshPlan();
    const chat = store.get().chat;
    if (chat.approval && chat.approval.proposal_id === prop.id) {
      patchChat({ approval: { ...chat.approval, done: '已应用' } });
    }
  } catch (err) {
    toast(errorText(err, '应用失败'), { tone: 'danger' });
  }
}

async function rejectProposal(ctx, prop) {
  const ok = await confirmDialog({
    title: '驳回提案',
    message: '驳回后这份提案不会再自动应用，规划保持原样。',
    confirmText: '驳回',
    danger: true
  });
  if (!ok) return;
  try {
    await api.rejectPlan(store.get().currentKey, prop.id, '');
    toast('已驳回提案');
    ctx.refreshPlan();
    const chat = store.get().chat;
    if (chat.approval && chat.approval.proposal_id === prop.id) {
      patchChat({ approval: { ...chat.approval, done: '已驳回' } });
    }
  } catch (err) {
    toast(errorText(err, '驳回失败'), { tone: 'danger' });
  }
}

/* --------------------------------------------------------------- state */

function entityGroup(title, iconName, items, renderRight) {
  const wrap = h('div', { class: 'rp-section' });
  wrap.append(sectionHead(title, String(items.length)));
  const list = h('div', { class: 'entity-group' });
  for (const item of items) {
    list.append(
      h(
        'div',
        { class: 'entity-row' },
        h('span', { class: 'ent-icon', html: icon(iconName, 12) }),
        h(
          'span',
          { class: 'ent-main' },
          h('span', { class: 'ent-name', text: item.name || item.text || '' }),
          h('span', { class: 'ent-sub', text: item.role || item.note || item.status || '' })
        ),
        renderRight ? renderRight(item) : null
      )
    );
  }
  wrap.append(list);
  return wrap;
}

function statePane(ctx) {
  const state = store.get();
  const payload = state.lore;
  const pane = h('div', { class: 'rp-pane' });
  if (!payload) {
    pane.append(
      featureMissing('状态库'),
      h('button', { class: 'btn btn-sm', type: 'button', text: '重试读取状态', on: { click: () => ctx.refreshState() } })
    );
    return pane;
  }
  const q = panel.stateQuery.trim().toLowerCase();
  const filter = (arr) =>
    !q
      ? arr
      : arr.filter((x) =>
          [x.name, x.role, x.note, x.status, x.text].some((v) => String(v || '').toLowerCase().includes(q))
        );

  const metrics = payload.state && payload.state.metrics ? payload.state.metrics : {};
  const kv = h('div', { class: 'kv-grid' });
  (() => {
    const status = state.work || {};
    const cells = [
      ['当前章', payload.current_chapter || '—', '章'],
      ['已写', `${payload.chapters ? payload.chapters.done : '—'}/${payload.chapters ? payload.chapters.total : '—'}`, ''],
      ['总字数', fmtNum(status.total_words || metrics.words || 0), '字'],
      ['到期伏笔', (payload.foreshadow_due && (payload.foreshadow_due.due_count || (payload.foreshadow_due.due || []).length)) || 0, '条']
    ];
    for (const [k, v, unit] of cells) {
      kv.append(
        h(
          'div',
          { class: 'kv-cell' },
          h('span', { class: 'kv-k', text: k }),
          h('span', { class: 'kv-v' }, `${v}`, unit ? h('span', { class: 'kv-unit', text: unit }) : null)
        )
      );
    }
  })();
  pane.append(kv);

  const search = h('input', {
    class: 'input',
    type: 'search',
    placeholder: '筛选人物、地点、线索',
    'aria-label': '筛选状态实体',
    value: panel.stateQuery,
    on: {
      input: (event) => {
        panel.stateQuery = event.target.value;
        ctx.rerenderPanel();
      }
    }
  });
  pane.append(search);

  const due = filter(payload.foreshadowing || []).filter(
    (f) => f.due != null && f.status !== 'done' && f.due <= (payload.current_chapter || 0) + 2
  );
  if (due.length) {
    const sec = h('div', { class: 'rp-section' });
    sec.append(sectionHead('即将到期', `${due.length} 条`));
    for (const f of due.slice(0, 6)) {
      const overdue = f.due < (payload.current_chapter || 0);
      sec.append(
        h(
          'div',
          { class: 'issue-row', dataset: { severity: overdue ? 'high' : 'medium' } },
          h('span', { class: 'issue-dot' }),
          h('span', { class: 'issue-text', text: f.text || f.note || '' }),
          h('span', { class: 'due-chip', dataset: { overdue: overdue ? 'true' : 'false', soon: !overdue }, text: `第${f.due}章` })
        )
      );
    }
    pane.append(sec);
  }

  const issues = (payload.issues || []).filter(
    (s) => !q || String(s.text || '').toLowerCase().includes(q) || String(s.ref || '').toLowerCase().includes(q)
  );
  if (issues.length) {
    const sec = h('div', { class: 'rp-section' });
    sec.append(sectionHead('一致性问题', `${issues.length} 条`));
    for (const issue of issues.slice(0, 6)) {
      sec.append(
        h(
          'div',
          { class: 'issue-row', dataset: { severity: issue.severity || 'medium' } },
          h('span', { class: 'issue-dot' }),
          h('span', { class: 'issue-text', text: issue.text }),
          issue.ref ? h('span', { class: 'issue-ref', text: issue.ref }) : null
        )
      );
    }
    pane.append(sec);
  }

  const entities = [
    ['人物', 'users', payload.characters || [], (x) =>
      h('span', { class: 'entity-sub', style: { flex: 'none', maxWidth: '84px' }, text: x.status || '' })],
    ['地点', 'pin', payload.locations || []],
    ['物件', 'package', payload.items || []],
    ['线索 / 伏笔', 'flag', payload.threads || [], (x) =>
      x.progress != null ? h('span', { class: 'progress-track', style: { width: '44px' }, html: `<span style="width:${x.progress}%"></span>` }) : null]
  ];
  for (const [title, ic, items, right] of entities) {
    const filtered = filter(items);
    if (filtered.length) pane.append(entityGroup(title, ic, filtered.slice(0, 10), right));
  }

  if (Array.isArray(payload.timeline) && payload.timeline.length) {
    const sec = h('div', { class: 'rp-section' });
    sec.append(sectionHead('时间线', `${payload.timeline.length} 个节点`));
    const tl = h('div', { class: 'timeline' });
    for (const item of payload.timeline) {
      tl.append(
        h(
          'div',
          { class: 'timeline-item', dataset: { current: item.current ? 'true' : 'false' } },
          h('span', { class: 'tl-at', text: item.at || '' }),
          h('span', { class: 'tl-main', text: item.text || '' })
        )
      );
    }
    sec.append(tl);
    pane.append(sec);
  }
  return pane;
}

/* ---------------------------------------------------------------- refs */

function refKindIcon(ref) {
  if (ref.kind === 'url') return 'link';
  if (ref.kind === 'file') return 'file-text';
  return 'file';
}

async function searchReferences(ctx) {
  const q = panel.refQuery.trim();
  if (!q) {
    panel.refHits = null;
    ctx.rerenderPanel();
    return;
  }
  panel.refLoading = true;
  ctx.rerenderPanel();
  try {
    const res = await api.searchRefs(store.get().currentKey, q, 6);
    panel.refHits = (res && res.items) || [];
  } catch (err) {
    panel.refHits = [];
    toast(errorText(err, '检索失败'), { tone: 'danger' });
  } finally {
    panel.refLoading = false;
    ctx.rerenderPanel();
  }
}

function refsPane(ctx) {
  const state = store.get();
  const pane = h('div', { class: 'rp-pane' });
  if (state.refsError) pane.append(h('div', { class: 'error-text', text: state.refsError }));
  if (state.features.references === false) pane.append(featureMissing('参考资料'));

  const add = h('div', { class: 'ref-add' });
  const seg = h('div', { class: 'segmented' });
  const modes = [
    ['upload', '上传'],
    ['text', '粘贴'],
    ['url', '网址']
  ];
  for (const [value, label] of modes) {
    seg.append(
      h('button', {
        class: 'seg-btn',
        type: 'button',
        'aria-pressed': panel.refMode === value ? 'true' : 'false',
        text: label,
        on: {
          click: () => {
            panel.refMode = value;
            ctx.rerenderPanel();
          }
        }
      })
    );
  }
  add.append(seg);

  if (panel.refMode === 'upload') {
    const input = h('input', { type: 'file', style: { display: 'none' }, accept: '.txt,.md,.docx,.pdf,.epub,.rtf,.html,.json,.csv' });
    add.append(
      h(
        'button',
        {
          class: 'btn btn-sm',
          type: 'button',
          html: `${icon('upload', 13)}<span>选择文件（≤20 MB）</span>`,
          on: { click: () => input.click() }
        },
        input
      ),
      h('div', { class: 'hint-text', text: '支持 txt / md / docx / pdf / epub；上传后会抽取正文并入库。' })
    );
    input.addEventListener('change', async () => {
      const file = input.files && input.files[0];
      input.value = '';
      if (!file) return;
      if (file.size > 20 * 1024 * 1024) {
        toast('文件超过 20 MB，请拆分后再上传', { tone: 'danger' });
        return;
      }
      try {
        const form = new FormData();
        form.append('file', file, file.name);
        form.append('filename', file.name);
        form.append('title', file.name.replace(/\.[^.]+$/, ''));
        form.append('usage_mode', 'facts');
        form.append('project_key', state.currentKey);
        form.append('copyright_confirmed', 'true');
        toast('正在解析文件…', { duration: 1800 });
        await api.uploadRef(form);
        toast('参考资料已入库', { tone: 'success' });
        ctx.refreshRefs();
      } catch (err) {
        toast(errorText(err, '上传失败'), { tone: 'danger' });
      }
    });
  } else if (panel.refMode === 'text') {
    const title = h('input', { class: 'input', placeholder: '资料标题，如「港城旧事」' });
    const usage = h('select', { class: 'select' }, ...USAGE_MODES.map((u) => h('option', { value: u.value, text: u.label })));
    const text = h('textarea', { class: 'textarea', rows: 3, placeholder: '粘贴正文或摘录…' });
    const copy = h('input', { type: 'checkbox', checked: true });
    add.append(
      h('div', { class: 'ref-form' },
        h('div', { class: 'form-row' }, title, usage),
        text,
        h('label', { class: 'copyright' }, copy, h('span', { text: '我确认拥有或已获授权使用这份资料' })),
        h('div', { class: 'form-actions' },
          h('button', {
            class: 'btn btn-primary btn-sm',
            type: 'button',
            text: '入库',
            on: {
              click: async () => {
                if (!text.value.trim()) {
                  toast('请粘贴资料正文', { tone: 'danger' });
                  return;
                }
                try {
                  await api.addRefText({
                    title: title.value.trim() || '未命名资料',
                    text: text.value.trim(),
                    usage_mode: usage.value,
                    copyright_confirmed: copy.checked,
                    project_key: state.currentKey
                  });
                  toast('参考资料已入库', { tone: 'success' });
                  ctx.refreshRefs();
                } catch (err) {
                  toast(errorText(err, '入库失败'), { tone: 'danger' });
                }
              }
            }
          })
        )
      )
    );
  } else {
    const url = h('input', { class: 'input', placeholder: 'https://…' });
    const usage = h('select', { class: 'select' }, ...USAGE_MODES.map((u) => h('option', { value: u.value, text: u.label })));
    add.append(
      h('div', { class: 'ref-form' },
        h('div', { class: 'form-row' }, url, usage),
        h('div', { class: 'form-actions' },
          h('button', {
            class: 'btn btn-primary btn-sm',
            type: 'button',
            text: '抓取并入库',
            on: {
              click: async () => {
                if (!/^https?:\/\//i.test(url.value.trim())) {
                  toast('只支持 http / https 网址', { tone: 'danger' });
                  return;
                }
                try {
                  await api.addRefUrl({
                    url: url.value.trim(),
                    usage_mode: usage.value,
                    copyright_confirmed: true,
                    project_key: state.currentKey
                  });
                  toast('网页已抓取并入库', { tone: 'success' });
                  ctx.refreshRefs();
                } catch (err) {
                  toast(errorText(err, '抓取失败'), { tone: 'danger' });
                }
              }
            }
          })
        )
      )
    );
  }
  pane.append(add);

  const searchSec = h('div', { class: 'rp-section' });
  searchSec.append(sectionHead('检索参考', panel.refHits ? `${panel.refHits.length} 条命中` : ''));
  const searchRow = h('div', { class: 'ref-search' },
    h('input', {
      class: 'input',
      type: 'search',
      placeholder: '例如：1998 年沉船',
      'aria-label': '检索参考资料',
      value: panel.refQuery,
      on: {
        input: (event) => {
          panel.refQuery = event.target.value;
        },
        keydown: (event) => {
          if (event.key === 'Enter') searchReferences(ctx);
        }
      }
    }),
    h('button', {
      class: 'btn btn-sm',
      type: 'button',
      text: panel.refLoading ? '检索中…' : '检索',
      disabled: panel.refLoading,
      on: { click: () => searchReferences(ctx) }
    })
  );
  searchSec.append(searchRow);
  if (panel.refHits) {
    const results = h('div', { class: 'ref-search-results' });
    if (!panel.refHits.length) results.append(h('div', { class: 'hint-text', text: '没有命中，换个关键词试试。' }));
    for (const hit of panel.refHits) {
      results.append(
        h(
          'div',
          { class: 'ref-hit' },
          h('div', { class: 'hit-title' }, `${hit.title || ''} · 片段 ${hit.chunk_no || ''}`, h('span', { class: 'score', text: hit.score != null ? Number(hit.score).toFixed(2) : '' })),
          h('div', { text: hit.snippet || '' })
        )
      );
    }
    searchSec.append(results);
  }
  pane.append(searchSec);

  const refs = state.refs || [];
  const listSec = h('div', { class: 'rp-section' });
  listSec.append(sectionHead('已关联资料', `${refs.length} 份`));
  if (!refs.length) {
    listSec.append(emptyState('folder', '还没有参考资料', '上传文件、粘贴文本或添加网址，让正文有据可依。'));
  }
  const list = h('div', { class: 'ref-list' });
  for (const ref of refs) {
    const card = h('div', { class: 'ref-card' });
    card.append(
      h(
        'div',
        { class: 'ref-head' },
        h('span', { class: 'ref-kind', html: icon(refKindIcon(ref), 12) }),
        h('span', { class: 'ref-title', title: ref.title, text: ref.title }),
        h('span', { class: 'chip chip-muted', text: usageLabel(ref.usage_mode) })
      ),
      h(
        'div',
        { class: 'ref-meta' },
        h('span', { text: ref.chars ? fmtWords(ref.chars) : '未知长度' }),
        h('span', { text: ref.created_at ? fmtDate(ref.created_at) : '' }),
        ref.copyright_confirmed === false ? h('span', { class: 'due-chip', dataset: { overdue: 'true' }, text: '版权待确认' }) : null
      ),
      h('div', { class: 'ref-summary', text: ref.summary || '还没有分析摘要。' })
    );
    const actions = h('div', { class: 'ref-actions' });
    const usage = h('select', { class: 'select', 'aria-label': `用途：${ref.title}` });
    for (const u of USAGE_MODES) usage.append(h('option', { value: u.value, text: u.label, selected: u.value === ref.usage_mode }));
    usage.addEventListener('change', async () => {
      try {
        await api.patchRef(ref.id, { usage_mode: usage.value });
        toast('用途已更新', { duration: 1600 });
        ctx.refreshRefs();
      } catch (err) {
        toast(errorText(err, '更新失败'), { tone: 'danger' });
      }
    });
    actions.append(
      usage,
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        text: '分析',
        on: {
          click: async (event) => {
            const btn = event.currentTarget;
            btn.disabled = true;
            btn.textContent = '分析中…';
            try {
              await api.analyzeRef(ref.id);
              toast('分析完成', { tone: 'success' });
              ctx.refreshRefs();
            } catch (err) {
              toast(errorText(err, '分析失败'), { tone: 'danger' });
              btn.disabled = false;
              btn.textContent = '分析';
            }
          }
        }
      }),
      h('button', {
        class: 'icon-btn',
        type: 'button',
        'aria-label': `删除 ${ref.title}`,
        html: icon('trash', 13),
        on: {
          click: async () => {
            const ok = await confirmDialog({
              title: '删除参考资料',
              message: `确定删除「${ref.title}」？已写入正文的内容不会受影响。`,
              confirmText: '删除',
              danger: true
            });
            if (!ok) return;
            try {
              await api.deleteRef(ref.id);
              toast('已删除');
              ctx.refreshRefs();
            } catch (err) {
              toast(errorText(err, '删除失败'), { tone: 'danger' });
            }
          }
        }
      })
    );
    card.append(actions);
    list.append(card);
  }
  if (refs.some((r) => r.copyright_confirmed === false)) {
    list.append(
      h('div', { class: 'copyright-note' }, h('span', { html: icon('alert', 12) }), h('span', { text: '有资料尚未确认版权。请只使用你拥有权利或已获授权的内容。' }))
    );
  }
  listSec.append(list);
  pane.append(listSec);
  return pane;
}

/* -------------------------------------------------------------- skills */

/* 右栏「本书适用技能」：异步查 /api/skills/applicable，把当前题材真正注入的技能显性化。 */
function applicableCard(ctx) {
  const key = store.get().currentKey || '';
  const sec = h('div', { class: 'rp-section' });
  const count = h('span', { class: 'count', text: '检测中…' });
  const body = h('div', { class: 'hint-text', text: '正在按本书文体与题材匹配技能…' });
  const go = h('button', {
    class: 'btn btn-sm',
    type: 'button',
    text: '打开技能库',
    on: { click: () => navigate('/skills') }
  });
  sec.append(h('div', { class: 'rp-section-head' }, h('h3', { text: '本书适用技能' }), count, go), body);

  async function load() {
    body.textContent = '正在按本书文体与题材匹配技能…';
    count.textContent = '检测中…';
    let res = null;
    let errText = '';
    try {
      res = await api.skillsApplicable(key);
    } catch (err) {
      errText = errorText(err, '查询失败');
    }
    if (store.get().currentKey !== key) return;
    body.textContent = '';
    if (!res) {
      count.textContent = '—';
      body.append(h('span', { class: 'error-text', text: errText || '后端未提供本书技能查询' }));
      return;
    }
    const items = (Array.isArray(res.items) ? res.items : []).filter(Boolean);
    count.textContent = `${items.length} 项 · ${res.medium || ''}${res.genre ? ` · ${res.genre}` : ''}`;
    if (!items.length) {
      body.append(h('span', { text: '这本书暂未命中任何题材技能，可在技能库里补一本题材craft。' }));
      return;
    }
    const chips = h('div', { class: 'chip-row' });
    for (const it of items.slice(0, 12)) {
      chips.append(h('span', { class: 'chip', text: it.title || it.name, title: it.genre || '' }));
    }
    if (items.length > 12) chips.append(h('span', { class: 'chip chip-muted', text: `+${items.length - 12}` }));
    body.append(chips);
  }

  if (!key || IS_MOCK) {
    count.textContent = '—';
    body.textContent = '选中一本书后可以查看它会注入的技能。';
  } else {
    load();
  }
  return sec;
}

function skillsPane(ctx) {
  const state = store.get();
  const payload = state.skills;
  const pane = h('div', { class: 'rp-pane' });
  pane.append(applicableCard(ctx));
  if (!payload) {
    pane.append(
      featureMissing('技能进化'),
      h('button', { class: 'btn btn-sm', type: 'button', text: '重试读取技能', on: { click: () => ctx.refreshSkills() } })
    );
    return pane;
  }
  const stats = payload.stats || {};
  const feedback = payload.feedback || {};
  const kv = h('div', { class: 'kv-grid' });
  [
    ['候选技能', (payload.candidates || []).filter((c) => c.pending).length, '个'],
    ['生效版本', stats.active || (payload.history || []).filter((v) => v.active).length, '个'],
    ['反馈样本', feedback.total || 0, '条'],
    ['平均评分', feedback.avg_rating || '—', '分']
  ].forEach(([k, v, unit]) => {
    kv.append(
      h(
        'div',
        { class: 'kv-cell' },
        h('span', { class: 'kv-k', text: k }),
        h('span', { class: 'kv-v' }, `${v}`, unit ? h('span', { class: 'kv-unit', text: unit }) : null)
      )
    );
  });
  pane.append(kv);

  const candSec = h('div', { class: 'rp-section' });
  candSec.append(
    sectionHead('候选技能', `${(payload.candidates || []).length} 个`, h('div', { class: 'rp-section-actions' },
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        html: `${icon('sparkles', 12)}<span>从反馈生成</span>`,
        on: {
          click: async (event) => {
            const btn = event.currentTarget;
            btn.disabled = true;
            try {
              await api.proposeSkill({ from_feedback: true, medium: state.ui.medium });
              toast('已生成候选，需评测后转正', { tone: 'success' });
              ctx.refreshSkills();
            } catch (err) {
              toast(errorText(err, '生成失败'), { tone: 'danger' });
              btn.disabled = false;
            }
          }
        }
      })
    ))
  );
  if (!(payload.candidates || []).length) {
    candSec.append(h('div', { class: 'hint-text', text: '还没有候选。积累反馈后可以从反馈生成。' }));
  }
  for (const cand of payload.candidates || []) {
    const card = h('div', { class: 'skill-card', dataset: { pending: cand.pending ? 'true' : 'false' } });
    card.append(
      h(
        'div',
        { class: 'skill-head' },
        h('span', { class: 'skill-name', text: cand.name }),
        h('span', { class: 'skill-delta', text: cand.delta ? `${cand.delta}` : '' })
      ),
      h('div', { class: 'hint-text', text: cand.reason || '' })
    );
    if (cand.diff) {
      const diff = h('div', { class: 'skill-diff' });
      for (const line of String(cand.diff).split('\n')) {
        const cls = line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : '';
        diff.append(h('span', { class: cls, text: `${line}\n` }));
      }
      card.append(diff);
    }
    if (cand.pending) {
      card.append(
        h(
          'div',
          { class: 'skill-actions' },
          h('button', {
            class: 'btn btn-sm',
            type: 'button',
            text: '评测',
            on: {
              click: async (event) => {
                const btn = event.currentTarget;
                btn.disabled = true;
                btn.textContent = '评测中…';
                try {
                  const res = await api.evaluateSkill({ id: cand.id, baseline: 'current' });
                  const delta = (res && res.result && res.result.delta) || '';
                  toast(`评测完成${delta ? `：${delta}` : ''}`, { tone: 'success' });
                  ctx.refreshSkills();
                } catch (err) {
                  toast(errorText(err, '评测失败'), { tone: 'danger' });
                  btn.disabled = false;
                  btn.textContent = '评测';
                }
              }
            }
          }),
          h('button', {
            class: 'btn btn-primary btn-sm',
            type: 'button',
            text: '转正',
            on: { click: () => promoteSkill(ctx, cand) }
          }),
          h('button', {
            class: 'btn btn-ghost btn-sm',
            type: 'button',
            text: '驳回',
            on: async () => {
              try {
                await api.rejectSkill(cand.id, '');
                toast('已驳回');
                ctx.refreshSkills();
              } catch (err) {
                toast(errorText(err, '驳回失败'), { tone: 'danger' });
              }
            }
          })
        )
      );
    } else {
      card.append(h('span', { class: 'status-chip', dataset: { tone: 'success' }, text: cand.promoted ? '已转正' : '已评测' }));
    }
    candSec.append(card);
  }
  pane.append(candSec);

  const histSec = h('div', { class: 'rp-section' });
  histSec.append(sectionHead('版本历史', `${(payload.history || []).length} 条`));
  const histList = h('div', { style: { display: 'flex', flexDirection: 'column', gap: '1px' } });
  for (const v of payload.history || []) {
    const row = h(
      'div',
      { class: 'version-row' },
      h('span', { class: 'dot', style: v.active ? { background: 'var(--success)' } : {} }),
      h('span', { class: 'v-name', text: `${v.name} · ${v.version}` }),
      h('span', { class: 'v-time', text: v.at ? relTime(v.at) : '' }),
      v.active
        ? h('span', { class: 'badge badge-accent', text: '生效' })
        : h('button', {
            class: 'btn btn-sm',
            type: 'button',
            text: '回滚',
            on: {
              click: async () => {
                const ok = await confirmDialog({
                  title: '回滚技能版本',
                  message: `将 ${v.name} 回滚到 ${v.version}。当前版本会保留，可再次回滚。`,
                  confirmText: '回滚'
                });
                if (!ok) return;
                try {
                  await api.rollbackSkill(v.name, v.version);
                  toast('已回滚', { tone: 'success' });
                  ctx.refreshSkills();
                } catch (err) {
                  toast(errorText(err, '回滚失败'), { tone: 'danger' });
                }
              }
            }
          })
    );
    histList.append(row);
  }
  histSec.append(histList);
  pane.append(histSec);

  if (Array.isArray(feedback.tags) && feedback.tags.length) {
    const fbSec = h('div', { class: 'rp-section' });
    fbSec.append(sectionHead('反馈热点', `${feedback.total || 0} 条`));
    const bars = h('div', { class: 'eval-bars' });
    const max = Math.max(...feedback.tags.map((t) => t.count || 1));
    for (const tag of feedback.tags) {
      bars.append(
        h(
          'div',
          { class: 'eval-row' },
          h('span', { text: tag.tag }),
          h('span', { class: 'eval-track' }, h('span', { style: { width: `${Math.round(((tag.count || 0) / max) * 100)}%` } })),
          h('span', { class: 'eval-val', text: String(tag.count || 0) })
        )
      );
    }
    fbSec.append(bars);
    pane.append(fbSec);
  }

  pane.append(
    h('button', {
      class: 'btn btn-sm',
      type: 'button',
      html: `${icon('external-link', 12)}<span>打开技能进化视图</span>`,
      on: { click: () => navigate('/evolution') }
    })
  );
  return pane;
}

async function promoteSkill(ctx, cand) {
  const ok = await confirmDialog({
    title: '把候选技能转正',
    message: `「${cand.name}」将进入正式技能库，并影响之后的生成。`,
    detail: '建议先完成固定评测，确认没有回退再转正。',
    confirmText: '转正'
  });
  if (!ok) return;
  try {
    await api.promoteSkill(cand.id, true);
    toast('已转正并生效', { tone: 'success' });
    ctx.refreshSkills();
  } catch (err) {
    toast(errorText(err, '转正失败'), { tone: 'danger' });
  }
}

/* --------------------------------------------------------------- shell */

export function renderRightPanel(container, ctx) {
  const state = store.get();
  resetPanelFor(state.currentKey);
  const mediumBench = isMediumBench(state.workMedium);
  const available = TABS.filter((item) => {
    /* 规划/状态/参考/协作都是小说引擎的数据面，非小说作品没有这些接口，页签直接不出现。 */
    if (mediumBench && item.key !== 'work' && item.key !== 'skills') return false;
    if (item.key === 'plan') return state.features.plan !== false;
    if (item.key === 'state') return state.features.state !== false;
    if (item.key === 'refs') return state.features.references !== false;
    if (item.key === 'skills') return state.features.skills !== false;
    return true;
  });
  const requested = state.ui.rightTab || 'work';
  const tab = available.some((t) => t.key === requested) ? requested : 'work';
  if (tab !== 'collab') destroyCollab();
  const scrollTop = (() => {
    const body = container.querySelector('.rp-body');
    return body ? body.scrollTop : 0;
  })();

  container.textContent = '';
  const wrap = h('div', { class: 'right-panel' });
  const head = h('div', { class: 'rp-head' });
  const tabs = h('div', { class: 'rp-tabs', role: 'tablist', 'aria-label': '工作台面板' });
  for (const item of available) {
    tabs.append(
      h('button', {
        class: 'rp-tab',
        type: 'button',
        role: 'tab',
        'aria-selected': tab === item.key ? 'true' : 'false',
        id: `rp-tab-${item.key}`,
        html: `${icon(item.icon, 13)}<span>${item.label}</span>`,
        on: {
          click: () => {
            patchUi({ rightTab: item.key });
            if (ctx.ensureTab) ctx.ensureTab(item.key);
          }
        }
      })
    );
  }
  head.append(tabs);
  head.append(
    h('button', {
      class: 'icon-btn icon-btn-sm',
      type: 'button',
      'aria-label': '关闭工作台',
      title: '关闭（Esc）',
      html: icon('x', 15),
      on: { click: () => ctx.closeRight() }
    })
  );
  wrap.append(head);

  const body = h('div', { class: 'rp-body', role: 'tabpanel', 'aria-labelledby': `rp-tab-${tab}` });
  let pane;
  if (tab === 'work') pane = workPane(ctx);
  else if (tab === 'plan') pane = planPane(ctx);
  else if (tab === 'state') pane = statePane(ctx);
  else if (tab === 'refs') pane = refsPane(ctx);
  else if (tab === 'collab') pane = collabPane(ctx);
  else pane = skillsPane(ctx);
  body.append(pane);
  wrap.append(body);
  container.append(wrap);
  body.scrollTop = scrollTop;

  return { tab };
}
