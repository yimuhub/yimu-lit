/* 打字音效（v5.3.0）：WebAudio 现场合成三款机械键盘手感——青轴 / 茶轴 / 打字机。
 * 纯本地合成（噪声脉冲 + 低频闷响），无音频文件、无网络请求；
 * 每次击键对频率/音量/时长做 ±8% 随机微调，避免"机关枪"式的机械重复感。
 * AudioContext 必须在用户手势后才能出声：首次 keydown / pointerdown 时解锁。
 * 开关、音色、音量持久化 localStorage（ew-typing-sound）。
 * 铁律：任何失败只 console.warn，绝不影响写作主流程。
 * 对外契约：setEnabled / playKey / mute（另附 setKind / setVolume / getState）。 */

const KEY = 'ew-typing-sound';

/* 三款音色元数据：settings 页直接用这份清单渲染选择卡。 */
export const SOUND_KINDS = [
  { id: 'blue', name: '青轴', desc: '清脆利落，段落感强' },
  { id: 'brown', name: '茶轴', desc: '沉稳顺滑，沙沙轻响' },
  { id: 'typewriter', name: '打字机', desc: '咔哒脆响加铜鼓闷声' }
];

/* 每款音色的合成参数：click = 高频噪声脉冲（键帽敲击），thump = 低频正弦闷响（键床回弹）。 */
const KIND_PARAMS = {
  blue: { clickFreq: 3200, clickDur: 0.032, clickGain: 0.9, thumpFreq: 190, thumpDur: 0.045, thumpGain: 0.3 },
  brown: { clickFreq: 1800, clickDur: 0.038, clickGain: 0.7, thumpFreq: 150, thumpDur: 0.06, thumpGain: 0.45 },
  typewriter: { clickFreq: 4200, clickDur: 0.026, clickGain: 1.0, thumpFreq: 110, thumpDur: 0.085, thumpGain: 0.8 }
};
const KIND_IDS = SOUND_KINDS.map((k) => k.id);

let state = { enabled: false, kind: 'blue', volume: 0.6 };
let muted = false;        // 运行时静音（v5.6 听书等场景临时调用），不落盘
let audio = null;         // AudioContext，首次用户手势时才创建
let noiseBuf = null;      // 预生成的白噪声缓冲（0.12s）
let installed = false;

function readState() {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || 'null');
    if (raw && typeof raw === 'object') {
      state = {
        enabled: raw.enabled === true,
        kind: KIND_IDS.includes(raw.kind) ? raw.kind : 'blue',
        volume: Math.min(1, Math.max(0, Number(raw.volume) || 0.6))
      };
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

/* ±8% 随机微调：同一段文字敲起来每一下都略有不同，防"机关枪"感。 */
function jitter(value, ratio = 0.08) {
  return value * (1 + (Math.random() * 2 - 1) * ratio);
}

function makeNoiseBuffer(ac) {
  const len = Math.max(1, Math.floor(ac.sampleRate * 0.12));
  const buf = ac.createBuffer(1, len, ac.sampleRate);
  const ch = buf.getChannelData(0);
  for (let i = 0; i < len; i += 1) ch[i] = Math.random() * 2 - 1;
  return buf;
}

/* 用户手势内调用才有效；无 AudioContext（老浏览器/测试环境）返回 null 静默降级。 */
function ensureAudio() {
  try {
    if (!audio) {
      const AC = typeof window !== 'undefined' ? (window.AudioContext || window.webkitAudioContext) : null;
      if (!AC) return null;
      audio = new AC();
      noiseBuf = makeNoiseBuffer(audio);
    }
    if (audio.state === 'suspended' && typeof audio.resume === 'function') {
      audio.resume().catch(() => {});
    }
    return audio;
  } catch (err) {
    console.warn('[打字音效] AudioContext 初始化失败，音效本次不可用', err);
    return null;
  }
}

/* 用户手势解锁（设置页开关打开时、以及首个 keydown/pointerdown 都会调）。 */
export function unlock() {
  ensureAudio();
}

/* 播一次击键音。kind 可选（blue/brown/typewriter），缺省用当前音色。
   开关关闭、运行时静音、音量为 0 或环境不支持时直接返回，绝不抛错。 */
export function playKey(kind) {
  try {
    if (!state.enabled || muted) return;
    const p = KIND_PARAMS[KIND_IDS.includes(kind) ? kind : state.kind];
    if (!p) return;
    const vol = Math.min(1, Math.max(0, state.volume));
    if (vol <= 0) return;
    const ac = ensureAudio();
    if (!ac || !noiseBuf || ac.state === 'closed') return;
    /* 微小的随机起始偏移，进一步打散节奏 */
    const t = ac.currentTime + Math.random() * 0.004;

    /* 1) 高频噪声脉冲：键帽敲击的"嗒" */
    const src = ac.createBufferSource();
    src.buffer = noiseBuf;
    const bp = ac.createBiquadFilter();
    bp.type = 'bandpass';
    bp.frequency.value = jitter(p.clickFreq);
    bp.Q.value = 1.1;
    const cg = ac.createGain();
    const cPeak = jitter(p.clickGain) * vol;
    cg.gain.setValueAtTime(cPeak, t);
    cg.gain.exponentialRampToValueAtTime(0.0001, t + jitter(p.clickDur));
    src.connect(bp);
    bp.connect(cg);
    cg.connect(ac.destination);
    src.start(t);
    src.stop(t + p.clickDur + 0.05);

    /* 2) 低频正弦闷响：键床回弹的"咚" */
    const osc = ac.createOscillator();
    osc.type = 'sine';
    osc.frequency.value = jitter(p.thumpFreq);
    const tg = ac.createGain();
    const tPeak = jitter(p.thumpGain) * vol;
    tg.gain.setValueAtTime(tPeak, t);
    tg.gain.exponentialRampToValueAtTime(0.0001, t + jitter(p.thumpDur));
    osc.connect(tg);
    tg.connect(ac.destination);
    osc.start(t);
    osc.stop(t + p.thumpDur + 0.05);
  } catch (err) {
    console.warn('[打字音效] 播放失败（已忽略）', err);
  }
}

export function setEnabled(on) {
  state.enabled = !!on;
  persist();
  if (state.enabled) unlock();   // 开关通常点在鼠标点击（用户手势）里，顺势解锁
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

/* 音量 0-1（设置页滑杆 0-100 除以 100 后传入）。 */
export function setVolume(v) {
  const n = Number(v);
  state.volume = Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : 0.6;
  persist();
}

export function getVolume() {
  return state.volume;
}

/* 运行时静音（不落盘、不改开关）：v5.6 听书播放时调用 mute(true)，结束恢复。 */
export function mute(on) {
  muted = !!on;
}

export function isMuted() {
  return muted;
}

export function getState() {
  return { enabled: state.enabled, kind: state.kind, volume: state.volume };
}

/* 只在可编辑元素里按键才出声；方向键/快捷键/长按不响。 */
function isEditable(el) {
  if (!el || !el.tagName) return false;
  const tag = el.tagName.toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable === true;
}

function onKeydown(e) {
  try {
    if (e.repeat || e.ctrlKey || e.metaKey || e.altKey) return;
    if (!isEditable(e.target)) return;
    playKey();
  } catch (err) {
    console.warn('[打字音效] 击键监听出错（已忽略）', err);
  }
}

function onPointerdown() {
  try {
    if (state.enabled) ensureAudio();   // 提前在手势里解锁，首次击键即出声
  } catch {
    /* ignore */
  }
}

/* 幂等安装：keydown 出声 + pointerdown 解锁。theme.js 在 boot 时带起。 */
export function installTypingSound() {
  if (installed || typeof document === 'undefined') return;
  installed = true;
  readState();
  document.addEventListener('keydown', onKeydown, true);
  document.addEventListener('pointerdown', onPointerdown, true);
}

/* 模块加载即安装（幂等、失败只 warn）：theme.js import 本模块即完成挂载。 */
try {
  installTypingSound();
} catch (err) {
  console.warn('[打字音效] 安装失败（不影响写作主流程）', err);
}
