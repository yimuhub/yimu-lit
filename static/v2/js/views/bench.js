/* 章节工作台：正文编辑、自动保存、版本历史、差异对比与回滚。 */

import { h, icon, fmtWords, fmtDate, relTime, copyText, downloadUrl } from '../core/dom.js';
import { store } from '../core/state.js';
import { api, errorText } from '../core/api.js';
import { toast, confirmDialog, openModal, popoverMenu } from '../core/ui.js';
import { navigate } from '../core/router.js';
import { cleanChapterText } from '../core/purity.js';
import { mountChatFloat } from '../components/chatfloat.js';


/* v4.22.1：复制口径统一走 core/purity.js（与后端 bot/purity.py 同源）——
   正文 = 章题 + 章首引子 + 正文；本章概要/章节备注/修改说明/审核评分一律不进剪贴板。
   实时基于编辑器内容计算，编辑后未保存也能复制正确口径。 */

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
    mode: 'prose',          /* v4.22.2：prose=纯净正文（默认）；raw=工作文件（含概要/备注/历史残留） */
    savedText: '',
    words: 0,
    updatedAt: '',
    versions: [],
    loading: true,
    error: '',
    saving: false,
    dirty: false
  };

  let autosaveTimer = 0;

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
    on: { click: () => save() }
  });
  view.append(head);

  const body = h('div', { class: 'bench-body' });
  const editorCol = h('div', { class: 'bench-editor' });
  const editor = h('textarea', {
    class: 'bench-input',
    spellcheck: 'false',
    placeholder: '这一章还没有正文。可以直接粘贴或开始写作，改动会形成版本快照。',
    'aria-label': '章节正文'
  });
  const foot = h('div', { class: 'bench-foot' });
  /* v4.0.0：正文纸张卡片（阅读列宽度受限、圆角卡片、轻投影） */
  const paper = h('div', { class: 'bench-paper' });
  paper.append(editor, foot);
  editorCol.append(paper);

  const side = h('div', { class: 'bench-side' });
  body.append(editorCol, side);
  view.append(body);

  /* ------------------------------------------------------------ paint */

  /* v4.22.1：整本导出菜单（md/docx/epub/print）——后端 /api/export 逐章走正文纯净域，
     导出的文件里只有 章题 + 章首引子 + 正文，不会混入概要/备注/修改说明/评分表。 */
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
          await api.download(api.exportUrl(key, fmt), `export.${fmt}`);
          toast(`${label} 已开始下载`, { tone: 'success', duration: 2000 });
        } catch (err) {
          toast(errorText(err, '导出失败'), { tone: 'danger', duration: 4000 });
        }
      }
    }));
    popoverMenu(anchor, items, { label: '导出整本（仅正文）' });
  }

  /* 正文 / 工作文件 切换（v4.22.2）。只在两者确实不同时出现，避免无谓打扰。 */
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
            editor.value = m === 'raw' ? model.rawText : model.text;
            model.savedText = editor.value;
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
      /* v4.22.2：正文 / 工作文件切换。默认「正文」——所见即可复制、可发布；
         「工作文件」才显示本章概要、章节备注与历史残留，供核对，不参与复制与导出。 */
      modeSwitch,
      /* v4.22.1：把「整本导出」放到编辑现场——导出的永远是去噪后的正文 */
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
              /* v4.22.1：复制「引子+正文」口径（剥全部元信息小节），与成书/导出一致 */
              const raw = cleanChapterText(editor.value) || editor.value;
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

  function paintFoot() {
    foot.textContent = '';
    const chars = editor.value.length;
    foot.append(
      h(
        'div',
        { class: 'bench-foot-info' },
        h('span', { text: `${chars.toLocaleString('zh-CN')} 字符` }),
        model.words ? h('span', { class: 'sep' }, h('span', { text: `记录字数 ${fmtWords(model.words)}` })) : null,
        model.updatedAt ? h('span', { class: 'sep' }, h('span', { text: `上次保存 ${fmtDate(model.updatedAt, true)}` })) : null
      ),
      h('span', { style: { flex: '1' } }),
      model.dirty ? h('span', { class: 'bench-dirty', text: '有未保存改动 · Cmd/ Ctrl+S 保存' }) : h('span', { class: 'hint-text', text: '已保存' })
    );
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
    const body = h('div', { class: 'bench-side-body' });
    if (!model.versions.length) {
      body.append(h('div', { class: 'hint-text', text: '还没有版本。第一次保存后会自动生成快照，之后每次保存都会留档。' }));
    }
    for (const v of model.versions) {
      body.append(
        h(
          'div',
          { class: 'bench-version' },
          h(
            'div',
            { class: 'bv-top' },
            h('span', { class: 'bv-label', text: v.label || v.id || '快照' }),
            h('span', {
              class: 'bv-meta',
              text: `${v.at ? relTime(v.at) : ''}${v.words ? ` · ${fmtWords(v.words)}` : ''}`
            })
          ),
          h(
            'div',
            { class: 'bv-actions' },
            h('button', { class: 'btn btn-sm', type: 'button', text: '对比', on: { click: () => showDiff(v) } }),
            h('button', {
              class: 'btn btn-sm',
              type: 'button',
              text: '恢复',
              on: {
                click: async () => {
                  const ok = await confirmDialog({
                    title: '恢复这个版本？',
                    message: '当前正文会先自动存一份快照，随时可以再切回来。',
                    confirmText: '恢复'
                  });
                  if (!ok) return;
                  try {
                    await api.restoreChapter(key, model.n, v.id);
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
    side.append(body);
  }

  function paintSaveBtn() {
    const state = model.saving ? 'saving' : model.error && !model.dirty ? 'error' : model.dirty ? 'dirty' : 'saved';
    saveBtn.dataset.state = state;
    saveBtn.disabled = !model.dirty || model.saving;
    saveBtn.textContent = model.saving ? '保存中…' : model.dirty ? '保存改动' : '已保存';
  }

  function paintAll() {
    paintHead();
    paintModeSwitch();
    paintFoot();
    paintSide();
    paintSaveBtn();
  }

  /* ------------------------------------------------------------ data */

  async function loadChapter() {
    model.loading = true;
    model.error = '';
    editor.value = '';
    editor.disabled = true;
    paintAll();
    try {
      const [ch, vers] = await Promise.all([api.chapter(key, model.n), api.chapterVersions(key, model.n)]);
      model.title = ch.title || '';
      /* v4.22.2：编辑框默认装「纯净正文」——从这里全选复制，拿到的就是能发的正文。
         原文件（含本章概要/章节备注，以及历史遗留的改写说明）收进 rawText，
         切到「工作文件」才显示；保存时后端会把概要/备注补回去，不会丢元信息。 */
      model.rawText = ch.rawText || ch.text || '';
      model.text = ch.cleanText || cleanChapterText(model.rawText) || model.rawText;
      model.savedText = model.text;
      model.words = ch.words || 0;
      model.updatedAt = ch.updatedAt || '';
      model.versions = (vers && vers.items) || [];
      editor.value = model.mode === 'raw' ? model.rawText : model.text;
    } catch (err) {
      model.error = errorText(err, '章节加载失败');
    }
    model.loading = false;
    model.dirty = false;
    editor.disabled = false;
    paintAll();
  }

  async function save() {
    if (model.saving || !model.dirty) return;
    model.saving = true;
    paintAll();
    try {
      const res = await api.saveChapter(key, model.n, editor.value, model.mode);
      model.savedText = editor.value;
      if (model.mode === 'prose') model.text = editor.value; else model.rawText = editor.value;
      model.dirty = false;
      model.words = (res && res.words) || model.words;
      model.updatedAt = (res && res.updatedAt) || new Date().toISOString();
      toast('已保存并生成版本快照', { tone: 'success', duration: 1800 });
      try {
        const vers = await api.chapterVersions(key, model.n);
        model.versions = (vers && vers.items) || [];
      } catch {
        /* 版本列表刷新失败不影响保存结果 */
      }
      ctx.refreshWork();
    } catch (err) {
      toast(errorText(err, '保存失败，请重试'), { tone: 'danger' });
    }
    model.saving = false;
    paintAll();
  }

  editor.addEventListener('input', () => {
    model.dirty = editor.value !== model.savedText;
    paintFoot();
    saveBtn.dataset.state = model.saving ? 'saving' : model.dirty ? 'dirty' : 'saved';
    saveBtn.disabled = !model.dirty || model.saving;
    saveBtn.textContent = model.dirty ? '保存改动' : '已保存';
    clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(() => {
      if (model.dirty) save();
    }, 8000);
  });

  const onKey = (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
      event.preventDefault();
      save();
    }
  };
  document.addEventListener('keydown', onKey);

  const onBeforeUnload = (event) => {
    if (model.dirty) {
      event.preventDefault();
      event.returnValue = '';
    }
  };
  window.addEventListener('beforeunload', onBeforeUnload);

  /* ------------------------------------------------------------- diff */

  async function showDiff(v) {
    const bodyEl = h('div', { class: 'diff-view' });
    bodyEl.append(h('div', { class: 'session-loading' }, h('span', { class: 'skeleton skeleton-line', style: { width: '50%' } })));
    const modal = openModal({ title: `对比：${v.label || v.id} → 当前`, body: bodyEl, size: 'lg' });
    try {
      const res = await api.chapterDiff(key, model.n, v.id);
      bodyEl.textContent = '';
      const diffText = String((res && res.diff) || '');
      if (!diffText.trim()) {
        bodyEl.append(h('div', { class: 'hint-text', text: '两个版本内容一致。' }));
      } else {
        for (const line of diffText.split('\n')) {
          const isAdd = line.startsWith('+') && !line.startsWith('+++');
          const isDel = line.startsWith('-') && !line.startsWith('---');
          const cls = isAdd || isDel ? (isAdd ? 'add' : 'del') : line.startsWith('@@') ? 'hunk' : 'meta';
          bodyEl.append(
            h(
              'div',
              { class: `diff-line ${cls}` },
              h('span', { class: 'dl-sign', text: isAdd ? '+' : isDel ? '-' : '' }),
              h('span', { text: line.replace(/^[+-]/, '') || ' ' })
            )
          );
        }
      }
    } catch (err) {
      bodyEl.textContent = '';
      bodyEl.append(h('div', { class: 'msg-error', text: errorText(err, '差异加载失败') }));
    }
    return modal;
  }

  /* ------------------------------------------------------------ start */

  paintAll();
  loadChapter();

  return {
    el: view,
    focusComposer: () => editor.focus(),
    refresh: loadChapter,
    destroy() {
      clearTimeout(autosaveTimer);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('beforeunload', onBeforeUnload);
      if (chatFloat) chatFloat.destroy();
      view.remove();
    }
  };
}
