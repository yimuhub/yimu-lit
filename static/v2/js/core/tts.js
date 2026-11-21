/* 听书（v5.4.x，docs/系统化升级方案-v5.1 第 8.3 节）：Web Speech API 纯前端朗读。
 * 逐句朗读（中文句读切分）+ 逐句回调（编辑器内 revealOffset 定位/预览高亮）+
 * 语速循环（0.8/1.0/1.25/1.5）+ 暂停/继续/停止 + 听书位置记忆（书+章+句索引，
 * localStorage `ew-tts-pos:<book>#<chapter>`）。
 * 联动 v5.3 打字音：播放期间调 typingsound.mute(true) 自动静音，结束/停止恢复
 * （mute 在 typingsound.js 已导出，调用处 try/catch 包裹，联动失败不影响朗读）。
 * 铁律：听书是附加能力——不支持时 supported()=false，由调用方 toast 降级说明；
 * 任何失败只 console.warn，绝不影响写作主链路。
 * 对外契约：splitSentences / posKey / savePos / loadPos / clearPos /
 *           RATES / nextRate / ttsSupported / hasChineseVoice / createTTS。 */

import { mute as muteTypingSound } from './typingsound.js';

/* ---------------------------------------------------------------- 语速 */

/* 语速档位（循环切换）：0.8 慢速精听 → 1.5 快速过章。 */
export const RATES = [0.8, 1.0, 1.25, 1.5];

const RATE_KEY = 'ew-tts-rate';

/* 语速循环：当前档的下一档（未知档位按 1.0 处理，天然回绕）。 */
export function nextRate(rate) {
  const n = Number(rate);
  const i = RATES.includes(n) ? RATES.indexOf(n) : RATES.indexOf(1.0);
  return RATES[(i + 1) % RATES.length];
}

function loadRate() {
  try {
    const n = Number(localStorage.getItem(RATE_KEY));
    return RATES.includes(n) ? n : 1.0;
  } catch {
    return 1.0;
  }
}

function persistRate(rate) {
  try {
    localStorage.setItem(RATE_KEY, String(rate));
  } catch {
    /* 隐私模式：语速只活在本次会话 */
  }
}

/* ---------------------------------------------------------------- 中文句读 */

/* 句末标点：省略号按单字符计，连续标点（！！？！?…）合并进同一句。 */
const END_PUNCT = new Set(['。', '！', '？', '；', '!', '?', ';', '…']);
/* 句末标点后跟随的闭合引号/括号：归入当前句，不单独成句。 */
const CLOSE_CHARS = new Set(['」', '』', '”', '）', '】', '》', '"', '\u2019']);
/* 软停顿（超长句的强制断点）：，、：,——只在没有句末标点的长段里启用 */
const SOFT_PUNCT = new Set(['，', '、', '：', ':', ',']);
const SOFT_BREAK_LEN = 80;   // 无句末标点的连续文本超过该长度时在软停顿处断句

/**
 * 中文句读切分（纯函数，node 直跑测试）。
 * 「夜色降临。他问：“你是谁？”她没答。」→ 3 句；闭合引号跟随句末标点；
 * 换行独立断句；省略号/连续标点合并；超长无标点段在软停顿处断句。
 * 返回 [{text, start, end}]（start/end 为原文偏移，可用于编辑器 revealOffset 定位）。
 */
export function splitSentences(text) {
  const t = String(text || '');
  const out = [];
  let buf = '';
  let start = 0;
  for (let i = 0; i < t.length; i += 1) {
    const ch = t[i];
    buf += ch;
    const next = t[i + 1] || '';
    const endHit = (END_PUNCT.has(ch) && !END_PUNCT.has(next))
      || ch === '\n'
      || (SOFT_PUNCT.has(ch) && buf.length >= SOFT_BREAK_LEN && !END_PUNCT.has(next) && ch !== '\n');
    if (endHit) {
      /* 闭合引号/括号跟随句尾（「他说。"」的引号并进本句） */
      while (CLOSE_CHARS.has(t[i + 1] || '')) {
        buf += t[i + 1];
        i += 1;
      }
      if (buf.trim()) out.push({ text: buf, start, end: start + buf.length });
      buf = '';
      start = i + 1;
    }
  }
  if (buf.trim()) out.push({ text: buf, start, end: start + buf.length });
  return out;
}

/* ---------------------------------------------------------------- 位置记忆 */

const POS_PREFIX = 'ew-tts-pos';
const POS_KEEP = 100;   // 位置记忆最多保留章数（防 localStorage 膨胀，裁最旧）

/**
 * 位置记忆键结构（纯函数）：`ew-tts-pos:<book>#<chapter>`。
 * book 缺省 default，chapter 缺省 1——键永远可用，读写不抛错。
 */
export function posKey(book, chapter) {
  return `${POS_PREFIX}:${String(book || 'default')}#${String(chapter || '1')}`;
}

/**
 * 保存听书位置：{book, chapter, index(下一句下标), at}。
 * 隐私模式等 localStorage 不可用时静默跳过（位置记忆只是体验优化）。
 */
export function savePos(book, chapter, index) {
  try {
    localStorage.setItem(posKey(book, chapter), JSON.stringify({
      book: String(book || ''),
      chapter: Number(chapter) || 1,
      index: Math.max(0, Number(index) || 0),
      at: new Date().toISOString()
    }));
    prunePos(POS_KEEP);
  } catch {
    /* 存储不可用：静默 */
  }
}

/** 读听书位置；没有记录或记号损坏返回 null。 */
export function loadPos(book, chapter) {
  try {
    const raw = JSON.parse(localStorage.getItem(posKey(book, chapter)) || 'null');
    return raw && typeof raw === 'object' && Number.isFinite(raw.index) ? raw : null;
  } catch {
    return null;
  }
}

/** 清除某章听书位置（全章读完自动调用）。 */
export function clearPos(book, chapter) {
  try {
    localStorage.removeItem(posKey(book, chapter));
  } catch {
    /* 存储不可用：静默 */
  }
}

/** 位置记忆轮转：按 at 时间戳只保留最近 keep 条。 */
export function prunePos(keep = POS_KEEP) {
  try {
    const items = [];
    for (let i = 0; i < localStorage.length; i += 1) {
      const k = localStorage.key(i);
      if (!k || !k.startsWith(`${POS_PREFIX}:`)) continue;
      let at = '';
      try {
        at = String((JSON.parse(localStorage.getItem(k) || '{}') || {}).at || '');
      } catch {
        at = '';
      }
      items.push([k, at]);
    }
    items.sort((a, b) => (a[1] < b[1] ? 1 : -1));
    for (const [k] of items.slice(Math.max(1, Number(keep) || POS_KEEP))) {
      localStorage.removeItem(k);
    }
  } catch {
    /* 存储不可用：静默 */
  }
}

/* ---------------------------------------------------------------- 能力检测 */

/** Web Speech API 是否可用（node/老浏览器 → false，调用方据此 toast 降级）。 */
export function ttsSupported() {
  try {
    return typeof window !== 'undefined'
      && 'speechSynthesis' in window
      && typeof window.SpeechSynthesisUtterance === 'function';
  } catch {
    return false;
  }
}

/** 是否检测到中文语音包（语音列表常异步加载，空列表视为未知而非缺失）。 */
export function hasChineseVoice() {
  try {
    if (!ttsSupported()) return false;
    const voices = window.speechSynthesis.getVoices() || [];
    if (!voices.length) return true;   // 列表未就绪：不误报，交给系统默认语音
    return voices.some((v) => /zh|cmn|yue/i.test(String(v.lang || '')));
  } catch {
    return false;
  }
}

/* 打字音静音联动（v5.3 typingsound.mute 已导出；调用 try/catch，失败不影响朗读）。 */
function muteTyping(on) {
  try {
    muteTypingSound(on);
  } catch (err) {
    console.warn('[听书] 打字音静音联动失败（已忽略）', err);
  }
}

function pickVoice() {
  try {
    const voices = window.speechSynthesis.getVoices() || [];
    return voices.find((v) => /^zh(-|_)?/i.test(String(v.lang || ''))) || null;
  } catch {
    return null;
  }
}

/* ---------------------------------------------------------------- 朗读控制器 */

/**
 * 创建听书控制器。
 * @param {object} opts
 *   book/chapter  位置记忆的两级键（bench 传作品 key 与章号）
 *   onSentence(i, sent)   每句开始朗读时回调（sent 为 splitSentences 的条目；i=-1 表示出错终止）
 *   onState(state, info)  状态机回调：idle / playing / paused（info: {index,total,rate}）
 * @returns 控制器 { loadText, start, pause, resume, stop, setRate, cycleRate,
 *                    getRate, state, index, total, sentenceAt }
 */
export function createTTS({ book = '', chapter = 1, onSentence = null, onState = null } = {}) {
  const synth = ttsSupported() ? window.speechSynthesis : null;
  let sentences = [];
  let idx = 0;
  let state = 'idle';          // idle / playing / paused
  let rate = loadRate();
  let session = 0;             // 会话代号：旧 utterance 的 onend 不许串进新会话

  function setState(s) {
    state = s;
    try {
      if (onState) onState(s, { index: idx, total: sentences.length, rate });
    } catch {
      /* 回调异常不影响朗读 */
    }
  }

  function announce(i) {
    try {
      if (onSentence) onSentence(i, sentences[i] || null);
    } catch {
      /* 高亮回调异常不影响朗读 */
    }
  }

  /** 装载文本（切句）；返回句数。位置与状态归零。 */
  function loadText(text) {
    sentences = splitSentences(text);
    idx = 0;
    return sentences.length;
  }

  function finish() {
    /* 全章读完：清位置记忆、恢复打字音 */
    clearPos(book, chapter);
    muteTyping(false);
    setState('idle');
  }

  function speakCurrent() {
    if (!synth) return;
    const token = session;
    const sent = sentences[idx];
    if (!sent) {
      finish();
      return;
    }
    let u;
    try {
      u = new window.SpeechSynthesisUtterance(sent.text);
    } catch (err) {
      console.warn('[听书] 语音合成不可用，已停止', err);
      muteTyping(false);
      setState('idle');
      return;
    }
    u.rate = rate;
    u.lang = 'zh-CN';
    const v = pickVoice();
    if (v) u.voice = v;
    u.onend = () => {
      if (token !== session) return;   // 旧会话残响：忽略
      savePos(book, chapter, idx + 1); // 记忆下一句下标（0 基）
      idx += 1;
      if (idx >= sentences.length) {
        finish();
        return;
      }
      announce(idx);
      speakCurrent();
    };
    u.onerror = (ev) => {
      if (token !== session) return;
      const reason = ev && ev.error;
      if (reason === 'interrupted' || reason === 'canceled') return;   // stop/cancel 触发的中断：正常
      console.warn('[听书] 朗读中断：', reason);
      muteTyping(false);
      setState('idle');
      announce(-1);
    };
    try {
      synth.speak(u);
    } catch (err) {
      console.warn('[听书] speak 调用失败，已停止', err);
      muteTyping(false);
      setState('idle');
    }
  }

  /**
   * 开始朗读。text 先由调用方过纯净域（cleanChapterText），本模块只做句切分。
   * fromIndex 为起始句下标（位置记忆恢复用）。返回句数；不支持时返回 0。
   */
  function start(text, fromIndex = 0) {
    if (!synth) return 0;
    stop();
    loadText(text);
    if (!sentences.length) return 0;
    idx = Math.max(0, Math.min(Number(fromIndex) || 0, sentences.length - 1));
    session += 1;
    muteTyping(true);            // 听书播放期间静音打字音（结束/停止在 finish/stop 恢复）
    setState('playing');
    announce(idx);
    speakCurrent();
    return sentences.length;
  }

  function pause() {
    if (synth && state === 'playing') {
      try {
        synth.pause();
      } catch {
        /* pause 失败下次点击再试 */
      }
      setState('paused');
    }
  }

  function resume() {
    if (synth && state === 'paused') {
      try {
        synth.resume();
      } catch {
        /* resume 失败下次点击再试 */
      }
      setState('playing');
    }
  }

  function stop() {
    session += 1;                // 作废在途 utterance 回调
    if (synth) {
      try {
        synth.cancel();
        synth.resume?.();        // Chrome 的 cancel 不清 paused 态，顺手复位（无害）
      } catch {
        /* cancel 失败静默 */
      }
    }
    muteTyping(false);
    setState('idle');
  }

  /** 设置语速；播放中会取消当前 utterance 并以新语速重说当前句。 */
  function setRate(r) {
    const n = Number(r);
    rate = RATES.includes(n) ? n : rate;
    persistRate(rate);
    if (state === 'playing' && synth) {
      try {
        synth.cancel();
      } catch {
        /* cancel 失败静默 */
      }
      announce(idx);
      speakCurrent();
    }
    return rate;
  }

  function cycleRate() {
    return setRate(nextRate(rate));
  }

  return {
    loadText,
    start,
    pause,
    resume,
    stop,
    setRate,
    cycleRate,
    getRate: () => rate,
    get state() { return state; },
    get index() { return idx; },
    get total() { return sentences.length; },
    sentenceAt: (i) => sentences[i] || null
  };
}
