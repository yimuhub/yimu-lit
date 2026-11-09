/* 实时作业反馈（v4.33.1）
 *
 * 用户实拍：点「继续」后界面一动不动，等 30 秒以上才开写，完全不知道是在工作
 * 还是卡死。这里给所有文体一套统一的**立即反馈**：
 *
 *   点击瞬间 → 右下角弹出「剧本引擎 · 正在接手…」卡片（不等后端）
 *   后端返回 → 卡片显示真实阶段（准备素材 / 提炼圣经 / 写第 3/50 集）
 *   作业结束 → ✅ 已完成自动收起；⚠️ 未完成保留原因 + 关闭按钮
 *
 * 卡片不阻塞任何操作，点「收起」只是隐藏视觉，作业仍在跑（顶部按钮仍是「创作中」）。
 * 所有样式走内联 + 主题变量，明暗主题自动适配，不新增全局 CSS 类。
 */

const API = '/api/job';
const POLL_MS = 1400;
const TICK_MS = 500;
const AUTO_HIDE_MS = 4200;

const cards = new Map();   // key -> { el, timer, ticker, key, closed }
let layer = null;

/* 九文体统一称呼：浮层、按钮、日志说同一个名字，用户不用猜"系统"是谁。 */
const MEDIUM_WORKER = {
  novel: '小说引擎', script: '剧本引擎', poetry: '诗词引擎', prose: '散文引擎',
  thesis: '论文引擎', children: '儿童文学引擎', news: '新闻稿引擎',
  copy: '文案引擎', other: '创作引擎'
};

export function workerLabelForMedium(medium) {
  return MEDIUM_WORKER[String(medium || '').trim()] || '创作引擎';
}

function ensureStyle() {
  const id = 'yimu-jobfeed-keyframes';
  if (document.getElementById(id)) return;
  const st = document.createElement('style');
  st.id = id;
  st.textContent = '@keyframes yimu-job-spin{to{transform:rotate(360deg)}}'
    + '@keyframes yimu-job-in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}';
  document.head.append(st);
}

function ensureLayer() {
  ensureStyle();
  if (layer && layer.isConnected) return layer;
  layer = document.createElement('div');
  layer.setAttribute('data-yimu-job-layer', '1');
  Object.assign(layer.style, {
    position: 'fixed', right: '18px', bottom: '18px', zIndex: '9999',
    display: 'flex', flexDirection: 'column', gap: '10px',
    maxWidth: 'min(360px, calc(100vw - 36px))', pointerEvents: 'none'
  });
  document.body.append(layer);
  return layer;
}

function fmtElapsed(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function buildCard(key, opts) {
  ensureLayer();
  const el = document.createElement('div');
  Object.assign(el.style, {
    pointerEvents: 'auto', animation: 'yimu-job-in .18s ease-out',
    background: 'var(--bg-elevated)', color: 'var(--ink-primary)',
    border: '1px solid var(--line-2)', borderLeft: '3px solid var(--accent)',
    borderRadius: '12px', padding: '12px 14px', minWidth: '280px',
    boxShadow: '0 10px 30px rgba(0,0,0,.16)', fontFamily: 'var(--font-ui)',
    fontSize: '13px', lineHeight: '1.5'
  });
  const head = document.createElement('div');
  Object.assign(head.style, { display: 'flex', alignItems: 'center', gap: '8px' });
  const spin = document.createElement('span');
  Object.assign(spin.style, {
    width: '13px', height: '13px', flex: '0 0 auto', borderRadius: '50%',
    border: '2px solid var(--accent-soft-2)', borderTopColor: 'var(--accent)',
    animation: 'yimu-job-spin .85s linear infinite', display: 'inline-block'
  });
  const title = document.createElement('strong');
  Object.assign(title.style, { fontSize: '13px', fontWeight: '600', flex: '1 1 auto' });
  title.textContent = (opts.workerLabel || '创作引擎') + ' · 正在工作';
  const time = document.createElement('span');
  Object.assign(time.style, {
    fontSize: '11px', color: 'var(--ink-tertiary)', fontVariantNumeric: 'tabular-nums'
  });
  time.textContent = '0s';
  head.append(spin, title, time);

  const detail = document.createElement('div');
  Object.assign(detail.style, {
    marginTop: '6px', color: 'var(--ink-secondary)', fontSize: '12px',
    whiteSpace: 'pre-wrap', wordBreak: 'break-word'
  });
  detail.textContent = opts.detail || '正在启动…';

  const bar = document.createElement('div');
  Object.assign(bar.style, {
    marginTop: '8px', height: '4px', borderRadius: '999px',
    background: 'var(--bg-chip)', overflow: 'hidden', display: 'none'
  });
  const fill = document.createElement('div');
  Object.assign(fill.style, {
    height: '100%', width: '0%', background: 'var(--accent)',
    transition: 'width .3s var(--ease)'
  });
  bar.append(fill);

  const foot = document.createElement('div');
  Object.assign(foot.style, {
    marginTop: '8px', display: 'flex', alignItems: 'center',
    justifyContent: 'space-between', gap: '10px'
  });
  const count = document.createElement('span');
  Object.assign(count.style, { fontSize: '11px', color: 'var(--ink-tertiary)' });
  count.textContent = '';
  const hide = document.createElement('button');
  hide.type = 'button';
  hide.textContent = '收起';
  Object.assign(hide.style, {
    border: 'none', background: 'transparent', color: 'var(--ink-tertiary)',
    fontSize: '11px', cursor: 'pointer', padding: '2px 4px'
  });
  foot.append(count, hide);

  el.append(head, detail, bar, foot);
  layer.append(el);

  const card = { el, detail, time, count, bar, fill, title, spin, key, timer: 0, ticker: 0, closed: false, opts };
  hide.addEventListener('click', () => {
    card.closed = true;
    stopTimers(card);
    el.remove();
    cards.delete(key);
  });
  return card;
}

function stopTimers(card) {
  if (card.timer) { clearTimeout(card.timer); card.timer = 0; }
  if (card.ticker) { clearInterval(card.ticker); card.ticker = 0; }
}

function render(card, job) {
  if (!card || card.closed) return;
  const label = job.worker_label || card.opts.workerLabel || '创作引擎';
  const phases = {
    starting: '正在接手', prepare: '准备素材', bible: '提炼创作圣经',
    writing: '正在写作', storyboard: '拆分镜', pausing: '正在暂停',
    done: '已完成', failed: '需要处理'
  };
  const phaseText = phases[job.phase] || '';
  card.title.textContent = `${label} · ${phaseText || '正在工作'}`;
  if (job.detail) card.detail.textContent = job.detail;
  card.time.textContent = fmtElapsed(job.elapsed);
  const total = Number(job.total) || 0;
  const done = Number(job.done) || 0;
  if (total > 0) {
    card.bar.style.display = 'block';
    card.fill.style.width = `${Math.min(100, Math.round(done / total * 100))}%`;
    card.count.textContent = `${done}/${total}`;
  } else {
    card.bar.style.display = 'none';
    card.count.textContent = '';
  }
  const finished = Boolean(job.finished);
  const ok = job.ok !== false;
  card.spin.style.animation = finished ? 'none' : 'yimu-job-spin .85s linear infinite';
  card.spin.style.borderColor = finished
    ? (ok ? 'var(--success)' : 'var(--warn)')
    : 'var(--accent-soft-2)';
  card.spin.style.borderTopColor = finished
    ? (ok ? 'var(--success)' : 'var(--warn)')
    : 'var(--accent)';
  card.el.style.borderLeftColor = finished
    ? (ok ? 'var(--success)' : 'var(--warn)')
    : 'var(--accent)';
  if (finished) {
    card.el.dataset.finished = '1';
    card.detail.textContent = job.error || job.detail || (ok ? '已完成' : '未完成');
    stopTimers(card);
    if (ok) {
      setTimeout(() => {
        if (!card.closed) {
          card.el.remove();
          cards.delete(card.key);
        }
      }, AUTO_HIDE_MS);
    }
  }
}

function poll(card) {
  if (!card || card.closed) return;
  const run = async () => {
    if (card.closed) return;
    try {
      const res = await fetch(`${API}?key=${encodeURIComponent(card.key)}`, {
        credentials: 'same-origin', headers: { Accept: 'application/json' }
      });
      if (res.ok) {
        const data = await res.json();
        if (data && data.job) {
          render(card, data.job);
          if (data.job.finished) return;
        }
      }
    } catch {
      /* 网络抖动：保持当前文案，下个周期再试 */
    }
    card.timer = setTimeout(run, POLL_MS);
  };
  run();
}

function startTicker(card) {
  const t0 = Date.now();
  card.ticker = setInterval(() => {
    if (card.closed || card.el.dataset.finished === '1') return;
    card.time.textContent = fmtElapsed((Date.now() - t0) / 1000);
  }, TICK_MS);
}

/**
 * 立即显示「谁在工作」。必须在 await 接口之前调用——用户点下去 100ms 内就要看到。
 * 同一 key 重复调用复用现有卡片（双击不会叠三个）。
 */
export function startJobFeedback({ key, workerLabel, detail, total = 0, done = 0 } = {}) {
  if (!key) return null;
  const old = cards.get(key);
  if (old && !old.closed) {
    if (detail) old.detail.textContent = detail;
    if (workerLabel) old.title.textContent = `${workerLabel} · 正在工作`;
    return old;
  }
  const card = buildCard(key, { workerLabel, detail });
  cards.set(key, card);
  render(card, { worker_label: workerLabel, detail, total, done, phase: 'starting', elapsed: 0 });
  startTicker(card);
  poll(card);
  return card;
}

/** 用接口返回体里的 job 覆盖卡片（后端已带真实阶段，不必等下一次轮询）。 */
export function attachJobFeedback(key, job) {
  if (!key || !job) return;
  const card = cards.get(key) || startJobFeedback({ key, workerLabel: job.worker_label });
  render(card, job);
  if (job.finished) card.el.dataset.finished = '1';
}

/** 手动结束（接口 ok:false / 抛异常时用）：弹窗说明原因，而不是静默消失。 */
export function endJobFeedback(key, { ok = true, text = '', error = '' } = {}) {
  if (!key) return;
  const card = cards.get(key);
  if (!card) return;
  render(card, {
    worker_label: card.opts.workerLabel,
    detail: text || (ok ? '已完成' : '未完成'),
    error, finished: true, ok
  });
  card.el.dataset.finished = '1';
}

/** 只更新文案（例如「已收到指令，正在排队…」）。 */
export function updateJobFeedback(key, detail) {
  const card = cards.get(key);
  if (card && !card.closed && detail) card.detail.textContent = detail;
}

export function hasJobFeedback(key) {
  const card = cards.get(key);
  return Boolean(card && !card.closed);
}
