/* 全书查找替换（v5.5.x，易创对齐清单 A1，机制自研）。
 *
 * 分层（与 bot/webbench.py 契约对齐）：
 *   · 当前章（编辑器内）：查找词输入防抖即时重扫 + position/total 导航 +
 *     三级替换的「替换（当前一处）」「本章替换」——只动编辑器正文模型；
 *   · 全书（后端扫描）：「搜索全书」走 GET /api/book/find，按章分组 snippet；
 *     「全书替换」走 POST /api/book/replace，逐章由后端走既有 saveChapter 链路
 *     落盘（每章替换前自动留快照，原文可在章节历史版本中恢复）。
 *
 * 纯净域红线（铁律 0）：
 *   · 搜索与替换只作用于「正文域」——后端以 purity.chapter_text 口径扫描与替换，
 *     概要/备注等元信息小节不计入结果、绝不写入；
 *   · bench 处于「工作文件」视图（raw）时当前章视为锁定（isLocked）：
 *     本地查找/替换停用，全书替换把该章放进 skip 交给后端跳过；
 *   · 编辑器正文本身只装纯净正文（bench 装载口径），本地替换天然不新增元信息小节。
 *
 * 状态机（node 测试覆盖）：
 *   · 输入（防抖）→ 重扫当前章 matches → position/total；
 *   · 回车 / 下一处 → 从光标处起找下一处（回卷全文），reveal 定位；
 *   · 编辑器 docChanged（editor input）→ 结果标 stale + 防抖自动重扫当前章
 *     （只刷新计数，不抢编辑器光标）；全书结果列表只标过期，点「重搜全书」或
 *     替换成功后自动刷新；
 *   · 章节装载完成（yimu-editor-session begin）→ 立即重扫新章；
 *   · 大小写：默认不区分（中文场景），勾选「区分大小写」走英文精确口径。
 *
 * 样式全部在 bench-editor.css 的 .fr-* 命名空间，本文件零硬编码颜色（主题变量）。
 */

import { h } from './dom.js';
import { request, errorText } from './api.js';
import { toast, confirmDialog } from './ui.js';

/* ---------------------------------------------------------------- 纯函数（node 测试直测） */

function escapeRegExp(s) {
  return String(s || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function makeMatcher(q, caseSensitive) {
  const needle = String(q || '');
  if (!needle) return null;
  try {
    return new RegExp(escapeRegExp(needle), caseSensitive ? 'g' : 'gi');
  } catch {
    return null;
  }
}

/* 当前章文本 → 匹配区间列表（[{start, end}]；不区分大小写默认）。 */
function findMatches(text, q, caseSensitive) {
  const rx = makeMatcher(q, caseSensitive);
  if (!rx) return [];
  const out = [];
  for (const m of String(text || '').matchAll(rx)) {
    if (m[0]) out.push({ start: m.index, end: m.index + m[0].length });
  }
  return out;
}

/* 全量替换：replacer 用函数形态，避免 replacement 里的 $&/$1 被二次解释。 */
function replaceAllMatches(text, q, replacement, caseSensitive) {
  const rx = makeMatcher(q, caseSensitive);
  if (!rx) return String(text || '');
  return String(text || '').replace(rx, () => String(replacement || ''));
}

/* 当前一处替换前的原文校验：区间内容仍与查找词一致（大小写口径一致）才动手，
   变了就拒绝（与编辑器 replaceSelectionChecked 同一防线）。 */
function matchStillEqual(text, m, q, caseSensitive) {
  const seg = String(text || '').slice(m.start, m.end);
  if (caseSensitive) return seg === q;
  return seg.toLowerCase() === String(q || '').toLowerCase();
}

/* 全书替换确认文案（导出给测试钉红线：必须注明原文可从章节历史版本恢复）。 */
function bookReplaceConfirmText(q, replacement, total, skipped = []) {
  const lines = [
    `把全书正文里的「${q}」替换为「${replacement === '' ? '（空，即删除）' : replacement}」，共 ${total} 处。`,
    '替换只作用于正文（章题+章首引子+正文），概要与备注等元信息小节一个字都不会动。',
    '每章替换前会自动留一份快照，原文可在章节历史版本中恢复。'
  ];
  if (skipped.length) {
    lines.push(`第 ${skipped.join('、')} 章处于「工作文件」视图将被跳过；切回正文视图后可单独替换。`);
  }
  return lines.join('\n');
}

/* ---------------------------------------------------------------- 浮层 */

export function mountFindReplace(options = {}) {
  const {
    editor,
    getContext = () => ({}),
    isLocked = () => false,
    flushSave = null,
    onGotoChapter = null,
    onChapterReplaced = null,
    confirm = confirmDialog,
    debounceMs = 220
  } = options;

  /* ---- 状态 ---- */
  let q = '';
  let repl = '';
  let caseSensitive = false;
  let matches = [];
  let pos = 0;              // 当前处下标（0-based；position 显示 pos+1/total）
  let stale = false;        // 编辑器 docChanged → 结果过期（自动重扫当前章后复位）
  let bookResults = null;   // /api/book/find 返回值（全书分组）
  let bookStale = false;    // 全书结果过期（编辑器变化/替换成功后）
  let bookBusy = false;
  let replaceBusy = false;
  let destroyed = false;

  let rescanTimer = 0;

  const locked = () => {
    try { return !!isLocked(); } catch { return false; }
  };
  const ctxOf = () => {
    try { return getContext() || {}; } catch { return {}; }
  };

  /* ---- DOM（样式在 bench-editor.css 的 .fr-* 命名空间）---- */
  const panel = h('div', { class: 'fr-panel', role: 'dialog', 'aria-label': '全书查找替换', hidden: true });
  const staleBadge = h('span', { class: 'fr-stale', text: '结果已过期 · 重扫中', hidden: true });
  const qInput = h('input', {
    class: 'fr-input', type: 'text', placeholder: '查找正文里的词…',
    'aria-label': '查找词', autocomplete: 'off', spellcheck: 'false'
  });
  const navEl = h('span', { class: 'fr-nav', text: '' });
  const replInput = h('input', {
    class: 'fr-input', type: 'text', placeholder: '替换为（留空 = 删除）',
    'aria-label': '替换为', autocomplete: 'off', spellcheck: 'false'
  });
  const caseChk = h('input', { type: 'checkbox', 'aria-label': '区分大小写' });
  const resultsEl = h('div', { class: 'fr-results', hidden: true });
  const bookSummaryEl = h('div', { class: 'fr-summary', hidden: true });
  const rescanBookBtn = h('button', {
    class: 'btn btn-sm', type: 'button', text: '重搜全书', hidden: true,
    title: '编辑器内容已变化，重新扫描全书',
    on: { click: () => { searchBook(); } }
  });
  const hintEl = h('span', { class: 'fr-hint', text: '' });

  function flashHint(msg) {
    hintEl.textContent = msg || '';
    if (msg) setTimeout(() => { if (hintEl.textContent === msg) hintEl.textContent = ''; }, 4000);
  }

  const btnOne = h('button', {
    class: 'btn btn-sm btn-primary', type: 'button', text: '替换',
    title: '替换当前一处',
    on: { click: () => { replaceOne(); } }
  });
  const btnChapter = h('button', {
    class: 'btn btn-sm', type: 'button', text: '本章替换',
    title: '替换当前章全部匹配（走编辑器落盘链路，自动留版本）',
    on: { click: () => { replaceChapter(); } }
  });
  const btnBook = h('button', {
    class: 'btn btn-sm', type: 'button', text: '全书替换',
    title: '后端逐章替换（走 saveChapter 链路，替换前自动留快照，可从历史版本恢复）',
    on: { click: () => { replaceBook().catch(() => {}); } }
  });
  const btnSearchBook = h('button', {
    class: 'btn btn-sm', type: 'button', text: '搜索全书',
    title: '扫描全书正文（按章分组结果，不碰概要/备注）',
    on: { click: () => { searchBook(); } }
  });

  const head = h('div', { class: 'fr-head' },
    h('span', { class: 'fr-title', text: '查找替换' }),
    staleBadge,
    h('span', { style: { flex: '1' } }),
    h('button', {
      class: 'fr-x', type: 'button', 'aria-label': '关闭查找替换',
      title: '关闭', text: '×',
      on: { click: () => close() }
    })
  );

  const caseLabel = h('label', { class: 'fr-case', title: '默认不区分大小写（中文场景）；勾选后英文精确匹配' },
    caseChk, h('span', { text: '区分大小写' }));

  panel.append(
    head,
    h('div', { class: 'fr-row' }, qInput, caseLabel, navEl),
    h('div', { class: 'fr-row' }, replInput),
    h('div', { class: 'fr-row fr-acts' }, btnOne, btnChapter, btnBook),
    h('div', { class: 'fr-row fr-tools' }, btnSearchBook, rescanBookBtn, hintEl),
    bookSummaryEl,
    resultsEl
  );
  document.body.append(panel);

  /* ---- 绘制 ---- */

  function paintNav() {
    if (locked()) {
      navEl.textContent = '正文视图锁定';
      navEl.classList.add('is-locked');
      return;
    }
    navEl.classList.remove('is-locked');
    navEl.textContent = q && matches.length ? `${Math.min(pos + 1, matches.length)}/${matches.length}`
      : q ? '0/0' : '';
  }

  function paintStale() {
    staleBadge.hidden = !stale;
  }

  function paintBookSummary() {
    bookSummaryEl.hidden = !(bookResults || bookStale);
    bookSummaryEl.textContent = '';
    if (bookStale && bookResults) {
      bookSummaryEl.append(h('span', { class: 'fr-summary-stale', text: '全书结果已过期（编辑器内容已变化）' }));
      return;
    }
    if (bookResults) {
      bookSummaryEl.append(h('span', {
        text: bookResults.total
          ? `全书共 ${bookResults.total} 处 · ${bookResults.chapters} 章`
          : '全书正文里没有找到（概要/备注不算）'
      }));
    }
  }

  function paintResults() {
    paintBookSummary();
    rescanBookBtn.hidden = !(bookResults && bookStale);
    resultsEl.hidden = !(bookResults && bookResults.groups && bookResults.groups.length);
    resultsEl.textContent = '';
    if (!bookResults || !bookResults.groups) return;
    const curN = Number(ctxOf().n) || 0;
    for (const g of bookResults.groups) {
      const isCur = g.n === curN;
      const groupEl = h('div', { class: `fr-group${isCur ? ' is-cur' : ''}` });
      groupEl.append(h('button', {
        class: 'fr-group-title', type: 'button',
        title: isCur ? '当前章（点击定位当前处）' : '跳到这一章',
        text: `第 ${g.n} 章 ${g.title || ''} · ${g.count} 处`,
        on: {
          click: () => {
            if (isCur) {
              if (matches.length) revealCurrent();
              else rescan({ reveal: true });
            } else if (typeof onGotoChapter === 'function') {
              try { onGotoChapter(g.n); } catch { /* 跳转失败静默 */ }
            }
          }
        }
      }));
      for (const s of (g.snippets || [])) {
        groupEl.append(h('div', { class: 'fr-snippet' },
          h('span', { text: s.before || '' }),
          h('mark', { class: 'fr-hit', text: s.match || '' }),
          h('span', { text: s.after || '' })
        ));
      }
      resultsEl.append(groupEl);
    }
  }

  /* ---- 当前章扫描 / 导航 ---- */

  function revealCurrent() {
    const m = matches[pos];
    if (!m) return;
    try { editor.revealOffset(m.start); } catch { /* 定位失败静默 */ }
  }

  function rescan(opts = {}) {
    if (destroyed) return;
    if (locked()) {
      matches = [];
      stale = false;
      paintStale();
      paintNav();
      return;
    }
    const text = editor.getText();
    matches = findMatches(text, q, caseSensitive);
    if (pos >= matches.length) pos = 0;
    stale = false;
    paintStale();
    paintNav();
    if (opts.reveal && matches.length) {
      if (opts.fromCaret) {
        const caret = typeof editor.getCaretOffset === 'function' ? editor.getCaretOffset() : -1;
        const idx = matches.findIndex((m) => m.start > caret);
        pos = idx >= 0 ? idx : 0;
        paintNav();
      }
      revealCurrent();
    }
  }

  function scheduleRescan() {
    clearTimeout(rescanTimer);
    rescanTimer = setTimeout(() => { rescan(); }, debounceMs);
  }

  /* 编辑器 docChanged：标 stale + 防抖重扫当前章（不抢光标）。 */
  function onDocInput() {
    if (destroyed || !panel || panel.hidden) return;
    stale = true;
    paintStale();
    if (bookResults) { bookStale = true; paintResults(); }
    scheduleRescan();
  }

  /* 章节装载完成（bench 会话契约 begin）：新章内容就位 → 立即重扫。 */
  function onSessionEvent(e) {
    const d = e && e.detail;
    if (!d || d.key !== ctxOf().key) return;
    if (d.phase === 'begin') {
      clearTimeout(rescanTimer);
      rescan();
    }
  }
  document.addEventListener('yimu-editor-session', onSessionEvent);
  if (editor && typeof editor.setInputListener === 'function') {
    editor.setInputListener(onDocInput);
  }

  /* ---- 三级替换 ---- */

  function guardLocked() {
    if (!locked()) return false;
    toast('当前是「工作文件」视图（含概要/备注）。切回「正文」视图后再查找替换，避免把替换写进元信息。', { tone: 'warn', duration: 4200 });
    return true;
  }

  function afterLocalChange() {
    if (typeof flushSave === 'function') {
      try { flushSave(); } catch { /* 落盘失败由 bench 保存状态提示 */ }
    }
  }

  /* 替换（当前一处） */
  function replaceOne() {
    if (guardLocked()) return;
    if (!q) { flashHint('先输入查找词'); return; }
    if (!matches.length) { flashHint('当前章没有匹配'); return; }
    const m = matches[pos] || matches[0];
    const text = editor.getText();
    if (!matchStillEqual(text, m, q, caseSensitive)) {
      /* 区间内容已变（编辑器改动还没重扫完）：先重扫再试一次 */
      rescan();
      const m2 = matches[pos];
      if (!m2 || !matchStillEqual(editor.getText(), m2, q, caseSensitive)) {
        flashHint('该处内容已变化，已重新定位，请再试一次');
        return;
      }
      const cur = m2;
      editor.replaceRange(cur.start, cur.end, repl);
      afterLocalChange();
      rescan();
      return;
    }
    editor.replaceRange(m.start, m.end, repl);
    afterLocalChange();
    rescan();   // pos 原地不动 → 指向替换文本之后的下一处
  }

  /* 本章替换 */
  function replaceChapter() {
    if (guardLocked()) return;
    if (!q) { flashHint('先输入查找词'); return; }
    const text = editor.getText();
    const n = findMatches(text, q, caseSensitive).length;
    if (!n) { flashHint('当前章没有匹配'); return; }
    editor.setText(replaceAllMatches(text, q, repl, caseSensitive));   // 非 silent → 落盘链路
    afterLocalChange();
    rescan();
    flashHint(`本章已替换 ${n} 处（可在版本历史里恢复）`);
  }

  /* 全书替换（后端 saveChapter 链路） */
  async function replaceBook() {
    if (replaceBusy) return;
    const c = ctxOf();
    if (!c.key) { flashHint('没有作品上下文'); return; }
    if (!q) { flashHint('先输入查找词（建议先「搜索全书」核对结果）'); return; }
    const skip = locked() && c.n ? [Number(c.n)] : [];
    const total = bookResults && !bookStale ? Number(bookResults.total) || 0 : 0;
    let ok = true;
    try {
      ok = await confirm({
        title: '全书替换？',
        message: bookReplaceConfirmText(q, repl, total, skip),
        confirmText: '开始替换',
        cancelText: '再想想',
        danger: true
      });
    } catch { ok = true; }
    if (!ok) return;
    if (typeof flushSave === 'function') {
      try { await flushSave(); } catch { /* 保存失败不阻断替换（后端按文件内容处理） */ }
    }
    replaceBusy = true;
    btnBook.disabled = true;
    try {
      const r = await request('/api/book/replace', {
        method: 'POST',
        body: {
          key: c.key, q, replacement: repl, scope: 'book',
          ...(skip.length ? { skip } : {}),
          ...(caseSensitive ? { case_sensitive: true } : {})
        },
        timeout: 120000
      });
      const changed = (r && Array.isArray(r.changed)) ? r.changed : [];
      const replacedCount = changed.reduce((acc, x) => acc + (Number(x.count) || 0), 0);
      const skippedCount = (r && Array.isArray(r.skipped)) ? r.skipped.length : 0;
      if (!changed.length) {
        toast('全书正文里没有需要替换的匹配（概要/备注不计入）', { duration: 3600 });
      } else {
        toast(`已替换 ${replacedCount} 处 · ${changed.length} 章${skippedCount ? ` · 跳过 ${skippedCount} 章` : ''}（原文可在章节历史版本中恢复）`,
          { tone: 'success', duration: 5200 });
      }
      /* 当前章被替换 → 编辑器内容同步刷新；全书结果立即重扫 */
      const curN = Number(c.n) || 0;
      const touched = changed.some((x) => Number(x.n) === curN);
      if (touched && typeof onChapterReplaced === 'function') {
        try { onChapterReplaced(); } catch { /* 重载失败静默 */ }
      } else {
        rescan();
      }
      searchBook();
      return r;
    } catch (err) {
      toast(errorText(err, '全书替换失败'), { tone: 'danger', duration: 4200 });
      return null;
    } finally {
      replaceBusy = false;
      btnBook.disabled = false;
    }
  }

  /* ---- 全书搜索 ---- */

  async function searchBook() {
    if (bookBusy) return;
    const c = ctxOf();
    if (!c.key) { flashHint('没有作品上下文'); return; }
    if (!q.trim()) { flashHint('先输入查找词'); return; }
    bookBusy = true;
    btnSearchBook.disabled = true;
    try {
      const r = await request('/api/book/find', {
        query: { key: c.key, q, ...(caseSensitive ? { case_sensitive: 1 } : {}) },
        timeout: 60000
      });
      bookResults = r && typeof r === 'object' ? r : null;
      bookStale = false;
      paintResults();
    } catch (err) {
      toast(errorText(err, '全书搜索失败'), { tone: 'danger', duration: 4200 });
    } finally {
      bookBusy = false;
      btnSearchBook.disabled = false;
    }
  }

  /* ---- 输入交互 ---- */

  qInput.addEventListener('input', () => {
    q = qInput.value;
    pos = 0;
    stale = false;
    paintStale();
    scheduleRescan();      // 输入防抖即时重扫当前章
    if (bookResults) { bookStale = true; paintResults(); }
  });
  qInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      rescan({ reveal: true, fromCaret: true });   // 回车 = 跳下一个（回卷全文）
    } else if (event.key === 'Escape') {
      event.preventDefault();
      close();
    }
  });
  replInput.addEventListener('input', () => {
    repl = replInput.value;
  });
  caseChk.addEventListener('change', () => {
    caseSensitive = caseChk.checked;
    if (bookResults) { bookStale = true; paintResults(); }
    rescan();
  });

  /* ---- 开合 / 拖拽 ---- */

  let dragCleanup = null;

  function clamp(v, min, max) {
    return Math.max(min, Math.min(max, v));
  }

  function place(x, y) {
    const w = panel.offsetWidth || 340;
    const hgt = panel.offsetHeight || 220;
    const vw = (typeof window !== 'undefined' && window.innerWidth) || 1200;
    const vh = (typeof window !== 'undefined' && window.innerHeight) || 800;
    panel.style.left = `${clamp(x, 8, Math.max(8, vw - w - 8))}px`;
    panel.style.top = `${clamp(y, 8, Math.max(8, vh - hgt - 8))}px`;
  }

  function startDrag(event) {
    if (event.button !== 0) return;
    const rect = panel.getBoundingClientRect();
    const dx = event.clientX - rect.left;
    const dy = event.clientY - rect.top;
    const move = (ev) => { place(ev.clientX - dx, ev.clientY - dy); };
    const up = () => {
      document.removeEventListener('pointermove', move, true);
      document.removeEventListener('pointerup', up, true);
      dragCleanup = null;
    };
    document.addEventListener('pointermove', move, true);
    document.addEventListener('pointerup', up, true);
    dragCleanup = up;
    event.preventDefault();
  }
  head.addEventListener('pointerdown', startDrag);

  function open() {
    if (destroyed) return;
    panel.hidden = false;
    const vw = (typeof window !== 'undefined' && window.innerWidth) || 1200;
    const w = panel.offsetWidth || 340;
    place(Math.max(8, vw - w - 28), 64);
    rescan();
    try { qInput.focus(); } catch { /* 聚焦失败静默 */ }
  }

  function close() {
    panel.hidden = true;
  }

  function isOpen() {
    return !panel.hidden;
  }

  function toggle() {
    if (isOpen()) close();
    else open();
  }

  function destroy() {
    destroyed = true;
    clearTimeout(rescanTimer);
    if (dragCleanup) { try { dragCleanup(); } catch { /* 已清理 */ } dragCleanup = null; }
    document.removeEventListener('yimu-editor-session', onSessionEvent);
    if (editor && typeof editor.setInputListener === 'function') {
      editor.setInputListener(null);
    }
    panel.remove();
  }

  return {
    el: panel,
    open, close, toggle, isOpen, destroy,
    rescan: () => rescan(),
    searchBook,
    /* 测试/诊断视图 */
    debug: () => ({ q, repl, caseSensitive, matches: matches.slice(), pos, stale, bookStale, locked: locked() })
  };
}

/* 供测试引用的纯函数集中导出（不进入 bench 主链路）。 */
export const findReplaceInternals = {
  findMatches, replaceAllMatches, matchStillEqual, bookReplaceConfirmText, makeMatcher
};
