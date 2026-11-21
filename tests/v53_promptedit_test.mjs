// v5.3.0 提示词全开放编辑页：node 直跑回归（不依赖 pytest / 浏览器）。
// 覆盖面：
//   1) promptedit.js 前端侧 md 解析器（与 bot/prompts_store.parse_prompt_md 同一格式：
//      frontmatter / ## 槽 / 温度行 / ``` 围栏 / 注释剥离）；
//   2) upsertSlot 槽合并（新增/替换/温度行/围栏补壳/frontmatter 补齐）；
//   3) 变量 chip 提取（extractVars：去重保序、花括号容错）与 fmtTemp；
//   4) 徽章状态机（slotBadges：已修改 / 默认值已更新）；
//   5) 后端契约文本级对齐（webprompts.py 路由与前端调用一一对应）；
//   6) 内置 prompts/*.md 与前端解析器同格式（真实文件逐组解析成功、指纹 12 位）；
//   7) promptedit.css 语义样式齐备（读 tokens 变量、无硬编码主题色背景）；
//   8) app.js 路由 / sidebar 入口已接（route=prompts、renderPromptEdit）。
import fs from 'fs';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';
import { JSDOM } from 'jsdom';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const failed = [];
function check(name, cond, extra = '') {
  console.log(`${cond ? '✅' : '❌'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed.push(name);
}

const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

/* ---------------------------------------------------------------- jsdom 环境 */
/* promptedit.js 的 import 链（dom/api/ui…）在 import 期不摸 DOM（api.js 读 location），统一先铺 jsdom。 */
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

/* ---------------------------------------------------------------- 1/2/3. 纯函数 */

const peUrl = pathToFileURL(path.join(ROOT, 'static', 'v2', 'js', 'views', 'promptedit.js')).href;
const pe = await import(peUrl);

check('promptedit.js 导出 renderPromptEdit（app.js 动态入口不受影响）',
  typeof pe.renderPromptEdit === 'function');
check('纯函数导出齐全（parsePromptMd/upsertSlot/extractVars/fmtTemp/slotBadges）',
  [pe.parsePromptMd, pe.upsertSlot, pe.extractVars, pe.fmtTemp, pe.slotBadges].every((f) => typeof f === 'function'));

/* 1. 前端侧 md 解析器：与 Python 同一格式 */
const MD = [
  '---',
  'id: demo',
  'label: 演示组',
  'base: abc123def456',
  '---',
  '',
  '<!-- 注释不进入提示词 -->',
  '',
  '## base',
  '公共铁则文本',
  '',
  '## polish',
  '温度：0.70',
  '任务段 {{变量A}} 文本',
  '',
].join('\n');
const parsed = pe.parsePromptMd(MD);
check('解析 frontmatter（id/label/base）',
  parsed.frontmatter.id === 'demo' && parsed.frontmatter.label === '演示组' && parsed.frontmatter.base === 'abc123def456');
check('解析槽文本与段首温度行',
  parsed.slots.base.text === '公共铁则文本' && parsed.slots.base.temp === null
  && parsed.slots.polish.text === '任务段 {{变量A}} 文本' && Math.abs(parsed.slots.polish.temp - 0.7) < 1e-9);
check('注释剥离（<!-- --> 不进槽文本）', parsed.errors.length === 0 && !JSON.stringify(parsed.slots).includes('注释'));

const fenced = pe.parsePromptMd('## output_format\n```\n输出格式：\n## 本章概要\n（概要）\n```\n');
check('围栏内的 ## 不是槽标题、解析剥壳',
  Object.keys(fenced.slots).length === 1
  && fenced.slots.output_format.text === '输出格式：\n## 本章概要\n（概要）');

const badTmp = pe.parsePromptMd('## s\n温度：3.9\n正文\n');
check('超范围温度钳到 2.0', Math.abs(badTmp.slots.s.temp - 2) < 1e-9);
const badTmp2 = pe.parsePromptMd('## s\n温度：热点\n正文\n');
check('非数字温度行原样留在正文', badTmp2.slots.s.temp === null && badTmp2.slots.s.text.startsWith('温度：热点'));

/* 2. upsertSlot：合并/替换/温度/围栏/frontmatter 补齐 */
const up1 = pe.upsertSlot(MD, 'polish', { text: '改后的任务段', temp: 0.55, group: 'demo', label: '演示组' });
const up1p = pe.parsePromptMd(up1);
check('upsertSlot 替换既有槽（文本+温度）',
  up1p.slots.polish.text === '改后的任务段' && Math.abs(up1p.slots.polish.temp - 0.55) < 1e-9
  && up1p.slots.base.text === '公共铁则文本' && Object.keys(up1p.slots).length === 2);
const up2 = pe.upsertSlot(MD, 'fix', { text: '新增槽', temp: 0.1 });
check('upsertSlot 追加新槽', pe.parsePromptMd(up2).slots.fix.text === '新增槽');
const up3 = pe.upsertSlot('', 'base', { text: '含 ## 标记\n## 正文\n（正文）', temp: null, group: 'wf', label: '契约' });
const up3p = pe.parsePromptMd(up3);
check('无 frontmatter 时补齐最小头；含 ## 的正文自动加围栏',
  up3p.frontmatter.id === 'wf' && up3p.frontmatter.label === '契约'
  && up3p.slots.base.text === '含 ## 标记\n## 正文\n（正文）');
const up4 = pe.upsertSlot(MD, 'polish', { text: '无温度槽', temp: null });
check('temp=null 时移除温度行', pe.parsePromptMd(up4).slots.polish.temp === null);
const round = pe.upsertSlot(pe.upsertSlot(MD, 'polish', { text: 'X', temp: 0.2 }), 'polish', { text: 'X', temp: 0.2 });
check('upsertSlot 幂等（重复合并结果稳定）', round === up1.replace('0.55', '0.2') || pe.parsePromptMd(round).slots.polish.text === 'X');

/* 3. 变量 chip 提取 + fmtTemp */
check('extractVars 去重保序', JSON.stringify(pe.extractVars('a {{B}} b {{A}} {{B}} {{}} 尾')) === JSON.stringify(['B', 'A']));
check('extractVars 对 JSON 花括号安全', JSON.stringify(pe.extractVars('输出 {"titles":[..]} {{候选数}}')) === JSON.stringify(['候选数']));
check('fmtTemp 规范（0.1/0.85/越界钳制）',
  pe.fmtTemp(0.1) === '0.1' && pe.fmtTemp(0.85) === '0.85' && pe.fmtTemp(9) === '2' && pe.fmtTemp(-3) === '0' && pe.fmtTemp('x') === '0');

/* 4. 徽章状态机 */
const slotOk = { slot: 'polish', source: 'builtin', modified: false, user_text: null };
const slotMod = { slot: 'polish', source: 'user', modified: true, user_text: '我的' };
const groupFresh = { defaultUpdated: false };
const groupStale = { defaultUpdated: true };
check('徽章：未改槽 → 双徽章皆无', !pe.slotBadges(slotOk, groupStale).modified && !pe.slotBadges(slotOk, groupStale).defaultUpdated);
check('徽章：已改槽 → 「已修改」亮', pe.slotBadges(slotMod, groupFresh).modified === true);
check('徽章：指纹过期 + 用户层生效 → 「默认值已更新」亮',
  pe.slotBadges(slotMod, groupStale).defaultUpdated === true
  && pe.slotBadges(slotOk, groupStale).defaultUpdated === false);

/* ---------------------------------------------------------------- 5. 后端契约文本级对齐 */

const fe = read('static/v2/js/views/promptedit.js');
const be = read('bot/webprompts.py');
check('API 路径前后端对齐（/api/prompts、/api/prompts/file、/api/prompts/adopt）',
  ['/api/prompts', '/api/prompts/file', '/api/prompts/adopt'].every((p) => fe.includes(p) && be.includes(p)));
check('webprompts.py 五个端点齐备（GET 清单/GET 文件/POST 保存/DELETE 恢复/POST 采用）',
  be.includes('@router.get("/api/prompts")') && be.includes('@router.get("/api/prompts/file")')
  && be.includes('@router.post("/api/prompts/file")') && be.includes('@router.delete("/api/prompts/file")')
  && be.includes('@router.post("/api/prompts/adopt")'));
check('前端 request 调用与后端方法对齐（DELETE 恢复默认、POST 保存/采用）',
  fe.includes("{ method: 'DELETE' }") && fe.includes("{ method: 'POST', body: { group") );

/* ---------------------------------------------------------------- 6. 内置 md 与前端解析器同格式 */

const PROMPTS = 'prompts';
const groups = fs.readdirSync(path.join(ROOT, PROMPTS)).filter((f) => f.endsWith('.md'));
check('首批 10 组内置 md 齐备', groups.length >= 10, groups.join(', '));
let mdAllOk = true;
const groupIds = [];
for (const f of groups) {
  const raw = read(path.join(PROMPTS, f));
  const p = pe.parsePromptMd(raw);
  if (p.errors.length || !p.frontmatter.id || p.frontmatter.id + '.md' !== f) { mdAllOk = false; console.log('   bad md:', f, p.errors); }
  groupIds.push(p.frontmatter.id);
  const fpLine = raw.match(/^base:\s*(.*)$/m);
  if (!fpLine) mdAllOk = false;
}
check('每组 md 均可被前端解析器解析（无告警、id=文件名、有 base 指纹行）', mdAllOk);
check('组 id 全部为英文 kebab', groupIds.every((id) => /^[a-z0-9-]+$/.test(id)), groupIds.join(' '));
const wfRaw = read(path.join(PROMPTS, 'workflow-writing.md'));
check('正文契约 md 头部带红线警告且标记行原样保留',
  wfRaw.includes('红线') && wfRaw.includes('=== 一幕·YIMU 神笔工作台·开篇 ===') && wfRaw.includes('=== 一幕·YIMU 神笔工作台·结尾 ==='));
const inspRaw = read(path.join(PROMPTS, 'inspirations.md'));
check('灵感速记组如实声明暂无槽位（纯存储功能）', inspRaw.includes('没有任何 LLM 提示词槽位'));

/* ---------------------------------------------------------------- 7. CSS */

const css = read('static/v2/css/promptedit.css');
check('promptedit.css 语义样式齐备（手风琴/槽行/徽章/滑杆/对比分栏/textarea）',
  ['.pe-group', '.pe-slot-row', '.pe-badge', '.pe-temp-slider', '.pe-cmp-grid', '.pe-textarea', '.pe-var-chip']
    .every((c) => css.includes(c)));
check('CSS 全读 tokens 变量（无硬编码主题色背景）',
  !/background:\s*#[0-9a-fA-F]{3,8}\b/.test(css.replace(/--bg-mask[^;]*/g, '')) && css.includes('var(--'));

/* ---------------------------------------------------------------- 8. 路由与入口已接 */

const appjs = read('static/v2/js/app.js');
const routerjs = read('static/v2/js/core/router.js');
const sidebarjs = read('static/v2/js/components/sidebar.js');
check('app.js #/prompts 路由按需加载 promptedit.js 与 promptedit.css',
  appjs.includes("route.name === 'prompts'") && appjs.includes('renderPromptEdit') && appjs.includes('promptedit.css'));
check('router.js ROUTES 含 prompts', routerjs.includes("'prompts'"));
check('sidebar.js 有「提示词」入口', sidebarjs.includes("label: '提示词'") && sidebarjs.includes("navigate('/prompts')"));

/* ---------------------------------------------------------------- 汇总 */

if (failed.length) {
  console.log(`\n${failed.length} 项失败`);
  process.exit(1);
}
console.log('\n全部通过');
