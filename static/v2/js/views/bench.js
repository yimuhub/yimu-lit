/* 章节工作台（v5.1.0 写作台编辑器内核）：段落编辑器 + 160ms 自动落盘 +
   排版可调 + 打字机滚动/段落聚焦 + 划词 AI + 节流版本历史。

   正文纯净域（铁律 0）不变式：
   · 编辑器装「纯净正文」（章题+引子+正文口径由 /api/chapter cleanText 提供）；
   · 复制全文实时过 cleanChapterText（与后端 bot/purity.py 同口径）；
   · 划词替换只动正文内容，编辑器内不新增任何元信息小节；
   · 导出整本仍走 /api/export（逐章去噪），工作文件（含概要/备注）只在
     「工作文件」视图核对，不参与复制与导出。 */

import { h, icon, fmtWords, fmtDate, relTime, copyText, esc } from '../core/dom.js';
import { store } from '../core/state.js';
import { api, request, errorText, IS_MOCK } from '../core/api.js';
import { toast, confirmDialog, openModal, popoverMenu } from '../core/ui.js';
import { navigate } from '../core/router.js';
import { cleanChapterText } from '../core/purity.js';
import { loadCss } from '../core/loadcss.js';
import { createEditor } from '../core/editor.js';
import { mountSelectionBar } from '../core/selectionbar.js';
import { mountGhost } from '../core/ghost.js';
import { mountQuickWrite } from '../core/quickwrite.js';
import { mountChatFloat } from '../components/chatfloat.js';
import { createTTS, ttsSupported, hasChineseVoice, loadPos } from '../core/tts.js';
import { mountFindReplace } from '../core/findreplace.js';
import { mountNameHighlight } from '../core/namehl.js';

/* v5.1.0：bench 编辑器样式按需注入（静态资源 no-store，无需版本参数）。 */
loadCss('/v2/css/bench-editor.css');

/* 排版设置：localStorage `ew-bench-typography`，CSS 变量挂纸面容器。 */
const TYPO_KEY = 'ew-bench-typography';
const TYPO_DEFAULTS = {
  font: 'song',          /* song=宋体阅读 / kai=楷体·霞鹜文楷 / hei=黑体 / fang=仿宋 / sys=系统 */
  fontSize: 17,          /* 12–28 */
  lineHeight: 1.9,       /* 1.4–3.0 */
  width: 92,             /* 行宽 30–100% */
  paraGap: true,         /* 段间空行 */
  indent: false,         /* 首行缩进两字符 */
  ruler: 'none',         /* none / solid / dashed */
  typewriter: false,     /* 打字机滚动 */
  paraFocus: false       /* 段落聚焦 */
};
/* 全部系统栈降级：霞鹜文楷未安装时落到楷体/宋体，不引外部字体文件（无构建无 CDN）。 */
const TYPO_FONTS = [
  ['song', '宋体阅读', '"Songti SC", "Noto Serif CJK SC", "Source Han Serif SC", "STSong", "SimSun", serif'],
  ['kai', '楷体·文楷', '"LXGW WenKai", "霞鹜文楷", "LXGW WenKai GB", "Kaiti SC", "KaiTi", "STKaiti", serif'],
  ['hei', '黑体', '-apple-system, "PingFang SC", "HarmonyOS Sans SC", "Microsoft YaHei", sans-serif'],
  ['fang', '仿宋', '"Fangsong SC", "FangSong", "STFangsong", "仿宋", serif'],
  ['sys', '系统默认', 'var(--font-ui)']
];

function loadTypo() {
  try {
    const raw = JSON.parse(localStorage.getItem(TYPO_KEY) || '{}');
    return { ...TYPO_DEFAULTS, ...(raw && typeof raw === 'object' ? raw : {}) };
  } catch {
    return { ...TYPO_DEFAULTS };
  }
}

function saveTypo(typo) {
  try {
    localStorage.setItem(TYPO_KEY, JSON.stringify(typo));
  } catch {
    /* 隐私模式：排版只活在本次会话 */
  }
}

/* 从工作文件里抽「本章概要」，供划词 AI 上下文（只读，不进编辑器）。 */
function extractSummary(raw) {
  const m = String(raw || '').match(/^##\s*本章概要\s*\n+([\s\S]*?)(?=\n##\s|\n*$)/m);
  return m ? m[1].trim().slice(0, 400) : '';
}

/* v5.2.0 mock 演示词表：敏感词接口在演示模式下不可用，徽标仍可验收交互。 */
const MOCK_SENSITIVE_WORDS = ['枪械买卖', '赌博网站', '诈骗话术'];

/* ══════════════════════════════════════════════════════════════
   v5.2.0 跨 Agent 事件契约（唯一派发点，约定不许改）：

   1) yimu-editor-session —— 编辑器会话上报（统计侧监听做手写/AI 分账）
      · begin   ：章节装载完成，detail {phase:'begin', key, text:<全文>}
      · update  ：防抖落盘的文本变化，detail {phase:'update', key, text}
                  （节流 ≤1 次/秒 + 内容去重，由本工厂封装）
      · commit  ：切章/卸载结算，detail {phase:'commit', key, text}
   2) yimu-ai-insert —— AI 生成文本插入编辑器后派发（detail {chars, key}）：
      幽灵字 Tab 采纳 / 快捷续写流入结束 / 划词替换成功 三条路径在
      renderBench 内分别接线到 emitAiInsert。
   ══════════════════════════════════════════════════════════════ */
export function createSessionReporter(key, { throttleMs = 1000 } = {}) {
  let active = false;
  let lastEmit = 0;
  let lastText = '';
  const emit = (phase, text) => {
    try {
      document.dispatchEvent(new CustomEvent('yimu-editor-session', {
        detail: { phase, key, text: String(text || '') }
      }));
    } catch { /* 统计侧缺失时静默，不影响编辑器 */ }
  };
  return {
    begin(text) {
      active = true;
      lastText = String(text || '');
      lastEmit = 0;   // begin 不占用 update 的节流窗口：装载后第一笔变化立即上报
      emit('begin', lastText);
    },
    update(text) {
      if (!active) return;
      const t = String(text || '');
      if (!t || t === lastText) return;
      const now = Date.now();
      if (now - lastEmit < throttleMs) return;   // 节流 ≤1 次/秒
      lastEmit = now;
      lastText = t;
      emit('update', t);
    },
    commitIfActive(text) {
      if (!active) return;
      active = false;
      emit('commit', String(text || ''));
    },
    isActive: () => active
  };
}

export function emitAiInsert(chars, key) {
  try {
    document.dispatchEvent(new CustomEvent('yimu-ai-insert', {
      detail: { chars: Number(chars) || 0, key }
    }));
  } catch { /* 统计侧缺失时静默 */ }
}

/* ══════════════════════════════════════════════════════════════
   v5.5.x A4 退出写作台结算卡（只追加区块，全部自研）：

   · 会话字数口径（与 core/writestats-client.js 同一事件流、同一算法）：
     bench 挂载周期内逐章净增 = max(0, 离开时全文汉字数 − 进入时汉字数)，
     删字不回扣（取见过的峰值）；只读 yimu-editor-session 事件，
     绝不写 writestats（不绕过统计侧分账，也不另发请求）。
   · 退出（destroy）时净增 >0 才弹结算卡：「本次码字 N 字」+ 今日目标进度
     （GET /api/writestats 只读，失败静默跳过）+ 文案库随机鼓励语；
     2.5 秒自动消失或点击关闭。
   ══════════════════════════════════════════════════════════════ */

/* 自研鼓励语文案库（14 条，全中文；风格：迟早成神 / 妙笔生花 / 今天也是码字之星）。 */
export const SETTLE_PHRASES = [
  '今天也是码字之星。',
  '妙笔生花，说的就是你。',
  '迟早成神，先把今天写完。',
  '字落纸上，故事又长了一寸。',
  '笔耕不辍，必有回响。',
  '这一段，读者会记住的。',
  '灵感正在排队等你回来。',
  '存稿又厚了一点，底气也厚了。',
  '今天的手速，配得上你的野心。',
  '每一个字都在给结局铺路。',
  '写完就休息，神仙也点头。',
  '主角还在等你，别太久。',
  '又一天，离完本更近了。',
  '手感正好，明天接着燃。'
];

let _lastPhraseIdx = -1;

export function pickSettlePhrase() {
  if (!SETTLE_PHRASES.length) return '';
  let i = Math.floor(Math.random() * SETTLE_PHRASES.length);
  if (SETTLE_PHRASES.length > 1) {
    while (i === _lastPhraseIdx) {
      i = Math.floor(Math.random() * SETTLE_PHRASES.length);
    }
  }
  _lastPhraseIdx = i;
  return SETTLE_PHRASES[i];
}

/* 会话净增字数账本：只消费 yimu-editor-session 事件（begin/update/commit），
   口径 = 汉字数（与 writestats-client 一致），逐章 net = 峰值 − 进入基线。 */
export function createSettleLedger(key) {
  let base = 0;
  let peak = 0;
  let armed = false;
  let total = 0;
  const count = (t) => (String(t || '').match(/[\u4e00-\u9fff]/g) || []).length;
  const onDetail = (d) => {
    if (!d || String(d.key || '') !== String(key || '')) return;
    const c = count(d.text);
    if (d.phase === 'begin') {
      armed = true;
      base = c;
      peak = c;
    } else if (d.phase === 'update') {
      if (armed && c > peak) peak = c;
    } else if (d.phase === 'commit') {
      if (!armed) return;
      armed = false;
      if (c > peak) peak = c;
      total += Math.max(0, peak - base);
      base = 0;
      peak = 0;
    }
  };
  let listener = null;
  return {
    onDetail,
    chars: () => total,
    attach(doc) {
      listener = (e) => { try { this.onDetail(e && e.detail); } catch { /* 结算是附加能力 */ } };
      doc.addEventListener('yimu-editor-session', listener);
    },
    detach(doc) {
      if (listener) doc.removeEventListener('yimu-editor-session', listener);
      listener = null;
    }
  };
}

/* 结算卡（挂 document.body，随 2.5s 定时或点击关闭自毁；任何失败静默）。
   今日目标进度只读 GET /api/writestats（days=1），读不到就不显示进度行。 */
function showBenchSettleCard(chars) {
  const n = Math.max(0, Math.floor(Number(chars) || 0));
  if (n <= 0) return;
  const card = h('div', { class: 'settle-card', role: 'dialog', 'aria-label': '本次码字结算' });
  const goalEl = h('div', { class: 'settle-goal', text: '' });
  const closeBtn = h('button', {
    class: 'settle-close', type: 'button', text: '关闭',
    'aria-label': '关闭结算卡',
    on: { click: () => done() }
  });
  let timer = 0;
  function done() {
    clearTimeout(timer);
    if (card.parentNode) card.remove();
  }
  card.append(
    h('div', { class: 'settle-title', text: '本次码字' }),
    h('div', { class: 'settle-words', text: `${n.toLocaleString('zh-CN')} 字` }),
    goalEl,
    h('div', { class: 'settle-phrase', text: pickSettlePhrase() }),
    closeBtn
  );
  document.body.append(card);
  timer = setTimeout(done, 2500);   // 2.5 秒自动消失
  (async () => {
    try {
      const r = await request('/api/writestats', { query: { days: 1 }, timeout: 6000 });
      const today = (r && r.today) || {};
      const goals = (r && r.goals) || {};
      const done0 = Number(today.manual || 0) + Number(today.ai || 0) + Number(today.session || 0);
      const goal = Number(goals.manual || 0) + Number(goals.ai || 0);
      if (!card.parentNode) return;   // 卡片已被关掉：不再补画进度
      if (goal > 0) {
        const pct = Math.min(100, Math.round((done0 / goal) * 100));
        goalEl.textContent = `今日 ${done0.toLocaleString('zh-CN')} / ${goal.toLocaleString('zh-CN')} 字 · ${pct}%`;
        goalEl.append(h('span', { class: 'settle-bar' },
          h('span', { class: 'settle-bar-fill', style: { width: `${pct}%` } })));
      } else if (done0 > 0) {
        goalEl.textContent = `今日已码 ${done0.toLocaleString('zh-CN')} 字`;
      }
    } catch { /* 统计失败静默跳过：结算卡保留鼓励语 */ }
  })();
}

/* ══════════════════════════════════════════════════════════════
   v5.5.x A3 bench 章节导航树（简版，只追加区块，全部自研）：

   · 数据源：store.plan（GET /api/plan 全书规划大对象，经既有 refreshPlan
     链路下发——不新增端点、不额外轮询）。卷→弧→章卡（chapter_cards 带
     done/writing/changed/planned 状态与 word_count）为长篇分层结构；
     无卷结构（旧书/未分层规划）回落 plan.chapters 平铺章列表。
   · 徽标词表归一：章卡 02d 用 done/writing/changed/planned，写作计划用
     completed/in_progress/failed/pending——两套先归一再映射徽标；
     本会话保存过的章就地升级（后端 _apply_chapter_text 落盘时已回写计划
     状态，等 refreshPlan 收敛后口径自然一致），不做额外轮询。
   · 折叠记忆：卷折叠状态存 localStorage `ew-bench-catalog-folds:<bookKey>`，
     LRU 键清单（ew-bench-catalog-folds:_idx）只留最近 50 本，超限删最旧。
   · 搜索：先 escapeHtml 再替换 <mark>（章题含 <script> 也只当文本渲染），
     命中卷自动展开；清空搜索恢复折叠记忆。
   ══════════════════════════════════════════════════════════════ */

export const CAT_FOLD_PREFIX = 'ew-bench-catalog-folds:';
export const CAT_FOLD_INDEX_KEY = 'ew-bench-catalog-folds:_idx';
export const CAT_FOLD_MAX_BOOKS = 50;
const CAT_RAIL_OPEN_KEY = 'ew-bench-catalog-open';
/* 与 bench-editor.css 里 @container bench (max-width: 920px) 对齐的收起阈值 */
export const CAT_NARROW_PX = 920;

/* 两套章节状态词表归一（口径与右栏章卡一致，实现自研、不引其模块）。
   未知值原样保留，由 catalogBadge 兜底成「待写」。 */
const CAT_STATUS_MAP = {
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

export function normCatalogStatus(raw) {
  const k = String(raw || '').trim().toLowerCase();
  return CAT_STATUS_MAP[k] || k || 'planned';
}

/* 归一态 → 徽标 { state, label }（.cat-badge 的 data-state）。 */
export function catalogBadge(status) {
  const s = normCatalogStatus(status);
  if (s === 'completed') return { state: 'completed', label: '完成' };
  if (s === 'in_progress') return { state: 'current', label: '写中' };
  if (s === 'failed') return { state: 'failed', label: '失败' };
  return { state: 'planned', label: '待写' };
}

function _catInt(v) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

/* 章卡 / 写作计划条目 → 树行模型 { n, title, status, words }。 */
export function chapterFromCard(c) {
  if (!c || typeof c !== 'object') return { n: 0, title: '', status: 'planned', words: 0 };
  const n = _catInt(c.n != null ? c.n : c.chapterNumber);
  const raw = String(c.title || '').trim();
  const words = _catInt(c.word_count != null ? c.word_count : (c.words != null ? c.words : c.wordCount));
  return { n, title: raw || (n ? `第${n}章` : ''), status: normCatalogStatus(c.status), words };
}

function _catDedupeSort(chapters) {
  const seen = new Set();
  const out = [];
  for (const c of chapters) {
    if (!c || !c.n || seen.has(c.n)) continue;
    seen.add(c.n);
    out.push(c);
  }
  out.sort((a, b) => a.n - b.n);
  return out;
}

/* 规划大对象 → 分组树模型（纯函数）：
   · volumes[].arcs[] → 每卷一组（chapter_cards 优先，缺失回落 arc.chapters 章号）；
   · plan.chapters 平铺 → 单组 kind:'flat'（渲染时不带卷头）；
   · 空结构 → { mode:'empty', groups: [], total: 0 }。 */
export function buildCatalogModel(payload) {
  const plan = (payload && typeof payload === 'object' && payload.plan)
    ? payload.plan
    : (payload && typeof payload === 'object' ? payload : {});
  const vols = Array.isArray(plan.volumes) ? plan.volumes : [];
  if (vols.length) {
    const groups = [];
    vols.forEach((vol, vi) => {
      const arcs = (vol && Array.isArray(vol.arcs)) ? vol.arcs : [];
      const chapters = [];
      for (const arc of arcs) {
        if (!arc) continue;
        const cards = Array.isArray(arc.chapter_cards) ? arc.chapter_cards : [];
        if (cards.length) {
          for (const c of cards) chapters.push(chapterFromCard(c));
        } else {
          for (const nn of (Array.isArray(arc.chapters) ? arc.chapters : [])) {
            chapters.push(chapterFromCard({ n: nn, status: 'planned' }));
          }
        }
      }
      const no = vol.no != null ? (_catInt(vol.no) || vi + 1) : vi + 1;
      const chs = _catDedupeSort(chapters);
      if (chs.length) {
        groups.push({
          id: `v${no}`,
          kind: 'volume',
          no,
          title: String(vol.title || '').trim() || `卷${no}`,
          chapters: chs,
          match: false
        });
      }
    });
    if (groups.length) {
      return { mode: 'volumes', groups, total: groups.reduce((n, g) => n + g.chapters.length, 0) };
    }
  }
  const flat = Array.isArray(plan.chapters) ? plan.chapters.map(chapterFromCard) : [];
  if (flat.length) {
    const chs = _catDedupeSort(flat);
    if (chs.length) {
      return { mode: 'flat', groups: [{ id: 'flat', kind: 'flat', no: 0, title: '', chapters: chs, match: false }], total: chs.length };
    }
  }
  return { mode: 'empty', groups: [], total: 0 };
}

/* 目录搜索过滤（纯函数）：命中词匹配章题 / 章号（第N章）/ 卷题；
   卷题命中保留整卷全部章（章行 hit 仍按各自标题算，供 <mark> 高亮）；
   空查询原样透传（hit 全 false）。 */
export function filterCatalogGroups(groups, query) {
  const src = Array.isArray(groups) ? groups : [];
  const q = String(query || '').trim().toLowerCase();
  if (!q) {
    return {
      groups: src.map((g) => ({
        ...g,
        match: false,
        chapters: (g.chapters || []).map((c) => ({ ...c, hit: false }))
      })),
      hits: 0
    };
  }
  let hits = 0;
  const out = [];
  for (const g of src) {
    const volHit = String(g.title || '').toLowerCase().includes(q);
    const chs = [];
    for (const c of g.chapters || []) {
      const hay = `${c.title || ''} 第${c.n}章 ${c.n}`.toLowerCase();
      const hit = hay.includes(q);
      if (hit || volHit) chs.push({ ...c, hit });
      if (hit) hits += 1;
    }
    if (!chs.length) continue;
    out.push({ ...g, match: volHit, chapters: chs });
  }
  return { groups: out, hits };
}

/* 命中词高亮（纯函数）：先 escapeHtml 再替换 <mark>——章题里的 <script>
   只会变成 &lt;script&gt; 文本，绝不产生可执行节点。 */
export function markMatch(title, query) {
  const escaped = esc(title);
  const q = String(query || '').trim();
  if (!q) return escaped;
  const lower = escaped.toLowerCase();
  const needle = q.toLowerCase();
  const parts = [];
  let i = 0;
  while (i < escaped.length) {
    const at = lower.indexOf(needle, i);
    if (at < 0) {
      parts.push(escaped.slice(i));
      break;
    }
    if (at > i) parts.push(escaped.slice(i, at));
    parts.push(`<mark class="cat-mark">${escaped.slice(at, at + q.length)}</mark>`);
    i = at + q.length;
  }
  return parts.join('');
}

function _catStorage(storage) {
  if (storage) return storage;
  return typeof localStorage !== 'undefined' ? localStorage : null;
}

export function catFoldKey(bookKey) {
  return CAT_FOLD_PREFIX + String(bookKey || '');
}

/* 卷折叠记忆读取：损坏 JSON / 非数组一律回空集（隐私模式也不炸）。 */
export function loadCatFolds(bookKey, storage) {
  const st = _catStorage(storage);
  if (!st) return [];
  try {
    const raw = JSON.parse(st.getItem(catFoldKey(bookKey)) || '[]');
    return Array.isArray(raw) ? Array.from(new Set(raw.map(String))) : [];
  } catch {
    return [];
  }
}

/* 卷折叠记忆写入 + LRU：键清单（_idx）按最近使用排序，超过 50 本删最旧的书目键。 */
export function saveCatFolds(bookKey, folds, storage) {
  const st = _catStorage(storage);
  if (!st) return;
  const bk = String(bookKey || '');
  const ids = Array.from(new Set((Array.isArray(folds) ? folds : []).map(String)));
  try {
    st.setItem(catFoldKey(bk), JSON.stringify(ids));
  } catch { /* 隐私模式：折叠记忆只活在本次会话 */ }
  let idx = [];
  try {
    const raw = JSON.parse(st.getItem(CAT_FOLD_INDEX_KEY) || '[]');
    if (Array.isArray(raw)) idx = raw.map(String);
  } catch { idx = []; }
  idx = idx.filter((k) => k !== bk);
  idx.push(bk);
  while (idx.length > CAT_FOLD_MAX_BOOKS) {
    const oldest = idx.shift();
    if (oldest != null && oldest !== bk) {
      try { st.removeItem(CAT_FOLD_PREFIX + oldest); } catch { /* 忽略 */ }
    }
  }
  try {
    st.setItem(CAT_FOLD_INDEX_KEY, JSON.stringify(idx));
  } catch { /* 隐私模式 */ }
}

/* 窄栏展开态记忆（全局一把，非书目维度）：默认展开。 */
export function loadCatRailOpen(storage) {
  const st = _catStorage(storage);
  if (!st) return true;
  return st.getItem(CAT_RAIL_OPEN_KEY) !== '0';
}

export function saveCatRailOpen(on, storage) {
  const st = _catStorage(storage);
  if (!st) return;
  try { st.setItem(CAT_RAIL_OPEN_KEY, on ? '1' : '0'); } catch { /* 隐私模式 */ }
}

/* 目录窄栏工厂（挂 .bench-body 最左）：
   · data-open 宽栏/图标条切换（记忆）、data-narrow 窄容器自动收起（可还原）；
   · dataState：loading（6 行骨架）/ ready / empty / failed（规划不可用）；
   · 搜索过滤 + <mark> 高亮 + 命中卷自动展开；卷折叠走 localStorage 记忆；
   · 当前章高亮 + 数据到位后滚动可见；onSaved 就地升级本会话保存章的徽标。 */
export function createCatalogRail({ key, onGoto, storage } = {}) {
  let destroyed = false;
  let folds = new Set(loadCatFolds(key, storage));
  let planModel = null;         // buildCatalogModel 结果缓存
  let dataState = 'loading';    // loading | ready | empty | failed
  let currentN = 0;
  let query = '';
  let lastPayload = null;       // store 订阅去重：plan 引用 + 章号 + 失败态都没变就不重画
  let lastFailed = false;
  let lastScrollKey = '';
  let narrowStash = null;       // 进入窄容器前的展开态，退回宽容器时还原
  let resizeObs = null;
  const savedLocal = new Map(); // 本会话保存过的章 n → words（徽标就地升级，不回写 store）

  const root = h('div', {
    class: 'cat-rail',
    role: 'navigation',
    'aria-label': '章节目录',
    dataset: { open: String(loadCatRailOpen(storage)), narrow: 'false' }
  });
  const openBtn = h('button', {
    class: 'cat-open-btn',
    type: 'button',
    title: '章节目录',
    'aria-label': '展开章节目录',
    html: icon('list', 15),
    on: { click: () => setOpen(true, true) }
  });
  const main = h('div', { class: 'cat-main' });
  const countEl = h('span', { class: 'cat-count', text: '' });
  const searchInput = h('input', {
    class: 'cat-search-input',
    type: 'search',
    placeholder: '搜索章 / 卷',
    'aria-label': '搜索目录',
    on: {
      input: () => {
        query = searchInput.value || '';
        paintBody();
      }
    }
  });
  const bodyEl = h('div', { class: 'cat-body' });

  function setOpen(on, persist) {
    root.dataset.open = String(!!on);
    if (persist) saveCatRailOpen(!!on, storage);
  }

  function scrollCurrentIntoView() {
    const cur = bodyEl.querySelector('.cat-row.is-current');
    if (cur && typeof cur.scrollIntoView === 'function') {
      try { cur.scrollIntoView({ block: 'nearest' }); } catch { /* jsdom/无布局时静默 */ }
    }
  }

  function titleNode(title, hit, cls) {
    if (hit && query.trim()) return h('span', { class: cls, html: markMatch(title, query) });
    return h('span', { class: cls, text: String(title || '') });
  }

  function skeletonEl() {
    const box = h('div', { class: 'cat-skel', 'aria-hidden': 'true' });
    for (let i = 0; i < 6; i += 1) {
      box.append(h('div', { class: 'cat-skel-row' },
        h('span', { class: 'cat-skel-no' }),
        h('span', { class: 'cat-skel-line' })));
    }
    return box;
  }

  function badgeFor(c) {
    const local = savedLocal.get(String(c.n));
    if (local && normCatalogStatus(c.status) !== 'completed') {
      return { state: 'completed', label: '完成' };
    }
    return catalogBadge(c.status);
  }

  function chapterRow(c) {
    const badge = badgeFor(c);
    const isCurrent = Number(c.n) === Number(currentN);
    const row = h('button', {
      class: `cat-row${isCurrent ? ' is-current' : ''}`,
      type: 'button',
      title: c.title || `第 ${c.n} 章`,
      'aria-current': isCurrent ? 'true' : null,
      dataset: { n: String(c.n), state: badge.state },
      on: { click: () => { if (onGoto) onGoto(c.n); } }
    },
    h('span', { class: 'cat-no', text: String(c.n) }),
    titleNode(c.title, c.hit, 'cat-title'),
    c.words > 0 ? h('span', { class: 'cat-words', text: fmtWords(c.words) }) : null,
    h('span', { class: 'cat-badge', dataset: { state: badge.state } },
      badge.state === 'completed' ? h('span', { class: 'cat-badge-ic', html: icon('check', 10) }) : null,
      h('span', { text: badge.label })));
    return row;
  }

  function volumeEl(g) {
    const folded = !query.trim() && folds.has(g.id);
    const vol = h('div', { class: 'cat-vol', dataset: { fold: folded ? 'true' : 'false', match: g.match ? 'true' : 'false' } });
    const head = h('button', {
      class: 'cat-vol-head',
      type: 'button',
      'aria-expanded': folded ? 'false' : 'true',
      on: {
        click: () => {
          const nowFolded = vol.dataset.fold !== 'true';
          vol.dataset.fold = nowFolded ? 'true' : 'false';
          head.setAttribute('aria-expanded', nowFolded ? 'false' : 'true');
          if (nowFolded) folds.add(g.id); else folds.delete(g.id);
          saveCatFolds(key, Array.from(folds), storage);
        }
      }
    },
    h('span', { class: 'cat-chev', html: icon('chevron-right', 12) }),
    titleNode(g.title, g.match, 'cat-vol-title'),
    h('span', { class: 'cat-vol-count', text: `${g.chapters.length} 章` }));
    const chs = h('div', { class: 'cat-vol-chs' });
    for (const c of g.chapters) chs.append(chapterRow(c));
    vol.append(head, chs);
    return vol;
  }

  function paintBody() {
    bodyEl.textContent = '';
    if (dataState === 'loading') {
      bodyEl.append(skeletonEl());
      return;
    }
    if (dataState === 'failed') {
      bodyEl.append(h('div', { class: 'cat-empty', text: '章节目录暂时不可用（规划加载失败）。可从右侧「规划」页签重试。' }));
      return;
    }
    if (!planModel || !planModel.groups.length) {
      bodyEl.append(h('div', { class: 'cat-empty', text: '这本书还没有章节规划。先在「规划」页签生成目录，这里就会长出全书导航。' }));
      return;
    }
    const q = query.trim();
    const { groups } = filterCatalogGroups(planModel.groups, q);
    if (q && !groups.length) {
      bodyEl.append(h('div', { class: 'cat-empty', text: `没有匹配「${q}」的章或卷。` }));
      return;
    }
    for (const g of groups) {
      if (g.kind === 'flat') {
        for (const c of g.chapters) bodyEl.append(chapterRow(c));
      } else {
        bodyEl.append(volumeEl(g));
      }
    }
  }

  function paintCount() {
    countEl.textContent = planModel && planModel.total ? `${planModel.total} 章` : '';
  }

  function setData(payload, n, opts = {}) {
    if (destroyed) return;
    const failed = !!(opts && opts.failed);
    const no = Number(n) || 0;
    if (payload === lastPayload && no === currentN && failed === lastFailed) return;
    const scrolledBefore = lastScrollKey;
    lastPayload = payload;
    lastFailed = failed;
    currentN = no;
    if (!payload || !(typeof payload === 'object' && (payload.plan || payload.volumes || payload.chapters))) {
      dataState = failed ? 'failed' : 'loading';
      planModel = null;
    } else {
      planModel = buildCatalogModel(payload);
      dataState = planModel.groups.length ? 'ready' : 'empty';
    }
    /* 当前章所在卷自动展开（折叠记忆里收起的卷，切到章内时也要让当前章可见） */
    if (dataState === 'ready' && planModel && currentN) {
      for (const g of planModel.groups) {
        if (g.chapters.some((c) => c.n === currentN) && folds.delete(g.id)) {
          saveCatFolds(key, Array.from(folds), storage);
          break;
        }
      }
    }
    paintCount();
    paintBody();
    const scrollKey = `${dataState}:${currentN}`;
    if (scrollKey !== scrolledBefore && dataState === 'ready') {
      lastScrollKey = scrollKey;
      scrollCurrentIntoView();
    }
  }

  /* 本会话保存过的章：徽标就地升级为「完成」（后端保存链路已回写计划状态）。 */
  function onSaved(n, words) {
    const no = Number(n) || 0;
    if (!no) return;
    savedLocal.set(String(no), Number(words) || 0);
    if (dataState === 'ready') paintBody();
  }

  function setNarrow(on) {
    const val = on ? 'true' : 'false';
    if (root.dataset.narrow === val) return;
    root.dataset.narrow = val;
    if (on) {
      if (root.dataset.open === 'true') {
        narrowStash = true;
        setOpen(false, false);   // 自动收起是瞬态，不写入展开偏好
      } else {
        narrowStash = false;
      }
    } else {
      if (narrowStash === true) setOpen(true, false);
      narrowStash = null;
    }
  }

  /* 窄容器探测（右面板停靠 / 窄窗）：bench-body 宽度越过阈值即收起为图标条。
     ResizeObserver 不可用（jsdom/旧内核）时静默跳过，CSS @container 兜底。 */
  function attachResize(hostEl) {
    if (destroyed || typeof ResizeObserver !== 'function' || !hostEl) return;
    try {
      resizeObs = new ResizeObserver((entries) => {
        const rect = entries && entries[0] && entries[0].contentRect;
        const w = rect ? rect.width : 0;
        setNarrow(w > 0 && w < CAT_NARROW_PX);
      });
      resizeObs.observe(hostEl);
    } catch { resizeObs = null; }
  }

  root.append(
    openBtn,
    h('div', { class: 'cat-main' },
      h('div', { class: 'cat-head' },
        h('span', { class: 'cat-head-ic', html: icon('list', 13) }),
        h('span', { class: 'cat-head-title', text: '目录' }),
        countEl,
        h('button', {
          class: 'cat-collapse',
          type: 'button',
          title: '收起章节目录',
          'aria-label': '收起章节目录',
          html: icon('chevron-left', 14),
          on: { click: () => setOpen(false, true) }
        })),
      h('div', { class: 'cat-search' }, searchInput),
      bodyEl)
  );

  /* 初始画面：数据未到先铺 6 行骨架（别像「空书」），plan 到位后由 setData 换树 */
  paintCount();
  paintBody();

  return {
    el: root,
    setData,
    onSaved,
    setNarrow,
    attachResize,
    scrollCurrentIntoView,
    debug: () => ({
      dataState,
      rows: bodyEl.querySelectorAll('.cat-row').length,
      vols: bodyEl.querySelectorAll('.cat-vol').length,
      query,
      folds: Array.from(folds),
      savedLocal: Array.from(savedLocal.keys())
    }),
    destroy() {
      destroyed = true;
      if (resizeObs) {
        try { resizeObs.disconnect(); } catch { /* 忽略 */ }
        resizeObs = null;
      }
      root.remove();
    }
  };
}

export function renderBench(container, ctx) {
  const state0 = store.get();
  const key = state0.route.key || state0.currentKey;
  const n = Number(state0.route.chapter) || 1;

  const view = h('div', { class: 'bench-view' });
  container.append(view);

  const model = {
    n,
    title: '',
    text: '',
    rawText: '',
    mode: 'prose',
    savedText: '',
    words: 0,
    updatedAt: '',
    versions: [],
    loading: true,
    error: '',
    dirty: false,
    saveState: 'saved'     /* saved / pending / saving / error */
  };

  /* v5.1.0 保存管线状态：160ms 防抖 + 组词保护 + 互斥补跑 */
  let saveTimer = 0;
  let saving = false;
  let pendingSave = false;
  let lastVersionsRefresh = 0;

  let typo = loadTypo();

  /* v5.2.0：会话上报器（契约 1 的封装见文件顶部 createSessionReporter） */
  const session = createSessionReporter(key);

  /* -------------------------------------------------------------- head */

  const head = h('div', { class: 'bench-head' });
  /* v3.2.0：小说章节工作台也有对话改稿（与其他文体同一套副驾，目标定位到当前章） */
  const chatCtx = { doc: String(n), block: null, title: '' };
  const chatFloat = mountChatFloat({
    getContext: () => ({
      key,
      medium: 'novel',
      doc: String(model.n || n),
      block: chatCtx.block,
      docTitle: model.title ? `第 ${model.n} 章 · ${model.title}` : `第 ${model.n} 章`
    }),
    onApplied: () => {
      /* 应用后正文已变：重新拉章节 */
      loadChapter();
    }
  });
  const saveBtn = h('button', {
    class: 'btn btn-primary btn-sm bench-save',
    type: 'button',
    text: '保存',
    disabled: true,
    dataset: { state: 'saved' },
    on: { click: () => flushSave() }
  });
  view.append(head);

  const body = h('div', { class: 'bench-body' });
  const editorCol = h('div', { class: 'bench-editor' });
  /* v5.1.0：原生 textarea → 段落编辑器（core/editor.js，纯文本 \n\n 分段模型）。
     滚动容器是 .bench-editor-body，编辑器根挂在其内；打字机滚动以它为基准。 */
  const scrollHost = h('div', { class: 'bench-editor-body' });
  const foot = h('div', { class: 'bench-foot' });
  const paper = h('div', { class: 'bench-paper' });
  paper.append(scrollHost, foot);
  editorCol.append(paper);

  const side = h('div', { class: 'bench-side' });
  body.append(editorCol, side);
  view.append(body);

  /* foot 常驻节点：输入路径只改 textContent，不整行重画（长章输入不卡）。 */
  const charsEl = h('span', { text: '0 字符' });
  const wordsEl = h('span', { class: 'sep' });
  const updatedEl = h('span', { class: 'sep' });
  const saveStateEl = h('span', { class: 'bench-save-state', dataset: { state: 'saved' } });
  const dirtyHint = h('span', { class: 'hint-text', text: '' });
  /* v5.2.0：敏感词计数徽标（本地词库纯前端匹配，点击定位第一处）+ AI 轻提示。 */
  const sensitiveEl = h('button', {
    class: 'bench-sensitive',
    type: 'button',
    text: '',
    title: '敏感词检测（本地词库，不联网；点击定位第一处）',
    hidden: true,
    on: { click: () => locateSensitive() }
  });
  const aiHintEl = h('span', { class: 'bench-ai-hint', text: '' });
  let aiHintTimer = 0;
  foot.append(
    h('div', { class: 'bench-foot-info' }, charsEl, wordsEl, updatedEl, sensitiveEl),
    h('span', { style: { flex: '1' } }),
    saveStateEl,
    dirtyHint,
    aiHintEl
  );

  function flashAiHint(msg) {
    /* AI 辅助能力（幽灵字/续写）失败静默 + 状态栏轻提示，绝不打断输入。 */
    aiHintEl.textContent = msg || '';
    clearTimeout(aiHintTimer);
    if (msg) aiHintTimer = setTimeout(() => { aiHintEl.textContent = ''; }, 6000);
  }

  /* v5.2.0 敏感词检测：本地词库（data/sensitive-words.txt，每行一词 # 注释），
     纯前端字符串匹配，不调 LLM。接口不可用时静默关闭（mock 下用演示词表）。 */
  let sensitiveWords = [];
  let sensitiveHits = [];
  function paintSensitive(text) {
    sensitiveHits = [];
    if (!sensitiveWords.length) { sensitiveEl.hidden = true; return; }
    const t = String(text || '');
    for (const w of sensitiveWords) {
      let idx = t.indexOf(w);
      while (idx >= 0 && sensitiveHits.length < 999) {
        sensitiveHits.push({ w, idx });
        idx = t.indexOf(w, idx + w.length);
      }
    }
    sensitiveHits.sort((a, b) => a.idx - b.idx);
    if (!sensitiveHits.length) { sensitiveEl.hidden = true; sensitiveEl.textContent = ''; return; }
    sensitiveEl.hidden = false;
    sensitiveEl.textContent = `敏感词 ${sensitiveHits.length}`;
  }
  function locateSensitive() {
    if (!sensitiveHits.length) return;
    try {
      editor.revealOffset(sensitiveHits[0].idx);
      flashAiHint(`第一处敏感词：「${sensitiveHits[0].w}」（共 ${sensitiveHits.length} 处）`);
    } catch { /* 定位失败静默 */ }
  }
  (async () => {
    try {
      const r = await request('/api/sensitive-words', { timeout: 8000 });
      sensitiveWords = (r && Array.isArray(r.items) ? r.items : []).filter(Boolean);
    } catch {
      sensitiveWords = IS_MOCK ? [...MOCK_SENSITIVE_WORDS] : [];   // 真实后端未挂载 → 徽标静默关闭
    }
    paintSensitive(editor.getText());
  })();

  const editor = createEditor({
    host: scrollHost,
    placeholder: '这一章还没有正文。可以直接粘贴或开始写作，改动会自动保存并形成版本快照。',
    onChange: (text, ev) => {
      markDirty(text);
      scheduleSave();
      schedulePhoneFill();
      /* v5.2.0：输入差值计数 + 幽灵字防抖重排（Tab 采纳/续写流入由事件自身标注） */
      ghostCtl.notifyText(text, ev || {});
      /* v5.6.0：人物名高亮 120ms 防抖重建（复用防抖保存链路同一钩子；组词期间模块内跳过） */
      namehl.notifyText(text, ev || {});
    },
    onCompositionEnd: () => {
      /* 拼音组词结束：把组词期间欠下的落盘补上 */
      scheduleSave();
      schedulePhoneFill();
      ghostCtl.notifyText(editor.getText(), {});
    }
  });

  /* 划词气泡：选中文字浮出六动作菜单，流式预览不落稿，替换前校验原选区未变。 */
  const selectionBar = mountSelectionBar({
    editor,
    getContext: () => ({
      key,
      n: model.n,
      title: model.title ? `第 ${model.n} 章 ${model.title}` : `第 ${model.n} 章`,
      summary: extractSummary(model.rawText)
    }),
    onReplaced: (r) => {
      /* 替换成功：立即落盘（后端写盘门禁会留「编辑前」快照 + 节流快照）。
         v5.2.0：划词结果属于 AI 插入，按契约上报字符数（统计侧手写/AI 分账）。 */
      if (r && typeof r.start === 'number' && typeof r.end === 'number') {
        emitAiInsert(Math.max(0, r.end - r.start), key);
      }
      flushSave();
    }
  });

  /* v5.2.0 AI 辅助写作挂载：
     · 幽灵字：光标末尾停顿 → 灰字建议 + Tab 采纳（Esc/方向键/输入即清）；
     · 快捷续写：Cmd/Ctrl+/ 流式逐字插入（期间冻结划词、悬挂幽灵字）。
     两者失败静默 + 状态栏轻提示，绝不影响正常输入。 */
  const aiCtx = () => ({
    key,
    n: model.n,
    title: model.title ? `第 ${model.n} 章 ${model.title}` : `第 ${model.n} 章`,
    summary: extractSummary(model.rawText)
  });
  const ghostCtl = mountGhost({
    editor,
    getContext: aiCtx,
    onHint: (msg) => flashAiHint(msg),
    onInsert: ({ chars } = {}) => {
      emitAiInsert(chars, key);
      flushSave();
    }
  });
  const quickWrite = mountQuickWrite({
    editor,
    getContext: aiCtx,
    onHint: (msg) => flashAiHint(msg),
    onInsert: ({ chars } = {}) => {
      emitAiInsert(chars, key);
      flushSave();
    },
    onStart: () => { selectionBar.close(); },
    onEnd: () => { ghostCtl.resync(); }
  });

  /* ══════════════════════════════════════════════════════════════
     v5.5.x A1 全书查找替换浮层（只追加区块）：
     · 查找词回车跳下一个（position/total 导航）、输入防抖即时重扫当前章；
     · 三级替换：替换（当前一处，编辑器内）/ 本章替换（编辑器落盘链路）/
       全书替换（后端 saveChapter 链路，替换前自动留快照可恢复）；
     · 编辑器 docChanged → 结果标 stale + 防抖自动重扫当前章；
     · 「工作文件」视图（raw）时当前章锁定：本地替换停用，全书替换经 skip 跳过
       （纯净域红线：替换绝不写进概要/备注等元信息小节）。
     ══════════════════════════════════════════════════════════════ */
  const findReplace = mountFindReplace({
    editor,
    getContext: () => ({ key, n: model.n }),
    isLocked: () => model.mode === 'raw',
    flushSave,
    onChapterReplaced: () => { loadChapter(); },   // 全书替换波及当前章 → 编辑器内容同步刷新
    onGotoChapter: (no) => { navigate(`/work/${encodeURIComponent(key)}/bench/${Number(no) || 1}`); }
  });

  /* ══════════════════════════════════════════════════════════════
     v5.6.0 编辑器内人物名高亮（只追加区块，全部自研）：
     · CSS Custom Highlight API（CSS.highlights + ::highlight(character-name)）
       ——纯渲染层 Range 标记：零 DOM 文本改动、零正文模型污染，复制/导出/落盘
       路径零接触；浏览器不支持（Chrome/Edge <105、Safari <17.0）静默降级不启用；
     · 名单来源：/api/canvas/graph 人物节点（复用画布聚合，无新增端点，模块级缓存）
       + 用户自定义词（localStorage `ew-name-hl:<bookKey>`）；
     · 文本变化 120ms 防抖重建 Range（组词期间跳过；按段落定位，跨段不标）；
     · 管理面板：bench 头部「高亮」按钮（总开关 / 人物清单 / 自定义词 / 单词开关）。
     ══════════════════════════════════════════════════════════════ */
  const namehl = mountNameHighlight({
    editor,
    getKey: () => key,
    onHint: (msg) => flashAiHint(msg)
  });

  /* v5.5.x A4 结算账本：只读会话事件流（begin/update/commit），退出时弹结算卡。 */
  const settleLedger = createSettleLedger(key);
  settleLedger.attach(document);

  /* ══════════════════════════════════════════════════════════════
     v5.5.x A3 章节导航树挂载（只追加区块）：
     · 数据源 = store.plan（GET /api/plan 既有 refreshPlan 链路下发，无新增端点）；
       深链直进写作台而规划未到时补一次 refreshPlan（guardedRefresh inflight 去重）；
     · store 订阅驱动重画（规划刷新/作品状态变化 → 徽标收敛），不额外轮询；
     · 点击行先 flushSave 再 navigate 切章（复用挂载链路里的 loadChapter）。
     ══════════════════════════════════════════════════════════════ */
  const catRail = createCatalogRail({
    key,
    onGoto: (no) => {
      const target = Number(no) || 1;
      flushSave();
      if (target === model.n) return;
      navigate(`/work/${encodeURIComponent(key)}/bench/${target}`);
    }
  });
  body.insertBefore(catRail.el, editorCol);   // 编辑区左侧窄栏（可收起）
  catRail.setData(store.get().plan, model.n, {
    failed: !!(store.get().features && store.get().features.plan === false)
  });
  const catUnsub = store.subscribe((s) => {
    catRail.setData(s.plan, model.n, { failed: !!(s.features && s.features.plan === false) });
  });
  catRail.attachResize(body);
  if (!store.get().plan && !(store.get().features && store.get().features.plan === false)) {
    ctx.refreshPlan && ctx.refreshPlan();   // 深链直进写作台：补一次规划刷新（既有链路）
  }

  applyTypo();

  /* ------------------------------------------------------------ paint */

  const EXPORT_FMTS = [
    ['md', 'Markdown'],
    ['docx', 'Word'],
    ['epub', 'EPUB'],
    ['print', '打印排版']
  ];

  function exportMenu(anchor) {
    const items = EXPORT_FMTS.map(([fmt, label]) => ({
      label: `导出全书 · ${label}`,
      icon: 'download',
      onSelect: async () => {
        try {
          toast(`正在导出 ${label}（正文已去噪）…`, { duration: 2400 });
          const r = await api.exportWork(key, fmt);
          if (r && r.cancelled) return;
          const where = r && r.saved ? `已保存到 ${r.saved}` : `${label} 已开始下载`;
          toast(where, { tone: 'success', duration: 3200 });
        } catch (err) {
          toast(errorText(err, '导出失败'), { tone: 'danger', duration: 4000 });
        }
      }
    }));
    popoverMenu(anchor, items, { label: '导出整本（仅正文）' });
  }

  /* 正文 / 工作文件 切换（v4.22.2 沿用）。只在两者确实不同时出现。 */
  const modeSwitch = h('div', { class: 'bench-mode', role: 'tablist', style: { display: 'none' } });
  function paintModeSwitch() {
    const differ = (model.rawText || '') !== (model.text || '');
    modeSwitch.style.display = differ ? '' : 'none';
    modeSwitch.textContent = '';
    if (!differ) return;
    for (const [m, label, tip] of [
      ['prose', '正文', '章题 + 章首引子 + 正文（默认，可复制/可发布）'],
      ['raw', '工作文件', '含本章概要 / 章节备注 / 历史残留，仅供核对']
    ]) {
      modeSwitch.append(h('button', {
        class: `bench-mode-btn${model.mode === m ? ' is-on' : ''}`,
        type: 'button',
        role: 'tab',
        'aria-selected': model.mode === m ? 'true' : 'false',
        title: tip,
        text: label,
        dataset: { mode: m },
        on: {
          click: () => {
            if (model.mode === m) return;
            if (model.dirty) {
              toast('先保存当前改动，再切换视图', { tone: 'danger', duration: 2200 });
              return;
            }
            model.mode = m;
            editor.setText(m === 'raw' ? model.rawText : model.text, { silent: true });
            model.savedText = editor.getText();
            model.dirty = false;
            paintModeSwitch();
            paintAll();
            namehl.refresh();   /* v5.6.0：正文/工作文件口径切换后重算高亮区间 */
          }
        }
      }));
    }
  }

  function paintHead() {
    head.textContent = '';
    const work = store.get().work;
    head.append(
      h('button', {
        class: 'icon-btn',
        type: 'button',
        'aria-label': '返回对话',
        title: '返回对话',
        html: icon('arrow-left', 16),
        on: { click: () => navigate(`/work/${encodeURIComponent(key)}`) }
      }),
      h(
        'div',
        { class: 'bench-title' },
        h('span', { class: 'bt-name', text: model.title || `第 ${model.n} 章` }),
        h('span', {
          class: 'bt-sub',
          text: work ? `${work.name} · 第 ${model.n} / ${work.total || '?'} 章` : `第 ${model.n} 章`
        })
      ),
      h('span', { style: { flex: '1' } }),
      modeSwitch,
      /* v5.1.0：排版面板入口 */
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        title: '排版设置（字体/字号/行高/行宽/网格线/打字机）',
        html: `${icon('type', 13)}<span>排版</span>`,
        on: { click: (event) => openTypoPanel(event.currentTarget) }
      }),
      /* v5.1.0：手机阅读预览抽屉 */
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        title: '手机阅读预览（滚动与编辑器同步）',
        html: `${icon('eye', 13)}<span>预览</span>`,
        on: { click: () => togglePhone() }
      }),
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        title: '导出整本（章题 + 章首引子 + 正文，不含任何概要/备注/修改说明/评分）',
        html: `${icon('download', 13)}<span>导出全书</span>`,
        on: { click: (event) => exportMenu(event.currentTarget) }
      }),
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        html: `${icon('copy', 13)}<span>复制全文</span>`,
        on: {
          click: async () => {
            try {
              /* v4.22.1 口径不变：复制「章题+引子+正文」（剥全部元信息小节），基于编辑器实时内容 */
              const raw = cleanChapterText(editor.getText()) || editor.getText();
              await copyText(raw);
              toast('已复制到剪贴板（章题+引子+正文，不含概要与任何说明/评分）', { tone: 'success', duration: 2400 });
            } catch {
              toast('复制失败，请手动选择文本', { tone: 'danger' });
            }
          }
        }
      }),
      /* v5.5.x A1：全书查找替换入口（浮层可拖拽，只作用于正文域） */
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        title: '全书查找替换（只作用于正文，概要/备注不计入）',
        html: `${icon('search', 13)}<span>查找</span>`,
        on: { click: () => findReplace.toggle() }
      }),
      /* v5.6.0：人物名高亮管理入口（CSS Custom Highlight API 纯渲染层，不动正文内容） */
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        title: '人物名高亮（编辑器内标记人物与自定义词，复制/导出内容不受影响）',
        html: `${icon('users', 13)}<span>高亮</span>`,
        on: { click: (event) => namehl.openPanel(event.currentTarget) }
      }),
      /* v5.4.x：听书胶囊（播放/暂停 · 语速 · 停止 · 句进度 · 当前句字幕，区块见下） */
      ttsCapsule,
      saveBtn
    );
  }

  /* foot 里的计数与保存状态节点常驻（输入路径只改 textContent，不整行重画）。 */

  function paintFoot() {
    wordsEl.textContent = '';
    updatedEl.textContent = '';
    wordsEl.append(model.words ? h('span', { text: `记录字数 ${fmtWords(model.words)}` }) : null);
    updatedEl.append(model.updatedAt ? h('span', { text: `上次保存 ${fmtDate(model.updatedAt, true)}` }) : null);
    paintSaveState();
  }

  function paintSaveState() {
    /* v5.1.0 保存状态图标：数据库=已存 / 时钟=待存 / 转圈=保存中 / 叹号=失败 */
    const st = model.saveState;
    const map = {
      saved: ['database', '已存'],
      pending: ['clock', '待存'],
      saving: ['refresh', '保存中'],
      error: ['alert', '保存失败 · 点「保存」重试']
    };
    const [ic, label] = map[st] || map.saved;
    saveStateEl.dataset.state = st;
    saveStateEl.textContent = '';
    saveStateEl.append(h('span', { html: icon(ic, 12) }), h('span', { text: label }));
    dirtyHint.textContent = '';
    if (model.dirty || pendingSave) {
      dirtyHint.className = 'bench-dirty';
      dirtyHint.textContent = '有未保存改动 · Cmd/Ctrl+S 立即保存';
    } else {
      dirtyHint.className = 'hint-text';
      dirtyHint.textContent = '';
    }
    saveBtn.dataset.state = st;
    saveBtn.disabled = model.loading || (!model.dirty && !pendingSave) || saving;
    saveBtn.textContent = saving ? '保存中…' : (model.dirty || pendingSave) ? '保存改动' : '已保存';
  }

  function paintSide() {
    side.textContent = '';
    side.append(
      h(
        'div',
        { class: 'bench-side-head' },
        h('span', { text: '版本历史' }),
        h('span', { class: 'count', text: `${model.versions.length} 份` })
      )
    );
    const list = h('div', { class: 'bench-side-body' });
    if (!model.versions.length) {
      list.append(h('div', { class: 'hint-text', text: '还没有版本。第一次保存后按节流规则自动留档（距上版 5 分钟或改动 200 字），自动快照每章保留 20 份。' }));
    }
    for (const v of model.versions) {
      list.append(
        h(
          'div',
          { class: 'bench-version' },
          h(
            'div',
            { class: 'bv-top' },
            h('span', { class: 'bv-label', text: v.label || v.id || '快照' }),
            h('span', {
              class: 'bv-meta',
              text: `${v.ts ? relTime(v.ts) : ''}${v.size ? ` · ${fmtWords(v.size)}` : ''}`
            })
          ),
          h(
            'div',
            { class: 'bv-actions' },
            h('button', { class: 'btn btn-sm', type: 'button', text: '对比', on: { click: () => showVersionModal(v) } }),
            h('button', {
              class: 'btn btn-sm',
              type: 'button',
              text: '恢复',
              on: {
                click: async () => {
                  const ok = await confirmDialog({
                    title: '恢复这个版本？',
                    message: '当前正文会先自动存一份「恢复前」快照，随时可以再切回来。',
                    confirmText: '恢复'
                  });
                  if (!ok) return;
                  try {
                    /* v5.1.0：走新恢复端点（PUT），恢复前服务端先落「恢复前」快照 */
                    await request('/api/chapter/versions/restore', {
                      method: 'PUT',
                      body: { key, n: model.n, id: v.id },
                      timeout: 60000
                    });
                    toast('已恢复该版本', { tone: 'success' });
                    await loadChapter();
                  } catch (err) {
                    toast(errorText(err, '恢复失败'), { tone: 'danger' });
                  }
                }
              }
            })
          )
        )
      );
    }
    side.append(list);
  }

  function paintAll() {
    paintHead();
    paintModeSwitch();
    paintFoot();
    paintSide();
    paintSaveState();
  }

  /* ------------------------------------------------------------- data */

  async function loadChapter() {
    model.loading = true;
    model.error = '';
    /* v5.2.0：切章前结算上一章会话（commit）+ 中止续写/幽灵字在途请求 */
    session.commitIfActive(editor.getText());
    quickWrite.stop();
    ttsStop();   /* v5.4.x：切章/重载先停听书（位置已按句记忆，重进本章可「继续听」） */
    ghostCtl.suspend();
    editor.setText('', { silent: true });
    scrollHost.style.opacity = '0.5';
    paintAll();
    try {
      const [ch, vers] = await Promise.all([api.chapter(key, model.n), api.chapterVersions(key, model.n)]);
      model.title = ch.title || '';
      /* v4.22.2 沿用：编辑器默认装「纯净正文」；工作文件收进 rawText 供核对；
         保存时后端会把概要/备注补回去，不会丢元信息。 */
      model.rawText = ch.rawText || ch.text || '';
      model.text = ch.cleanText || cleanChapterText(model.rawText) || model.rawText;
      model.savedText = model.mode === 'raw' ? model.rawText : model.text;
      model.words = ch.words || 0;
      model.updatedAt = ch.updatedAt || '';
      model.versions = (vers && vers.items) || [];
      editor.setText(model.mode === 'raw' ? model.rawText : model.text, { silent: true });
      markDirty(editor.getText());
      /* v5.2.0：新章会话开始（统计侧以此对齐手写基线）+ 输入基线/敏感词重算 */
      session.begin(editor.getText());
      ghostCtl.resync();
      paintSensitive(editor.getText());
    } catch (err) {
      model.error = errorText(err, '章节加载失败');
    }
    model.loading = false;
    model.dirty = false;
    model.saveState = 'saved';
    scrollHost.style.opacity = '';
    paintAll();
  }

  function markDirty(text) {
    const t = typeof text === 'string' ? text : editor.getText();
    model.dirty = t !== model.savedText;
    charsEl.textContent = `${t.length.toLocaleString('zh-CN')} 字符`;
    if (!model.dirty && !pendingSave) model.saveState = 'saved';
    paintSaveState();
  }

  function scheduleSave() {
    /* 160ms 防抖：连打不打盘；拼音组词期间由 compositionend 补触发。 */
    if (editor.isComposing()) return;
    clearTimeout(saveTimer);
    model.saveState = model.dirty || pendingSave ? 'pending' : model.saveState;
    paintSaveState();
    saveTimer = setTimeout(() => {
      /* v5.2.0：防抖落盘的文本变化 → 会话 update 上报（节流 ≤1/s）+ 敏感词计数刷新 */
      const text = editor.getText();
      session.update(text);
      paintSensitive(text);
      flushSave(false);
    }, 160);
  }

  async function flushSave() {
    if (editor.isComposing()) { pendingSave = true; return; }
    if (model.loading) return;
    if (saving) { pendingSave = true; return; }
    clearTimeout(saveTimer);
    if (!model.dirty && !pendingSave) { model.saveState = 'saved'; paintSaveState(); return; }
    await doSave();
  }

  async function doSave() {
    /* 保存互斥：上请求未返回期间再脏，只记 pendingSave，返回后补跑一次（合并脏标记）。 */
    saving = true;
    pendingSave = false;
    model.saveState = 'saving';
    paintSaveState();
    const text = editor.getText();
    try {
      const res = await api.saveChapter(key, model.n, text, model.mode);
      model.savedText = text;
      if (model.mode === 'prose') model.text = text; else model.rawText = text;
      model.dirty = false;
      model.saveState = pendingSave ? 'pending' : 'saved';
      model.words = (res && res.words) || model.words;
      model.updatedAt = (res && res.updatedAt) || new Date().toISOString();
      paintFoot();
      refreshVersionsSoon();
      ctx.refreshWork && ctx.refreshWork();
      catRail.onSaved(model.n, model.words);   /* v5.5.x A3：本会话保存章的徽标就地升级 */
    } catch (err) {
      model.saveState = 'error';
      toast(errorText(err, '保存失败，请重试'), { tone: 'danger' });
    }
    saving = false;
    paintSaveState();
    if (pendingSave && model.dirty) {
      /* 补跑：把保存期间的新改动再落一次 */
      pendingSave = false;
      await doSave();
    } else {
      pendingSave = false;
    }
  }

  async function refreshVersionsSoon() {
    /* 版本列表刷新节流（5s）：边写边存高频落盘时不刷屏。 */
    const now = Date.now();
    if (now - lastVersionsRefresh < 5000) return;
    lastVersionsRefresh = now;
    try {
      const vers = await api.chapterVersions(key, model.n);
      model.versions = (vers && vers.items) || [];
      paintSide();
    } catch {
      /* 版本列表刷新失败不影响保存结果 */
    }
  }

  /* ------------------------------------------------------------- 排版面板 */

  let typoPanel = null;
  let typoOutside = null;

  function closeTypoPanel() {
    if (typoPanel) { typoPanel.remove(); typoPanel = null; }
    if (typoOutside) { document.removeEventListener('pointerdown', typoOutside, true); typoOutside = null; }
  }

  function applyTypo() {
    /* CSS 变量挂纸面容器 .bench-paper（手机预览 .bp-frame 也继承同一套排版）。 */
    const font = TYPO_FONTS.find((f) => f[0] === typo.font) || TYPO_FONTS[0];
    paper.style.setProperty('--editor-font-family', font[2]);
    paper.style.setProperty('--editor-font-size', `${typo.fontSize}px`);
    paper.style.setProperty('--editor-line-height', String(typo.lineHeight));
    paper.style.setProperty('--editor-width', `${typo.width}%`);
    paper.style.setProperty('--editor-para-gap', typo.paraGap ? '0.6em' : '0');
    paper.style.setProperty('--editor-indent', typo.indent ? '2em' : '0');
    paper.style.setProperty('--ruler-step', `${Math.round(typo.fontSize * typo.lineHeight)}px`);
    paper.dataset.ruler = typo.ruler;
    editor.setParagraphFocus(typo.paraFocus);
    editor.setTypewriter(typo.typewriter);
    if (typo.typewriter) editor.maybeScroll();
  }

  /* 面板不整体重建（拖滑杆时不丢拖拽状态）：每个 chip 组登记一个刷新器，
     setTypo 后就地更新 is-on 标记。 */
  const chipRefreshers = [];

  function setTypo(patch) {
    typo = { ...typo, ...patch };
    saveTypo(typo);
    applyTypo();
    for (const refresh of chipRefreshers) refresh();
  }

  function typoSlider(label, min, max, step, value, onInput, fmt) {
    const val = h('span', { class: 'bt-val', text: fmt(value) });
    const input = h('input', { type: 'range', min: String(min), max: String(max), step: String(step), value: String(value) });
    input.addEventListener('input', () => {
      const v = Number(input.value);
      val.textContent = fmt(v);
      onInput(v);
    });
    return h('div', { class: 'bt-row' }, h('span', { class: 'bt-label', text: label }), input, val);
  }

  function typoChips(label, options, getKey, onPick) {
    const chips = h('div', { class: 'bt-chips' });
    const btns = [];
    for (const [ckey, name] of options) {
      const b = h('button', {
        class: 'bt-chip',
        type: 'button',
        text: name,
        on: { click: () => onPick(ckey) }
      });
      btns.push([b, ckey]);
      chips.append(b);
    }
    chipRefreshers.push(() => {
      const cur = getKey();
      for (const [b, ckey] of btns) b.classList.toggle('is-on', cur === ckey);
    });
    return h('div', { class: 'bt-row' }, h('span', { class: 'bt-label', text: label }), chips);
  }

  function openTypoPanel(anchor) {
    closeTypoPanel();
    chipRefreshers.length = 0;
    const rect = (anchor && anchor.getBoundingClientRect()) || { right: 200, bottom: 60 };
    typoPanel = h('div', { class: 'bench-typo' });
    typoPanel.style.position = 'fixed';
    typoPanel.style.zIndex = '110';
    typoPanel.style.background = 'var(--bg-elevated)';
    typoPanel.style.border = '1px solid var(--line-3)';
    typoPanel.style.borderRadius = 'var(--radius-md)';
    typoPanel.style.boxShadow = '0 12px 36px rgba(9, 12, 20, 0.18)';
    typoPanel.append(
      typoChips('字体', TYPO_FONTS.map(([k, name]) => [k, name]), () => typo.font, (k) => setTypo({ font: k })),
      typoSlider('字号', 12, 28, 1, typo.fontSize, (v) => setTypo({ fontSize: v }), (v) => `${v}px`),
      typoSlider('行高', 1.4, 3.0, 0.05, typo.lineHeight, (v) => setTypo({ lineHeight: v }), (v) => v.toFixed(2)),
      typoSlider('行宽', 30, 100, 1, typo.width, (v) => setTypo({ width: v }), (v) => `${v}%`),
      typoChips('段间', [['on', '空行'], ['off', '紧凑']], () => (typo.paraGap ? 'on' : 'off'),
        (k) => setTypo({ paraGap: k === 'on' })),
      typoChips('缩进', [['on', '首行两字'], ['off', '无']], () => (typo.indent ? 'on' : 'off'),
        (k) => setTypo({ indent: k === 'on' })),
      typoChips('网格线', [['none', '无'], ['solid', '实线'], ['dashed', '虚线']], () => typo.ruler,
        (k) => setTypo({ ruler: k })),
      typoChips('打字机', [['on', '开'], ['off', '关']], () => (typo.typewriter ? 'on' : 'off'),
        (k) => setTypo({ typewriter: k === 'on' })),
      typoChips('聚焦', [['on', '段落聚焦'], ['off', '关']], () => (typo.paraFocus ? 'on' : 'off'),
        (k) => setTypo({ paraFocus: k === 'on' })),
      h('div', { class: 'bt-row' },
        h('span', { class: 'bt-label' }),
        h('button', {
          class: 'bt-chip', type: 'button', text: '恢复默认排版',
          on: { click: () => setTypo({ ...TYPO_DEFAULTS }) }
        })
      )
    );
    for (const refresh of chipRefreshers) refresh();
    document.body.append(typoPanel);
    const w = typoPanel.offsetWidth || 300;
    const left = Math.max(10, Math.min(rect.right - w, (window.innerWidth || 1200) - w - 10));
    typoPanel.style.left = `${left}px`;
    typoPanel.style.top = `${Math.min((rect.bottom || 60) + 8, (window.innerHeight || 800) - typoPanel.offsetHeight - 10)}px`;
    typoOutside = (event) => {
      if (typoPanel && typoPanel.contains(event.target)) return;
      if (anchor && anchor.contains(event.target)) return;
      closeTypoPanel();
    };
    document.addEventListener('pointerdown', typoOutside, true);
  }

  /* ------------------------------------------------------------- 手机预览抽屉 */

  let phoneEl = null;
  let phoneFrame = null;
  let phoneSyncing = false;
  let fillPhoneTimer = 0;

  function schedulePhoneFill() {
    /* 输入期间预览内容节流刷新（600ms），不抢主线程。 */
    if (!phoneFrame) return;
    clearTimeout(fillPhoneTimer);
    fillPhoneTimer = setTimeout(fillPhone, 600);
  }

  function togglePhone() {
    if (phoneEl) { closePhone(); return; }
    openPhone();
  }

  function closePhone() {
    if (phoneEl) { phoneEl.remove(); phoneEl = null; phoneFrame = null; }
  }

  function phoneParagraphs() {
    /* 预览 = 读者口径：剥章题与一切元信息小节（铁律 0），再按空行分段渲染。 */
    const body = cleanChapterText(editor.getText(), { keepTitle: false }) || '';
    return body.split(/\n{2,}/).filter((p) => p.trim()).map((p) => p.replace(/\n/g, ' '));
  }

  function fillPhone() {
    if (!phoneFrame) return;
    phoneFrame.textContent = '';
    for (const p of phoneParagraphs()) phoneFrame.append(h('p', { class: 'bp-para', text: p }));
    if (!phoneFrame.childElementCount) {
      phoneFrame.append(h('p', { class: 'bp-para hint-text', text: '（正文为空）' }));
    }
  }

  /* 滚动双向同步（带防回环标志）：编辑器滚 → 预览滚；预览滚 → 编辑器滚。
     监听挂 renderBench 作用域，destroy 时能精确摘除。 */
  function scrollRatio(el) {
    const max = el.scrollHeight - el.clientHeight;
    return max > 0 ? el.scrollTop / max : 0;
  }

  function onBenchScroll() {
    if (phoneSyncing || !phoneFrame) return;
    phoneSyncing = true;
    const max = phoneFrame.scrollHeight - phoneFrame.clientHeight;
    phoneFrame.scrollTop = max * scrollRatio(scrollHost);
    requestAnimationFrame(() => { phoneSyncing = false; });
  }

  function openPhone() {
    closeTypoPanel();
    phoneEl = h('div', { class: 'bench-phone', role: 'dialog', 'aria-label': '手机阅读预览' });
    const slider = h('input', {
      type: 'range', min: '320', max: '430', step: '1',
      value: String(typo.phoneWidth || 375),
      'aria-label': '手机屏宽'
    });
    phoneFrame = h('div', { class: 'bp-frame' });
    fillPhone();
    scrollHost.addEventListener('scroll', onBenchScroll);
    phoneFrame.addEventListener('scroll', () => {
      if (phoneSyncing) return;
      phoneSyncing = true;
      const max = scrollHost.scrollHeight - scrollHost.clientHeight;
      scrollHost.scrollTop = max * scrollRatio(phoneFrame);
      requestAnimationFrame(() => { phoneSyncing = false; });
    });
    slider.addEventListener('input', () => {
      phoneFrame.style.setProperty('--phone-width', `${slider.value}px`);
      typo.phoneWidth = Number(slider.value);
      saveTypo(typo);
    });
    phoneEl.append(
      h('div', { class: 'bp-bar' },
        h('span', { text: '手机阅读预览' }),
        slider,
        h('button', {
          class: 'btn btn-sm', type: 'button', text: '关闭',
          on: { click: closePhone }
        })
      ),
      phoneFrame
    );
    document.body.append(phoneEl);
  }

  /* ══════════════════════════════════════════════════════════════
     v5.4.x 听书胶囊（Web Speech API 纯前端，附加能力——任何失败静默，不影响写作主链路）：
     · 朗读文本先过 cleanChapterText 纯净域（只读章题+引子+正文，不读概要/备注）；
     · 逐句朗读 + 当前句定位：编辑器内 revealOffset（core/editor.js）优先，
       找不到/失败时落到手机预览段落高亮；胶囊内始终显示当前句字幕；
     · 语速循环 0.8/1.0/1.25/1.5（core/tts.js RATES）；暂停/继续/停止；
     · 位置记忆（书+章+句，localStorage），重进本章询问「继续听？」；
     · 播放期间 typingsound.mute(true) 自动静音打字音，结束/停止恢复。
     样式全部内联（不新增 css 文件）；SpeechSynthesis 不可用时点击给降级 toast。 */
  const ttsCtl = createTTS({
    book: key,
    chapter: model.n,
    onSentence: (_i, sent) => {
      paintTtsState();
      highlightTtsSentence(sent);
    },
    onState: () => paintTtsState()
  });
  let ttsSearchFrom = 0;     /* 编辑器内当前句检索游标（防重复句误定位） */
  let ttsSpeakingEl = null;  /* 预览面板当前高亮段落 */

  function ttsCurrentText() {
    /* 纯净域：朗读口径 = 复制全文口径（章题+引子+正文，剥全部元信息小节）。 */
    return cleanChapterText(editor.getText()) || editor.getText();
  }

  function setTtsSpeaking(el) {
    if (ttsSpeakingEl === el) return;
    if (ttsSpeakingEl) ttsSpeakingEl.style.removeProperty('background');
    ttsSpeakingEl = el;
    if (el) el.style.background = 'var(--bg-selection)';
  }

  function highlightTtsSentence(sent) {
    setTtsSpeaking(null);
    if (!sent) return;
    /* 1) 编辑器内定位：句文本在编辑器内容里检索（容忍 re-clean 后的轻微偏移） */
    try {
      const t = editor.getText();
      const probe = String(sent.text || '').trim().slice(0, 24);
      if (probe) {
        const off = t.indexOf(probe, ttsSearchFrom);
        if (off >= 0) {
          ttsSearchFrom = off + probe.length;
          editor.revealOffset(off);
          return;
        }
        if (ttsSearchFrom > 0) {
          const off2 = t.indexOf(probe);   /* 游标后找不到：回卷全文再找一次 */
          if (off2 >= 0) {
            ttsSearchFrom = off2 + probe.length;
            editor.revealOffset(off2);
            return;
          }
        }
      }
    } catch { /* 编辑器定位失败 → 落预览面板 */ }
    /* 2) 预览面板段落高亮（抽屉打开时生效；关着时字幕仍在胶囊里可见） */
    try {
      if (phoneFrame) {
        const probe = String(sent.text || '').trim().slice(0, 12);
        for (const p of phoneFrame.querySelectorAll('.bp-para')) {
          if (probe && p.textContent.indexOf(probe) >= 0) {
            setTtsSpeaking(p);
            p.scrollIntoView({ block: 'center' });
            break;
          }
        }
      }
    } catch { /* 预览高亮失败静默 */ }
  }

  function paintTtsState() {
    const playing = ttsCtl.state === 'playing';
    const paused = ttsCtl.state === 'paused';
    const active = playing || paused;
    ttsMainBtn.textContent = '';
    ttsMainBtn.append(h('span', {
      html: playing ? icon('pause', 13) : paused ? icon('play', 13) : icon('play', 13)
    }), h('span', { text: playing ? '暂停' : paused ? '继续' : '听本章' }));
    ttsRateBtn.hidden = !active;
    ttsRateBtn.textContent = `${ttsCtl.getRate()}×`;
    ttsStopBtn.hidden = !active;
    ttsStatusEl.textContent = active && ttsCtl.total ? `${Math.min(ttsCtl.index + 1, ttsCtl.total)} / ${ttsCtl.total} 句` : '';
    const cur = ttsCtl.sentenceAt(ttsCtl.index);
    ttsSubtitleEl.textContent = active && cur ? String(cur.text || '').trim().slice(0, 42) : '';
  }

  async function startTTS() {
    if (!ttsSupported()) {
      /* 优雅降级：浏览器不支持 Web Speech API，只提示不改其他能力 */
      toast('当前浏览器不支持语音合成（Web Speech API），请改用较新的 Chrome / Edge / Safari 再试', { tone: 'danger', duration: 4000 });
      return;
    }
    if (ttsCtl.state === 'playing') { ttsCtl.pause(); paintTtsState(); return; }
    if (ttsCtl.state === 'paused') { ttsCtl.resume(); paintTtsState(); return; }
    const text = ttsCurrentText();
    if (!text.trim()) { toast('这一章还没有正文，先写点内容再听', { tone: 'danger' }); return; }
    if (!hasChineseVoice()) toast('未检测到中文语音包，将使用系统默认语音朗读', { duration: 2600 });
    let from = 0;
    const pos = loadPos(key, model.n);
    if (pos && pos.index > 0) {
      /* 听书位置记忆：下次进同一章询问「继续听？」（取消则从头听） */
      const go = await confirmDialog({
        title: '继续上次的听书？',
        message: `这本书上次听到第 ${pos.index + 1} 句。`,
        confirmText: '继续听',
        cancelText: '从头听'
      });
      from = go ? pos.index : 0;
    }
    ttsSearchFrom = 0;
    const total = ttsCtl.start(text, from);
    if (!total) toast('没有可朗读的内容', { tone: 'danger' });
    paintTtsState();
  }

  function ttsStop() {
    /* 听书收尾（停止按钮 / 切章 / 卸载共用）：停朗读并恢复打字音，失败静默 */
    try {
      ttsCtl.stop();
      paintTtsState();
    } catch { /* 听书失败静默 */ }
  }

  const ttsMainBtn = h('button', {
    class: 'btn btn-sm',
    type: 'button',
    title: '听本章（逐句朗读，播放时自动静音打字音）',
    html: `${icon('play', 13)}<span>听本章</span>`,
    on: { click: () => { startTTS().catch(() => {}); } }
  });
  const ttsRateBtn = h('button', {
    class: 'btn btn-sm',
    type: 'button',
    hidden: true,
    title: '切换语速（0.8 / 1.0 / 1.25 / 1.5）',
    text: '1.0×',
    on: { click: () => { ttsCtl.cycleRate(); paintTtsState(); } }
  });
  const ttsStopBtn = h('button', {
    class: 'btn btn-sm',
    type: 'button',
    hidden: true,
    title: '停止听书',
    html: icon('stop', 13),
    on: { click: () => ttsStop() }
  });
  const ttsStatusEl = h('span', { class: 'hint-text' });
  const ttsSubtitleEl = h('span', {
    class: 'hint-text',
    style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '280px' }
  });
  const ttsCapsule = h('div', {
    class: 'bench-tts',
    role: 'group',
    'aria-label': '听书控制',
    style: { display: 'flex', alignItems: 'center', gap: '6px', minWidth: '0' }
  }, ttsMainBtn, ttsRateBtn, ttsStopBtn, ttsStatusEl, ttsSubtitleEl);

  /* ------------------------------------------------------------- 版本对比（内容 ⇄ 对比） */

  /* 自研逐行 + 行内字符级 diff：最长公共子序列（LCS）。行数/行宽都钳制防长章卡顿。 */
  function lcsOps(a, b, eq) {
    const n = a.length;
    const m = b.length;
    const dp = [];
    for (let i = 0; i <= n; i += 1) dp.push(new Int32Array(m + 1));
    for (let i = n - 1; i >= 0; i -= 1) {
      for (let j = m - 1; j >= 0; j -= 1) {
        dp[i][j] = eq(a[i], b[j]) ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    const ops = [];
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (eq(a[i], b[j])) { ops.push({ type: 'same', a: a[i], b: b[j] }); i += 1; j += 1; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push({ type: 'del', a: a[i] }); i += 1; }
      else { ops.push({ type: 'add', b: b[j] }); j += 1; }
    }
    while (i < n) { ops.push({ type: 'del', a: a[i] }); i += 1; }
    while (j < m) { ops.push({ type: 'add', b: b[j] }); j += 1; }
    return ops;
  }

  function inlineDiff(oldLine, newLine) {
    /* 行内字符级：把一行里的改动段用双色标出（del 段红 / add 段绿）。 */
    const MAXC = 320;
    const A = [...String(oldLine)].slice(0, MAXC);
    const B = [...String(newLine)].slice(0, MAXC);
    const ops = lcsOps(A, B, (x, y) => x === y);
    const segs = [];
    for (const op of ops) {
      const last = segs[segs.length - 1];
      if (last && last.type === op.type) {
        last.text += op.type === 'same' ? op.a : (op.type === 'del' ? op.a : op.b);
      } else {
        segs.push({ type: op.type, text: op.type === 'same' ? op.a : (op.type === 'del' ? op.a : op.b) });
      }
    }
    return segs;
  }

  function buildDiffView(oldText, newText) {
    const wrapEl = h('div', { class: 'diff-view' });
    const A = String(oldText || '').split('\n');
    const B = String(newText || '').split('\n');
    const ops = lcsOps(A, B, (x, y) => x === y);
    /* 相邻 del/add 配对做行内标记；没有变化的行灰字带出上下文。 */
    for (let k = 0; k < ops.length; k += 1) {
      const op = ops[k];
      if (op.type === 'same') {
        wrapEl.append(h('div', { class: 'diff-line meta' }, h('span', { class: 'dl-sign', text: '' }), h('span', { text: op.a || ' ' })));
        continue;
      }
      if (op.type === 'del') {
        const next = ops[k + 1];
        if (next && next.type === 'add') {
          k += 1;
          const segs = inlineDiff(op.a, next.b);
          wrapEl.append(h('div', { class: 'diff-line del' },
            h('span', { class: 'dl-sign', text: '-' }),
            h('span', {}, segs.filter((s) => s.type !== 'add').map((s) =>
              s.type === 'del' ? h('mark', { class: 'di-del', text: s.text }) : document.createTextNode(s.text)))),
            h('div', { class: 'diff-line add' },
              h('span', { class: 'dl-sign', text: '+' }),
              h('span', {}, segs.filter((s) => s.type !== 'del').map((s) =>
                s.type === 'add' ? h('mark', { class: 'di-add', text: s.text }) : document.createTextNode(s.text))))
          );
        } else {
          wrapEl.append(h('div', { class: 'diff-line del' }, h('span', { class: 'dl-sign', text: '-' }),
            h('span', { text: op.a || ' ' })));
        }
        continue;
      }
      /* add 没有相邻 del */
      wrapEl.append(h('div', { class: 'diff-line add' }, h('span', { class: 'dl-sign', text: '+' }),
        h('span', { text: op.b || ' ' })));
    }
    return wrapEl;
  }

  async function showVersionModal(v) {
    /* v5.1.0 历史版本弹窗：内容 ⇄ 对比 双视图（对比为本地 LCS 双色 diff）。 */
    let loaded = null;
    const tabs = h('div', { class: 'bench-mode', style: { marginBottom: '10px' } });
    const content = h('div', {});
    const modal = openModal({
      title: `版本：${v.label || v.id}${v.ts ? ` · ${v.ts}` : ''}`,
      body: h('div', {}, tabs, content),
      size: 'lg'
    });
    const paintTab = (which, text, diffTarget) => {
      tabs.textContent = '';
      content.textContent = '';
      for (const [w, label] of [['content', '内容'], ['diff', '对比']]) {
        tabs.append(h('button', {
          class: `bench-mode-btn${which === w ? ' is-on' : ''}`,
          type: 'button', text: label,
          on: { click: () => paintTab(w, text, diffTarget) }
        }));
      }
      if (which === 'content') {
        const pre = h('div', { class: 'diff-view', style: { fontFamily: 'var(--font-reading)', fontSize: '13px' } });
        for (const p of String(text || '').split(/\n{2,}/)) {
          pre.append(h('div', { class: 'diff-line', style: { color: 'var(--ink-primary)' } }, h('span', { text: p || ' ' })));
        }
        content.append(pre);
      } else {
        content.append(buildDiffView(text, diffTarget));
      }
    };
    try {
      const res = await request('/api/chapter/version', { query: { key, n: model.n, id: v.id } });
      loaded = String((res && res.text) || '');
      paintTab('content', loaded, editor.getText());
    } catch (err) {
      content.textContent = '';
      content.append(h('div', { class: 'msg-error', text: errorText(err, '版本内容加载失败') }));
    }
    return modal;
  }

  /* ------------------------------------------------------------- 全局键鼠 */

  const onKey = (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
      event.preventDefault();
      flushSave();
    }
  };
  document.addEventListener('keydown', onKey);

  const onBeforeUnload = (event) => {
    if (model.dirty || pendingSave) {
      /* 有未落盘改动：拦一次让用户确认（浏览器原生弹窗），不做后台补传
         （sendBeacon 只能 POST，与 PUT /api/chapter 语义不符，宁拦不猜）。 */
      event.preventDefault();
      event.returnValue = '';
    }
  };
  window.addEventListener('beforeunload', onBeforeUnload);

  /* ------------------------------------------------------------ start */

  paintAll();
  loadChapter();

  return {
    el: view,
    focusComposer: () => editor.focus(),
    refresh: loadChapter,
    destroy() {
      /* v5.2.0：卸载结算会话（commit），再摘 AI 辅助与监听 */
      session.commitIfActive(editor.getText());
      clearTimeout(saveTimer);
      clearTimeout(fillPhoneTimer);
      clearTimeout(aiHintTimer);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('beforeunload', onBeforeUnload);
      scrollHost.removeEventListener('scroll', onBenchScroll);
      closeTypoPanel();
      closePhone();
      ghostCtl.destroy();
      quickWrite.destroy();
      ttsStop();   /* v5.4.x：卸载时停止听书并恢复打字音（mute 复位） */
      selectionBar.destroy();
      if (chatFloat) chatFloat.destroy();
      findReplace.destroy();
      namehl.destroy();   /* v5.6.0：摘人物名高亮注册（CSS.highlights）与管理面板 */
      catUnsub();          /* v5.5.x A3：摘掉 store 订阅与目录窄栏 */
      catRail.destroy();
      editor.destroy();
      view.remove();
      /* v5.5.x A4：退出写作台结算卡（本会话净增字数 >0 才弹；统计只读不写，失败静默）。
         commitIfActive 已在 destroy 开头结算最后一章，此刻账本即本会话净增。 */
      try { showBenchSettleCard(settleLedger.chars()); } catch { /* 结算是附加能力 */ }
      settleLedger.detach(document);
    }
  };
}
