/* 段落型编辑器核心（v5.1.0 写作台编辑器内核）。
 *
 * 设计取舍（为什么不用富文本库）：
 * · 正文数据模型是纯文本（\n\n 分段），与纯净域「章题+引子+正文」口径天然契合，
 *   导出/复制/划词替换都不需要经过富文本 JSON 转换，零口径风险；
 * · 每段一个 contenteditable <p>，浏览器自己处理段落内的光标/输入/原生撤销，
 *   本模块只管「段间结构」（回车分段、删除合并、跨段选区偏移换算）；
 * · 输入路径绝不整树重渲染：input 事件只读取当前段文本聚合模型，长章输入不卡。
 *
 * 纯文本模型约定：
 * · getText() = 各段文本用 \n\n 连接；段内软换行（Shift+Enter）保留为单 \n；
 * · setText() 按 \n{2,} 切段（多余空行折叠成一段空段），往返幂等；
 * · 全局光标偏移 = 段前文本累计长度 + 2（\n\n 分隔符），与 getText 一致。
 *
 * 改这里必须跑 tests/v51_editor_purity_test.mjs（编辑器口径 × core/purity.js 比对）。
 */

import { h } from './dom.js';

/* ---------------------------------------------------------- 段内文本提取（不依赖 innerText，jsdom 可跑） */

/* v5.2.0 幽灵字虚拟节点：.ghost-text（建议灰字）与 .ghost-key（Tab 键帽）是
   「渲染层存在、模型层不存在」的节点——提取/偏移换算一律跳过，正文模型保持纯净，
   采纳（Tab）时才真正写入模型。 */
function isGhostEl(node) {
  return !!(node && node.nodeType === 1 &&
    (node.classList && (node.classList.contains('ghost-text') || node.classList.contains('ghost-key'))));
}

function nodeText(node) {
  /* TEXT → 原值；BR → '\n'；元素 → 递归拼接（粘贴残留的 span/b 等一律摊平；
     幽灵字虚拟节点不计入正文模型）。 */
  if (isGhostEl(node)) return '';
  if (node.nodeType === 3) return node.nodeValue || '';
  if (node.nodeType !== 1) return '';
  if (node.tagName === 'BR') return '\n';
  let out = '';
  for (const child of node.childNodes) out += nodeText(child);
  return out;
}

function paraText(p) {
  return nodeText(p).replace(/\u00a0/g, ' ');
}

/* 元素内第 idx 个子节点之前的文本量（Range offset 落在容器上时用）。 */
function childPrefixLen(el, idx) {
  let n = 0;
  const kids = el.childNodes;
  for (let i = 0; i < idx && i < kids.length; i += 1) {
    const c = kids[i];
    if (c.nodeType === 3) n += (c.nodeValue || '').length;
    else if (c.nodeType === 1 && c.tagName === 'BR') n += 1;
    else n += paraText(c).length;
  }
  return n;
}

/* ------------------------------------------------------------ 光标/偏移换算 */

export function createEditor({ host, placeholder = '', onChange = null, onSelectionChange = null,
                               onCompositionEnd = null } = {}) {
  const root = h('div', { class: 'bench-ed' });
  root.setAttribute('role', 'textbox');
  root.setAttribute('aria-multiline', 'true');
  root.setAttribute('aria-label', '章节正文');
  if (placeholder) root.dataset.placeholder = placeholder;
  host.append(root);

  /* 回调集中存放：划词气泡等外部组件可经 setXxxListener 挂钩（工厂参数之外的挂载点）。
     v5.2.0：input（输入转发，幽灵字防抖用）、ghostAdopt/ghostDismiss（Tab 采纳/建议消失）。 */
  const handlers = {
    change: onChange,
    selection: onSelectionChange,
    compositionEnd: onCompositionEnd,
    input: null,
    ghostAdopt: null,
    ghostDismiss: null
  };

  let paras = [];              // 段元素列表（与文本分段一一对应）
  let composing = false;       // 拼音组词期间：bench 不落盘，本模块不重排当前段
  let destroyed = false;
  const opts = { paragraphFocus: false, typewriter: false };

  /* ------------------------------------------------------------- 渲染 */

  function makePara(text) {
    const p = h('p', { class: 'edp', contenteditable: 'true', spellcheck: 'false' });
    fillPara(p, text);
    return p;
  }

  function fillPara(p, text) {
    /* 段内文本写入：单 \n 软换行渲染成 <br>，其余直接放文本节点。 */
    p.textContent = '';
    const pieces = String(text || '').split('\n');
    pieces.forEach((piece, i) => {
      if (i > 0) p.append(document.createElement('br'));
      if (piece) p.append(document.createTextNode(piece));
    });
  }

  function render(text) {
    const parts = String(text || '').replace(/\r\n?/g, '\n').split(/\n{2,}/);
    ghostState = null;   // 全量重渲：幽灵字虚拟节点随旧 DOM 消失，状态同步复位
    root.textContent = '';
    paras = parts.map((t) => makePara(t));
    if (!paras.length) paras = [makePara('')];
    for (const p of paras) root.append(p);
    root.append(h('div', { class: 'ed-spacer', 'aria-hidden': 'true' }));
    applyFocusDim();
  }

  /* ------------------------------------------------------------- 读取 */

  function getText() {
    return paras.map((p) => paraText(p)).join('\n\n');
  }

  function paraStart(i) {
    let n = 0;
    for (let k = 0; k < i; k += 1) n += paraText(paras[k]).length + 2;
    return n;
  }

  function _locate(p, node, offset) {
    /* 段内定位：node+offset（Selection 锚点）→ 该段文本内的字符偏移。
       锚点落在段容器本身时，offset 是子节点序号，按前缀长度换算。 */
    if (node === p) return Math.min(childPrefixLen(p, offset || 0), paraText(p).length);
    let count = 0;
    let done = false;
    const walk = (el) => {
      if (done) return;
      for (const child of el.childNodes) {
        if (child === node) {
          count += child.nodeType === 3 ? (offset || 0) : childPrefixLen(child, offset || 0);
          done = true;
          return;
        }
        if (isGhostEl(child)) continue;   // 幽灵字虚拟节点不占模型偏移
        if (child.nodeType === 3) count += (child.nodeValue || '').length;
        else if (child.nodeType === 1 && child.tagName === 'BR') count += 1;
        else walk(child);
        if (done) return;
      }
    };
    walk(p);
    if (!done) count = paraText(p).length;
    return Math.min(count, paraText(p).length);
  }

  function findParaOf(node) {
    let el = node && node.nodeType === 3 ? node.parentNode : node;
    while (el) {
      const idx = paras.indexOf(el);
      if (idx >= 0) return idx;
      el = el.parentNode;
    }
    return -1;
  }

  /* 选区/光标 → 全局偏移。拿不到（选区不在编辑器里）返回 null。 */
  function currentSel() {
    const sel = typeof window !== 'undefined' && window.getSelection ? window.getSelection() : null;
    if (!sel || !sel.anchorNode || !sel.focusNode) return null;
    const ai = findParaOf(sel.anchorNode);
    const fi = findParaOf(sel.focusNode);
    if (ai < 0 || fi < 0) return null;
    const a = paraStart(ai) + _locate(paras[ai], sel.anchorNode, sel.anchorOffset);
    const f = paraStart(fi) + _locate(paras[fi], sel.focusNode, sel.focusOffset);
    const start = Math.min(a, f);
    const end = Math.max(a, f);
    let rect = null;
    try {
      if (sel.rangeCount) {
        const r = sel.getRangeAt(0).getBoundingClientRect();
        if (r && (r.width || r.height || r.top || r.left)) {
          rect = { top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height };
        }
      }
    } catch { /* jsdom / 无布局时没有几何信息，气泡靠挂载位置兜底 */ }
    return { start, end, text: getText().slice(start, end), rect, collapsed: start === end };
  }

  function getCaretOffset() {
    const s = currentSel();
    return s ? s.start : -1;
  }

  /* 全局偏移 → 段号 + 段内偏移。 */
  function toLocal(offset) {
    const total = getText();
    const o = Math.max(0, Math.min(offset, total.length));
    let acc = 0;
    for (let i = 0; i < paras.length; i += 1) {
      const len = paraText(paras[i]).length;
      if (o <= acc + len) return { i, local: o - acc };
      acc += len + 2;
    }
    return { i: paras.length - 1, local: paraText(paras[paras.length - 1]).length };
  }

  function setCaretOffset(offset) {
    const { i, local } = toLocal(offset);
    const p = paras[i];
    const sel = window.getSelection();
    if (!sel) return;
    const range = document.createRange();
    _placeRange(range, p, local);
    sel.removeAllRanges();
    sel.addRange(range);
  }

  function _placeRange(range, p, local) {
    /* 在段 p 内把 range 起点放到第 local 个字符处；local 落在软换行上取边界。
       幽灵字虚拟节点不占模型偏移：跳过不消费 want，兜底落点也避开它。 */
    let want = local;
    let placed = false;
    const walk = (el) => {
      for (const child of el.childNodes) {
        if (isGhostEl(child)) continue;
        if (child.nodeType === 3) {
          const len = (child.nodeValue || '').length;
          if (want <= len) { range.setStart(child, want); placed = true; return; }
          want -= len;
        } else if (child.nodeType === 1 && child.tagName === 'BR') {
          if (want <= 1) { range.setStart(child.parentNode, childIndex(child)); placed = true; return; }
          want -= 1;
        }
        if (placed) return;
      }
    };
    walk(p);
    if (!placed) {
      /* 兜底：从尾部找最后一个非幽灵子节点落点（幽灵字显示时光标停在灰字之前）。 */
      let anchor = null;
      for (let k = p.childNodes.length - 1; k >= 0; k -= 1) {
        if (!isGhostEl(p.childNodes[k])) { anchor = p.childNodes[k]; break; }
      }
      if (anchor && anchor.nodeType === 3) range.setStart(anchor, (anchor.nodeValue || '').length);
      else if (anchor) range.setStart(p, childIndex(anchor) + 1);
      else range.setStart(p, p.childNodes.length);
    }
    range.collapse(true);
  }

  function childIndex(node) {
    let i = 0;
    let el = node;
    while (el.previousSibling) { i += 1; el = el.previousSibling; }
    return i;
  }

  /* ------------------------------------------------------------- 结构操作 */

  function _caretLocal(p) {
    const sel = window.getSelection();
    if (!sel || !sel.anchorNode || findParaOf(sel.anchorNode) < 0) return null;
    return _locate(p, sel.anchorNode, sel.anchorOffset);
  }

  function paraIndex(p) { return paras.indexOf(p); }

  function rebuild() {
    /* 全量重渲（分段结构变化时用）：渲染后把回调交给 onChange。 */
    const text = getText();
    render(text);
    _emitChange();
  }

  function splitPara(p, local) {
    const i = paraIndex(p);
    const text = paraText(p);
    const head = text.slice(0, local);
    const tail = text.slice(local);
    fillPara(p, head);
    const np = makePara(tail, false);
    p.parentNode.insertBefore(np, p.nextSibling);
    paras.splice(i + 1, 0, np);
    applyFocusDim();
    _emitChange();
    setCaretOffset(paraStart(i + 1));
  }

  function mergeWithPrev(p) {
    const i = paraIndex(p);
    if (i <= 0) return false;
    const prev = paras[i - 1];
    const prevLen = paraText(prev).length;
    fillPara(prev, paraText(prev) + paraText(p));
    p.remove();
    paras.splice(i, 1);
    applyFocusDim();
    _emitChange();
    setCaretOffset(paraStart(i - 1) + prevLen);
    return true;
  }

  function mergeWithNext(p) {
    const i = paraIndex(p);
    if (i < 0 || i >= paras.length - 1) return false;
    const next = paras[i + 1];
    const curLen = paraText(p).length;
    fillPara(p, paraText(p) + paraText(next));
    next.remove();
    paras.splice(i + 1, 1);
    applyFocusDim();
    _emitChange();
    setCaretOffset(paraStart(i) + curLen);
    return true;
  }

  /* 模型级区间替换（跨段安全）：替换后重渲并恢复光标到替换文本之后。 */
  function replaceRange(start, end, replacement) {
    const text = getText();
    const s = Math.max(0, Math.min(start, text.length));
    const e = Math.max(s, Math.min(end, text.length));
    const next = text.slice(0, s) + String(replacement || '') + text.slice(e);
    render(next);
    setCaretOffset(s + String(replacement || '').length);
    _emitChange();
    return { start: s, end: s + String(replacement || '').length };
  }

  /* 划词替换专用：先校验原选区文本没变，变了就拒绝（前端提示，绝不覆盖新输入）。 */
  function replaceSelectionChecked(expected, replacement) {
    const cur = currentSel();
    if (!cur || cur.collapsed) return { ok: false, reason: 'empty' };
    if (cur.text !== expected) return { ok: false, reason: 'changed' };
    replaceRange(cur.start, cur.end, replacement);
    return { ok: true, start: cur.start, end: cur.start + String(replacement || '').length };
  }

  function insertAtCaret(text) {
    const cur = currentSel();
    if (!cur) return false;
    if (cur.collapsed) {
      const { i, local } = toLocal(cur.start);
      const p = paras[i];
      const t = paraText(p);
      if (!String(text || '').includes('\n')) {
        /* 单段内小段插入：优先 execCommand（保留浏览器原生撤销栈），失败再回 DOM 重写。 */
        try {
          if (typeof document.execCommand === 'function' && document.execCommand('insertText', false, String(text))) {
            _emitChange();
            return true;
          }
        } catch { /* jsdom / 不支持时走回退 */ }
        fillPara(p, t.slice(0, local) + text + t.slice(local));
        setCaretOffset(cur.start + text.length);
        _emitChange();
        return true;
      }
      replaceRange(cur.start, cur.start, text);
      return true;
    }
    replaceRange(cur.start, cur.end, text);
    return true;
  }

  /* ------------------------------------------------------------- 幽灵字（v5.2.0） */

  /* 幽灵字状态：{ text, i, local, off, span }。灰字 span 挂在光标所在段内，
     是「渲染层存在、模型层不存在」的虚拟节点（nodeText/_locate/_placeRange 均跳过），
     Tab 采纳时才经 insertAtCaret 真正写入模型（保留浏览器原生撤销栈）。 */
  let ghostState = null;
  let ghostAdopting = false;   // Tab 采纳期间抑制 input 事件触发的自动清除
  let pluginLock = false;      // 插件运行锁（流式续写期间冻结划词派发；幽灵字触发由其自行悬挂）

  function _clearGhostDom() {
    if (ghostState && ghostState.span) {
      try { ghostState.span.remove(); } catch { /* 已随段落重建消失 */ }
    }
    if (ghostState && ghostState.hint) {
      try { ghostState.hint.remove(); } catch { /* 同上 */ }
    }
    ghostState = null;
  }

  function clearGhost() {
    if (!ghostState) return false;
    _clearGhostDom();
    if (handlers.ghostDismiss && !destroyed) handlers.ghostDismiss();
    return true;
  }

  /* 光标后渲染灰字建议 + 「Tab 采纳」键帽。要求光标在编辑器内且收拢；
     落点按当前光标模型偏移计算。任何输入/移动按键都会先清除旧建议。 */
  function setGhost(text) {
    clearGhost();
    const t = String(text || '');
    if (!t) return false;
    const sel = currentSel();
    if (!sel || !sel.collapsed) return false;
    const { i, local } = toLocal(sel.start);
    const p = paras[i];
    if (!p) return false;
    const span = h('span', { class: 'ghost-text', contenteditable: 'false', text: t });
    const hint = h('span', { class: 'ghost-key', contenteditable: 'false' },
      h('kbd', { class: 'ghost-kbd', text: 'Tab' }), ' 采纳');
    /* 段内定位插入：want 与 _placeRange 同算法（文本节点按需劈开，BR 越过）。 */
    let want = local;
    let inserted = false;
    for (const child of Array.from(p.childNodes)) {
      if (isGhostEl(child)) continue;
      if (child.nodeType === 3) {
        const len = (child.nodeValue || '').length;
        if (want < len) {
          const tail = child.splitText(want);
          p.insertBefore(span, tail);
          inserted = true;
          break;
        }
        if (want === len) { p.insertBefore(span, child.nextSibling); inserted = true; break; }
        want -= len;
      } else if (child.nodeType === 1 && child.tagName === 'BR') {
        if (want <= 1) { p.insertBefore(span, child.nextSibling); inserted = true; break; }
        want -= 1;
      }
    }
    if (!inserted) p.append(span);
    span.after(hint);
    ghostState = { text: t, i, local, off: sel.start, span, hint };
    return true;
  }

  function getGhost() { return ghostState ? ghostState.text : ''; }
  function ghostActive() { return !!ghostState; }

  /* Tab 采纳：摘灰字 → 光标回原位 → 走 insertAtCaret 真正入模（原生撤销可回退）。
     成功后回调 ghostAdopt（bench 侧据此派发 AI 插入事件与落盘）。 */
  function adoptGhost() {
    if (!ghostState) return null;
    const st = ghostState;
    ghostAdopting = true;
    try {
      _clearGhostDom();
      setCaretOffset(st.off);
      const ok = insertAtCaret(st.text);
      if (!ok) return null;
      const result = { text: st.text, chars: st.text.length, start: st.off, end: st.off + st.text.length };
      if (handlers.ghostAdopt && !destroyed) handlers.ghostAdopt(result);
      return result;
    } finally {
      ghostAdopting = false;
    }
  }

  /* ------------------------------------------------------------- 事件 */

  function _emitChange() {
    if (handlers.change && !destroyed) {
      handlers.change(getText(), { composing, adopting: ghostAdopting });
    }
  }

  root.addEventListener('compositionstart', () => {
    composing = true;
    if (ghostState) _clearGhostDom();   // 组词开始：建议让位
  });
  root.addEventListener('compositionend', () => {
    composing = false;
    _emitChange();
    if (handlers.compositionEnd && !destroyed) handlers.compositionEnd(getText());
  });

  root.addEventListener('input', (event) => {
    /* 只重读当前段（event.target 可能是段内文本节点 → 取所在段）。 */
    const p = (event.target && event.target.nodeType === 1 && event.target.classList.contains('edp'))
      ? event.target
      : paras[findParaOf(event.target)];
    /* v5.2.0：任何输入清除幽灵字（Tab 采纳路径自带 ghostAdopting 抑制标志，不在此列）。 */
    if (!ghostAdopting && ghostState) _clearGhostDom();
    if (p && !composing) applyFocusDim();
    _emitChange();
    if (handlers.input && !destroyed) handlers.input(getText(), { composing });
    if (opts.typewriter) maybeScroll();
  });

  root.addEventListener('keydown', (event) => {
    if (composing) return;   // 组词期间的按键交给输入法
    /* v5.2.0 幽灵字按键：Tab 采纳 / Esc 取消 / 方向键光标照走并清除 / 其余先摘建议。 */
    if (ghostState) {
      if (event.key === 'Tab') {
        event.preventDefault();
        adoptGhost();
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        clearGhost();
        return;
      }
      if (/^Arrow/.test(event.key) || ['Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) {
        clearGhost();   // 不拦默认：光标正常移动，灰字与键帽摘除
        return;
      }
      if (event.key === 'Enter' || event.key === 'Backspace' || event.key === 'Delete') {
        clearGhost();   // 结构键先摘建议，再走原有分段/合并逻辑
      }
      /* 其余可打印键不清除：随后的 input 事件统一清除（采纳路径有抑制标志） */
    }
    const sel = window.getSelection();
    const anchor = sel && sel.anchorNode;
    const i = findParaOf(anchor);
    if (i < 0) return;
    const p = paras[i];
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      const local = _caretLocal(p);
      splitPara(p, local == null ? paraText(p).length : local);
      return;
    }
    if (event.key === 'Enter' && event.shiftKey) {
      /* 软换行：段内留一个真实 \n（渲染成 <br>），模型与 getText 口径一致。 */
      event.preventDefault();
      const local = _caretLocal(p);
      const t = paraText(p);
      const at = local == null ? t.length : local;
      fillPara(p, t.slice(0, at) + '\n' + t.slice(at));
      _emitChange();
      setCaretOffset(paraStart(i) + at + 1);
      return;
    }
    if (event.key === 'Backspace') {
      const local = _caretLocal(p);
      const cur = currentSel();
      if (cur && !cur.collapsed) return;   // 有选区交给浏览器默认删除
      if (local === 0) {
        event.preventDefault();
        mergeWithPrev(p);
      }
      return;
    }
    if (event.key === 'Delete') {
      const cur = currentSel();
      if (cur && !cur.collapsed) return;
      const local = _caretLocal(p);
      if (local != null && local >= paraText(p).length && i < paras.length - 1) {
        event.preventDefault();
        mergeWithNext(p);
      }
      return;
    }
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      const local = _caretLocal(p);
      const len = paraText(p).length;
      if (event.key === 'ArrowUp' && (local === 0 || paraText(p).length === 0) && i > 0) {
        event.preventDefault();
        setCaretOffset(paraStart(i - 1) + Math.min(local || 0, paraText(paras[i - 1]).length));
      } else if (event.key === 'ArrowDown' && local >= len && i < paras.length - 1) {
        event.preventDefault();
        setCaretOffset(paraStart(i + 1) + Math.min(local - len, paraText(paras[i + 1]).length));
      }
      return;
    }
    if (event.key === 'Tab') {
      /* 写作台不给 Tab 制表位：防止焦点被切出编辑器丢字。 */
      event.preventDefault();
    }
  });

  root.addEventListener('paste', (event) => {
    /* 粘贴一律转纯文本：富文本 HTML 不进正文（纯净域 + 模型简单性双赢）。
       多段文本按空行切段后落进模型；小段纯文本走 DOM 直插保留原生撤销。 */
    if (composing) return;
    const dt = event.clipboardData || (typeof window !== 'undefined' && window.clipboardData);
    const raw = dt && typeof dt.getData === 'function' ? dt.getData('text/plain') : '';
    if (!raw) {
      event.preventDefault();
      return;
    }
    event.preventDefault();
    const text = raw.replace(/\r\n?/g, '\n').replace(/\u00a0/g, ' ');
    const cur = currentSel();
    if (!cur) return;
    if (!cur.collapsed || /\n{2,}/.test(text) || /\n/.test(text)) {
      replaceRange(cur.start, cur.end, text);
    } else {
      insertAtCaret(text);
    }
  });

  function onDocSelectionChange() {
    if (destroyed) return;
    /* v5.2.0 幽灵字：光标移走（鼠标点击等非按键路径）即摘建议，防止采纳落点过期。 */
    if (ghostState) {
      const off = getCaretOffset();
      if (off < 0 || off !== ghostState.off) { clearGhost(); return; }
    }
    /* v5.2.0 插件锁：流式续写等插件运行期冻结划词派发（气泡不弹、生成不被打断）。 */
    if (pluginLock) return;
    const sel = typeof window !== 'undefined' && window.getSelection ? window.getSelection() : null;
    if (!sel || !sel.anchorNode || findParaOf(sel.anchorNode) < 0) return;
    applyFocusDim();
    if (handlers.selection) handlers.selection(currentSel());
  }
  document.addEventListener('selectionchange', onDocSelectionChange);

  /* ------------------------------------------------------------- 段落聚焦 / 打字机 */

  function applyFocusDim() {
    if (!opts.paragraphFocus) {
      for (const p of paras) p.classList.remove('ed-dim');
      root.removeAttribute('data-focus-on');
      return;
    }
    root.setAttribute('data-focus-on', '');
    const sel = typeof window !== 'undefined' && window.getSelection ? window.getSelection() : null;
    const idx = sel && sel.anchorNode ? findParaOf(sel.anchorNode) : -1;
    paras.forEach((p, i) => p.classList.toggle('ed-dim', idx >= 0 && i !== idx));
  }

  function maybeScroll() {
    /* 打字机滚动：光标低于「编辑区高度 × 0.5」基准线时把滚动条上推，让光标行停在基准线上。 */
    if (!opts.typewriter) return;
    let caretRect = null;
    try {
      const sel = window.getSelection();
      if (sel && sel.rangeCount) caretRect = sel.getRangeAt(0).getBoundingClientRect();
    } catch { return; }
    if (!caretRect || !caretRect.top) return;
    const box = host.getBoundingClientRect();
    if (!box || !box.top) return;
    const baseline = box.top + box.height * 0.5;
    if (caretRect.bottom > baseline) {
      host.scrollTop += caretRect.bottom - baseline;
    }
  }

  /* ------------------------------------------------------------- 对外 API */

  return {
    el: root,
    getText,
    setText(text, { silent = false } = {}) {
      render(text);
      if (!silent) _emitChange();
    },
    isComposing: () => composing,
    currentSel,
    getCaretOffset,
    setCaretOffset,
    replaceRange,
    replaceSelectionChecked,
    insertAtCaret,
    paraCount: () => paras.length,
    setSelectionListener(fn) { handlers.selection = typeof fn === 'function' ? fn : null; },
    /* ---- v5.2.0 幽灵字 / 插件协作 API ---- */
    setGhost,
    clearGhost,
    getGhost,
    ghostActive,
    setGhostAdoptListener(fn) { handlers.ghostAdopt = typeof fn === 'function' ? fn : null; },
    setGhostDismissListener(fn) { handlers.ghostDismiss = typeof fn === 'function' ? fn : null; },
    setInputListener(fn) { handlers.input = typeof fn === 'function' ? fn : null; },
    /* 插件锁：流式续写运行期冻结划词派发（selectionchange 不再转发）；
       幽灵字模块经 isPluginLocked 自行悬挂触发。任何失败不得影响正常输入。 */
    setPluginLock(on) { pluginLock = !!on; },
    isPluginLocked: () => pluginLock,
    /* 定位到全局偏移（敏感词徽标点击用）：光标落点 + 段聚焦 + 打字机滚动跟随。 */
    revealOffset(offset) {
      try {
        setCaretOffset(offset);
        const { i } = toLocal(offset);
        const p = paras[i];
        if (p && typeof p.focus === 'function') p.focus();
        if (opts.typewriter) maybeScroll();
      } catch { /* jsdom 无 focus 实现时只落选区 */ }
    },
    focus() {
      try {
        const p = paras[0];
        const sel = window.getSelection();
        if (!p || !sel) return;
        const range = document.createRange();
        _placeRange(range, p, 0);
        sel.removeAllRanges();
        sel.addRange(range);
        p.focus();
      } catch { /* jsdom 无 focus 实现时忽略 */ }
    },    focusEnd() {
      try {
        this.setCaretOffset(getText().length);
        const last = paras[paras.length - 1];
        if (last) last.focus();
      } catch { /* 同上 */ }
    },
    setParagraphFocus(on) {
      opts.paragraphFocus = !!on;
      applyFocusDim();
    },
    setTypewriter(on) {
      opts.typewriter = !!on;
      if (on) host.classList.add('ed-typewriter');
      else host.classList.remove('ed-typewriter');
    },
    maybeScroll,
    clearSelection() {
      try { window.getSelection().removeAllRanges(); } catch { /* 忽略 */ }
    },
    destroy() {
      destroyed = true;
      document.removeEventListener('selectionchange', onDocSelectionChange);
      root.remove();
    }
  };
}
