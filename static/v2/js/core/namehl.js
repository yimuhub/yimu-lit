/* 编辑器内人物名高亮（v5.6.0，CSS Custom Highlight API，纯渲染层）。
 *
 * 定位（对齐清单 backlog「人物名高亮管理」，全部自研）：
 * · 用 CSS Custom Highlight API（CSS.highlights + ::highlight(character-name)）
 *   对编辑器全文按人物名做 Range 标记——比幽灵字的虚拟节点更干净：
 *   零 DOM 文本改动、零正文模型污染，复制/导出/落盘路径零接触；
 * · 浏览器不支持（Chrome/Edge < 105、Safari < 17.0 等）→ isHighlightSupported()
 *   返回 false，注册路径整体跳过（静默降级），面板仍可管理词表。
 *
 * 名单来源（无新增端点）：
 * · GET /api/canvas/graph?key= 的 characters.nodes[].label（含主名；别名扩展不做 v1）；
 * · 用户自定义词：localStorage `ew-name-hl:<bookKey>`（JSON 数组）；
 * · 每个词可单独开关：localStorage `ew-name-hl-off:<bookKey>`（关闭词名单）；
 * · 总开关：localStorage `ew-name-hl-enabled`（'1'/'0'）。
 *
 * 重建时机（复用既有钩子，绝不打断输入）：
 * · bench onChange → notifyText（与防抖保存链路同一钩子）→ 120ms 防抖重建；
 * · 拼音组词期间（ev.composing）跳过，compositionend 后 onChange 自然补上；
 * · yimu-editor-session begin（切章装载完成）→ 重算 + 名单缓存刷新；
 * · Range 构建纯算（按段落 indexOf 定位，跨段不标），无重排、无 DOM 写入。
 *
 * 铁律：任何失败只静默降级（清空注册即回到无高亮），绝不影响写作主流程。
 */

import { h, icon, debounce } from './dom.js';
import { request } from './api.js';

/* ------------------------------------------------------------ 常量（测试钉契约） */

export const NAMEHL_HL_NAME = 'character-name';
export const NAMEHL_MASTER_KEY = 'ew-name-hl-enabled';
export const NAMEHL_STORE_PREFIX = 'ew-name-hl:';
export const NAMEHL_OFF_PREFIX = 'ew-name-hl-off:';
export const NAMEHL_DEBOUNCE_MS = 120;
export const NAMEHL_PANEL_MAX = 30;
export const NAMEHL_MAX_WORD_LEN = 20;
export const NAMEHL_MAX_NODES = 300;
export const NAMEHL_MAX_RANGES = 2000;

/* ------------------------------------------------------------ 纯函数（node 测试直测） */

/* /api/canvas/graph 聚合 payload → 人物主名数组（label 优先、去重、滤非法、封顶）。 */
export function parseGraphNames(payload) {
  const chars = payload && typeof payload === 'object' ? payload.characters : null;
  const nodes = chars && Array.isArray(chars.nodes) ? chars.nodes : [];
  const out = [];
  const seen = new Set();
  for (const nd of nodes) {
    const raw = nd && typeof nd === 'object' ? String(nd.label != null ? nd.label : (nd.id || '')) : String(nd || '');
    const name = raw.trim().slice(0, NAMEHL_MAX_WORD_LEN);
    if (!name || name.includes('\n') || seen.has(name)) continue;
    seen.add(name);
    out.push(name);
    if (out.length >= NAMEHL_MAX_NODES) break;
  }
  return out;
}

/* 全文 → 段落 span（[{text, start}]）。分隔符按 \n{2,} 口径（与编辑器 setText 一致，
   兼容多余空行）；start 为该段首字符的全局偏移。 */
export function paragraphSpans(text) {
  const src = String(text || '');
  const spans = [];
  let start = 0;
  const rx = /\n{2,}/g;
  let m;
  while ((m = rx.exec(src))) {
    spans.push({ text: src.slice(start, m.index), start });
    start = m.index + m[0].length;
  }
  spans.push({ text: src.slice(start), start });
  return spans;
}

function _overlapAny(occ, s, e) {
  /* 已占用有序区间二分查重叠。 */
  let lo = 0;
  let hi = occ.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const iv = occ[mid];
    if (iv[1] <= s) lo = mid + 1;
    else if (iv[0] >= e) hi = mid - 1;
    else return true;
  }
  return false;
}

function _insertInterval(occ, s, e) {
  let lo = 0;
  let hi = occ.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (occ[mid][0] < s) lo = mid + 1;
    else hi = mid;
  }
  occ.splice(lo, 0, [s, e]);
}

function _byStart(a, b) {
  return a.start - b.start || (b.end - b.start) - (a.end - a.start);
}

/* 全文 + 生效词表 → 高亮区间 [{start, end, name}]（纯算，无 DOM）。
 * · 长名优先（「张三丰」不吃掉给「张三」的区间）：名单按长度降序占领；
 * · 已被长名占用的区间短名跳过（重叠不标、绝不双重标记）；
 * · 按段落 indexOf 定位——词不含换行，天然跨段不标；
 * · 区间总量 NAMEHL_MAX_RANGES 封顶（性能安全阀）。
 */
export function computeNameRanges(text, names, { maxRanges = NAMEHL_MAX_RANGES } = {}) {
  const src = String(text || '');
  if (!src || !Array.isArray(names) || !names.length) return [];
  const seen = new Set();
  const list = [];
  names.forEach((n, i) => {
    const w = String(n == null ? '' : n).trim();
    if (!w || w.includes('\n') || w.length > NAMEHL_MAX_WORD_LEN || seen.has(w)) return;
    seen.add(w);
    list.push({ w, i });
  });
  if (!list.length) return [];
  list.sort((a, b) => b.w.length - a.w.length || a.i - b.i);
  const occ = [];
  const out = [];
  for (const span of paragraphSpans(src)) {
    const ptext = span.text;
    if (!ptext) continue;
    for (const { w } of list) {
      if (w.length > ptext.length) continue;
      let from = 0;
      let at = ptext.indexOf(w, from);
      while (at >= 0) {
        const s = span.start + at;
        const e = s + w.length;
        if (!_overlapAny(occ, s, e)) {
          out.push({ start: s, end: e, name: w });
          _insertInterval(occ, s, e);
          if (out.length >= maxRanges) return out.sort(_byStart);
        }
        from = at + w.length;
        at = ptext.indexOf(w, from);
      }
    }
  }
  return out.sort(_byStart);
}

/* 面板清单过滤：搜索词包含匹配（不分大小写）+ 显示条数封顶。 */
export function filterNameList(names, query, limit = NAMEHL_PANEL_MAX) {
  const src = (Array.isArray(names) ? names : []).filter((n) => typeof n === 'string' && n.trim());
  const q = String(query || '').trim().toLowerCase();
  const hit = q ? src.filter((n) => n.toLowerCase().includes(q)) : src;
  return hit.slice(0, Math.max(1, Number(limit) || NAMEHL_PANEL_MAX));
}

/* ------------------------------------------------------------ 存取（损坏 JSON 一律回默认） */

function _storage(storage) {
  if (storage) return storage;
  return typeof localStorage !== 'undefined' ? localStorage : null;
}

function _readJsonArray(st, key) {
  try {
    const raw = JSON.parse(st.getItem(key) || '[]');
    return Array.isArray(raw) ? raw.map(String) : [];
  } catch {
    return [];
  }
}

export function loadCustomWords(bookKey, storage) {
  const st = _storage(storage);
  if (!st) return [];
  return _readJsonArray(st, NAMEHL_STORE_PREFIX + String(bookKey || ''));
}

export function saveCustomWords(bookKey, words, storage) {
  const st = _storage(storage);
  if (!st) return;
  const seen = new Set();
  const clean = [];
  for (const w of Array.isArray(words) ? words : []) {
    const word = String(w || '').trim().slice(0, NAMEHL_MAX_WORD_LEN);
    if (!word || word.includes('\n') || seen.has(word)) continue;
    seen.add(word);
    clean.push(word);
  }
  try { st.setItem(NAMEHL_STORE_PREFIX + String(bookKey || ''), JSON.stringify(clean)); } catch { /* 隐私模式 */ }
}

export function loadNameOff(bookKey, storage) {
  const st = _storage(storage);
  if (!st) return [];
  return _readJsonArray(st, NAMEHL_OFF_PREFIX + String(bookKey || ''));
}

export function saveNameOff(bookKey, off, storage) {
  const st = _storage(storage);
  if (!st) return;
  try { st.setItem(NAMEHL_OFF_PREFIX + String(bookKey || ''), JSON.stringify(Array.from(new Set((off || []).map(String))))); } catch { /* 隐私模式 */ }
}

export function loadMasterEnabled(storage) {
  const st = _storage(storage);
  if (!st) return true;                       /* 默认开：装上即用，面板可关 */
  const raw = st.getItem(NAMEHL_MASTER_KEY);
  if (raw == null || raw === '') return true;
  return raw === '1';
}

export function saveMasterEnabled(on, storage) {
  const st = _storage(storage);
  if (!st) return;
  try { st.setItem(NAMEHL_MASTER_KEY, on ? '1' : '0'); } catch { /* 隐私模式 */ }
}

/* ------------------------------------------------------------ 能力检测 / 名单缓存 */

/* CSS Custom Highlight API 能力（调用时检测，方便测试注桩）：
   Highlight 构造器走 globalThis 属性访问（老 eslint browser globals 可能没有它）。 */
export function isHighlightSupported() {
  try {
    const hl = typeof globalThis !== 'undefined' ? globalThis.Highlight : undefined;
    if (typeof hl !== 'function') return false;
    if (typeof CSS === 'undefined' || !CSS.highlights || typeof CSS.highlights.set !== 'function') return false;
    if (typeof document === 'undefined' || typeof document.createRange !== 'function') return false;
    const probe = document.createRange();
    return typeof probe.setStart === 'function';
  } catch {
    return false;
  }
}

/* 名单缓存（模块级，跨挂载复用）：key → 名单 promise；成功缓存、失败不缓存
   （下次切章/刷新可重试）。 */
const graphCache = new Map();

export function clearNameCache() {
  graphCache.clear();
}

async function fetchNames(bookKey) {
  const bk = String(bookKey || '');
  if (!bk) return [];
  if (!graphCache.has(bk)) {
    const p = (async () => {
      try {
        const res = await request('/api/canvas/graph', { query: { key: bk }, timeout: 30000 });
        const list = parseGraphNames(res);
        graphCache.set(bk, Promise.resolve(list));
        return list;
      } catch (err) {
        graphCache.delete(bk);   /* 失败不缓存：静默降级为空名单，可重试 */
        return [];
      }
    })();
    graphCache.set(bk, p);
  }
  try {
    return await graphCache.get(bk);
  } catch {
    return [];
  }
}

/* ------------------------------------------------------------ 段内 DOM 定位（只读，零写入） */

/* 与 core/editor.js 的 isGhostEl 同口径：幽灵字虚拟节点不占模型偏移，跳过。 */
function _isGhostLike(node) {
  return !!(node && node.nodeType === 1 &&
    (node.classList && (node.classList.contains('ghost-text') || node.classList.contains('ghost-key'))));
}

/* 段容器文本 → 模型口径文本（BR→\n、幽灵字跳过、\u00a0→空格；与 editor.getText 一致）。 */
function paraDomText(p) {
  let out = '';
  const walk = (node) => {
    if (_isGhostLike(node)) return;
    if (node.nodeType === 3) { out += node.nodeValue || ''; return; }
    if (node.nodeType !== 1) return;
    if (node.tagName === 'BR') { out += '\n'; return; }
    for (const c of node.childNodes) walk(c);
  };
  for (const c of p.childNodes) walk(c);
  return out.replace(/\u00a0/g, ' ');
}

/* 段内模型偏移 → (文本节点, offset) 落点（幽灵字跳过；只读定位，绝不改 DOM）。 */
function locatePoint(p, want) {
  let rest = Math.max(0, want);
  const kids = p.childNodes;
  for (let i = 0; i < kids.length; i += 1) {
    const c = kids[i];
    if (_isGhostLike(c)) continue;
    if (c.nodeType === 3) {
      const len = (c.nodeValue || '').length;
      if (rest <= len) return { node: c, offset: rest };
      rest -= len;
    } else if (c.nodeType === 1 && c.tagName === 'BR') {
      if (rest === 0) return { node: p, offset: i };
      if (rest === 1) {
        for (let k = i + 1; k < kids.length; k += 1) {
          const nk = kids[k];
          if (_isGhostLike(nk)) continue;
          if (nk.nodeType === 3) return { node: nk, offset: 0 };
          return { node: p, offset: k };
        }
        return { node: p, offset: kids.length };
      }
      rest -= 1;
    }
  }
  return { node: p, offset: kids.length };
}

/* ------------------------------------------------------------ 挂载 */

export function mountNameHighlight({
  editor,
  getKey = null,
  storage = null,
  onHint = null
} = {}) {
  const st = _storage(storage);
  let destroyed = false;
  let supported = isHighlightSupported();
  let masterOn = loadMasterEnabled(st);
  let names = [];                 /* 画布人物主名 */
  let customWords = [];           /* 用户自定义词 */
  let offSet = new Set();         /* 单独关闭的词 */
  let loadedKey = null;
  let lastRangeCount = 0;

  const activeKey = () => {
    try { return String((typeof getKey === 'function' && getKey()) || ''); } catch { return ''; }
  };

  const scheduleRebuild = debounce(() => rebuildNow(), NAMEHL_DEBOUNCE_MS);

  /* ---------------------------------------------------------- 状态装载 */

  function syncState(bookKey) {
    customWords = loadCustomWords(bookKey, st);
    offSet = new Set(loadNameOff(bookKey, st));
  }

  async function ensureNames(bookKey) {
    const list = await fetchNames(bookKey);
    if (!destroyed && String(bookKey) === String(activeKey())) {
      names = Array.isArray(list) ? list : [];
      loadedKey = String(bookKey);
    }
    return names;
  }

  /* 生效词表 = 画布人物 + 自定义词 − 单独关闭；保序去重。 */
  function activeNames() {
    const out = [];
    const seen = new Set();
    for (const w of [...names, ...customWords]) {
      const k = String(w || '').trim();
      if (!k || seen.has(k) || offSet.has(k)) continue;
      seen.add(k);
      out.push(k);
    }
    return out;
  }

  /* ---------------------------------------------------------- 注册 / 清除 */

  function clearHighlight() {
    try {
      if (supported && typeof CSS !== 'undefined' && CSS.highlights) {
        CSS.highlights.delete(NAMEHL_HL_NAME);
      }
    } catch { /* 附加能力，失败静默 */ }
    lastRangeCount = 0;
  }

  /* 模型区间 → DOM Range（只读定位；构建纯算无重排，注册交给 CSS.highlights）。 */
  function buildDomRanges(ranges) {
    const root = editor && editor.el;
    if (!root || !Array.isArray(ranges) || !ranges.length) return [];
    const paras = Array.from(root.querySelectorAll('p.edp'));
    if (!paras.length) return [];
    const spans = [];
    let acc = 0;
    for (const p of paras) {
      const t = paraDomText(p);
      spans.push({ p, text: t, start: acc, len: t.length });
      acc += t.length + 2;
    }
    const out = [];
    for (const r of ranges) {
      let target = null;
      for (const sp of spans) {
        if (r.start >= sp.start && r.start <= sp.start + sp.len) { target = sp; break; }
      }
      if (!target) continue;
      const a = r.start - target.start;
      let b = r.end - target.start;
      if (b > target.len) b = target.len;
      if (b <= a) continue;
      try {
        const pa = locatePoint(target.p, a);
        const pb = locatePoint(target.p, b);
        if (!pa || !pb) continue;
        const range = document.createRange();
        range.setStart(pa.node, pa.offset);
        range.setEnd(pb.node, pb.offset);
        if (range.collapsed) continue;
        out.push(range);
      } catch { /* 单条定位失败只丢这一条 */ }
    }
    return out;
  }

  function applyHighlight(ranges) {
    try {
      if (!supported) return;
      const hl = typeof globalThis !== 'undefined' ? globalThis.Highlight : undefined;
      if (typeof hl !== 'function') return;
      if (!ranges.length) { clearHighlight(); return; }
      const domRanges = buildDomRanges(ranges);
      if (!domRanges.length) { clearHighlight(); return; }
      const instance = new hl(...domRanges);
      CSS.highlights.set(NAMEHL_HL_NAME, instance);
      lastRangeCount = domRanges.length;
    } catch { /* 注册失败静默降级 */ }
  }

  function rebuildNow() {
    if (destroyed) return;
    try {
      const text = editor && typeof editor.getText === 'function' ? editor.getText() : '';
      if (!supported || !masterOn || !text) { clearHighlight(); return; }
      const active = activeNames();
      if (!active.length) { clearHighlight(); return; }
      applyHighlight(computeNameRanges(text, active));
    } catch { /* 重建失败静默，绝不影响输入 */ }
  }

  /* ---------------------------------------------------------- 对外动作 */

  function notifyText(_text, ev) {
    if (destroyed) return;
    if (ev && ev.composing) return;   /* 拼音组词期间跳过（compositionend 后 onChange 会补） */
    scheduleRebuild();
  }

  function refresh() {
    if (destroyed) return;
    const bk = activeKey();
    if (bk !== loadedKey) {
      syncState(bk);
      ensureNames(bk).then(() => { if (!destroyed) rebuildNow(); }).catch(() => {});
    }
    rebuildNow();
  }

  function setEnabled(on) {
    masterOn = !!on;
    saveMasterEnabled(masterOn, st);
    rebuildNow();
    if (panelEl) paintPanel();
  }

  function addCustomWord(word) {
    const w = String(word || '').trim().slice(0, NAMEHL_MAX_WORD_LEN);
    if (!w || w.includes('\n')) return false;
    if (customWords.includes(w) || names.includes(w)) return false;
    customWords.push(w);
    saveCustomWords(activeKey(), customWords, st);
    rebuildNow();
    return true;
  }

  function removeCustomWord(word) {
    const w = String(word || '').trim();
    const at = customWords.indexOf(w);
    if (at < 0) return false;
    customWords.splice(at, 1);
    offSet.delete(w);
    saveCustomWords(activeKey(), customWords, st);
    saveNameOff(activeKey(), [...offSet], st);
    rebuildNow();
    return true;
  }

  function setWordEnabled(word, on) {
    const w = String(word || '').trim();
    if (!w) return;
    if (on) offSet.delete(w);
    else offSet.add(w);
    saveNameOff(activeKey(), [...offSet], st);
    rebuildNow();
    if (panelEl) paintPanel();
  }

  function revealName(word) {
    try {
      const text = editor && typeof editor.getText === 'function' ? editor.getText() : '';
      const at = String(text || '').indexOf(String(word || ''));
      if (at < 0) {
        if (typeof onHint === 'function') onHint(`本章正文没有出现「${word}」`);
        return false;
      }
      editor.revealOffset(at);
      return true;
    } catch { return false; }
  }

  /* ---------------------------------------------------------- 管理面板 */

  let panelEl = null;
  let panelOutside = null;
  let searchInput = null;
  let addInput = null;
  let masterBtn = null;
  let graphList = null;
  let customList = null;
  let graphCount = null;
  let customCount = null;

  function paintMaster() {
    if (!masterBtn) return;
    masterBtn.dataset.on = String(!!masterOn);
    masterBtn.setAttribute('aria-checked', masterOn ? 'true' : 'false');
    masterBtn.textContent = '';
    masterBtn.append(
      h('span', { class: 'nh-master-label', text: '总开关' }),
      h('span', { class: 'nh-master-state', text: masterOn ? '开' : '关' })
    );
  }

  function wordItem(word, isCustom) {
    const on = !offSet.has(word);
    return h('div', { class: 'nh-item', role: 'listitem', dataset: { name: word, on: String(on) } },
      h('button', {
        class: 'nh-item-main',
        type: 'button',
        title: '定位到第一处出现',
        text: word,
        on: { click: () => revealName(word) }
      }),
      h('button', {
        class: 'nh-toggle',
        type: 'button',
        role: 'switch',
        'aria-checked': on ? 'true' : 'false',
        'aria-label': `${on ? '关闭' : '开启'}「${word}」高亮`,
        dataset: { on: String(on) },
        on: { click: () => setWordEnabled(word, !on) }
      }),
      isCustom ? h('button', {
        class: 'nh-del',
        type: 'button',
        title: '删除自定义词',
        'aria-label': `删除自定义词「${word}」`,
        html: icon('x', 11),
        on: { click: () => { removeCustomWord(word); paintPanel(); } }
      }) : null
    );
  }

  function paintPanel() {
    if (!panelEl) return;
    paintMaster();
    const q = searchInput ? (searchInput.value || '') : '';
    const graphPool = filterNameList(names, q, NAMEHL_PANEL_MAX);
    const customPool = filterNameList(customWords, q, NAMEHL_PANEL_MAX);
    if (graphCount) graphCount.textContent = `${names.length ? `共 ${names.length} · ` : ''}显示 ${graphPool.length}`;
    if (customCount) customCount.textContent = `显示 ${customPool.length}`;
    if (graphList) {
      graphList.textContent = '';
      if (graphPool.length) {
        for (const w of graphPool) graphList.append(wordItem(w, false));
      } else {
        graphList.append(h('div', { class: 'nh-empty', text: names.length ? '没有匹配的人物' : '资料画布还没有人物数据（写完人物档案后自动带出）' }));
      }
    }
    if (customList) {
      customList.textContent = '';
      if (customPool.length) {
        for (const w of customPool) customList.append(wordItem(w, true));
      } else {
        customList.append(h('div', { class: 'nh-empty', text: customWords.length ? '没有匹配的自定义词' : '还没有自定义词（输入框回车添加）' }));
      }
    }
  }

  function buildPanel(anchor) {
    searchInput = h('input', {
      class: 'nh-search-input',
      type: 'search',
      placeholder: '搜索人物 / 词',
      'aria-label': '搜索高亮词',
      on: { input: () => paintPanel() }
    });
    addInput = h('input', {
      class: 'nh-add-input',
      type: 'text',
      placeholder: '添加自定义词，回车确认',
      maxlength: String(NAMEHL_MAX_WORD_LEN),
      'aria-label': '添加自定义词'
    });
    addInput.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      if (addCustomWord(addInput.value)) addInput.value = '';
      paintPanel();
    });
    masterBtn = h('button', {
      class: 'nh-master',
      type: 'button',
      role: 'switch',
      title: '人物名高亮总开关',
      on: { click: () => setEnabled(!masterOn) }
    });
    graphCount = h('span', { class: 'nh-count' });
    customCount = h('span', { class: 'nh-count' });
    graphList = h('div', { class: 'nh-list', role: 'list', 'aria-label': '书中人物' });
    customList = h('div', { class: 'nh-list', role: 'list', 'aria-label': '自定义词' });
    const panel = h('div', {
      class: 'namehl-panel',
      role: 'dialog',
      'aria-label': '人物名高亮管理'
    },
      h('div', { class: 'nh-head' }, masterBtn),
      supported ? null : h('div', {
        class: 'nh-unsupport',
        text: '当前浏览器不支持 CSS 高亮（需 Chrome/Edge 105+ 或 Safari 17.0+），词表仍可维护。'
      }),
      h('div', { class: 'nh-search' }, searchInput),
      h('div', { class: 'nh-section' }, h('span', { text: '书中人物' }), graphCount),
      graphList,
      h('div', { class: 'nh-section' }, h('span', { text: '自定义词' }), customCount),
      customList,
      h('div', { class: 'nh-add' }, addInput)
    );
    panel.style.position = 'fixed';
    panel.style.zIndex = '110';
    const rect = (anchor && typeof anchor.getBoundingClientRect === 'function' && anchor.getBoundingClientRect()) ||
      { right: 200, bottom: 60 };
    document.body.append(panel);
    const w = panel.offsetWidth || 264;
    panel.style.left = `${Math.max(10, Math.min(rect.right - w, (window.innerWidth || 1200) - w - 10))}px`;
    panel.style.top = `${Math.min((rect.bottom || 60) + 8, (window.innerHeight || 800) - panel.offsetHeight - 10)}px`;
    return panel;
  }

  function closePanel() {
    if (panelEl) { panelEl.remove(); panelEl = null; }
    if (panelOutside) {
      document.removeEventListener('pointerdown', panelOutside, true);
      panelOutside = null;
    }
    searchInput = addInput = masterBtn = graphList = customList = graphCount = customCount = null;
  }

  function openPanel(anchor) {
    if (destroyed) return;
    closePanel();
    panelEl = buildPanel(anchor);
    paintPanel();
    panelOutside = (event) => {
      if (panelEl && panelEl.contains(event.target)) return;
      if (anchor && anchor.contains && anchor.contains(event.target)) return;
      closePanel();
    };
    document.addEventListener('pointerdown', panelOutside, true);
  }

  /* ---------------------------------------------------------- 事件接线 */

  function onSessionEvent(e) {
    try {
      if (destroyed) return;
      const d = e && e.detail;
      if (!d || !d.key) return;
      const bk = activeKey();
      if (!bk || String(d.key) !== bk) return;
      if (d.phase === 'begin') {
        /* 切章装载完成（editor.setText 是 silent）：正文已换，重算高亮 + 名单缓存命中刷新 */
        ensureNames(bk).then(() => { if (!destroyed) rebuildNow(); }).catch(() => {});
      }
    } catch { /* 附加能力，失败静默 */ }
  }

  document.addEventListener('yimu-editor-session', onSessionEvent);

  /* ---------------------------------------------------------- 启动 */

  syncState(activeKey());
  ensureNames(activeKey()).then(() => { if (!destroyed) rebuildNow(); }).catch(() => {});

  return {
    notifyText,
    refresh,
    openPanel,
    closePanel,
    setEnabled,
    addCustomWord,
    removeCustomWord,
    setWordEnabled,
    revealName,
    isSupported: () => supported,
    isEnabled: () => masterOn,
    names: () => [...names],
    customWords: () => [...customWords],
    offList: () => [...offSet],
    activeWords: activeNames,
    debug: () => ({
      supported,
      masterOn,
      loadedKey,
      names: [...names],
      custom: [...customWords],
      off: [...offSet],
      ranges: lastRangeCount
    }),
    destroy() {
      destroyed = true;
      scheduleRebuild.cancel();
      document.removeEventListener('yimu-editor-session', onSessionEvent);
      clearHighlight();
      closePanel();
    }
  };
}
