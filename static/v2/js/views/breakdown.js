/* 拆书工作台（v5.4.0，docs/系统化升级方案 8.1，全部自研）。
   流程：上传/粘贴 → 切章预览（卷/章数、前 12 章标题+字数，可整体重切）→
   工作台（左章节列表带状态徽标，右分析卡 + 原文对照（段落高亮））→ 全书报告 + 手法入账本。

   纯净域：拆书对象是外部文本；展示的原文只用于工作台对照，
   入账本的手法卡只带手法描述 + 证据位置（后端 bot/breakdown.py 强制校验）。 */

import { h, fmtNum } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { request, errorText } from '../core/api.js';
import { toast, confirmDialog } from '../core/ui.js';
import { renderMarkdownInto } from '../core/markdown.js';

/* ---------------------------------------------------------------- 纯函数（导出供 node 测试） */

/** UI 状态机：list → create → preview → work（report 是 work 内的页签）。 */
export function nextScreen(screen, event) {
  switch (event && event.type) {
    case 'new': return 'create';
    case 'created': return 'preview';      // 上传/粘贴成功，先看切章预览
    case 'resplit': return 'preview';      // 整体重切后留在预览
    case 'open':
    case 'confirm': return 'work';         // 打开已有项目 / 开始拆解
    case 'back': return 'list';
    default: return screen;
  }
}

/** 段落区间归一化：clamp 到 [1, total]、start ≤ end、非法输入回退。返回 [s, e]。 */
export function paraRange(startPara, endPara, total) {
  const n = Math.max(0, Number(total) || 0);
  let s = Math.round(Number(startPara));
  let e = Math.round(Number(endPara));
  if (!Number.isFinite(s) || s < 1) s = 1;
  if (!Number.isFinite(e) || e < 1) e = s;
  s = Math.min(Math.max(1, s), Math.max(1, n));
  e = Math.min(Math.max(1, e), Math.max(1, n));
  if (s > e) { const t = s; s = e; e = t; }
  return [s, e];
}

/** 章节状态徽标（label + tone，tone 对应 CSS 类）。 */
export function chapterBadge(state) {
  const map = {
    pending: { label: '待拆', tone: 'dim' },
    draft: { label: '待拆', tone: 'dim' },
    running: { label: '拆解中', tone: 'running' },
    done: { label: '完成', tone: 'success' },
    partial: { label: '部分失败', tone: 'warn' },
    failed: { label: '失败', tone: 'danger' }
  };
  return map[state] || map.pending;
}

/** 切章预览行（前 12 章）：[{no,title,chars,volume,golden}]。 */
export function splitPreviewRows(chapters, limit = 12) {
  return (Array.isArray(chapters) ? chapters : []).slice(0, Math.max(0, limit))
    .map((c) => ({
      no: c.no, title: c.title || `第${c.num}章`,
      chars: Number(c.chars) || 0, volume: c.volume || '', golden: !!c.golden
    }));
}

/** 手法入账本 payload：勾选下标 + 可选入账目标作品。indices 只保留合法范围。 */
export function buildAdoptPayload(report, checkedIndices, projectKey = '') {
  const total = (report && Array.isArray(report.techniques)) ? report.techniques.length : 0;
  const indices = (Array.isArray(checkedIndices) ? checkedIndices : [])
    .map((i) => Number(i))
    .filter((i) => Number.isInteger(i) && i >= 0 && i < total);
  return { indices, projectKey: String(projectKey || '') };
}

/** 进度摘要文案。 */
export function progressText(progress) {
  const p = progress || {};
  const total = Number(p.total) || 0;
  const done = Number(p.done) || 0;
  const failed = Number(p.failed) || 0;
  let s = `${done}/${total} 章已拆`;
  if (failed) s += ` · ${failed} 章失败`;
  if (p.currentTitle) s += ` · 正在拆：${p.currentTitle}`;
  return s;
}

/** 段落级高亮映射：给原文容器加 .bd-hl（startPara..endPara），其余清除；返回高亮段落号。 */
export function highlightParas(origEl, startPara, endPara) {
  if (!origEl || !origEl.isConnected) return [];
  const paras = Array.from(origEl.querySelectorAll('[data-para]'));
  if (!paras.length) return [];
  const total = Number(paras[0].dataset.total) || paras.length;
  const [s, e] = paraRange(startPara, endPara, total);
  const hit = [];
  for (const el of paras) {
    const no = Number(el.dataset.para);
    const inRange = no >= s && no <= e;
    el.classList.toggle('bd-hl', inRange);
    if (inRange) hit.push(no);
  }
  const first = origEl.querySelector('[data-para].bd-hl');
  if (first && typeof first.scrollIntoView === 'function') {
    try { first.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch { /* jsdom 无实现 */ }
  }
  return hit;
}

/* ---------------------------------------------------------------- 渲染片段（导出供 jsdom 测试） */

/** 分析卡集合：点击节奏点/大纲节点/钩子卡 → onAnchor(startPara, endPara) 锚定高亮。 */
export function renderAnalysisCards(analysis, { onAnchor } = {}) {
  const a = analysis || {};
  const wrap = h('div', { class: 'bd-cards' });
  const anchor = (s, e, cardEl) => () => {
    for (const el of wrap.querySelectorAll('.bd-card.active')) el.classList.remove('active');
    if (cardEl) cardEl.classList.add('active');
    if (onAnchor) onAnchor(s, e);
  };

  if (a.brief) {
    wrap.append(h('section', { class: 'bd-card' },
      h('h4', { class: 'bd-card-title', text: '本章梗概' }),
      h('p', { class: 'bd-brief', text: a.brief })));
  }
  const rhythm = Array.isArray(a.rhythm) ? a.rhythm : [];
  if (rhythm.length) {
    const card = h('section', { class: 'bd-card' },
      h('h4', { class: 'bd-card-title', text: '节奏标记（点击定位原文）' }));
    const box = h('div', { class: 'bd-chips' });
    for (const r of rhythm) {
      box.append(h('button', {
        class: 'bd-chip', type: 'button',
        title: r.note || '',
        text: `[段${r.para}] ${r.type}${r.note ? ' · ' + r.note : ''}`,
        on: { click: anchor(r.para, r.para, card) }
      }));
    }
    card.append(box);
    wrap.append(card);
  }
  if (a.conflict || a.value_shift) {
    const card = h('section', { class: 'bd-card' },
      h('h4', { class: 'bd-card-title', text: '冲突与价值翻转' }));
    if (a.conflict) card.append(h('p', { class: 'bd-line', text: `冲突：${a.conflict}` }));
    if (a.value_shift) card.append(h('p', { class: 'bd-line bd-shift', text: `价值翻转：${a.value_shift}` }));
    wrap.append(card);
  }
  if (a.hook && (a.hook.type || a.hook.text)) {
    const last = Number(a.para_count) || 0;
    const card = h('section', { class: 'bd-card' },
      h('h4', { class: 'bd-card-title', text: '章末钩子' }),
      h('p', { class: 'bd-line', text: `类型：${a.hook.type || '无'}` }),
      a.hook.text ? h('p', { class: 'bd-line', text: a.hook.text }) : null);
    if (last && onAnchor) {
      card.append(h('button', {
        class: 'bd-mini', type: 'button', text: `定位章末段（段${last}）`,
        on: { click: anchor(last, last, card) }
      }));
    }
    wrap.append(card);
  }
  const outline = Array.isArray(a.outline) ? a.outline : [];
  if (outline.length) {
    const card = h('section', { class: 'bd-card' },
      h('h4', { class: 'bd-card-title', text: '大纲节点（点击高亮段落区间）' }));
    for (const o of outline) {
      const range = paraRange(o.startPara, o.endPara, a.para_count || 0);
      card.append(h('button', {
        class: 'bd-outline-item', type: 'button',
        text: `[段${range[0]}-${range[1]}] ${o.title || '节点'}${o.summary ? ' — ' + o.summary : ''}`,
        on: { click: anchor(range[0], range[1], card) }
      }));
    }
    wrap.append(card);
  }
  const techs = Array.isArray(a.techniques) ? a.techniques : [];
  if (techs.length) {
    const card = h('section', { class: 'bd-card' },
      h('h4', { class: 'bd-card-title', text: '手法标签' }));
    const box = h('div', { class: 'bd-chips' });
    for (const t of techs) box.append(h('span', { class: 'bd-tag', text: t }));
    card.append(box);
    wrap.append(card);
  }
  if (a.golden && (a.golden.opening_hook || a.golden.immersion || a.golden.info_pacing)) {
    const g = a.golden;
    wrap.append(h('section', { class: 'bd-card bd-golden' },
      h('h4', { class: 'bd-card-title', html: `${icon('zap', 13)}<span>黄金三章深拆</span>` }),
      g.opening_hook ? h('p', { class: 'bd-line', text: `开篇钩子：${g.opening_hook}` }) : null,
      g.immersion ? h('p', { class: 'bd-line', text: `代入感：${g.immersion}` }) : null,
      g.info_pacing ? h('p', { class: 'bd-line', text: `信息释放节奏：${g.info_pacing}` }) : null));
  }
  return wrap;
}

/** 原文对照：段落列表（[n] 编号 + data-para，供高亮映射）。 */
export function renderOriginalText(paras) {
  const list = Array.isArray(paras) ? paras : [];
  const el = h('div', { class: 'bd-original' });
  list.forEach((p, i) => {
    el.append(h('p', {
      class: 'bd-para', dataset: { para: String(i + 1), total: String(list.length) }
    }, h('span', { class: 'bd-para-no', text: `[${i + 1}]` }), String(p)));
  });
  return el;
}

/* ---------------------------------------------------------------- 页面主体 */

export function renderBreakdown(container, ctx) {
  const view = h('div', { class: 'view-scroll' });
  const pad = h('div', { class: 'view-pad bd-pad' });
  view.append(pad);
  container.append(view);

  const model = {
    screen: 'list',          // list | create | preview | work
    projects: [],
    project: null,           // 当前项目全量（GET /api/breakdown/{id}）
    preview: null,           // 切章预览（upload/paste/resplit 返回）
    selected: 1,             // 工作台当前章
    tab: 'chapter',          // chapter | report
    pollTimer: null,
    pollDeadline: 0,
    destroyed: false,
    adoptChecked: new Set(),
    reportBusy: false
  };

  function setScreen(screen) {
    model.screen = screen;
    paint();
  }

  /* ---------------- 数据 ---------------- */

  async function loadList() {
    try {
      const d = await request('/api/breakdowns');
      model.projects = (d && d.projects) || [];
    } catch (err) {
      model.projects = [];
      toast(errorText(err, '拆书项目列表加载失败'), { tone: 'danger' });
    }
    paint();
  }

  async function loadProject(id, { keepScreen = true } = {}) {
    try {
      const d = await request(`/api/breakdown/${encodeURIComponent(id)}`);
      model.project = (d && d.project) || null;
      if (model.project && !model.project.chapters.some((c) => c.no === model.selected)) {
        model.selected = model.project.chapters.length ? model.project.chapters[0].no : 1;
      }
      if (keepScreen) setScreen('work');   // 打开项目一律进工作台
      else paint();
      syncPolling();
    } catch (err) {
      toast(errorText(err, '拆书项目加载失败'), { tone: 'danger' });
      setScreen('list');
    }
  }

  function syncPolling() {
    const running = !!(model.project && model.project.running);
    if (running && !model.pollTimer) {
      model.pollDeadline = Date.now() + 30 * 60 * 1000;   // 轮询总时长上限 30 分钟
      const tick = async () => {
        if (model.destroyed || !model.project) { stopPoll(); return; }
        if (Date.now() > model.pollDeadline) { stopPoll(); toast('拆解时间较长，已停止自动刷新，可稍后手动刷新'); return; }
        try {
          const d = await request(`/api/breakdown/${encodeURIComponent(model.project.id)}/progress`);
          if (d && d.ok && model.project) {
            model.project.running = !!d.running;
            model.project.progress = d.progress;
            model.project.status = d.status;
            paint();
            if (!d.running) {
              stopPoll();
              await loadProject(model.project.id, { keepScreen: true });
              return;
            }
          }
        } catch { /* 轮询失败静默，下一轮再试 */ }
        model.pollTimer = setTimeout(tick, 2000);
      };
      model.pollTimer = setTimeout(tick, 800);
    } else if (!running && model.pollTimer) {
      stopPoll();
    }
  }

  function stopPoll() {
    if (model.pollTimer) { clearTimeout(model.pollTimer); model.pollTimer = null; }
  }

  /* ---------------- 动作 ---------------- */

  async function doUpload(file, title) {
    const form = new FormData();
    form.append('file', file);
    if (title) form.append('title', title);
    toast('正在上传并切章…');
    try {
      const d = await request('/api/breakdown/upload', { method: 'POST', form, timeout: 120000 });
      model.preview = d;
      model.project = null;
      setScreen(nextScreen(model.screen, { type: 'created' }));
      if ((d.warnings || []).length) toast(d.warnings[0], { tone: 'warn' });
    } catch (err) {
      toast(errorText(err, '上传失败'), { tone: 'danger' });
    }
  }

  async function doPaste(title, text) {
    try {
      const d = await request('/api/breakdown/paste', {
        method: 'POST', body: { title, text }, timeout: 120000
      });
      model.preview = d;
      model.project = null;
      setScreen(nextScreen(model.screen, { type: 'created' }));
      if ((d.warnings || []).length) toast(d.warnings[0], { tone: 'warn' });
    } catch (err) {
      toast(errorText(err, '创建失败'), { tone: 'danger' });
    }
  }

  async function doRun(id) {
    try {
      const d = await request(`/api/breakdown/${encodeURIComponent(id)}/run`, { method: 'POST' });
      toast(`拆解已启动：${(d.pending || 0)} 章待拆`);
      await loadProject(id, { keepScreen: true });
    } catch (err) {
      toast(errorText(err, '启动拆解失败'), { tone: 'danger' });
    }
  }

  async function doResplit(id) {
    const ok = await confirmDialog({
      title: '整体重切', danger: true,
      message: '重新执行自动切章？',
      detail: '章节状态、各章分析与全书报告都会清空（原文不会丢）。'
    });
    if (!ok) return;
    try {
      const d = await request(`/api/breakdown/${encodeURIComponent(id)}/resplit`, { method: 'POST', timeout: 60000 });
      model.preview = d;
      model.project = null;
      setScreen(nextScreen(model.screen, { type: 'resplit' }));
      toast('已重新切章');
    } catch (err) {
      toast(errorText(err, '重切失败'), { tone: 'danger' });
    }
  }

  async function doRetry(no) {
    const id = model.project && model.project.id;
    if (!id) return;
    toast(`正在重试第 ${no} 章…`);
    try {
      const d = await request(`/api/breakdown/${encodeURIComponent(id)}/chapter/${no}/retry`,
        { method: 'POST', timeout: 180000 });
      if (d.ok && d.chapter && d.chapter.state === 'done') toast(`第 ${no} 章拆解完成`, { tone: 'success' });
      else toast(errorText({ message: (d.note || '重试失败') }, '重试失败'), { tone: 'warn' });
    } catch (err) {
      toast(errorText(err, '重试失败'), { tone: 'danger' });
    }
    await loadProject(id, { keepScreen: true });
  }

  async function doReport() {
    const id = model.project && model.project.id;
    if (!id || model.reportBusy) return;
    model.reportBusy = true;
    model.tab = 'report';
    paint();
    toast('正在生成全书报告…');
    try {
      await request(`/api/breakdown/${encodeURIComponent(id)}/report`, { method: 'POST', timeout: 300000 });
      toast('全书报告已生成', { tone: 'success' });
      model.adoptChecked = new Set();
    } catch (err) {
      toast(errorText(err, '报告生成失败'), { tone: 'danger' });
    }
    model.reportBusy = false;
    await loadProject(id, { keepScreen: true });
    paint();
  }

  async function doAdopt() {
    const id = model.project && model.project.id;
    const report = model.project && model.project.report;
    if (!id || !report) return;
    const payload = buildAdoptPayload(report, Array.from(model.adoptChecked), '');
    if (!payload.indices.length) { toast('请先勾选要入账的手法', { tone: 'warn' }); return; }
    try {
      const d = await request(`/api/breakdown/${encodeURIComponent(id)}/adopt-techniques`,
        { method: 'POST', body: payload });
      toast(`已入账 ${d.adopted} 条手法${(d.dropped || []).length ? `，${d.dropped.length} 条被拒（见明细）` : ''}`,
        { tone: 'success' });
      await loadProject(id, { keepScreen: true });
    } catch (err) {
      toast(errorText(err, '手法入账失败'), { tone: 'danger' });
    }
  }

  async function doDelete(id) {
    const ok = await confirmDialog({
      title: '删除拆书项目', danger: true,
      message: '删除这个拆书项目？', detail: '项目文件与手法账本一并删除，不可恢复。'
    });
    if (!ok) return;
    try {
      await request(`/api/breakdown/${encodeURIComponent(id)}`, { method: 'DELETE' });
      toast('已删除', { tone: 'success' });
      await loadList();
    } catch (err) {
      toast(errorText(err, '删除失败'), { tone: 'danger' });
    }
  }

  /* ---------------- 渲染 ---------------- */

  function badgeEl(state) {
    const b = chapterBadge(state);
    return h('span', { class: `bd-badge bd-badge-${b.tone}`, text: b.label });
  }

  function renderList() {
    const box = h('div', { class: 'bd-list' });
    box.append(h('div', { class: 'evo-head' },
      h('div', { class: 'evo-head-copy' },
        h('h1', { text: '拆书台' }),
        h('p', { class: 'hint-text', text: '上传或粘贴外部作品，自动切章、逐章拆解成结构化笔记；全书报告里的可复用手法可一键入账本。' })),
      h('button', {
        class: 'btn btn-primary', type: 'button',
        html: `${icon('plus', 14)}<span>新建拆书</span>`,
        on: { click: () => setScreen(nextScreen(model.screen, { type: 'new' })) }
      })));
    if (!model.projects.length) {
      box.append(h('div', { class: 'bd-empty' },
        h('div', { class: 'bd-empty-icon', html: icon('book', 26) }),
        h('p', { text: '还没有拆书项目' }),
        h('p', { class: 'hint-text', text: '支持 txt / md / docx / epub，上传后自动切章' })));
      return box;
    }
    for (const p of model.projects) {
      const meta = [`${fmtNum(p.chapters)} 章`, `${fmtNum(p.chars || 0)} 字`];
      if (p.done) meta.unshift(`已拆 ${p.done}/${p.chapters}`);
      if (p.failed) meta.push(`${p.failed} 章失败`);
      if (p.has_report) meta.push('有报告');
      box.append(h('div', { class: 'bd-project-card' + (p.running ? ' bd-project-running' : '') },
        h('div', { class: 'bd-project-main' },
          h('button', {
            class: 'bd-project-title', type: 'button', text: p.title || p.id,
            on: { click: () => { model.selected = 1; loadProject(p.id, { keepScreen: true }); } }
          }),
          h('div', { class: 'bd-project-meta' },
            p.running ? badgeEl('running') : badgeEl(p.status),
            h('span', { class: 'hint-text', text: meta.join(' · ') })),
          h('div', { class: 'hint-text', text: p.updated_at || '' })),
        h('div', { class: 'bd-project-actions' },
          h('button', {
            class: 'btn btn-sm', type: 'button', text: '打开',
            on: { click: () => { model.selected = 1; loadProject(p.id, { keepScreen: true }); } }
          }),
          h('button', {
            class: 'btn btn-sm bd-btn-danger-ghost', type: 'button', html: `${icon('trash', 13)}<span>删除</span>`,
            on: { click: () => doDelete(p.id) }
          }))));
    }
    return box;
  }

  function renderCreate() {
    const fileInput = h('input', {
      class: 'input', type: 'file',
      accept: '.txt,.md,.markdown,.docx,.epub,.html,.htm,.pdf'
    });
    const uploadTitle = h('input', { class: 'input', type: 'text', placeholder: '书名（可留空，默认取文件名）' });
    const uploadBtn = h('button', {
      class: 'btn btn-primary', type: 'button', text: '上传并切章',
      on: {
        click: () => {
          const f = fileInput.files && fileInput.files[0];
          if (!f) { toast('请先选择文件', { tone: 'warn' }); return; }
          doUpload(f, uploadTitle.value.trim());
        }
      }
    });
    const pasteTitle = h('input', { class: 'input', type: 'text', placeholder: '书名' });
    const pasteText = h('textarea', {
      class: 'input bd-textarea', rows: '10',
      placeholder: '粘贴整本文本（自动切章：第N章/回/节）…'
    });
    const pasteBtn = h('button', {
      class: 'btn btn-primary', type: 'button', text: '创建拆书项目',
      on: {
        click: () => {
          const t = pasteText.value || '';
          if (t.trim().length < 20) { toast('正文太短（少于 20 字）', { tone: 'warn' }); return; }
          doPaste(pasteTitle.value.trim(), t);
        }
      }
    });

    return h('div', { class: 'bd-create' },
      h('div', { class: 'evo-head' },
        h('div', { class: 'evo-head-copy' }, h('h1', { text: '新建拆书' })),
        h('button', { class: 'btn', type: 'button', text: '返回列表', on: { click: () => setScreen(nextScreen(model.screen, { type: 'back' })) } })),
      h('div', { class: 'bd-create-grid' },
        h('section', { class: 'bd-panel' },
          h('h3', { text: '上传文件' }),
          h('p', { class: 'hint-text', text: 'txt / md / docx / epub / html / pdf，≤30MB；安全校验与参考上传同口径。' }),
          h('div', { class: 'bd-field' }, h('label', { text: '书名' }), uploadTitle),
          h('div', { class: 'bd-field' }, h('label', { text: '文件' }), fileInput),
          h('div', { class: 'bd-actions' }, uploadBtn)),
        h('section', { class: 'bd-panel' },
          h('h3', { text: '粘贴文本' }),
          h('p', { class: 'hint-text', text: '直接粘贴整本文本；识别不到章节标记时整本按单章处理。' }),
          h('div', { class: 'bd-field' }, h('label', { text: '书名' }), pasteTitle),
          pasteText,
          h('div', { class: 'bd-actions' }, pasteBtn))));
  }

  function renderPreview() {
    const p = model.preview;
    if (!p) return renderCreate();
    const rows = splitPreviewRows(p.preview || p.chapters, 12);
    const tbl = h('div', { class: 'bd-preview-table' },
      h('div', { class: 'bd-preview-head' },
        h('span', { text: '序号' }), h('span', { text: '章标题' }),
        h('span', { text: '卷' }), h('span', { text: '字数' }), h('span', { text: '' })),
      ...rows.map((r) => h('div', { class: 'bd-preview-row' },
        h('span', { text: `#${r.no}` }),
        h('span', { class: 'bd-preview-title', text: r.title }),
        h('span', { class: 'hint-text', text: r.volume || '—' }),
        h('span', { text: fmtNum(r.chars) }),
        r.golden ? h('span', { class: 'bd-tag bd-tag-golden', text: '黄金三章' }) : h('span', { text: '' }))));

    return h('div', { class: 'bd-create' },
      h('div', { class: 'evo-head' },
        h('div', { class: 'evo-head-copy' },
          h('h1', { text: '切章预览' }),
          h('p', {
            class: 'hint-text',
            text: `「${p.title || ''}」共 ${p.volumeCount ? p.volumeCount + ' 卷 · ' : ''}${fmtNum(p.chapterCount)} 章 · 约 ${fmtNum(p.chars || 0)} 字`
          })),
        h('button', { class: 'btn', type: 'button', text: '返回列表', on: { click: () => setScreen(nextScreen(model.screen, { type: 'back' })) } })),
      (p.warnings || []).length ? h('div', { class: 'bd-warn-box' },
        ...(p.warnings || []).map((w) => h('p', { text: w }))) : null,
      (p.extractWarnings || []).length ? h('div', { class: 'bd-warn-box bd-warn-soft' },
        ...(p.extractWarnings || []).map((w) => h('p', { text: w }))) : null,
      tbl,
      rows.length === 12 && p.chapterCount > 12
        ? h('p', { class: 'hint-text', text: `仅展示前 12 章，其余 ${p.chapterCount - 12} 章进入工作台后可见` }) : null,
      h('div', { class: 'bd-actions bd-actions-main' },
        h('button', {
          class: 'btn', type: 'button', text: '整体重切',
          on: { click: () => doResplit(p.id) }
        }),
        h('button', {
          class: 'btn btn-primary', type: 'button',
          html: `${icon('play', 13)}<span>进入工作台</span>`,
          on: { click: () => { model.selected = 1; loadProject(p.id, { keepScreen: true }); } }
        })));
  }

  function renderChapterPane() {
    const bd = model.project;
    const chapter = (bd.chapters || []).find((c) => c.no === model.selected) || bd.chapters[0];
    if (!chapter) return h('div', { class: 'bd-empty', text: '没有章节' });
    const orig = renderOriginalText(chapter.paras || []);
    const cards = renderAnalysisCards(chapter.analysis, {
      onAnchor: (s, e) => highlightParas(orig, s, e)
    });
    return h('div', { class: 'bd-worksplit' },
      h('div', { class: 'bd-cards-col' },
        chapter.state === 'failed'
          ? h('div', { class: 'bd-warn-box' },
            h('p', { text: `本章拆解失败：${chapter.error || '未知错误'}` }),
            h('div', { class: 'bd-actions' },
              h('button', { class: 'btn btn-sm', type: 'button', text: '重试本章', on: { click: () => doRetry(chapter.no) } })))
          : null,
        chapter.state === 'running'
          ? h('div', { class: 'bd-warn-box bd-warn-soft' }, h('p', { text: '本章拆解中…' })) : null,
        !chapter.analysis && chapter.state === 'pending'
          ? h('div', { class: 'bd-empty bd-empty-sm' },
            h('p', { text: '本章还没有拆解结果' }),
            h('button', {
              class: 'btn btn-primary btn-sm', type: 'button', text: '开始拆解',
              on: { click: () => doRun(bd.id) }
            })) : null,
        cards),
      h('div', { class: 'bd-original-col' },
        h('h4', { class: 'bd-card-title', text: '原文对照' }),
        orig));
  }

  function pacingCurveSvg(curve) {
    const pts = (Array.isArray(curve) ? curve : []).filter((x) => x && x.no && x.score);
    if (!pts.length) return null;
    const w = 560; const hpx = 120; const pad = 14;
    const innerW = w - pad * 2; const innerH = hpx - pad * 2;
    const maxNo = Math.max(...pts.map((p) => p.no), 1);
    const xy = pts.map((p) => ({
      x: pad + (innerW * (p.no - 1)) / Math.max(1, maxNo - 1 || 1),
      y: pad + innerH - ((p.score - 1) / 4) * innerH
    }));
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${w} ${hpx}`);
    svg.setAttribute('class', 'bd-curve');
    const mk = (tag, attrs) => {
      const el = document.createElementNS(NS, tag);
      for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
      return el;
    };
    svg.append(mk('polyline', {
      points: xy.map((p) => `${p.x},${p.y}`).join(' '),
      fill: 'none', stroke: 'var(--accent)', 'stroke-width': 2,
      'stroke-linejoin': 'round', 'stroke-linecap': 'round'
    }));
    for (const p of xy) svg.append(mk('circle', { cx: p.x, cy: p.y, r: 3, fill: 'var(--accent)' }));
    return svg;
  }

  function renderReportPane() {
    const bd = model.project;
    const rep = bd.report;
    if (!rep) {
      return h('div', { class: 'bd-empty bd-empty-sm' },
        h('p', { text: '还没有全书报告' }),
        h('p', { class: 'hint-text', text: '至少拆解完成一章后即可生成（聚合各章笔记再做一次分析）' }),
        h('button', {
          class: 'btn btn-primary btn-sm', type: 'button', text: '生成全书报告',
          on: { click: () => doReport() }
        }));
    }
    const box = h('div', { class: 'bd-report' });
    if (rep.editor_comment) {
      const sec = h('section', { class: 'bd-card' }, h('h4', { class: 'bd-card-title', text: '编辑点评' }));
      sec.append(renderMarkdownInto(h('div', { class: 'bd-md' }), rep.editor_comment));
      box.append(sec);
    }
    if (rep.outline_restore) {
      const sec = h('section', { class: 'bd-card' }, h('h4', { class: 'bd-card-title', text: '大纲还原' }));
      sec.append(renderMarkdownInto(h('div', { class: 'bd-md' }), rep.outline_restore));
      box.append(sec);
    }
    const arcs = rep.character_arcs || [];
    if (arcs.length) {
      const sec = h('section', { class: 'bd-card' }, h('h4', { class: 'bd-card-title', text: '人物弧光' }));
      for (const a of arcs) sec.append(h('p', { class: 'bd-line', html: `<b>${a.name}</b>：${a.arc}` }));
      box.append(sec);
    }
    const fore = rep.foreshadow || [];
    if (fore.length) {
      const sec = h('section', { class: 'bd-card' }, h('h4', { class: 'bd-card-title', text: '伏笔账本' }));
      for (const f of fore) {
        sec.append(h('p', { class: 'bd-line', text: `${f.item}（埋：${f.plant || '?'}；收：${f.payoff || '未回收'}）` }));
      }
      box.append(sec);
    }
    const curve = pacingCurveSvg(rep.pacing_curve);
    if (curve) {
      const sec = h('section', { class: 'bd-card' }, h('h4', { class: 'bd-card-title', text: '节奏曲线' }), curve);
      box.append(sec);
    }
    const techs = rep.techniques || [];
    if (techs.length) {
      const sec = h('section', { class: 'bd-card' },
        h('h4', { class: 'bd-card-title', text: '可复用手法（勾选后入账本）' }));
      techs.forEach((t, i) => {
        const cb = h('input', { type: 'checkbox', checked: model.adoptChecked.has(i) });
        cb.addEventListener('change', () => {
          if (cb.checked) model.adoptChecked.add(i); else model.adoptChecked.delete(i);
        });
        const already = (bd.adopted_techniques || []).some((a) => a.name === t.name && a.chapter === t.chapter);
        sec.append(h('label', { class: 'bd-tech' + (already ? ' bd-tech-adopted' : '') },
          cb,
          h('span', { class: 'bd-tech-main' },
            h('b', { text: t.name }),
            h('span', { class: 'bd-tech-desc', text: t.desc }),
            h('span', { class: 'bd-tech-evidence', text: `证据（第${t.chapter}章 段${t.startPara}-${t.endPara}）：「${t.evidence}」` })),
          already ? h('span', { class: 'bd-tag bd-tag-ok', text: '已入账' }) : null));
      });
      const drop = rep.dropped_techniques || [];
      if (drop.length) {
        sec.append(h('p', {
          class: 'hint-text',
          text: `另有 ${drop.length} 条候选手法因证据未逐字命中原文被丢弃：${drop.map((d) => d.name).join('、')}`
        }));
      }
      sec.append(h('div', { class: 'bd-actions' },
        h('button', {
          class: 'btn btn-primary', type: 'button',
          html: `${icon('check', 13)}<span>手法入账本（${model.adoptChecked.size}）</span>`,
          on: { click: () => doAdopt() }
        })));
      box.append(sec);
    }
    return box;
  }

  function renderWork() {
    const bd = model.project;
    if (!bd) return renderList();
    const pg = bd.progress || {};
    const head = h('div', { class: 'evo-head bd-work-head' },
      h('div', { class: 'evo-head-copy' },
        h('h1', { text: bd.title || '拆书工作台' }),
        h('p', { class: 'hint-text', text: progressText(pg) })),
      h('div', { class: 'bd-head-actions' },
        badgeEl(bd.running ? 'running' : bd.status),
        h('button', {
          class: 'btn', type: 'button', text: '返回列表',
          on: { click: () => { stopPoll(); model.project = null; setScreen(nextScreen(model.screen, { type: 'back' })); } }
        })));
    const progress = h('div', { class: 'bd-progress' },
      h('div', { class: 'bd-progress-bar' },
        h('div', {
          class: 'bd-progress-fill',
          style: { width: `${pg.total ? Math.round(((pg.done || 0) / pg.total) * 100) : 0}%` }
        })),
      h('div', { class: 'bd-actions' },
        h('button', {
          class: 'btn btn-primary', type: 'button', text: bd.running ? '拆解进行中…' : '开始拆解',
          disabled: !!bd.running,
          on: { click: () => doRun(bd.id) }
        }),
        h('button', {
          class: 'btn', type: 'button', text: '重新生成报告',
          on: { click: () => doReport() }
        })));

    const left = h('div', { class: 'bd-chapters' });
    for (const c of bd.chapters || []) {
      const row = h('div', {
        class: 'bd-chapter-row' + (c.no === model.selected ? ' active' : ''),
        role: 'button', tabindex: '0',
        on: { click: () => { model.selected = c.no; paint(); } }
      },
      h('span', { class: 'bd-chapter-no', text: `#${c.no}` }),
      h('span', { class: 'bd-chapter-title', text: c.title }),
      c.golden ? h('span', { class: 'bd-tag bd-tag-golden', text: '黄金' }) : null,
      h('span', { class: 'hint-text', text: `${fmtNum(c.chars || 0)}字` }),
      badgeEl(c.state),
      c.state === 'failed'
        ? h('button', {
          class: 'bd-mini bd-mini-retry', type: 'button', text: '重试',
          on: { click: (ev) => { ev.stopPropagation(); doRetry(c.no); } }
        }) : null);
      left.append(row);
    }

    const tabs = h('div', { class: 'bd-tabs' },
      h('button', {
        class: 'bd-tab' + (model.tab === 'chapter' ? ' active' : ''), type: 'button', text: '章节分析',
        on: { click: () => { model.tab = 'chapter'; paint(); } }
      }),
      h('button', {
        class: 'bd-tab' + (model.tab === 'report' ? ' active' : ''), type: 'button', text: '全书报告',
        on: { click: () => { model.tab = 'report'; paint(); } }
      }));

    const right = h('div', { class: 'bd-right' },
      tabs,
      model.tab === 'chapter' ? renderChapterPane() : renderReportPane());

    return h('div', { class: 'bd-work' }, head, progress,
      h('div', { class: 'bd-worksplit bd-worksplit-main' }, left, right));
  }

  function paint() {
    if (model.destroyed) return;
    pad.textContent = '';
    if (model.screen === 'list') pad.append(renderList());
    else if (model.screen === 'create') pad.append(renderCreate());
    else if (model.screen === 'preview') pad.append(renderPreview());
    else if (model.screen === 'work') pad.append(renderWork());
  }

  paint();
  loadList();

  return {
    el: view,
    refresh: () => (model.screen === 'work' && model.project
      ? loadProject(model.project.id) : loadList()),
    destroy() {
      model.destroyed = true;
      stopPoll();
      view.remove();
    }
  };
}
