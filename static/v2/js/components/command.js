/* 命令面板：搜索作品、跳转章节与执行全局动作。键盘优先，鼠标可点。 */

import { h, icon, trapFocus, fmtNum, modKey } from '../core/dom.js';
import { store } from '../core/state.js';
import { themeLabel } from '../core/theme.js';

let active = null;

export function isCommandOpen() {
  return Boolean(active);
}

export function closeCommand() {
  if (active) active.close();
}

export function openCommand(ctx) {
  if (active) {
    active.close();
    return active;
  }

  const root = document.getElementById('overlays');
  if (!root) return null;

  const previous = document.activeElement;
  const backdrop = h('div', { class: 'modal-backdrop palette-backdrop' });
  const palette = h('div', {
    class: 'palette',
    role: 'dialog',
    'aria-modal': 'true',
    'aria-label': '命令面板'
  });
  const input = h('input', {
    type: 'text',
    placeholder: '搜索作品、章节或执行动作…',
    'aria-label': '搜索命令',
    autocomplete: 'off',
    spellcheck: 'false'
  });
  const searchRow = h(
    'div',
    { class: 'palette-search' },
    h('span', { html: icon('search', 16) }),
    input,
    h('kbd', { text: 'Esc' })
  );
  const list = h('div', { class: 'palette-list', role: 'listbox', 'aria-label': '命令与作品' });
  const foot = h(
    'div',
    { class: 'palette-foot' },
    h('span', {}, h('kbd', { text: '↑' }), h('kbd', { text: '↓' }), '选择'),
    h('span', {}, h('kbd', { text: '↵' }), '打开'),
    h('span', { class: 'spacer', style: { flex: '1' } }),
    h('span', { text: '输入以筛选' })
  );
  palette.append(searchRow, list, foot);
  backdrop.append(palette);
  root.append(backdrop);

  let entries = [];
  let selected = 0;
  let closed = false;
  const releaseTrap = trapFocus(palette);

  function buildEntries(query) {
    const state = store.get();
    const q = query.trim().toLowerCase();
    const actions = [
      {
        group: '动作',
        label: '新建作品',
        hint: 'N',
        icon: 'plus',
        keywords: '新建 开书 create',
        run: () => {
          close();
          ctx.openWizard();
        }
      },
      {
        group: '动作',
        label: '回到新会话',
        icon: 'message',
        keywords: 'welcome 首页 会话',
        run: () => {
          close();
          ctx.navigate('/welcome');
        }
      },
      {
        group: '动作',
        label: '打开作品库',
        icon: 'library',
        keywords: 'library 全部 作品',
        run: () => {
          close();
          ctx.navigate('/library');
        }
      },
      {
        group: '动作',
        label: '打开技能进化',
        icon: 'sparkles',
        keywords: 'skill 技能 进化',
        run: () => {
          close();
          ctx.navigate('/evolution');
        }
      },
      {
        group: '动作',
        label: `切换主题（当前：${themeLabel(state.theme)}）`,
        icon: state.resolvedTheme === 'dark' ? 'moon' : 'sun',
        keywords: 'theme 主题 深色 浅色 系统',
        run: () => {
          close();
          ctx.toggleTheme();
        }
      },
      {
        group: '动作',
        label: '打开设置',
        icon: 'settings',
        keywords: 'settings 设置 模型 密钥',
        run: () => {
          close();
          ctx.openSettings();
        }
      },
      {
        group: '动作',
        label: state.ui.rightOpen ? '关闭右侧工作台' : '打开右侧工作台',
        hint: `${modKey()}+J`,
        icon: 'panel-right',
        keywords: 'panel 工作台 右栏',
        run: () => {
          close();
          ctx.toggleRight();
        }
      }
    ];

    if (state.work) {
      actions.splice(2, 0, {
        group: '当前作品',
        label: `章节工作台 · 第 ${Math.min((state.work.done || 0) + 1, state.work.total || 1)} 章`,
        icon: 'edit',
        keywords: 'chapter 章节 编辑 工作台',
        run: () => {
          close();
          ctx.openBench(Math.min((state.work.done || 0) + 1, state.work.total || 1));
        }
      });
      for (const [tab, label] of [
        ['plan', '规划'],
        ['state', '状态'],
        ['refs', '参考'],
        ['skills', '技能']
      ]) {
        actions.push({
          group: '工作台',
          label: `打开${label}面板`,
          icon: tab === 'plan' ? 'list' : tab === 'state' ? 'activity' : tab === 'refs' ? 'folder' : 'sparkles',
          keywords: `panel ${label} 工作台`,
          run: () => {
            close();
            ctx.openRight(tab);
          }
        });
      }
    }

    const acts = actions
      .filter((a) => !q || `${a.label} ${a.keywords || ''}`.toLowerCase().includes(q))
      .slice(0, q ? 9 : 8);

    const works = (state.library.items || [])
      .filter((item) => !item.archived)
      .filter((item) => !q || `${item.name} ${item.genre || ''} ${(item.tags || []).join(' ')}`.toLowerCase().includes(q))
      .slice(0, q ? 8 : 5)
      .map((item) => ({
        group: '作品',
        label: item.name,
        hint: `${item.done || 0}/${item.total || 0} 章 · ${fmtNum(item.total_words)} 字`,
        icon: 'book',
        run: () => {
          close();
          ctx.navigate(`/work/${encodeURIComponent(item.key)}`);
        }
      }));

    let chapters = [];
    if (state.work && typeof ctx.chapterList === 'function') {
      chapters = ctx
        .chapterList()
        .filter((c) => !q || String(c.n).includes(q) || String(c.title || '').toLowerCase().includes(q))
        .slice(0, q ? 8 : 0)
        .map((c) => ({
          group: '章节',
          label: c.title || `第 ${c.n} 章`,
          hint: `第 ${c.n} 章`,
          icon: 'file-text',
          run: () => {
            close();
            ctx.openBench(c.n);
          }
        }));
    }

    return [...acts, ...works, ...chapters];
  }

  function paint() {
    const query = input.value;
    entries = buildEntries(query);
    selected = Math.min(selected, Math.max(0, entries.length - 1));
    list.textContent = '';
    if (!entries.length) {
      list.append(
        h('div', { class: 'palette-empty' }, h('div', { text: '没有找到匹配项' }), h('div', { class: 'hint-text', text: '换个关键词，或按 Esc 关闭。' }))
      );
      return;
    }
    let lastGroup = '';
    entries.forEach((entry, index) => {
      if (entry.group !== lastGroup) {
        lastGroup = entry.group;
        list.append(h('div', { class: 'palette-group-label', text: entry.group }));
      }
      const item = h(
        'button',
        {
          class: 'palette-item',
          type: 'button',
          role: 'option',
          'aria-selected': index === selected ? 'true' : 'false',
          dataset: { index: String(index) }
        },
        h('span', { class: 'pi-icon', html: icon(entry.icon || 'dot', 15) }),
        h('span', { class: 'pi-label', text: entry.label }),
        entry.hint ? h('span', { class: 'pi-hint', text: entry.hint }) : null
      );
      item.addEventListener('mousemove', () => {
        if (selected !== index) {
          selected = index;
          updateSelection();
        }
      });
      item.addEventListener('click', () => runEntry(index));
      list.append(item);
    });
    updateSelection();
    requestAnimationFrame(() => {
      const current = list.querySelector(`[data-index="${selected}"]`);
      if (current) current.scrollIntoView({ block: 'nearest' });
    });
  }

  function updateSelection() {
    list.querySelectorAll('.palette-item').forEach((el) => {
      const index = Number(el.dataset.index);
      el.setAttribute('aria-selected', index === selected ? 'true' : 'false');
    });
  }

  function move(delta) {
    if (!entries.length) return;
    selected = (selected + delta + entries.length) % entries.length;
    updateSelection();
    const current = list.querySelector(`[data-index="${selected}"]`);
    if (current) current.scrollIntoView({ block: 'nearest' });
  }

  function runEntry(index) {
    const entry = entries[index];
    if (!entry) return;
    entry.run();
  }

  const onKey = (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close();
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      move(1);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      move(-1);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      runEntry(selected);
    } else if (event.key === 'Home') {
      event.preventDefault();
      selected = 0;
      updateSelection();
    } else if (event.key === 'End') {
      event.preventDefault();
      selected = entries.length - 1;
      updateSelection();
    }
  };

  function onBackdrop(event) {
    if (event.target === backdrop) close();
  }

  function close() {
    if (closed) return;
    closed = true;
    releaseTrap();
    document.removeEventListener('keydown', onKey, true);
    backdrop.remove();
    active = null;
    if (previous && previous.focus) {
      try {
        previous.focus();
      } catch {
        /* ignore */
      }
    }
  }

  input.addEventListener('input', () => {
    selected = 0;
    paint();
  });
  input.addEventListener('keydown', onKey);
  backdrop.addEventListener('mousedown', onBackdrop);
  document.addEventListener('keydown', onKey, true);

  paint();
  active = { close };
  requestAnimationFrame(() => input.focus());
  return active;
}
