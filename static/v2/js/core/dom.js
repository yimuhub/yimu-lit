/* 轻量 DOM 工具：构建、转义、格式化、日期分组、拖拽文件校验。
   图标也在这里转出，组件统一从 dom.js 取 DOM 与图标能力。 */

export { icon, iconEl } from './icons.js';

export function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function escAttr(value) {
  return esc(value).replace(/`/g, '&#96;');
}

export function isHttpUrl(raw) {
  if (typeof raw !== 'string') return false;
  try {
    const u = new URL(raw);
    return u.protocol === 'http:' || u.protocol === 'https:';
  } catch {
    return false;
  }
}

export function safeUrl(raw) {
  return isHttpUrl(raw) ? raw : '';
}

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null || value === false) continue;
      if (key === 'class') el.className = value;
      else if (key === 'text') el.textContent = value;
      else if (key === 'html') el.innerHTML = value;
      else if (key === 'dataset') Object.assign(el.dataset, value);
      else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
      else if (key === 'on' && typeof value === 'object') {
        for (const [evt, fn] of Object.entries(value)) el.addEventListener(evt, fn);
      } else if (key === 'checked' || key === 'disabled' || key === 'hidden' || key === 'selected') {
        el[key] = Boolean(value);
        if (key === 'hidden' && !value) el.removeAttribute('hidden');
      } else if (key === 'value') {
        el.value = value;
      } else {
        el.setAttribute(key, value === true ? '' : String(value));
      }
    }
  }
  for (const child of children.flat(4)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function fragment(html) {
  const t = document.createElement('template');
  t.innerHTML = html;
  return t.content;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function on(el, type, handler, opts) {
  el.addEventListener(type, handler, opts);
  return () => el.removeEventListener(type, handler, opts);
}

export function delegate(root, selector, type, handler) {
  const fn = (event) => {
    const target = event.target instanceof Element ? event.target.closest(selector) : null;
    if (target && root.contains(target)) handler(event, target);
  };
  root.addEventListener(type, fn);
  return () => root.removeEventListener(type, fn);
}

export function debounce(fn, wait = 300) {
  let timer = 0;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}

export function throttle(fn, wait = 80) {
  let last = 0;
  let timer = 0;
  return (...args) => {
    const now = Date.now();
    const remain = wait - (now - last);
    if (remain <= 0) {
      clearTimeout(timer);
      last = now;
      fn(...args);
    } else if (!timer) {
      timer = setTimeout(() => {
        timer = 0;
        last = Date.now();
        fn(...args);
      }, remain);
    }
  };
}

export function uid(prefix = 'id') {
  return `${prefix}-${Math.random().toString(36).slice(2, 9)}${Date.now().toString(36).slice(-3)}`;
}

export function clamp(n, min, max) {
  return Math.min(max, Math.max(min, n));
}

export function fmtNum(n) {
  const v = Number(n) || 0;
  if (v >= 100000000) return `${(v / 100000000).toFixed(1).replace(/\.0$/, '')}亿`;
  if (v >= 10000) return `${(v / 10000).toFixed(1).replace(/\.0$/, '')}万`;
  return v.toLocaleString('zh-CN');
}

export function fmtWords(n) {
  const v = Number(n) || 0;
  if (v >= 10000) return `${(v / 10000).toFixed(1).replace(/\.0$/, '')} 万字`;
  return `${v.toLocaleString('zh-CN')} 字`;
}

export function fmtPct(n) {
  const v = Number(n) || 0;
  return `${Math.round(v)}%`;
}

export function pad2(n) {
  return String(n).padStart(2, '0');
}

export function parseDate(value) {
  if (!value) return null;
  if (value instanceof Date) return value;
  if (typeof value === 'number') return new Date(value < 1e12 ? value * 1000 : value);
  let s = String(value).trim();
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(s)) s = s.replace(' ', 'T');
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return null;
  return d;
}

export function fmtDate(value, withTime = false) {
  const d = parseDate(value);
  if (!d) return '';
  const now = new Date();
  const sameYear = d.getFullYear() === now.getFullYear();
  const datePart = `${sameYear ? '' : d.getFullYear() + '年'}${d.getMonth() + 1}月${d.getDate()}日`;
  return withTime ? `${datePart} ${pad2(d.getHours())}:${pad2(d.getMinutes())}` : datePart;
}

export function fmtTime(value) {
  const d = parseDate(value);
  if (!d) return '';
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

export function relTime(value) {
  const d = parseDate(value);
  if (!d) return '';
  const diff = Date.now() - d.getTime();
  if (diff < 60 * 1000) return '刚刚';
  if (diff < 3600 * 1000) return `${Math.floor(diff / 60000)} 分钟前`;
  if (diff < 24 * 3600 * 1000) return `${Math.floor(diff / 3600000)} 小时前`;
  if (diff < 7 * 24 * 3600 * 1000) return `${Math.floor(diff / 86400000)} 天前`;
  return fmtDate(d);
}

export function dayStart(date = new Date()) {
  const d = new Date(date);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

export function groupByRecency(items, getDate = (i) => i.updated_at || i.created_at) {
  const today = dayStart();
  const day = 86400000;
  const groups = [
    { label: '今天', key: 'today', items: [] },
    { label: '昨天', key: 'yesterday', items: [] },
    { label: '本周', key: 'week', items: [] },
    { label: '更早', key: 'earlier', items: [] }
  ];
  for (const item of items) {
    const d = parseDate(getDate(item));
    const t = d ? d.getTime() : 0;
    if (!t) groups[3].items.push(item);
    else if (t >= today) groups[0].items.push(item);
    else if (t >= today - day) groups[1].items.push(item);
    else if (t >= today - day * 6) groups[2].items.push(item);
    else groups[3].items.push(item);
  }
  return groups.filter((g) => g.items.length);
}

export function statusMeta(state, label) {
  const map = {
    completed: { tone: 'success', label: label || '已出书' },
    in_progress: { tone: 'running', label: label || '创作中' },
    planning: { tone: 'warn', label: label || '规划中' },
    writing: { tone: 'running', label: label || '写作中' },
    validating: { tone: 'running', label: label || '校验中' },
    awaiting_title: { tone: 'warn', label: label || '选书名' },
    failed: { tone: 'danger', label: label || '失败' },
    idle: { tone: 'muted', label: label || '草稿' }
  };
  return map[state] || { tone: 'muted', label: label || '未开始' };
}

export const MEDIUM_LABELS = { novel: '小说', script: '剧本', poetry: '诗词', prose: '散文', thesis: '论文', children: '儿童文学', news: '新闻稿', copy: '文案', other: '其他' };
/* 各文体的进度单位不同：小说计章、剧本计场、诗词计改稿轮、散文计阶段。 */
export const MEDIUM_UNITS = { novel: '章', script: '场', poetry: '轮', prose: '阶段', thesis: '章', children: '篇', news: '份', copy: '篇', other: '篇' };

export function mediumChip(medium) {
  /* v4.2.0：四文体全部显示彩色标签（此前小说不显示） */
  const label = MEDIUM_LABELS[medium];
  if (!label) return null;
  return h('span', { class: 'lib-medium', dataset: { medium }, text: label, title: `${label}作品` });
}

export function initials(name) {
  const s = String(name || '').trim();
  if (!s) return '书';
  return s.slice(0, 1);
}

/* v4.2.0：左栏/封面按文体显示单字头像（小/剧/散/诗/词/论/赋/歌） */
export function mediumGlyph(item) {
  if (!item || typeof item !== 'object') return null;
  const medium = item.medium || '';
  const form = String(item.form || '');
  if (medium === 'poetry') {
    if (form.includes('词')) return '词';
    if (form.includes('曲')) return '曲';
    return '诗';
  }
  const glyph = { novel: '小', script: '剧', prose: '散', thesis: '论', children: '童', news: '新', copy: '案', other: '他', fu: '赋', song: '歌' }[medium];
  return glyph || null;
}

export function bytes(n) {
  const v = Number(n) || 0;
  if (v > 1024 * 1024) return `${(v / 1024 / 1024).toFixed(1)} MB`;
  if (v > 1024) return `${Math.round(v / 1024)} KB`;
  return `${v} B`;
}

const TEXT_EXT = ['.txt', '.md', '.markdown', '.json', '.csv', '.log'];
const DOC_EXT = ['.docx', '.doc', '.pdf', '.epub', '.rtf', '.html', '.htm'];
export const ATTACH_ACCEPT = [...TEXT_EXT, ...DOC_EXT].join(',');
export const ATTACH_MAX_BYTES = 20 * 1024 * 1024;

export function validateFile(file) {
  if (!file) return { ok: false, message: '没有读取到文件' };
  const name = String(file.name || '');
  const lower = name.toLowerCase();
  const okExt = [...TEXT_EXT, ...DOC_EXT].some((ext) => lower.endsWith(ext));
  if (!okExt) return { ok: false, message: `不支持的文件类型：${name}。请上传 txt / md / docx / pdf / epub` };
  if (file.size > ATTACH_MAX_BYTES) return { ok: false, message: `${name} 超过 20 MB，请拆分后再上传` };
  if (file.size === 0) return { ok: false, message: `${name} 是空文件` };
  return { ok: true, message: '' };
}

export function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise((resolve, reject) => {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.append(ta);
    ta.select();
    try {
      document.execCommand('copy');
      resolve();
    } catch (e) {
      reject(e);
    } finally {
      ta.remove();
    }
  });
}

export function downloadUrl(url) {
  const a = document.createElement('a');
  a.href = url;
  a.rel = 'noopener';
  a.download = '';
  document.body.append(a);
  a.click();
  a.remove();
}

export function trapFocus(container) {
  const selector =
    'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';
  const onKey = (event) => {
    if (event.key !== 'Tab') return;
    const focusables = $$(selector, container).filter((el) => el.offsetParent !== null);
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };
  container.addEventListener('keydown', onKey);
  return () => container.removeEventListener('keydown', onKey);
}

export function isMac() {
  return /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
}

export function modKey() {
  return isMac() ? '⌘' : 'Ctrl';
}
