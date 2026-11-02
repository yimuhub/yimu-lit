/* 消息渲染：安全 Markdown、过程卡、思考态、流式光标、操作与错误。 */

import { h, icon, relTime, fmtNum, parseDate, fmtTime } from '../core/dom.js';
import { renderMarkdownInto } from '../core/markdown.js';
import { store, updateMessage, patchUi } from '../core/state.js';
import { navigate } from '../core/router.js';
import { usageLabel } from '../core/constants.js';
import { toast, openFeedback } from '../core/ui.js';
import { api, errorText } from '../core/api.js';
import { cleanChapterText } from '../core/purity.js';

function thinkingDots() {
  return h('span', { class: 'thinking', 'aria-label': '正在思考' }, h('span', { class: 'tdot' }), h('span', { class: 'tdot' }), h('span', { class: 'tdot' }));
}

export function processCard(process, { live = false } = {}) {
  if (!process) return null;
  const details = h('details', { class: 'process-card', open: Boolean(live) });
  const summary = h('summary');
  summary.append(
    h('span', { class: 'pc-icon', html: icon(live ? 'activity' : 'cpu', 13) }),
    h('span', { class: 'pc-title', text: process.title || '处理过程' }),
    h('span', { class: 'pc-meta' })
  );
  const meta = summary.querySelector('.pc-meta');
  const parts = [];
  if (process.stage) parts.push(process.stage);
  if (process.model) parts.push(process.model);
  if (process.duration) parts.push(process.duration);
  if (process.tokens) parts.push(`${fmtNum(process.tokens)} tokens`);
  if (process.retries) parts.push(`重试 ${process.retries}`);
  parts.forEach((p, index) => {
    if (index) meta.append(h('span', { class: 'pc-sep' }));
    meta.append(h('span', { text: p }));
  });
  summary.append(h('span', { class: 'pc-chev', html: icon('chevron-right', 12) }));
  details.append(summary);

  const body = h('div', { class: 'pc-body' });
  if (process.detail) body.append(h('div', { text: process.detail }));
  if (Array.isArray(process.steps) && process.steps.length) {
    const steps = h('div', { class: 'pc-steps' });
    for (const step of process.steps) {
      steps.append(
        h(
          'div',
          { class: 'pc-step', dataset: { state: step.state || 'done' } },
          h('span', { class: 'step-dot' }),
          h('span', { text: step.label }),
          step.ms ? h('span', { class: 'muted', style: { marginLeft: 'auto' }, text: step.ms }) : null
        )
      );
    }
    body.append(steps);
  }
  details.append(body);
  return details;
}

function userAttachments(msg) {
  if (!Array.isArray(msg.attachments) || !msg.attachments.length) return null;
  const wrap = h('div', { class: 'attachments' });
  for (const att of msg.attachments) {
    wrap.append(
      h(
        'span',
        { class: 'attach-chip', title: att.title },
        h('span', { class: 'ac-icon', html: icon('file-text', 12) }),
        h('span', { class: 'ac-name', text: att.title || '附件' }),
        h('span', { class: 'sr-only', text: usageLabel(att.usage_mode) })
      )
    );
  }
  return wrap;
}

export function renderUserMessage(msg) {
  const el = h('div', { class: 'msg msg-user', dataset: { id: msg.id } });
  const atts = userAttachments(msg);
  if (atts) el.append(atts);
  el.append(h('div', { class: 'bubble', text: msg.content || '' }));
  /* v4.21：创作按钮（继续/暂停）回显气泡的副标注 */
  if (msg.creatorNote) {
    el.append(h('div', { class: 'bubble-note', text: msg.creatorNote }));
  }
  const meta = h(
    'div',
    { class: 'user-meta' },
    h('span', { text: msg.created_at ? relTime(msg.created_at) : '刚刚' }),
    h('button', {
      class: 'msg-action',
      type: 'button',
      'aria-label': '复制这条消息',
      html: icon('copy', 12),
      on: {
        click: async () => {
          try {
            await navigator.clipboard.writeText(msg.content || '');
            toast('已复制', { duration: 1400 });
          } catch {
            toast('复制失败，请手动选择文本', { tone: 'danger' });
          }
        }
      }
    })
  );
  el.append(meta);
  return el;
}

/* v2.9.0：章节完成卡片。
   默认折叠（标题 + 完成时间/字数/token/质量分 + 开头摘录），点开看全文、再点收起；
   操作：复制本章 / 从此章分支创作（分支成新书）/ 章节工作台。 */
function chapterExcerpt(text, chars = 260) {
  const t = String(text || '').trim();
  if (t.length <= chars) return t;
  return `${Array.from(t).slice(0, chars).join('')}…`;
}

function renderChapterCard(msg, handlers = {}) {
  const meta = msg.chapterMeta || {};
  const expanded = !msg.collapsed;
  const card = h('div', { class: `chapter-card${expanded ? ' is-open' : ''}` });

  /* v4.21.7：标题去重（章名缺省「第N章」时不再显示「第45章 · 第45章」） */
  const _t = String(meta.title || '').trim();
  const titleText = _t && !_t.match(/^第\s*\d+\s*章$/) ? `第${meta.no ?? '?'}章 · ${_t}` : `第${meta.no ?? '?'}章`;
  /* v4.21.7：本章过程下拉——同一章所有 Agent 的思考/工作/产出收进一个可展开区（懒加载） */
  let procLoaded = false;
  const procBody = h('div', { class: 'cc-process', style: { display: 'none' } },
    h('div', { class: 'hint-text cc-process-loading', text: '加载中…' }));
  const procBtn = h('button', {
    class: 'cc-proc-btn', type: 'button',
    'aria-expanded': 'false',
    title: '查看所有参与本章创作的 Agent 的思考、工作与产出过程',
    html: `${icon('history', 12)}<span>本章过程</span>`,
    on: { click: async (event) => {
      event.stopPropagation();
      const show = procBody.style.display === 'none';
      procBody.style.display = show ? '' : 'none';
      procBtn.setAttribute('aria-expanded', show ? 'true' : 'false');
      if (show && !procLoaded) {
        procLoaded = true;
        try {
          const key = handlers.workKey || store.get().currentKey;
          const res = await fetch(`/api/chapter-steps?key=${encodeURIComponent(key)}&chapter=${meta.no}`, { credentials: 'include' });
          const data = await res.json();
          procBody.textContent = '';
          const items = (data && data.items) || [];
          if (!items.length) {
            procBody.append(h('div', { class: 'hint-text cc-process-loading', text: '这一章还没有过程记录。' }));
            return;
          }
          for (const e of items) {
            const row = h('div', { class: 'arch-row' },
              h('span', { class: 'arch-time mono', text: String(e.time || '').slice(0, 8) || '—' }),
              h('span', { class: 'arch-badge', dataset: { agent: e.agent || 'system' }, text: e.agent_label || e.agent || '系统' }),
              h('span', { class: 'arch-title', text: `${e.phase === 'start' ? '▶' : e.phase === 'warn' ? '⚠' : '✓'} ${e.title || ''}${e.ms ? ` · ${(e.ms / 1000).toFixed(1)}s` : ''}` }));
            const det = [e.thinking, e.output].filter(Boolean).join('\n');
            if (det) {
              const pre = h('pre', { class: 'arch-detail', style: { display: 'none' }, text: det });
              row.classList.add('arch-expandable');
              row.addEventListener('click', () => { pre.style.display = pre.style.display === 'none' ? '' : 'none'; });
              procBody.append(h('div', {}, row, pre));
              continue;
            }
            procBody.append(row);
          }
        } catch {
          procBody.textContent = '';
          procBody.append(h('div', { class: 'hint-text cc-process-loading', text: '过程加载失败，请重试。' }));
        }
      }
    } }
  });
  /* cc-head 从 button 改为 div（内部嵌本章过程按钮，button 不能嵌套 button） */
  const head = h('div', {
    class: 'cc-head',
    role: 'button',
    tabindex: '0',
    'aria-expanded': expanded ? 'true' : 'false',
    title: expanded ? '收起本章' : '展开本章全文',
    on: { click: () => updateMessage(msg.id, { collapsed: expanded }) }
  },
    h('span', { class: 'cc-icon', html: icon('book-open', 14) }),
    h('span', { class: 'cc-title', text: titleText }),
    h('span', { class: 'cc-head-spacer' }),
    procBtn,
    h('span', { class: 'cc-chev', html: icon(expanded ? 'pause' : 'play', 12) })
  );
  card.append(head);
  card.append(procBody);

  const chips = h('div', { class: 'cc-meta' });
  /* v4.21.8：四维度同时显示（完成时间/字数/消耗 tokens/质量分）——
     消耗 = 该章全过程所有 Agent 的 token 总和（usage by_chapter）；
     质量分 < 90 时显示「重写本章」按钮（≥90 隐藏），未评分不显示。 */
  const items = [];
  if (meta.ts) items.push(['完成于', String(meta.ts).replace('T', ' ').slice(5, 16)]);
  items.push(['字数', fmtNum(Number(meta.words) || 0)]);
  items.push(['消耗', `${fmtNum(Number(meta.tokens) || 0)} tokens`]);
  items.push(['质量', meta.quality != null && meta.quality !== '' ? `${meta.quality} 分` : '未评分']);
  for (const [k, v] of items) {
    chips.append(h('span', { class: 'cc-chip' },
      h('span', { class: 'cc-k', text: k }),
      h('span', { class: 'cc-v', text: String(v) })));
  }
  const _q = Number(meta.quality);
  if (meta.quality != null && meta.quality !== '' && !Number.isNaN(_q) && _q < 90) {
    chips.append(h('button', {
      class: 'cc-rewrite-btn', type: 'button',
      title: '审查后定向重写本章（约 2-5 分钟）',
      text: '重写本章',
      on: { click: async (event) => {
        const btn = event.currentTarget;
        const key = handlers.workKey || store.get().currentKey;
        if (!key) return;
        btn.disabled = true;
        btn.textContent = '重写中…';
        try {
          const res = await api.rewriteChapter(key, meta.no);
          toast((res && res.msg) || '重写已启动：审查后定向重写该章', { tone: 'success', duration: 5000 });
        } catch (err) {
          toast(errorText(err, '重写启动失败'), { tone: 'danger' });
          btn.disabled = false;
          btn.textContent = '重写本章';
        }
      } }
    }));
  }
  card.append(chips);

  const body = h('div', { class: 'cc-body md' });
  /* v4.22.1：章节卡一律按正文纯净域渲染——旧留档里的概要/修改说明/评分表不再显示 */
  const clean = cleanChapterText(msg.content || '') || String(msg.content || '');
  if (expanded) {
    renderMarkdownInto(body, clean);
  } else {
    body.append(h('div', { class: 'cc-preview', text: chapterExcerpt(clean) }));
  }
  card.append(body);

  const acts = h('div', { class: 'cc-actions' });
  acts.append(h('button', {
    class: 'msg-action', type: 'button',
    html: `${icon(expanded ? 'pause' : 'eye', 12)}<span>${expanded ? '收起' : '展开全文'}</span>`,
    on: { click: () => updateMessage(msg.id, { collapsed: expanded }) }
  }));
  acts.append(h('button', {
    class: 'msg-action', type: 'button',
    html: `${icon('copy', 12)}<span>复制本章</span>`,
    on: {
      click: async () => {
        try {
          /* v4.22.1：只复制正文（章题+引子+正文），元信息小节与前后的说明一律剥掉 */
          await navigator.clipboard.writeText(clean);
          toast('本章正文已复制（不含概要/备注/修改说明/评分）', { duration: 2000 });
        } catch {
          toast('复制失败，请手动选择文本', { tone: 'danger' });
        }
      }
    }
  }));
  if (handlers.workKey) {
    acts.append(h('button', {
      class: 'msg-action', type: 'button',
      html: `${icon('branch', 12)}<span>从此章分支创作</span>`,
      title: '复制 1..N 章成一本新书，从下一章开始新思路',
      on: {
        click: async (event) => {
          const btn = event.currentTarget;
          btn.disabled = true;
          try {
            const res = await api.branchBook(handlers.workKey, meta.no);
            toast(`已创建分支《${res.name || res.key}》，第${(meta.no || 0) + 1}章起按新思路创作`, { tone: 'success', duration: 4200 });
            navigate(`/work/${encodeURIComponent(res.key)}`);
          } catch (err) {
            toast(errorText(err, '分支失败'), { tone: 'danger' });
            btn.disabled = false;
          }
        }
      }
    }));
    acts.append(h('button', {
      class: 'msg-action', type: 'button',
      html: `${icon('edit', 12)}<span>章节工作台</span>`,
      on: { click: () => navigate(`/work/${encodeURIComponent(handlers.workKey)}/bench/${meta.no || 1}`) }
    }));
  }
  card.append(acts);
  return card;
}

export function renderAssistantMessage(msg, handlers = {}) {
  const assistant = h('div', { class: 'msg msg-assistant', dataset: { id: msg.id } });
  assistant.append(h('div', { class: 'msg-avatar', html: icon('quill', 14) }));
  const main = h('div', { class: 'msg-main' });

  const head = h('div', { class: 'msg-head' });
  head.append(h('span', { class: 'msg-author', text: '一幕·YIMU 神笔' }));
  if (msg.created_at) head.append(h('span', { class: 'msg-time', text: relTime(msg.created_at) }));
  if (msg.tag) head.append(h('span', { class: 'msg-tag', text: msg.tag }));
  if (msg.streaming) {
    head.append(h('span', { class: 'msg-tag msg-phase', text: msg.phase || '生成中' }));
  }
  main.append(head);

  const card = processCard(msg.process, { live: Boolean(msg.streaming) });
  if (card) main.append(card);

  if (msg.error) {
    main.append(renderErrorBanner(msg.error, { onRetry: handlers.onRetry, onDismiss: handlers.onDismissError }));
  }

  const body = h('div', { class: `msg-body md${msg.streaming ? ' is-streaming' : ''}` });
  if (msg.chapterMeta && !msg.streaming) {
    /* v2.9.0：章节完成卡片（折叠/展开 + 复制/分支/工作台） */
    body.classList.add('has-chapter-card');
    body.append(renderChapterCard(msg, handlers));
  } else if (msg.content) renderMarkdownInto(body, msg.content);
  else if (msg.streaming) body.append(thinkingDots());
  main.append(body);

  const hints = h('div', { class: 'msg-hints' });
  if (msg.status) hints.append(h('div', { class: 'hint-text msg-status', text: msg.status }));
  if (msg.notice) hints.append(h('div', { class: 'hint-text msg-notice', text: msg.notice }));
  if (msg.streaming || hints.childNodes.length) main.append(hints);

  if (msg.action && msg.action.text) {
    main.append(
      h('button', {
        class: 'msg-link',
        type: 'button',
        text: msg.action.text,
        on: { click: () => navigate(msg.action.path || '/settings') }
      })
    );
  }

  if (!msg.streaming) {
    const actions = h('div', { class: 'msg-actions' });
    const copyBtn = h(
      'button',
      { class: 'msg-action', type: 'button', html: `${icon('copy', 13)}<span>复制</span>` },
      null
    );
    copyBtn.addEventListener('click', async () => {
      try {
        /* v4.22.1：章节卡/作家正文卡的「复制」也走正文纯净域，不把元信息带进剪贴板 */
        await navigator.clipboard.writeText(copyTextOf(msg));
        toast('已复制回复', { duration: 1400 });
      } catch {
        toast('复制失败，请手动选择文本', { tone: 'danger' });
      }
    });
    actions.append(copyBtn);

    if (handlers.onRetry) {
      actions.append(
        h('button', {
          class: 'msg-action',
          type: 'button',
          html: `${icon('refresh', 13)}<span>重试</span>`,
          on: { click: () => handlers.onRetry(msg) }
        })
      );
    }

    const good = h('button', {
      class: `msg-action${msg.rating === 'up' ? ' is-on' : ''}`,
      type: 'button',
      'aria-pressed': msg.rating === 'up' ? 'true' : 'false',
      'aria-label': '这条回复有帮助',
      html: icon('thumbs-up', 13),
      on: {
        click: async () => {
          updateMessage(msg.id, { rating: msg.rating === 'up' ? null : 'up' });
          if (msg.rating !== 'up') {
            try {
              await api.feedback({ project_key: handlers.workKey || '', rating: 5, tags: ['有帮助'], note: msg.content.slice(0, 200) });
            } catch {
              /* 反馈失败不打断阅读 */
            }
            toast('已记为“有帮助”，会强化这类写法', { duration: 1800 });
          }
        }
      }
    });
    const bad = h('button', {
      class: `msg-action${msg.rating === 'down' ? ' is-on' : ''}`,
      type: 'button',
      'aria-label': '这条回复需要改进',
      html: icon('thumbs-down', 13),
      on: {
        click: () => {
          openFeedback({
            onSubmit: async ({ rating, tags, note }) => {
              await api.feedback({
                project_key: handlers.workKey || '',
                chapter_no: handlers.chapter || null,
                rating,
                tags,
                note
              });
              updateMessage(msg.id, { rating: 'down' });
            }
          });
        }
      }
    });
    actions.append(good, bad);

    if (handlers.onBranch) {
      actions.append(
        h('button', {
          class: 'msg-action',
          type: 'button',
          html: `${icon('branch', 13)}<span>从这里分支</span>`,
          on: { click: () => handlers.onBranch(msg) }
        })
      );
    }

    actions.append(h('span', { class: 'actions-spacer' }));
    if (msg.created_at) actions.append(h('span', { class: 'msg-stamp', text: relTime(msg.created_at) }));
    main.append(actions);
  }

  assistant.append(main);
  return assistant;
}

function renderErrorBanner(error, { onRetry, onDismiss } = {}) {
  const wrap = h('div', { class: 'msg-error', role: 'alert' });
  wrap.append(h('span', { html: icon('alert', 15) }));
  const main = h('div', { class: 'err-main' });
  main.append(h('strong', { text: '这一轮没有完成' }));
  main.append(h('div', { text: error }));
  wrap.append(main);
  const actions = h('div', { class: 'err-actions' });
  if (onRetry) {
    actions.append(h('button', { class: 'err-btn', type: 'button', text: '重试', on: { click: onRetry } }));
  }
  if (onDismiss) {
    actions.append(h('button', { class: 'err-btn', type: 'button', text: '知道了', on: { click: onDismiss } }));
  }
  wrap.append(actions);
  return wrap;
}

export function renderApprovalCard(approval, handlers = {}) {
  if (!approval) return null;
  const wrap = h('div', { class: 'approval-card' });
  wrap.append(
    h(
      'div',
      { class: 'ap-head' },
      h('span', { html: icon('shield', 14) }),
      h('span', { text: approval.done ? '规划提案已处理' : '需要你确认' })
    ),
    h('div', { class: 'ap-title', text: approval.title }),
    h('div', { class: 'ap-reason', text: approval.reason })
  );
  if (Array.isArray(approval.scope) && approval.scope.length) {
    const scope = h('div', { class: 'ap-scope' });
    for (const s of approval.scope) scope.append(h('span', { class: 'chip chip-muted', text: s }));
    wrap.append(scope);
  }
  const actions = h('div', { class: 'ap-actions' });
  if (approval.done) {
    actions.append(h('span', { class: 'status-chip', dataset: { tone: 'success' }, text: approval.done }));
  } else {
    actions.append(
      h('button', {
        class: 'btn btn-primary btn-sm',
        type: 'button',
        text: '应用提案',
        on: { click: () => handlers.onApply && handlers.onApply(approval) }
      }),
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        text: '先看差异',
        on: { click: () => handlers.onViewPlan && handlers.onViewPlan(approval) }
      }),
      h('button', {
        class: 'btn btn-ghost btn-sm',
        type: 'button',
        text: '驳回',
        on: { click: () => handlers.onReject && handlers.onReject(approval) }
      })
    );
  }
  wrap.append(actions);
  return wrap;
}

export function renderQueueRow(queue, { onRemove } = {}) {
  if (!queue || !queue.length) return null;
  const row = h('div', { class: 'queue-row', 'aria-label': '待发送队列' });
  queue.forEach((item) => {
    const chip = h(
      'span',
      { class: 'queue-chip', title: item.text || '' },
      h('span', { html: icon('clock', 11) }),
      h('span', { class: 'q-text', text: item.text || '（附件）' })
    );
    if (onRemove) {
      chip.append(
        h('button', {
          class: 'q-x',
          type: 'button',
          'aria-label': '移出队列',
          html: icon('x', 10),
          on: { click: () => onRemove(item.id) }
        })
      );
    }
    row.append(chip);
  });
  return row;
}

/* --------------------------------------------- v4.21 对话内创作活动流
   步骤卡（kind:'step'）：左侧岗位徽标头像 + 标题行（agent_label · title +
   状态点 + 时间 + 耗时 + 可选「思考」折叠钮）+ 单行 output 摘要；
   chapter>0 且有产出时附「查看本章过程」→ patchUi.chapterArchive（右栏弹本章档案）。
   流式卡（msg.stream）：同一外壳，正文换成自动滚底的流式文本区；完成后收起为
   保留「展开查看全文」的完成卡。phase: start=呼吸点 / done=绿✓ / warn=警示。 */

const STEP_AGENT_ICONS = {
  planner: 'target',
  writer: 'pen',
  reviewer: 'check',
  deslopper: 'sparkles',
  rewriter: 'wand',
  editor: 'eye',
  system: 'clock'
};

function stepOneLine(text) {
  return String(text || '').replace(/\s+/g, ' ').trim();
}

function stepTimeText(msg) {
  const st = msg.step || {};
  const raw = String(st.time || '').trim();
  if (/^\d{1,2}:\d{2}/.test(raw)) return raw.slice(0, 5);
  const d = parseDate(st.ts || raw);
  if (d) return fmtTime(d);
  return msg.created_at ? relTime(msg.created_at) : '';
}

function stepMsText(st) {
  const ms = Number(st && st.ms) || 0;
  return ms > 0 ? `· ${(ms / 1000).toFixed(1)}s` : '';
}

function chapterBranchBtn(st) {
  /* v4.21.5：从本章分支出新书（全新思路/规划）——正文全文卡与实时完成卡共用 */
  if (!(Number(st && st.chapter) > 0)) return null;
  return h('button', {
    class: 'step-link', type: 'button',
    html: `${icon('branch', 11)}<span>从此章分支</span>`,
    title: '复制 1..N 章成一本新书，从下一章开始全新思路与规划',
    on: { click: async (event) => {
      const btn = event.currentTarget;
      const key = store.get().currentKey;
      if (!key) return;
      btn.disabled = true;
      try {
        const res = await api.branchBook(key, Number(st.chapter));
        toast(`已创建分支《${res.name || res.key}》，第${(Number(st.chapter) || 0) + 1}章起按新思路创作`, { tone: 'success', duration: 4200 });
        navigate(`/work/${encodeURIComponent(res.key)}`);
      } catch (err) {
        toast(errorText(err, '分支失败'), { tone: 'danger' });
        btn.disabled = false;
      }
    } }
  });
}

function isThinkCard0(st) {
  return Boolean(st && (st.is_thinking || /（模型思考）$/.test(String(st.title || ''))));
}

/* v4.22.1：任意消息的「复制」口径——章节卡/作家正文卡给去噪正文，其余原样。 */
function copyTextOf(msg) {
  const raw = msg.content || '';
  if (msg.chapterMeta) return cleanChapterText(raw) || raw;
  if (msg.stream) return copyableStreamText(msg);
  return raw;
}

/* v4.22.1：作家流式正文的「复制」必须给去噪后的正文（章题+引子+正文）；
   规划师大纲卡、模型思考卡保持原文。 */
function copyableStreamText(msg) {
  const st = msg.step || {};
  const raw = msg.content || '';
  if (String(st.agent || '') !== 'writer' || isThinkCard0(st)) return raw;
  return cleanChapterText(raw) || raw;
}

function stepChapterBtn(st) {
  if (!(Number(st.chapter) > 0)) return null;
  return h('button', {
    class: 'step-link',
    type: 'button',
    html: `${icon('book-open', 11)}<span>查看本章过程</span>`,
    title: '在右侧打开本章创作档案',
    on: {
      click: () => {
        const key = store.get().currentKey;
        if (!key) return;
        /* v4.21.7：同时打开右栏并切到档案页签——此前右栏关闭时点击无任何反应 */
        patchUi({ chapterArchive: { key, chapter: Number(st.chapter), ts: Date.now() },
                  rightOpen: true, rightTab: 'archive' });
      }
    }
  });
}

function stepStreamBox(msg, live) {
  const box = h('div', { class: `step-stream${live ? '' : ' is-open'}` });
  box.append(
    h('div', {
      class: `step-stream-text${live ? ' step-streaming' : ''}`,
      dataset: { raw: msg.content || '' },
      text: msg.content || '正在输出…'
    })
  );
  return box;
}

export function renderStepMessage(msg) {
  const st = msg.step || {};
  const agent = String(st.agent || 'system');
  const label = String(st.agent_label || '系统').trim() || '系统';
  const phase = String(st.phase || (st.streaming ? 'start' : 'done'));
  const isStreamCard = Boolean(msg.stream);
  const collapsed = Boolean(msg.collapsed);

  const el = h('div', { class: 'msg-step step-msg', dataset: { id: msg.id } });

  /* 岗位徽标头像：圆形底 + 岗位 icon（缺映射时退回 agent_label 首字） */
  const avaIcon = STEP_AGENT_ICONS[agent];
  el.append(
    h(
      'span',
      { class: `step-ava step-ava-${agent}`, title: label },
      avaIcon ? h('span', { html: icon(avaIcon, 12) }) : label.slice(0, 1)
    )
  );

  const main = h('div', { class: 'step-main' });

  /* 标题行：agent_label · title + 状态 + 时间/耗时 + 「思考」 */
  const head = h('div', { class: 'step-head' });
  head.append(
    h(
      'span',
      { class: 'step-title' },
      h('span', { class: 'st-agent', text: label }),
      h('span', { class: 'st-sep', text: '·' }),
      h('span', {
        class: 'st-name',
        text: stepOneLine(st.title) || (Number(st.chapter) > 0 ? `第${st.chapter}章` : '创作活动')
      })
    )
  );
  if (phase === 'start') head.append(h('span', { class: 'step-dot-live', 'aria-label': '进行中' }));
  else if (phase === 'warn') head.append(h('span', { class: 'step-phase-warn', html: icon('alert', 12), title: '需要注意' }));
  else head.append(h('span', { class: 'step-phase-done', html: icon('check', 12), title: '已完成' }));

  if (isThinkCard0(st)) {
    head.append(h('span', { class: 'chip chip-muted', text: '思考', title: '模型思维链全文' }));
  }
  const timeText = stepTimeText(msg);
  const msText = stepMsText(st);
  const right = h('span', { class: 'step-right' });
  if (timeText || msText) {
    right.append(h('span', { class: 'step-meta', text: [timeText, msText].filter(Boolean).join(' ') }));
  }
  const thinking = String(st.thinking || '').trim();
  if (thinking && !isStreamCard) {
    right.append(
      h('button', {
        class: `step-think-btn${msg.thinkOpen ? ' is-open' : ''}`,
        type: 'button',
        'aria-expanded': msg.thinkOpen ? 'true' : 'false',
        text: '思考',
        on: { click: () => updateMessage(msg.id, { thinkOpen: !msg.thinkOpen }) }
      })
    );
  }
  if (right.childNodes.length) head.append(right);
  main.append(head);

  if (thinking && !isStreamCard && msg.thinkOpen) {
    main.append(h('div', { class: 'step-think', text: thinking }));
  }

  if (isStreamCard) {
    /* v4.21.4：模型思维链实时区（session 的 paintStreamNode 直改 .step-stream-think-text） */
    if (msg.withThinking) {
      const thinkWrap = h('div', { class: 'step-stream-think', hidden: true },
        h('div', { class: 'step-stream-think-label' },
          h('span', { html: icon('sparkles', 11) }),
          h('span', { text: '模型思考中…' })),
        h('pre', { class: 'step-stream-think-text' }, '（等待模型思维链…）'));
      main.append(thinkWrap);
    }
    if (collapsed) {
      /* 完成卡：默认收起，保留「展开查看全文」+ 复制全文 */
      const acts = h('div', { class: 'step-output' });
      acts.append(
        h('button', {
          class: 'step-link',
          type: 'button',
          html: `${icon('copy', 11)}<span>复制全文</span>`,
          on: { click: async () => {
            try {
              /* v4.22.1：作家正文卡只复制去噪正文；其它岗位（规划师等）保持原文 */
              await navigator.clipboard.writeText(copyableStreamText(msg));
              toast('全文已复制', { duration: 1400 });
            } catch {
              toast('复制失败', { tone: 'danger' });
            }
          } }
        })
      );
      acts.append(
        h('button', {
          class: 'step-link',
          type: 'button',
          html: `${icon('eye', 11)}<span>展开查看全文</span>`,
          on: { click: () => updateMessage(msg.id, { collapsed: false }) }
        })
      );
      const brBtn1 = chapterBranchBtn(st);
      if (brBtn1) acts.append(brBtn1);
      const chBtn = stepChapterBtn(st);
      if (chBtn) acts.append(chBtn);
      main.append(acts);
    } else {
      main.append(stepStreamBox(msg, phase === 'start'));
      const acts = h('div', { class: 'step-output' });
      acts.append(
        h('button', {
          class: 'step-link',
          type: 'button',
          html: `${icon('copy', 11)}<span>复制已出内容</span>`,
          on: { click: async () => {
            try {
              /* v4.22.1：同上——作家正文按纯净域口径复制 */
              await navigator.clipboard.writeText(copyableStreamText(msg));
              toast('已复制当前已流出的内容', { duration: 1400 });
            } catch {
              toast('复制失败', { tone: 'danger' });
            }
          } }
        })
      );
      acts.append(
        h('button', {
          class: 'step-link',
          type: 'button',
          html: `${icon('eye', 11)}<span>收起</span>`,
          on: { click: () => updateMessage(msg.id, { collapsed: true }) }
        })
      );
      const chBtn = stepChapterBtn(st);
      if (chBtn) acts.append(chBtn);
      main.append(acts);
    }
  } else {
    /* 普通步骤卡：单行 output 摘要 + 章节入口。
       v4.21.3：流式正文全文已永久落盘（stream_text）——历史回放时以可展开卡呈现。 */
    const fullText = String(st.stream_text || '').trim();
    const isThinkCard = Boolean(st.is_thinking);
    const out = stepOneLine(st.output);
    if (out || fullText) {
      const acts = h('div', { class: 'step-output' });
      if (out) acts.append(h('span', { class: 'so-text', text: out, title: out }));
      if (fullText) {
        const body = h('pre', {
          class: 'step-stream-text',
          style: { display: 'none', maxHeight: '260px', overflow: 'auto', margin: '8px 0 0',
                   padding: '10px 12px', background: 'var(--code-fill)', border: '0.5px solid var(--line-2)',
                   borderRadius: 'var(--radius-md)', whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                   fontSize: '12px', lineHeight: '1.8', color: 'var(--ink-secondary)' },
          text: fullText
        });
        const toggle = (btn) => {
          const show = body.style.display === 'none';
          body.style.display = show ? '' : 'none';
          const label = btn.querySelector('span');
          if (label) label.textContent = show ? '收起' : (isThinkCard ? '思考全文' : '正文全文');
          if (show) body.scrollTop = 0;
        };
        acts.append(h('button', {
          class: 'step-link', type: 'button',
          html: `${icon('eye', 11)}<span>${isThinkCard ? '思考全文' : '正文全文'}</span>`,
          on: { click: (e) => toggle(e.currentTarget) }
        }));
        acts.append(h('button', {
          class: 'step-link', type: 'button',
          title: '复制全文',
          html: `${icon('copy', 11)}<span>复制</span>`,
          on: { click: async () => {
            try {
              /* v4.22.1：正文全文卡（stream_text）同样是作家原稿——复制前先去噪 */
              const text = (!isThinkCard && String(st.agent || '') === 'writer')
                ? (cleanChapterText(fullText) || fullText) : fullText;
              await navigator.clipboard.writeText(text);
              toast('全文已复制', { duration: 1400 });
            } catch {
              toast('复制失败，请手动选择文本', { tone: 'danger' });
            }
          } }
        }));
        main.append(body);
      }
      const brBtn2 = chapterBranchBtn(st);
      if (brBtn2) acts.append(brBtn2);
      const chBtn = stepChapterBtn(st);
      if (chBtn) acts.append(chBtn);
      main.append(acts);
    }
  }

  el.append(main);
  return el;
}

