/* 章节工作台（v5.1.0 写作台编辑器内核）：段落编辑器 + 160ms 自动落盘 +
   排版可调 + 打字机滚动/段落聚焦 + 划词 AI + 节流版本历史。

   正文纯净域（铁律 0）不变式：
   · 编辑器装「纯净正文」（章题+引子+正文口径由 /api/chapter cleanText 提供）；
   · 复制全文实时过 cleanChapterText（与后端 bot/purity.py 同口径）；
   · 划词替换只动正文内容，编辑器内不新增任何元信息小节；
   · 导出整本仍走 /api/export（逐章去噪），工作文件（含概要/备注）只在
     「工作文件」视图核对，不参与复制与导出。 */

import { h, icon, fmtWords, fmtDate, relTime, copyText } from '../core/dom.js';
import { store } from '../core/state.js';
import { api, request, errorText } from '../core/api.js';
import { toast, confirmDialog, openModal, popoverMenu } from '../core/ui.js';
import { navigate } from '../core/router.js';
import { cleanChapterText } from '../core/purity.js';
import { loadCss } from '../core/loadcss.js';
import { createEditor } from '../core/editor.js';
import { mountSelectionBar } from '../core/selectionbar.js';
import { mountChatFloat } from '../components/chatfloat.js';

/* v5.1.0：bench 编辑器样式按需注入（静态资源 no-store，无需版本参数）。 */
loadCss('/v2/css/bench-editor.css');

/* 排版设置：localStorage `ew-bench-typography`，CSS 变量挂纸面容器。 */
const TYPO_KEY = 'ew-bench-typography';
const TYPO_DEFAULTS = {
  font: 'song',          /* song=宋体阅读 / kai=楷体·霞鹜文楷 / hei=黑体 / fang=仿宋 / sys=系统 */
  fontSize: 17,          /* 12–28 */
  lineHeight: 1.9,       /* 1.4–3.0 */
  width: 92,             /* 行宽 30–100% */
  paraGap: true,         /* 段间空行 */
  indent: false,         /* 首行缩进两字符 */
  ruler: 'none',         /* none / solid / dashed */
  typewriter: false,     /* 打字机滚动 */
  paraFocus: false       /* 段落聚焦 */
};
/* 全部系统栈降级：霞鹜文楷未安装时落到楷体/宋体，不引外部字体文件（无构建无 CDN）。 */
const TYPO_FONTS = [
  ['song', '宋体阅读', '"Songti SC", "Noto Serif CJK SC", "Source Han Serif SC", "STSong", "SimSun", serif'],
  ['kai', '楷体·文楷', '"LXGW WenKai", "霞鹜文楷", "LXGW WenKai GB", "Kaiti SC", "KaiTi", "STKaiti", serif'],
  ['hei', '黑体', '-apple-system, "PingFang SC", "HarmonyOS Sans SC", "Microsoft YaHei", sans-serif'],
  ['fang', '仿宋', '"Fangsong SC", "FangSong", "STFangsong", "仿宋", serif'],
  ['sys', '系统默认', 'var(--font-ui)']
];

function loadTypo() {
  try {
    const raw = JSON.parse(localStorage.getItem(TYPO_KEY) || '{}');
    return { ...TYPO_DEFAULTS, ...(raw && typeof raw === 'object' ? raw : {}) };
  } catch {
    return { ...TYPO_DEFAULTS };
  }
}

function saveTypo(typo) {
  try {
    localStorage.setItem(TYPO_KEY, JSON.stringify(typo));
  } catch {
    /* 隐私模式：排版只活在本次会话 */
  }
}

/* 从工作文件里抽「本章概要」，供划词 AI 上下文（只读，不进编辑器）。 */
function extractSummary(raw) {
  const m = String(raw || '').match(/^##\s*本章概要\s*\n+([\s\S]*?)(?=\n##\s|\n*$)/m);
  return m ? m[1].trim().slice(0, 400) : '';
}

export function renderBench(container, ctx) {
  const state0 = store.get();
  const key = state0.route.key || state0.currentKey;
  const n = Number(state0.route.chapter) || 1;

  const view = h('div', { class: 'bench-view' });
  container.append(view);

  const model = {
    n,
    title: '',
    text: '',
    rawText: '',
    mode: 'prose',
    savedText: '',
    words: 0,
    updatedAt: '',
    versions: [],
    loading: true,
    error: '',
    dirty: false,
    saveState: 'saved'     /* saved / pending / saving / error */
  };

  /* v5.1.0 保存管线状态：160ms 防抖 + 组词保护 + 互斥补跑 */
  let saveTimer = 0;
  let saving = false;
  let pendingSave = false;
  let lastVersionsRefresh = 0;

  let typo = loadTypo();

  /* -------------------------------------------------------------- head */

  const head = h('div', { class: 'bench-head' });
  /* v3.2.0：小说章节工作台也有对话改稿（与其他文体同一套副驾，目标定位到当前章） */
  const chatCtx = { doc: String(n), block: null, title: '' };
  const chatFloat = mountChatFloat({
    getContext: () => ({
      key,
      medium: 'novel',
      doc: String(model.n || n),
      block: chatCtx.block,
      docTitle: model.title ? `第 ${model.n} 章 · ${model.title}` : `第 ${model.n} 章`
    }),
    onApplied: () => {
      /* 应用后正文已变：重新拉章节 */
      loadChapter();
    }
  });
  const saveBtn = h('button', {
    class: 'btn btn-primary btn-sm bench-save',
    type: 'button',
    text: '保存',
    disabled: true,
    dataset: { state: 'saved' },
    on: { click: () => flushSave() }
  });
  view.append(head);

  const body = h('div', { class: 'bench-body' });
  const editorCol = h('div', { class: 'bench-editor' });
  /* v5.1.0：原生 textarea → 段落编辑器（core/editor.js，纯文本 \n\n 分段模型）。
     滚动容器是 .bench-editor-body，编辑器根挂在其内；打字机滚动以它为基准。 */
  const scrollHost = h('div', { class: 'bench-editor-body' });
  const foot = h('div', { class: 'bench-foot' });
  const paper = h('div', { class: 'bench-paper' });
  paper.append(scrollHost, foot);
  editorCol.append(paper);

  const side = h('div', { class: 'bench-side' });
  body.append(editorCol, side);
  view.append(body);

  /* foot 常驻节点：输入路径只改 textContent，不整行重画（长章输入不卡）。 */
  const charsEl = h('span', { text: '0 字符' });
  const wordsEl = h('span', { class: 'sep' });
  const updatedEl = h('span', { class: 'sep' });
  const saveStateEl = h('span', { class: 'bench-save-state', dataset: { state: 'saved' } });
  const dirtyHint = h('span', { class: 'hint-text', text: '' });
  foot.append(
    h('div', { class: 'bench-foot-info' }, charsEl, wordsEl, updatedEl),
    h('span', { style: { flex: '1' } }),
    saveStateEl,
    dirtyHint
  );

  const editor = createEditor({
    host: scrollHost,
    placeholder: '这一章还没有正文。可以直接粘贴或开始写作，改动会自动保存并形成版本快照。',
    onChange: (text) => {
      markDirty(text);
      scheduleSave();
      schedulePhoneFill();
    },
    onCompositionEnd: () => {
      /* 拼音组词结束：把组词期间欠下的落盘补上 */
      scheduleSave();
      schedulePhoneFill();
    }
  });

  /* 划词气泡：选中文字浮出六动作菜单，流式预览不落稿，替换前校验原选区未变。 */
  const selectionBar = mountSelectionBar({
    editor,
    getContext: () => ({
      key,
      n: model.n,
      title: model.title ? `第 ${model.n} 章 ${model.title}` : `第 ${model.n} 章`,
      summary: extractSummary(model.rawText)
    }),
    onReplaced: () => {
      /* 替换成功：立即落盘（后端写盘门禁会留「编辑前」快照 + 节流快照） */
      flushSave();
    }
  });

  applyTypo();

  /* ------------------------------------------------------------ paint */

  const EXPORT_FMTS = [
    ['md', 'Markdown'],
    ['docx', 'Word'],
    ['epub', 'EPUB'],
    ['print', '打印排版']
  ];

  function exportMenu(anchor) {
    const items = EXPORT_FMTS.map(([fmt, label]) => ({
      label: `导出全书 · ${label}`,
      icon: 'download',
      onSelect: async () => {
        try {
          toast(`正在导出 ${label}（正文已去噪）…`, { duration: 2400 });
          const r = await api.exportWork(key, fmt);
          if (r && r.cancelled) return;
          const where = r && r.saved ? `已保存到 ${r.saved}` : `${label} 已开始下载`;
          toast(where, { tone: 'success', duration: 3200 });
        } catch (err) {
          toast(errorText(err, '导出失败'), { tone: 'danger', duration: 4000 });
        }
      }
    }));
    popoverMenu(anchor, items, { label: '导出整本（仅正文）' });
  }

  /* 正文 / 工作文件 切换（v4.22.2 沿用）。只在两者确实不同时出现。 */
  const modeSwitch = h('div', { class: 'bench-mode', role: 'tablist', style: { display: 'none' } });
  function paintModeSwitch() {
    const differ = (model.rawText || '') !== (model.text || '');
    modeSwitch.style.display = differ ? '' : 'none';
    modeSwitch.textContent = '';
    if (!differ) return;
    for (const [m, label, tip] of [
      ['prose', '正文', '章题 + 章首引子 + 正文（默认，可复制/可发布）'],
      ['raw', '工作文件', '含本章概要 / 章节备注 / 历史残留，仅供核对']
    ]) {
      modeSwitch.append(h('button', {
        class: `bench-mode-btn${model.mode === m ? ' is-on' : ''}`,
        type: 'button',
        role: 'tab',
        'aria-selected': model.mode === m ? 'true' : 'false',
        title: tip,
        text: label,
        dataset: { mode: m },
        on: {
          click: () => {
            if (model.mode === m) return;
            if (model.dirty) {
              toast('先保存当前改动，再切换视图', { tone: 'danger', duration: 2200 });
              return;
            }
            model.mode = m;
            editor.setText(m === 'raw' ? model.rawText : model.text, { silent: true });
            model.savedText = editor.getText();
            model.dirty = false;
            paintModeSwitch();
            paintAll();
          }
        }
      }));
    }
  }

  function paintHead() {
    head.textContent = '';
    const work = store.get().work;
    head.append(
      h('button', {
        class: 'icon-btn',
        type: 'button',
        'aria-label': '返回对话',
        title: '返回对话',
        html: icon('arrow-left', 16),
        on: { click: () => navigate(`/work/${encodeURIComponent(key)}`) }
      }),
      h(
        'div',
        { class: 'bench-title' },
        h('span', { class: 'bt-name', text: model.title || `第 ${model.n} 章` }),
        h('span', {
          class: 'bt-sub',
          text: work ? `${work.name} · 第 ${model.n} / ${work.total || '?'} 章` : `第 ${model.n} 章`
        })
      ),
      h('span', { style: { flex: '1' } }),
      modeSwitch,
      /* v5.1.0：排版面板入口 */
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        title: '排版设置（字体/字号/行高/行宽/网格线/打字机）',
        html: `${icon('type', 13)}<span>排版</span>`,
        on: { click: (event) => openTypoPanel(event.currentTarget) }
      }),
      /* v5.1.0：手机阅读预览抽屉 */
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        title: '手机阅读预览（滚动与编辑器同步）',
        html: `${icon('eye', 13)}<span>预览</span>`,
        on: { click: () => togglePhone() }
      }),
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        title: '导出整本（章题 + 章首引子 + 正文，不含任何概要/备注/修改说明/评分）',
        html: `${icon('download', 13)}<span>导出全书</span>`,
        on: { click: (event) => exportMenu(event.currentTarget) }
      }),
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        html: `${icon('copy', 13)}<span>复制全文</span>`,
        on: {
          click: async () => {
            try {
              /* v4.22.1 口径不变：复制「章题+引子+正文」（剥全部元信息小节），基于编辑器实时内容 */
              const raw = cleanChapterText(editor.getText()) || editor.getText();
              await copyText(raw);
              toast('已复制到剪贴板（章题+引子+正文，不含概要与任何说明/评分）', { tone: 'success', duration: 2400 });
            } catch {
              toast('复制失败，请手动选择文本', { tone: 'danger' });
            }
          }
        }
      }),
      saveBtn
    );
  }

  /* foot 里的计数与保存状态节点常驻（输入路径只改 textContent，不整行重画）。 */

  function paintFoot() {
    wordsEl.textContent = '';
    updatedEl.textContent = '';
    wordsEl.append(model.words ? h('span', { text: `记录字数 ${fmtWords(model.words)}` }) : null);
    updatedEl.append(model.updatedAt ? h('span', { text: `上次保存 ${fmtDate(model.updatedAt, true)}` }) : null);
    paintSaveState();
  }

  function paintSaveState() {
    /* v5.1.0 保存状态图标：数据库=已存 / 时钟=待存 / 转圈=保存中 / 叹号=失败 */
    const st = model.saveState;
    const map = {
      saved: ['database', '已存'],
      pending: ['clock', '待存'],
      saving: ['refresh', '保存中'],
      error: ['alert', '保存失败 · 点「保存」重试']
    };
    const [ic, label] = map[st] || map.saved;
    saveStateEl.dataset.state = st;
    saveStateEl.textContent = '';
    saveStateEl.append(h('span', { html: icon(ic, 12) }), h('span', { text: label }));
    dirtyHint.textContent = '';
    if (model.dirty || pendingSave) {
      dirtyHint.className = 'bench-dirty';
      dirtyHint.textContent = '有未保存改动 · Cmd/Ctrl+S 立即保存';
    } else {
      dirtyHint.className = 'hint-text';
      dirtyHint.textContent = '';
    }
    saveBtn.dataset.state = st;
    saveBtn.disabled = model.loading || (!model.dirty && !pendingSave) || saving;
    saveBtn.textContent = saving ? '保存中…' : (model.dirty || pendingSave) ? '保存改动' : '已保存';
  }

  function paintSide() {
    side.textContent = '';
    side.append(
      h(
        'div',
        { class: 'bench-side-head' },
        h('span', { text: '版本历史' }),
        h('span', { class: 'count', text: `${model.versions.length} 份` })
      )
    );
    const list = h('div', { class: 'bench-side-body' });
    if (!model.versions.length) {
      list.append(h('div', { class: 'hint-text', text: '还没有版本。第一次保存后按节流规则自动留档（距上版 5 分钟或改动 200 字），自动快照每章保留 20 份。' }));
    }
    for (const v of model.versions) {
      list.append(
        h(
          'div',
          { class: 'bench-version' },
          h(
            'div',
            { class: 'bv-top' },
            h('span', { class: 'bv-label', text: v.label || v.id || '快照' }),
            h('span', {
              class: 'bv-meta',
              text: `${v.ts ? relTime(v.ts) : ''}${v.size ? ` · ${fmtWords(v.size)}` : ''}`
            })
          ),
          h(
            'div',
            { class: 'bv-actions' },
            h('button', { class: 'btn btn-sm', type: 'button', text: '对比', on: { click: () => showVersionModal(v) } }),
            h('button', {
              class: 'btn btn-sm',
              type: 'button',
              text: '恢复',
              on: {
                click: async () => {
                  const ok = await confirmDialog({
                    title: '恢复这个版本？',
                    message: '当前正文会先自动存一份「恢复前」快照，随时可以再切回来。',
                    confirmText: '恢复'
                  });
                  if (!ok) return;
                  try {
                    /* v5.1.0：走新恢复端点（PUT），恢复前服务端先落「恢复前」快照 */
                    await request('/api/chapter/versions/restore', {
                      method: 'PUT',
                      body: { key, n: model.n, id: v.id },
                      timeout: 60000
                    });
                    toast('已恢复该版本', { tone: 'success' });
                    await loadChapter();
                  } catch (err) {
                    toast(errorText(err, '恢复失败'), { tone: 'danger' });
                  }
                }
              }
            })
          )
        )
      );
    }
    side.append(list);
  }

  function paintAll() {
    paintHead();
    paintModeSwitch();
    paintFoot();
    paintSide();
    paintSaveState();
  }

  /* ------------------------------------------------------------- data */

  async function loadChapter() {
    model.loading = true;
    model.error = '';
    editor.setText('', { silent: true });
    scrollHost.style.opacity = '0.5';
    paintAll();
    try {
      const [ch, vers] = await Promise.all([api.chapter(key, model.n), api.chapterVersions(key, model.n)]);
      model.title = ch.title || '';
      /* v4.22.2 沿用：编辑器默认装「纯净正文」；工作文件收进 rawText 供核对；
         保存时后端会把概要/备注补回去，不会丢元信息。 */
      model.rawText = ch.rawText || ch.text || '';
      model.text = ch.cleanText || cleanChapterText(model.rawText) || model.rawText;
      model.savedText = model.mode === 'raw' ? model.rawText : model.text;
      model.words = ch.words || 0;
      model.updatedAt = ch.updatedAt || '';
      model.versions = (vers && vers.items) || [];
      editor.setText(model.mode === 'raw' ? model.rawText : model.text, { silent: true });
      markDirty(editor.getText());
    } catch (err) {
      model.error = errorText(err, '章节加载失败');
    }
    model.loading = false;
    model.dirty = false;
    model.saveState = 'saved';
    scrollHost.style.opacity = '';
    paintAll();
  }

  function markDirty(text) {
    const t = typeof text === 'string' ? text : editor.getText();
    model.dirty = t !== model.savedText;
    charsEl.textContent = `${t.length.toLocaleString('zh-CN')} 字符`;
    if (!model.dirty && !pendingSave) model.saveState = 'saved';
    paintSaveState();
  }

  function scheduleSave() {
    /* 160ms 防抖：连打不打盘；拼音组词期间由 compositionend 补触发。 */
    if (editor.isComposing()) return;
    clearTimeout(saveTimer);
    model.saveState = model.dirty || pendingSave ? 'pending' : model.saveState;
    paintSaveState();
    saveTimer = setTimeout(() => flushSave(false), 160);
  }

  async function flushSave() {
    if (editor.isComposing()) { pendingSave = true; return; }
    if (model.loading) return;
    if (saving) { pendingSave = true; return; }
    clearTimeout(saveTimer);
    if (!model.dirty && !pendingSave) { model.saveState = 'saved'; paintSaveState(); return; }
    await doSave();
  }

  async function doSave() {
    /* 保存互斥：上请求未返回期间再脏，只记 pendingSave，返回后补跑一次（合并脏标记）。 */
    saving = true;
    pendingSave = false;
    model.saveState = 'saving';
    paintSaveState();
    const text = editor.getText();
    try {
      const res = await api.saveChapter(key, model.n, text, model.mode);
      model.savedText = text;
      if (model.mode === 'prose') model.text = text; else model.rawText = text;
      model.dirty = false;
      model.saveState = pendingSave ? 'pending' : 'saved';
      model.words = (res && res.words) || model.words;
      model.updatedAt = (res && res.updatedAt) || new Date().toISOString();
      paintFoot();
      refreshVersionsSoon();
      ctx.refreshWork && ctx.refreshWork();
    } catch (err) {
      model.saveState = 'error';
      toast(errorText(err, '保存失败，请重试'), { tone: 'danger' });
    }
    saving = false;
    paintSaveState();
    if (pendingSave && model.dirty) {
      /* 补跑：把保存期间的新改动再落一次 */
      pendingSave = false;
      await doSave();
    } else {
      pendingSave = false;
    }
  }

  async function refreshVersionsSoon() {
    /* 版本列表刷新节流（5s）：边写边存高频落盘时不刷屏。 */
    const now = Date.now();
    if (now - lastVersionsRefresh < 5000) return;
    lastVersionsRefresh = now;
    try {
      const vers = await api.chapterVersions(key, model.n);
      model.versions = (vers && vers.items) || [];
      paintSide();
    } catch {
      /* 版本列表刷新失败不影响保存结果 */
    }
  }

  /* ------------------------------------------------------------- 排版面板 */

  let typoPanel = null;
  let typoOutside = null;

  function closeTypoPanel() {
    if (typoPanel) { typoPanel.remove(); typoPanel = null; }
    if (typoOutside) { document.removeEventListener('pointerdown', typoOutside, true); typoOutside = null; }
  }

  function applyTypo() {
    /* CSS 变量挂纸面容器 .bench-paper（手机预览 .bp-frame 也继承同一套排版）。 */
    const font = TYPO_FONTS.find((f) => f[0] === typo.font) || TYPO_FONTS[0];
    paper.style.setProperty('--editor-font-family', font[2]);
    paper.style.setProperty('--editor-font-size', `${typo.fontSize}px`);
    paper.style.setProperty('--editor-line-height', String(typo.lineHeight));
    paper.style.setProperty('--editor-width', `${typo.width}%`);
    paper.style.setProperty('--editor-para-gap', typo.paraGap ? '0.6em' : '0');
    paper.style.setProperty('--editor-indent', typo.indent ? '2em' : '0');
    paper.style.setProperty('--ruler-step', `${Math.round(typo.fontSize * typo.lineHeight)}px`);
    paper.dataset.ruler = typo.ruler;
    editor.setParagraphFocus(typo.paraFocus);
    editor.setTypewriter(typo.typewriter);
    if (typo.typewriter) editor.maybeScroll();
  }

  /* 面板不整体重建（拖滑杆时不丢拖拽状态）：每个 chip 组登记一个刷新器，
     setTypo 后就地更新 is-on 标记。 */
  const chipRefreshers = [];

  function setTypo(patch) {
    typo = { ...typo, ...patch };
    saveTypo(typo);
    applyTypo();
    for (const refresh of chipRefreshers) refresh();
  }

  function typoSlider(label, min, max, step, value, onInput, fmt) {
    const val = h('span', { class: 'bt-val', text: fmt(value) });
    const input = h('input', { type: 'range', min: String(min), max: String(max), step: String(step), value: String(value) });
    input.addEventListener('input', () => {
      const v = Number(input.value);
      val.textContent = fmt(v);
      onInput(v);
    });
    return h('div', { class: 'bt-row' }, h('span', { class: 'bt-label', text: label }), input, val);
  }

  function typoChips(label, options, getKey, onPick) {
    const chips = h('div', { class: 'bt-chips' });
    const btns = [];
    for (const [ckey, name] of options) {
      const b = h('button', {
        class: 'bt-chip',
        type: 'button',
        text: name,
        on: { click: () => onPick(ckey) }
      });
      btns.push([b, ckey]);
      chips.append(b);
    }
    chipRefreshers.push(() => {
      const cur = getKey();
      for (const [b, ckey] of btns) b.classList.toggle('is-on', cur === ckey);
    });
    return h('div', { class: 'bt-row' }, h('span', { class: 'bt-label', text: label }), chips);
  }

  function openTypoPanel(anchor) {
    closeTypoPanel();
    chipRefreshers.length = 0;
    const rect = (anchor && anchor.getBoundingClientRect()) || { right: 200, bottom: 60 };
    typoPanel = h('div', { class: 'bench-typo' });
    typoPanel.style.position = 'fixed';
    typoPanel.style.zIndex = '110';
    typoPanel.style.background = 'var(--bg-elevated)';
    typoPanel.style.border = '1px solid var(--line-3)';
    typoPanel.style.borderRadius = 'var(--radius-md)';
    typoPanel.style.boxShadow = '0 12px 36px rgba(9, 12, 20, 0.18)';
    typoPanel.append(
      typoChips('字体', TYPO_FONTS.map(([k, name]) => [k, name]), () => typo.font, (k) => setTypo({ font: k })),
      typoSlider('字号', 12, 28, 1, typo.fontSize, (v) => setTypo({ fontSize: v }), (v) => `${v}px`),
      typoSlider('行高', 1.4, 3.0, 0.05, typo.lineHeight, (v) => setTypo({ lineHeight: v }), (v) => v.toFixed(2)),
      typoSlider('行宽', 30, 100, 1, typo.width, (v) => setTypo({ width: v }), (v) => `${v}%`),
      typoChips('段间', [['on', '空行'], ['off', '紧凑']], () => (typo.paraGap ? 'on' : 'off'),
        (k) => setTypo({ paraGap: k === 'on' })),
      typoChips('缩进', [['on', '首行两字'], ['off', '无']], () => (typo.indent ? 'on' : 'off'),
        (k) => setTypo({ indent: k === 'on' })),
      typoChips('网格线', [['none', '无'], ['solid', '实线'], ['dashed', '虚线']], () => typo.ruler,
        (k) => setTypo({ ruler: k })),
      typoChips('打字机', [['on', '开'], ['off', '关']], () => (typo.typewriter ? 'on' : 'off'),
        (k) => setTypo({ typewriter: k === 'on' })),
      typoChips('聚焦', [['on', '段落聚焦'], ['off', '关']], () => (typo.paraFocus ? 'on' : 'off'),
        (k) => setTypo({ paraFocus: k === 'on' })),
      h('div', { class: 'bt-row' },
        h('span', { class: 'bt-label' }),
        h('button', {
          class: 'bt-chip', type: 'button', text: '恢复默认排版',
          on: { click: () => setTypo({ ...TYPO_DEFAULTS }) }
        })
      )
    );
    for (const refresh of chipRefreshers) refresh();
    document.body.append(typoPanel);
    const w = typoPanel.offsetWidth || 300;
    const left = Math.max(10, Math.min(rect.right - w, (window.innerWidth || 1200) - w - 10));
    typoPanel.style.left = `${left}px`;
    typoPanel.style.top = `${Math.min((rect.bottom || 60) + 8, (window.innerHeight || 800) - typoPanel.offsetHeight - 10)}px`;
    typoOutside = (event) => {
      if (typoPanel && typoPanel.contains(event.target)) return;
      if (anchor && anchor.contains(event.target)) return;
      closeTypoPanel();
    };
    document.addEventListener('pointerdown', typoOutside, true);
  }

  /* ------------------------------------------------------------- 手机预览抽屉 */

  let phoneEl = null;
  let phoneFrame = null;
  let phoneSyncing = false;
  let fillPhoneTimer = 0;

  function schedulePhoneFill() {
    /* 输入期间预览内容节流刷新（600ms），不抢主线程。 */
    if (!phoneFrame) return;
    clearTimeout(fillPhoneTimer);
    fillPhoneTimer = setTimeout(fillPhone, 600);
  }

  function togglePhone() {
    if (phoneEl) { closePhone(); return; }
    openPhone();
  }

  function closePhone() {
    if (phoneEl) { phoneEl.remove(); phoneEl = null; phoneFrame = null; }
  }

  function phoneParagraphs() {
    /* 预览 = 读者口径：剥章题与一切元信息小节（铁律 0），再按空行分段渲染。 */
    const body = cleanChapterText(editor.getText(), { keepTitle: false }) || '';
    return body.split(/\n{2,}/).filter((p) => p.trim()).map((p) => p.replace(/\n/g, ' '));
  }

  function fillPhone() {
    if (!phoneFrame) return;
    phoneFrame.textContent = '';
    for (const p of phoneParagraphs()) phoneFrame.append(h('p', { class: 'bp-para', text: p }));
    if (!phoneFrame.childElementCount) {
      phoneFrame.append(h('p', { class: 'bp-para hint-text', text: '（正文为空）' }));
    }
  }

  /* 滚动双向同步（带防回环标志）：编辑器滚 → 预览滚；预览滚 → 编辑器滚。
     监听挂 renderBench 作用域，destroy 时能精确摘除。 */
  function scrollRatio(el) {
    const max = el.scrollHeight - el.clientHeight;
    return max > 0 ? el.scrollTop / max : 0;
  }

  function onBenchScroll() {
    if (phoneSyncing || !phoneFrame) return;
    phoneSyncing = true;
    const max = phoneFrame.scrollHeight - phoneFrame.clientHeight;
    phoneFrame.scrollTop = max * scrollRatio(scrollHost);
    requestAnimationFrame(() => { phoneSyncing = false; });
  }

  function openPhone() {
    closeTypoPanel();
    phoneEl = h('div', { class: 'bench-phone', role: 'dialog', 'aria-label': '手机阅读预览' });
    const slider = h('input', {
      type: 'range', min: '320', max: '430', step: '1',
      value: String(typo.phoneWidth || 375),
      'aria-label': '手机屏宽'
    });
    phoneFrame = h('div', { class: 'bp-frame' });
    fillPhone();
    scrollHost.addEventListener('scroll', onBenchScroll);
    phoneFrame.addEventListener('scroll', () => {
      if (phoneSyncing) return;
      phoneSyncing = true;
      const max = scrollHost.scrollHeight - scrollHost.clientHeight;
      scrollHost.scrollTop = max * scrollRatio(phoneFrame);
      requestAnimationFrame(() => { phoneSyncing = false; });
    });
    slider.addEventListener('input', () => {
      phoneFrame.style.setProperty('--phone-width', `${slider.value}px`);
      typo.phoneWidth = Number(slider.value);
      saveTypo(typo);
    });
    phoneEl.append(
      h('div', { class: 'bp-bar' },
        h('span', { text: '手机阅读预览' }),
        slider,
        h('button', {
          class: 'btn btn-sm', type: 'button', text: '关闭',
          on: { click: closePhone }
        })
      ),
      phoneFrame
    );
    document.body.append(phoneEl);
  }

  /* ------------------------------------------------------------- 版本对比（内容 ⇄ 对比） */

  /* 自研逐行 + 行内字符级 diff：最长公共子序列（LCS）。行数/行宽都钳制防长章卡顿。 */
  function lcsOps(a, b, eq) {
    const n = a.length;
    const m = b.length;
    const dp = [];
    for (let i = 0; i <= n; i += 1) dp.push(new Int32Array(m + 1));
    for (let i = n - 1; i >= 0; i -= 1) {
      for (let j = m - 1; j >= 0; j -= 1) {
        dp[i][j] = eq(a[i], b[j]) ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    const ops = [];
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (eq(a[i], b[j])) { ops.push({ type: 'same', a: a[i], b: b[j] }); i += 1; j += 1; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push({ type: 'del', a: a[i] }); i += 1; }
      else { ops.push({ type: 'add', b: b[j] }); j += 1; }
    }
    while (i < n) { ops.push({ type: 'del', a: a[i] }); i += 1; }
    while (j < m) { ops.push({ type: 'add', b: b[j] }); j += 1; }
    return ops;
  }

  function inlineDiff(oldLine, newLine) {
    /* 行内字符级：把一行里的改动段用双色标出（del 段红 / add 段绿）。 */
    const MAXC = 320;
    const A = [...String(oldLine)].slice(0, MAXC);
    const B = [...String(newLine)].slice(0, MAXC);
    const ops = lcsOps(A, B, (x, y) => x === y);
    const segs = [];
    for (const op of ops) {
      const last = segs[segs.length - 1];
      if (last && last.type === op.type) {
        last.text += op.type === 'same' ? op.a : (op.type === 'del' ? op.a : op.b);
      } else {
        segs.push({ type: op.type, text: op.type === 'same' ? op.a : (op.type === 'del' ? op.a : op.b) });
      }
    }
    return segs;
  }

  function buildDiffView(oldText, newText) {
    const wrapEl = h('div', { class: 'diff-view' });
    const A = String(oldText || '').split('\n');
    const B = String(newText || '').split('\n');
    const ops = lcsOps(A, B, (x, y) => x === y);
    /* 相邻 del/add 配对做行内标记；没有变化的行灰字带出上下文。 */
    for (let k = 0; k < ops.length; k += 1) {
      const op = ops[k];
      if (op.type === 'same') {
        wrapEl.append(h('div', { class: 'diff-line meta' }, h('span', { class: 'dl-sign', text: '' }), h('span', { text: op.a || ' ' })));
        continue;
      }
      if (op.type === 'del') {
        const next = ops[k + 1];
        if (next && next.type === 'add') {
          k += 1;
          const segs = inlineDiff(op.a, next.b);
          wrapEl.append(h('div', { class: 'diff-line del' },
            h('span', { class: 'dl-sign', text: '-' }),
            h('span', {}, segs.filter((s) => s.type !== 'add').map((s) =>
              s.type === 'del' ? h('mark', { class: 'di-del', text: s.text }) : document.createTextNode(s.text)))),
            h('div', { class: 'diff-line add' },
              h('span', { class: 'dl-sign', text: '+' }),
              h('span', {}, segs.filter((s) => s.type !== 'del').map((s) =>
                s.type === 'add' ? h('mark', { class: 'di-add', text: s.text }) : document.createTextNode(s.text))))
          );
        } else {
          wrapEl.append(h('div', { class: 'diff-line del' }, h('span', { class: 'dl-sign', text: '-' }),
            h('span', { text: op.a || ' ' })));
        }
        continue;
      }
      /* add 没有相邻 del */
      wrapEl.append(h('div', { class: 'diff-line add' }, h('span', { class: 'dl-sign', text: '+' }),
        h('span', { text: op.b || ' ' })));
    }
    return wrapEl;
  }

  async function showVersionModal(v) {
    /* v5.1.0 历史版本弹窗：内容 ⇄ 对比 双视图（对比为本地 LCS 双色 diff）。 */
    let loaded = null;
    const tabs = h('div', { class: 'bench-mode', style: { marginBottom: '10px' } });
    const content = h('div', {});
    const modal = openModal({
      title: `版本：${v.label || v.id}${v.ts ? ` · ${v.ts}` : ''}`,
      body: h('div', {}, tabs, content),
      size: 'lg'
    });
    const paintTab = (which, text, diffTarget) => {
      tabs.textContent = '';
      content.textContent = '';
      for (const [w, label] of [['content', '内容'], ['diff', '对比']]) {
        tabs.append(h('button', {
          class: `bench-mode-btn${which === w ? ' is-on' : ''}`,
          type: 'button', text: label,
          on: { click: () => paintTab(w, text, diffTarget) }
        }));
      }
      if (which === 'content') {
        const pre = h('div', { class: 'diff-view', style: { fontFamily: 'var(--font-reading)', fontSize: '13px' } });
        for (const p of String(text || '').split(/\n{2,}/)) {
          pre.append(h('div', { class: 'diff-line', style: { color: 'var(--ink-primary)' } }, h('span', { text: p || ' ' })));
        }
        content.append(pre);
      } else {
        content.append(buildDiffView(text, diffTarget));
      }
    };
    try {
      const res = await request('/api/chapter/version', { query: { key, n: model.n, id: v.id } });
      loaded = String((res && res.text) || '');
      paintTab('content', loaded, editor.getText());
    } catch (err) {
      content.textContent = '';
      content.append(h('div', { class: 'msg-error', text: errorText(err, '版本内容加载失败') }));
    }
    return modal;
  }

  /* ------------------------------------------------------------- 全局键鼠 */

  const onKey = (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
      event.preventDefault();
      flushSave();
    }
  };
  document.addEventListener('keydown', onKey);

  const onBeforeUnload = (event) => {
    if (model.dirty || pendingSave) {
      /* 有未落盘改动：拦一次让用户确认（浏览器原生弹窗），不做后台补传
         （sendBeacon 只能 POST，与 PUT /api/chapter 语义不符，宁拦不猜）。 */
      event.preventDefault();
      event.returnValue = '';
    }
  };
  window.addEventListener('beforeunload', onBeforeUnload);

  /* ------------------------------------------------------------ start */

  paintAll();
  loadChapter();

  return {
    el: view,
    focusComposer: () => editor.focus(),
    refresh: loadChapter,
    destroy() {
      clearTimeout(saveTimer);
      clearTimeout(fillPhoneTimer);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('beforeunload', onBeforeUnload);
      scrollHost.removeEventListener('scroll', onBenchScroll);
      closeTypoPanel();
      closePhone();
      selectionBar.destroy();
      if (chatFloat) chatFloat.destroy();
      editor.destroy();
      view.remove();
    }
  };
}
