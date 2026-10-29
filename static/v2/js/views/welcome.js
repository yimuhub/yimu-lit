/* 欢迎页：品牌标题、hero 输入器、四种体裁入口、最近作品与文件拖入。 */

import {
  h,
  icon,
  fmtWords,
  relTime,
  validateFile,
  initials,
  statusMeta
} from '../core/dom.js';
import { store } from '../core/state.js';
import { navigate } from '../core/router.js';
import { createComposer } from '../components/composer.js';
import { MEDIUM_FALLBACK, mediumIcon } from '../core/constants.js';
import { toast } from '../core/ui.js';

function mediumList() {
  const loaded = store.get().mediums || [];
  return loaded.length ? loaded : MEDIUM_FALLBACK;
}

function recentWorks() {
  return (store.get().library.items || [])
    .filter((item) => !item.archived)
    .slice()
    .sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')))
    .slice(0, 5);
}

export function renderWelcome(container, ctx) {
  container.textContent = '';
  const welcome = h('div', { class: 'welcome' });
  const inner = h('div', { class: 'welcome-inner' });
  welcome.append(inner);

  const hero = h(
    'div',
    { class: 'hero-head' },
    h('div', { class: 'hero-mark', html: icon('quill', 20) }),
    h('h1', { class: 'hero-title', text: '把念头，写成故事' }),
    h('p', {
      class: 'hero-sub',
      text: '神笔会记住人物、伏笔与你的文风。先聊清楚方向，再一章一章往前写。'
    })
  );
  inner.append(hero);

  const composer = createComposer({
    variant: 'hero',
    ctx,
    placeholder: '描述你想写的故事，或直接说「继续第 47 章」…',
    onSend(payload) {
      ctx.openWizard({
        idea: payload.text,
        medium: payload.medium,
        genre: payload.genre,
        attachments: payload.attachments
      });
    }
  });
  inner.append(composer.el);

  inner.append(
    h(
      'div',
      { class: 'welcome-notice' },
      h('span', { html: icon('shield', 12) }),
      h('span', { text: '模型输出会先给你确认；参考只用于这本作品。' })
    )
  );

  const quickSection = h('div', { class: 'welcome-section' });
  quickSection.append(
    h(
      'div',
      { class: 'welcome-section-head' },
      h('h2', { text: '从一种体裁开始' }),
      h('a', { href: '#/library', text: '已有作品 →' })
    )
  );
  const quick = h('div', { class: 'quick-grid' });
  for (const medium of mediumList()) {   // v4.12.1：全部体裁
    const btn = h(
      'button',
      { class: 'quick-card', type: 'button', dataset: { medium: medium.key } },
      h('span', { class: 'qc-icon', html: icon(mediumIcon(medium), 15) }),
      h('span', { class: 'qc-name', text: medium.label }),
      h('span', { class: 'qc-desc', text: medium.desc || '' })
    );
    btn.addEventListener('click', () => ctx.openWizard({ medium: medium.key }));
    quick.append(btn);
  }
  quickSection.append(quick);
  inner.append(quickSection);

  const recentSection = h('div', { class: 'welcome-section' });
  recentSection.append(
    h(
      'div',
      { class: 'welcome-section-head' },
      h('h2', { text: '最近的作品' }),
      h('a', { href: '#/library', text: '作品库 →' })
    )
  );
  const list = h('div', { class: 'recent-list' });
  recentSection.append(list);
  inner.append(recentSection);

  function paintRecent() {
    const state = store.get();
    list.textContent = '';
    if (state.library.loading && !state.library.loaded) {
      for (let i = 0; i < 4; i += 1) {
        list.append(
          h(
            'div',
            { class: 'recent-row', style: { pointerEvents: 'none' } },
            h('span', { class: 'skeleton', style: { width: '28px', height: '28px', borderRadius: '8px' } }),
            h(
              'span',
              { class: 'rr-main' },
              h('span', { class: 'skeleton skeleton-line', style: { width: `${52 + i * 9}%`, margin: '2px 0' } }),
              h('span', { class: 'skeleton skeleton-line w-40', style: { height: '7px', margin: 0 } })
            )
          )
        );
      }
      return;
    }
    const works = recentWorks();
    if (!works.length) {
      list.append(
        h(
          'div',
          { class: 'empty-state', style: { padding: '28px 20px' } },
          h('div', { class: 'empty-icon', html: icon('book', 18) }),
          h('h3', { text: '还没有作品' }),
          h('p', { text: '点击上方任意体裁，或直接把文件拖进来，开始第一本。' }),
          h('button', {
            class: 'btn btn-sm',
            type: 'button',
            text: '新建作品',
            on: { click: () => ctx.openWizard() }
          })
        )
      );
      return;
    }
    for (const work of works) {
      const meta = statusMeta(work.state, work.status_label);
      const pct = work.total ? Math.round((work.done / work.total) * 100) : 0;
      const row = h(
        'button',
        { class: 'recent-row', type: 'button' },
        h('span', { class: 'rr-mark', text: initials(work.name) }),
        h(
          'span',
          { class: 'rr-main' },
          h('span', { class: 'rr-name', text: work.name }),
          h(
            'span',
            { class: 'rr-meta' },
            h('span', { class: `dot dot-${meta.tone}` }),
            h('span', { text: meta.label }),
            h('span', { text: `${work.done}/${work.total} 章` }),
            h('span', { text: fmtWords(work.total_words) }),
            h('span', { text: relTime(work.updated_at) })
          )
        ),
        h(
          'span',
          { class: 'rr-progress' },
          h('span', { class: 'progress-track', html: `<span style="width:${pct}%"></span>` })
        )
      );
      row.addEventListener('click', () => navigate(`/work/${encodeURIComponent(work.key)}`));
      list.append(row);
    }
  }

  const dropHint = h(
    'div',
    {
      class: 'welcome-drop-hint',
      style: {
        position: 'absolute',
        inset: '16px',
        zIndex: '6',
        display: 'none',
        background: 'color-mix(in srgb, var(--accent-soft) 92%, var(--bg-base))',
        alignItems: 'center',
        justifyContent: 'center',
        borderStyle: 'solid'
      }
    },
    h('span', { html: icon('upload', 16) }),
    h('span', { text: '松开即可把文件带入新建作品' })
  );
  welcome.append(dropHint);
  welcome.style.position = 'relative';

  let dragDepth = 0;
  function hasFiles(event) {
    return event.dataTransfer && Array.from(event.dataTransfer.types || []).includes('Files');
  }
  welcome.addEventListener('dragenter', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
    dropHint.style.display = 'flex';
  });
  welcome.addEventListener('dragover', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
  });
  welcome.addEventListener('dragleave', (event) => {
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) dropHint.style.display = 'none';
  });
  welcome.addEventListener('drop', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    dropHint.style.display = 'none';
    const files = Array.from(event.dataTransfer.files || []);
    const accepted = [];
    for (const file of files) {
      const check = validateFile(file);
      if (check.ok) accepted.push(file);
      else toast(check.message, { tone: 'danger' });
    }
    if (accepted.length) {
      ctx.openWizard({
        attachments: accepted.map((file) => ({ file, title: file.name, usage_mode: 'facts' }))
      });
      toast(`已带入 ${accepted.length} 份资料，创建时会自动入库`, { duration: 2400 });
    }
  });

  const unsubscribe = store.subscribe((state) => {
    if (!welcome.isConnected) return;
    if (!state.library.loaded) return;
    paintRecent();
  });

  container.append(welcome);
  paintRecent();

  return {
    el: welcome,
    focusComposer: () => composer.focus(),
    refresh: paintRecent,
    destroy() {
      unsubscribe();
      welcome.remove();
    }
  };
}
