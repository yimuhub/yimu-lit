/* 对话区上方的目标条：当前章、规模、进度、暂停/继续。 */

import { h, icon, fmtWords } from '../core/dom.js';
import { api, errorText, IS_MOCK } from '../core/api.js';
import { toast } from '../core/ui.js';

export function renderPlanBar({ work, plan, ctx, onOpenPlan }) {
  if (!work) return null;
  const total = work.total || 0;
  const done = work.done || 0;
  const pct = total ? Math.round((done / total) * 100) : 0;
  const planData = (plan && plan.plan) || {};
  const scale = planData.scale_label || (planData.target_words ? fmtWords(planData.target_words) : '');
  const mode = planData.mode || '滚动规划';
  let chapterLabel = `第 ${Math.min(done + 1, total || done + 1)} 章`;
  const cards = planData.chapters || [];
  const current = cards.find((c) => c.status === 'in_progress');
  if (current && current.title) chapterLabel = `第 ${current.n} 章 · ${String(current.title).replace(/^第\d+章\s*/, '')}`;

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
  const toggle = h('button', {
    class: 'icon-btn',
    type: 'button',
    'aria-label': work.running ? '暂停创作' : '继续创作',
    title: work.running ? '暂停创作' : '继续创作',
    html: icon(work.running ? 'pause' : 'play', 14)
  });
  toggle.addEventListener('click', async () => {
    if (IS_MOCK) {
      work.running = !work.running;
      toast(work.running ? '已继续（演示）' : '已暂停（演示）', { duration: 1500 });
      if (ctx && ctx.refreshWork) ctx.refreshWork();
      return;
    }
    try {
      if (work.running) await api.pause(work.key);
      else await api.resume(work.key);
      toast(work.running ? '已暂停，当前章会在安全点停下' : '已继续创作', { tone: 'success' });
      if (ctx && ctx.refreshWork) ctx.refreshWork();
    } catch (err) {
      toast(errorText(err, '操作失败'), { tone: 'danger' });
    }
  });
  actions.append(toggle);
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
