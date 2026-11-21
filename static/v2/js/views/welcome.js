/* 欢迎页（v5.4.0 改造）：时段问候 + 随机诗句、hero 输入器、四种体裁快捷入口、
   最近 2 本作品横卡、码字进度卡（双口径）、灵感速记卡与常用工具入口。
   文件拖入建书与既有交互全部保留。 */

import {
  h,
  icon,
  fmtWords,
  relTime,
  validateFile,
  initials,
  mediumGlyph,
  mediumChip
} from '../core/dom.js';
import { store, patchUi } from '../core/state.js';
import { navigate } from '../core/router.js';
import { createComposer } from '../components/composer.js';
import { MEDIUM_FALLBACK, mediumIcon } from '../core/constants.js';
import { toast } from '../core/ui.js';
import { request, errorText, IS_MOCK } from '../core/api.js';
import { loadCss } from '../core/loadcss.js';
/* v5.5.0：新建作品双入口弹窗 + 直接开聊（无 key 会话）挂载 */
import { openNewWorkDialog, isDirectChatQuery } from '../components/newwork.js';
import { renderSession } from './session.js';

/* ---------------------------------------------------------------- 自研文案库 */

/* 时段问候语（全中文自研） */
export const GREETINGS = [
  { from: 23, to: 5, label: '夜深了' },
  { from: 5, to: 9, label: '早上好' },
  { from: 9, to: 12, label: '上午好' },
  { from: 12, to: 14, label: '中午好' },
  { from: 14, to: 18, label: '下午好' },
  { from: 18, to: 23, label: '晚上好' }
];

/** 按小时取问候语（纯函数，导出供测试）。 */
export function greetingFor(hour) {
  const hh = Number(hour);
  const idx = Number.isFinite(hh) ? ((Math.floor(hh) % 24) + 24) % 24 : 12;
  for (const g of GREETINGS) {
    const { from, to } = g;
    if (from > to ? (idx >= from || idx < to) : idx >= from && idx < to) return g.label;
  }
  return '你好';
}

/* 随机诗句（20 句全中文自研文案库，每次进首页随机取一句） */
export const POEMS = [
  '字句是墨写的桥，把心里的雾渡成风景。',
  '一个开头，胜过一百个空想。',
  '今天写下的一行，是明天回望的坐标。',
  '让人物先开口，你再决定跟不跟。',
  '好故事不急着完稿，先让它活过今天。',
  '写不下去的时候，就把卡住的地方写成情节。',
  '每一章，都是一次落笔的承诺。',
  '情节会撒谎，细节不会。',
  '先写下最笨的一稿，聪明留给修改。',
  '码字如行路，日拱一卒，不期速成。',
  '把想说的都交给正文，其余交给时间。',
  '一句一句来，长篇就是这样写成的。',
  '别怕初稿粗糙，纸会替你记住热忱。',
  '人物走了远路，你要替他记得来处。',
  '伏笔像种子，埋下时安静，回收时发光。',
  '今天的一千字，是明天的底气。',
  '删掉一句多余的，比添十句更难。',
  '空白页不可怕，可怕的是不肯坐下。',
  '灵感偏爱正在打字的手。',
  '写到卡住时，回到人物的愿望再出发。'
];

/** 随机取一句诗（纯函数，导出供测试）。 */
export function randomPoem() {
  return POEMS[Math.floor(Math.random() * POEMS.length)] || POEMS[0];
}

/* ``#标签`` 语法提取：# 后跟非空白、非常见标点的 1-20 字，且后面必须紧跟边界
   （空白/#/标点/串尾）——超长标签整体不收；去重、最多 8 个。
   与后端 bot/inspirations.py::extract_tags 同口径（导出供测试）。 */
const TAG_CHARS = '\\s#，。；！？、：\u201c\u201d\u2018\u2019（）【】《》…!?.,:;()';
const TAG_RE = new RegExp(`#([^${TAG_CHARS}]{1,20})(?=$|[${TAG_CHARS}])`, 'g');

export function extractTags(text) {
  const out = [];
  TAG_RE.lastIndex = 0;
  let m;
  while ((m = TAG_RE.exec(String(text || '')))) {
    const t = m[1].trim();
    if (t && !out.includes(t)) out.push(t);
    if (out.length >= 8) break;
  }
  return out;
}

const INSP_MAX_LEN = 300; // 灵感单条字数上限（后端同口径）

function mediumList() {
  const loaded = store.get().mediums || [];
  return loaded.length ? loaded : MEDIUM_FALLBACK;
}

/* 最近 2 本作品（未归档，按更新时间倒序） */
function recentWorks() {
  return (store.get().library.items || [])
    .filter((item) => !item.archived)
    .slice()
    .sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')))
    .slice(0, 2);
}

/* 「点进 bench」：续写下一章（写完 done 章 → 第 done+1 章；全书写完停在末章）。 */
function benchChapterOf(item) {
  const total = Math.max(1, Number(item && item.total) || 1);
  const next = (Number(item && item.done) || 0) + 1;
  return Math.min(Math.max(1, next), total);
}

export function renderWelcome(container, ctx) {
  container.textContent = '';
  /* v5.5.0 直接开聊：#/welcome?chat=1 —— 无作品绑定的新会话。session 视图直挂在本路由，
     currentKey 保持空串：session.js 的 buildStreamPayload 不带 key，后端 /api/chat/stream
     按「无绑定新会话」处理（总编辑引导：想写什么 → 定体裁/书名 → 建书）。
     预填语由 newwork.enterDirectChat 写进 ui.directChatPrefill，取走即清，不自动发送。 */
  if (isDirectChatQuery((store.get().route || {}).query)) {
    const ui = store.get().ui || {};
    const prefill = String(ui.directChatPrefill || '');
    if (ui.directChatPrefill) patchUi({ directChatPrefill: '' });
    const sess = renderSession(container, ctx, { directChat: true, prefill });
    return {
      el: sess.el,
      focusComposer: sess.focusComposer,
      refresh: sess.refresh,
      scrollToBottom: sess.scrollToBottom,
      destroy() { sess.destroy(); }
    };
  }
  /* 首页新卡片样式（码字进度 / 灵感速记 / 工具 / 横卡）随统计样式一同注入，loadCss 幂等。 */
  loadCss('/v2/css/stats.css');
  const welcome = h('div', { class: 'welcome' });
  const inner = h('div', { class: 'welcome-inner' });
  /* v5.4.x 结构调整（仅挂载位置，交互逻辑不变）：双栏栅格放 .home-lane（welcome-inner 的
     --composer-max 输入宽度会挤压 主列+340px 布局，hero/composer 保持原宽度不变）；
     首页响应式断点以 .welcome 为容器查询锚点（见 stats.css @container home）。 */
  const homeLane = h('div', { class: 'home-lane' });
  welcome.append(inner, homeLane);

  /* ---------------------------------------------- hero：品牌 + 时段问候 + 随机诗句 */
  const hero = h(
    'div',
    { class: 'hero-head' },
    h('div', { class: 'hero-mark', html: '<img class="logo-img" src="/v2/assets/logo.png" alt="一幕神笔">' }),
    h('h1', { class: 'hero-title', text: '把念头，写成故事' }),
    h(
      'div',
      { class: 'home-greet' },
      h('span', { class: 'home-greet-label', text: `${greetingFor(new Date().getHours())}，` }),
      h('span', { class: 'home-greet-poem', text: `「${randomPoem()}」` })
    ),
    h('p', {
      class: 'hero-sub',
      text: '一幕·YIMU 神笔会记住人物、伏笔与你的文风。先聊清楚方向，再一章一章往前写。'
    })
  );
  inner.append(hero);

  /* ---------------------------------------------- hero 输入器（保留） */
  const composer = createComposer({
    variant: 'hero',
    ctx,
    placeholder: '描述你想写的故事，或直接说「继续第 47 章」…',
    onSend(payload) {
      /* v5.5.0：hero 输入也走双入口弹窗——preset 原样透传：
         「按开书流程走」→ openWizard(preset)（idea/体裁/附件不丢，现行为不变）；
         「直接开聊」→ 无 key 会话，preset.text 成为 composer 预填语。 */
      openNewWorkDialog(ctx, { preset: payload, source: 'hero' });
    }
  });
  inner.append(composer.el);

  inner.append(
    h(
      'div',
      { class: 'welcome-notice' },
      h('span', { html: icon('shield', 12) }),
      h('span', { text: '模型输出会先给你确认；参考只用于这本作品。' })
    )
  );

  /* ---------------------------------------------- 首页双栏栅格 */
  const homeGrid = h('div', { class: 'home-grid' });
  const colMain = h('div', { class: 'home-col home-col-main' });
  const colSide = h('div', { class: 'home-col home-col-side' });
  homeGrid.append(colMain, colSide);
  homeLane.append(homeGrid);

  /* ---------------------------------------------- 文体快捷入口（保留） */
  const quickSection = h('div', { class: 'welcome-section home-sec-quick' });
  quickSection.append(
    h(
      'div',
      { class: 'welcome-section-head' },
      h('h2', { text: '从一种体裁开始' }),
      h('a', { href: '#/library', text: '已有作品 →' })
    )
  );
  const quick = h('div', { class: 'quick-grid' });
  for (const medium of mediumList()) {   // v4.12.1：全部体裁
    const btn = h(
      'button',
      { class: 'quick-card', type: 'button', dataset: { medium: medium.key } },
      h('span', { class: 'qc-icon', html: icon(mediumIcon(medium), 15) }),
      h('span', { class: 'qc-name', text: medium.label }),
      h('span', { class: 'qc-desc', text: medium.desc || '' })
    );
    btn.addEventListener('click', () => ctx.openWizard({ medium: medium.key }));
    quick.append(btn);
  }
  quickSection.append(quick);
  colMain.append(quickSection);

  /* ---------------------------------------------- 最近 2 本作品横卡 */
  const recentSection = h('div', { class: 'welcome-section home-sec-recent' });
  recentSection.append(
    h(
      'div',
      { class: 'welcome-section-head' },
      h('h2', { text: '最近的作品' }),
      h('a', { href: '#/library', text: '作品库 →' })
    )
  );
  const recentList = h('div', { class: 'home-recent-grid' });
  recentSection.append(recentList);
  colMain.append(recentSection);

  function paintRecent() {
    const state = store.get();
    recentList.textContent = '';
    if (state.library.loading && !state.library.loaded) {
      for (let i = 0; i < 2; i += 1) {
        recentList.append(
          h(
            'div',
            { class: 'home-work-card', style: { pointerEvents: 'none' } },
            h('span', { class: 'home-work-cover skeleton' }),
            h(
              'span',
              { class: 'home-work-main' },
              h('span', { class: 'skeleton skeleton-line', style: { width: `${52 + i * 16}%`, margin: '2px 0' } }),
              h('span', { class: 'skeleton skeleton-line w-40', style: { height: '7px', margin: 0 } })
            )
          )
        );
      }
      return;
    }
    const works = recentWorks();
    if (!works.length) {
      recentList.append(
        h(
          'div',
          { class: 'empty-state', style: { padding: '24px 16px', gridColumn: '1 / -1' } },
          h('div', { class: 'empty-icon', html: icon('book', 18) }),
          h('h3', { text: '还没有作品' }),
          h('p', { text: '点击上方任意体裁，或直接把文件拖进来，开始第一本。' }),
          h('button', {
            class: 'btn btn-sm',
            type: 'button',
            text: '新建作品',
            on: { click: () => openNewWorkDialog(ctx, { source: 'welcome-empty' }) }
          })
        )
      );
      return;
    }
    for (const work of works) {
      const glyph = mediumGlyph(work) || initials(work.name);
      const card = h(
        'button',
        { class: 'home-work-card', type: 'button', title: `进入《${work.name}》章节工作台` },
        h('span', { class: 'home-work-cover', text: glyph }),
        h(
          'span',
          { class: 'home-work-main' },
          h('span', { class: 'home-work-name' }, h('span', { text: work.name }), mediumChip(work.medium)),
          h(
            'span',
            { class: 'home-work-meta' },
            h('span', { text: fmtWords(work.total_words) }),
            h('span', { text: `${work.done || 0}/${work.total || 0} 章` }),
            h('span', { text: relTime(work.updated_at) })
          )
        ),
        h('span', { class: 'home-work-go', html: icon('pen', 15) })
      );
      card.addEventListener('click', () => {
        navigate(`/work/${encodeURIComponent(work.key)}/bench/${benchChapterOf(work)}`);
      });
      recentList.append(card);
    }
  }

  /* ---------------------------------------------- 常用工具入口卡 */
  const toolsSection = h('div', { class: 'welcome-section home-sec-tools' });
  toolsSection.append(
    h(
      'div',
      { class: 'welcome-section-head' },
      h('h2', { text: '常用工具' })
    )
  );
  const toolGrid = h('div', { class: 'ht-grid' });
  const tools = [
    { icon: 'quill', label: '一键开书', hint: '开书向导或直接开聊', onClick: () => openNewWorkDialog(ctx, { source: 'welcome-tools' }) },
    { icon: 'library', label: '作品库', hint: '全部作品', onClick: () => navigate('/library') },
    { icon: 'activity', label: '码字统计', hint: '手写与 AI 分账', onClick: () => navigate('/stats') },
    /* v5.4.0：拆书/榜单/灵感库三入口（路由已预接线，视图按需加载） */
    { icon: 'book', label: '拆书台', hint: '竞品拆解入账本', onClick: () => navigate('/breakdown') },
    { icon: 'globe', label: '榜单风向', hint: '采风与 AI 解读', onClick: () => navigate('/rank') },
    { icon: 'feather', label: '灵感库', hint: '速记与素材', onClick: () => navigate('/inspiration') },
    {
      icon: 'settings',
      label: '账号设置',
      hint: '模型与外观',
      onClick: () => {
        if (typeof ctx.openSettings === 'function') ctx.openSettings();
        else navigate('/settings');
      }
    }
  ];
  for (const t of tools) {
    toolGrid.append(
      h(
        'button',
        {
          class: 'ht-item',
          type: 'button',
          title: t.hint,
          on: { click: t.onClick }
        },
        h('span', { class: 'qc-icon', html: icon(t.icon, 15) }),
        h('span', { text: t.label })
      )
    );
  }
  toolsSection.append(toolGrid);
  colMain.append(toolsSection);

  /* ---------------------------------------------- 码字进度卡（侧栏） */
  const progressSection = h('div', { class: 'home-sec-progress' });
  const progressCard = h(
    'div',
    { class: 'home-progress' },
    h('div', { class: 'home-progress-mark', text: '码字', 'aria-hidden': 'true' }),
    h(
      'div',
      { class: 'hp-head' },
      h('span', { class: 'hp-title' }, h('span', { html: icon('activity', 14) }), h('span', { text: '码字进度' })),
      h('span', { class: 'hp-streak', text: '…' })
    )
  );
  const hpBars = h('div', { class: 'hp-bars' });
  const hpFoot = h('div', { class: 'hp-foot', text: '统计加载中…' });
  progressCard.append(hpBars, hpFoot);
  progressSection.append(progressCard);
  colSide.append(progressSection);

  function paintProgress(d) {
    hpBars.textContent = '';
    hpFoot.textContent = '';
    const streakEl = progressCard.querySelector('.hp-streak');
    if (!d) {
      streakEl.textContent = '暂无数据';
      hpFoot.textContent = '码字统计还没就绪——开始写作后这里会亮起来。';
      return;
    }
    const t = d.today || { manual: 0, ai: 0, session: 0 };
    const goals = d.goals || {};
    const streakDays = ((d.streak || {}).days) || 0;
    streakEl.textContent = `连续 ${streakDays} 天`;
    const rows = [
      { kind: 'manual', label: '手写', val: (t.manual || 0) + (t.session || 0), goal: Number(goals.manual) || 0 },
      { kind: 'ai', label: 'AI', val: t.ai || 0, goal: Number(goals.ai) || 0 }
    ];
    for (const r of rows) {
      const pct = r.goal > 0 ? Math.min(100, Math.round((r.val / r.goal) * 100)) : 0;
      hpBars.append(
        h(
          'div',
          { class: `hp-bar-row ${r.kind === 'ai' ? 'ai' : ''}` },
          h('span', { class: 'hp-bar-kind', text: r.label }),
          h('div', { class: 'hp-bar-track' }, h('div', { class: 'hp-bar-fill', style: { width: `${r.goal > 0 ? pct : 0}%` } })),
          h('span', { class: 'hp-bar-val', text: r.goal > 0 ? `${fmtNumHome(r.val)} / ${fmtNumHome(r.goal)} · ${pct}%` : `${fmtNumHome(r.val)} 字 · 未设目标` })
        )
      );
    }
    const month = d.month || {};
    const parts = [`本月手写 ${fmtNumHome(month.manual || 0)} 字`, `AI ${fmtNumHome(month.ai || 0)} 字`];
    if ((t.session || 0) > 0) parts.push(`含未定稿会话 ${fmtNumHome(t.session)} 字`);
    if (!goals.manual && !goals.ai) parts.push('点「码字统计 → 设置计划」定个目标');
    hpFoot.textContent = parts.join(' · ');
  }

  async function loadProgress() {
    try {
      if (IS_MOCK) throw new Error('mock 模式不穿透真实后端');
      const d = await request('/api/writestats?days=1');
      paintProgress(d && d.ok ? d : null);
    } catch {
      paintProgress(null);
    }
  }

  /* ---------------------------------------------- 灵感速记卡（侧栏） */
  const inspireSection = h('div', { class: 'home-sec-inspire' });
  const inspireCard = h(
    'div',
    { class: 'home-inspire' },
    h('div', { class: 'hp-head' }, h('span', { class: 'hp-title' }, h('span', { html: icon('feather', 14) }), h('span', { text: '灵感速记' })))
  );
  const inspInput = h('input', {
    class: 'input',
    type: 'text',
    maxlength: String(INSP_MAX_LEN),
    placeholder: '记一条灵感，#标签 可直接归类，失焦即存…',
    'aria-label': '灵感速记输入'
  });
  const inspCount = h('span', { class: 'hi-count', text: `0 / ${INSP_MAX_LEN}` });
  const inspRow = h('div', { class: 'hi-row' }, inspInput);
  const inspList = h('div', { class: 'hi-list' });
  let inspItems = [];
  inspireCard.append(inspRow, inspCount, inspList);
  inspireSection.append(inspireCard);
  colSide.append(inspireSection);

  function paintInspirations() {
    inspList.textContent = '';
    if (!inspItems.length) {
      inspList.append(h('div', { class: 'hi-empty', text: '还没有灵感。想到什么就记下来，回车或失焦即存。' }));
      return;
    }
    for (const item of inspItems) {
      const text = String((item && item.text) || '').trim();
      const tags = extractTags(text);
      inspList.append(
        h(
          'div',
          { class: 'hi-item' },
          h(
            'span',
            { class: 'hi-item-text' },
            h('span', { text }),
            tags.length ? h('span', { class: 'hi-tags' }, tags.map((t) => h('span', { class: 'hi-tag', text: `#${t}` }))) : null
          ),
          h('button', {
            class: 'icon-btn icon-btn-sm hi-del',
            type: 'button',
            'aria-label': '删除这条灵感',
            html: icon('trash', 13),
            on: {
              click: async () => {
                try {
                  await request(`/api/inspirations/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
                  await loadInspirations();
                } catch (err) {
                  toast(errorText(err, '删除失败'), { tone: 'danger' });
                }
              }
            }
          })
        )
      );
    }
  }

  async function loadInspirations() {
    try {
      if (IS_MOCK) throw new Error('mock');
      const d = await request('/api/inspirations?limit=5');
      inspItems = (d && Array.isArray(d.items)) ? d.items : [];
    } catch {
      inspItems = [];
    }
    paintInspirations();
  }

  function updateInspCount() {
    const n = inspInput.value.length;
    inspCount.textContent = `${n} / ${INSP_MAX_LEN}`;
    inspCount.classList.toggle('over', n > INSP_MAX_LEN);
  }
  inspInput.addEventListener('input', updateInspCount);

  let savingInsp = false;
  async function saveInspiration() {
    const text = inspInput.value.trim().slice(0, INSP_MAX_LEN);
    if (!text || savingInsp) return;
    savingInsp = true;
    try {
      await request('/api/inspirations', {
        method: 'POST',
        body: { text }   // request() 会统一 JSON 序列化，这里传对象即可
      });
      inspInput.value = '';
      updateInspCount();
      await loadInspirations();
    } catch (err) {
      toast(errorText(err, '灵感保存失败'), { tone: 'danger' });
    }
    savingInsp = false;
  }
  /* 失焦即存；回车也可以立即存 */
  inspInput.addEventListener('blur', () => { void saveInspiration(); });
  inspInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      void saveInspiration();
    }
  });

  /* ---------------------------------------------- 文件拖入建书（保留） */
  const dropHint = h(
    'div',
    {
      class: 'welcome-drop-hint',
      style: {
        position: 'absolute',
        inset: '16px',
        zIndex: '6',
        display: 'none',
        background: 'color-mix(in srgb, var(--accent-soft) 92%, var(--bg-base))',
        alignItems: 'center',
        justifyContent: 'center',
        borderStyle: 'solid'
      }
    },
    h('span', { html: icon('upload', 16) }),
    h('span', { text: '松开即可把文件带入新建作品' })
  );
  welcome.append(dropHint);
  welcome.style.position = 'relative';

  let dragDepth = 0;
  function hasFiles(event) {
    return event.dataTransfer && Array.from(event.dataTransfer.types || []).includes('Files');
  }
  welcome.addEventListener('dragenter', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
    dropHint.style.display = 'flex';
  });
  welcome.addEventListener('dragover', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
  });
  welcome.addEventListener('dragleave', (event) => {
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) dropHint.style.display = 'none';
  });
  welcome.addEventListener('drop', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    dropHint.style.display = 'none';
    const files = Array.from(event.dataTransfer.files || []);
    const accepted = [];
    for (const file of files) {
      const check = validateFile(file);
      if (check.ok) accepted.push(file);
      else toast(check.message, { tone: 'danger' });
    }
    if (accepted.length) {
      ctx.openWizard({
        attachments: accepted.map((file) => ({ file, title: file.name, usage_mode: 'facts' }))
      });
      toast(`已带入 ${accepted.length} 份资料，创建时会自动入库`, { duration: 2400 });
    }
  });

  /* ---------------------------------------------- 启动与销毁 */
  const unsubscribe = store.subscribe((state) => {
    if (!welcome.isConnected) return;
    if (!state.library.loaded) return;
    paintRecent();
  });

  container.append(welcome);
  paintRecent();
  void loadProgress();
  void loadInspirations();

  return {
    el: welcome,
    focusComposer: () => composer.focus(),
    refresh: paintRecent,
    destroy() {
      unsubscribe();
      welcome.remove();
    }
  };
}

/* 进度条数值的轻量格式化（不引 fmtNum 也行，但保持与统计页一致的「万」口径） */
function fmtNumHome(n) {
  const v = Number(n) || 0;
  if (v >= 10000) return `${(v / 10000).toFixed(1).replace(/\.0$/, '')}万`;
  return String(v);
}
