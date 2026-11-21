// v5.4.0 码字统计与首页改造：node 直跑回归（不依赖 pytest / 浏览器）。
// 覆盖面：
//   1) welcome.js 自研文案库：诗句 20 句全中文、时段问候齐全；
//   2) welcome.js 灵感速记 #标签 提取（与后端同口径：去重/上限/超长不收/边界）；
//   3) welcome.js 首页结构保留：文体快捷入口卡（quick-grid）不删、新卡片类名齐备；
//   4) stats.js SVG 折线函数纯函数性（给数据出点位：递增 x、有界 y、单点居中、空集）；
//   5) stats.js 日历格函数纯函数性（整周补齐、给定月的天数）；
//   6) writestats-client.js 自安装副作用模块（node 缺 document 时安全跳过）+ 事件契约字面量；
//   7) stats.css 语义样式齐备（全部读 tokens 变量，无硬编码主题色背景）；
//   8) bot 侧路由契约对齐：webstats.py 的路径与前端调用一一对应（文本级校验）。
import fs from 'fs';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';
import { JSDOM } from 'jsdom';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const V2 = path.join(ROOT, 'static', 'v2');
const failed = [];
function check(name, cond, extra = '') {
  console.log(`${cond ? '✅' : '❌'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed.push(name);
}

const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

/* ---------------------------------------------------------------- jsdom 环境 */
/* welcome.js 的 import 链（composer/ui/state…）在 import 期不摸 DOM，但为了与
   v53 主题回归同一姿势，统一先铺 jsdom 全局再动态 import。 */
const dom = new JSDOM('<!doctype html><html><body></body></html>',
  { url: 'http://127.0.0.1:8848/', pretendToBeVisual: true });
global.window = dom.window;
global.document = dom.window.document;
global.Node = dom.window.Node;
global.Element = dom.window.Element;
global.HTMLElement = dom.window.HTMLElement;
global.localStorage = dom.window.localStorage;
global.getComputedStyle = dom.window.getComputedStyle;
global.location = dom.window.location;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true, writable: true });

/* ---------------------------------------------------------------- 1/2/3. welcome.js */

const welcomeUrl = pathToFileURL(path.join(V2, 'js/views/welcome.js')).href;
const welcome = await import(welcomeUrl);

check('welcome.js 导出 renderWelcome（app.js 静态入口不受影响）',
  typeof welcome.renderWelcome === 'function');

check('诗句文案库 20 句全中文自研',
  Array.isArray(welcome.POEMS) && welcome.POEMS.length === 20
  && welcome.POEMS.every((p) => /^[\u4e00-\u9fff，。、；：！？「」『』（）·—…\s]+$/.test(p)
    && !/[A-Za-z0-9]/.test(p)),
  `${(welcome.POEMS || []).length} 句`);
check('诗句无重复', new Set(welcome.POEMS).size === 20);

check('randomPoem 只从文案库取词',
  welcome.POEMS.includes(welcome.randomPoem()));

const greetCase = [[2, '夜深了'], [6, '早上好'], [10, '上午好'], [12, '中午好'], [15, '下午好'], [20, '晚上好'], [23, '夜深了']];
check('时段问候覆盖全天（含深夜/清晨边界）',
  greetCase.every(([h, label]) => welcome.greetingFor(h) === label),
  greetCase.map(([h, l]) => `${h}点→${welcome.greetingFor(h)}`).join(' '));
check('问候语全中文', welcome.GREETINGS.every((g) => /^[\u4e00-\u9fff]+$/.test(g.label)));

const tagCases = [
  ['#灵感 顺手记一条 #人物-林晚', ['灵感', '人物-林晚']],
  ['没有标签的普通句子', []],
  ['#重复 #重复', ['重复']],
  ['句尾#句尾标签。', ['句尾标签']],
  [`#${'x'.repeat(21)}`, []],            // 超长标签整体不收（不静默截断）
  [`#${'x'.repeat(19)} 尾`, ['x'.repeat(19)]]
];
check('灵感速记 #标签 提取与后端同口径',
  tagCases.every(([input, expected]) => JSON.stringify(welcome.extractTags(input)) === JSON.stringify(expected)),
  JSON.stringify(welcome.extractTags('#a #b-2 #c')));
const tenTags = Array.from({ length: 10 }, (_, i) => `#标${i}`).join(' ');
check('#标签 最多收 8 个', welcome.extractTags(tenTags).length === 8);

const welcomeSrc = read('static/v2/js/views/welcome.js');
check('文体快捷入口卡保留（quick-grid + openWizard 体裁入口）',
  welcomeSrc.includes("class: 'quick-grid'") && welcomeSrc.includes('mediumIcon(medium)')
  && welcomeSrc.includes('从一种体裁开始'));
check('新增首页卡片类名齐备（进度/灵感/工具/横卡/问候）',
  ['home-grid', 'home-progress', 'home-progress-mark', 'home-inspire', 'ht-grid',
    'home-recent-grid', 'home-work-card', 'home-greet'].every((c) => welcomeSrc.includes(c)));
check('最近作品横卡点进 bench 路由', welcomeSrc.includes('/bench/'));
check('灵感速记失焦即存 + 300 字上限',
  welcomeSrc.includes("addEventListener('blur'") && welcomeSrc.includes('INSP_MAX_LEN = 300'));
check('码字进度卡读 /api/writestats', welcomeSrc.includes('/api/writestats'));

/* ---------------------------------------------------------------- 4/5. stats.js 纯函数 */

const statsUrl = pathToFileURL(path.join(V2, 'js/views/stats.js')).href;
const stats = await import(statsUrl);

check('stats.js 导出 renderStats', typeof stats.renderStats === 'function');

const pts = stats.linePoints([0, 100, 50], { width: 300, height: 100, pad: 10 });
check('折线点位：数量一致且 x 严格递增',
  pts.length === 3 && pts[0].x < pts[1].x && pts[1].x < pts[2].x,
  JSON.stringify(pts));
check('折线点位：y 有界且值越大 y 越小（画在上方）',
  pts.every((p) => p.y >= 10 && p.y <= 90)
  && Math.abs(pts[1].y - 10) < 1e-9   // 最大值 100 顶到上边
  && Math.abs(pts[0].y - 90) < 1e-9); // 0 落在下边
check('折线点位：空数据返回空数组', stats.linePoints([]).length === 0);
check('折线点位：单点居中', (() => {
  const p = stats.linePoints([7], { width: 200, height: 100, pad: 20 });
  return p.length === 1 && Math.abs(p[0].x - 100) < 1e-9;
})());
check('折线点位：负值钳到 0（字数不为负）', (() => {
  const p = stats.linePoints([-5, 10], { width: 100, height: 100, pad: 10 });
  return p[0].v === 0 && Math.abs(p[0].y - 90) < 1e-9;
})());
check('折线点位：全零数据不除零（有最低量程）', (() => {
  const p = stats.linePoints([0, 0, 0], { width: 100, height: 100, pad: 10 });
  return p.length === 3 && Number.isFinite(p[1].y);
})());

const grid = stats.monthGrid(2026, 11);
const dayCells = grid.filter((c) => c && c.date);
check('日历格：2026-11 补齐整周且含 30 天',
  grid.length % 7 === 0 && dayCells.length === 30
  && dayCells[0].date === '2026-11-01' && dayCells[29].date === '2026-11-30',
  `${grid.length} 格`);
check('日历格：周一开头（首日是周日 → 前导 6 个空格）',
  grid.slice(0, 6).every((c) => c === null) && Boolean(grid[6]) && grid[6].date === '2026-11-01');
const gridFeb = stats.monthGrid(2026, 2);
check('日历格：平年二月 28 天', gridFeb.filter((c) => c && c.date).length === 28);

/* ---------------------------------------------------------------- 6. writestats-client.js */

const clientSrc = read('static/v2/js/core/writestats-client.js');
const clientUrl = pathToFileURL(path.join(V2, 'js/core/writestats-client.js')).href;
const client = await import(clientUrl);   // node 有 document（jsdom）→ 安装成功也不炸
check('writestats-client 自安装副作用模块可安全 import',
  typeof client.writestatsDebug === 'function');
const dbg = client.writestatsDebug();
check('调试 API：会话为空时返回 null 会话且 countChars 可用（只数汉字）',
  dbg.session === null && typeof dbg.countChars === 'function' && dbg.countChars('汉字三') === 3
  && dbg.countChars('abc, 123!') === 0, `countChars('汉字三')=${dbg.countChars('汉字三')}`);
check('事件契约：监听 yimu-editor-session 与 yimu-ai-insert（document 级）',
  clientSrc.includes("addEventListener('yimu-editor-session'")
  && clientSrc.includes("addEventListener('yimu-ai-insert'"));
check('崩溃兜底：pagehide/beforeunload/visibilitychange 全接',
  clientSrc.includes("'pagehide'") && clientSrc.includes("'beforeunload'")
  && clientSrc.includes("'visibilitychange'") && clientSrc.includes('visibilityState'));
check('AI 抬基线 + 手写会话分账接口调用齐备',
  clientSrc.includes('/api/writestats/session')
  && clientSrc.includes('/api/writestats/commit')
  && clientSrc.includes('/api/writestats/baseline'));
check('统计失败不影响主链路：上报全部 try/catch + keepalive',
  clientSrc.includes('keepalive') && (clientSrc.match(/catch/g) || []).length >= 6);
check('演示模式不穿透真实后端', clientSrc.includes('IS_MOCK'));

/* ---- 行为级分账：直接派发契约事件，校验手写只增不减 + AI 归账（不双记） ---- */
(() => {
  window.location.hash = '#/work/p-b1/bench/3';
  const fire = (type, detail) => document.dispatchEvent(new dom.window.CustomEvent(type, { detail }));
  const text = (n) => '字'.repeat(n);
  const dbg = () => client.writestatsDebug();

  fire('yimu-editor-session', { phase: 'begin', key: 'p-b1', text: text(100) });
  check('begin：基线=当前字数，手写 0',
    dbg().session && dbg().session.baseline === 100 && dbg().session.chars === 0);

  fire('yimu-editor-session', { phase: 'update', key: 'p-b1', text: text(150) });
  check('update：手写净增 50', dbg().session.chars === 50);

  fire('yimu-editor-session', { phase: 'update', key: 'p-b1', text: text(120) });
  check('删字不回扣已入账（净增变小累计不变）', dbg().session.chars === 50);

  // AI 插入 60 字且文本先随 update 进了手写（poisoned 路径）：抬基线，重复部分待归账
  fire('yimu-editor-session', { phase: 'update', key: 'p-b1', text: text(210) });
  check('AI 文本随 update 进手写（poisoned 瞬态）', dbg().session.chars === 110);
  fire('yimu-ai-insert', { chars: 60, key: 'p-b1' });
  check('ai-insert：基线抬升 60，手写暂不变（归账留到下一次 update/commit）',
    dbg().session.baseline === 160 && dbg().session.chars === 110 && dbg().session.pendingAi === 60);
  fire('yimu-editor-session', { phase: 'update', key: 'p-b1', text: text(210) });
  check('归账后：手写退回重复的 AI 字数（仍 50，不双记）',
    dbg().session.chars === 50 && dbg().session.pendingAi === 0);

  // 用户继续手写 30 字：从抬升后的基线继续累计
  fire('yimu-editor-session', { phase: 'update', key: 'p-b1', text: text(240) });
  check('AI 后继续手写：累计 80（50 手写 + 30 新写）', dbg().session.chars === 80);

  // AI 插入但 update 被节流跳过（文本未进手写）：只抬基线，手写不误扣
  fire('yimu-ai-insert', { chars: 40, key: 'p-b1' });
  check('AI 插入（未随 update 进手写）：手写不误扣',
    dbg().session.chars === 80 && dbg().session.baseline === 200 && dbg().session.pendingAi === 40);
  fire('yimu-editor-session', { phase: 'commit', key: 'p-b1', text: text(280) });
  check('commit：归账 + 定稿按真实净增（80 不缩水）',
    dbg().session === null || (dbg().session.chars === 80 && dbg().session.pendingAi === 0),
    `session=${JSON.stringify(dbg().session)}`);
})();

/* ---------------------------------------------------------------- 7. stats.css */

const statsCss = read('static/v2/css/stats.css');
check('stats.css 覆盖统计页关键类',
  ['stats-top', 'stats-trend-svg', 'stats-cal-grid', 'stats-ring-face', 'stats-range',
    'stats-streak-pill', 'stats-goal-fill'].every((c) => statsCss.includes(`.${c}`)));
check('stats.css 覆盖首页新卡片类',
  ['.home-grid', '.home-progress', '.home-inspire', '.ht-grid', '.home-work-card', '.home-greet']
    .every((c) => statsCss.includes(c)));
check('目标环用 conic-gradient（不引图表库）', statsCss.includes('conic-gradient'));
/* 颜色必须走语义变量：禁止硬编码主题背景色（折线色由 JS 读 CSS 变量注入） */
const hexColors = statsCss.match(/#[0-9a-fA-F]{3,8}\b/g) || [];
check('stats.css 无硬编码颜色（全走 tokens 语义变量）', hexColors.length === 0,
  hexColors.length ? hexColors.join(' ') : '');
check('单列重排断点 ≤875px（容器 order 重排）',
  statsCss.includes('@media (max-width: 875px)') && statsCss.includes('display: contents')
  && statsCss.includes('order:'));

/* ---------------------------------------------------------------- 8. 路由契约对齐 */

const webstatsPy = read('bot/webstats.py');
const routes = ['/api/writestats', '/api/writestats/session', '/api/writestats/commit',
  '/api/writestats/baseline', '/api/writestats/goals', '/api/inspirations'];
check('bot/webstats.py 路由契约齐备', routes.every((r) => webstatsPy.includes(r)));
check('前端调用与后端路由一一对应（无悬空调用）',
  clientSrc.includes('/api/writestats/session')
  && clientSrc.includes('/api/writestats/commit')
  && clientSrc.includes('/api/writestats/baseline')
  && welcomeSrc.includes('/api/inspirations')
  && read('static/v2/js/views/stats.js').includes('/api/writestats'));
check('app.py 未被本任务改动挂载（提供两行挂载说明）',
  webstatsPy.includes('app.include_router(webstats.router)'));

/* ---------------------------------------------------------------- 收尾 */

console.log('');
if (failed.length) {
  console.log(`❌ ${failed.length} 项未通过：`);
  for (const f of failed) console.log(`   - ${f}`);
  process.exit(1);
}
console.log('✅ v5.4.0 码字统计与首页改造回归全绿');
process.exit(0);
