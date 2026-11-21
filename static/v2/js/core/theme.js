/* 主题与个性化（v5.3.0）：
 * 1) 四主题注册表（宣纸白/古韵信笺/森系护眼/暗夜水墨）+ system 跟随系统，
 *    CSS 变量 + data-theme 根属性切换，记忆用户选择；
 * 2) 背景皮肤（与主题解耦）：内置 SVG 皮肤或用户自定义 dataURL，存 localStorage，
 *    滤镜/透明度随主题走（tokens.css 各主题块的 --skin-filter/--skin-opacity）；
 * 3) 打字音效与光标特效模块在 boot 时由本文件带入挂载（见文件尾部 import）。
 * 兼容约定：setTheme/toggleTheme/themeLabel/initTheme 导出与旧版一致，
 * 旧偏好值 light/dark 自动映射为 paper/ink，不改键名不丢用户选择。 */

import { store } from './state.js';
/* v5.3.0：个性化挂件随主题模块一起进 boot（两个模块内部自带 try/catch，
   安装失败只 console.warn，绝不影响主流程）。 */
import './typingsound.js';
import './typingfx.js';

const KEY = 'shenbi.v2.theme';
const SKIN_KEY = 'ew-skin';

/* 四主题注册表：名称 / 描述 / 是否暗色（topbar 的日/月图标读 resolvedTheme 明暗）。
   swatch = [底色, 强调色, 文字色]，供设置页四选卡的缩略色板预览。 */
export const THEMES = [
  { id: 'paper', name: '宣纸白', desc: '干净明亮的白纸底色，白天写作最稳', dark: false, meta: '#ffffff', swatch: ['#ffffff', '#4176e6', '#0f1115'] },
  { id: 'scroll', name: '古韵信笺', desc: '米黄信笺配赭红点缀，落笔有旧时光的味道', dark: false, meta: '#f8f1e0', swatch: ['#f8f1e0', '#b4552d', '#3d3226'] },
  { id: 'forest', name: '森系护眼', desc: '豆绿底色温润不刺眼，长夜码字的护眼方案', dark: false, meta: '#d9e4cb', swatch: ['#d9e4cb', '#3e7d54', '#26301f'] },
  { id: 'ink', name: '暗夜水墨', desc: '墨色夜幕压住屏幕，深夜写作眼睛不遭罪', dark: true, meta: '#151517', swatch: ['#151517', '#7aaaff', '#f3f5f7'] }
];
const THEME_MAP = new Map(THEMES.map((t) => [t.id, t]));

/* 旧版偏好值兼容：light/dark 已升级为 paper/ink，读档时自动映射。 */
const LEGACY_ALIAS = { light: 'paper', dark: 'ink' };
const VALID_PREFS = ['system', 'paper', 'scroll', 'forest', 'ink'];

const media = typeof window !== 'undefined' && window.matchMedia
  ? window.matchMedia('(prefers-color-scheme: dark)')
  : null;

function normalize(pref) {
  if (LEGACY_ALIAS[pref]) return LEGACY_ALIAS[pref];
  return VALID_PREFS.includes(pref) ? pref : 'system';
}

function readPref() {
  try {
    return normalize(localStorage.getItem(KEY));
  } catch {
    return 'system';
  }
}

/* 偏好 → 实际主题 id：system 按系统明暗解析成 paper/ink，其余原样。 */
function resolve(pref) {
  if (pref === 'system') return media && media.matches ? 'ink' : 'paper';
  return THEME_MAP.has(pref) ? pref : 'paper';
}

function themeMeta(id) {
  return THEME_MAP.get(id) || THEMES[0];
}

function paint(pref) {
  const resolved = resolve(pref);
  const meta = themeMeta(resolved);
  const root = document.documentElement;
  root.dataset.theme = resolved;
  root.dataset.themePreference = pref;
  /* colorScheme / resolvedTheme 保持旧的「明暗」语义（topbar 日/月图标依赖它）。 */
  root.style.colorScheme = meta.dark ? 'dark' : 'light';
  const tag = document.querySelector('meta[name="theme-color"]');
  if (tag) tag.setAttribute('content', meta.meta);
  store.set((state) => ({ ...state, theme: pref, resolvedTheme: meta.dark ? 'dark' : 'light', themeId: resolved }));
}

/* 应用主题偏好；接受四主题 id、system 与旧值 light/dark。 */
export function setTheme(pref) {
  const next = normalize(pref);
  try {
    localStorage.setItem(KEY, next);
  } catch {
    /* storage may be unavailable */
  }
  paint(next);
}

/* v5.3.0：按主题 id 显式切换（设置页四选卡用；等价 setTheme，仅语义上强调「直接指定主题」）。 */
export function switchTheme(themeId) {
  setTheme(themeId);
  return resolve(normalize(themeId));
}

/* 顶栏单按钮循环切换：宣纸白 → 古韵信笺 → 森系护眼 → 暗夜水墨 → 跟随系统。 */
export function toggleTheme() {
  const order = ['paper', 'scroll', 'forest', 'ink', 'system'];
  const current = normalize(store.get().theme);
  const idx = order.indexOf(current);
  const next = order[(idx + 1) % order.length] || 'paper';
  setTheme(next);
  return store.get().theme;
}

export function themeLabel(pref) {
  const p = normalize(pref);
  if (p === 'system') return '跟随系统';
  return (THEME_MAP.get(p) || {}).name || '跟随系统';
}

/* v5.3.0：主题是否暗色（供特效取色、明暗分支判断；旧值也兼容）。 */
export function isThemeDark(id) {
  const t = THEME_MAP.get(normalize(id));
  return t ? t.dark : false;
}

export function initTheme() {
  paint(readPref());
  applySkin(readSkin());
  if (media) {
    const onChange = () => {
      if (store.get().theme === 'system') paint('system');
    };
    if (media.addEventListener) media.addEventListener('change', onChange);
    else if (media.addListener) media.addListener(onChange);
  }
}

/* --------------------------------------------------------------- 背景皮肤 */
/* 皮肤与主题解耦：皮肤记号存 ew-skin（内置 key 或用户 dataURL）。
   启动防闪白脚本会先读 ew-skin 画出底色（--skin-base），图片等 JS 起来再补。 */

export const SKINS = [
  { id: 'shanshui-dawn', name: '远山晨雾', base: '#e8eef2', desc: '青灰远山淡淡雾' },
  { id: 'shanshui-night', name: '夜航山影', base: '#181c22', desc: '墨色山影压夜幕' },
  { id: 'rice-paper', name: '宣纸纹', base: '#f6f1e4', desc: '细纤维宣纸质感' },
  { id: 'letter-old', name: '旧信笺', base: '#f5ebd3', desc: '米黄信笺横纹' },
  { id: 'grid-mist', name: '雾青稿格', base: '#e9edef', desc: '青灰方格像稿纸' },
  { id: 'bamboo-shade', name: '竹影', base: '#e5ecd9', desc: '竹叶浅影护眼底' },
  { id: 'plum-fog', name: '梅雾', base: '#f2e8ea', desc: '淡梅粉雾面' },
  { id: 'inkwave', name: '墨浪', base: '#1e222a', desc: '深蓝墨浪暗纹' }
];

function readSkin() {
  try {
    const r = JSON.parse(localStorage.getItem(SKIN_KEY) || 'null');
    if (r && (r.kind === 'builtin' || r.kind === 'custom') && r.value) return r;
  } catch {
    /* 记号损坏当作没皮肤 */
  }
  return { kind: 'none' };
}

function skinImageValue(record) {
  if (record.kind === 'custom') return `url("${record.value}")`;
  return `url("/v2/img/skins/${encodeURIComponent(record.value)}.svg")`;
}

function applySkin(record) {
  const root = document.documentElement;
  if (!record || record.kind === 'none' || !record.value) {
    root.removeAttribute('data-skin');
    root.style.removeProperty('--skin-image');
    root.style.removeProperty('--skin-base');
    return;
  }
  root.dataset.skin = 'on';
  root.style.setProperty('--skin-image', skinImageValue(record));
  if (record.base) root.style.setProperty('--skin-base', record.base);
}

/* 设置皮肤：kind = 'builtin'（value 为 SKINS 里的 id）| 'custom'（value 为 dataURL）| 'none'。
   存储失败（隐私模式/超配额）不拦使用——本次会话内仍然生效。 */
export function setSkin(kind, value, base) {
  const record = (kind === 'builtin' || kind === 'custom') && value
    ? { kind, value, base: base || '' }
    : { kind: 'none' };
  try {
    localStorage.setItem(SKIN_KEY, JSON.stringify(record));
  } catch (err) {
    console.warn('[theme] 皮肤未能写入本机存储（可能超出容量），本次会话内仍生效', err);
  }
  applySkin(record);
}

export function getSkin() {
  return readSkin();
}

/* 自定义图片取平均底色：供防闪白脚本下次启动时先铺一层接近的底色。 */
export function skinBaseFromImage(dataUrl) {
  return new Promise((resolve) => {
    try {
      const img = new Image();
      img.onload = () => {
        try {
          const S = 8;
          const c = document.createElement('canvas');
          c.width = S;
          c.height = S;
          const ctx = c.getContext('2d');
          if (!ctx) return resolve('');
          ctx.drawImage(img, 0, 0, S, S);
          const d = ctx.getImageData(0, 0, S, S).data;
          let r = 0;
          let g = 0;
          let b = 0;
          let n = 0;
          for (let i = 0; i < d.length; i += 4) {
            r += d[i];
            g += d[i + 1];
            b += d[i + 2];
            n += 1;
          }
          if (!n) return resolve('');
          const hex = '#' + [r, g, b]
            .map((v) => Math.round(v / n).toString(16).padStart(2, '0'))
            .join('');
          resolve(hex);
        } catch {
          resolve('');
        }
      };
      img.onerror = () => resolve('');
      img.src = dataUrl;
    } catch {
      resolve('');
    }
  });
}
