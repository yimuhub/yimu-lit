/* 技能进化：写作画像、候选技能评测转正、版本回滚与反馈样本。 */

import { h, icon, relTime, fmtPct } from '../core/dom.js';
import { store } from '../core/state.js';
import { api, errorText } from '../core/api.js';
import { toast, confirmDialog } from '../core/ui.js';

export function renderEvolution(container, ctx) {
  const view = h('div', { class: 'view-scroll' });
  const pad = h('div', { class: 'view-pad' });
  view.append(pad);
  container.append(view);

  const model = {
    skills: null,
    learn: null,
    feedbackItems: [],
    growth: null,
    error: '',
    proposing: false
  };

  pad.append(
    h(
      'div',
      { class: 'evo-head' },
      h(
        'div',
        { class: 'evo-head-copy' },
        h('h1', { text: '技能进化' }),
        h('p', { class: 'hint-text', text: '每一章的评分都会沉淀成你的写作数据：下面是「越写越强」的实测曲线；数据提炼出的候选技能，评测达标后由你亲手转正——下一本书写得更好。' })
      ),
      h(
        'button',
        {
          class: 'btn btn-primary',
          type: 'button',
          html: model.proposing
            ? '<span class="wiz-loading"><span class="spinner"></span>生成中…</span>'
            : `${icon('activity', 14)}<span>从写作数据生成候选</span>`,
          disabled: model.proposing,
          on: { click: proposeWriting }
        }
      )
    )
  );

  const body = h('div', { class: 'evo-view' });
  pad.append(body);

  /* v4.27.0：从真实写作数据（跨书逐章评分弱点）生成进化候选 */
  async function proposeWriting() {
    if (model.proposing) return;
    model.proposing = true;
    paint();
    try {
      await api.proposeSkill({ from_writing_data: true, medium: 'novel' });
      toast('已从写作数据生成候选技能，评测通过后可转正', { tone: 'success' });
      await load();
    } catch (err) {
      toast(errorText(err, '生成失败——可能写作数据还不够'), { tone: 'danger' });
    }
    model.proposing = false;
    paint();
  }

  async function load() {
    model.error = '';
    paint();
    const [skillsRes, learnRes, feedbackRes, growthRes] = await Promise.allSettled([
      api.skillsEvolution(),
      api.learn(),
      api.feedbackList({ limit: 20 }),
      api.skillGrowth()
    ]);
    if (skillsRes.status === 'fulfilled') model.skills = skillsRes.value;
    else if (skillsRes.reason && !skillsRes.reason.isMissing) model.error = errorText(skillsRes.reason, '技能数据加载失败');
    if (learnRes.status === 'fulfilled') model.learn = learnRes.value;
    if (growthRes.status === 'fulfilled' && growthRes.value && growthRes.value.ok) {
      model.growth = growthRes.value;
    } else if (growthRes.status === 'rejected') {
      model.growth = null;
    }
    if (feedbackRes.status === 'fulfilled' && feedbackRes.value) {
      const v = feedbackRes.value;
      model.feedbackItems = Array.isArray(v)
        ? v
        : Array.isArray(v.items)
          ? v.items
          : Array.isArray(v.feedback)
            ? v.feedback
            : Array.isArray(v.notes)
              ? v.notes
              : Array.isArray(v.summary)
                ? v.summary
                : [];
    }
    paint();
  }

  function paint() {
    body.textContent = '';
    if (model.error) {
      body.append(
        h(
          'div',
          { class: 'lib-empty empty-state', role: 'alert' },
          h('div', { class: 'empty-icon', html: icon('alert', 18) }),
          h('h3', { text: '技能数据没有加载出来' }),
          h('p', { text: model.error }),
          h('button', { class: 'btn btn-sm', type: 'button', text: '重试', on: { click: load } })
        )
      );
      return;
    }
    if (!model.skills) {
      for (let i = 0; i < 3; i += 1) {
        body.append(
          h(
            'div',
            { class: 'evo-card' },
            h('div', { class: 'skeleton skeleton-line', style: { width: `${40 + i * 15}%` } }),
            h('div', { class: 'skeleton skeleton-line w-40' })
          )
        );
      }
      return;
    }

    paintGrowth();
    paintProfile();
    paintStats();
    paintCandidates();
    paintHistory();
    paintFeedback();
  }

  /* ------------------------------------------- v4.27.0 越写越强（成长仪表） */

  function fmtNum(n) {
    if (n == null) return '—';
    if (n >= 10000) return `${(n / 10000).toFixed(1)} 万`;
    return String(n);
  }

  function paintGrowth() {
    const g = model.growth;
    if (!g || !g.total || !g.total.chaptersScored) {
      const card = h('div', { class: 'evo-card evo-growth' });
      card.append(h('h2', {}, h('span', { html: icon('activity', 14) }), h('span', { text: '写作水平成长' })));
      card.append(h('div', { class: 'hint-text', text: '还没有带评分的章节。写完第一章起，这里会画出你「越写越强」的实测曲线。' }));
      body.append(card);
      return;
    }
    const t = g.total;
    const card = h('div', { class: 'evo-card evo-growth' });
    card.append(h('h2', {}, h('span', { html: icon('activity', 14) }), h('span', { text: '写作水平成长' }),
      h('span', { class: 'hint-text', text: `（基于 ${t.chaptersScored} 个带评分章节的真实数据）` })));

    /* 三张大数字卡 */
    const big = h('div', { class: 'evo-big-grid' });
    big.append(h('div', { class: 'evo-big' },
      h('span', { class: 'stat-k', text: '累计创作' }),
      h('span', { class: 'evo-big-v' }, `${t.books}`, h('small', { text: ' 本书' })),
      h('span', { class: 'hint-text', text: `${t.chaptersScored} 章已评分 · ${fmtNum(t.totalWords)} 字` })));
    const delta = Number(t.delta) || 0;
    big.append(h('div', { class: 'evo-big' },
      h('span', { class: 'stat-k', text: '写作水平变化' }),
      h('span', { class: `evo-big-v ${delta >= 0 ? 'up' : 'down'}` },
        `${delta >= 0 ? '+' : ''}${delta}`, h('small', { text: ' 分' })),
      h('span', { class: 'hint-text', text: `早期均分 ${t.earlyAvg ?? '—'} → 最近均分 ${t.recentAvg ?? '—'}` })));
    big.append(h('div', { class: 'evo-big' },
      h('span', { class: 'stat-k', text: '进化沉淀' }),
      h('span', { class: 'evo-big-v' }, `${t.evolvedSkills}`, h('small', { text: ' 个技能' })),
      h('span', { class: 'hint-text', text: '转正后自动注入后续写作' })));
    card.append(big);

    /* 成长曲线（滑动平均分，SVG 折线 + 80 达标线） */
    const series = (g.series || []).slice(-160);
    if (series.length >= 3) {
      const W = 640, H = 150, PAD = 6;
      const lo = Math.min(45, Math.min(...series) - 3), hi = Math.max(84, Math.max(...series) + 2);
      const xs = (i) => PAD + (i * (W - PAD * 2)) / (series.length - 1);
      const ys = (v) => H - PAD - ((v - lo) * (H - PAD * 2)) / (hi - lo);
      const pts = series.map((v, i) => `${xs(i).toFixed(1)},${ys(v).toFixed(1)}`).join(' ');
      const y80 = ys(80);
      const svg = h('svg', {
        viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', class: 'evo-trend-svg', role: 'img',
        'aria-label': '逐章评分滑动平均趋势'
      });
      svg.append(h('line', { x1: PAD, y1: y80, x2: W - PAD, y2: y80, class: 'evo-trend-goal' }));
      svg.append(h('polyline', { points: pts, class: 'evo-trend-line' }));
      svg.append(h('text', { x: W - PAD - 2, y: y80 - 4, class: 'evo-trend-goal-txt', text: '达标线 80' }));
      const chart = h('div', { class: 'evo-trend' }, svg,
        h('div', { class: 'evo-trend-cap' },
          h('span', { text: `第 1 章 ${series[0]} 分` }),
          h('span', { text: `第 ${t.chaptersScored} 章 ${series[series.length - 1]} 分` })));
      card.append(chart);
    }

    /* 六维对比：早期 vs 最近 */
    const cmp = g.dimsCompare || {};
    const dims = Object.entries(cmp);
    if (dims.length) {
      card.append(h('div', { class: 'evo-sub', text: '六维成长（最早 30% 章 vs 最近 30% 章）' }));
      const list = h('div', { class: 'evo-dims' });
      for (const [dim, d] of dims) {
        const up = (d.delta || 0) >= 0;
        const maxv = Math.max(d.early || 0, d.recent || 0, 10);
        const row = h('div', { class: 'evo-dim-row' },
          h('span', { class: 'evo-dim-name', text: dim }),
          h('div', { class: 'evo-dim-bars' },
            h('div', { class: 'evo-dim-bar-wrap' }, h('div', { class: 'evo-dim-bar early', style: { width: `${((d.early || 0) / maxv) * 100}%` } })),
            h('div', { class: 'evo-dim-bar-wrap' }, h('div', { class: 'evo-dim-bar recent', style: { width: `${((d.recent || 0) / maxv) * 100}%` } }))),
          h('span', { class: `evo-dim-delta ${up ? 'up' : 'down'}`, text: `${d.early} → ${d.recent}（${up ? '+' : ''}${d.delta}）` }));
        list.append(row);
      }
      card.append(list);
    }

    /* 进化信号 + 一键生成 */
    const weak = g.weakTop || [];
    if (weak.length) {
      card.append(h('div', { class: 'evo-sub', text: '当前最高频弱点（进化信号）' }));
      const wrow = h('div', { class: 'evo-weak-row' });
      const maxw = Math.max(...weak.map((x) => x[1]), 1);
      for (const [w, n] of weak) {
        wrow.append(h('div', { class: 'evo-weak-item' },
          h('span', { class: 'evo-dim-name', text: w }),
          h('div', { class: 'evo-dim-bar-wrap' }, h('div', { class: 'evo-dim-bar weak', style: { width: `${(n / maxw) * 100}%` } })),
          h('span', { class: 'hint-text', text: `${n} 章` })));
      }
      card.append(wrow);
      card.append(h('p', { class: 'hint-text', text: '点右上「从写作数据生成候选」：把这些弱点提炼成修订要点，评测转正后自动注入后续写作。' }));
    }
    body.append(card);
  }

  /* --------------------------------------------------------- 画像 */

  function paintProfile() {
    const learn = model.learn;
    const card = h('div', { class: 'evo-card evo-insight' });
    card.append(h('h2', {}, h('span', { html: icon('target', 14) }), h('span', { text: '写作画像' })));
    if (!learn) {
      card.append(h('div', { class: 'hint-text', text: '还没有足够的写作记录。完成几章后，这里会给出题材偏好与改进重点。' }));
      body.append(card);
      return;
    }
    if (learn.insight) card.append(h('p', { text: learn.insight }));
    const chips = h('div', { class: 'evo-chips' });
    for (const s of learn.strengths || []) chips.append(h('span', { class: 'badge', dataset: { tone: 'success' }, text: `长处 · ${s}` }));
    for (const w of learn.weaknesses || []) chips.append(h('span', { class: 'badge', dataset: { tone: 'warn' }, text: `待改进 · ${w}` }));
    if (learn.focus) chips.append(h('span', { class: 'badge badge-accent', text: `下一步 · ${learn.focus}` }));
    if (chips.children.length) card.append(chips);
    if ((learn.genres || []).length) {
      const rows = h('div', { class: 'kv-grid' });
      for (const g of learn.genres.slice(0, 4)) {
        rows.append(
          h(
            'div',
            { class: 'kv-cell' },
            h('span', { class: 'kv-k', text: g.genre }),
            h('span', { class: 'kv-v' }, `${g.count} 部`, g.avg_quality != null ? h('span', { class: 'kv-unit', text: ` · 均 ${g.avg_quality} 分` }) : null)
          )
        );
      }
      card.append(rows);
    }
    body.append(card);
  }

  /* --------------------------------------------------------- 概览 */

  function paintStats() {
    const s = model.skills || {};
    const stats = s.stats || {};
    const feedback = s.feedback || {};
    const grid = h('div', { class: 'evo-stats' });
    const cells = [
      ['候选技能', `${(s.candidates || []).filter((c) => c.pending).length}`, '个待评测'],
      ['生效版本', `${stats.active || (s.history || []).filter((v) => v.active).length}`, '个'],
      ['反馈样本', `${feedback.total || 0}`, '条'],
      ['平均评分', feedback.avg_rating != null ? feedback.avg_rating : '—', '分']
    ];
    for (const [k, v, unit] of cells) {
      grid.append(
        h('div', { class: 'stat-card' }, h('span', { class: 'stat-k', text: k }), h('span', { class: 'stat-v' }, v, unit ? h('small', { text: unit }) : null))
      );
    }
    body.append(grid);
  }

  /* --------------------------------------------------------- 候选 */

  function paintCandidates() {
    const sec = h('div', { class: 'rp-section' });
    sec.append(
      h(
        'div',
        { class: 'rp-section-head' },
        h('h3', { text: '候选技能' }),
        h('span', { class: 'count', text: `${(model.skills.candidates || []).length} 个` })
      )
    );
    const cands = model.skills.candidates || [];
    if (!cands.length) {
      sec.append(
        h(
          'div',
          { class: 'empty-state' },
          h('div', { class: 'empty-icon', html: icon('sparkles', 16) }),
          h('p', { text: '还没有候选。给作品打分或写反馈，积累几条之后就能一键生成候选技能。' })
        )
      );
      body.append(sec);
      return;
    }
    for (const cand of cands) {
      sec.append(candidateCard(cand));
    }
    body.append(sec);
  }

  function candidateCard(cand) {
    const card = h('div', { class: 'evo-card skill-card', dataset: { pending: cand.pending ? 'true' : 'false' } });
    card.append(
      h(
        'div',
        { class: 'skill-head' },
        h('span', { class: 'skill-name', text: cand.name }),
        cand.delta ? h('span', { class: 'skill-delta', text: String(cand.delta) }) : null,
        cand.pending ? h('span', { class: 'badge badge-accent', text: '待评测' }) : h('span', { class: 'status-chip', dataset: { tone: 'success' }, text: cand.promoted ? '已转正' : '已评测' })
      )
    );
    if (cand.reason) card.append(h('div', { class: 'hint-text', text: cand.reason }));
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
          h('button', { class: 'btn btn-sm', type: 'button', text: '评测', on: { click: (e) => evaluate(e, cand) } }),
          h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: '转正', on: { click: () => promote(cand) } }),
          h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '驳回', on: { click: () => reject(cand) } })
        )
      );
    }
    return card;
  }

  async function evaluate(event, cand) {
    const btn = event.currentTarget;
    btn.disabled = true;
    btn.textContent = '评测中…';
    try {
      const res = await api.evaluateSkill({ id: cand.id, baseline: 'current' });
      const delta = (res && res.result && res.result.delta) || '';
      toast(`评测完成${delta ? `：${delta}` : ''}`, { tone: 'success' });
      await load();
    } catch (err) {
      toast(errorText(err, '评测失败'), { tone: 'danger' });
      btn.disabled = false;
      btn.textContent = '评测';
    }
  }

  async function promote(cand) {
    const ok = await confirmDialog({
      title: `转正技能「${cand.name}」？`,
      message: '转正后会立即参与后续写作；效果不理想可以随时回滚。',
      confirmText: '转正'
    });
    if (!ok) return;
    try {
      await api.promoteSkill(cand.id);
      toast('已转正，新技能开始生效', { tone: 'success' });
      await load();
    } catch (err) {
      toast(errorText(err, '转正失败'), { tone: 'danger' });
    }
  }

  async function reject(cand) {
    try {
      await api.rejectSkill(cand.id, '');
      toast('已驳回');
      await load();
    } catch (err) {
      toast(errorText(err, '驳回失败'), { tone: 'danger' });
    }
  }

  /* --------------------------------------------------------- 历史 */

  function paintHistory() {
    const sec = h('div', { class: 'rp-section' });
    sec.append(
      h(
        'div',
        { class: 'rp-section-head' },
        h('h3', { text: '版本历史' }),
        h('span', { class: 'count', text: `${(model.skills.history || []).length} 条` })
      )
    );
    const history = model.skills.history || [];
    if (!history.length) {
      sec.append(
        h(
          'div',
          { class: 'empty-state' },
          h('div', { class: 'empty-icon', html: icon('history', 16) }),
          h('p', { text: '还没有版本记录。第一个转正的技能会出现在这里。' })
        )
      );
      body.append(sec);
      return;
    }
    const list = h('div', { class: 'evo-history' });
    for (const v of history) {
      const row = h(
        'div',
        { class: 'version-row' },
        h('span', { class: v.active ? 'dot dot-success' : 'dot' }),
        h('span', { class: 'v-name', text: `${v.name} · ${v.version}` }),
        h('span', { class: 'v-time', text: v.at ? relTime(v.at) : '' }),
        v.active
          ? h('span', { class: 'badge badge-accent', text: '生效' })
          : h('button', {
              class: 'btn btn-sm',
              type: 'button',
              text: '回滚到此版',
              on: {
                click: async () => {
                  const ok = await confirmDialog({
                    title: `回滚「${v.name}」到 ${v.version}？`,
                    message: '当前生效版本会被替换；随时可以再切回来。',
                    confirmText: '回滚'
                  });
                  if (!ok) return;
                  try {
                    await api.rollbackSkill(v.name, v.version);
                    toast('已回滚', { tone: 'success' });
                    await load();
                  } catch (err) {
                    toast(errorText(err, '回滚失败'), { tone: 'danger' });
                  }
                }
              }
            })
      );
      list.append(row);
    }
    sec.append(list);
    body.append(sec);
  }

  /* --------------------------------------------------------- 反馈 */

  function paintFeedback() {
    const sec = h('div', { class: 'rp-section' });
    sec.append(
      h(
        'div',
        { class: 'rp-section-head' },
        h('h3', { text: '最近反馈' }),
        h('span', { class: 'count', text: `${model.feedbackItems.length} 条` })
      )
    );
    if (!model.feedbackItems.length) {
      sec.append(
        h(
          'div',
          { class: 'empty-state' },
          h('div', { class: 'empty-icon', html: icon('message', 16) }),
          h('p', { text: '还没有反馈样本。在作品里对回复打分或添加标签，就会出现在这里。' })
        )
      );
      body.append(sec);
      return;
    }
    const list = h('div', { class: 'evo-list' });
    for (const fb of model.feedbackItems.slice(0, 12)) {
      const tags = Array.isArray(fb.tags) ? fb.tags : fb.tag ? [fb.tag] : [];
      const text = fb.comment || fb.note || fb.text || '';
      const rating = fb.rating != null ? fb.rating : fb.score;
      const ratingBadge = (() => {
        if (rating == null) return null;
        if (typeof rating === 'number') {
          return h('span', { class: 'badge', dataset: { tone: rating >= 4 ? 'success' : 'muted' }, text: `${rating} 分` });
        }
        const label = rating === 'up' ? '有用' : rating === 'down' ? '不满意' : rating === 'issue' ? '待改进' : String(rating);
        return h('span', { class: 'badge', dataset: { tone: rating === 'up' ? 'success' : rating === 'issue' ? 'warn' : 'muted' }, text: label });
      })();
      const at = fb.at || fb.created_at;
      const row = h(
        'div',
        { class: 'feedback-row' },
        h(
          'div',
          { class: 'evo-meta-row' },
          ratingBadge,
          tags.map((t) => h('span', { class: 'badge', text: t })),
          at ? h('span', { class: 'hint-text', text: relTime(at) }) : null
        ),
        text ? h('div', { class: 'hint-text', text: String(text).slice(0, 120) }) : null
      );
      list.append(row);
    }
    sec.append(list);
    body.append(sec);
  }

  load();

  return {
    el: view,
    refresh: load,
    destroy() {
      view.remove();
    }
  };
}
