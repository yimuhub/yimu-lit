/* 光标特效（v5.3.0）：四款——挥毫溅墨 / 墨纹涟漪 / 文字鼓励 / 烈焰。
 * Web Animations API 实现，粒子挂在 fixed 全屏容器里（不拦截任何事件）；
 * 全局开关与特效种类持久化 localStorage（ew-typing-fx）。
 * 文案库为本库自研（15 条随机彩句），不取自外部项目。
 * 铁律：任何失败只 console.warn，绝不影响写作主流程。
 * 对外契约：trigger(x, y, type) + setEnabled / setKind（另附 getState）。 */

const KEY = 'ew-typing-fx';

/* 四款特效元数据：settings 页直接用这份清单渲染选择卡。 */
export const FX_KINDS = [
  { id: 'splash', name: '挥毫溅墨', desc: '笔锋落下溅起墨点' },
  { id: 'ripple', name: '墨纹涟漪', desc: '墨圈在光标处荡开' },
  { id: 'cheer', name: '文字鼓励', desc: '随机彩句与 emoji 浮起' },
  { id: 'flame', name: '烈焰', desc: '键键生焰，越写越燃' }
];
const KIND_IDS = FX_KINDS.map((k) => k.id);

/* 自研鼓励文案库（随机抽一条；emoji 另池随机，可叠加） */
const CHEERS = [
  '迟早成神',
  '妙笔生花',
  '今天也是码字之星',
  '下笔如有神',
  '文思泉涌',
  '这个开头绝了',
  '读者正在催更',
  '手感火热',
  '字字珠玑',
  '灵气来了',
  '一气呵成',
  '打字如飞',
  '灵感上头',
  '今天必然完稿',
  '笔下生风'
];
const CHEER_EMOJI = ['✨', '🖋️', '🔥', '🌟', '🍵', '📜', '🚀', '🐉'];

let state = { enabled: false, kind: 'splash' };
let layer = null;
let installed = false;

function readState() {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || 'null');
    if (raw && typeof raw === 'object') {
      state = { enabled: raw.enabled === true, kind: KIND_IDS.includes(raw.kind) ? raw.kind : 'splash' };
    }
  } catch {
    /* 记号损坏用默认值 */
  }
}

function persist() {
  try {
    localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    /* 存储不可用时设置只活在本次会话 */
  }
}

/* 粒子容器：懒创建、幂等；找不到 body 就放弃（不抛错）。 */
function ensureLayer() {
  if (layer && layer.isConnected) return layer;
  if (typeof document === 'undefined' || !document.body) return null;
  layer = document.createElement('div');
  layer.className = 'typing-fx-layer';
  layer.setAttribute('aria-hidden', 'true');
  document.body.append(layer);
  return layer;
}

/* 取主题语义色（失败回退固定值），让特效跟主题一起换妆。 */
function cssColor(name, fallback) {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  } catch {
    return fallback;
  }
}

/* 统一的 WAAPI 封装：不支持 element.animate 的环境退化为定时移除，不抛错。 */
function animateOut(el, frames, opts) {
  const life = (opts && opts.duration) || 600;
  let finished = false;
  const done = () => {
    if (finished) return;
    finished = true;
    el.remove();
  };
  try {
    if (typeof el.animate === 'function') {
      const anim = el.animate(frames, opts);
      anim.onfinish = done;
      anim.onerror = done;
    }
  } catch {
    /* WAAPI 不可用：走兜底定时清理 */
  }
  setTimeout(done, life + 400);
}

function rand(min, max) {
  return min + Math.random() * (max - min);
}

/* 挥毫溅墨：一小撮墨点自光标向上抛出、再受重力落下，逐渐缩小消散。 */
function spawnSplash(x, y) {
  const host = ensureLayer();
  if (!host) return;
  const ink = cssColor('--ink-primary', '#1c1e24');
  const deep = cssColor('--accent-ink', ink);
  const count = Math.round(rand(6, 9));
  for (let i = 0; i < count; i += 1) {
    const dot = document.createElement('span');
    const size = rand(3, 8);
    dot.style.cssText = `left:${x}px;top:${y}px;width:${size}px;height:${size}px;`
      + `border-radius:50%;background:${Math.random() < 0.3 ? deep : ink};opacity:.95;`;
    host.append(dot);
    const ang = rand(-Math.PI * 0.85, -Math.PI * 0.15);   // 上半抛物线
    const dist = rand(16, 46);
    const dx = Math.cos(ang) * dist;
    const dy = Math.sin(ang) * dist;
    animateOut(dot, [
      { transform: 'translate(0,0) scale(1)', opacity: 0.95 },
      { transform: `translate(${dx * 0.62}px, ${dy}px) scale(${rand(0.65, 0.95)})`, opacity: 0.85, offset: 0.45 },
      { transform: `translate(${dx}px, ${dy + 12}px) scale(0.2)`, opacity: 0 }
    ], { duration: rand(520, 780), easing: 'cubic-bezier(0.2, 0.8, 0.3, 1)' });
  }
}

/* 墨纹涟漪：两圈墨环自光标荡开，外圈稍带主题强调色。 */
function spawnRipple(x, y) {
  const host = ensureLayer();
  if (!host) return;
  const ring = (color, size, delay) => {
    setTimeout(() => {
      const el = document.createElement('span');
      el.style.cssText = `left:${x}px;top:${y}px;width:${size}px;height:${size}px;`
        + `margin:${-size / 2}px 0 0 ${-size / 2}px;border-radius:50%;`
        + `border:1.5px solid ${color};opacity:.75;background:transparent;`;
      host.append(el);
      animateOut(el, [
        { transform: 'scale(0.3)', opacity: 0.75 },
        { transform: 'scale(2.6)', opacity: 0 }
      ], { duration: 680, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' });
    }, delay);
  };
  ring(cssColor('--ink-primary', '#1c1e24'), 12, 0);
  ring(cssColor('--accent', '#4176e6'), 8, 120);
}

/* 文字鼓励：随机彩句（+emoji）自光标浮起、微旋、淡出。 */
function spawnCheer(x, y) {
  const host = ensureLayer();
  if (!host) return;
  const accent = cssColor('--accent', '#4176e6');
  const text = CHEERS[Math.floor(Math.random() * CHEERS.length)];
  const el = document.createElement('span');
  el.textContent = text;
  el.style.cssText = `left:${x}px;top:${y}px;white-space:nowrap;`
    + `font:600 13px/1 "Songti SC","Noto Serif CJK SC",serif;`
    + `color:${accent};text-shadow:0 1px 6px rgba(0,0,0,.12);`;
  host.append(el);
  animateOut(el, [
    { transform: 'translate(-50%, -120%) translateY(0) rotate(0deg)', opacity: 0 },
    { transform: 'translate(-50%, -120%) translateY(-12px) rotate(-1deg)', opacity: 1, offset: 0.2 },
    { transform: 'translate(-50%, -120%) translateY(-34px) rotate(-2.5deg)', opacity: 0 }
  ], { duration: 1150, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' });
  if (Math.random() < 0.6) {
    const emo = document.createElement('span');
    emo.textContent = CHEER_EMOJI[Math.floor(Math.random() * CHEER_EMOJI.length)];
    emo.style.cssText = `left:${x + rand(-26, 26)}px;top:${y - rand(10, 22)}px;font-size:15px;`;
    host.append(emo);
    animateOut(emo, [
      { transform: 'translate(0,0) scale(0.6)', opacity: 0 },
      { transform: 'translate(2px,-16px) scale(1.05)', opacity: 1, offset: 0.25 },
      { transform: 'translate(4px,-34px) scale(0.9)', opacity: 0 }
    ], { duration: 1050, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' });
  }
}

/* 烈焰：几粒火星自光标升腾、摇曳、熄灭，暖色取自告警语义色。 */
function spawnFlame(x, y) {
  const host = ensureLayer();
  if (!host) return;
  const warm = cssColor('--warn', '#d99022');
  const count = Math.round(rand(6, 8));
  for (let i = 0; i < count; i += 1) {
    setTimeout(() => {
      const el = document.createElement('span');
      const size = rand(5, 11);
      el.style.cssText = `left:${x + rand(-8, 8)}px;top:${y}px;width:${size}px;height:${size}px;`
        + `border-radius:50% 50% 50% 0;background:${warm};opacity:.9;filter:blur(0.4px);`;
      host.append(el);
      animateOut(el, [
        { transform: 'translate(0,0) scale(1) rotate(0deg)', opacity: 0.9 },
        { transform: `translate(${rand(-10, 10)}px, ${-rand(24, 56)}px) scale(${rand(0.4, 0.7)}) rotate(${rand(-40, 40)}deg)`, opacity: 0.6, offset: 0.5 },
        { transform: `translate(${rand(-14, 14)}px, ${-rand(44, 72)}px) scale(0.15)`, opacity: 0 }
      ], { duration: rand(500, 720), easing: 'cubic-bezier(0.2, 0.7, 0.3, 1)' });
    }, Math.round(rand(0, 90)));
  }
}

const SPAWNERS = { splash: spawnSplash, ripple: spawnRipple, cheer: spawnCheer, flame: spawnFlame };

/* 对外契约：在视口坐标 (x, y) 触发一次特效；type 缺省用当前选中种类。 */
export function trigger(x, y, type) {
  try {
    if (!state.enabled) return;
    const spawn = SPAWNERS[KIND_IDS.includes(type) ? type : state.kind];
    if (!spawn || typeof x !== 'number' || typeof y !== 'number') return;
    spawn(x, y);
  } catch (err) {
    console.warn('[光标特效] 渲染失败（已忽略）', err);
  }
}

export function setEnabled(on) {
  state.enabled = !!on;
  persist();
}

export function isEnabled() {
  return state.enabled;
}

export function setKind(kind) {
  if (!KIND_IDS.includes(kind)) return;
  state.kind = kind;
  persist();
}

export function getKind() {
  return state.kind;
}

export function getState() {
  return { enabled: state.enabled, kind: state.kind };
}

/* 估算光标的视口坐标：优先取选区末端的矩形，取不到就落在焦点元素右下角。 */
function caretPoint() {
  try {
    const sel = typeof window !== 'undefined' && window.getSelection ? window.getSelection() : null;
    if (sel && sel.rangeCount) {
      const range = sel.getRangeAt(0);
      const rects = range.getClientRects ? range.getClientRects() : [];
      const last = rects.length ? rects[rects.length - 1] : null;
      if (last && (last.right || last.bottom)) return { x: last.right, y: last.bottom };
    }
  } catch {
    /* 选区不可用走兜底 */
  }
  try {
    const el = document.activeElement;
    if (el && typeof el.getBoundingClientRect === 'function') {
      const b = el.getBoundingClientRect();
      return { x: Math.max(24, b.right - 28), y: b.bottom - 6 };
    }
  } catch {
    /* ignore */
  }
  return null;
}

function isEditable(el) {
  if (!el || !el.tagName) return false;
  const tag = el.tagName.toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable === true;
}

function onKeydown(e) {
  try {
    if (!state.enabled || e.repeat || e.ctrlKey || e.metaKey || e.altKey) return;
    if (!isEditable(e.target)) return;
    const p = caretPoint();
    if (p) trigger(p.x, p.y);
  } catch (err) {
    console.warn('[光标特效] 击键监听出错（已忽略）', err);
  }
}

/* 幂等安装：keydown 触发特效。theme.js 在 boot 时带起。 */
export function installTypingFx() {
  if (installed || typeof document === 'undefined') return;
  installed = true;
  readState();
  document.addEventListener('keydown', onKeydown, true);
}

/* 模块加载即安装（幂等、失败只 warn）：theme.js import 本模块即完成挂载。 */
try {
  installTypingFx();
} catch (err) {
  console.warn('[光标特效] 安装失败（不影响写作主流程）', err);
}
