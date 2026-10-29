/* 剧本/诗词/散文工作台：在 v2 里直接读各引擎的文档接口。
   小说的正文有计划里的章节结构，这三套只有"引擎逐阶段落盘的 markdown 文档"，
   所以这里只做两件事：按文体列出文档页签，再接上该文体真正支持的动作。 */

import {
  h,
  icon,
  fmtWords,
  statusMeta,
  mediumChip,
  MEDIUM_UNITS,
  downloadUrl
} from '../core/dom.js';
import { store } from '../core/state.js';
import { api, errorText } from '../core/api.js';
import { toast, confirmDialog, skeletonBlock, openModal } from '../core/ui.js';
import { navigate } from '../core/router.js';
import { renderMarkdownInto } from '../core/markdown.js';
import { mountChatFloat } from '../components/chatfloat.js';
import { openDistillModal } from '../components/distill.js';

const BENCH = {
  script: {
    title: '剧本工作台',
    tabs: [['scenes', '场次'], ['bible', '创作圣经'], ['chars', '人物'], ['struct', '结构'], ['revisions', '修订'], ['quality', '质量'], ['storyboard', '分镜']],
    status: (key) => api.scriptStatus(key),
    doc: (key, tab) => api.scriptDoc(tab, key)
  },
  thesis: {
    title: '论文工作台',
    tabs: [['outline', '提纲'], ['refs', '参考文献'], ['report', '降味报告'], ['final', '定稿']],
    defaultTab: 'final',
    status: (key) => api.thesisStatus(key),
    doc: (key, part) => api.thesisPart(key, part)
  },
  children: {
    title: '儿童文学工作台',
    tabs: [['outline', '分级设定'], ['story', '故事'], ['interactive', '互动与插图'], ['final', '定稿']],
    defaultTab: 'final',
    status: (key) => api.miniStatus(key),
    doc: (key, tab) => api.miniPart(key, tab)
  },
  news: {
    title: '新闻稿工作台',
    tabs: [['outline', '素材'], ['final', '正文'], ['check', '事实核查'], ['final', '定稿']],
    defaultTab: 'final',
    status: (key) => api.miniStatus(key),
    doc: (key, tab) => api.miniPart(key, tab)
  },
  copy: {
    title: '文案工作台',
    tabs: [['outline', '需求'], ['hooks', '钩子'], ['final', '文案'], ['titles', '标题标签'], ['final', '定稿']],
    defaultTab: 'final',
    status: (key) => api.miniStatus(key),
    doc: (key, tab) => api.miniPart(key, tab)
  },

  poetry: {
    title: '诗词工作台',
    tabs: [['note', '题解'], ['imagery', '意象'], ['draft', '草稿'], ['check', '格律校验'], ['final', '定稿']],
    defaultTab: 'final',
    status: (key) => api.poetryStatus(key),
    doc: (key, part) => api.poetryPart(key, part)
  },
  prose: {
    title: '散文工作台',
    tabs: [['brief', 'Brief'], ['material', '素材'], ['draft', '初稿'], ['slop', '去味报告'], ['final', '定稿']],
    defaultTab: 'final',
    status: (key) => api.proseStatus(key),
    doc: (key, part) => api.prosePart(key, part)
  }
};

/* 后端 status 的 state 是各引擎自己的码，"已出书"这类小说说法在这里不适用。 */
const STATE_LABELS = {
  completed: '已完成',
  planning: '规划中',
  writing: '写作中',
  in_progress: '创作中',
  validating: '校验中',
  failed: '失败',
  unknown: '草稿'
};

const SETTLED = new Set(['completed', 'failed']);

/* status 里的 operation/verdict 是引擎内部的码（写/审的码、pass|fail|unclear），
   直接上屏等于把流水线术语丢给用户；这里只留各自的短标签。
   stage 是 S5/F 这类阶段码，引擎侧没有标签表，进度条已有 done/total，不再显示。 */
const OPERATION_LABELS = { write: '写', review: '审', refactor: '改', recreate: '重写' };
const VERDICT_LABELS = { pass: '通过', fail: '未过', unclear: '存疑' };
/* 场次状态是计划文件里的码，pending/failed 直接上屏没人看得懂。 */
const SCENE_STATE_LABELS = { pending: '待写', in_progress: '写作中', failed: '失败' };

export const MEDIUM_BENCHES = Object.keys(BENCH);

/* 诗词/散文里能按段改的页签：这两个文件就是副驾能落盘的两份文档。 */
const PARAGRAPH_TABS = new Set(['draft', 'final']);

/* 切段口径必须与后端 bot/assist.py 的 split_blocks 一致：
   按空行切、逐块 trim、丢掉全空块。 */
function splitParagraphs(text) {
  return String(text || '')
    .replace(/\r\n?/g, '\n')
    .split(/\n\s*\n/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/* 格律判定在库列表项里也是原始码，右侧面板同样需要一个中文说法。 */
export function verdictLabel(verdict) {
  return VERDICT_LABELS[verdict] || verdict || '';
}

export function isMediumBench(medium) {
  return MEDIUM_BENCHES.includes(medium);
}

/* 场次计划里是 内/外 + 地点 + 时间，没有单独的标题字段；预览接口给的是 heading。 */
function sceneHeading(s) {
  return [s.kind, s.place, s.time].filter(Boolean).join(' ') || s.heading || s.title || '';
}

/* 预览接口与场次清单的字段名不完全一样（words/wordCount、heading/title），先统一成一种形状。 */
function normalizeScene(s) {
  return {
    no: Number(s.no || s.sceneNo) || 0,
    heading: String(s.heading || s.title || '').trim(),
    status: String(s.status || ''),
    words: Number(s.words || s.wordCount) || 0,
    text: String(s.text || '')
  };
}

/* completed 由引擎回写，偶尔正文已落盘、状态还没跟上；两样都算写过。 */
function sceneWritten(s) {
  return s.status === 'completed' || Boolean(String(s.text || '').trim());
}

export function renderMediumBench(container, ctx) {
  const state0 = store.get();
  const key = state0.route.key || state0.currentKey;
  const medium = state0.workMedium;
  const cfg = BENCH[medium];
  const unit = MEDIUM_UNITS[medium] || '';

  const view = h('div', { class: 'bench-view mb-view' });
  const head = h('div', { class: 'bench-head' });
  const bar = h('div', { class: 'mb-bar' });
  const tabsRow = h('div', { class: 'segmented mb-tabs', role: 'tablist' });
  const scroll = h('div', { class: 'view-scroll' });
  const bodyWrap = h('div', { class: 'mb-body' });
  scroll.append(bodyWrap);
  view.append(head, bar, tabsRow, scroll);
  container.append(view);

  const model = {
    tab: cfg.tabs[0][0],
    status: null,
    doc: '',
    scenes: [],
    loading: true,
    error: '',
    busy: '',
    /* 整本预览一次取回就缓存，翻场和再开都不重复请求；err 也记住，失败只报一次。 */
    scriptPreview: null,
    scriptPreviewErr: null,
    scriptPreviewReq: null
  };

  let timer = 0;
  let gone = false;
  let tabChosen = false;
  let confirming = false;
  let previewOpen = false;
  /* /api/create 把建目录与规划都丢给了后台线程：向导跳到工作台时，
     计划文件很可能还没落盘。这不是错误，等它出现就行。 */
  let awaitingDisk = false;
  let waitingTicks = 0;

  /* 对话副驾的目标：点场次卡片或正文段落时更新；悬浮窗轮询 getContext 取它。
     doc 统一成字符串：剧本是场号，诗词散文是 final/draft。 */
  const chatCtx = { doc: null, block: null, title: '' };
  let chatNode = null;
  /* 悬浮窗在函数末尾才挂上；先声明，目标变了才知道该不该通知它。 */
  let chatFloat = null;

  function setChatTarget(doc, block, title, node) {
    chatCtx.doc = doc == null || doc === '' ? null : String(doc);
    chatCtx.block = chatCtx.doc && block ? Number(block) : null;
    chatCtx.title = title || '';
    if (chatNode) chatNode.dataset.chatTarget = 'false';
    chatNode = node || null;
    if (chatNode) chatNode.dataset.chatTarget = 'true';
    /* 目标变了就让悬浮窗的 chip 立刻跟上，别等到发消息才刷新。 */
    if (chatFloat) chatFloat.refresh();
  }

  /* 正文按空行切段渲染：每段都能点，段号与后端 split_blocks 一致，改稿目标才落得准。 */
  function renderSceneText(container, text, sceneNo, title) {
    const blocks = splitParagraphs(text);
    blocks.forEach((part, i) => {
      const doc = h('div', {
        class: 'md mb-doc mb-par',
        dataset: { block: String(i + 1), chatTarget: 'false' },
        title: `点这一下，把「第 ${sceneNo} 场 · 第 ${i + 1} 段」设为对话改稿目标`
      });
      renderMarkdownInto(doc, part);
      doc.addEventListener('click', () => setChatTarget(String(sceneNo), i + 1, title, doc));
      container.append(doc);
    });
    return blocks.length;
  }

  /* 诗词/散文的定稿与草稿也按空行切段：后端 split_blocks 就是这个口径，
     段号对不上就改不到那一段。 */
  function renderParagraphs(container, text, docId, docLabel) {
    const blocks = splitParagraphs(text);
    blocks.forEach((part, i) => {
      const node = h('div', {
        class: 'md mb-doc mb-par',
        dataset: { block: String(i + 1), chatTarget: 'false' },
        title: `点这一下，把「${docLabel} · 第 ${i + 1} 段」设为对话改稿目标`
      });
      renderMarkdownInto(node, part);
      node.addEventListener('click', () => setChatTarget(docId, i + 1, docLabel, node));
      container.append(node);
    });
    return blocks.length;
  }

  /* ------------------------------------------------------------- paint */

  function meta() {
    const st = model.status || {};
    return statusMeta(st.state || 'unknown', STATE_LABELS[st.state || 'unknown']);
  }

  /* 四种动作节奏不同：bg 是引擎后台线程（立刻返回，靠轮询看结果）、
     sync 是接口当场算完、file 只是把浏览器指向下载链接、view 只开本地浮层、不碰引擎。 */
  function actions() {
    if (medium === 'script') {
      return [
        {
          label: '预览剧本',
          iconName: 'book',
          kind: 'view',
          run: () => openScriptPreview('all')
        },
        {
          label: '时代体检',
          iconName: 'shield',
          kind: 'check',
          run: () => runEraCheck()
        },
        {
          label: '写全部场次',
          iconName: 'pen',
          kind: 'bg',
          confirm: { title: '写全部场次？', message: '未写完的场次会在后台逐场创作，已写过的会跳过；整本写完是分钟级的模型任务。', confirmText: '开始写作' },
          run: () => api.scriptWrite(key)
        },
        {
          label: '通读修订',
          iconName: 'diff',
          kind: 'bg',
          confirm: { title: '通读修订？', message: '会整本重读并出一份修订记录，是分钟级的模型任务。', confirmText: '开始修订' },
          run: () => api.scriptRevise(key)
        },
        { label: '分镜表', iconName: 'grid', kind: 'sync', run: () => openStoryboard() },
        { label: '导出剧本', iconName: 'download', kind: 'file', run: () => downloadUrl(`/api/script/export?key=${encodeURIComponent(key)}`) }
      ];
    }
    if (medium === 'poetry') {
      return [{ label: '重新格律校验', iconName: 'shield', kind: 'sync', run: () => api.poetryRecheck(key) }];
    }
    return [];
  }

  /* v4.5.0：分镜表——查看 / 生成（AI 短剧/漫剧写完自动生成，其它形式可手动触发） */
  async function openStoryboard() {
    let data;
    try {
      data = await api.scriptStoryboard(key);
    } catch (err) {
      toast(errorText(err, '分镜数据读取失败'), { tone: 'danger' });
      return;
    }
    const shots = (data && data.shots) || [];
    if (!shots.length) {
      const ok = await confirmDialog({
        title: '生成分镜头脚本？',
        message: '把已写完的场次逐场拆成分镜（镜号/景别/画面/台词/音效/时长），分钟级模型任务；AI 短剧/漫剧写完也会自动生成。',
        confirmText: '开始生成'
      });
      if (!ok) return;
      toast('分镜拆解已后台启动', { tone: 'success' });
      await api.scriptStoryboardRun(key);
      return;
    }
    const body = h('div', { class: 'sb-wrap' });
    for (const line of String(data.text || '').split('\n')) {
      body.append(h('div', { class: line.startsWith('|') ? 'sb-row' : 'sb-head', text: line }));
    }
    const foot = h('div', { class: 'set-actions' },
      h('button', {
        class: 'btn btn-sm', type: 'button', text: '重新生成',
        on: {
          click: async () => {
            await api.scriptStoryboardRun(key);
            toast('分镜拆解已后台启动，完成后重新打开查看', { tone: 'success' });
            modal.close();
          }
        }
      })
    );
    const modal = openModal({ title: `分镜表（${shots.length} 个镜头）`, body, footer: foot, size: 'lg' });
  }

  function sceneWriteAction(no) {
    return { label: '写这一场', kind: 'bg', run: () => api.scriptWrite(key, no) };
  }

  /* ---------------------------------------------------------- era check */

  const ERA_LABELS = { ancient: '古代', republic: '民国', modern: '当代', future: '近未来', fantasy: '架空', custom: '自定义' };

  /* 体检是纯词表扫描、当场返回，不进 busy 轮询；报告里再决定要不要整场重写。 */
  async function runEraCheck() {
    if (model.busy) return;
    model.busy = '时代体检';
    paintHead();
    try {
      const res = await api.scriptCheck(key);
      if (gone) return;
      openEraReport(res || {});
    } catch (err) {
      toast(errorText(err, '体检失败'), { tone: 'danger', duration: 5000 });
    }
    model.busy = '';
    paintHead();
  }

  function openEraReport(res) {
    const summary = res.summary || {};
    const items = Array.isArray(res.items) ? res.items : [];
    const flagged = items.filter((it) => (it.issues || []).length);
    const eraLabel = ERA_LABELS[res.era] || res.era || '';
    const body = h('div', { class: 'mb-check' });

    if (!flagged.length) {
      body.append(
        h('p', {
          class: 'hint-text',
          text: items.length
            ? `已扫 ${items.length} 场正文，没有发现与「${eraLabel || '当前设定'}」冲突的用词。`
            : '还没有正文可查：先写场次，再来体检。'
        })
      );
      if (res.error) body.append(h('div', { class: 'msg-error', text: String(res.error) }));
    } else {
      body.append(
        h('p', {
          class: 'hint-text',
          text: `${items.length} 场里有 ${flagged.length} 场串代（时代：${eraLabel || '未判定'}）。可以先逐场看词，再让引擎按设定重写。`
        })
      );
      for (const it of flagged) {
        const chip = h('div', { class: 'mb-check-scene' });
        chip.append(
          h(
            'div',
            { class: 'mb-check-head' },
            h('span', { class: 'ms-no', text: `第 ${it.scene} 场` }),
            h('span', { class: 'spacer' }),
            h('button', {
              class: 'btn btn-sm',
              type: 'button',
              text: '重写这一场',
              on: { click: () => rewriteScenes([it.scene], [modal]) }
            })
          )
        );
        for (const issue of it.issues || []) {
          chip.append(
            h(
              'div',
              { class: 'mb-check-issue' },
              h('span', { class: 'mb-check-term', text: `${issue.term} ×${issue.count}` }),
              h('span', { class: 'mb-check-snip', text: issue.snippet || '' })
            )
          );
        }
        body.append(chip);
      }
    }

    const modal = openModal({
      title: '时代一致性体检',
      subtitle: `${(model.status || {}).name || key}${eraLabel ? ` · ${eraLabel}` : ''}`,
      body,
      footer: h(
        'div',
        { style: { display: 'flex', alignItems: 'center', gap: '8px' } },
        h('span', { class: 'spacer' }),
        h('button', { class: 'btn btn-sm', type: 'button', text: '关闭', on: { click: () => modal.close() } }),
        flagged.length
          ? h('button', {
              class: 'btn btn-sm btn-primary',
              type: 'button',
              text: `按设定重写 ${flagged.length} 场`,
              on: { click: () => rewriteScenes(flagged.map((it) => it.scene), [modal]) }
            })
          : null
      ),
      size: 'lg'
    });
  }

  async function rewriteScenes(nos, modals) {
    const ok = await confirmDialog({
      title: `重写 ${nos.length} 场？`,
      message: '引擎会把这些场次按时代设定整场重写，旧正文会先快照到 04-正文/.versions 目录，可在版本里找回。',
      confirmText: '开始重写'
    });
    if (!ok) return;
    try {
      await api.scriptRewriteScenes(key, nos);
      for (const m of modals) {
        if (m && m.close) m.close();
      }
      if (gone) return;
      toast('已开始按设定重写，完成后自动刷新', { tone: 'success' });
      model.scriptPreview = null;
      model.scriptPreviewErr = null;
      startTimer();
    } catch (err) {
      toast(errorText(err, '重写失败'), { tone: 'danger', duration: 5000 });
    }
  }

  /* ---------------------------------------------------------- preview */

  /* 整本预览只取一次：成功缓存，失败也缓存，免得每次点开都重打一遍接口。 */
  function ensureScriptPreview() {
    if (model.scriptPreview) return Promise.resolve(model.scriptPreview);
    if (model.scriptPreviewErr) return Promise.reject(model.scriptPreviewErr);
    if (!model.scriptPreviewReq) {
      model.scriptPreviewReq = api
        .scriptPreview(key)
        .then((res) => {
          model.scriptPreview = {
            name: String((res && res.name) || ''),
            scenes: ((res && res.scenes) || []).map(normalizeScene)
          };
          return model.scriptPreview;
        })
        .catch((err) => {
          model.scriptPreviewErr = err;
          throw err;
        })
        .finally(() => {
          model.scriptPreviewReq = null;
        });
    }
    return model.scriptPreviewReq;
  }

  /* 场次卡片（逐场翻页）与"预览剧本"（整本通读）共用这一个模态：
     数据是同一份，只是逐场渲染还是一次铺开。 */
  function openScriptPreview(mode, sceneNo = 0) {
    if (previewOpen) return;
    previewOpen = true;

    let data = null;
    let loadErr = null;
    let list = [];
    let idx = 0;
    let closed = false;
    let onKey = null;

    const content = h('div', { class: 'mb-prev', tabindex: '-1' }, skeletonBlock(3));
    const foot = h(
      'div',
      { class: 'mb-prev-foot' },
      h('span', { class: 'spacer' }),
      h('span', { class: 'mb-prev-count', text: '读取中…' })
    );
    const prevBtn = h('button', { class: 'btn btn-sm', type: 'button', text: '上一场', on: { click: () => go(idx - 1) } });
    const nextBtn = h('button', { class: 'btn btn-sm', type: 'button', text: '下一场', on: { click: () => go(idx + 1) } });
    const counter = h('span', { class: 'mb-prev-count' });

    const modal = openModal({
      title: mode === 'all' ? '通读剧本' : '剧本预览',
      subtitle: '正在读取正文…',
      body: content,
      footer: foot,
      size: 'xl',
      initialFocus: content,
      onClose: () => {
        closed = true;
        previewOpen = false;
        if (onKey) document.removeEventListener('keydown', onKey);
      }
    });

    const titleEl = modal.el.querySelector('.modal-title');
    const subEl = modal.el.querySelector('.modal-subtitle');

    function setHead(title, words, extra = '') {
      const name = (data && data.name) || (model.status || {}).name || key;
      titleEl.textContent = title;
      subEl.textContent = `《${name}》 · 已写 ${fmtWords(words)}${extra}`;
    }

    function fillFoot(...nodes) {
      foot.textContent = '';
      foot.append(...nodes);
    }

    function go(next) {
      if (!list.length) return;
      const clamped = Math.max(0, Math.min(list.length - 1, next));
      if (clamped === idx) return;
      idx = clamped;
      paintScene();
    }

    function paintScene() {
      const s = list[idx];
      const head = s.heading ? `第 ${s.no} 场 · ${s.heading}` : `第 ${s.no} 场`;
      setHead(head, s.words);
      setChatTarget(String(s.no), null, head, null);
      content.textContent = '';
      if (!sceneWritten(s)) {
        content.append(h('div', { class: 'mb-prev-pending', text: `第 ${s.no} 场 · 待写` }));
      } else {
        content.append(h('div', { class: 'mb-prev-meta', text: `已写 ${fmtWords(s.words)}` }));
        if (!renderSceneText(content, s.text, s.no, head)) {
          /* 状态写着已写、正文却是空的：如实说明，别只给一块白板。 */
          content.append(
            h(
              'div',
              { class: 'mb-prev-empty' },
              h('div', { text: '这一场还没有正文，可能是生成时跳过了' }),
              h('div', { class: 'mb-prev-meta', text: `${head} · 状态仍标记为已写` })
            )
          );
        }
      }
      prevBtn.disabled = idx <= 0;
      nextBtn.disabled = idx >= list.length - 1;
      counter.textContent = `第 ${idx + 1} / ${list.length} 场`;
      modal.body.scrollTop = 0;
    }

    function paintAll() {
      content.textContent = '';
      let words = 0;
      let written = 0;
      for (const s of list) {
        words += s.words;
        /* 这里按"真有正文"计数：整本通读里状态写已写、正文为空的场会显示成提示，不算写完。 */
        if (s.text.trim()) written += 1;
      }
      for (const s of list) {
        if (!sceneWritten(s)) {
          content.append(h('div', { class: 'mb-prev-pending', text: `第 ${s.no} 场 · 待写` }));
          continue;
        }
        content.append(
          h('div', { class: 'mb-prev-sep', text: s.heading ? `第 ${s.no} 场 · ${s.heading}` : `第 ${s.no} 场` })
        );
        if (!s.text.trim()) {
          content.append(h('div', { class: 'mb-prev-empty', text: '这一场还没有正文，可能是生成时跳过了' }));
          continue;
        }
        renderSceneText(content, s.text, s.no, s.heading || `第 ${s.no} 场`);
      }
      if (!list.length) content.append(h('p', { class: 'hint-text', text: '剧本里还没有场次。' }));
      setHead('通读剧本', words, ` · 共 ${list.length} 场`);
      fillFoot(
        h('span', { class: 'spacer' }),
        h('span', { class: 'mb-prev-count', text: `已写 ${written} / ${list.length} 场` })
      );
      modal.body.scrollTop = 0;
    }

    /* 整本接口缺失时退回单场接口：只够读用户点的那一场，翻页就没有了。 */
    async function loadFallback() {
      if (mode === 'all') {
        toast('整本预览加载失败，可以先在卡片上逐场阅读', { tone: 'danger', duration: 5000 });
        content.textContent = '';
        content.append(
          h(
            'div',
            { class: 'mb-prev-empty' },
            h('div', { text: errorText(loadErr, '剧本预览加载失败') }),
            h('div', {
              class: 'mb-prev-meta',
              text: '整本预览接口不可用，没法一次列出全部场次；回到场次卡片逐场打开，或稍后刷新再试。'
            })
          )
        );
        fillFoot(h('span', { class: 'spacer' }));
        return;
      }
      toast('整本预览取不到，这里只显示这一场', { tone: 'danger', duration: 5000 });
      const card = model.scenes.find((s) => Number(s.no || s.sceneNo) === Number(sceneNo)) || {};
      try {
        const res = await api.scriptScene(key, Number(sceneNo));
        if (closed) return;
        list = [
          normalizeScene({
            no: (res && res.no) || sceneNo,
            heading: sceneHeading(card),
            status: 'completed',
            words: card.wordCount || 0,
            text: (res && res.text) || ''
          })
        ];
        idx = 0;
        fillFoot(h('span', { class: 'spacer' }), counter);
        paintScene();
        counter.textContent = `第 ${list[0].no} 场`;
      } catch (err) {
        if (closed) return;
        content.textContent = '';
        content.append(h('div', { class: 'msg-error', text: errorText(err, '这一场读取失败') }));
        fillFoot(h('span', { class: 'spacer' }));
      }
    }

    async function load() {
      try {
        data = await ensureScriptPreview();
      } catch (err) {
        loadErr = err;
      }
      if (closed) return;
      if (!data) {
        await loadFallback();
        return;
      }
      if (mode === 'all') {
        list = data.scenes;
        paintAll();
        return;
      }
      if (!data.scenes.length) {
        content.textContent = '';
        content.append(h('p', { class: 'hint-text', text: '剧本里还没有场次。' }));
        fillFoot(h('span', { class: 'spacer' }));
        return;
      }
      const written = data.scenes.filter(sceneWritten);
      list = written.length ? written : data.scenes;
      let at = list.findIndex((s) => s.no === Number(sceneNo));
      /* 预览里没找到点的那场（多半是缓存旧了），把未写的场也放进翻页列表再找一次。 */
      if (at < 0 && list !== data.scenes) {
        list = data.scenes;
        at = list.findIndex((s) => s.no === Number(sceneNo));
      }
      idx = at >= 0 ? at : 0;
      fillFoot(prevBtn, nextBtn, h('span', { class: 'spacer' }), counter);
      /* ←/→ 翻场；Esc 由 openModal 的全局处理器负责，这里不重复接管。 */
      onKey = (event) => {
        if (event.key === 'ArrowLeft') {
          event.preventDefault();
          go(idx - 1);
        } else if (event.key === 'ArrowRight') {
          event.preventDefault();
          go(idx + 1);
        }
      };
      document.addEventListener('keydown', onKey);
      paintScene();
    }

    void load();
  }

  function paintHead() {
    head.textContent = '';
    const st = model.status || {};
    const m = meta();
    head.append(
      h('button', {
        class: 'icon-btn',
        type: 'button',
        'aria-label': '返回作品库',
        title: '返回作品库',
        html: icon('arrow-left', 16),
        on: { click: () => navigate('/library') }
      }),
      h(
        'div',
        { class: 'bench-title' },
        h('span', { class: 'bt-name', text: st.name || key }),
        mediumChip(medium),
        h('span', { class: 'bt-sub', text: cfg.title })
      ),
      h('span', { class: `dot dot-${m.tone}` }),
      h('span', { class: 'mb-state', text: m.label }),
      h('span', { style: { flex: '1' } }),
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        title: '把目标作者的文本蒸馏成风格卡片',
        html: `${icon('sparkles', 13)}<span>蒸馏风格</span>`,
        on: {
          click: () =>
            openDistillModal({
              medium,
              /* status.form 是"体式/媒介形式"（电影剧本、现代诗），不是题材，别拿来填错字段。 */
              genre: '',
              onDone: () => {
                if (!gone) loadAll();
              }
            })
        }
      }),
      ...actions().map((a) =>
        h('button', {
          class: 'btn btn-sm',
          type: 'button',
          disabled: Boolean(model.busy),
          html: `${icon(a.iconName, 13)}<span>${model.busy === a.label ? '执行中…' : a.label}</span>`,
          on: { click: () => runAction(a) }
        })
      ),
      h('button', {
        class: 'icon-btn',
        type: 'button',
        'aria-label': '刷新',
        title: '刷新',
        html: icon('refresh', 15),
        on: { click: () => loadAll() }
      })
    );
  }

  function paintBar() {
    bar.textContent = '';
    const st = model.status || {};
    const total = Number(st.total) || 0;
    const done = Number(st.done) || 0;
    const pct = st.state === 'completed' ? 100 : total ? Math.round((100 * done) / total) : 0;
    const bits = [
      st.form ? h('span', { text: st.form }) : null,
      h('span', { class: 'mb-count', text: total ? `${done}/${total} ${unit}` : `0 ${unit}` }),
      h('span', { class: 'sep' }),
      h('span', { text: fmtWords(st.words || 0) })
    ];
    const eraLabel = (st.setting || {}).era_label;
    if (eraLabel) bits.push(h('span', { class: 'sep' }), h('span', { text: `时代：${eraLabel}` }));
    if (st.paused) bits.push(h('span', { class: 'sep' }), h('span', { class: 'mb-paused', text: '已暂停：连续串代，等人工确认' }));
    if (st.operation) bits.push(h('span', { class: 'sep' }), h('span', { text: `写法：${OPERATION_LABELS[st.operation] || st.operation}` }));
    if (st.verdict) bits.push(h('span', { class: 'sep' }), h('span', { text: `格律：${VERDICT_LABELS[st.verdict] || st.verdict}` }));
    bar.append(
      h('div', { class: 'mb-bar-info' }, ...bits.filter(Boolean)),
      h('span', { class: 'progress-track mb-track', dataset: { tone: pct >= 100 ? 'success' : 'accent' }, html: `<span style="width:${pct}%"></span>` }),
      h('span', { class: 'pct', text: `${pct}%` })
    );
  }

  function paintTabs() {
    tabsRow.textContent = '';
    for (const [id, label] of cfg.tabs) {
      tabsRow.append(
        h('button', {
          class: 'seg-btn',
          type: 'button',
          role: 'tab',
          text: label,
          'aria-pressed': id === model.tab ? 'true' : 'false',
          on: {
            click: () => {
              if (model.tab === id) return;
              model.tab = id;
              paintTabs();
              loadDoc();
            }
          }
        })
      );
    }
  }

  function sceneStrip() {
    const wrap = h('div', { class: 'mb-scenes' });
    for (const s of model.scenes) {
      const no = Number(s.no || s.sceneNo) || 0;
      const done = s.status === 'completed';
      const heading = sceneHeading(s);
      /* 写过的场可以点开读正文：整张卡片就是一个按钮（里面没有别的控件）；
         没写的场保持静态，仍然只给"写这一场"。 */
      const readable = done && Boolean(no);
      wrap.append(
        h(
          'div',
          {
            class: 'mb-scene',
            dataset: { state: done ? 'done' : 'todo' },
            role: readable ? 'button' : null,
            tabindex: readable ? '0' : null,
            'aria-label': readable ? `阅读第 ${no} 场${heading ? ` · ${heading}` : ''}` : null,
            on: readable
              ? {
                  click: () => openScriptPreview('one', no),
                  keydown: (event) => {
                    if (event.key !== 'Enter' && event.key !== ' ') return;
                    event.preventDefault();
                    openScriptPreview('one', no);
                  }
                }
              : null,
            title: [s.setup, s.cast ? `人物：${s.cast}` : '', readable ? '点击阅读正文' : ''].filter(Boolean).join('\n')
          },
          h('span', { class: 'ms-no', text: `第 ${no} 场` }),
          h('span', { class: 'ms-head', text: heading || '（场次清单里没写场景头）' }),
          h('span', { class: 'ms-state', text: done ? `已写 ${fmtWords(s.wordCount || 0)}` : SCENE_STATE_LABELS[s.status] || s.status || '待写' }),
          /* 引擎的 write_scenes 会跳过 completed 的场次，写过的场在这里没有"重写"可做，
             所以只在未写完的场次上给按钮——按钮点了没反应比没有按钮更糟。 */
          done || !no
            ? null
            : h('button', {
                class: 'btn btn-sm',
                type: 'button',
                text: '写这一场',
                disabled: Boolean(model.busy),
                on: { click: () => runAction(sceneWriteAction(no)) }
              })
        )
      );
    }
    return wrap;
  }

  function paintBody() {
    bodyWrap.textContent = '';
    if (model.loading) {
      bodyWrap.append(skeletonBlock(4));
      return;
    }
    if (model.error) {
      bodyWrap.append(h('div', { class: 'msg-error', text: model.error }));
      return;
    }
    if (awaitingDisk) {
      bodyWrap.append(
        h('div', {
          class: 'hint-text',
          style: { padding: '18px 2px' },
          text: '作品正在后台创建中，引擎落盘后这里会自动出现内容。'
        })
      );
      return;
    }
    if (medium === 'script' && model.tab === 'scenes' && model.scenes.length) {
      bodyWrap.append(sceneStrip());
    }
    const tabLabel = (cfg.tabs.find(([id]) => id === model.tab) || [])[1] || '这一份';
    const emptyText = SETTLED.has((model.status || {}).state)
      ? `《${tabLabel}》还不存在。有的引擎只在跑过修订或评分后才写它，可以在上方触发对应动作。`
      : `《${tabLabel}》还没有内容。引擎是逐阶段落盘的，写完后这里会自动出现。`;
    /* 诗词散文的定稿与草稿要按段渲染：段号是改稿目标的坐标，其余页签还是整块文档。 */
    if (medium !== 'script' && PARAGRAPH_TABS.has(model.tab)) {
      if (!renderParagraphs(bodyWrap, model.doc, model.tab, tabLabel)) {
        bodyWrap.append(h('p', { class: 'hint-text', text: emptyText }));
      }
      return;
    }
    const doc = h('div', { class: 'md mb-doc' });
    renderMarkdownInto(doc, model.doc || '');
    if (!String(model.doc || '').trim()) {
      doc.append(h('p', { class: 'hint-text', text: emptyText }));
    }
    bodyWrap.append(doc);
  }

  /* -------------------------------------------------------------- data */

  async function loadStatus() {
    try {
      model.status = await cfg.status(key);
      awaitingDisk = false;
      waitingTicks = 0;
    } catch (err) {
      model.status = model.status || {};
      if (err && err.isMissing) {
        awaitingDisk = true;
        model.error = '';
        startTimer();
      } else {
        model.error = errorText(err, '状态加载失败');
      }
    }
  }

  async function loadDoc() {
    model.loading = true;
    paintBody();
    try {
      const res = await cfg.doc(key, model.tab);
      const text = String((res && res.text) || '');
      model.error = '';
      awaitingDisk = false;
      if (text !== model.doc) model.doc = text;
      model.scenes = (res && res.scenes) || [];
    } catch (err) {
      if (err && err.isMissing) {
        awaitingDisk = true;
        model.error = '';
      } else {
        model.error = errorText(err, '内容加载失败');
      }
    }
    model.loading = false;
    paintBody();
  }

  /* 完成态直接落在成品上；还在生成的从第一份过程文档开始看，别一屏空白。 */
  function pickDefaultTab() {
    const st = model.status || {};
    model.tab = SETTLED.has(st.state) ? cfg.defaultTab || cfg.tabs[0][0] : cfg.tabs[0][0];
    paintTabs();
  }

  async function loadAll() {
    /* 刚写完或刚修订过，缓存的整本预览就旧了；但模态还开着时先留着，
       别把用户正在读的正文抽走，等关掉后再取。 */
    if (!previewOpen) {
      model.scriptPreview = null;
      model.scriptPreviewErr = null;
    }
    await loadStatus();
    paintHead();
    paintBar();
    if (!tabChosen) {
      tabChosen = true;
      pickDefaultTab();
    }
    await loadDoc();
  }

  async function runAction(action) {
    if (model.busy || confirming) return;
    if (action.confirm) {
      confirming = true;
      let ok = false;
      try {
        ok = await confirmDialog({ ...action.confirm, confirmText: action.confirm.confirmText || '开始' });
      } finally {
        confirming = false;
      }
      if (!ok || gone) return;
    }
    /* file 只是跳下载、view 只是开浮层、check 自己管结果弹窗：都不进 busy，也不轮询。 */
    if (action.kind === 'file' || action.kind === 'view' || action.kind === 'check') {
      action.run();
      return;
    }
    model.busy = action.label;
    paintHead();
    try {
      await action.run();
      await loadAll();
      toast(action.kind === 'bg' ? `${action.label}已在后台开始，进度会自动刷新` : `${action.label}已完成`, {
        tone: 'success'
      });
      startTimer();
    } catch (err) {
      toast(errorText(err, `${action.label}失败`), { tone: 'danger', duration: 5000 });
    }
    model.busy = '';
    paintHead();
  }

  /* 剧本的写作与修订在后台线程里跑，靠轮询把进度与文档拉回来。 */
  function startTimer() {
    if (timer) return;
    timer = setInterval(async () => {
      if (gone) return;
      await loadStatus();
      if (gone) return;
      if (awaitingDisk && ++waitingTicks > 23) {
        /* 两分钟还没落盘，多半是创建失败了，不该再无声地轮下去。 */
        stopTimer();
        model.error = '等了一分多钟仍没等到作品落盘，创建可能失败了；可以回作品库刷新确认，或重新新建。';
        paintBody();
        return;
      }
      paintHead();
      paintBar();
      const st = model.status || {};
      if (!SETTLED.has(st.state) || model.busy) await loadDoc();
      else stopTimer();
    }, 5000);
  }

  function stopTimer() {
    if (!timer) return;
    clearInterval(timer);
    timer = 0;
  }

  /* ------------------------------------------------------------- start */

  /* 三种文体都挂副驾：剧本按场定位，诗词散文按文档段落定位，改法是同一套。 */
  chatFloat = mountChatFloat({
    getContext: () => ({
      key,
      medium,
      doc: chatCtx.doc,
      block: chatCtx.block,
      docTitle: chatCtx.title
    }),
    onApplied: () => {
      if (gone) return;
      /* 落盘后整本预览缓存与当前文档都旧了，重新取一遍。 */
      model.scriptPreview = null;
      model.scriptPreviewErr = null;
      loadDoc();
    }
  });

  paintHead();
  paintBar();
  paintTabs();
  loadAll().then(() => {
    if (gone) return;
    const st = model.status || {};
    if (!SETTLED.has(st.state)) startTimer();
  });

  return {
    el: view,
    refresh: loadAll,
    destroy() {
      gone = true;
      stopTimer();
      if (chatFloat) chatFloat.destroy();
      view.remove();
    }
  };
}
