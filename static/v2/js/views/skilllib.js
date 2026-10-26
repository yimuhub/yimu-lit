/* 技能库：按文体与题材浏览全部风格技能，预览正文，检查「这本书到底用没用上」，
   并管理自定义技能（上传 / 删除）。不同风格的技能从这里重新露出来。 */

import { h, icon, iconEl, fmtNum, fmtDate } from '../core/dom.js';
import { store } from '../core/state.js';
import { api, errorText, IS_MOCK, request } from '../core/api.js';
import { toast, confirmDialog, openModal } from '../core/ui.js';
import { renderMarkdownInto } from '../core/markdown.js';
import { navigate } from '../core/router.js';
import { openDistillModal, distillMediumLabel } from '../components/distill.js';

const MEDIUM_FILTERS = [
  { value: '', label: '全部' },
  { value: 'novel', label: '小说' },
  { value: 'script', label: '剧本' },
  { value: 'poetry', label: '诗词' },
  { value: 'prose', label: '散文' },
  { value: 'custom', label: '自定义' }
];

const MEDIUM_LABEL = {
  novel: '小说',
  script: '剧本',
  poetry: '诗词',
  prose: '散文',
  all: '通用',
  custom: '自定义'
};

const KIND_LABEL = {
  method: '方法',
  template: '模板',
  data: '数据'
};

const CUSTOM_MEDIUMS = ['all', 'novel', 'script', 'poetry', 'prose', 'custom'];
const SKILL_KINDS = ['method', 'template', 'data'];
const MAX_SKILL_BYTES = 500 * 1024;

function mediumLabel(medium) {
  return MEDIUM_LABEL[medium] || medium || '通用';
}

function kindLabel(kind) {
  return KIND_LABEL[kind] || kind || '方法';
}

function isCustomSkill(skill) {
  const rel = String(skill && skill.relpath ? skill.relpath : '');
  return skill.medium === 'custom' || rel.startsWith('custom/');
}

export function renderSkillLib(container, ctx) {
  container.textContent = '';
  const view = h('div', { class: 'view-scroll' });
  const pad = h('div', { class: 'view-pad' });
  view.append(pad);
  container.append(view);

  const model = {
    medium: '',
    activeGroup: null,
    nodes: [],
    treeLoading: true,
    treeError: '',
    stats: null,
    selected: null,
    file: null,
    fileLoading: false,
    fileError: '',
    uploading: false,
    styles: [],
    stylesLoading: true,
    stylesError: ''
  };
  let destroyed = false;

  /* --------------------------------------------------------------- head */

  const statsRow = h('div', { class: 'skill-stats' });
  pad.append(
    h(
      'div',
      { class: 'skill-head' },
      h(
        'div',
        { class: 'skill-head-copy' },
        h('h1', { text: '技能库' }),
        h('p', {
          class: 'hint-text',
          text: '每种文体、每种风格都有对应的写作技能。翻一翻，再看看它们有没有真的用进你的书里。'
        })
      ),
      h(
        'button',
        {
          class: 'btn btn-sm',
          type: 'button',
          html: `${icon('refresh', 14)}<span>刷新</span>`,
          on: { click: () => load() }
        }
      )
    ),
    statsRow
  );

  const side = h('aside', { class: 'skill-side', 'aria-label': '文体与题材筛选' });
  const listCol = h('div', { class: 'skill-list-col' });
  const preview = h('section', { class: 'skill-preview-card', 'aria-label': '技能正文预览', hidden: true });
  const listWrap = h('div', { class: 'skill-list' });
  listCol.append(preview, listWrap);

  let distillList = null;
  const customPanel = buildCustomPanel();
  const distillPanel = buildDistillPanel();

  pad.append(
    h(
      'div',
      { class: 'skill-layout' },
      side,
      listCol
    )
  );
  pad.append(customPanel);
  pad.append(distillPanel);

  /* -------------------------------------------------------------- stats */

  async function loadStats() {
    try {
      model.stats = await api.skillkitStats();
    } catch {
      model.stats = null;
    }
    if (destroyed) return;
    paintStats();
  }

  function paintStats() {
    statsRow.textContent = '';
    const chips = [];
    const s = model.stats;
    if (s) {
      chips.push(h('span', { class: 'chip chip-accent skill-chip skill-chip-total', text: `共 ${fmtNum(s.files || s.total || 0)} 条技能` }));
      const byMedia = s.by_media || {};
      for (const key of ['novel', 'script', 'poetry', 'prose', 'all', 'custom']) {
        if (byMedia[key]) {
          chips.push(
            h('span', { class: 'chip chip-muted skill-chip', text: `${mediumLabel(key)} ${byMedia[key]}` })
          );
        }
      }
      const byKind = s.by_kind || {};
      for (const key of SKILL_KINDS) {
        if (byKind[key]) {
          chips.push(h('span', { class: 'chip chip-muted skill-chip', text: `${kindLabel(key)} ${byKind[key]}` }));
        }
      }
    } else {
      chips.push(h('span', { class: 'chip chip-muted skill-chip', text: '统计暂不可用' }));
    }
    statsRow.append(...chips);
  }

  /* ---------------------------------------------------------------- tree */

  async function loadTree() {
    model.treeLoading = true;
    model.treeError = '';
    model.activeGroup = null;
    paintSide();
    paintList();
    try {
      const res = await api.skillkitTree(model.medium);
      model.nodes = (res && res.tree) || (Array.isArray(res) ? res : []);
    } catch (err) {
      model.treeError = errorText(err, '技能库加载失败');
      model.nodes = [];
    }
    model.treeLoading = false;
    if (destroyed) return;
    /* 选中的技能若已不在当前筛选里，收起预览 */
    if (model.selected && !visibleSkills().some((sk) => sk.relpath === model.selected.relpath)) {
      model.selected = null;
      model.file = null;
    }
    paintSide();
    paintList();
    paintPreview();
  }

  function visibleSkills() {
    const out = [];
    for (const node of model.nodes) {
      for (const group of node.groups || []) {
        if (model.activeGroup && !(node.medium === model.activeGroup.medium && group.genre === model.activeGroup.genre)) {
          continue;
        }
        for (const skill of group.skills || []) out.push(skill);
      }
    }
    return out;
  }

  function paintSide() {
    side.textContent = '';

    const medias = h('div', { class: 'skill-medias', role: 'group', 'aria-label': '文体筛选' });
    for (const m of MEDIUM_FILTERS) {
      const active = model.medium === m.value;
      medias.append(
        h('button', {
          class: 'skill-media-btn',
          type: 'button',
          'aria-current': active ? 'true' : 'false',
          text: m.label,
          on: {
            click: () => {
              if (model.medium === m.value) return;
              model.medium = m.value;
              loadTree();
            }
          }
        })
      );
    }
    side.append(medias);

    const tree = h('div', { class: 'skill-tree' });
    if (model.treeLoading) {
      const loading = h('div', { class: 'session-loading', 'aria-label': '分组加载中' });
      for (let i = 0; i < 5; i += 1) {
        loading.append(
          h('div', { class: 'skeleton skeleton-line', style: { width: `${48 + ((i * 11) % 40)}%` } })
        );
      }
      tree.append(loading);
    } else if (!model.treeError) {
      for (const node of model.nodes) {
        const groups = node.groups || [];
        if (!groups.length) continue;
        tree.append(
          h('div', { class: 'skill-tree-media', text: node.label || mediumLabel(node.medium) })
        );
        const allBtn = h('button', {
          class: 'skill-group-btn',
          type: 'button',
          'aria-current': !model.activeGroup ? 'true' : 'false',
          text: '不限题材'
        });
        allBtn.addEventListener('click', () => {
          model.activeGroup = null;
          paintSide();
          paintList();
        });
        tree.append(allBtn);
        for (const g of groups) {
          const active =
            model.activeGroup &&
            model.activeGroup.medium === node.medium &&
            model.activeGroup.genre === g.genre;
          const btn = h(
            'button',
            {
              class: 'skill-group-btn',
              type: 'button',
              'aria-current': active ? 'true' : 'false',
              dataset: { genre: g.genre }
            },
            h('span', { class: 'skill-group-label', text: g.label || g.genre || '未分组' }),
            h('span', { class: 'skill-group-count tnum', text: String(g.count != null ? g.count : (g.skills || []).length) })
          );
          btn.addEventListener('click', () => {
            model.activeGroup = active ? null : { medium: node.medium, genre: g.genre };
            paintSide();
            paintList();
          });
          tree.append(btn);
        }
      }
    }
    side.append(tree);
  }

  /* ---------------------------------------------------------------- list */

  function paintList() {
    listWrap.textContent = '';
    if (model.treeLoading) {
      const loading = h('div', { class: 'skill-loading', 'aria-label': '技能列表加载中' });
      for (let i = 0; i < 6; i += 1) {
        loading.append(
          h(
            'div',
            { class: 'skill-item skill-item-skeleton' },
            h('div', { class: 'skill-fill' },
              h('div', { class: 'skeleton skeleton-line', style: { width: `${40 + ((i * 17) % 45)}%` } }),
              h('div', { class: 'skeleton skeleton-line w-40' })
            )
          )
        );
      }
      listWrap.append(loading);
      return;
    }
    if (model.treeError) {
      listWrap.append(
        h(
          'div',
          { class: 'lib-empty empty-state', role: 'alert' },
          h('div', { class: 'empty-icon', html: icon('alert', 18) }),
          h('h3', { text: '技能库没有加载出来' }),
          h('p', { text: model.treeError }),
          h('button', { class: 'btn btn-sm', type: 'button', text: '重试', on: { click: load } })
        )
      );
      return;
    }
    const skills = visibleSkills();
    if (!skills.length) {
      const filtered = model.activeGroup || model.medium;
      listWrap.append(
        h(
          'div',
          { class: 'lib-empty empty-state' },
          h('div', { class: 'empty-icon', html: icon('library', 18) }),
          h('h3', { text: filtered ? '这个筛选下还没有技能' : '技能库还是空的' }),
          h('p', {
            text: filtered
              ? '换一个文体或题材看看，也可以自己传一份写法进「自定义技能」。'
              : IS_MOCK
                ? '演示模式下技能库数据有限；接上本地服务后就能看到全部文体技能。'
                : '后端技能目录还没就绪；稍后刷新，或在下面先传一份自己的写法。'
          }),
          h('button', {
            class: 'btn btn-sm',
            type: 'button',
            text: filtered ? '清空筛选' : '刷新',
            on: {
              click: () => {
                if (filtered) {
                  model.medium = '';
                  loadTree();
                } else {
                  load();
                }
              }
            }
          })
        )
      );
      return;
    }
    const head = h(
      'div',
      { class: 'skill-list-head' },
      h('span', { class: 'count', text: `${skills.length} 条技能` }),
      h('span', { class: 'hint-text', text: '点一条看正文，或按「在本书使用」查它有没有注入你的书。' })
    );
    listWrap.append(head);
    for (const skill of skills) listWrap.append(skillItem(skill));
  }

  function skillItem(skill) {
    const custom = isCustomSkill(skill);
    const item = h('div', { class: 'skill-item', dataset: { rel: skill.relpath || '' } });

    const openBtn = h(
      'button',
      {
        class: 'skill-item-main',
        type: 'button',
        'aria-current': model.selected && model.selected.relpath === skill.relpath ? 'true' : 'false'
      },
      h(
        'span',
        { class: 'skill-item-title-line' },
        h('span', { class: 'skill-item-title', text: skill.title || skill.name }),
        skill.evolved ? h('span', { class: 'badge badge-accent', text: '进化' }) : null,
        custom ? h('span', { class: 'badge', dataset: { tone: 'warn' }, text: '自定义' }) : null
      ),
      h(
        'span',
        { class: 'skill-meta' },
        h('span', { class: 'skill-mono', text: skill.name }),
        h('span', { class: 'sep' }),
        h('span', { text: mediumLabel(skill.medium) }),
        skill.stage ? h('span', { text: `阶段 ${skill.stage}` }) : null,
        h('span', { text: kindLabel(skill.kind) }),
        skill.weight != null ? h('span', { text: `权重 ${skill.weight}` }) : null,
        skill.genre ? h('span', { class: 'badge', text: skill.genre }) : null
      )
    );
    openBtn.addEventListener('click', () => openSkill(skill));

    const actions = h(
      'div',
      { class: 'skill-item-actions' },
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        html: `${icon('book-open', 13)}<span>在本书使用</span>`,
        on: {
          click: (event) => {
            event.stopPropagation();
            useInBookDialog(skill);
          }
        }
      })
    );
    if (custom) {
      actions.append(
        h('button', {
          class: 'icon-btn icon-btn-sm',
          type: 'button',
          'aria-label': `删除自定义技能 ${skill.title || skill.name}`,
          title: '删除自定义技能',
          html: icon('trash', 14),
          on: {
            click: (event) => {
              event.stopPropagation();
              removeSkill(skill);
            }
          }
        })
      );
    }

    item.append(openBtn, actions);
    return item;
  }

  /* ------------------------------------------------------------- preview */

  async function openSkill(skill) {
    model.selected = skill;
    model.file = null;
    model.fileError = '';
    model.fileLoading = true;
    paintList();
    paintPreview();
    try {
      const res = await api.skillkitFile(skill.relpath);
      model.file = res || null;
    } catch (err) {
      model.fileError = errorText(err, '技能正文读取失败');
    }
    model.fileLoading = false;
    if (destroyed) return;
    paintPreview();
    preview.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  function paintPreview() {
    preview.textContent = '';
    preview.hidden = !model.selected && !model.fileLoading;
    if (!model.selected) return;
    const skill = model.selected;
    preview.append(
      h(
        'div',
        { class: 'skill-preview-head' },
        h(
          'div',
          { class: 'skill-fill' },
          h('h2', { class: 'skill-preview-title', text: skill.title || skill.name }),
          h(
            'div',
            { class: 'skill-meta' },
            h('span', { class: 'skill-mono', text: skill.name }),
            h('span', { text: mediumLabel(skill.medium) }),
            skill.stage ? h('span', { text: `阶段 ${skill.stage}` }) : null,
            h('span', { text: kindLabel(skill.kind) }),
            skill.weight != null ? h('span', { text: `权重 ${skill.weight}` }) : null,
            skill.evolved ? h('span', { class: 'badge badge-accent', text: '进化' }) : null
          )
        ),
        h('button', {
          class: 'icon-btn icon-btn-sm',
          type: 'button',
          'aria-label': '关闭预览',
          html: icon('x', 15),
          on: {
            click: () => {
              model.selected = null;
              model.file = null;
              paintPreview();
              paintList();
            }
          }
        })
      )
    );

    if (model.fileLoading) {
      const loading = h('div', { class: 'session-loading', 'aria-label': '正文加载中' });
      loading.append(h('div', { class: 'skeleton skeleton-line' }));
      for (let i = 0; i < 4; i += 1) {
        loading.append(
          h('div', { class: 'skeleton skeleton-line skill-sk-doc', style: { width: `${68 + ((i * 9) % 28)}%` } })
        );
      }
      preview.append(loading);
      return;
    }
    if (model.fileError) {
      preview.append(
        h(
          'div',
          { class: 'skill-preview-error', role: 'alert' },
          h('span', { class: 'error-text', text: model.fileError }),
          h('button', {
            class: 'btn btn-sm',
            type: 'button',
            text: '重试',
            on: { click: () => openSkill(skill) }
          })
        )
      );
      return;
    }

    const file = model.file || {};
    const fm = file.frontmatter || {};
    const fmKeys = Object.keys(fm).filter((k) => !['name', 'title', 'content'].includes(k));
    if (fmKeys.length) {
      const grid = h('div', { class: 'skill-fm' });
      for (const key of fmKeys.slice(0, 8)) {
        const value = Array.isArray(fm[key]) ? fm[key].join('、') : String(fm[key] == null ? '' : fm[key]);
        if (!value) continue;
        grid.append(
          h(
            'div',
            { class: 'skill-fm-cell' },
            h('span', { class: 'skill-fm-k', text: key }),
            h('span', { class: 'skill-fm-v', text: value })
          )
        );
      }
      preview.append(grid);
    }
    if (file.relpath) {
      preview.append(h('div', { class: 'caption skill-path', text: `skillkit/${file.relpath}` }));
    }
    const doc = h('div', { class: 'md skill-doc' });
    renderMarkdownInto(doc, file.content || '');
    if (!(file.content || '').trim()) {
      doc.append(h('p', { class: 'hint-text', text: '这份技能正文是空的，可能是后端文件还没写好。' }));
    }
    preview.append(doc);
  }

  /* --------------------------------------------------------- 在本书使用 */

  function useInBookDialog(skill) {
    const libraryItems = ((store.get().library || {}).items || []).filter((it) => !it.archived);
    const body = h('div', { class: 'skill-apply' });
    const result = h('div', { class: 'skill-apply-result', 'aria-live': 'polite' });
    let sel = null;

    if (!libraryItems.length) {
      body.append(
        h('div', { class: 'empty-state' },
          h('div', { class: 'empty-icon', html: icon('library', 18) }),
          h('p', { text: '作品库还是空的：先建一部作品，才能看到这本书实际注入了哪些技能。' }),
          h('button', {
            class: 'btn btn-sm',
            type: 'button',
            text: '去作品库',
            on: {
              click: () => {
                modal.close();
                navigate('/library');
              }
            }
          })
        )
      );
    } else {
      sel = h('select', { class: 'select', 'aria-label': '选择作品' });
      for (const it of libraryItems) {
        sel.append(h('option', { value: it.key, text: it.name || it.key }));
      }
      body.append(
        h(
          'div',
          { class: 'field' },
          h('span', { class: 'field-label', text: '选一本书，看它命中了哪些风格技能' }),
          h(
            'div',
            { class: 'skill-apply-row' },
            sel,
            h('button', {
              class: 'btn btn-primary btn-sm',
              type: 'button',
              text: '查看命中',
              on: { click: () => check(sel.value) }
            })
          )
        )
      );
    }
    body.append(result);

    async function check(key) {
      result.textContent = '';
      const loading = h('div', { class: 'session-loading' });
      for (let i = 0; i < 2; i += 1) {
        loading.append(h('div', { class: 'skeleton skeleton-line', style: { width: `${55 + i * 20}%` } }));
      }
      result.append(loading);
      let res = null;
      let errText = '';
      try {
        res = await api.skillsApplicable(key);
      } catch (err) {
        errText = errorText(err, '查询失败');
      }
      result.textContent = '';
      if (!res) {
        result.append(
          h(
            'div',
            { class: 'skill-apply-error' },
            h('span', { class: 'error-text', text: errText || '没有查到这本书的技能注入情况' }),
            h('button', { class: 'btn btn-sm', type: 'button', text: '重试', on: { click: () => check(key) } })
          )
        );
        return;
      }
      const items = (Array.isArray(res.items) ? res.items : []).filter(Boolean);
      const hit = items.some(
        (it) => it && (it.name === skill.name || (it.relpath && it.relpath === skill.relpath))
      );
      const book = libraryItems.find((it) => it.key === key);
      result.append(
        h(
          'div',
          { class: 'skill-apply-summary' },
          h('span', { text: `《${(book && book.name) || '这本书'}》· 文体 ${mediumLabel(res.medium)}${res.genre ? ` · 题材 ${res.genre}` : ''}` }),
          h('span', {
            class: 'badge',
            dataset: { tone: hit ? 'success' : 'muted' },
            text: hit ? '本书命中：是' : '本书命中：否'
          })
        )
      );
      if (!items.length) {
        result.append(h('div', { class: 'hint-text', text: '这本书暂时没有命中任何风格技能，可能是文体或题材还没对上。' }));
      } else {
        const chips = h('div', { class: 'skill-apply-chips' });
        for (const it of items.slice(0, 16)) {
          const mine = it && it.name === skill.name;
          chips.append(
            h('span', {
              class: `chip skill-chip${mine ? ' chip-accent' : ''}`,
              text: it.title || it.name,
              title: it.reason || ''
            })
          );
        }
        if (items.length > 16) {
          chips.append(h('span', { class: 'chip chip-muted skill-chip', text: `还有 ${items.length - 16} 条` }));
        }
        result.append(chips);
      }
      if (res.injected_preview) {
        result.append(
          h('div', { class: 'field skill-inject-field' },
            h('span', { class: 'field-label', text: '本书实际注入的上下文片段' }),
            h('pre', { class: 'skill-inject', text: String(res.injected_preview) })
          )
        );
      }
      if (book) {
        result.append(
          h('button', {
            class: 'btn btn-sm skill-apply-open',
            type: 'button',
            html: `${icon('book-open', 13)}<span>打开这本书</span>`,
            on: {
              click: () => {
                modal.close();
                navigate(`/work/${encodeURIComponent(book.key)}`);
              }
            }
          })
        );
      }
    }

    const modal = openModal({
      title: `在本书中使用「${skill.title || skill.name}」`,
      subtitle: '先选一本书，看这个技能是否真的进入了它的创作上下文。',
      body,
      size: 'md'
    });
  }

  /* ------------------------------------------------------ 自定义技能区 */

  function buildCustomPanel() {
    const nameInput = h('input', {
      class: 'input',
      placeholder: '英文标识，如 noir-pace',
      maxlength: '48',
      'aria-label': '自定义技能名'
    });
    const titleInput = h('input', {
      class: 'input',
      placeholder: '中文标题，如：冷硬派节奏',
      maxlength: '40',
      'aria-label': '自定义技能标题'
    });
    const mediumSel = h('select', { class: 'select', 'aria-label': '适用文体' });
    for (const m of CUSTOM_MEDIUMS) {
      mediumSel.append(h('option', { value: m, text: m === 'all' ? '全部文体' : mediumLabel(m) }));
    }
    const kindSel = h('select', { class: 'select', 'aria-label': '技能类型' });
    for (const k of SKILL_KINDS) {
      kindSel.append(h('option', { value: k, text: kindLabel(k) }));
    }
    const weightInput = h('input', {
      class: 'input',
      type: 'number',
      min: '1',
      max: '9',
      value: '4',
      'aria-label': '注入权重'
    });
    const fileInput = h('input', {
      class: 'skill-file-input',
      type: 'file',
      accept: '.md,.markdown,.txt',
      'aria-label': '技能正文文件'
    });
    const submitLabel = h('span', { text: '上传并保存' });
    const submitBtn = h('button', {
      class: 'btn btn-primary btn-sm',
      type: 'button',
      on: { click: submitAdd }
    }, iconEl('upload', 13), submitLabel);

    const panel = h(
      'div',
      { class: 'skill-custom' },
      h(
        'div',
        { class: 'skill-custom-head' },
        h('h2', { text: '自定义技能' }),
        h('p', {
          class: 'hint-text',
          text: '把你自己的写法整理成一份 markdown 传上来，保存后立即注入到对应文体和阶段的创作里。'
        })
      ),
      h(
        'div',
        { class: 'skill-custom-form' },
        h('div', { class: 'field' }, h('span', { class: 'field-label', text: '技能名' }), nameInput),
        h('div', { class: 'field' }, h('span', { class: 'field-label', text: '标题' }), titleInput),
        h('div', { class: 'field' }, h('span', { class: 'field-label', text: '适用文体' }), mediumSel),
        h('div', { class: 'field' }, h('span', { class: 'field-label', text: '类型' }), kindSel),
        h('div', { class: 'field' }, h('span', { class: 'field-label', text: '权重 1-9' }), weightInput),
        h(
          'div',
          { class: 'field' },
          h('span', { class: 'field-label', text: '正文文件（.md）' }),
          fileInput
        ),
        h('div', { class: 'form-actions' }, submitBtn)
      )
    );

    async function submitAdd() {
      if (model.uploading) return;
      const name = nameInput.value.trim();
      if (!/^[a-z0-9][a-z0-9-]{0,47}$/.test(name)) {
        toast('技能名要用小写字母、数字和短横线，如 noir-pace', { tone: 'danger' });
        nameInput.focus();
        return;
      }
      const file = fileInput.files && fileInput.files[0];
      if (!file) {
        toast('请选择一份 .md 正文文件', { tone: 'danger' });
        return;
      }
      if (file.size > MAX_SKILL_BYTES) {
        toast('技能正文超过 500 KB，请精简后再上传', { tone: 'danger' });
        return;
      }
      let content = '';
      try {
        content = (await file.text()).trim();
      } catch {
        toast('文件读取失败，请重试', { tone: 'danger' });
        return;
      }
      if (!content) {
        toast('这份文件是空的', { tone: 'danger' });
        return;
      }
      model.uploading = true;
      submitBtn.disabled = true;
      submitLabel.textContent = '保存中…';
      try {
        await api.skillkitAdd({
          name,
          title: titleInput.value.trim() || name,
          medium: [mediumSel.value],
          stages: [],
          kind: kindSel.value,
          weight: Number(weightInput.value) || 4,
          content
        });
        toast('自定义技能已保存并生效', { tone: 'success' });
        nameInput.value = '';
        titleInput.value = '';
        fileInput.value = '';
        if (model.medium !== 'custom') model.medium = 'custom';
        await loadTree();
      } catch (err) {
        toast(errorText(err, '保存失败'), { tone: 'danger' });
      } finally {
        model.uploading = false;
        submitBtn.disabled = false;
        submitLabel.textContent = '上传并保存';
      }
    }

    return panel;
  }

  async function removeSkill(skill) {
    const ok = await confirmDialog({
      title: `删除自定义技能「${skill.title || skill.name}」？`,
      message: '删除后立即生效，后续创作不再注入这份技能。',
      detail: '如果是系统自带的技能，这里不会显示删除按钮。',
      confirmText: '删除',
      danger: true
    });
    if (!ok) return;
    try {
      await api.skillkitDelete(skill.name);
      toast('已删除', { tone: 'success', duration: 1800 });
      if (model.selected && model.selected.relpath === skill.relpath) {
        model.selected = null;
        model.file = null;
      }
      await load();
    } catch (err) {
      toast(errorText(err, '删除失败'), { tone: 'danger' });
    }
  }

  /* ------------------------------------------------------- 蒸馏风格区 */

  function buildDistillPanel() {
    distillList = h('div', { class: 'skill-distill-list' });
    return h(
      'div',
      { class: 'skill-distill' },
      h(
        'div',
        { class: 'skill-distill-head' },
        h(
          'div',
          { class: 'skill-fill' },
          h('h2', { text: '蒸馏风格' }),
          h('p', {
            class: 'hint-text',
            text: '把喜欢的作者文本蒸馏成可复用风格，开书与创作时都能选它。'
          })
        ),
        h('button', {
          class: 'btn btn-primary btn-sm',
          type: 'button',
          html: `${icon('sparkles', 13)}<span>蒸馏新风格</span>`,
          on: {
            click: () => {
              openDistillModal({ onDone: () => loadStyles() });
            }
          }
        })
      ),
      distillList
    );
  }

  async function loadStyles() {
    model.stylesLoading = true;
    model.stylesError = '';
    paintStyles();
    try {
      const res = await api.styles();
      const items = Array.isArray(res) ? res : (res && (res.items || res.styles)) || [];
      model.styles = items.filter(Boolean);
    } catch (err) {
      model.stylesError = errorText(err, '风格列表加载失败');
      model.styles = [];
      toast(model.stylesError, { tone: 'danger' });
    }
    model.stylesLoading = false;
    if (destroyed) return;
    paintStyles();
  }

  function paintStyles() {
    if (!distillList) return;
    distillList.textContent = '';
    if (model.stylesLoading) {
      const loading = h('div', { class: 'skill-loading' });
      for (let i = 0; i < 2; i += 1) {
        loading.append(
          h('div', { class: 'skeleton skeleton-line', style: { width: `${40 + i * 28}%` } })
        );
      }
      distillList.append(loading);
      return;
    }
    if (model.stylesError) {
      distillList.append(
        h(
          'div',
          { class: 'skill-apply-error', role: 'alert' },
          h('span', { class: 'error-text', text: model.stylesError }),
          h('button', {
            class: 'btn btn-sm',
            type: 'button',
            text: '重试',
            on: { click: () => loadStyles() }
          })
        )
      );
      return;
    }
    if (!model.styles.length) {
      distillList.append(
        h('div', { class: 'empty-state' },
          h('div', { class: 'empty-icon', html: icon('sparkles', 18) }),
          h('p', {
            text: '还没有蒸馏风格。点右上角「蒸馏新风格」，粘一段目标作者的文本就能生成一张风格卡片。'
          })
        )
      );
      return;
    }
    for (const st of model.styles) distillList.append(styleCard(st));
  }

  function styleCard(st) {
    const words = Array.isArray(st.fingerprint_words) ? st.fingerprint_words.filter(Boolean) : [];
    return h(
      'div',
      { class: 'style-card' },
      h(
        'div',
        { class: 'style-card-main' },
        h(
          'div',
          { class: 'style-card-title' },
          h('span', { class: 'style-card-name', text: st.name || '' }),
          st.medium ? h('span', { class: 'badge', text: distillMediumLabel(st.medium) }) : null,
          st.genre ? h('span', { class: 'badge', text: st.genre }) : null
        ),
        h('p', { class: 'style-card-summary', text: st.summary || '（这张卡片没有写概括）' }),
        h(
          'div',
          { class: 'style-card-meta' },
          h('span', { text: st.created_at ? `创建于 ${fmtDate(st.created_at, true)}` : '创建时间未知' })
        ),
        words.length
          ? h(
              'div',
              { class: 'style-card-words' },
              ...words.slice(0, 10).map((w) => h('span', { class: 'chip chip-muted style-word', text: String(w) }))
            )
          : null
      ),
      h(
        'div',
        { class: 'style-card-actions' },
        h('button', {
          class: 'btn btn-sm',
          type: 'button',
          html: `${icon('eye', 13)}<span>查看</span>`,
          on: { click: () => viewStyle(st) }
        }),
        h('button', {
          class: 'icon-btn icon-btn-sm',
          type: 'button',
          'aria-label': `删除风格 ${st.name}`,
          title: '删除风格',
          html: icon('trash', 14),
          on: { click: () => removeStyle(st) }
        })
      )
    );
  }

  async function viewStyle(st) {
    const body = h('div', { class: 'style-detail' });
    openModal({
      title: `风格：${st.name}`,
      subtitle: st.summary || '',
      body,
      size: 'md'
    });
    body.append(
      h(
        'div',
        { class: 'session-loading' },
        h('div', { class: 'skeleton skeleton-line' }),
        h('div', { class: 'skeleton skeleton-line' })
      )
    );
    let card = null;
    try {
      card = await fetchStyleDetail(st.name);
    } catch (err) {
      body.textContent = '';
      body.append(h('div', { class: 'error-text', text: errorText(err, '风格详情读取失败') }));
      return;
    }
    body.textContent = '';
    const dims = (card && card.dims) || {};
    const keys = Object.keys(dims);
    if (keys.length) {
      const grid = h('div', { class: 'style-dims' });
      for (const k of keys) {
        grid.append(
          h(
            'div',
            { class: 'style-dim' },
            h('span', { class: 'style-dim-k', text: k }),
            h('span', { class: 'style-dim-v', text: String(dims[k] == null ? '' : dims[k]) })
          )
        );
      }
      body.append(grid);
    }
    const words = (card && card.fingerprint_words) || st.fingerprint_words || [];
    if (words.length) {
      body.append(
        h(
          'div',
          { class: 'field' },
          h('span', { class: 'field-label', text: '高频指纹词' }),
          h(
            'div',
            { class: 'style-card-words' },
            ...words.map((w) => h('span', { class: 'chip chip-muted style-word', text: String(w) }))
          )
        )
      );
    }
    if (card && card.style_prompt) {
      body.append(
        h(
          'div',
          { class: 'field' },
          h('span', { class: 'field-label', text: '注入用风格说明' }),
          h('pre', { class: 'skill-inject', text: String(card.style_prompt) })
        )
      );
    }
  }

  /* api.js 没有风格详情/删除的方法，这里用它的 request 打同一套接口。 */
  async function fetchStyleDetail(name) {
    return request(`/api/styles/${encodeURIComponent(name)}`);
  }

  async function removeStyle(st) {
    const ok = await confirmDialog({
      title: `删除风格「${st.name}」？`,
      message: '删除后开书与创作时就选不到这个风格了。',
      detail: '已经用这个风格开过的书不受影响。',
      confirmText: '删除',
      danger: true
    });
    if (!ok) return;
    try {
      const payload = await request(`/api/styles/${encodeURIComponent(st.name)}`, { method: 'DELETE' });
      if (payload && payload.ok === false) throw new Error(payload.note || '删除失败');
      toast('已删除风格', { tone: 'success', duration: 1800 });
      await loadStyles();
    } catch (err) {
      toast(errorText(err, '删除失败'), { tone: 'danger' });
    }
  }

  /* -------------------------------------------------------------- start */

  async function load() {
    await Promise.allSettled([loadTree(), loadStats(), loadStyles()]);
  }

  load();

  return {
    el: view,
    refresh: load,
    destroy() {
      destroyed = true;
      view.remove();
    }
  };
}
