/* 提示词全开放编辑页（v5.3.0，方案 docs/系统化升级方案-v5.1 §7.2）。
   分组手风琴 + 槽行（标签 / 变量 chip / {{占位}} / 温度滑杆 0-2 步长 0.05）+
   弹窗多行编辑（textarea）+「已修改」「默认值已更新」徽章 + 恢复默认 +
   对比视图（用户版 vs 新默认）+ 一键采用新版。
   数据走 bot/webprompts.py；两层目录 + 代码兜底由后端 prompts_store 收口，
   出口闸门（纯净域/写前写后/交付闸门）不因提示词编辑有任何变化。 */

import { h, icon } from '../core/dom.js';
import { request, errorText } from '../core/api.js';
import { toast, openModal } from '../core/ui.js';

/* ---------------------------------------------------------------- 纯函数（导出供 node 测试；与 bot/prompts_store.py 同一格式） */

/** 提取槽文本里的 {{变量}}（去重保序）。 */
export function extractVars(text) {
  const seen = new Set();
  const out = [];
  const s = String(text || '');
  const re = /\{\{\s*([^{}]+?)\s*\}\}/g;
  let m;
  while ((m = re.exec(s)) !== null) {
    if (!seen.has(m[1])) {
      seen.add(m[1]);
      out.push(m[1]);
    }
  }
  return out;
}

/**
 * 解析组 md：frontmatter（--- 块里的 id/label/base）+ `## 槽名` 段落 + 段首 `温度：x` 行。
 * 与 bot/prompts_store.parse_prompt_md 同一格式：注释段落剥离；``` 围栏内的 `## ` 不是槽标题，
 * 解析时剥掉外围围栏行。返回 {frontmatter:{id,label,base}, slots:{名:{text,temp}}, errors:[]}。
 */
export function parsePromptMd(raw) {
  const out = { frontmatter: { id: '', label: '', base: '' }, slots: {}, errors: [] };
  let s = String(raw || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  const fm = s.match(/^---\s*\n([\s\S]*?)\n---\s*\n?/);
  if (fm) {
    for (const line of fm[1].split('\n')) {
      const idx = line.indexOf(':');
      if (idx < 0) continue;
      const k = line.slice(0, idx).trim().toLowerCase();
      const v = line.slice(idx + 1).trim();
      if (k === 'id' || k === 'label' || k === 'base') out.frontmatter[k] = v;
    }
    s = s.slice(fm[0].length);
  } else {
    out.errors.push('缺少 frontmatter（--- 块）');
  }
  s = s.replace(/<!--[\s\S]*?-->/g, '');
  // 切槽：围栏内不识别标题
  let cur = '';
  let buf = [];
  let inFence = false;
  for (const line of s.split('\n')) {
    if (line.trim().startsWith('```')) {
      inFence = !inFence;
      if (cur) buf.push(line);
      continue;
    }
    if (!inFence) {
      const hm = line.match(/^##\s+(.+?)\s*$/);
      if (hm) {
        if (cur) out.slots[cur] = finishSlot(buf);
        cur = hm[1];
        buf = [];
        continue;
      }
    }
    if (cur) buf.push(line);
    else if (line.trim() && !line.startsWith('#')) out.errors.push(`游离文本：${line.trim().slice(0, 30)}`);
  }
  if (cur) out.slots[cur] = finishSlot(buf);
  return out;
}

function finishSlot(buf) {
  const lines = buf.join('\n').replace(/^\n+/, '').split('\n');
  let temp = null;
  let i = 0;
  while (i < lines.length && !lines[i].trim()) i += 1;
  const tm = i < lines.length ? lines[i].trim().match(/^温度\s*[:：]\s*(-?\d+(?:\.\d+)?)\s*$/) : null;
  if (tm) {
    temp = Math.max(0, Math.min(2, Number(tm[1])));
    i += 1;
  }
  const rest = lines.slice(i);
  while (rest.length && !rest[0].trim()) rest.shift();
  while (rest.length && !rest[rest.length - 1].trim()) rest.pop();
  if (rest.length >= 2 && rest[0].trim().startsWith('```') && rest[rest.length - 1].trim().startsWith('```')) {
    rest.splice(0, 1);
    rest.splice(rest.length - 1, 1);
  }
  return { text: rest.join('\n').trim(), temp };
}

/** 温度显示/写入统一两位小数内的干净形式（0.1 → '0.1'，0.85 → '0.85'）。 */
export function fmtTemp(t) {
  const n = Math.max(0, Math.min(2, Number(t) || 0));
  return String(Math.round(n * 100) / 100);
}

/**
 * 把一个槽合并进组 md 原文（无该槽则追加；frontmatter 缺失则按 patch 补最小头）。
 * patch: {text, temp, group?, label?}；temp=null/undefined 表示不写温度行。
 */
export function upsertSlot(raw, slot, patch = {}) {
  const parsed = parsePromptMd(raw);
  const fm = { ...parsed.frontmatter };
  if (!fm.id && patch.group) fm.id = patch.group;
  if (!fm.label && patch.label) fm.label = patch.label;
  const lines = ['---', `id: ${fm.id || ''}`, `label: ${fm.label || ''}`, `base: ${fm.base || ''}`, '---', ''];
  const names = Object.keys(parsed.slots);
  const body = [];
  if (!names.includes(slot)) names.push(slot);
  for (const name of names) {
    const cur = name === slot
      ? { text: String(patch.text ?? ''), temp: (patch.temp === undefined ? null : patch.temp) }
      : parsed.slots[name];
    body.push(`## ${name}`);
    if (cur.temp !== null && cur.temp !== undefined) body.push(`温度：${fmtTemp(cur.temp)}`);
    const txt = cur.text || '';
    if (/^## /m.test(txt)) {
      body.push('```');
      body.push(txt);
      body.push('```');
    } else {
      body.push(txt);
    }
    body.push('');
  }
  return lines.join('\n') + body.join('\n').replace(/\n+$/, '') + '\n';
}

/**
 * 徽章状态机（纯函数）：给定槽态与组态，判定两个徽章。
 *   modified       ——「已修改」：用户层文本与当前代码默认不同（后端按槽判定）
 *   defaultUpdated ——「默认值已更新」：用户文件还钉在旧指纹，且该槽生效文本来自用户层
 */
export function slotBadges(slot, group) {
  return {
    modified: Boolean(slot && slot.modified),
    defaultUpdated: Boolean(group && group.defaultUpdated && slot && slot.source === 'user'),
  };
}

/* ---------------------------------------------------------------- 视图 */

export function renderPromptEdit(container, ctx) {
  const view = h('div', { class: 'view-scroll' });
  const pad = h('div', { class: 'view-pad pe-pad' });
  view.append(pad);
  container.append(view);

  const model = { groups: [], open: new Set() };

  const head = h(
    'div',
    { class: 'evo-head' },
    h(
      'div',
      { class: 'evo-head-copy' },
      h('h1', { text: '提示词' }),
      h('p', { class: 'hint-text', text: '两层开放：内置默认随代码更新，你的修改存进 data/prompts/ 覆盖层；删掉覆盖文件即恢复默认。改坏提示词不影响安全——纯净域与出口闸门照常兜底。' })
    ),
    h('button', {
      class: 'btn', type: 'button', html: `${icon('refresh', 14)}<span>刷新</span>`,
      on: { click: () => load() }
    })
  );
  pad.append(head);

  const listEl = h('div', { class: 'pe-list' });
  pad.append(listEl);

  async function load() {
    listEl.textContent = '';
    listEl.append(h('div', { class: 'pe-loading', text: '载入中…' }));
    try {
      const d = await request('/api/prompts');
      model.groups = (d && d.groups) || [];
    } catch (err) {
      listEl.textContent = '';
      listEl.append(h('div', { class: 'pe-error', text: errorText(err, '提示词清单读取失败') }));
      return;
    }
    paint();
  }

  function paint() {
    listEl.textContent = '';
    if (!model.groups.length) {
      listEl.append(h('div', { class: 'pe-empty', text: '没有可用提示词组。' }));
      return;
    }
    for (const g of model.groups) {
      const groupEl = buildGroup(g);
      listEl.append(groupEl);
      if (model.open.has(g.id)) groupEl.classList.add('pe-open');
    }
  }

  /* ---------------------------------------------- 组手风琴 */

  function buildGroup(g) {
    const bodyEl = h('div', { class: 'pe-group-body' });
    if (!g.slots || !g.slots.length) {
      bodyEl.append(h('div', { class: 'pe-note', text: '本组暂无可编辑槽位（见组说明）。' }));
    }
    for (const s of g.slots || []) bodyEl.append(buildSlotRow(g, s));

    const badgeEls = [];
    const modifiedCnt = (g.slots || []).filter((s) => s.modified).length;
    if (g.hasUserFile) {
      badgeEls.push(h('span', { class: 'pe-badge', dataset: { tone: 'info' }, text: '用户覆盖中' }));
    }
    if (modifiedCnt) badgeEls.push(h('span', { class: 'pe-badge', dataset: { tone: 'ok' }, text: `已修改 ${modifiedCnt} 槽` }));
    if (g.defaultUpdated) badgeEls.push(h('span', { class: 'pe-badge', dataset: { tone: 'warn' }, text: '默认值已更新' }));
    if (g.fingerprint) badgeEls.push(h('span', { class: 'pe-fp', text: `指纹 ${g.fingerprint}` }));

    const headerBtn = h(
      'button',
      { class: 'pe-group-head', type: 'button' },
      h('span', { class: 'pe-caret', html: icon('chevron-down', 14) }),
      h(
        'span',
        { class: 'pe-group-title' },
        h('span', { class: 'pe-group-label', text: g.label }),
        h('span', { class: 'pe-group-id', text: g.id })
      ),
      h('span', { class: 'pe-group-badges' }, badgeEls)
    );
    headerBtn.addEventListener('click', () => {
      if (model.open.has(g.id)) {
        model.open.delete(g.id);
        groupEl.classList.remove('pe-open');
      } else {
        model.open.add(g.id);
        groupEl.classList.add('pe-open');
      }
    });

    const actions = h('div', { class: 'pe-group-actions' });
    if (g.defaultUpdated) {
      actions.append(h('button', {
        class: 'btn btn-sm btn-primary', type: 'button', text: '采用新版默认',
        on: {
          click: async () => {
            try {
              await request('/api/prompts/adopt', { method: 'POST', body: { group: g.id } });
              toast('已采用新版默认（保留你的修改，指纹已对齐）', { tone: 'success' });
              await load();
            } catch (err) {
              toast(errorText(err, '采用失败'), { tone: 'danger' });
            }
          }
        }
      }));
      actions.append(h('button', {
        class: 'btn btn-sm', type: 'button', text: '对比',
        on: { click: () => openCompare(g) }
      }));
    }
    if (g.hasUserFile) {
      actions.append(h('button', {
        class: 'btn btn-sm', type: 'button', text: '编辑整档',
        on: { click: () => openRawEditor(g) }
      }));
      actions.append(h('button', {
        class: 'btn btn-sm pe-btn-danger', type: 'button', text: '恢复默认',
        on: {
          click: async () => {
            if (!window.confirm(`恢复「${g.label}」为默认？将删除你的覆盖文件（data/prompts/${g.id}.md）。`)) return;
            try {
              await request(`/api/prompts/file?group=${encodeURIComponent(g.id)}`, { method: 'DELETE' });
              toast('已恢复默认', { tone: 'success' });
              await load();
            } catch (err) {
              toast(errorText(err, '恢复失败'), { tone: 'danger' });
            }
          }
        }
      }));
    } else {
      actions.append(h('button', {
        class: 'btn btn-sm', type: 'button', text: '编辑整档',
        on: { click: () => openRawEditor(g) }
      }));
    }

    const groupEl = h(
      'section',
      { class: 'pe-group card' },
      h('div', { class: 'pe-group-bar' }, headerBtn, actions),
      bodyEl
    );
    return groupEl;
  }

  /* ---------------------------------------------- 槽行 */

  function buildSlotRow(g, s) {
    const badges = slotBadges(s, g);
    const chips = (s.vars || []).map((v) => h('code', { class: 'pe-var-chip', text: `{{${v}}}` }));
    const row = h('div', { class: 'pe-slot-row' });

    const left = h(
      'div',
      { class: 'pe-slot-main' },
      h(
        'div',
        { class: 'pe-slot-title' },
        h('span', { class: 'pe-slot-label', text: s.label }),
        h('code', { class: 'pe-slot-name', text: s.slot }),
        badges.modified ? h('span', { class: 'pe-badge', dataset: { tone: 'ok' }, text: '已修改' }) : null,
        badges.defaultUpdated ? h('span', { class: 'pe-badge', dataset: { tone: 'warn' }, text: '默认值已更新' }) : null
      ),
      s.desc ? h('div', { class: 'pe-slot-desc', text: s.desc }) : null,
      chips.length ? h('div', { class: 'pe-slot-vars' }, h('span', { class: 'pe-slot-vars-hint', text: '变量' }), chips) : null
    );
    row.append(left);

    const right = h('div', { class: 'pe-slot-side' });
    if (s.temp !== null && s.temp !== undefined) {
      const val = h('span', { class: 'pe-temp-val', text: fmtTemp(s.temp) });
      const slider = h('input', {
        class: 'pe-temp-slider', type: 'range', min: '0', max: '2', step: '0.05',
        value: String(Number(s.temp) || 0), 'aria-label': `「${s.label}」温度`
      });
      slider.addEventListener('change', async () => {
        const t = Number(slider.value) || 0;
        val.textContent = fmtTemp(t);
        await saveSlot(g, s, s.text, t, `温度已更新为 ${fmtTemp(t)}`);
      });
      right.append(h('div', { class: 'pe-temp-box' }, h('span', { class: 'pe-temp-hint', text: '温度' }), slider, val));
    }
    right.append(h('button', {
      class: 'btn btn-sm', type: 'button', text: '编辑',
      on: { click: () => openSlotEditor(g, s) }
    }));
    row.append(right);
    return row;
  }

  /* ---------------------------------------------- 保存 */

  async function saveSlot(g, s, text, temp, okMsg) {
    try {
      const fd = await request(`/api/prompts/file?group=${encodeURIComponent(g.id)}`);
      const raw = upsertSlot((fd && fd.raw) || '', s.slot, { text, temp, group: g.id, label: g.label });
      await request('/api/prompts/file', { method: 'POST', body: { group: g.id, raw } });
      toast(okMsg || '已保存到用户覆盖层', { tone: 'success' });
      await load();
      return true;
    } catch (err) {
      toast(errorText(err, '保存失败'), { tone: 'danger' });
      return false;
    }
  }

  function openSlotEditor(g, s) {
    const ta = h('textarea', {
      class: 'pe-textarea', rows: String(Math.min(24, Math.max(8, Math.ceil((s.text || '').length / 60) + 2))),
      spellcheck: 'false', 'aria-label': `编辑「${s.label}」槽文本`
    });
    ta.value = s.text || '';
    const varHint = (s.vars || []).length
      ? h('div', { class: 'pe-modal-hint' }, '可用变量（保留 {{占位}} 原样，运行时注入）：', (s.vars || []).map((v) => h('code', { class: 'pe-var-chip', text: `{{${v}}}` })))
      : h('div', { class: 'pe-modal-hint', text: '本槽无动态变量。保存后写入用户覆盖层 data/prompts/，删档即恢复默认。' });
    const handle = openModal({
      title: `编辑「${g.label} · ${s.label}」`,
      subtitle: s.temp !== null && s.temp !== undefined ? `当前温度 ${fmtTemp(s.temp)}（在行内滑杆调整）` : '系统提示词槽',
      size: 'lg',
      body: h('div', { class: 'pe-modal-body' }, varHint, ta),
      footer: [
        h('button', { class: 'btn', type: 'button', text: '取消', on: { click: () => handle.close() } }),
        h('button', {
          class: 'btn btn-primary', type: 'button', text: '保存', 'data-autofocus': true,
          on: {
            click: async () => {
              const ok = await saveSlot(g, s, ta.value, s.temp, '槽已保存');
              if (ok) handle.close();
            }
          }
        })
      ],
      onClose: () => {}
    });
  }

  function openRawEditor(g) {
    const ta = h('textarea', { class: 'pe-textarea pe-textarea-md', rows: '20', spellcheck: 'false', 'aria-label': '整档 md' });
    ta.value = '载入中…';
    const hintEl = h('div', { class: 'pe-modal-hint', text: 'md 格式正常。' });
    ta.addEventListener('input', () => {
      const parsed = parsePromptMd(ta.value);
      hintEl.textContent = parsed.errors.length ? `解析告警：${parsed.errors[0]}` : 'md 格式正常。';
    });
    const handle = openModal({
      title: `编辑整档「${g.label}」`,
      subtitle: `data/prompts/${g.id}.md（原始 md，含 frontmatter；槽名必须与内置一致）`,
      size: 'lg',
      body: h('div', { class: 'pe-modal-body' }, ta, hintEl),
      footer: [
        h('button', { class: 'btn', type: 'button', text: '取消', on: { click: () => handle.close() } }),
        h('button', {
          class: 'btn btn-primary', type: 'button', text: '保存',
          on: {
            click: async () => {
              try {
                await request('/api/prompts/file', { method: 'POST', body: { group: g.id, raw: ta.value } });
                toast('整档已保存', { tone: 'success' });
                handle.close();
                await load();
              } catch (err) {
                toast(errorText(err, '整档保存失败（槽名校验未过？）'), { tone: 'danger' });
              }
            }
          }
        })
      ],
      onClose: () => {}
    });
    // 异步填入当前原始 md（用户层优先，回落内置层）
    request(`/api/prompts/file?group=${encodeURIComponent(g.id)}`)
      .then((fd) => { ta.value = (fd && fd.raw) || ''; })
      .catch((err) => { ta.value = ''; toast(errorText(err, '整档读取失败'), { tone: 'danger' }); });
  }

  /* ---------------------------------------------- 对比视图（用户版 vs 新默认） */

  function openCompare(g) {
    const rows = h('div', { class: 'pe-modal-body' });
    let shown = 0;
    for (const s of g.slots || []) {
      if (!s.modified && !(g.defaultUpdated && s.source === 'user')) continue;
      shown += 1;
      rows.append(h(
        'div',
        { class: 'pe-cmp-row' },
        h('div', { class: 'pe-cmp-title' }, h('span', { text: s.label }), h('code', { text: s.slot })),
        h(
          'div',
          { class: 'pe-cmp-grid' },
          h('div', { class: 'pe-cmp-col' }, h('div', { class: 'pe-cmp-cap', text: '用户版（当前生效）' }), h('pre', { class: 'pe-cmp-pre', text: s.user_text ?? s.text ?? '' })),
          h('div', { class: 'pe-cmp-col' }, h('div', { class: 'pe-cmp-cap pe-cmp-cap-new', text: '新默认' }), h('pre', { class: 'pe-cmp-pre', text: s.default_text ?? '' }))
        )
      ));
    }
    if (!shown) rows.append(h('div', { class: 'pe-note', text: '没有内容差异可对比。' }));
    const handle = openModal({
      title: `对比「${g.label}」`,
      subtitle: '用户版 vs 新默认（新默认 = 当前代码默认值）',
      size: 'lg',
      body: rows,
      footer: [
        h('button', { class: 'btn', type: 'button', text: '关闭', on: { click: () => handle.close() } }),
        h('button', {
          class: 'btn btn-primary', type: 'button', text: '一键采用新版（保留我的修改）',
          on: {
            click: async () => {
              try {
                await request('/api/prompts/adopt', { method: 'POST', body: { group: g.id } });
                toast('已采用新版默认', { tone: 'success' });
                handle.close();
                await load();
              } catch (err) {
                toast(errorText(err, '采用失败'), { tone: 'danger' });
              }
            }
          }
        })
      ],
      onClose: () => {}
    });
  }

  load();   // 异步拉清单（render 约定同步返回，加载完成后 paint）
  return view;
}
