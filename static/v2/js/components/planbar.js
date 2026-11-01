/* 对话区上方的目标条：当前章、规模、进度、创作控制（三态）。 */

import { h, icon, fmtWords } from '../core/dom.js';
import { api, errorText } from '../core/api.js';
import { store, patch, patchUi } from '../core/state.js';
import { toast } from '../core/ui.js';

/* ------------------------------------------------------- v4.20.0 创作控制三态按钮
   全部文体、全部入口（目标条 / 右栏作品页签）共用的统一交互规范：
   - 创作中（running）→「创作中」+ 竖杠闪烁，点击请求暂停（api.pause）；
   - 暂停请求已发出（running && paused）→「暂停中…」disabled，等线程走到安全点；
   - 已暂停 / 线程中断 →「继续」+ play，点击恢复（api.resume，后端已全文体统一）；
   - 空闲 / 完成 → 不渲染按钮。
   点击后立即乐观反馈并防连点；成功后用返回体乐观落库，等轮询（2.5s）确认后
   自然落到真实状态；失败 toast 并还原。 */

/* 由 work 推导创作控制态：running=创作中 / pausing=暂停请求已发出 / resume=待继续 / idle=空闲或完成 */
function creativeToggleState(work) {
  const running = Boolean(work && work.running);
  const paused = Boolean(work && work.paused);
  const st = String((work && work.state) || '');
  if (running && paused) return 'pausing';
  if (running) return 'running';
  if (paused || st === 'paused') return 'resume';
  /* 线程中断（服务重启 / 断点）但进度未完：一键续跑也归入「继续」态 */
  if (['planning', 'writing', 'in_progress', 'validating'].includes(st)) return 'resume';
  return 'idle';
}

/* 用操作返回体（含 running/paused/state）乐观合并进 store.work，让目标条 / 顶栏
   在下一次轮询前先落到真实态；返回体缺字段时按操作语义补齐（pause→paused:true）。 */
function mergeWorkLive(key, res, expectPaused) {
  try {
    patch('work', (w) => {
      if (!w || w.key !== key) return w;
      return {
        ...w,
        running: res && res.running != null ? Boolean(res.running) : w.running,
        paused: res && res.paused != null ? Boolean(res.paused) : Boolean(expectPaused),
        state: res && res.state ? res.state : w.state
      };
    });
  } catch {
    /* store 未就绪（理论上不会）时静默，等轮询兜底 */
  }
}

/* 创作控制按钮工厂：planbar / rightpanel 共用，保证所有入口文案、图标、
   乐观反馈与防连点行为完全一致。idle 态返回 null（调用方不渲染）。
   - compact：目标条里的紧凑尺寸；
   - ctx.refreshWork：操作成功后主动拉一次 /api/status，加速各处收敛。
   按钮自带 store 订阅：右栏的 rightSig 不含 paused（app.js 不可改），乐观态之后
   由这里按最新 work 收敛；元素离开文档后自动退订，不残留监听。 */
export function createCreativeToggle({ work, ctx, compact = false } = {}) {
  if (!work || !work.key) return null;
  const key = work.key;
  let phase = creativeToggleState(work); // 当前视觉阶段
  let optimistic = null; // 'pausing' | 'resuming'：乐观期间屏蔽重复点击、且不被旧轮询数据打回
  let optimisticTimer = 0;

  const currentWork = () => {
    const s = store.get();
    if (s.work && s.work.key === key) return s.work;
    return s.library.items.find((i) => i.key === key) || work;
  };

  const el = h('button', { class: 'btn-creative', type: 'button' });
  if (compact) el.classList.add('btn-creative-compact');

  function paint(st) {
    phase = st;
    el.disabled = false;
    el.classList.remove('btn-creative-running', 'btn-creative-resume', 'btn-creative-busy');
    el.textContent = '';
    if (st === 'running') {
      el.classList.add('btn-creative-running');
      el.title = '点击请求暂停，当前章节写完后停止';
      el.setAttribute('aria-label', '暂停创作');
      el.append(
        h('span', { html: icon('pause', 13, 'creative-bars') }),
        h('span', { text: '创作中' })
      );
    } else if (st === 'pausing') {
      el.classList.add('btn-creative-running', 'btn-creative-busy');
      el.disabled = true;
      el.title = '暂停请求已发出，当前章节完成后停止';
      el.setAttribute('aria-label', '暂停请求已发出');
      el.append(
        h('span', { html: icon('pause', 13, 'creative-bars') }),
        h('span', { text: '暂停中…' })
      );
    } else if (st === 'resume') {
      el.classList.add('btn-creative-resume');
      el.title = '从上次中断处继续创作';
      el.setAttribute('aria-label', '继续创作');
      el.append(
        h('span', { html: icon('play', 13) }),
        h('span', { text: '继续' })
      );
    } else if (st === 'resuming') {
      el.classList.add('btn-creative-resume', 'btn-creative-busy');
      el.disabled = true;
      el.title = '正在恢复创作…';
      el.setAttribute('aria-label', '正在恢复创作');
      el.append(
        h('span', { html: icon('play', 13) }),
        h('span', { text: '正在恢复…' })
      );
    }
  }

  /* store 变化时的收敛逻辑：确认了乐观态才落地，避免被 2.5s 轮询里的旧数据打回。 */
  function sync(w) {
    if (!w || w.key !== key) return;
    const st = creativeToggleState(w);
    if (optimistic === 'pausing') {
      /* 暂停已确认：后端停在同一安全点（resume）或已进入暂停中（pausing） */
      if (st === 'pausing' || st === 'resume') {
        optimistic = null;
        if (optimisticTimer) clearTimeout(optimisticTimer);
        paint(st);
      }
      return;
    }
    if (optimistic === 'resuming') {
      if (st === 'running' || st === 'pausing') {
        optimistic = null;
        if (optimisticTimer) clearTimeout(optimisticTimer);
        paint(st);
      }
      return;
    }
    if (st !== phase) paint(st);
  }

  /* 挂载期订阅：每次 store patch 都按最新 work 校准一次；元素离场即退订。
     （首次连接前不退订——创建与 append 在同一 tick，但防御未来异步挂载的调用方） */
  let mountedOnce = false;
  const unsub = store.subscribe((state) => {
    if (!el.isConnected) {
      if (mountedOnce) {
        unsub();
        if (optimisticTimer) clearTimeout(optimisticTimer);
      }
      return;
    }
    mountedOnce = true;
    const w =
      state.work && state.work.key === key
        ? state.work
        : (state.library.items || []).find((i) => i.key === key);
    if (w) sync(w);
  });

  el.addEventListener('click', async () => {
    if (el.disabled || optimistic) return;
    const st = phase;
    if (st === 'running') {
      /* 乐观反馈：立刻切「暂停中…」并禁用，防连点 */
      optimistic = 'pausing';
      paint('pausing');
      toast('暂停请求已发出，当前章节完成后停止', { duration: 2600 });
      if (optimisticTimer) clearTimeout(optimisticTimer);
      optimisticTimer = setTimeout(() => {
        /* 兜底：20s 内没等到确认（网络异常等）就按服务端真实态恢复按钮 */
        if (optimistic === 'pausing') {
          optimistic = null;
          paint(creativeToggleState(currentWork()));
        }
      }, 20000);
      try {
        const res = await api.pause(key);
        /* v4.20.1：后端明确拒绝（ok:false）时不再装成功——按返回体落真实态并提示原因 */
        if (res && res.ok === false) {
          optimistic = null;
          if (optimisticTimer) clearTimeout(optimisticTimer);
          mergeWorkLive(key, res, true);
          paint(creativeToggleState(currentWork()));
          toast((res && res.msg) || '暂停请求未生效', { tone: 'warn' });
          if (ctx && ctx.refreshWork) ctx.refreshWork();
          return;
        }
        mergeWorkLive(key, res, true);
        patchUi({ creatorAction: { key, text: '暂停', ts: Date.now() } });
        if (ctx && ctx.refreshWork) ctx.refreshWork();
      } catch (err) {
        optimistic = null;
        if (optimisticTimer) clearTimeout(optimisticTimer);
        paint(creativeToggleState(currentWork()));
        toast(errorText(err, '暂停请求失败'), { tone: 'danger' });
      }
    } else if (st === 'resume') {
      optimistic = 'resuming';
      paint('resuming');
      toast('正在恢复创作…', { duration: 2000 });
      if (optimisticTimer) clearTimeout(optimisticTimer);
      optimisticTimer = setTimeout(() => {
        if (optimistic === 'resuming') {
          optimistic = null;
          paint(creativeToggleState(currentWork()));
        }
      }, 20000);
      try {
        const res = await api.resume(key);
        /* v4.20.1：后端明确拒绝（ok:false，如已在创作中/已完成）——按返回体落真实态：
           running=true 会自然收敛成「创作中」，completed 则按钮隐藏；提示用后端原话。 */
        if (res && res.ok === false) {
          optimistic = null;
          if (optimisticTimer) clearTimeout(optimisticTimer);
          mergeWorkLive(key, res, false);
          paint(creativeToggleState(currentWork()));
          toast((res && res.msg) || '暂时无法继续', { tone: 'warn' });
          if (ctx && ctx.refreshWork) ctx.refreshWork();
          return;
        }
        mergeWorkLive(key, res, false);
        patchUi({ creatorAction: { key, text: '继续', ts: Date.now() } });
        toast('已继续创作', { tone: 'success' });
        if (ctx && ctx.refreshWork) ctx.refreshWork();
      } catch (err) {
        optimistic = null;
        if (optimisticTimer) clearTimeout(optimisticTimer);
        paint(creativeToggleState(currentWork()));
        toast(errorText(err, '恢复失败'), { tone: 'danger' });
      }
    }
  });

  paint(phase);
  return el;
}

export function renderPlanBar({ work, plan, ctx, onOpenPlan }) {
  if (!work) return null;
  const total = work.total || 0;
  const done = work.done || 0;
  const pct = total ? Math.round((done / total) * 100) : 0;
  const planData = (plan && plan.plan) || {};
  const scale = planData.scale_label || (planData.target_words ? fmtWords(planData.target_words) : '');
  const mode = planData.mode || '滚动规划';
  /* v4.14.0：章节标签按文体定制（小说用「章」、剧本用「场」、诗词用「首」…） */
  const MEDIUM_UNITS = {
    novel: '章', script: '场', poetry: '首', prose: '阶段',
    thesis: '章', children: '篇', news: '篇', copy: '篇', other: '节'
  };
  const unit = MEDIUM_UNITS[work.medium] || '章';
  let chapterLabel = `第 ${Math.min(done + 1, total || done + 1)} ${unit}`;
  const cards = planData.chapters || [];
  /* 章节状态两套词表都认：写作计划 in_progress / 章卡 writing。 */
  const current = cards.find((c) => c.status === 'in_progress' || c.status === 'writing');
  if (current && current.title) chapterLabel = `第 ${current.n} ${unit} · ${String(current.title).replace(/^第\d+章\s*/, '')}`;

  const bar = h('div', { class: 'plan-bar' });
  const inner = h('div', { class: 'plan-bar-inner' });
  inner.append(
    h(
      'span',
      { class: 'goal-label' },
      h('span', { html: icon('target', 13) }),
      h('span', { text: '本轮目标' })
    ),
    h('span', { class: 'goal-text', text: `${chapterLabel} · ${scale ? scale + '字 · ' : ''}${mode}`, title: chapterLabel }),
    h('span', { class: 'goal-progress progress-track', html: `<span style="width:${pct}%"></span>` }),
    h('span', { class: 'goal-pct', text: `${pct}%` })
  );

  const actions = h('span', { class: 'goal-actions' });
  /* v4.20.0：三态创作控制（创作中→暂停 / 已暂停→继续 / 空闲→隐藏），
     与右栏、其他文体入口同一交互规范；mock 模式也走同一工厂（mock 接口已实现 pause/resume）。 */
  const toggle = createCreativeToggle({ work, ctx, compact: true });
  if (toggle) actions.append(toggle);
  actions.append(
    h('button', {
      class: 'icon-btn',
      type: 'button',
      'aria-label': '查看完整规划',
      title: '查看完整规划',
      html: icon('list', 14),
      on: { click: () => (onOpenPlan ? onOpenPlan() : ctx && ctx.openRight('plan')) }
    })
  );
  inner.append(actions);
  bar.append(inner);
  return bar;
}
