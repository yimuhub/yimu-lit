// v5.3.0 主题与个性化：node 直跑回归（不依赖 pytest / 浏览器）。
// 覆盖面：
//   1) tokens.css 四主题块齐全、每主题关键语义变量齐全、旧变量未改名未反转；
//   2) theme.js 注册表 / switchTheme / 皮肤机制 / 旧值兼容 / 顶栏循环切换；
//   3) typingsound / typingfx 语法 + import 冒烟 + 契约（DOM/AudioContext 缺失时静默降级）；
//   4) index.html 防闪白脚本位置（先于样式表）与皮肤层容器；
//   5) static/v2/img/skins/ 内置 SVG 皮肤与注册表一一对应（纯文本、无二进制）；
//   6) settings.js 外观区扩展到位且既有分组未被破坏；
//   7) index.html 的 ?v= 与 app.js 的 CSS_V 仍一致（test_v4232_assets.py 的前端镜像）。
import fs from 'fs';
import path from 'path';
import { execFileSync } from 'child_process';
import { fileURLToPath, pathToFileURL } from 'url';
import { JSDOM } from 'jsdom';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const V2 = path.join(ROOT, 'static', 'v2');
const failed = [];
function check(name, cond, extra = '') {
  console.log(`${cond ? '✅' : '❌'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed.push(name);
}

const read = (p) => fs.readFileSync(path.join(V2, p), 'utf8');

/* ------------------------------------------------------------------ 1. tokens.css */

const tokens = read('css/tokens.css');
const THEME_IDS = ['paper', 'scroll', 'forest', 'ink'];

function themeBlock(id) {
  // 匹配 :root[data-theme="x"] 或 html[data-theme="x"] 的整块声明
  const re = new RegExp(`(?::root|html)\\[data-theme="${id}"\\]\\s*\\{([\\s\\S]*?)\\n\\}`);
  const m = tokens.match(re);
  return m ? m[1] : null;
}

/* 每主题必须齐全的关键语义变量（五档文字 + 面层 + 状态四件套 + 按钮/输入框/面板/导航/皮肤） */
const REQUIRED_VARS = [
  '--bg-base', '--bg-sidebar', '--bg-elevated', '--bg-chip',
  '--ink-primary', '--ink-secondary', '--ink-tertiary', '--ink-caption', '--ink-dimmed', '--ink-inverse',
  '--line-2',
  '--surface-1', '--surface-2',
  '--accent', '--accent-soft', '--accent-ink',
  '--success', '--success-soft', '--success-ink', '--success-line',
  '--warn', '--warn-soft', '--warn-label', '--warn-line',
  '--danger', '--danger-soft', '--danger-ink', '--danger-line',
  '--info', '--info-soft', '--info-ink', '--info-line',
  '--btn-bg', '--btn-bg-hover', '--btn-border', '--btn-ink', '--btn-primary-bg', '--btn-primary-ink',
  '--input-bg', '--input-border', '--input-ink', '--input-placeholder', '--input-ring',
  '--panel-bg', '--panel-border',
  '--nav-bg', '--nav-ink', '--nav-ink-muted', '--nav-ink-active', '--nav-item-hover-bg',
  '--scrollbar-thumb',
  '--skin-base', '--skin-opacity', '--skin-filter'
];

for (const id of THEME_IDS) {
  const body = themeBlock(id);
  check(`tokens.css 存在 data-theme="${id}" 主题块`, !!body);
  if (!body) continue;
  const missing = REQUIRED_VARS.filter((v) => !body.includes(`${v}:`));
  check(`主题 ${id} 关键语义变量齐全（${REQUIRED_VARS.length} 项）`, missing.length === 0,
    missing.length ? `缺 ${missing.join(' ')}` : '');
}

/* 既有变量与语义不得改名/反转：旧 :root（浅色）与 dark 块保持原值 */
const rootBlock = tokens.match(/:root\s*\{([\s\S]*?)\n\}/)?.[1] || '';
check('旧 :root 仍以 #ffffff 为底（浅色语义未反转）', /--bg-base:\s*#ffffff/.test(rootBlock));
check('旧 :root 仍保留五档 --ink-* 与强调色原值',
  /--ink-primary:\s*#0f1115/.test(rootBlock) && /--accent:\s*#4176e6/.test(rootBlock));
const darkBlock = tokens.match(/:root\[data-theme="dark"\]\s*\{([\s\S]*?)\n\}/)?.[1] || '';
check('旧 data-theme="dark" 块仍在（向后兼容）', /--bg-base:\s*#151517/.test(darkBlock));

/* 皮肤滤镜/透明度随主题：古韵 sepia(0.6)+opacity .15；暗夜 grayscale+brightness(0.6)+opacity .2 */
const scrollBody = themeBlock('scroll') || '';
const inkBody = themeBlock('ink') || '';
check('古韵信笺皮肤滤镜 sepia(0.6) / opacity 0.15',
  /--skin-filter:\s*sepia\(0\.6\)/.test(scrollBody) && /--skin-opacity:\s*0\.15/.test(scrollBody));
check('暗夜水墨皮肤滤镜 grayscale+brightness(0.6) / opacity 0.2',
  /grayscale\(1\)\s*brightness\(0\.6\)/.test(inkBody) && /--skin-opacity:\s*0\.2;/.test(inkBody));

/* ------------------------------------------------------------------ 2. theme.js */

/* jsdom 环境：theme.js 及其挂件（typingsound/typingfx）在 import 时就会摸 DOM。 */
const dom = new JSDOM('<!doctype html><html><head><meta name="theme-color" content="#ffffff"></head>'
  + '<body><div id="bg-skin" class="bg-skin" aria-hidden="true"></div></body></html>',
  { url: 'http://127.0.0.1:8848/', pretendToBeVisual: true });
global.window = dom.window;
global.document = dom.window.document;
global.Node = dom.window.Node;
global.Element = dom.window.Element;
global.HTMLElement = dom.window.HTMLElement;
global.localStorage = dom.window.localStorage;
global.getComputedStyle = dom.window.getComputedStyle;
global.Image = dom.window.Image;
/* Node 21+ 的全局 navigator 是只读 getter，用 defineProperty 覆盖 */
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true, writable: true });

const theme = await import(pathToFileURL(path.join(V2, 'js/core/theme.js')).href);

check('theme.js 导出四主题注册表 THEMES',
  Array.isArray(theme.THEMES) && theme.THEMES.length === 4
  && THEME_IDS.every((id) => theme.THEMES.some((t) => t.id === id)));
check('注册表每项带 名称/描述/是否暗色',
  theme.THEMES.every((t) => typeof t.name === 'string' && t.name.length >= 2
    && typeof t.desc === 'string' && t.desc.length >= 4 && typeof t.dark === 'boolean'),
  theme.THEMES.map((t) => `${t.name}${t.dark ? '(暗)' : '(亮)'}`).join(' / '));
check('暗夜水墨标暗、其余标亮',
  theme.THEMES.find((t) => t.id === 'ink').dark === true
  && theme.THEMES.filter((t) => t.id !== 'ink').every((t) => t.dark === false));

check('theme.js 导出 switchTheme / setTheme / toggleTheme / themeLabel / initTheme',
  ['switchTheme', 'setTheme', 'toggleTheme', 'themeLabel', 'initTheme']
    .every((k) => typeof theme[k] === 'function'));
check('theme.js 导出皮肤机制 setSkin/getSkin/SKINS',
  typeof theme.setSkin === 'function' && typeof theme.getSkin === 'function' && Array.isArray(theme.SKINS));

/* 主题切换真的写到根节点与 localStorage */
theme.setTheme('scroll');
check('switchTheme/setTheme 后 html data-theme=scroll',
  document.documentElement.dataset.theme === 'scroll');
check('偏好落盘 shenbi.v2.theme=scroll', localStorage.getItem('shenbi.v2.theme') === 'scroll');
check('themeLabel 返回自研主题名', theme.themeLabel('scroll') === '古韵信笺'
  && theme.themeLabel('paper') === '宣纸白' && theme.themeLabel('forest') === '森系护眼'
  && theme.themeLabel('ink') === '暗夜水墨' && theme.themeLabel('system') === '跟随系统');
check('明暗语义保留：scroll 为亮 / ink 为暗（topbar 日月图标依赖 resolvedTheme）',
  document.documentElement.style.colorScheme === 'light');
theme.setTheme('ink');
check('ink 主题 colorScheme 翻为 dark', document.documentElement.style.colorScheme === 'dark');

/* 旧偏好值兼容：light/dark 自动映射 paper/ink */
localStorage.setItem('shenbi.v2.theme', 'light');
theme.setTheme('light');
check('旧值 light 自动映射为宣纸白', document.documentElement.dataset.theme === 'paper');
theme.setTheme('dark');
check('旧值 dark 自动映射为暗夜水墨', document.documentElement.dataset.theme === 'ink');

/* 顶栏循环切换仍可用（真实流程：boot 时 initTheme 把落盘偏好灌进 store） */
localStorage.setItem('shenbi.v2.theme', 'paper');
theme.initTheme();
check('initTheme 后 store 同步偏好与解析结果',
  theme.themeLabel(document.documentElement.dataset.theme) === '宣纸白');
const afterToggle = theme.toggleTheme();
check('toggleTheme 循环切到下一主题（古韵信笺）', afterToggle === 'scroll', `got ${afterToggle}`);
theme.toggleTheme(); theme.toggleTheme(); // scroll→forest→ink
check('toggleTheme 三连到暗夜水墨', localStorage.getItem('shenbi.v2.theme') === 'ink');

/* 皮肤：内置 key 存 ew-skin，data-skin 与 --skin-image/--skin-base 写到根节点 */
theme.setSkin('builtin', 'rice-paper', '#f6f1e4');
const skinRecord = JSON.parse(localStorage.getItem('ew-skin') || 'null');
check('皮肤记号存入 ew-skin（builtin）', skinRecord && skinRecord.kind === 'builtin'
  && skinRecord.value === 'rice-paper');
check('皮肤开启后 html data-skin=on', document.documentElement.dataset.skin === 'on');
check('--skin-image 指向内置 SVG',
  document.documentElement.style.getPropertyValue('--skin-image').includes('rice-paper.svg'));
check('--skin-base 写入底色（防闪白用）',
  document.documentElement.style.getPropertyValue('--skin-base') === '#f6f1e4');
theme.setSkin('none');
check('清除皮肤后 data-skin 摘除', !document.documentElement.hasAttribute('data-skin'));

/* 内置皮肤与磁盘 SVG 一一对应 */
for (const s of theme.SKINS) {
  const p = path.join(V2, 'img/skins', `${s.id}.svg`);
  check(`内置皮肤 ${s.name}(${s.id}) 有对应 SVG 文件`, fs.existsSync(p));
}
check('内置皮肤 6-8 张', theme.SKINS.length >= 6 && theme.SKINS.length <= 8,
  `${theme.SKINS.length} 张`);

/* ------------------------------------------------------------------ 3. 打字音效 / 光标特效 */

/* 语法自检：node --check（ESM 语法走 --input-type 由 import 冒烟兜底） */
for (const f of ['js/core/typingsound.js', 'js/core/typingfx.js', 'js/core/theme.js', 'js/views/settings.js']) {
  let ok = true;
  try {
    execFileSync(process.execPath, ['--check', path.join(V2, f)], { stdio: 'pipe' });
  } catch {
    ok = false;
  }
  check(`node --check 通过：${f}`, ok);
}

const sound = await import(pathToFileURL(path.join(V2, 'js/core/typingsound.js')).href);
check('typingsound 契约：setEnabled / playKey / mute',
  ['setEnabled', 'playKey', 'mute', 'setKind', 'setVolume', 'getState', 'isEnabled', 'isMuted', 'unlock']
    .every((k) => typeof sound[k] === 'function'));
check('打字音三款（青轴/茶轴/打字机）',
  sound.SOUND_KINDS.length === 3
  && ['blue', 'brown', 'typewriter'].every((id) => sound.SOUND_KINDS.some((k) => k.id === id)));
/* 无 AudioContext 的环境里开/关 + 播放必须静默降级不抛错 */
sound.setEnabled(true);
sound.setVolume(0.5);
sound.playKey('blue');
sound.playKey('typewriter');
sound.mute(true);
sound.playKey();
sound.mute(false);
sound.setEnabled(false);
check('typingsound 在无 AudioContext 环境下静默降级（不抛错）', true);
check('打字音开关/音量持久化到 ew-typing-sound', (() => {
  sound.setEnabled(true);
  sound.setVolume(0.8);
  const raw = JSON.parse(localStorage.getItem('ew-typing-sound') || 'null');
  return raw && raw.enabled === true && Math.abs(raw.volume - 0.8) < 1e-6;
})());

const fx = await import(pathToFileURL(path.join(V2, 'js/core/typingfx.js')).href);
check('typingfx 契约：trigger(x,y,type) / setEnabled / setKind',
  ['trigger', 'setEnabled', 'setKind', 'getState', 'isEnabled', 'installTypingFx']
    .every((k) => typeof fx[k] === 'function'));
check('光标特效四款（挥毫溅墨/墨纹涟漪/文字鼓励/烈焰）',
  fx.FX_KINDS.length === 4
  && ['splash', 'ripple', 'cheer', 'flame'].every((id) => fx.FX_KINDS.some((k) => k.id === id)));

/* 关闭状态下 trigger 必须无操作 */
fx.setEnabled(false);
fx.trigger(30, 40, 'splash');
check('特效关闭时 trigger 不产生粒子', !document.querySelector('.typing-fx-layer'));
/* 开启后 trigger 生成粒子层；无 element.animate（jsdom）走兜底清理也不抛错 */
fx.setEnabled(true);
fx.trigger(60, 80, 'splash');
fx.trigger(60, 80, 'cheer');
fx.trigger(60, 80, 'ripple');
fx.trigger(60, 80, 'flame');
check('特效开启时 trigger 挂出 fixed 粒子容器', !!document.querySelector('.typing-fx-layer'));
check('特效开关持久化到 ew-typing-fx', (() => {
  const raw = JSON.parse(localStorage.getItem('ew-typing-fx') || 'null');
  return raw && raw.enabled === true;
})());

/* 自研鼓励文案库 12-16 条 */
const fxSrc = fs.readFileSync(path.join(V2, 'js/core/typingfx.js'), 'utf8');
const cheerArr = fxSrc.match(/const CHEERS\s*=\s*\[([\s\S]*?)\]/)?.[1] || '';
const cheerCount = (cheerArr.match(/'[^']+'/g) || []).length;
check('文字鼓励文案库 12-16 条且全部中文自研', cheerCount >= 12 && cheerCount <= 16
  && (cheerArr.match(/'[^']*[\u4e00-\u9fff][^']*'/g) || []).length === cheerCount,
  `${cheerCount} 条`);

/* ------------------------------------------------------------------ 4. index.html 防闪白 */

const indexHtml = read('index.html');
const bootIdx = indexHtml.indexOf('shenbi.v2.theme');
const firstCssIdx = indexHtml.indexOf('<link rel="stylesheet"');
check('index.html head 有防闪白内联脚本（读主题记号）', bootIdx > -1);
check('防闪白脚本在样式表之前执行（禁缓存冷启动先于样式应用底色）',
  bootIdx > -1 && firstCssIdx > -1 && bootIdx < firstCssIdx,
  `script@${bootIdx} < link@${firstCssIdx}`);
check('防闪白脚本处理皮肤底色（ew-skin / --skin-base）',
  indexHtml.includes('ew-skin') && indexHtml.includes('--skin-base'));
check('body 含背景皮肤层容器 .bg-skin 且在应用容器之前',
  indexHtml.indexOf('class="bg-skin"') > -1
  && indexHtml.indexOf('class="bg-skin"') < indexHtml.indexOf('id="app"'));
check('防闪白脚本兼容旧值并解析系统深浅（light/dark/matchMedia）',
  indexHtml.includes("'light'") && indexHtml.includes("'dark'")
  && indexHtml.includes('prefers-color-scheme: dark'));

/* 皮肤层样式与半透明钩子在常驻 CSS 里 */
const shellCss = read('css/shell.css');
check('shell.css 定义 .bg-skin 皮肤层（fixed 全屏 + 主题滤镜/透明度变量）',
  /\.bg-skin\s*\{/.test(shellCss) && shellCss.includes('var(--skin-filter')
  && shellCss.includes('var(--skin-opacity'));
check('皮肤开启时主面层半透明（data-skin="on" 钩子）',
  shellCss.includes('html[data-skin="on"]') && shellCss.includes('color-mix'));

/* ------------------------------------------------------------------ 5. 皮肤 SVG 卫生检查 */

for (const s of theme.SKINS) {
  const p = path.join(V2, 'img/skins', `${s.id}.svg`);
  const raw = fs.readFileSync(p, 'latin1');
  const stat = fs.statSync(p);
  const isText = !raw.includes('\u0000') && /<svg[\s>]/.test(raw);
  check(`皮肤 ${s.id}.svg 是纯文本矢量（无二进制/无脚本）且 <20KB`,
    isText && stat.size < 20 * 1024 && !raw.includes('<script'),
    `${stat.size}B`);
}

/* ------------------------------------------------------------------ 6. settings.js 外观区扩展 */

const settingsSrc = fs.readFileSync(path.join(V2, 'js/views/settings.js'), 'utf8');
check('settings.js 接入主题注册表与 switchTheme',
  settingsSrc.includes("from '../core/theme.js'") && settingsSrc.includes('switchTheme')
  && settingsSrc.includes('THEMES'));
check('settings.js 渲染主题四选卡（theme-card + swatch 预览）',
  settingsSrc.includes('theme-card') && settingsSrc.includes('swatch'));
check('settings.js 接入皮肤选择（setSkin/getSkin/SKINS/skinBaseFromImage）',
  settingsSrc.includes('setSkin') && settingsSrc.includes('getSkin')
  && settingsSrc.includes('skinBaseFromImage'));
check('settings.js 自定义上传限 10MB', settingsSrc.includes('10 * 1024 * 1024'));
check('settings.js 接入打字音效（三选 + 开关 + 音量）',
  settingsSrc.includes('SOUND_KINDS') && settingsSrc.includes('setSoundEnabled')
  && settingsSrc.includes('setSoundVolume'));
check('settings.js 接入光标特效（四选 + 开关）',
  settingsSrc.includes('FX_KINDS') && settingsSrc.includes('setFxEnabled') && settingsSrc.includes('triggerFx'));
/* 既有分组未被破坏：抽查各组关键锚点仍在 */
const anchors = ['质量门禁', '评分专用模型', '全局默认模型', '创作岗位', '全局并发上限',
  '会话留档', '技能进化', '全局默认笔名', '在侧栏显示经典版入口', '系统操作日志'];
const lostAnchors = anchors.filter((a) => !settingsSrc.includes(a));
check('settings.js 既有分组锚点全部保留', lostAnchors.length === 0,
  lostAnchors.length ? `丢失：${lostAnchors.join('、')}` : '');

/* ------------------------------------------------------------------ 7. 版本戳一致性（前端镜像） */

const vInHtml = [...indexHtml.matchAll(/(?:href|src)="(\/v2\/[^"?]+)\?v=(\d+)"/g)].map((m) => m[2]);
const cssV = (fs.readFileSync(path.join(V2, 'js/app.js'), 'utf8').match(/CSS_V\s*=\s*['"](\d+)['"]/) || [])[1];
check('index.html 所有 ?v= 仍一致', new Set(vInHtml).size === 1 && vInHtml.length > 0, `?v=${vInHtml[0]}`);
check('index.html ?v= 与 app.js CSS_V 一致（app.js 未被本任务改动）', cssV === vInHtml[0],
  `CSS_V=${cssV}`);

/* ------------------------------------------------------------------ 收尾 */

console.log('');
if (failed.length) {
  console.log(`❌ ${failed.length} 项未通过：`);
  for (const f of failed) console.log(`   - ${f}`);
  process.exit(1);
}
console.log('✅ v5.3.0 主题与个性化回归全绿');
process.exit(0);
