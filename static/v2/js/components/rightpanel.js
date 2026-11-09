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
import { store, patch, patchUi, patchChat } from '../core/state.js';
import { navigate, routePath } from '../core/router.js';
import {
  USAGE_MODES,
  usageLabel,
  /* v4.22.0：从 views/mediumbench.js 下沉到 constants——只为这两个函数不该整条
     mediumbench → chatfloat → markdown 链都被拖进首屏。 */
  isMediumBench,
  verdictLabel
} from '../core/constants.js';
import { toast, confirmDialog, openModal, popoverMenu, registerOverlay, unregisterOverlay } from '../core/ui.js';
import { api, errorText, IS_MOCK } from '../core/api.js';
/* v4.22.0：collab（≈38K，含 skillCatalog）与 distill 都改为按需 import——
   两者只在用户真正点开「协作」页签 / 「蒸馏风格」时才用得到，
   静态 import 会把整条链拖进首屏闭包。 */
import { createCreativeToggle, createExpressButton, createExpressSwitch } from './planbar.js';

/* distill 模块懒加载：保留原函数名，两个调用点零改动。 */
function openDistillModal(payload) {
  import('./distill.js').then((m) => m.openDistillModal(payload));
}

const TABS = [
  { key: 'work', label: '作品', icon: 'book-open' },
  { key: 'plan', label: '规划', icon: 'list' },
  { key: 'state', label: '状态', icon: 'activity' },
  { key: 'refs', label: '参考', icon: 'folder' },
  { key: 'skills', label: '技能', icon: 'sparkles' },
  { key: 'collab', label: '协作', icon: 'layers' },
  { key: 'archive', label: '档案', icon: 'history' }
];

/* v4.21.0：对话产出卡「查看本章过程」→ 自动切到档案页签的水位线（每次点击只触发一次切换） */
let _archiveSeenTs = 0;

/* 协作面板带轮询与订阅，跨 tab / 跨书必须显式销毁，否则 interval 会残留。 */
let collabInst = null;
let collabModPromise = null;

function destroyCollab() {
  if (!collabInst) return;
  try {
    collabInst.destroy();
  } catch (err) {
    console.error('[collab] destroy failed', err);
  }
  collabInst = null;
}

function collabLoadingPane() {
  return h('div', { class: 'rp-section', style: { padding: '18px 4px', textAlign: 'center' } },
    h('div', { class: 'spinner', style: { width: '18px', height: '18px', margin: '0 auto 8px', border: '2px solid var(--line-2)', borderTopColor: 'var(--accent)', borderRadius: '50%', animation: 'spin 0.8s linear infinite' } }),
    h('span', { class: 'hint-text', text: '协作面板加载中…' }));
}

function collabPane(ctx) {
  const key = store.get().currentKey || '';
  if (!collabInst || collabInst.key !== key) {
    destroyCollab();
    /* 模块在途时先给占位；到位后若用户仍停在协作页签，再真正挂载并重渲染。 */
    if (!collabModPromise) collabModPromise = import('./collab.js');
    collabModPromise.then((m) => {
      const cur = store.get().currentKey || '';
      if (store.get().ui.rightTab !== 'collab') return;
      if (collabInst && collabInst.key === cur) return;
      destroyCollab();
      collabInst = m.createCollabPanel(ctx);
      collabInst.key = cur;
      if (ctx.rerenderPanel) ctx.rerenderPanel();
    });
    return collabLoadingPane();
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
  stateQuery: '',
  /* v2.3 标题候选 / 一致性体检：null 表示还没拉过，切书时随 resetPanelFor 一起清。 */
  titleCands: null,
  titleCandLoading: false,
  titleCandError: '',
  titleCandMissing: false,
  titleCandBusy: false,
  auditLoading: false,
  auditError: ''
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
  panel.titleCands = null;
  panel.titleCandLoading = false;
  panel.titleCandError = '';
  panel.titleCandMissing = false;
  panel.titleCandBusy = false;
  panel.auditLoading = false;
  panel.auditError = '';
  destroyCollab();
}

/* 两套章节状态词表归一：写作计划（writer 落盘）用 completed/in_progress/failed/pending，
   章卡 02d-章卡.jsonl 用 done/writing/changed/planned；后端合并下发时两套都会出现，
   前端先归一到五态再渲染。未知值原样保留，由 statusLabel 兜底「待写」。 */
const CHAPTER_STATUS_MAP = {
  done: 'completed',
  completed: 'completed',
  writing: 'in_progress',
  in_progress: 'in_progress',
  error: 'failed',
  failed: 'failed',
  pending: 'pending',
  changed: 'planned',
  planned: 'planned'
};

function normChapterStatus(raw) {
  const key = String(raw || '').trim().toLowerCase();
  return CHAPTER_STATUS_MAP[key] || key || 'planned';
}

function chaptersFrom(planPayload) {
  const plan = (planPayload && planPayload.plan) || {};
  if (Array.isArray(plan.chapters) && plan.chapters.length) {
    return plan.chapters.map((c) => ({
      n: c.n || c.chapterNumber,
      title: c.title || `第${c.n || c.chapterNumber}章`,
      status: normChapterStatus(c.status),
      words: c.words || c.wordCount || c.word_count || 0
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
            status: normChapterStatus(c.status),
            words: c.words || c.wordCount || c.word_count || 0
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
    in_progress: '正在创作',
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

/* ------------------------------------------------------- v2.5.0 弹窗系统 */

function _modalBox(title, bodyNode, opts = {}) {
  /* v4.24.16 修复：右栏弹窗此前不进 core/ui 的 modalStack——按 Esc 关掉的是底下
     的 openModal 弹窗而不是当前可见的 rp-modal；且无焦点圈禁。现在登记进统一
     modalStack（Esc 由 app.js 的 closeTopOverlay 关最顶层），并做最小焦点管理。 */
  const overlay = h('div', { class: 'rp-modal-overlay', on: {
    click: (e) => { if (e.target === overlay) close(); }
  }});
  const box = h('div', { class: 'rp-modal-box', role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    h('div', { class: 'rp-modal-head' },
      h('span', { class: 'rp-modal-title', text: title }),
      h('button', { class: 'rp-modal-close', type: 'button', html: icon('x', 14),
        on: { click: () => close() } })
    ),
    h('div', { class: 'rp-modal-body' }, bodyNode)
  );
  if (opts.footer) box.append(h('div', { class: 'rp-modal-foot' }, opts.footer));
  overlay.append(box);
  document.body.append(overlay);
  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    overlay.remove();
    unregisterOverlay({ close });
  }
  registerOverlay({ close });
  const first = box.querySelector('input, textarea, select, button:not(.rp-modal-close)');
  if (first) setTimeout(() => { try { first.focus(); } catch { /* ignore */ } }, 0);
  return overlay;
}

function showQualityDetail(ctx, work) {
  const q = work.quality;
  const body = h('div', { class: 'rp-detail' });
  const kv = h('div', { class: 'kv-grid' });
  kv.append(h('div', { class: 'kv-cell' },
    h('span', { class: 'kv-k', text: '当前评分' }),
    h('span', { class: 'kv-v', text: q == null ? '未评分' : `${q} 分` })
  ));
  kv.append(h('div', { class: 'kv-cell' },
    h('span', { class: 'kv-k', text: '评分来源' }),
    h('span', { class: 'kv-v', text: q == null ? '尚无质量报告' : '章节级 quality_signals 均值' })
  ));
  kv.append(h('div', { class: 'kv-cell' },
    h('span', { class: 'kv-k', text: '已写章节' }),
    h('span', { class: 'kv-v', text: `${work.done} / ${work.total} 章` })
  ));
  kv.append(h('div', { class: 'kv-cell' },
    h('span', { class: 'kv-k', text: '总字数' }),
    h('span', { class: 'kv-v', text: fmtWords(work.total_words) })
  ));
  body.append(kv);
  /* v4.17.3：章节评分列表——每章分数一目了然，低分章可单独改写 */
  const chList = h('div', { style: 'max-height:260px;overflow:auto;border:1px solid var(--line,#ddd);border-radius:6px;padding:6px;margin-top:10px' },
    h('div', { class: 'hint-text', style: 'padding:6px', text: '章节评分加载中…' }));
  body.append(h('div', { class: 'rp-section' },
    h('div', { class: 'section-head', text: '章节评分（低分章可单独改写）' }), chList));
  (async () => {
    try {
      const key = store.get().currentKey || '';
      const d = await api.chapterScores(key);
      const pl = d.pass_line || 90;
      const scores = (d.scores || []).filter((x) => typeof x.score === 'number');
      chList.textContent = '';
      if (!scores.length) { chList.append(h('div', { class: 'hint-text', style: 'padding:6px', text: '还没有逐章评分。' })); return; }
      for (const r of scores) {
        const low = r.score < pl;
        chList.append(h('div', { style: 'display:flex;align-items:center;justify-content:space-between;padding:5px 8px;border-bottom:1px solid var(--line,#eee)' },
          h('span', { style: `font-size:13px;${low ? 'color:var(--vermilion,#C73A32)' : ''}`,
                       text: `第 ${r.ch} 章` }),
          h('span', { style: 'display:flex;gap:8px;align-items:center' },
            h('span', { style: `font-size:13px;font-weight:600;${low ? 'color:var(--vermilion,#C73A32)' : ''}`,
                         text: `${r.score} 分${low ? ' ↓' : ''}` }),
            low ? h('button', { class: 'btn btn-sm', type: 'button', text: '改写此章', on: {
              click: async () => {
                try {
                  toast(`第 ${r.ch} 章改写已启动`, { tone: 'success', duration: 3000 });
                  await api.rewriteParallel({ key: store.get().currentKey, chapters: [r.ch], workers: 1 });
                } catch (err) { toast(errorText(err, '启动失败'), { tone: 'danger' }); }
              }
            } }) : null)));
      }
    } catch { chList.textContent = ''; chList.append(h('div', { class: 'hint-text', text: '章节评分加载失败' })); }
  })();

  if (q == null) {
    body.append(h('div', { class: 'hint-text', text: '当前作品尚无质量评分。点击下方按钮可立即生成评分。评分基于五维质量信号（文笔、节奏、钩子、连续性、爽点），每章 0–100 分。' }));
    body.append(h('div', { class: 'rp-modal-foot', style: 'margin-top:16px' },
      h('button', {
        class: 'btn btn-primary btn-sm', type: 'button', text: '立即评分',
        on: { click: async () => {
          const key = store.get().currentKey || '';
          /* v4.17.1：评分过程可视化——中间弹窗内步骤清单 + 进度条轮询 */
          const stepL1 = h('div', { style: 'padding:4px 0', text: '① 一致性体检（人物/伏笔/设定）…' });
          const stepReview = h('div', { style: 'padding:4px 0', text: '② 分层采样评审…' });
          const stepBlend = h('div', { style: 'padding:4px 0', text: '③ 双轨合成总分…' });
          const bar = h('div', { class: 'progress-track', style: 'height:8px;margin:12px 0' },
            h('span', { style: 'width:2%;background:var(--accent,#C73A32);display:block;height:100%;border-radius:4px;transition:width .5s' }));
          const detail = h('div', { class: 'hint-text', text: '准备中…' });
          const resultBox = h('div', {});
          const progBody = h('div', { style: 'min-height:160px' }, stepL1, stepReview, stepBlend, bar, detail, resultBox);
          _modalBox('评分进行中', progBody);
          const setStep = (doneIdx, activeIdx, pct, dtext) => {
            [stepL1, stepReview, stepBlend].forEach((el, i) => {
              el.style.color = i < doneIdx ? '#7d9a6a' : (i === activeIdx ? 'var(--accent,#C73A32)' : 'inherit');
              if (i < doneIdx) el.textContent = el.textContent.replace(/…$/, ' ✓');
            });
            const inner = bar.querySelector('span');
            if (inner) inner.style.width = `${Math.min(100, pct)}%`;
            detail.textContent = dtext || '';
          };
          try {
            const first = await api.scoreBook(key, true);
            if (first && first.score != null && first.source === 'existing_report' && !first.started) {
              setStep(3, -1, 100, `已有评分：${first.score} 分`);
              return;
            }
            // 轮询进度（v4.24.16：切书即停——评分结果只属于发起时的那本书）
            for (let i = 0; i < 240; i++) {
              await new Promise(r => setTimeout(r, 2000));
              if (store.get().currentKey !== key) return;
              let pr;
              try { pr = await api.scoreProgress(key); } catch { continue; }
              if (!pr || !pr.running && !pr.done) continue;
              if (pr.stage === '一致性体检（L1）') setStep(0, 0, pr.pct, pr.detail || '');
              else if (pr.stage === '分层采样评审') setStep(1, 1, pr.pct, pr.detail || '');
              else if (pr.stage === '合成总分') setStep(2, 2, pr.pct, pr.detail || '');
              if (pr.done || pr.error) {
                if (pr.error) {
                  setStep(3, -1, 100, `❌ ${pr.error}`);
                  return;
                }
                setStep(3, -1, 100, '');
                resultBox.innerHTML = '';
                const q = pr.score;
                const rwBtn = h('button', { class: 'btn btn-primary btn-sm', type: 'button',
                  style: 'margin-top:12px', text: '⚡ 多岗位并行改写低分章', on: {
                  click: () => openParallelRewrite(key, ctx) } });
                resultBox.append(h('div', { style: 'text-align:center;padding:14px 0' },
                  h('div', { style: 'font-size:34px;font-weight:700;color:var(--accent,#C73A32)',
                             text: q != null ? `${q} 分` : '完成' }),
                  h('div', { class: 'hint-text', text: q != null
                    ? (q >= 90 ? '已达标（≥90）' : q >= 70 ? '接近达标（70-89）——可并行改写低分章提分' : '低于 70——建议并行改写低分章')
                    : '评分完成' }),
                  q != null && q < 90 ? rwBtn : null));
                toast(`评分完成：${q != null ? `${q} 分` : ''}`, { tone: 'success', duration: 5000 });
                ctx.refreshState?.();
                ctx.refreshLibrary?.();
                return;
              }
            }
          } catch (err) {
            detail.textContent = `评分失败：${errorText(err, '未知错误')}`;
          }
        } }
      })
    ));
  } else {
    const bar = h('div', { class: 'progress-track' },
      h('span', { style: `width:${Math.min(q, 100)}%` })
    );
    body.append(h('div', { class: 'rp-section' },
      h('div', { class: 'section-head', text: '评分分布' }), bar
    ));
    body.append(h('div', { class: 'rp-modal-foot', style: 'margin-top:12px' },
      h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: '⚡ 查看章节评分 / 改写低分章', on: {
        click: () => openParallelRewrite(store.get().currentKey, ctx) } })));
  }
  _modalBox('质量评分详情', body);
}

function showEntityDetail(ctx, entity, type) {
  if (!entity || typeof entity !== 'object') {
    _modalBox('详情', h('div', { class: 'hint-text', text: '该实体暂无详细数据。可能是状态文件尚未生成，请先创作几章后再查看。' }));
    return;
  }
  const body = h('div', { class: 'rp-detail' });
  const kv = h('div', { class: 'kv-grid' });
  const fields = {
    character: [['名称', entity.name], ['身份', entity.role || entity.identity || '—'], ['当前状态', entity.status || entity.state || '—'], ['关联章节', (entity.chapters || []).join(', ') || '—']],
    location: [['名称', entity.name], ['描述', entity.desc || entity.description || '—'], ['关联章节', (entity.chapters || []).join(', ') || '—']],
    event: [['事件', entity.name || entity.title], ['描述', entity.desc || entity.description || '—'], ['关联章节', (entity.chapters || []).join(', ') || '—']],
    foreshadow: [['线索', entity.name || entity.id], ['埋设章节', entity.planted_at || entity.plant || '—'], ['兑现章节', entity.due || entity.payoff_at || '—'], ['状态', entity.status || 'pending']],
  };
  const fieldSet = fields[type] || fields.character;
  for (const [k, v] of fieldSet) {
    kv.append(h('div', { class: 'kv-cell' },
      h('span', { class: 'kv-k', text: k }),
      h('span', { class: 'kv-v', text: String(v) })
    ));
  }
  body.append(kv);
  if (entity.summary || entity.desc) {
    body.append(h('div', { class: 'rp-section' },
      h('div', { class: 'section-head', text: '详细描述' }),
      h('div', { class: 'rp-text', text: entity.summary || entity.desc || entity.description || '' })
    ));
  }
  _modalBox(`详情 · ${entity.name || entity.id || type}`, body);
}

/* v4.1.0：时间线节点详情弹窗（右栏列表是提要，点开看全文） */
function showTimelineDetail(ctx, item) {
  const body = h('div', { class: 'rp-detail' });
  const kv = h('div', { class: 'kv-grid' });
  for (const [k, v] of [
    ['章节', `第${item.chapter || '?'}章`],
    ['故事内时间', item.time || '—'],
    ['地点', item.location || '—'],
  ]) {
    kv.append(h('div', { class: 'kv-cell' },
      h('span', { class: 'kv-k', text: k }),
      h('span', { class: 'kv-v', text: String(v) })));
  }
  body.append(kv);
  const chars = Array.isArray(item.characters) ? item.characters.filter(Boolean) : [];
  if (chars.length) {
    const row = h('div', { class: 'tl-meta', style: { marginTop: '8px' } });
    for (const c of chars) row.append(h('span', { class: 'tl-tag tl-tag-person', text: String(c) }));
    body.append(row);
  }
  body.append(h('div', { class: 'rp-section' },
    h('div', { class: 'section-head', text: '事件详情' }),
    h('div', { class: 'rp-text', text: item.event || item.text || '（无事件描述）' })));
  _modalBox(`第${item.chapter || '?'}章 · 时间线详情`, body);
}

function showConsistencyIssue(ctx, issue) {
  const body = h('div', { class: 'rp-detail' });
  const kv = h('div', { class: 'kv-grid' });
  kv.append(h('div', { class: 'kv-cell' },
    h('span', { class: 'kv-k', text: '问题类型' }),
    h('span', { class: 'kv-v', text: issue.type || issue.kind || '一致性' })
  ));
  kv.append(h('div', { class: 'kv-cell' },
    h('span', { class: 'kv-k', text: '严重程度' }),
    h('span', { class: 'kv-v', text: issue.severity || '中' })
  ));
  kv.append(h('div', { class: 'kv-cell' },
    h('span', { class: 'kv-k', text: '涉及章节' }),
    h('span', { class: 'kv-v', text: String(issue.chapter || issue.chapters || '—') })
  ));
  body.append(kv);
  body.append(h('div', { class: 'rp-section' },
    h('div', { class: 'section-head', text: '问题描述' }),
    h('div', { class: 'rp-text', text: issue.desc || issue.description || issue.message || '' })
  ));
  if (issue.suggestion || issue.fix) {
    body.append(h('div', { class: 'rp-section' },
      h('div', { class: 'section-head', text: '修复建议' }),
      h('div', { class: 'rp-text', text: issue.suggestion || issue.fix || '' })
    ));
  }
  _modalBox('一致性问题详情', body, {
    footer: h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: '一键修复',
      on: { click: async () => {
        try {
          toast('正在修复…', { tone: 'info' });
          /* v2.8.0：ctx 上没有 api（原写法直接抛 TypeError）；api 已在模块顶部导入 */
          const res = await api.fixConsistency(issue.id || issue.type || issue.desc || '', store.get().currentKey || '');
          const acts = ((res && res.result && res.result.actions) || []).join('；');
          const isRewrite = acts && acts.includes('定向重写');
          toast(acts ? (isRewrite ? `已启动修复：${acts}（完成后状态页签自动更新）` : `修复完成：${acts}`) : '修复完成',
                { tone: 'success', duration: 6000 });
          ctx.refreshState();
          document.querySelector('.rp-modal-overlay')?.remove();
        } catch (err) { toast(errorText(err, '修复失败'), { tone: 'danger' }); }
      } }
    })
  });
}

function showSkillDetail(ctx, skill, type) {
  if (!skill || typeof skill !== 'object') {
    _modalBox('技能详情', h('div', { class: 'hint-text', text: '该技能暂无详细数据。可能是技能系统尚未初始化，或该作品还没有适用的技能数据。' }));
    return;
  }
  const body = h('div', { class: 'rp-detail' });
  const kv = h('div', { class: 'kv-grid' });
  const fields = {
    candidate: [['技能名', skill.name], ['版本', skill.version || 'draft'], ['状态', skill.pending ? '待评测' : '已评测'], ['来源', skill.source || '反馈蒸馏'], ['评分差', skill.delta || '—']],
    active: [['技能名', skill.name], ['生效版本', skill.version || '—'], ['启用状态', skill.enabled ? '已启用' : '已禁用'], ['样本数', skill.samples || 0], ['平均评分', skill.avg_rating || '—']],
    feedback: [['反馈样本', skill.total || 0], ['正向', skill.positive || 0], ['负向', skill.negative || 0], ['平均评分', skill.avg_rating || '—']],
  };
  const fieldSet = fields[type] || fields.active;
  for (const [k, v] of fieldSet) {
    kv.append(h('div', { class: 'kv-cell' },
      h('span', { class: 'kv-k', text: k }),
      h('span', { class: 'kv-v', text: String(v) })
    ));
  }
  body.append(kv);
  if (skill.desc || skill.description) {
    body.append(h('div', { class: 'rp-section' },
      h('div', { class: 'section-head', text: '技能描述' }),
      h('div', { class: 'rp-text', text: skill.desc || skill.description || '' })
    ));
  }
  _modalBox(`技能详情 · ${skill.name}`, body);
}

async function exportWork(key, fmt, label, name = '') {
  if (IS_MOCK) {
    toast(`演示模式不生成文件；正式环境会导出 ${label}`, { duration: 2600 });
    return;
  }
  /* v4.32.0：桌面端走原生「存储」对话框选位置，网页端走浏览器下载 */
  try {
    toast(`正在导出 ${label}…`, { duration: 2000 });
    const r = await api.exportWork(key, fmt, { name });
    if (r && r.cancelled) return;
    const where = r && r.saved ? `已保存到 ${r.saved}` : `${label} 已开始下载`;
    toast(where, { tone: 'success', duration: 3200 });
  } catch (err) {
    toast(errorText(err, '导出失败，请稍后重试'), { tone: 'danger', duration: 4000 });
  }
}

/* ------------------------------------------------------- v2.3 标题改名/候选 */

/* 改名成功后 key 会变：先本地替换侧栏条目并 toast，再把路由切到新 key，
   侧栏选中、右栏、对话都会跟着 onRoute 重建；随后 refreshLibrary 以后端为准覆盖。 */
async function applyRename(ctx, oldKey, newTitle) {
  const res = await api.title.set(oldKey, newTitle);
  const newKey = (res && res.key) || oldKey;
  const name = (res && res.name) || newTitle;
  patch('library', {
    items: store.get().library.items.map((i) => (i.key === oldKey ? { ...i, key: newKey, name } : i))
  });
  toast(`已改名《${name}》`, { tone: 'success' });
  if (newKey !== oldKey) {
    navigate(routePath('work', { key: newKey }));
    if (ctx.refreshLibrary) ctx.refreshLibrary();
  } else if (ctx.refreshWork) {
    ctx.refreshWork();
  }
  return res;
}

function startTitleEdit(topEl, work, ctx) {
  topEl.textContent = '';
  const input = h('input', {
    class: 'input rp-title-input',
    type: 'text',
    value: work.name || '',
    'aria-label': '新书名'
  });
  let busy = false;
  const submit = async () => {
    if (busy) return;
    const name = String(input.value || '').trim();
    if (!name) {
      toast('书名不能为空', { tone: 'danger' });
      input.focus();
      return;
    }
    if (name === work.name) {
      ctx.rerenderPanel();
      return;
    }
    busy = true;
    confirmBtn.disabled = true;
    confirmBtn.textContent = '改名中…';
    try {
      await applyRename(ctx, work.key, name);
    } catch (err) {
      busy = false;
      confirmBtn.disabled = false;
      confirmBtn.textContent = '确定';
      toast(errorText(err, '改名失败'), { tone: 'danger' });
    }
  };
  const confirmBtn = h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: '确定' });
  confirmBtn.addEventListener('click', submit);
  const cancelBtn = h('button', {
    class: 'btn btn-sm',
    type: 'button',
    text: '取消',
    on: { click: () => ctx.rerenderPanel() }
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      submit();
    } else if (event.key === 'Escape') {
      ctx.rerenderPanel();
    }
  });
  topEl.append(input, confirmBtn, cancelBtn);
  input.focus();
  input.select();
}

/* 标题候选 chips：打开 work pane 时拉一次；空则「生成候选」，有则「再要几个」（各 n=3）。 */
function titleCandsSection(ctx) {
  const key = store.get().currentKey || '';
  const sec = h('div', { class: 'rp-section' });
  const countEl = h('span', { class: 'count' });
  sec.append(h('div', { class: 'rp-section-head' }, h('h3', { text: '标题候选' }), countEl));
  const body = h('div', { class: 'rp-cand-body' });
  sec.append(body);

  function paint() {
    body.textContent = '';
    if (panel.titleCandLoading) {
      countEl.textContent = '';
      body.append(
        h('div', { class: 'rp-cand-loading' },
          h('span', { class: 'spinner' }),
          h('span', { class: 'hint-text', text: '正在请模型起名，约需半分钟…' }))
      );
      return;
    }
    const items = panel.titleCands || [];
    countEl.textContent = items.length ? `${items.length} 个` : '';
    if (panel.titleCandError) {
      body.append(
        h('div', { class: panel.titleCandMissing ? 'hint-text' : 'error-text', text: panel.titleCandError })
      );
    }
    if (items.length) {
      const row = h('div', { class: 'rp-cand-row' });
      for (const it of items) {
        const tip = [it.technique ? `手法：${it.technique}` : '', it.why || ''].filter(Boolean).join(' · ');
        const chip = h('span', { class: 'chip rp-cand-chip', title: tip || it.title || '' });
        chip.append(
          h('span', { class: 'rp-cand-name', text: it.title || '' }),
          h('button', {
            class: 'rp-cand-adopt',
            type: 'button',
            text: '采用',
            on: { click: (event) => adopt(it, event.currentTarget) }
          })
        );
        row.append(chip);
      }
      body.append(row);
    }
    const foot = h('div', { class: 'rp-cand-foot' });
    foot.append(
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        html: items.length
          ? `${icon('refresh', 12)}<span>再要几个</span>`
          : `${icon('sparkles', 12)}<span>生成候选</span>`,
        disabled: panel.titleCandBusy || !key,
        on: { click: () => generate() }
      })
    );
    body.append(foot);
  }

  async function generate() {
    if (panel.titleCandLoading) return;
    panel.titleCandLoading = true;
    panel.titleCandError = '';
    panel.titleCandMissing = false;
    paint();
    try {
      const res = await api.title.generate(key, 3);
      const fresh = (res && Array.isArray(res.items) && res.items) || [];
      panel.titleCands = [...(panel.titleCands || []), ...fresh].slice(0, 12);
    } catch (err) {
      panel.titleCandError = errorText(err, '生成候选失败');
      panel.titleCandMissing = Boolean(err && err.isMissing);
    } finally {
      panel.titleCandLoading = false;
      if (store.get().currentKey === key) paint();
    }
  }

  async function adopt(it, btn) {
    if (panel.titleCandBusy) return;
    panel.titleCandBusy = true;
    btn.disabled = true;
    try {
      await applyRename(ctx, key, it.title);
    } catch (err) {
      panel.titleCandBusy = false;
      btn.disabled = false;
      toast(errorText(err, '采用失败'), { tone: 'danger' });
    }
  }

  if (!key) {
    body.append(h('div', { class: 'hint-text', text: '打开一本作品后可以查看与生成标题候选。' }));
    return sec;
  }
  if (panel.titleCands === null && !panel.titleCandLoading) {
    panel.titleCandLoading = true;
    paint();
    api
      .title.candidates(key)
      .then((res) => {
        panel.titleCands = (res && Array.isArray(res.items) && res.items) || [];
      })
      .catch((err) => {
        panel.titleCands = [];
        panel.titleCandError = errorText(err, '候选读取失败');
        panel.titleCandMissing = Boolean(err && err.isMissing);
      })
      .finally(() => {
        panel.titleCandLoading = false;
        if (store.get().currentKey === key) paint();
      });
  } else {
    paint();
  }
  return sec;
}

/* ---------------------------------------------------------------- work */

/* 剧本/诗词/散文：这里的数字单位属于各自引擎，小说的章节列表、导出与暂停/继续都不适用。 */
function mediumWorkPane(work, ctx) {
  const pane = h('div', { class: 'rp-pane' });
  const st = store.get();   // v4.6.1：实时日志带需要读全局 live；此前引用未定义的 state 变量导致整个右栏崩溃
  const unit = MEDIUM_UNITS[work.medium || 'novel'] || '';
  const pct = Number(work.progress) || 0;
  const meta = statusMeta(work.state, work.status_label);
  const summary = h('div', { class: 'rp-summary' });
  const topRow = h('div', { class: 'rp-summary-top' });
  topRow.append(
    h('div', { class: 'rp-summary-title', text: work.name }),
    /* v4.3.0：改名入口（四文体通用，复用小说的 startTitleEdit） */
    h('button', {
      class: 'icon-btn icon-btn-sm',
      type: 'button',
      'aria-label': '修改标题',
      title: '修改标题',
      html: icon('pen', 13),
      on: { click: () => startTitleEdit(topRow, work, ctx) }
    }),
    h('span', { class: 'status-chip', dataset: { tone: meta.tone }, text: meta.label })
  );
  summary.append(topRow);
  summary.append(
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
  pane.append(agentsRow(work));   // v4.24.11：绑定岗位一眼可见（非小说同款）

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
  /* v3.3.0：当前阶段（S5=写作 / C=起草 / F=定稿…），每步到哪了一眼可见 */
  if (work.stage) cells.push(['阶段', work.stage_label || work.stage, '']);
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
  const actions = h('div', { class: 'rp-actions rp-actions-pad' });
  /* v4.12.1：非小说加「打开工作台」按钮 */
  actions.append(
    h('button', {
      class: 'btn btn-primary btn-sm',
      type: 'button',
      html: `${icon('edit', 13)}<span>${MEDIUM_LABELS[work.medium || 'novel'] || ''}工作台</span>`,
      on: { click: () => navigate(`/work/${encodeURIComponent(work.key)}/bench/1`) }
    })
  );
  /* 断点恢复 / 暂停控制：v4.20.0 起与其他入口统一三态——
     running→「创作中」可点击请求暂停（api.pause）；running 且 paused→「暂停中…」disabled；
     非运行但 planning/writing/in_progress（或 paused）→「继续」（api.resume，后端已全文体统一，
     对未跑过线程的书会代理到 continue）；空闲/完成→不渲染。乐观反馈与防连点由工厂统一提供。 */
  const creativeBtn = createCreativeToggle({ work, ctx });
  if (creativeBtn) actions.append(creativeBtn);
  /* v4.24.12：极速续写入口——剩余章节一键用极速写完（普通/极速模式都可用）。 */
  const expressBtn = createExpressButton({ work, ctx, plan: st.plan });
  if (expressBtn) actions.append(expressBtn);
  /* v4.24.13：极速开关也补齐到非小说面板——此前只有小说页有，
     剧本/诗词只能用「续写」顺带切速度，没法先把速度调好再点继续。 */
  const expressSwitch = createExpressSwitch({ work, ctx, plan: st.plan });
  if (expressSwitch) actions.append(expressSwitch);
  actions.append(
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
    }),
    /* v4.32.0：导出按钮补齐到非小说面板（用户要求：在极速、蒸馏风格之后）——
       走统一 api.exportWork，桌面端弹原生保存对话框选位置 */
    h('button', {
      class: 'btn btn-sm',
      type: 'button',
      html: `${icon('download', 13)}<span>导出</span>`,
      on: {
        click: (event) => {
          popoverMenu(
            event.currentTarget,
            [
              { label: 'Word 文档（.docx）', icon: 'file-text', onSelect: () => exportWork(work.key, 'docx', 'Word', work.name) },
              { label: 'Markdown（.md）', icon: 'file', onSelect: () => exportWork(work.key, 'md', 'Markdown', work.name) },
              { label: 'EPUB 电子书', icon: 'book', onSelect: () => exportWork(work.key, 'epub', 'EPUB', work.name) },
              { label: '打印版（.html）', icon: 'grid', onSelect: () => exportWork(work.key, 'print', '打印版', work.name) }
            ],
            { align: 'start', label: '导出整本' }
          );
        }
      }
    })
  );
  pane.append(actions);

  /* v3.3.0：剧本一致性体检（人物在场/跳场/时代串代），与小说的状态 pane 同一套体验。 */
  if ((work.medium || '') === 'script') pane.append(scriptAuditSection(work, ctx));

  /* v3.3.0：标题候选泛化到全文体（拟题）。 */
  try {
    const tsec = titleCandsSection(ctx);
    if (tsec) pane.append(tsec);
  } catch { /* 标题区仅在数据可用时出现 */ }

  /* v4.3.0：实时日志带——Agent 每一步（S0 引擎/场次/逐场写作/去味）都看得到 */
  if (Array.isArray(st.live) && st.live.length) {
    const liveSec = h('div', { class: 'rp-section' });
    liveSec.append(sectionHead('实时日志', st.liveConnected === false ? '重连中' : '跟随'));
    const log = h('div', { class: 'live-log' });
    for (const line of st.live.slice(-8).reverse()) {
      log.append(h('div', { class: 'live-log-line', text: line && line.message ? line.message : String(line) }));
    }
    liveSec.append(log);
    pane.append(liveSec);
  }
  pane.append(
    h('div', { class: 'hint-text rp-note', text: '正文与写作动作在中间的工作台里，按文体分页签展开。' })
  );
  return pane;
}

/* v3.3.0：剧本一致性体检区（读 /api/script/audit，一键体检 → 问题列表 → 按设定重写入口）。 */
function scriptAuditSection(work, ctx) {
  const sec = h('div', { class: 'rp-section' });
  const state = { loading: false, report: null, error: '' };
  const bodyEl = h('div', {});

  function paint() {
    bodyEl.textContent = '';
    if (state.error) {
      bodyEl.append(h('div', { class: 'hint-text', text: state.error }));
    } else if (!state.report) {
      bodyEl.append(h('div', { class: 'hint-text', text: '检查人物在场、跳场/重复与时代串代；只读体检，不改动正文。' }));
    } else {
      const issues = state.report.issues || [];
      bodyEl.append(h('div', { class: 'hint-text',
        text: `上次体检：${(state.report.at || '').replace('T', ' ').slice(5, 16)} · 检查 ${(state.report.stats || {}).checked || 0} 场` }));
      if (!issues.length) {
        bodyEl.append(h('div', { class: 'hint-text', text: '未发现一致性问题。' }));
      } else {
        const list = h('div', { class: 'rp-issue-list' });
        for (const it of issues.slice(0, 8)) list.append(h('div', { class: 'rp-issue', text: it }));
        if (issues.length > 8) list.append(h('div', { class: 'hint-text', text: `…还有 ${issues.length - 8} 条` }));
        bodyEl.append(list);
        bodyEl.append(h('div', { class: 'hint-text', text: '违规场次可在工作台「场次」页签里按设定重写。' }));
      }
    }
  }

  const runBtn = h('button', {
    class: 'btn btn-sm', type: 'button',
    html: `${icon('shield', 12)}<span>${state.loading ? '体检中…' : '一致性体检'}</span>`,
    on: {
      click: async () => {
        if (state.loading) return;
        state.loading = true;
        runBtn.innerHTML = `${icon('shield', 12)}<span>体检中…</span>`;
        try {
          const res = await api.scriptAudit(work.key);
          state.report = res;
          state.error = '';
        } catch (err) {
          state.error = errorText(err, '体检失败');
        }
        state.loading = false;
        paint();
      }
    }
  });
  sec.append(h('div', { class: 'rp-section-head' }, h('h3', { text: '一致性体检' }),
    h('div', { class: 'rp-section-actions' }, runBtn)));
  sec.append(bodyEl);
  paint();
  // 上次结果预热
  api.scriptAuditGet(work.key).then((r) => {
    if (r && r.at && !state.report) { state.report = r; paint(); }
  }).catch(() => {});
  return sec;
}

/* v4.24.11：绑定岗位徽章行——开书引导「专属 Agent」绑定的岗位在作品面板一眼可见。
   未绑定不占位；点徽章跳协作页签（那里可增删/绑技能）。 */
function agentsRow(work) {
  const row = h('div', { class: 'rp-agents-row', hidden: true });
  const key = (work && work.key) || store.get().currentKey;   // v4.24.11：非小说 store.work 无 key，回落当前书
  if (!key) return row;
  api.bookAgents(key).then((res) => {
    const list = (res && res.agents) || [];
    if (!list.length) return;
    row.hidden = false;
    row.textContent = '';
    row.append(h('span', { class: 'rp-agents-label', text: '创作岗位' }));
    for (const a of list) {
      row.append(h('button', {
        class: 'rp-agents-chip', type: 'button',
        title: `${a.label || a.agent}${a.stage ? ' · ' + a.stage : ''}——开书引导绑定，点这里到「协作」页签可调整`,
        text: a.label || a.agent,
        on: { click: () => patchUi({ rightOpen: true, rightTab: 'collab' }) }
      }));
    }
  }).catch(() => {});
  return row;
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
    h('button', {
      class: 'icon-btn icon-btn-sm',
      type: 'button',
      'aria-label': '修改书名',
      title: '修改书名',
      html: icon('pen', 13),
      on: { click: () => startTitleEdit(top, work, ctx) }
    }),
    h('button', {
      class: 'icon-btn icon-btn-sm',
      type: 'button',
      'aria-label': '回填章名',
      title: '为仍是「第N章」的章节批量起章名（后台跑完所有章）',
      html: icon('tag', 13),
      on: {
        click: async () => {
          try {
            await api.chapterNamesBackfill(work.key);
            toast('章名回填已后台启动，完成后工作台可见新章名', { tone: 'success', duration: 3000 });
          } catch (err) {
            toast(errorText(err, '回填失败'), { tone: 'danger' });
          }
        }
      }
    }),
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
  pane.append(agentsRow(work));   // v4.24.11：绑定岗位一眼可见
  pane.append(titleCandsSection(ctx));

  const kv = h('div', { class: 'kv-grid' });
  const cells = [
    ['已写字数', fmtNum(work.total_words), '字'],
    ['平均质量', work.quality == null ? '未评分' : work.quality, work.quality == null ? '' : '分'],
    ['今日进度', `${work.done}/${work.total}`, '章'],
    ['体裁', work.genre || '小说', '']
  ];
  for (const [k, v, unit] of cells) {
    const cellEl = h(
      'div',
      { class: 'kv-cell', style: (k === '平均质量') ? 'cursor:pointer' : '',
        ...(k === '平均质量' ? { on: { click: () => showQualityDetail(ctx, work) } } : {}) },
      h('span', { class: 'kv-k', text: k }),
      h('span', { class: 'kv-v' }, `${v}`, unit ? h('span', { class: 'kv-unit', text: unit }) : null)
    );
    kv.append(cellEl);
  }
  pane.append(kv);

  const actions = h(
    'div',
    { class: 'rp-actions' },
    h('button', {
      class: 'btn btn-primary btn-sm',
      type: 'button',
      html: `${icon('edit', 13)}<span>章节工作台</span>`,
      on: { click: () => ctx.openBench(Math.min(work.done + 1, work.total || 1)) }
    }),
    /* v4.22.2：极速模式开关——已存在的书随时切换；打开后未完成章节不评分不重写。
       v4.24.13：抽成工厂与「极速续写」共用同一套状态判定（含运行中禁用）。 */
    createExpressSwitch({ work, ctx, plan: state.plan }),
    h('button', {
      class: 'btn btn-sm',
      type: 'button',
      title: '一致性巡检：扫描全书矛盾、盘点超期未收的坑，产出修复计划',
      html: `${icon('search', 13)}<span>巡检</span>`,
      on: { click: async (event) => {
        const btn = event.currentTarget;
        const key = store.get().currentKey;
        if (!key) return;
        btn.disabled = true;
        btn.textContent = '巡检中…';
        try {
          const res = await api.consistencyAuditRun(key);
          const r = (res && res.report) || {};
          const s = r.stats || {};
          toast(`巡检完成：硬矛盾 ${s.must_fix || 0} 项 · 待填坑 ${s.fill_plan || 0} 个 · 超期伏笔 ${(s.pits || {}).overdue || 0} 个`,
                { tone: 'info', duration: 6000 });
          if (ctx && ctx.refreshState) ctx.refreshState();
        } catch (err) {
          toast(errorText(err, '巡检失败'), { tone: 'danger' });
        }
        btn.disabled = false;
        btn.innerHTML = `${icon('search', 13)}<span>巡检</span>`;
      } }
    }),
    h('button', {
      class: 'btn btn-sm',
      type: 'button',
      html: `${icon('film', 13)}<span>转短剧</span>`,
      title: '把已完成的章节一键改编成 AI 短剧分集剧本并自动分镜',
      on: {
        click: async () => {
          try {
            const res = await api.novelToDrama({ key: work.key, form: 'short_drama' });
            const newKey = res.key_hint || '';
            const dramaName = res.name_hint || '改编短剧';
            let pollInterval = 0;
            /* v4.32.3：用户实拍「点查看剧本没有任何反应」。三个叠加原因：
               ① 点击只 navigate，弹窗从不关闭，遮罩把后面的界面全挡住；
               ② 按钮要等 6 秒轮询成功才解禁，期间点了等于没点；
               ③ 新项目还没进作品库缓存，路由渲染成「作品不存在」。
               现在：点击立刻关弹窗 → 左侧插入全新剧本任务会话 → 刷新作品库 → 切路由。 */
            const openDramaSession = () => {
              if (!newKey) return;
              clearInterval(pollInterval);
              dramaModal.close();
              if (ctx && typeof ctx.addPendingWork === 'function') {
                ctx.addPendingWork({ key: newKey, name: dramaName, medium: 'script' });
              }
              if (ctx && ctx.refreshLibrary) ctx.refreshLibrary().catch(() => {});
              navigate(routePath('work', { key: newKey }));
            };
            const statusBtn = h('button', {
              class: 'btn btn-primary btn-sm',
              type: 'button',
              text: '查看剧本（进行中…）',
              on: { click: openDramaSession }
            });
            const body = h('div', { class: 'rp-drama-modal-body' },
              h('div', { class: 'rp-drama-modal-line' },
                h('span', { class: 'rp-drama-modal-ico' }, '🎬'),
                h('span', {}, `《${work.name || work.key}》 正在改编为《${dramaName}》`),
              ),
              h('p', { class: 'rp-drama-modal-note' },
                '剧本与分镜在后台逐集生成，进度实时可见。点击「查看剧本」会在左侧新建一个剧本任务会话，可直接查看场次与分镜；也可以先关闭，稍后从作品库进入。'),
            );
            const closeBtn = h('button', {
              class: 'btn btn-sm',
              type: 'button',
              text: '关闭'
            });
            const dramaModal = openModal({
              title: '小说 → AI 短剧',
              subtitle: '改编任务已启动',
              body,
              size: 'sm',
              footer: h('div', {}, statusBtn, closeBtn),
              onClose: () => clearInterval(pollInterval)
            });
            closeBtn.addEventListener('click', () => dramaModal.close());
            /* 轮询：项目落盘后更新文案并同步左侧列表；running=false 视为完成。
               404/异常只表示尚未落盘，下一轮再试（按钮始终可点，不再禁用）。 */
            pollInterval = setInterval(async () => {
              try {
                const st = await api.scriptStatus(newKey);
                statusBtn.textContent = st.running ? '查看剧本（创作中…）' : '查看剧本（已完成 ✓）';
                if (ctx && ctx.refreshLibrary) ctx.refreshLibrary().catch(() => {});
                if (!st.running) clearInterval(pollInterval);
              } catch {
                /* 尚未落盘，下一轮再试 */
              }
            }, 3000);
            setTimeout(() => clearInterval(pollInterval), 300000); // 5 min 停止轮询
            toast(res.msg || '改编已后台启动', { tone: 'success', duration: 3000 });
          } catch (err) {
            toast(errorText(err, '改编失败'), { tone: 'danger' });
          }
        }
      }
    }),
    /* v4.20.0：统一三态创作控制——running→「创作中」（竖杠闪烁、点击请求暂停、乐观反馈）；
       paused / 线程中断→「继续」（点击恢复）；空闲/完成→隐藏。防连点由工厂统一处理。 */
    createCreativeToggle({ work, ctx }),
    /* v4.24.12：极速续写入口——剩余章节一键用极速写完（普通/极速模式都可用）。 */
    createExpressButton({ work, ctx, plan: state.plan }),
    h('button', {
      class: 'btn btn-sm',
      type: 'button',
      html: `${icon('download', 13)}<span>导出</span>`,
      on: {
        click: (event) => {
          popoverMenu(
            event.currentTarget,
            [
              { label: 'Word 文档（.docx）', icon: 'file-text', onSelect: () => exportWork(work.key, 'docx', 'Word', work.name) },
              { label: 'Markdown（.md）', icon: 'file', onSelect: () => exportWork(work.key, 'md', 'Markdown', work.name) },
              { label: 'EPUB 电子书', icon: 'book', onSelect: () => exportWork(work.key, 'epub', 'EPUB', work.name) },
              { label: '打印版（.html）', icon: 'grid', onSelect: () => exportWork(work.key, 'print', '打印版', work.name) }
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
      listEl.append(h('div', { class: 'empty-state' }, h('p', { text: '没有匹配的章节' })));
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
    /* v4.24.0：极速模式用强调色标出——产出是初稿，未跑评分/去味/审校 */
    h('span', {
      class: plan.express ? 'chip chip-accent' : 'chip chip-muted',
      text: plan.express ? '⚡ 极速模式 · 初稿' : (plan.mode || '滚动规划')
    }),
    h('span', { class: 'rp-spring' }),
    /* v4.12.13：全部文体都有重规划（非小说走各自引擎的规划重跑） */
    h('button', {
      class: 'btn btn-sm',
      type: 'button',
      html: `${icon('refresh', 13)}<span>重规划</span>`,
      on: { click: () => openReplanDialog(ctx) }
    })
  );
  pane.append(head);

  if (plan.简介) pane.append(h('div', { class: 'plan-digest', text: plan.简介 }));
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
    if (vol.goal) body.append(h('div', { class: 'hint-text', text: vol.goal }));
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
        const st = card ? normChapterStatus(card.status) : 'planned';
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
    proposalSection.append(h('div', { class: 'hint-text', text: '还没有提案。需要调整结构时，点上方「重规划」。v2.5.0 默认双岗位同时规划，会生成多视角对比提案供你选择。' }));
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
  const state = store.get();
  const medium = state.workMedium || 'novel';
  const isNovel = medium === 'novel' || !medium;
  const key = state.currentKey;

  /* v4.12.13：全部文体都有重规划——非小说显示 Agent 多选 + 重跑规划 */
  const agentSection = h('div', { class: 'field' },
    h('span', { class: 'field-label', text: '选择规划 Agent（可多选）' }));
  const agentChips = h('div', { class: 'chip-row', style: { flexWrap: 'wrap', gap: '4px' } });
  const selectedAgents = new Set();
  api.agents().then((res) => {
    const agents = (res && res.agents) || [];
    for (const a of agents) {
      const chip = h('button', {
        class: 'wiz-chip', type: 'button', 'aria-pressed': 'false',
        on: { click: () => {
          if (selectedAgents.has(a.agent)) { selectedAgents.delete(a.agent); chip.setAttribute('aria-pressed','false'); }
          else { selectedAgents.add(a.agent); chip.setAttribute('aria-pressed','true'); }
        }}
      }, h('span', { text: a.label || a.agent }));
      agentChips.append(chip);
    }
  }).catch(() => {});
  agentSection.append(agentChips);

  const instruction = h('textarea', {
    class: 'textarea', rows: 4,
    placeholder: '例如：调整节奏/换一个视角/加强冲突/换一种文体风格…'
  });

  let body, footer;
  if (isNovel) {
    const scope = h('select', { class: 'select' },
      h('option', { value: 'window', text: '接下来 10 章' }),
      h('option', { value: 'volume', text: '当前卷' }),
      h('option', { value: 'arc', text: '当前弧' }),
      h('option', { value: 'book', text: '全书' }));
    const mode = h('select', { class: 'select' },
      h('option', { value: 'dual', text: '双岗位同时规划（多视角对比）', selected: true }),
      h('option', { value: 'single', text: '单个岗位' }));
    body = h('div', { class: 'feedback-form' },
      h('div', { class: 'field' }, h('span', { class: 'field-label', text: '出提案的岗位' }), mode),
      h('div', { class: 'field' }, h('span', { class: 'field-label', text: '调整范围' }), scope),
      h('div', { class: 'field' }, h('span', { class: 'field-label', text: '调整要求' }), instruction));
    footer = h('div', { class: 'rp-form-foot' },
      h('button', { class: 'btn', type: 'button', text: '取消', on: { click: () => modal.close() } }),
      h('button', { class: 'btn btn-primary', type: 'button', text: '生成提案', on: {
        click: async (event) => {
          if (!instruction.value.trim()) { toast('请写清楚要调整什么', { tone: 'danger' }); return; }
          event.currentTarget.disabled = true;
          try {
            const multi = mode.value === 'dual';
            await api.planMulti({ key, scope: scope.value, roles: multi ? ['planner','editor'] : undefined,
                                  instruction: instruction.value });
            toast('已提交，提案稍后可在下方查看', { tone: 'success' });
            modal.close();
          } catch (err) { toast(errorText(err, '提交失败'), { tone: 'danger' }); event.currentTarget.disabled = false; }
        }
      } }));
  } else {
    body = h('div', { class: 'feedback-form' },
      agentSection,
      h('div', { class: 'field' }, h('span', { class: 'field-label', text: '调整要求' }), instruction));
    footer = h('div', { class: 'rp-form-foot' },
      h('button', { class: 'btn', type: 'button', text: '取消', on: { click: () => modal.close() } }),
      h('button', { class: 'btn btn-primary', type: 'button', text: '重新规划', on: {
        click: async (event) => {
          event.currentTarget.disabled = true;
          try {
            await api.regenPlan({ key, agents: Array.from(selectedAgents), instruction: instruction.value });
            toast('已启动重新规划，完成后工作台可见新规划', { tone: 'success', duration: 4000 });
            modal.close();
            if (ctx.refreshPlan) ctx.refreshPlan();
          } catch (err) { toast(errorText(err, '重规划失败'), { tone: 'danger' }); event.currentTarget.disabled = false; }
        }
      } }));
  }
  const modal = openModal({ title: isNovel ? '重规划' : `重规划（${medium}）`, body, footer, size: 'md' });
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


/* v2.5.0: 可点击的实体分组——每个实体行可点击展开详情弹窗 */
function entityGroupClickable(title, iconName, items, renderRight, ctx, groupType) {
  const typeMap = { '人物': 'character', '地点': 'location', '事件': 'event', '线索 / 伏笔': 'foreshadow', '物件': 'location' };
  const entType = typeMap[groupType] || 'character';
  const wrap = h('div', { class: 'rp-section' });
  wrap.append(sectionHead(title, String(items.length)));
  const list = h('div', { class: 'entity-group' });
  for (const item of items) {
    list.append(
      h(
        'div',
        { class: 'entity-row', style: 'cursor:pointer',
          on: { click: () => showEntityDetail(ctx, item, entType) } },
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

/* v2.3 一致性体检入口：后端把结果落在 state.last_audit {at, scope, issues[str]}，
   这里展示上次记录并提供「一致性体检」按钮；完成后走既有 refreshState 刷新本 pane。 */
function auditSection(ctx, payload) {
  const key = store.get().currentKey || '';
  const sec = h('div', { class: 'rp-section' });
  const audit = (payload.state && payload.state.last_audit) || null;
  const runBtn = h('button', {
    class: 'btn btn-sm',
    type: 'button',
    html: `${icon('shield', 12)}<span>${panel.auditLoading ? '体检中…' : '一致性体检'}</span>`,
    disabled: panel.auditLoading || !key,
    on: {
      click: async () => {
        if (panel.auditLoading) return;
        panel.auditLoading = true;
        panel.auditError = '';
        ctx.rerenderPanel();
        try {
          await api.audit(key, 'book');
          toast('体检完成', { tone: 'success' });
        } catch (err) {
          panel.auditError = errorText(err, '体检失败');
          toast(panel.auditError, { tone: 'danger' });
        } finally {
          panel.auditLoading = false;
          if (ctx.refreshState) {
            try {
              await ctx.refreshState();
            } catch {
              /* 刷新失败时下方 pane 会走既有兜底 */
            }
          }
          ctx.rerenderPanel();
        }
      }
    }
  });
  /* v4.15.11：全部一键修复——确定性修复批量执行，改稿类合并为一次 LLM 定向重写（后台） */
  const fixAllBtn = h('button', {
    class: 'btn btn-sm btn-accent',
    type: 'button',
    html: `${icon('wand', 12)}<span>全部一键修复</span>`,
    disabled: !key,
    on: {
      click: async () => {
        fixAllBtn.disabled = true;
        toast('正在批量修复…', { tone: 'info' });
        try {
          const res = await api.fixConsistencyAll(key);
          toast((res && res.msg) || '批量修复已执行', { tone: 'success', duration: 6000 });
          if (ctx.refreshState) {
            try { await ctx.refreshState(); } catch { /* ignore */ }
          }
          ctx.rerenderPanel();
        } catch (err) {
          toast(errorText(err, '批量修复失败'), { tone: 'danger' });
        } finally {
          fixAllBtn.disabled = false;
        }
      }
    }
  });
  sec.append(
    h('div', { class: 'rp-section-head' }, h('h3', { text: '一致性体检' }),
      h('div', { class: 'rp-section-actions' }, runBtn, fixAllBtn))
  );
  if (panel.auditError) sec.append(h('div', { class: 'error-text', text: panel.auditError }));
  if (!audit) {
    sec.append(
      h('div', {
        class: 'hint-text',
        text: panel.auditLoading
          ? '体检进行中，稍后即可查看人物、伏笔与设定的冲突。'
          : '还没有体检记录：点「一致性体检」，扫描全书人物、伏笔与设定冲突。'
      })
    );
    return sec;
  }
  sec.append(
    h(
      'div',
      { class: 'rp-audit-meta' },
      h('span', { text: `上次体检：${audit.at ? relTime(audit.at) : '刚刚'}` }),
      h('span', { class: 'chip chip-muted', text: audit.scope === 'book' ? '全书' : audit.scope || '—' })
    )
  );
  const issues = Array.isArray(audit.issues) ? audit.issues : [];
  if (!issues.length) {
    sec.append(h('div', { class: 'hint-text', text: '未发现一致性问题。' }));
  } else {
    for (const text of issues.slice(0, 8)) {
      sec.append(
        h(
          'div',
          { class: 'issue-row', style: 'cursor:pointer',
            on: { click: () => showConsistencyIssue(ctx, { type: '一致性', desc: String(text), severity: '中' }) } },
          h('span', { class: 'issue-dot' }),
          h('span', { class: 'issue-text', text: String(text) })
        )
      );
    }
  }
  return sec;
}

/* v4.17.2：多岗位并行改写弹窗——低分章清单 + 岗位数选择 + 启动与进度 */
async function openParallelRewrite(key, ctx) {
  try {
    const prog = await api.rewriteParallelProgress(key);
    if (prog.rw_running) { toast('该书已有改写任务在跑，请稍候', { tone: 'warn' }); return; }
  } catch { /* 忽略进度查询失败 */ }
  const numInput = h('input', { class: 'input', type: 'number', min: '1', max: '8', value: '3',
                                'aria-label': '并行岗位数' });
  const listWrap = h('div', { style: 'max-height:220px;overflow:auto;border:1px solid var(--line,#ddd);border-radius:6px;padding:8px' });
  /* v4.17.5：达标线自选（70/80/90/自定义）——选完未达标章即时刷出，改写目标联动 */
  let threshold = Number(store.get().work?.pass_line || 90) || 90;
  let allScores = [];
  let chapters = [];
  const paintList = () => {
    listWrap.textContent = '';
    chapters = allScores.filter((x) => typeof x.score === 'number' && x.score < threshold);
    if (!chapters.length) {
      listWrap.append(h('div', { class: 'hint-text',
        text: allScores.length ? `没有低于 ${threshold} 分的章节（可调低达标线）。` : '还没有逐章评分。' }));
      return;
    }
    for (const r of chapters) {
      listWrap.append(h('label', { style: 'display:flex;gap:8px;align-items:center;padding:5px;cursor:pointer' },
        h('input', { type: 'checkbox', checked: true, 'data-ch': r.ch }),
        h('span', { style: 'font-size:13px', text: `第 ${r.ch} 章 · ${r.score} 分` })));
    }
  };
  const paintFrom = async () => {
    try {
      const d = await api.chapterScores(key);
      allScores = (d.scores || []).filter((x) => typeof x.score === 'number');
      paintList();
    } catch { paintList(); }
  };
  await paintFrom();
  const thresholdInput = h('input', { class: 'input', type: 'number', min: '30', max: '100',
    value: String(threshold), style: 'width:76px', 'aria-label': '自定义达标线' });
  const chipWrap = h('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px' });
  const paintChips = () => {
    chipWrap.textContent = '';
    for (const n of [70, 80, 90]) {
      chipWrap.append(h('button', { class: 'btn btn-sm' + (threshold === n ? ' btn-accent' : ''),
        type: 'button', text: `${n} 分`,
        on: { click: () => { threshold = n; thresholdInput.value = String(n); paintChips(); paintList(); } } }));
    }
  };
  paintChips();
  thresholdInput.addEventListener('input', () => {
    const v = Number(thresholdInput.value);
    if (v >= 30 && v <= 100) { threshold = v; paintChips(); paintList(); }
  });
  const body = h('div', {},
    h('p', { class: 'hint-text', style: 'margin-bottom:8px',
             text: '先选达标线，未达标章节自动列出（默认全选）。审稿问题清单一次性下发给改写岗位；每章只改 1 轮；改写分数必须高于原分，否则自动回滚原稿（质量只进不退）。' }),
    h('div', { class: 'field' },
      h('span', { class: 'field-label', text: '达标线（满分 100）' }),
      h('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap' }, chipWrap, thresholdInput)),
    listWrap,
    h('div', { class: 'field', style: 'margin-top:10px' },
      h('span', { class: 'field-label', text: '并行岗位数（1-8）' }), numInput));
  let modal;   // v4.17.4：此前 footer 里引用未定义的 modal → 取消/启动点击即 ReferenceError
  const footer = h('div', { class: 'set-actions' },
    h('button', { class: 'btn', type: 'button', text: '取消', on: { click: (ev) => {
      try { modal.close(); } catch {}
      // v4.17.5：精确移除自身所在 overlay（querySelector 取第一个可能移错层）
      const self = ev.currentTarget ? ev.currentTarget.closest('.rp-modal-overlay') : null;
      (self || ev.target.closest('.rp-modal-overlay'))?.remove();
    } } }),
    h('button', { class: 'btn btn-primary', type: 'button', text: '⚡ 开始并行改写', on: {
      click: async () => {
        const picked = [...listWrap.querySelectorAll('input:checked')].map((i) => Number(i.dataset.ch));
        if (!picked.length) { toast('请至少勾选一章', { tone: 'danger' }); return; }
        const workers = Math.max(1, Math.min(8, Number(numInput.value) || 3));
        modal.close();
        /* 写进对话框：用户指令 + Agent 确认（进度体验闭环） */
        try {
          store.set((st) => {
            const msgs = [...((st.chat && st.chat.messages) || [])];
            msgs.push({ id: `rw_${Date.now()}`, role: 'user',
                        content: `⚡ 多岗位并行改写：${picked.length} 个低分章（第 ${picked.slice(0, 6).join('、')}${picked.length > 6 ? '…' : ''} 章），${workers} 个岗位同时改` });
            msgs.push({ id: `rw_a_${Date.now()}`, role: 'assistant',
                        content: `▶ 已启动并行改写：审稿问题清单已下发给 ${workers} 个改写岗位，每章只改 1 轮；改写分数必须高于原分，未提分的章节自动回滚原稿。完成后我会逐章汇报提分情况。` });
            return { ...st, chat: { ...(st.chat || {}), messages: msgs } };
          });
        } catch { /* 对话注入失败不影响改写 */ }
        try {
          await api.rewriteParallel({ key, chapters: picked, workers, target: threshold });
          toast('并行改写已启动', { tone: 'success', duration: 4000 });
          /* 进度轮询 → 完成后逐章汇报进对话框
             v4.24.16 修复：轮询此前无总时长上限、切书/关弹窗也不停（永久 4s 轮询）。
             加 30 分钟兜底 + 书已切换即退出（完成汇报只属于发起时的那本书）。 */
          const startedKey = key;
          let pollTicks = 0;
          const poll = setInterval(async () => {
            pollTicks += 1;
            if (store.get().currentKey !== startedKey || pollTicks > 450) {   // 450×4s = 30 分钟
              clearInterval(poll);
              return;
            }
            try {
              const pr = await api.rewriteParallelProgress(key);
              if (pr.rw_running === false && (pr.done || pr.error)) {
                clearInterval(poll);
                const results = pr.results || [];
                const lines = results.map((r) => `第${r.ch}章 ${r.before ?? '?'}→${r.after ?? '?'}${r.kept ? ' ↑' : '（回滚）'}`);
                store.set((st) => {
                  const msgs = [...((st.chat && st.chat.messages) || [])];
                  msgs.push({ id: `rw_done_${Date.now()}`, role: 'assistant',
                              content: `⚡ 并行改写完成：${results.filter((r) => r.kept).length}/${results.length} 章提分。\n${lines.join('\n')}\n\n未提分的章节已回滚原稿（质量只进不退）。可再次评分查看总分变化。` });
                  return { ...st, chat: { ...(st.chat || {}), messages: msgs } };
                });
                if (ctx && ctx.refreshState) try { ctx.refreshState(); } catch {}
                if (ctx && ctx.refreshLibrary) try { ctx.refreshLibrary(); } catch {}
              }
            } catch { /* 轮询失败下次再试 */ }
          }, 4000);
        } catch (err) {
          toast(errorText(err, '启动失败'), { tone: 'danger' });
        }
      }
    } })
  );
  modal = _modalBox('多岗位并行改写', body, { footer });
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
  pane.append(auditSection(ctx, payload));

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
          { class: 'issue-row', style: 'cursor:pointer', dataset: { severity: overdue ? 'high' : 'medium' },
            on: { click: () => showEntityDetail(ctx, { name: f.text || f.note || '', desc: `伏笔「${f.text || f.note || ''}」计划在第 ${f.due} 章回收${overdue ? '（已逾期）' : ''}。`, status: overdue ? '逾期' : '待回收' }, 'foreshadowing') } },
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
      const text = typeof issue === 'string' ? issue : (issue.text || '');
      sec.append(
        h(
          'div',
          { class: 'issue-row', style: 'cursor:pointer', dataset: { severity: (typeof issue === 'object' && issue && issue.severity) || 'medium' },
            on: { click: () => showConsistencyIssue(ctx, typeof issue === 'object' && issue ? issue : { type: '一致性', desc: String(text), severity: 'medium', ref: '' }) } },
          h('span', { class: 'issue-dot' }),
          h('span', { class: 'issue-text', text }),
          (typeof issue === 'object' && issue && issue.ref) ? h('span', { class: 'issue-ref', text: issue.ref }) : null
        )
      );
    }
    pane.append(sec);
  }

  const entities = [
    ['人物', 'users', payload.characters || [], (x) =>
      h('span', { class: 'entity-sub', text: x.status || '' })],
    ['地点', 'pin', payload.locations || []],
    ['物件', 'package', payload.items || []],
    ['线索 / 伏笔', 'flag', payload.threads || [], (x) =>
      x.progress != null ? h('span', { class: 'progress-track entity-progress', html: `<span style="width:${x.progress}%"></span>` }) : null]
  ];
  for (const [title, ic, items, right] of entities) {
    const filtered = filter(items);
    if (filtered.length) pane.append(entityGroupClickable(title, ic, filtered.slice(0, 10), right, ctx, title));
  }

  /* v4.7.0（借鉴 reveric 手法账本）：最近 5 章的钩子/情绪/紧张度轨迹，防模式收敛 */
  if (Array.isArray(payload.ledger) && payload.ledger.recent && payload.ledger.recent.length) {
    const sec = h('div', { class: 'rp-section' });
    sec.append(sectionHead('手法账本', `最近 ${payload.ledger.recent.length} 章`));
    const tl = h('div', { class: 'timeline timeline-full' });
    for (const r of payload.ledger.recent) {
      tl.append(h('div', { class: 'timeline-item' },
        h('span', { class: 'tl-ch', text: `第${r.n || '?'}章` }),
        h('div', { class: 'tl-body' },
          h('div', { class: 'tl-main', text: r.hook_type ? `钩子：${r.hook_type}` : (r.hook || '（未提取）') }),
          h('div', { class: 'tl-meta' },
            r.emotion ? h('span', { class: 'tl-tag', text: r.emotion }) : null,
            r.tension != null ? h('span', { class: 'tl-tag', text: `紧张 ${r.tension}/10` }) : null))));
    }
    if (payload.ledger.since_peak != null) {
      sec.append(h('div', { class: 'hint-text', style: { marginTop: '6px' },
        text: `距上次高峰（紧张度≥8）已 ${payload.ledger.since_peak} 章` }));
    }
    sec.append(tl);
    pane.append(sec);
  }

  /* v2.8.0：时间线全节点渲染（章号徽标 + 故事内时间 + 事件全文 + 地点/人物标签）。
     后端字段是 {chapter,time,event,location,characters}（bot/state.py _norm_timeline_item），
     旧代码读 item.at/item.text 全是 undefined，所以界面上只剩几个空圆点。 */
  if (Array.isArray(payload.timeline) && payload.timeline.length) {
    const sec = h('div', { class: 'rp-section' });
    sec.append(sectionHead('时间线', `${payload.timeline.length} 个节点`));
    const tl = h('div', { class: 'timeline timeline-full' });
    for (const item of payload.timeline) {
      const tags = [];
      if (item.location) tags.push(h('span', { class: 'tl-tag', text: item.location }));
      for (const c of (Array.isArray(item.characters) ? item.characters : []).slice(0, 4)) {
        tags.push(h('span', { class: 'tl-tag tl-tag-person', text: String(c) }));
      }
      tl.append(
        h('div', { class: 'timeline-item', dataset: { current: item.current ? 'true' : 'false' },
          style: 'cursor:pointer',
          on: { click: () => showTimelineDetail(ctx, item) } },
          h('span', { class: 'tl-ch', text: `第${item.chapter || '?'}章` }),
          h('div', { class: 'tl-body' },
            h('div', { class: 'tl-main', text: item.event || item.text || '（无事件描述）' }),
            h('div', { class: 'tl-meta' },
              item.time ? h('span', { class: 'tl-tag tl-tag-time', text: item.time }) : null,
              ...tags
            )
          )
        )
      );
    }
    sec.append(tl);
    pane.append(sec);
  } else if (payload.state && (payload.state.metrics || payload.state.summary)) {
    /* 有状态库但还没有时间线节点：给出明确空态而不是无声消失 */
    const sec = h('div', { class: 'rp-section' });
    sec.append(sectionHead('时间线', '0 个节点'));
    sec.append(h('div', { class: 'hint-text', text: '还没有时间线节点。每写完一章，这里会按章节记录发生的事件、地点与出场人物。' }));
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
  const key = store.get().currentKey;   // v4.24.16：切书竞态防护——旧书的检索结果不得画进新书
  try {
    const res = await api.searchRefs(key, q, 6);
    if (store.get().currentKey !== key) return;
    panel.refHits = (res && res.items) || [];
  } catch (err) {
    if (store.get().currentKey !== key) return;
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
    const input = h('input', { type: 'file', hidden: true, accept: '.txt,.md,.docx,.pdf,.epub,.rtf,.html,.json,.csv' });
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
      const enabled = it.enabled !== false; /* 默认启用 */
      chips.append(h('span', {
        class: `chip chip-toggle${enabled ? ' chip-active' : ' chip-inactive'}`,
        text: it.title || it.name,
        title: it.genre || (enabled ? '已启用·点击禁用' : '已禁用·点击启用'),
        on: { click: async () => {
          try {
            await api.toggleSkill({ name: it.name || it.title, enabled: !enabled });
            toast(!enabled ? `已启用「${it.title || it.name}」` : `已禁用「${it.title || it.name}」`, { tone: 'success' });
            load(); /* 重新加载 */
          } catch (err) { toast(errorText(err, '切换失败'), { tone: 'danger' }); }
        } }
      }));
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
  const skillCells = [
    ['候选技能', (payload.candidates || []).filter((c) => c.pending).length, '个', 'candidate', { candidates: payload.candidates || [] }],
    ['生效版本', stats.active || (payload.history || []).filter((v) => v.active).length, '个', 'active', { history: payload.history || [] }],
    ['反馈样本', feedback.total || 0, '条', 'feedback', feedback],
    ['平均评分', feedback.avg_rating || '—', '分', 'feedback', feedback]
  ];
  skillCells.forEach(([k, v, unit, detailType, detailData]) => {
    kv.append(
      h(
        'div',
        { class: 'kv-cell', style: 'cursor:pointer',
          on: { click: () => showSkillDetail(ctx, detailData, detailType) } },
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
            /* v4.24.16 修复：on 传成了裸函数——dom.js 只认 {click: fn}，此前按钮没有任何
               click 监听，点「驳回」毫无反应（同页 evolution.js 的同功能按钮是正确写法） */
            on: { click: async () => {
              try {
                await api.rejectSkill(cand.id, '');
                toast('已驳回');
                ctx.refreshSkills();
              } catch (err) {
                toast(errorText(err, '驳回失败'), { tone: 'danger' });
              }
            } }
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
  const histList = h('div', { class: 'stack-tight' });
  for (const v of payload.history || []) {
    const row = h(
      'div',
      { class: 'version-row' },
      h('span', { class: 'dot', dataset: v.active ? { tone: 'success' } : null }),
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

/* v4.21.0：本章创作档案——该章的全部创作事件时间线（岗位/思考/产出，新→旧）。
   数据源 /api/chapter-steps（bot/steps.chapter_events：内存 ring + 历史 JSONL 去重合并）。 */
function archivePane(ctx) {
  const state = store.get();
  const key = state.currentKey;
  const work = state.work || {};
  const total = Number(work.total || 0);
  const requested = (state.ui.chapterArchive && state.ui.chapterArchive.key === key)
    ? state.ui.chapterArchive : {};
  let chapter = Number(requested.chapter || work.current_chapter || 1) || 1;
  if (total && chapter > total) chapter = total;
  if (chapter < 1) chapter = 1;

  const pane = h('div', { class: 'arch-pane' });
  const sel = h('select', { class: 'input arch-select', 'aria-label': '选择章节' });
  const list = h('div', { class: 'arch-list' });
  const head = h('div', { class: 'arch-head' },
    h('span', { class: 'arch-head-label', text: '章节' }), sel,
    h('button', { class: 'btn btn-sm', type: 'button', text: '刷新', on: { click: () => load() } })
  );
  pane.append(head, list);

  function paint(items) {
    list.textContent = '';
    if (items == null) {
      list.append(h('div', { class: 'hint-text', style: 'padding:12px 4px', text: '档案加载失败，请重试。' }));
      return;
    }
    if (!items.length) {
      list.append(h('div', { class: 'hint-text', style: 'padding:12px 4px',
        text: '这一章还没有记录。创作过程中每个岗位的动作（规划/写作/审校/去味）都会逐步沉淀到这里。' }));
      return;
    }
    for (const e of items) list.append(archRow(e));
  }

  function archRow(e) {
    const wrap = h('div', { class: 'arch-entry' });
    const mark = e.phase === 'start' ? '▶' : (e.phase === 'warn' ? '⚠' : '✓');
    const msTxt = e.ms ? ` · ${(e.ms / 1000).toFixed(1)}s` : '';
    const row = h('div', { class: 'arch-row' },
      h('span', { class: 'arch-time mono', text: String(e.time || '').slice(0, 8) || '—' }),
      h('span', { class: 'arch-badge', dataset: { agent: e.agent || 'system' }, text: e.agent_label || e.agent || '系统' }),
      h('span', { class: 'arch-title', text: `${mark} ${e.title || ''}${msTxt}` })
    );
    const det = [e.thinking, e.output].filter(Boolean).join('\n');
    if (det) {
      const pre = h('pre', { class: 'arch-detail', style: 'display:none', text: det });
      row.classList.add('arch-expandable');
      row.title = '点击展开/收起详情';
      row.addEventListener('click', () => {
        pre.style.display = pre.style.display === 'none' ? '' : 'none';
      });
      wrap.append(row, pre);
    } else {
      wrap.append(row);
    }
    return wrap;
  }

  async function load() {
    list.textContent = '';
    list.append(h('div', { class: 'set-loading' }, h('span', { class: 'skeleton skeleton-line w-50' })));
    try {
      const res = await fetch(`/api/chapter-steps?key=${encodeURIComponent(key || '')}&chapter=${chapter}`,
        { credentials: 'include' });
      const data = await res.json();
      paint(data && data.ok ? (data.items || []) : null);
    } catch {
      paint(null);
    }
  }

  /* 章号下拉：有总量就列全章；否则只列当前章 */
  sel.append(h('option', { value: String(chapter), text: `第 ${chapter} 章`, selected: true }));
  if (total > 1) {
    sel.textContent = '';
    for (let n = 1; n <= total; n++) {
      sel.append(h('option', { value: String(n), selected: n === chapter, text: `第 ${n} 章` }));
    }
  }
  sel.addEventListener('change', () => {
    chapter = Number(sel.value) || 1;
    load();
  });
  load();
  return pane;
}

export function renderRightPanel(container, ctx) {
  const state = store.get();
  resetPanelFor(state.currentKey);
  const mediumBench = isMediumBench(state.workMedium);
  const available = TABS.filter((item) => {
    /* v4.6.2：规划/状态/参考/协作全部文体都有（数据源按文体分流到各自引擎） */
    if (mediumBench && !['work', 'skills', 'refs', 'collab', 'plan', 'state', 'archive'].includes(item.key)) return false;
    if (item.key === 'plan') return state.features.plan !== false;
    if (item.key === 'state') return state.features.state !== false;
    if (item.key === 'refs') return state.features.references !== false;
    if (item.key === 'skills') return state.features.skills !== false;
    return true;
  });
  const requested = state.ui.rightTab || 'work';
  let tab = available.some((t) => t.key === requested) ? requested : 'work';
  /* v4.21.0：对话产出卡点了「查看本章过程」→ 本次渲染直接落到档案页签（ts 水位线保证只切一次） */
  const _ca = state.ui.chapterArchive;
  if (_ca && _ca.ts && _ca.ts > _archiveSeenTs && _ca.key === state.currentKey
      && available.some((t) => t.key === 'archive')) {
    _archiveSeenTs = _ca.ts;
    tab = 'archive';
  }
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
  else if (tab === 'archive') pane = archivePane(ctx);
  else pane = skillsPane(ctx);
  body.append(pane);
  wrap.append(body);
  container.append(wrap);
  body.scrollTop = scrollTop;

  return { tab };
}
