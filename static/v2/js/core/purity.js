/* 正文纯净域（前端侧，与后端 bot/purity.py 同口径）。
 *
 * P0 铁律：能复制、能展示、能导出的正文 = 章题 + 章首引子 + 正文。
 * 本章概要、章节备注、修改说明、审核评分、字数统计、思考标签……一个字符都不许进剪贴板。
 *
 * 为什么要在前端再做一遍：后端 /api/chat/history 会读旧版 chat_log.jsonl，
 * 章节卡 content 可能带着历史污染；而工作台「复制全文」用的是编辑器里的实时内容
 * （可能刚粘贴过草稿）。前端这一层是最后一道闸门，口径必须与后端一致：
 * 只认 `## 章首引子` / `## 正文`，其余 `##` 小节一律丢弃。
 *
 * 改这里必须同步改 bot/purity.py，并跑 tests/test_purity.py。
 */

const INTRO_RE = /^(?:章首|章前)?引子$|^开篇引子$|^章首语$|^题记$/;
const BODY_RE = /^(?:本章|全章)?正文(?:内容|部分|全文)?$/;
const SUMMARY_RE = /^(?:本章)?(?:概要|摘要)$|^章节摘要$|^内容概要$/;
const NOTES_RE = /^(?:本章|章节|作者|创作|下章|写作)?备注$|^写作备注$/;
const NOISE_WORDS = [
  '修改', '改写', '修订', '去味', '去AI', 'AI腔', '润色', '编辑说明', '改动',
  '评分', '得分', '打分', '百分制', '审稿', '审核', '审阅', '评审', '点评', '评估',
  '质量', '体检', '自检', '自查', '检查', '问题', '建议', '意见', '待改进', '待办',
  '字数', '统计', '清单', '报告', '总结', '结论', '说明', '对比', '记录',
  '元信息', '参数', '状态', '版本', '下一步', '耗时'
];
const THINK_RE = /<(?:think|thinking|reasoning)>[\s\S]*?<\/(?:think|thinking|reasoning)>\s*/gi;
const THINK_OPEN_RE = /<(?:think|thinking|reasoning)>[\s\S]*$/gi;
const FENCE_RE = /```[a-zA-Z0-9]*\n?([\s\S]*?)\n?```/g;
const ACK_RE = /^(?:好的|没问题|收到|明白了|以下是|下面是|这是|当然|OK|Okay)[，,：:\s]+(?:以下是|下面是|这是)?[^。\n]{0,30}(?:[：:]|\n)\s*/;
const CH_TITLE_RE = /^#{1,3}\s*第\s*[0-9０-９一二三四五六七八九十百零]+\s*章[^\n]*$/;
const HEADER_RE = /^(#{1,6})[ \t]*(.+?)[ \t]*$/;
const SEP_RE = /^\s*(?:[-*_=]\s*){3,}$/;
const TAIL_NOISE_RE = /^[【\[（(]?\s*(?:本章|全章|章节)?\s*(?:修改说明|改写说明|修订说明|去味说明|润色说明|编辑说明|调整说明|改动说明|评分|得分|总分|综合评分|质量分|质量评分|审稿意见|审核意见|评审意见|点评|字数统计|字数|自检|自查|待改进|修改要点|改动要点|说明|备注)\s*[】\]）)]?\s*[:：]/;
const TAIL_SCORE_RE = /^[【\[（(]?\s*(?:综合|总体|加权)?\s*(?:评分|得分|总分|百分制|质量分)\s*(?:为|是)?\s*[:：]?\s*\d{1,3}\s*(?:分|\/100|\/\s*100)/;
const TAIL_TABLE_RE = /^\|.*(?:维度|权重|得分|评分|分值|项目).*\|/;
const TAIL_DONE_RE = /^[（(]?\s*本章(?:完|结束|正文完)\s*[)）]?$/;
const TAIL_WEIGHT_RE = /^\|\s*(?:结构|因果|人物|连续性|语言|读者体验|节奏|文笔)\s*\|/;
const FENCE_LINE_RE = /^[ \t]*```[a-zA-Z0-9_+-]*[ \t]*$/gm;

/* 正文契约标记（v4.22.3）：生成时要求模型把正文写在标记之间，复制/导出只取框内。
   === 一幕·YIMU 神笔工作台·开篇 ===
   === 一幕·YIMU 神笔工作台·结尾 ===
   容错：允许 ===/---/***、#、>、`、【】 装饰与 ·/•/・ 混用。 */
const MARK_NAME = '一幕\\s*[·•・.．]\\s*YIMU\\s*[·•・.．]?\\s*神笔(?:工作台|创作台|创作系统)?\\s*[·•・.．]\\s*';
const MARK_DECOR = '[\\s=\\-#*>*`~【\\[「(（|:：]*';
const MARK_DECOR_END = '[\\s=\\-#*<`~】\\]」)）|]*';
const MARK_OPEN_RE = new RegExp(`^${MARK_DECOR}${MARK_NAME}开篇${MARK_DECOR_END}$`);
const MARK_CLOSE_RE = new RegExp(`^${MARK_DECOR}${MARK_NAME}结尾${MARK_DECOR_END}$`);
const MARK_ANY_RE = new RegExp(`^${MARK_DECOR}${MARK_NAME}(?:开篇|结尾)${MARK_DECOR_END}$`, 'gm');

export const MARK_OPEN = '一幕·YIMU 神笔工作台·开篇';
export const MARK_CLOSE = '一幕·YIMU 神笔工作台·结尾';

/* 按契约标记切分 → {before, inner, after, marked} */
export function splitMarked(text) {
  const t = String(text || '').replace(/\r\n?/g, '\n').replace(/^\ufeff/, '');
  const lines = t.split('\n');
  let open = -1;
  let close = -1;
  for (let i = 0; i < lines.length; i += 1) {
    const s = lines[i].trim();
    if (open < 0) {
      if (MARK_OPEN_RE.test(s)) {
        open = i;
      } else if (MARK_CLOSE_RE.test(s)) {
        close = i;
        break;
      }
    } else if (MARK_CLOSE_RE.test(s)) {
      close = i;
      break;
    }
  }
  if (open >= 0 && close > open) {
    return {
      before: lines.slice(0, open).join('\n'),
      inner: lines.slice(open + 1, close).join('\n'),
      after: lines.slice(close + 1).join('\n'),
      marked: true
    };
  }
  if (open >= 0) {
    return { before: lines.slice(0, open).join('\n'), inner: lines.slice(open + 1).join('\n'), after: '', marked: true };
  }
  if (close >= 0) {
    return { before: '', inner: lines.slice(0, close).join('\n'), after: lines.slice(close + 1).join('\n'), marked: true };
  }
  return { before: '', inner: t, after: '', marked: false };
}

function stripMarks(text) {
  return String(text || '').replace(MARK_ANY_RE, '');
}
/* 章题之前的「前言噪音」——模型把改写说明/修改逻辑甩在章题前面（第90章实证）。 */
const LEAD_MARK_RE = /^[\s【\[（(]*(?:这|那)?\s*(?:是|为)?\s*(?:一份|一个|一篇|一份针对)?\s*(?:针对|关于|专门为|围绕)\s*[《「“"][^》」”"]{0,40}[》」”"]\s*(?:的)?\s*(?:最小|完整|精简|局部|轻度|定向|全文)?\s*(?:改写|重写|修改|润色|去味|修订|调整|重制)/;
const LEAD_HEAD_RE = /^[\s【\[（(]*(?:(?:主要)?(?:修改|改写|修订|润色|去味|调整)(?:逻辑|说明|要点|点|项|思路|原则|清单|方向)|(?:以下|下面|上述)(?:是|为)|(?:修改|改写)(?:如下|说明如下))/;
const LEAD_ITEM_RE = /^\s*(?:\d{1,2}[.、)）]|[-*•])\s+/;

function cnLen(text) {
  const m = String(text || '').match(/[\u4e00-\u9fff]/g);
  return m ? m.length : 0;
}

function isNoiseTitle(title) {
  const t = String(title || '').replace(/[\s:：、.。·—\-]+/g, '');
  if (!t) return false;
  return NOISE_WORDS.some((w) => t.includes(w));
}

function classify(title) {
  const t = String(title || '')
    .replace(/^[\s\d一二三四五六七八九十、.．)）(（]+/, '')
    .replace(/[\s:：。.]+$/, '');
  if (SUMMARY_RE.test(t)) return 'summary';
  if (INTRO_RE.test(t)) return 'intro';
  if (BODY_RE.test(t)) return 'body';
  if (NOTES_RE.test(t)) return 'notes';
  if (isNoiseTitle(t)) return 'noise';
  return 'unknown';
}

/* 剥掉开头「改写说明 + 修改逻辑清单」块；遇到正常正文段立即停止。 */
function dropLeadNoise(text) {
  const paras = String(text || '').split(/\n\s*\n/);
  let i = 0;
  let dropped = false;
  while (i < paras.length) {
    const p = paras[i].trim();
    if (!p) { i += 1; continue; }
    if (LEAD_MARK_RE.test(p) || LEAD_HEAD_RE.test(p)) {
      dropped = true;
      i += 1;
      while (i < paras.length && (!paras[i].trim() || LEAD_ITEM_RE.test(paras[i].trim()))) i += 1;
      continue;
    }
    break;
  }
  return dropped ? paras.slice(i).join('\n\n') : text;
}

export function stripModelNoise(text) {
  let t = String(text || '').replace(/\r\n?/g, '\n').replace(/^\ufeff/, '');
  t = t.replace(THINK_RE, '').replace(THINK_OPEN_RE, '');
  t = t.replace(FENCE_RE, '$1');
  t = t.replace(FENCE_LINE_RE, '');
  t = stripMarks(t);
  for (let i = 0; i < 3; i += 1) {
    const next = t.replace(/^\s+/, '').replace(ACK_RE, '');
    if (next === t) break;
    t = next;
  }
  /* 关键规则（与后端一致）：章题之前的内容一律不是正文——整块丢弃。 */
  const lines = t.split('\n');
  const ti = lines.findIndex((ln) => CH_TITLE_RE.test(ln.trim()));
  if (ti > 0) t = lines.slice(ti).join('\n');
  return dropLeadNoise(t).trim();
}

function isTailNoisePara(p) {
  const s = String(p || '').trim();
  if (!s) return true;
  if (TAIL_NOISE_RE.test(s) || TAIL_SCORE_RE.test(s) || TAIL_TABLE_RE.test(s)) return true;
  const lines = s.split('\n').filter((ln) => ln.trim());
  if (lines.length && lines.every((ln) => TAIL_WEIGHT_RE.test(ln.trim()) || ln.trim().startsWith('|'))) {
    return true;
  }
  if (s.length <= 120 && /(本章|全章)[^。\n]{0,10}(评分|得分|字数)/.test(s)) return true;
  return false;
}

export function stripTailNoise(text) {
  const paras = String(text || '').split(/\n\s*\n/);
  while (paras.length) {
    const p = paras[paras.length - 1].trim();
    if (!p || SEP_RE.test(p) || TAIL_DONE_RE.test(p) || isTailNoisePara(p)) {
      paras.pop();
      continue;
    }
    break;
  }
  return paras.filter((x) => x.trim()).join('\n\n');
}

function stripMdStyle(text) {
  let t = String(text || '');
  t = t.replace(/\*{1,3}([^*\n]{1,80}?)\*{1,3}/g, '$1');
  t = t.replace(/^(\s*)[-*•]\s+/gm, '$1');
  t = t.replace(/_{1,3}([^_\n]{1,80}?)_{1,3}/g, '$1');
  t = t.replace(/`+/g, '');
  t = t.replace(/^\s*([-*_]\s*){3,}$/gm, '');
  return t;
}

function splitSections(text) {
  const head = [];
  const secs = [];
  let cur = null;
  let titled = false;
  for (const line of String(text || '').split('\n')) {
    const m = line.match(HEADER_RE);
    if (m && m[1].length === 1 && CH_TITLE_RE.test(line.trim())) {
      if (!titled && cur === null) {
        head.push(line);
        titled = true;
      }
      continue;
    }
    if (m && m[1].length <= 2 && !SEP_RE.test(line)) {
      const title = m[2].trim();
      let kind = classify(title);
      let rest = '';
      const pairs = [[SUMMARY_RE, 'summary'], [INTRO_RE, 'intro'], [BODY_RE, 'body'], [NOTES_RE, 'notes']];
      for (const [re, k] of pairs) {
        const mm = title.match(re);
        if (mm) {
          kind = k;
          rest = title.slice(mm[0].length).replace(/^[ ：:，,。]+/, '');
          break;
        }
      }
      cur = { kind, title, level: m[1].length, lines: rest ? [rest] : [] };
      secs.push(cur);
      continue;
    }
    (cur ? cur.lines : head).push(line);
  }
  return { head, secs };
}

function joinBlocks(blocks) {
  return blocks.filter((b) => b && b.trim()).join('\n\n').replace(/\n{3,}/g, '\n\n').trim();
}

function renderKept(kept, keepIntro) {
  const parts = [];
  for (const { kind, lines } of kept) {
    if (kind === 'intro' && !keepIntro) continue;
    let body = lines.join('\n');
    body = kind === 'body' ? stripTailNoise(body) : body.replace(/^\n+|\n+$/g, '');
    if (body.trim()) parts.push(body);
  }
  return joinBlocks(parts);
}

function contentBlocks(secs, strict) {
  const kept = [];
  for (const s of secs) {
    if (s.kind === 'intro' || s.kind === 'body') kept.push(s);
    else if (s.kind === 'summary' || s.kind === 'notes' || s.kind === 'noise') continue;
    else if (!strict) kept.push(s);
  }
  return kept;
}

function dropMetaSections(text) {
  const { head, secs } = splitSections(text);
  const out = [...head];
  for (const s of secs) {
    if (s.kind === 'summary' || s.kind === 'notes' || s.kind === 'noise') {
      if (s.kind === 'summary') {
        const inner = s.lines.join('\n').trim();
        if (cnLen(inner) > 700) {
          const paras = inner.split(/\n\s*\n/);
          out.push((paras.length > 1 ? paras.slice(1) : paras).join('\n\n'));
        }
      }
      continue;
    }
    out.push(`${'#'.repeat(Math.max(2, s.level))} ${s.title}`);
    out.push(...s.lines);
  }
  return out.join('\n');
}

export function chapterTitle(text) {
  const raw = stripModelNoise(text);
  const { head } = splitSections(raw);
  for (const ln of head) {
    const s = ln.trim();
    if (!s) continue;
    if (CH_TITLE_RE.test(s)) return s.replace(/^#{1,6}\s*/, '').trim();
  }
  return '';
}

function stripTitleAndTail(raw, text, keepIntro) {
  let out = text;
  const title = chapterTitle(raw);
  if (title) {
    out = out.replace(new RegExp(`^#{1,4}\\s*${title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*$`, 'm'), '');
  }
  if (!keepIntro) out = out.replace(/^#{1,4}\s*(?:章首|章前)?引子\s*$/m, '');
  return stripTailNoise(out.replace(/^\n+|\n+$/g, ''));
}

/* 可导出的正文内容：章首引子 + 正文，不含章题/小节标记/任何元信息。 */
export function chapterBody(text, { keepIntro = true } = {}) {
  /* ① 有契约标记时只取标记之间——框外的东西在结构上就不是正文 */
  const mk = splitMarked(text);
  const raw = stripModelNoise(mk.marked ? mk.inner : text);
  const { secs } = splitSections(raw);
  const kinds = new Set(secs.map((s) => s.kind));
  let out;
  if (kinds.has('body') || kinds.has('intro')) {
    out = renderKept(contentBlocks(secs, kinds.has('body')), keepIntro);
  } else {
    out = stripTitleAndTail(raw, dropMetaSections(raw), keepIntro);
  }
  return stripMdStyle(out).replace(/\n{3,}/g, '\n\n').trim();
}

/* 可复制/可展示的整章：章题（可选）+ 章首引子 + 正文。 */
export function cleanChapterText(text, { keepTitle = true, keepIntro = true } = {}) {
  const body = chapterBody(text, { keepIntro });
  if (!keepTitle) return body;
  const title = chapterTitle(text);
  if (title && body) return `# ${title}\n\n${body}`;
  if (title) return `# ${title}`;
  return body;
}

export default {
  cleanChapterText, chapterBody, chapterTitle, stripModelNoise, stripTailNoise,
  splitMarked, MARK_OPEN, MARK_CLOSE
};
