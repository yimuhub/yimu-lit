/* 对话区上方的目标条：当前章、规模、进度、创作控制（三态）。 */

import { h, icon, fmtWords } from '../core/dom.js';
import { api, errorText } from '../core/api.js';
import { store, patch, patchUi } from '../core/state.js';
import { toast, popoverMenu } from '../core/ui.js';
import { startJobFeedback, attachJobFeedback, endJobFeedback, workerLabelForMedium } from '../core/jobfeed.js';

/* v4.33.1：九文体的「谁在工作」统一称呼——浮层与日志用同一套名字，不再笼统说"系统" */
function workerLabelOf(work) {
  const med = (work && (work.medium || work.mediumKey)) || (store.get() && store.get().workMedium) || 'novel';
  return workerLabelForMedium(med);
}

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
  /* v4.24.12：状态名不在上面的白名单里（如 unknown / 缺失），但只要还有没写完的章节，
     就仍然可以「继续」——断点续跑是主诉求，不能因为状态字段脏了就让用户失去入口。
     这一条也是「刷新后按钮消失」的最后一道兜底：刷新瞬间 status 可能只回了进度、
     状态字段还没到，此刻仍应按「有活没干完」渲染「继续」。 */
  const done = Number(work && work.done) || 0;
  const total = Number(work && work.total) || 0;
  if (total > done) return 'resume';
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
  /* v4.24.12：key 缺失也要能定位作品——刷新瞬间 work 可能还没并进作品库条目，
     此时退回当前路由的作品 key，按钮就不会整颗不渲染（这是「一刷新就没了」的根因之一）。 */
  const key = (work && work.key) || (ctx && ctx.currentKey) || '';
  if (!work || !key) return null;
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
    /* v4.24.12：空闲/完成态不画任何内容，也不能「只留一个空壳按钮」——
       .btn-creative 有 padding: 0 12px，空壳仍占 25px 宽，在按钮行里表现为
       一段莫名加宽的空白（目标条与右栏两处都中招）。
       用 hidden 属性整体移出布局（CSS 配套 .btn-creative[hidden]{display:none}），
       元素本身留着，store 订阅在状态转成 running/resume 时还能把它点亮。 */
    el.hidden = st === 'idle';
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
      el.title = '正在启动创作线程…';
      el.setAttribute('aria-label', '正在启动创作');
      el.append(
        h('span', { html: icon('play', 13) }),
        h('span', { text: '启动中…' })
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
        try { api.chatNote(key, '暂停', '来自创作按钮'); } catch { /* 入库失败不影响交互 */ }
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
      /* v4.33.1：点击的同一帧就弹「谁在工作」——后端要准备素材 / 生成创作圣经，
         可能 1-2 分钟才吐第一个字，这段时间界面必须有反馈，不能像卡死。 */
      startJobFeedback({ key, workerLabel: workerLabelOf(work),
                         detail: '已收到继续指令，正在启动创作线程…' });
      toast('已收到继续指令，正在启动…', { duration: 2400 });
      if (optimisticTimer) clearTimeout(optimisticTimer);
      optimisticTimer = setTimeout(() => {
        /* 3 分钟兜底。旧值 20s 太短：改编要先提炼全书创作圣经，20 秒后按钮
           自己变回「继续」，用户以为失败又点一次，反而更乱。 */
        if (optimistic === 'resuming') {
          optimistic = null;
          paint(creativeToggleState(currentWork()));
        }
      }, 180000);
      try {
        const res = await api.resume(key);
        if (res && res.job) attachJobFeedback(key, res.job);
        /* v4.20.1：后端明确拒绝（ok:false，如已在创作中/已完成）——按返回体落真实态：
           running=true 会自然收敛成「创作中」，completed 则按钮隐藏；提示用后端原话。 */
        if (res && res.ok === false) {
          optimistic = null;
          if (optimisticTimer) clearTimeout(optimisticTimer);
          endJobFeedback(key, { ok: false, text: res.msg || '暂时无法继续' });
          mergeWorkLive(key, res, false);
          paint(creativeToggleState(currentWork()));
          toast((res && res.msg) || '暂时无法继续', { tone: 'warn', duration: 5000 });
          if (ctx && ctx.refreshWork) ctx.refreshWork();
          return;
        }
        mergeWorkLive(key, res, false);
        /* 后端明确没有真正启动（已完成 / 无需续跑）：立刻恢复按钮，别让「启动中…」空转 */
        if (res && res.running === false) {
          optimistic = null;
          if (optimisticTimer) clearTimeout(optimisticTimer);
          paint(creativeToggleState(currentWork()));
        }
        patchUi({ creatorAction: { key, text: '继续', ts: Date.now() } });
        try { api.chatNote(key, '继续', '来自创作按钮'); } catch { /* 入库失败不影响交互 */ }
        toast('已继续创作', { tone: 'success' });
        if (ctx && ctx.refreshWork) ctx.refreshWork();
      } catch (err) {
        optimistic = null;
        if (optimisticTimer) clearTimeout(optimisticTimer);
        paint(creativeToggleState(currentWork()));
        endJobFeedback(key, { ok: false, text: '启动失败，请重试', error: errorText(err, '恢复失败') });
        toast(errorText(err, '恢复失败'), { tone: 'danger', duration: 5000 });
      }
    }
  });

  paint(phase);
  return el;
}

/* ------------------------------------------------------- v4.24.12 极速续写入口
   需求：「在极速模式和普通模式下还未完成的章节也可以直接采用极速模式进行创作」。

   v4.24.13：与「极速开关」共用同一个 config.express，不是两套并行状态。
   区别只在动作范围——开关只改速度；续写改速度 + 立刻开写。所以续写会让这本书
   保持极速，直到用户点「切回标准」。菜单文案必须写明这一点，否则用户会以为
   只对这批生效，下次点「继续」时发现没有评分/重写，反过来怀疑功能坏了。

   做成「创作速度」菜单而不是裸按钮：
   - ⚡ 极速续写 = 切极速 + 立刻开写剩余章节（重动作，值得一次明确点击）；
   - 🐢 切回标准 = 只改开关不开写（下一批恢复逐章审校与打磨）。
   两个动作放一起，用户不用在别处找「怎么切回去」。 */
export function createExpressButton({ work, ctx, compact = false, plan = null } = {}) {
  const key = (work && work.key) || (ctx && ctx.currentKey) || '';
  if (!work || !key) return null;
  const total = Number(work.total) || 0;
  const done = Number(work.done) || 0;
  /* 没有待写章节 → 没有「续写」可言；正在写时也不让切速度（会和在跑的线程抢口径）。 */
  if (!total || total <= done || work.running) return null;
  const left = total - done;
  const planData = (plan && plan.plan) || {};
  const isExpress = Boolean(planData.express);
  let busy = false;   // v4.24.13：请求在飞时禁止二次点击

  async function run(mode) {
    if (busy) return;
    busy = true;
    btn.disabled = true;
    try {
      const res = await api.expressWrite(key, mode);
      if (res && res.ok === false) {
        toast((res && res.msg) || '操作未生效', { tone: 'warn' });
        if (ctx && ctx.refreshWork) ctx.refreshWork();
        return;
      }
      toast((res && res.msg) || (mode === 'express' ? '已切到极速模式' : '已切回标准模式'), {
        tone: 'success',
        duration: 3200
      });
      if (mode === 'express') {
        try { api.chatNote(key, '极速续写', '来自极速入口'); } catch { /* 入库失败不影响交互 */ }
      }
      if (ctx && ctx.refreshWork) ctx.refreshWork();
      if (ctx && ctx.refreshPlan) ctx.refreshPlan();
    } catch (err) {
      toast(errorText(err, mode === 'express' ? '极速续写失败' : '切换创作速度失败'), { tone: 'danger' });
    } finally {
      busy = false;
      /* 按钮可能已随重绘离开文档；对游离节点设 disabled 无害。 */
      btn.disabled = false;
    }
  }

  const btn = compact
    ? h('button', {
        class: 'icon-btn',
        type: 'button',
        'aria-label': '创作速度',
        title: '创作速度：极速续写 / 切回标准',
        html: icon('zap', 14)
      })
    : h('button', {
        class: 'btn btn-sm',
        type: 'button',
        title: '不评分、不重写，一口气写完剩余章节（会切到极速模式）',
        html: `${icon('zap', 13)}<span>极速续写</span>`
      });
  btn.addEventListener('click', (event) => {
    popoverMenu(
      event.currentTarget,
      [
        {
          label: '极速续写',
          icon: 'zap',
          /* v4.24.13：这是一次「切速度 + 开写」的复合动作，写完后书会保持极速。
             文案必须说清楚，否则用户会以为只对这批生效，下次「继续」发现没评分。 */
          sub: isExpress
            ? `已是极速 · 写完剩余 ${left} 章`
            : `切到极速并写完剩余 ${left} 章（写完后保持极速）`,
          onSelect: () => run('express')
        },
        {
          label: '切回标准',
          icon: 'check',
          sub: isExpress ? '恢复逐章审校与打磨（只改速度，不立即开写）' : '当前已是标准模式',
          disabled: !isExpress,
          onSelect: () => run('standard')
        }
      ],
      { align: 'start', label: '创作速度' }
    );
  });
  return btn;
}

/* ------------------------------------------------------- v4.24.13 极速开关
   与「极速续写」共用同一个后端开关（express.set_plan_flag），区别只有一点：
   - 开关：只改速度，不开写；
   - 续写：改速度 + 立刻开写。
   两个入口以前各写一半字段（开关写顶层 express，续写写 config.mode），
   同一本书会出现「顶层说开、config 说关」；现在后端只有一个写入口。
   v4.24.15：**写作中也能切**。写作线程每章重读 config.express，切档只影响还没写的章，
   所以不再禁用按钮——以前「先暂停再切」把用户卡死在「正在写」状态里。
   非小说引擎仍在运行中拒绝（阶段在启动时算定），后端会返回 ok:false + 说明。 */
export function createExpressSwitch({ work, ctx, plan = null } = {}) {
  const key = (work && work.key) || (ctx && ctx.currentKey) || '';
  if (!work || !key) return null;
  const planData = (plan && plan.plan) || {};
  const on = Boolean(planData.express);
  const running = Boolean(work.running);
  const when = running ? '，当前这一章写完即生效' : '；只改速度，不会立即开写';
  const btn = h('button', {
    class: 'btn btn-sm' + (on ? ' btn-accent' : ''),
    type: 'button',
    title: on
      ? `极速模式已开启（不评分不重写）——点击关闭${when}`
      : `开启极速模式：剩余章节不评分、不重写${when}`,
    html: `${icon('zap', 13)}<span>极速${on ? ' · 开' : ''}</span>`
  });
  btn.addEventListener('click', async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      const res = await api.expressToggle(key, !on);
      if (res && res.ok === false) {
        toast((res && res.msg) || '切换未生效', { tone: 'warn' });
        btn.disabled = false;   // 运行中拒绝：保持可点，暂停后可再点
        return;
      }
      toast((res && res.msg) || (res && res.express ? '极速模式已开启' : '已关闭极速模式'),
            { tone: res && res.express ? 'success' : 'info', duration: 4200 });
      if (ctx && ctx.refreshState) ctx.refreshState();
      if (ctx && ctx.refreshPlan) ctx.refreshPlan();
      if (ctx && ctx.refreshWork) ctx.refreshWork();
      /* 刷新失败时按钮会留在禁用态；接口是「显式置位」而非取反，重复点也幂等，安全解锁。 */
      btn.disabled = false;
    } catch (err) {
      toast(errorText(err, '切换失败'), { tone: 'danger' });
      btn.disabled = false;
    }
  });
  return btn;
}

export function renderPlanBar({ work, plan, ctx, onOpenPlan }) {
  if (!work) return null;
  const total = work.total || 0;
  const done = work.done || 0;
  const pct = total ? Math.round((done / total) * 100) : 0;
  const planData = (plan && plan.plan) || {};
  const scale = planData.scale_label || (planData.target_words ? fmtWords(planData.target_words) : '');
  /* v4.24.0：极速模式要一眼可辨——产出是初稿，不标出来会让人误以为已经过审校。 */
  const mode = planData.express ? '⚡ 极速模式 · 初稿' : (planData.mode || '滚动规划');
  /* v4.14.0：章节标签按文体定制（小说用「章」、剧本用「场」、诗词用「首」…） */
  const MEDIUM_UNITS = {
    novel: '章', script: '场', poetry: '首', prose: '阶段',
    thesis: '章', children: '篇', news: '阶段', copy: '阶段', other: '阶段'
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
  /* v4.24.12：极速续写入口——普通模式/极速模式都能把剩余章节用极速方式写完。 */
  const expressBtn = createExpressButton({ work, ctx, compact: true, plan });
  if (expressBtn) actions.append(expressBtn);
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
