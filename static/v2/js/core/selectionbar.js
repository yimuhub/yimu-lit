/* 划词气泡（v5.1.0 写作台编辑器内核）。
 *
 * 交互约定（与 bot/selection_ai.py 的接口一一对应）：
 * · 选中编辑器文字 → 浮出动作气泡：润色/扩写/纠错/去AI味/按人物口吻/自定义指令；
 * · 结果面板流式逐字显示，**预览不落稿**——只有用户点「替换选区」才动正文；
 * · 替换前校验原选区文本未变（editor.replaceSelectionChecked），变了就放弃并提示，
 *   绝不覆盖用户在生成期间的继续输入；
 * · 替换成功后回调 onReplaced()，由 bench 触发保存与版本快照（AI 改动立即留痕）。
 *
 * 气泡与面板挂 body（fixed 定位），样式在 bench-editor.css。
 */

import { h, icon } from './dom.js';
import { toast } from './ui.js';
import { request, streamSSE, errorText, IS_MOCK } from './api.js';

/* 动作顺序即气泡排布顺序；与后端 ACTIONS 同键。 */
export const SELECTION_ACTIONS = [
  { key: 'polish', label: '润色' },
  { key: 'expand', label: '扩写' },
  { key: 'fix', label: '纠错' },
  { key: 'deslop', label: '去 AI 味' },
  { key: 'tone', label: '按人物口吻' },
  { key: 'custom', label: '自定义指令' }
];

const MIN_SEL = 1;
const MAX_SEL = 2000;

export function mountSelectionBar({ editor, getContext, onReplaced = null } = {}) {
  const wrap = h('div', { class: 'sb-wrap', hidden: true });
  document.body.append(wrap);

  let bubble = null;
  let panel = null;
  let abort = null;          // 生成中的 AbortController
  let captured = null;       // { start, end, text, action, instruction, persona, result }
  let openBubbleTimer = 0;

  /* ------------------------------------------------------------- 气泡 */

  function hideBubble() {
    clearTimeout(openBubbleTimer);
    if (bubble) { bubble.remove(); bubble = null; }
  }

  function showBubble(sel) {
    hideBubble();
    const rect = sel.rect || { left: 200, top: 200, right: 260, bottom: 230 };
    const x = Math.max(8, Math.min(rect.left + rect.width / 2, (window.innerWidth || 1200) - 200));
    const y = Math.max(8, rect.top - 44);
    bubble = h(
      'div',
      { class: 'sb-bubble', role: 'menu', style: { left: `${x}px`, top: `${y}px` } },
      SELECTION_ACTIONS.slice(0, 5).map((a) => bubbleBtn(a)),
      h('span', { class: 'sb-sep' }),
      bubbleBtn(SELECTION_ACTIONS[5])
    );
    wrap.append(bubble);
  }

  function bubbleBtn(action) {
    return h('button', {
      class: 'sb-btn',
      type: 'button',
      role: 'menuitem',
      text: action.label,
      dataset: { action: action.key },
      on: { click: () => startAction(action.key) }
    });
  }

  /* ------------------------------------------------------------- 结果面板 */

  function closePanel({ abortStream = true } = {}) {
    if (abortStream && abort) { try { abort.abort(); } catch { /* 已结束 */ } }
    abort = null;
    /* 只有真的关掉了面板才清 captured —— openPanel 复位时也会调本函数，
       若无条件清空，紧随其后的 run() 会因 captured 为空静默退出（面板永远停在生成中）。 */
    if (panel) { panel.remove(); panel = null; captured = null; }
  }

  function openPanel(action, origText) {
    closePanel({ abortStream: false });
    panel = h('div', { class: 'sb-panel', role: 'dialog', 'aria-label': `划词${action.label}` });
    const head = h(
      'div',
      { class: 'sb-panel-head' },
      h('span', { class: 'sb-title', html: `${icon('wand', 14)}<span>${action.label}</span>` }),
      h('button', {
        class: 'icon-btn icon-btn-sm', type: 'button', 'aria-label': '关闭',
        html: icon('x', 14),
        on: { click: () => closePanel() }
      })
    );
    const orig = h('div', { class: 'sb-orig', title: '选区原文（替换前会校验这段没有被改过）', text: `选区：${origText.slice(0, 80)}${origText.length > 80 ? '…' : ''}` });
    const result = h('div', { class: 'sb-result' });
    const status = h('div', { class: 'sb-status hint-text', text: '正在生成…' });
    /* 按人物口吻：可填人物名（不填 = 模型从上下文自行识别）；自定义指令：必填指令。 */
    let extraInput = null;
    if (action.key === 'custom' || action.key === 'tone') {
      extraInput = h('input', {
        class: 'sb-extra',
        type: 'text',
        placeholder: action.key === 'custom' ? '用一句指令描述你想怎么改这段（必填）' : '人物名或口吻说明（可选，如「老刑警，说话短促」）'
      });
    }
    const replaceBtn = h('button', {
      class: 'btn btn-primary btn-sm', type: 'button', text: '替换选区', disabled: true,
      on: { click: () => doReplace(status) }
    });
    const rerunBtn = h('button', {
      class: 'btn btn-sm', type: 'button', text: '重新生成', disabled: true,
      on: { click: () => run(action, { extraInput, result, status, replaceBtn, rerunBtn }) }
    });
    const foot = h(
      'div',
      { class: 'sb-panel-foot' },
      h('span', { style: { flex: '1' } }),
      rerunBtn,
      replaceBtn
    );
    panel.append(head, orig, ...(extraInput ? [extraInput] : []), result, status, foot);
    wrap.append(panel);
    positionPanel();
    if (extraInput) {
      extraInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          run(action, { extraInput, result, status, replaceBtn, rerunBtn });
        }
      });
      if (action.key === 'custom') extraInput.focus();
    }
    run(action, { extraInput, result, status, replaceBtn, rerunBtn });
  }

  function positionPanel() {
    if (!panel || !captured) return;
    const rect = captured.rect || { right: 300, top: 260, bottom: 300, left: 200 };
    const vw = window.innerWidth || 1200;
    const vh = window.innerHeight || 800;
    const width = Math.min(460, vw - 24);
    let left = Math.max(12, Math.min(rect.right + 12, vw - width - 12));
    if (left + width > vw - 12) left = Math.max(12, rect.left - width - 12);
    let top = Math.max(12, Math.min(rect.bottom + 8, vh - 320));
    panel.style.left = `${left}px`;
    panel.style.top = `${top}px`;
    panel.style.width = `${width}px`;
  }

  /* ------------------------------------------------------------- 调用 */

  function startAction(actionKey) {
    const sel = editor.currentSel();
    if (!sel || sel.collapsed) {
      toast('请先选中要处理的一段文字', { tone: 'warn' });
      return;
    }
    if (sel.text.trim().length < MIN_SEL || sel.text.length > MAX_SEL) {
      toast(`选区请在 ${MIN_SEL}-${MAX_SEL} 字之间`, { tone: 'warn' });
      return;
    }
    const action = SELECTION_ACTIONS.find((a) => a.key === actionKey);
    if (!action) return;
    captured = { start: sel.start, end: sel.end, text: sel.text, rect: sel.rect, action: action.key, result: '' };
    hideBubble();
    openPanel(action, sel.text);
  }

  async function run(action, ui) {
    if (!captured) return;
    if (action.key === 'custom') {
      const ins = (ui.extraInput && ui.extraInput.value || '').trim();
      if (!ins) {
        ui.status.textContent = '请先填写自定义指令';
        ui.extraInput && ui.extraInput.focus();
        return;
      }
    }
    if (abort) { try { abort.abort(); } catch { /* 上一次已结束 */ } }
    abort = new AbortController();
    const ctx = typeof getContext === 'function' ? (getContext() || {}) : {};
    const text = editor.getText();
    const payload = {
      action: action.key,
      selection: captured.text,
      before: text.slice(Math.max(0, captured.start - 220), captured.start),
      after: text.slice(captured.end, captured.end + 220),
      title: ctx.title || '',
      summary: ctx.summary || '',
      key: ctx.key || '',
      n: ctx.n || 0,
      instruction: ui.extraInput && action.key === 'custom' ? ui.extraInput.value.trim() : '',
      persona: ui.extraInput && action.key === 'tone' ? ui.extraInput.value.trim() : ''
    };
    ui.result.textContent = '';
    ui.status.textContent = IS_MOCK ? '演示模式：请求真实后端获取划词结果…' : '正在生成…';
    ui.replaceBtn.disabled = true;
    ui.rerunBtn.disabled = true;
    let acc = '';
    let err = '';
    try {
      /* 真实流式：delta 逐字进预览区；done 带全文（以后端为准，防丢帧）。 */
      const final = await streamSSE('/api/chapter/selection-ai', payload, {
        signal: abort.signal,
        onEvent: (event, data) => {
          if (event === 'delta' && data && data.text) {
            acc += String(data.text);
            ui.result.textContent = acc;
            ui.result.scrollTop = ui.result.scrollHeight;
          } else if (event === 'error' && data) {
            err = String(data.error || '划词失败');
          }
        }
      });
      const doneText = (final && final.text) || acc;
      if (!doneText || !doneText.trim()) {
        ui.status.textContent = err || '结果为空，请重试';
        return;
      }
      captured.result = doneText;
      ui.result.textContent = doneText;
      ui.status.textContent = `完成（${doneText.length} 字）· 检查无误后点「替换选区」`;
      ui.replaceBtn.disabled = false;
      ui.rerunBtn.disabled = false;
    } catch (e) {
      if (e && (e.isAbort || e.code === 'aborted')) {
        ui.status.textContent = '已取消';
        return;
      }
      ui.status.textContent = errorText(e, '划词失败，请重试');
      ui.rerunBtn.disabled = false;
    } finally {
      abort = null;
    }
  }

  function doReplace(status) {
    if (!captured || !captured.result) return;
    const r = editor.replaceSelectionChecked(captured.text, captured.result);
    if (!r.ok) {
      if (r.reason === 'changed') {
        toast('选区内容已变化（生成期间你又改了正文），本次替换已放弃，防止覆盖新输入', { tone: 'warn', duration: 5000 });
        status.textContent = '替换放弃：原选区已变化';
      } else {
        toast('没有找到原选区，请重新选中后再试', { tone: 'warn' });
        status.textContent = '替换放弃：选区不存在';
      }
      return;
    }
    closePanel();
    toast('已替换选区，正在保存并留版本快照…', { tone: 'success', duration: 2000 });
    if (onReplaced) onReplaced({ start: r.start, end: r.end });
  }

  /* ------------------------------------------------------------- 全局监听 */

  function onSelectionChange(sel) {
    if (!sel) { hideBubble(); return; }
    if (sel.collapsed || sel.text.trim().length < MIN_SEL || sel.text.length > MAX_SEL) {
      hideBubble();
      return;
    }
    if (panel) return;   // 面板开着时不弹气泡（生成期间继续选字不打断）
    clearTimeout(openBubbleTimer);
    openBubbleTimer = setTimeout(() => showBubble(sel), 220);
  }

  function onDocPointerDown(event) {
    if (wrap.contains(event.target)) return;
    hideBubble();
    if (panel) closePanel();
  }

  function onKeydown(event) {
    if (event.key === 'Escape') {
      hideBubble();
      if (panel) closePanel();
    }
  }

  document.addEventListener('pointerdown', onDocPointerDown, true);
  document.addEventListener('keydown', onKeydown);

  const onScroll = () => { hideBubble(); positionPanel(); };
  window.addEventListener('scroll', onScroll, true);
  window.addEventListener('resize', onScroll);

  editor.setSelectionListener(onSelectionChange);

  return {
    el: wrap,
    close: () => { hideBubble(); closePanel(); },
    destroy() {
      document.removeEventListener('pointerdown', onDocPointerDown, true);
      document.removeEventListener('keydown', onKeydown);
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', onScroll);
      closePanel();
      wrap.remove();
    }
  };
}

/* 动作表从后端拿（可选增强）：拿不到就用内置表，气泡渲染不受影响。 */
export async function loadActionMeta() {
  try {
    const r = await request('/api/selection-ai/actions', { timeout: 6000 });
    return (r && r.actions) || null;
  } catch {
    return null;
  }
}
