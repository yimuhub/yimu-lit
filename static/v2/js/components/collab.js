/* 协作工作台（v2.1 P2）：多 Agent 岗位 / ⚡分章齐写 / 后台任务 / 用量与预算。
   由 rightpanel「协作」tab 挂载；返回 { el, destroy }，切书重建时先 destroy 再 create。
   实时数据一律读 store.get().live（app.js 的 SSE 订阅），本面板不再自开 EventSource。 */

import { h, icon, fmtNum, fmtWords, relTime, statusMeta } from '../core/dom.js';
import { store } from '../core/state.js';
import { api, errorText } from '../core/api.js';
import { toast, popoverMenu } from '../core/ui.js';
import { SKILL_TITLES, loadSkillCatalog } from '../core/skillCatalog.js';

const POLL_MS = 2500;
const LIVE_ROWS = 6;
const JOB_ROWS = 12;

/* 数字缺失显示「—」而不是 NaN。 */
function numText(v) {
  if (v == null || v === '') return '—';
  const n = Number(v);
  return Number.isFinite(n) ? fmtNum(n) : '—';
}

/* 统一走 api.xxx；同事尚未在 core/api.js 加方法时给出可读错误而不是崩溃。 */
function callApi(name, ...args) {
  const fn = api && api[name];
  if (typeof fn !== 'function') {
    return Promise.reject(new Error(`界面方法 api.${name} 尚未接入（core/api.js）`));
  }
  return Promise.resolve().then(() => fn.apply(api, args));
}

function sectionHead(title, countText) {
  const head = h('div', { class: 'rp-section-head' }, h('h3', { text: title }));
  if (countText) head.append(h('span', { class: 'count', text: countText }));
  return head;
}

function errorLine(message, retry) {
  return h(
    'div',
    { class: 'msg-error', role: 'alert' },
    h('span', { html: icon('alert', 14) }),
    h('span', { text: message }),
    h('button', { class: 'err-btn', type: 'button', text: '重试', on: { click: () => retry() } })
  );
}

function loadingSkeleton() {
  return h(
    'div',
    { class: 'session-loading' },
    h('span', { class: 'skeleton skeleton-line', style: { width: '60%' } })
  );
}

const JOB_STATUS = {
  queued: { label: '排队', tone: 'muted' },
  pending: { label: '排队', tone: 'muted' },
  running: { label: '运行', tone: 'running' },
  in_progress: { label: '运行', tone: 'running' },
  done: { label: '完成', tone: 'success' },
  completed: { label: '完成', tone: 'success' },
  ok: { label: '完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  error: { label: '失败', tone: 'danger' },
  cancelled: { label: '已取消', tone: 'muted' }
};

function jobStatus(job) {
  const raw = String((job && job.status) || 'queued').toLowerCase();
  return JOB_STATUS[raw] || { label: raw, tone: 'muted' };
}

function sourceBadge(a) {
  if (a.provider_id || a.model) return h('span', { class: 'badge badge-accent', text: '本岗位' });
  if (a.stage) return h('span', { class: 'badge', text: '按阶段' });
  return h('span', { class: 'badge', dataset: { tone: 'muted' }, text: '默认' });
}

export function createCollabPanel(ctx) {
  const s = {
    key: store.get().currentKey || '',
    view: null,
    viewError: '',
    jobs: null,
    jobsError: '',
    usage: null,
    usageError: '',
    workers: 5,
    workersTouched: false,
    running: false,
    logResult: null,
    pollTimer: 0,
    pollFails: 0,
    catalogRepainted: false,
    destroyed: false
  };

  const el = h('div', { class: 'rp-pane' });
  const summaryWrap = h('div', { class: 'rp-section' });
  const agentsBody = h('div', {});
  const parallelBody = h('div', {});
  const jobsBody = h('div', {});
  const usageBody = h('div', {});
  el.append(summaryWrap, agentsBody, parallelBody, jobsBody, usageBody);

  /* ------------------------------------------------------ 本书协作状态 */

  function currentWork() {
    const state = store.get();
    return state.work || (state.library.items || []).find((i) => i.key === s.key) || null;
  }

  function paintSummary() {
    summaryWrap.textContent = '';
    const work = currentWork();
    summaryWrap.append(sectionHead('本书协作状态'));
    if (!work) {
      summaryWrap.append(
        h('div', { class: 'hint-text', text: '还没有打开作品：从左侧选择一本作品后，这里会显示它的多 Agent 协作进度。' })
      );
      return;
    }
    const done = Number(work.done) || 0;
    const total = Number(work.total) || 0;
    const pct = total ? Math.round((done / total) * 100) : 0;
    const meta = statusMeta(work.state, work.status_label);
    summaryWrap.append(
      h(
        'div',
        { class: 'rp-summary' },
        h(
          'div',
          { class: 'rp-summary-top' },
          h('div', { class: 'rp-summary-title', text: work.name || s.key || '未命名作品' }),
          h('span', { class: 'status-chip', dataset: { tone: meta.tone }, text: meta.label })
        ),
        h(
          'div',
          { class: 'rp-summary-meta' },
          h('span', { text: `${done} / ${total} 章` }),
          h('span', { text: `${pct}%` }),
          h('span', { text: fmtWords(work.total_words) })
        ),
        h('div', { class: 'progress-track', html: `<span style="width:${pct}%"></span>` })
      )
    );
  }

  /* ---------------------------------------------------------- 岗位卡 */

  async function loadAgents() {
    s.viewError = '';
    paintAgents();
    try {
      s.view = await callApi('agents');
      const dw = Number(s.view && s.view.default_workers);
      if (!s.workersTouched && dw >= 1 && dw <= 8 && dw !== s.workers) {
        s.workers = dw;
        paintParallel();
      }
    } catch (err) {
      s.viewError = errorText(err, '岗位列表加载失败');
    }
    paintAgents();
  }

  async function setBinding(a, providerId, modelId) {
    try {
      await callApi('agentsSet', { agent: a.agent, provider_id: providerId || '', model: modelId || '' });
      toast(`${a.label}已改用 ${modelId || providerId || '阶段默认'}`, { tone: 'success', duration: 2200 });
      loadAgents();
    } catch (err) {
      toast(errorText(err, '换模型失败'), { tone: 'danger' });
    }
  }

  function openModelMenu(anchor, a) {
    const options = (s.view && Array.isArray(s.view.options) && s.view.options) || [];
    if (!options.length) {
      toast('还没有可选供应商：先在设置里配置模型与密钥', { tone: 'warn', duration: 2600 });
      return;
    }
    const items = [{ label: '回落 stage/默认（解除绑定）', icon: 'refresh', onSelect: () => setBinding(a, '', '') }];
    for (const p of options) {
      items.push({ separator: true });
      items.push({
        label: p.name || p.id,
        sub: p.default_model ? `默认 ${p.default_model}` : '',
        icon: 'plug',
        onSelect: () => openProviderModels(anchor, a, p)
      });
    }
    popoverMenu(anchor, items, { label: `${a.label} · 选供应商` });
  }

  function openProviderModels(anchor, a, p) {
    const models = Array.isArray(p.models) ? p.models : [];
    const items = [];
    if (p.default_model) {
      items.push({
        label: '供应商默认模型',
        sub: p.default_model,
        icon: 'dot',
        checked: a.provider_id === p.id && !a.model,
        onSelect: () => setBinding(a, p.id, '')
      });
    }
    for (const m of models) {
      items.push({
        label: String(m),
        icon: 'dot',
        checked: a.provider_id === p.id && a.model === m,
        onSelect: () => setBinding(a, p.id, m)
      });
    }
    popoverMenu(anchor, items, { label: `${p.name || p.id} · 选模型` });
  }

  function agentCard(a) {
    const card = h(
      'div',
      {
        class: 'collab-card',
        style: {
          border: '1px solid var(--line-2)',
          borderRadius: 'var(--radius-sm)',
          background: 'var(--bg-elevated)',
          padding: '10px',
          display: 'flex',
          flexDirection: 'column',
          gap: '6px'
        }
      }
    );
    const modelText = a.model || (a.provider_id ? '供应商默认模型' : a.effective) || '未绑定';
    card.append(
      h(
        'div',
        { style: { display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' } },
        h('span', { style: { fontWeight: '600', color: 'var(--ink-primary)' }, text: a.label || a.agent }),
        a.stage ? h('span', { class: 'chip chip-muted', text: a.stage }) : null,
        sourceBadge(a),
        h('span', { style: { flex: '1' } }),
        h('button', {
          class: 'icon-btn icon-btn-sm',
          type: 'button',
          title: '改模型',
          'aria-label': `${a.label}：改模型`,
          html: icon('cpu', 13),
          on: { click: (event) => openModelMenu(event.currentTarget, a) }
        })
      ),
      h('div', { class: 'mono', style: { fontSize: '11px', color: 'var(--ink-secondary)' }, text: modelText })
    );
    if (a.desc) card.append(h('div', { class: 'hint-text', text: a.desc }));
    /* skills 为同事并行新增字段：没有该字段就整块不渲染，绝不报错。 */
    if (Array.isArray(a.skills)) {
      const chips = h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '4px' } });
      if (a.skills.length) {
        for (const sk of a.skills) chips.append(h('span', { class: 'chip', text: SKILL_TITLES[String(sk)] || String(sk) }));
      } else {
        chips.append(h('span', { class: 'chip chip-muted', text: '未绑定技能' }));
      }
      card.append(chips);
    }
    return card;
  }

  function paintAgents() {
    agentsBody.textContent = '';
    const sec = h('div', { class: 'rp-section' });
    const list = s.view && Array.isArray(s.view.agents) ? s.view.agents : [];
    sec.append(sectionHead('创作岗位', list.length ? `${list.length} 个岗位` : ''));
    if (s.viewError) {
      sec.append(errorLine(s.viewError, loadAgents));
    } else if (!s.view) {
      sec.append(loadingSkeleton());
    } else {
      if (!list.length) sec.append(h('div', { class: 'hint-text', text: '后端没有返回岗位数据。' }));
      const wrap = h('div', { style: { display: 'flex', flexDirection: 'column', gap: '8px' } });
      for (const a of list) wrap.append(agentCard(a));
      sec.append(wrap);
    }
    agentsBody.append(sec);
    /* 绑定技能显示中文标题：清单到位后重绘一次（只重绘一次，失败也不再重试）。 */
    if (!s.catalogRepainted && list.some((a) => Array.isArray(a.skills) && a.skills.length)) {
      s.catalogRepainted = true;
      loadSkillCatalog().then((cat) => {
        if (cat.length) paintAgents();
      });
    }
  }

  /* -------------------------------------------------------- 分章齐写 */

  function stopPoll() {
    if (s.pollTimer) {
      clearInterval(s.pollTimer);
      s.pollTimer = 0;
    }
  }

  function startPoll() {
    stopPoll();
    if (s.destroyed || !s.key) return;
    s.pollTimer = setInterval(() => pollParallelLog(), POLL_MS);
  }

  async function pollParallelLog() {
    if (s.destroyed || !s.key || document.hidden) return;
    try {
      const res = await callApi('parallelLog', s.key);
      if (s.destroyed) return;
      s.pollFails = 0;
      s.running = Boolean(res && res.running);
      s.logResult = res ? res.result : null;
      if (!s.running) {
        stopPoll();
        if (s.logResult === 'ok') {
          toast('⚡ 并行创作完成', { tone: 'success' });
        } else if (s.logResult && typeof s.logResult === 'object' && s.logResult.error) {
          toast(String(s.logResult.error).slice(0, 120), { tone: 'danger', duration: 5000 });
        }
        loadJobs();
        loadUsage();
        if (ctx && typeof ctx.refreshWork === 'function') ctx.refreshWork();
      }
    } catch (err) {
      if (s.destroyed) return;
      s.pollFails += 1;
      if (s.pollFails >= 4) {
        stopPoll();
        s.running = false;
        toast(errorText(err, '并行进度查询失败，已停止轮询'), { tone: 'danger' });
      }
    }
    paintParallel();
  }

  async function startParallel(btn) {
    if (!s.key) {
      toast('请先打开一本作品', { tone: 'danger' });
      return;
    }
    if (btn) btn.disabled = true;
    try {
      const res = await callApi('parallelWrite', { key: s.key, workers: s.workers });
      toast((res && res.msg) || '并行创作已启动', { tone: 'success', duration: 4200 });
      if (res && res.running) {
        s.running = true;
        s.logResult = null;
        startPoll();
      }
    } catch (err) {
      toast(errorText(err, '启动并行创作失败'), { tone: 'danger' });
    }
    if (btn) btn.disabled = false;
    paintParallel();
  }

  function paintLive() {
    /* 从 app.js 的 SSE 数据读，不自建连接。 */
    const live = Array.isArray(store.get().live) ? store.get().live : [];
    const box = el.querySelector('.collab-live');
    if (!box) return;
    box.textContent = '';
    if (!live.length) {
      box.append(h('div', { class: 'hint-text', text: '暂无实时日志；启动并行创作后会跟随 SSE 输出。' }));
      return;
    }
    for (const line of live.slice(-LIVE_ROWS).reverse()) {
      box.append(h('div', { class: 'live-log-line', text: line && line.message ? String(line.message) : String(line) }));
    }
  }

  function paintParallel() {
    parallelBody.textContent = '';
    const sec = h('div', { class: 'rp-section' });
    sec.append(sectionHead('分章齐写', s.running ? '进行中' : ''));

    const valueOut = h('span', { class: 'badge badge-accent', text: `${s.workers} Agents` });
    const slider = h('input', {
      type: 'range',
      min: '1',
      max: '8',
      step: '1',
      value: String(s.workers),
      'aria-label': '并行 Agent 数',
      style: { flex: '1', accentColor: 'var(--accent)', minWidth: '0' },
      on: {
        input: (event) => {
          s.workersTouched = true;
          s.workers = Math.min(8, Math.max(1, Number(event.target.value) || 5));
          valueOut.textContent = `${s.workers} Agents`;
        }
      }
    });
    sec.append(
      h(
        'div',
        { style: { display: 'flex', alignItems: 'center', gap: '8px' } },
        slider,
        valueOut,
        h('button', {
          class: 'btn btn-primary btn-sm',
          type: 'button',
          html: `${icon('zap', 13)}<span>开始齐写</span>`,
          disabled: s.running || !s.key,
          on: { click: (event) => startParallel(event.currentTarget) }
        })
      )
    );
    if (!s.key) sec.append(h('div', { class: 'hint-text', text: '打开一本作品后才能启动并行创作。' }));

    if (s.running) {
      sec.append(
        h(
          'div',
          { class: 'hint-text', style: { display: 'flex', alignItems: 'center', gap: '6px' } },
          h('span', { class: 'spinner' }),
          h('span', { text: `${s.workers} 个 Agent 分章齐写中：先熔炼总架构，写完一本立即取下一章…` })
        )
      );
    } else if (s.logResult === 'ok') {
      sec.append(h('div', {}, h('span', { class: 'badge', dataset: { tone: 'success' }, text: '上一轮并行创作已完成' })));
    } else if (s.logResult && typeof s.logResult === 'object' && s.logResult.error) {
      sec.append(h('div', { class: 'error-text', text: `上次并行创作失败：${String(s.logResult.error).slice(0, 160)}` }));
    }

    sec.append(
      h('div', { style: { marginTop: '4px' } }, h('span', { class: 'count', text: 'Agent 领章（实时）' })),
      h('div', { class: 'live-log collab-live' })
    );
    parallelBody.append(sec);
    paintLive();
  }

  /* -------------------------------------------------------- 后台任务 */

  async function loadJobs() {
    s.jobsError = '';
    paintJobs();
    try {
      const res = await callApi('jobs', s.key);
      s.jobs = Array.isArray(res && res.items) ? res.items : [];
    } catch (err) {
      s.jobsError = errorText(err, '后台任务加载失败');
    }
    paintJobs();
  }

  function paintJobs() {
    jobsBody.textContent = '';
    const sec = h('div', { class: 'rp-section' });
    const items = s.jobs || [];
    sec.append(
      sectionHead('后台任务', items.length ? `${items.length} 条` : ''),
      h('div', { style: { display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px' } },
        h('button', {
          class: 'btn btn-sm',
          type: 'button',
          html: `${icon('refresh', 12)}<span>刷新</span>`,
          on: { click: () => { loadJobs(); loadUsage(); } }
        })
      )
    );
    if (s.jobsError) {
      sec.append(errorLine(s.jobsError, loadJobs));
    } else if (!s.jobs) {
      sec.append(loadingSkeleton());
    } else if (!items.length) {
      sec.append(h('div', { class: 'hint-text', text: '这本书还没有后台任务记录。' }));
    } else {
      const list = h('div', { style: { display: 'flex', flexDirection: 'column', gap: '1px' } });
      for (const job of items.slice(0, JOB_ROWS)) {
        const st = jobStatus(job);
        list.append(
          h(
            'div',
            { class: 'version-row' },
            h('span', { class: 'dot', dataset: { tone: st.tone } }),
            h('span', { class: 'v-name', text: job.title || job.kind || job.id || '任务' }),
            h('span', { class: 'status-chip', dataset: { tone: st.tone }, text: st.label }),
            h('span', { class: 'v-time', text: job.finished_at ? relTime(job.finished_at) : job.created_at ? relTime(job.created_at) : '' })
          )
        );
      }
      sec.append(list);
    }
    jobsBody.append(sec);
  }

  /* ------------------------------------------------------ 用量与预算 */

  async function loadUsage() {
    s.usageError = '';
    paintUsage();
    try {
      s.usage = await callApi('usage', s.key);
    } catch (err) {
      s.usageError = errorText(err, '用量加载失败');
    }
    paintUsage();
  }

  function paintUsage() {
    usageBody.textContent = '';
    const sec = h('div', { class: 'rp-section' });
    sec.append(sectionHead('用量与预算'));
    if (s.usageError) {
      sec.append(errorLine(s.usageError, loadUsage));
    } else if (!s.usage) {
      sec.append(loadingSkeleton());
    } else {
      const u = (s.usage && s.usage.usage) || {};
      const used = Number(u.total_tokens);
      const cap = Number(s.usage.budget) || 0;
      const remaining = s.usage.remaining == null ? null : Number(s.usage.remaining);
      const pct = Number.isFinite(used) && cap > 0 ? Math.min(100, Math.round((used / cap) * 100)) : 0;
      const kv = h('div', { class: 'kv-grid' });
      for (const [k, v, unit] of [
        ['已用 tokens', numText(u.total_tokens), ''],
        ['输入 / 输出', `${numText(u.total_prompt)} / ${numText(u.total_completion)}`, ''],
        ['调用次数', numText(u.calls), ''],
        ['剩余预算', s.usage.remaining == null ? '—' : numText(remaining), s.usage.exceeded ? '已超' : '']
      ]) {
        kv.append(
          h(
            'div',
            { class: 'kv-cell' },
            h('span', { class: 'kv-k', text: k }),
            h('span', { class: 'kv-v' }, `${v}`, unit ? h('span', { class: 'kv-unit', text: unit }) : null)
          )
        );
      }
      sec.append(kv);
      const bar = h('div', { class: 'progress-track', dataset: s.usage.exceeded ? { tone: 'danger' } : {} });
      bar.append(h('span', { style: { width: `${pct}%`, background: s.usage.exceeded ? 'var(--danger-ink)' : '' } }));
      sec.append(
        bar,
        h('div', {
          class: 'hint-text',
          text: cap > 0 ? `预算 ${fmtNum(cap)} tokens，已用 ${pct}%` : '未设置 token 预算（config.json 的 budget 生效后可见）。'
        })
      );
      if (s.usage.exceeded) {
        sec.append(h('div', { class: 'error-text', text: '本书已超出 token 预算，继续写作可能失败或降级。' }));
      }
    }
    usageBody.append(sec);
  }

  /* ------------------------------------------------------- 生命周期 */

  const onVisibility = () => {
    if (!document.hidden && s.running) pollParallelLog();
  };
  document.addEventListener('visibilitychange', onVisibility);

  const unsub = store.subscribe((state) => {
    if (s.destroyed) return;
    const nextKey = state.currentKey || '';
    if (nextKey !== s.key) {
      s.key = nextKey;
      s.running = false;
      s.logResult = null;
      stopPoll();
      s.jobs = null;
      s.usage = null;
      paintSummary();
      paintParallel();
      loadJobs();
      loadUsage();
      return;
    }
    paintSummary();
    paintLive();
  });

  paintSummary();
  paintAgents();
  paintParallel();
  paintJobs();
  paintUsage();
  loadAgents();
  if (s.key) {
    loadJobs();
    loadUsage();
    /* 面板重建后若后端仍在跑，恢复进度显示。 */
    callApi('parallelLog', s.key)
      .then((res) => {
        if (s.destroyed) return;
        s.running = Boolean(res && res.running);
        s.logResult = res ? res.result : null;
        if (s.running) startPoll();
        paintParallel();
      })
      .catch(() => {
        /* 后端未上线：保持空闲态即可 */
      });
  }

  return {
    el,
    destroy() {
      s.destroyed = true;
      stopPoll();
      document.removeEventListener('visibilitychange', onVisibility);
      unsub();
      el.remove();
    }
  };
}
