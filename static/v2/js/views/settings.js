/* 设置：外观、生成质量、模型与密钥。只发送后端支持的字段，其余只读展示。 */

import { h, icon } from '../core/dom.js';
import { store, patchUi } from '../core/state.js';
import { api, errorText } from '../core/api.js';
import { toast, confirmDialog, openModal, popoverMenu } from '../core/ui.js';
import { setTheme, themeLabel } from '../core/theme.js';
import { setPref } from '../core/prefs.js';
import { SKILL_TITLES, loadSkillCatalog } from '../core/skillCatalog.js';

const QUALITY_MODES = [
  { value: 'off', label: '关', desc: '不做质量门禁，速度最快' },
  { value: 'report', label: '出报告', desc: '写完给评分与问题清单，不自动改' },
  { value: 'auto', label: '自动修订', desc: '低于及格线自动重写一次' }
];

const THEME_OPTIONS = [
  { value: 'light', icon: 'sun' },
  { value: 'dark', icon: 'moon' },
  { value: 'system', icon: 'monitor' }
];

/* 技能标题清单见 core/skillCatalog.js：设置页与右栏协作面板共用同一份缓存。 */

/* 错误态：error-text 原语 + .btn 系重试按钮，三块数据区共用。 */
function errorRow(message, onRetry) {
  return h(
    'div',
    { class: 'set-error', role: 'alert' },
    h('span', { html: icon('alert', 14) }),
    h('span', { class: 'error-text', text: message }),
    h('button', { class: 'btn btn-sm', type: 'button', text: '重试', on: { click: onRetry } })
  );
}

export function renderSettings(container, ctx) {
  const view = h('div', { class: 'view-scroll' });
  const pad = h('div', { class: 'view-pad set-page' });
  view.append(pad);
  container.append(view);

  const model = {
    settings: null,
    settingsError: '',
    providers: null,
    providerError: '',
    collab: null,
    collabError: '',
    collabSaving: false,
    dirty: false,
    saving: false
  };

  pad.append(
    h(
      'div',
      { class: 'lib-head' },
      h(
        'div',
        { class: 'lib-head-copy' },
        h('h1', { text: '设置' }),
        h('p', { class: 'hint-text', text: '密钥只存在本机 providers.json，不会上传；改动即时生效。' })
      )
    )
  );

  /* ----------------------------------------------------------- 外观 */

  const themeSec = h('div', { class: 'set-section' });
  themeSec.append(
    h('div', { class: 'set-section-head' }, h('h3', { text: '外观' })),
    h(
      'div',
      { class: 'theme-options' },
      THEME_OPTIONS.map((opt) => {
        const current = store.get().theme;
        const btn = h(
          'button',
          {
            class: 'theme-opt',
            type: 'button',
            'aria-pressed': current === opt.value ? 'true' : 'false',
            dataset: { value: opt.value }
          },
          h('span', { html: icon(opt.icon, 15) }),
          h('span', { text: themeLabel(opt.value) })
        );
        btn.addEventListener('click', () => {
          setTheme(opt.value);
          paintTheme();
        });
        return btn;
      })
    )
  );
  pad.append(themeSec);

  function paintTheme() {
    const current = store.get().theme;
    for (const btn of themeSec.querySelectorAll('.theme-opt')) {
      btn.setAttribute('aria-pressed', btn.dataset.value === current ? 'true' : 'false');
    }
  }

  /* ------------------------------------------------------- 生成质量 */

  const qualitySec = h('div', { class: 'set-section' });
  const qualityBody = h('div', {});
  qualitySec.append(h('div', { class: 'set-section-head' }, h('h3', { text: '生成质量' })), qualityBody);
  pad.append(qualitySec);

  function paintQuality() {
    qualityBody.textContent = '';
    if (model.settingsError) {
      qualityBody.append(errorRow(model.settingsError, loadSettings));
      return;
    }
    if (!model.settings) {
      qualityBody.append(h('div', { class: 'set-loading' }, h('span', { class: 'skeleton skeleton-line w-40' })));
      return;
    }
    const s = model.settings;

    const seg = h('div', { class: 'segmented', role: 'group', 'aria-label': '质量门禁' });
    for (const opt of QUALITY_MODES) {
      seg.append(
        h('button', {
          class: 'seg-btn',
          type: 'button',
          'aria-pressed': s.quality_mode === opt.value ? 'true' : 'false',
          title: opt.desc,
          text: opt.label,
          on: {
            click: () => {
              model.settings.quality_mode = opt.value;
              model.dirty = true;
              paintQuality();
            }
          }
        })
      );
    }
    qualityBody.append(
      h(
        'div',
        { class: 'set-row' },
        h(
          'div',
          { class: 'set-row-copy' },
          h('div', { class: 'set-row-title', text: '质量门禁' }),
          h('div', { class: 'set-row-desc', text: '每章写完后的校验强度；「自动修订」会重写不达标章节。' })
        ),
        h('div', { class: 'set-control' }, seg)
      )
    );

    const budget = h('input', {
      class: 'input',
      type: 'number',
      min: '20000',
      max: '400000',
      step: '5000',
      value: String(s.token_budget || 90000),
      'aria-label': '单次上下文预算'
    });
    budget.addEventListener('change', () => {
      model.settings.token_budget = Number(budget.value) || s.token_budget;
      model.dirty = true;
    });
    qualityBody.append(
      h(
        'div',
        { class: 'set-row' },
        h(
          'div',
          { class: 'set-row-copy' },
          h('div', { class: 'set-row-title', text: '上下文预算' }),
          h('div', { class: 'set-row-desc', text: '每次调用允许携带的最大 token 数；百万字长篇建议 90k 以上。' })
        ),
        h('div', { class: 'set-control' }, budget)
      )
    );

    qualityBody.append(
      switchRow('世界书', '维护人物、地点与设定的长期记忆库', s.lorebook, (v) => {
        model.settings.lorebook = v;
        model.dirty = true;
      }),
      switchRow('LLM 调用日志', '记录每次模型的请求与响应，便于排查问题', s.llm_trace, (v) => {
        model.settings.llm_trace = v;
        model.dirty = true;
      })
    );

    const info = h('div', { class: 'set-row' });
    info.append(
      h(
        'div',
        { class: 'set-row-copy' },
        h('div', { class: 'set-row-title', text: '并发与节奏' }),
        h('div', {
          class: 'set-row-desc',
          text: `调用间隔 ${s.call_gap ?? '—'} 秒 · 全局并发上限见下方「模型与岗位」（可编辑）`
        })
      )
    );
    qualityBody.append(info);

    const saveBtn = h('button', {
      class: 'btn btn-primary btn-sm',
      type: 'button',
      text: model.dirty ? '保存改动' : '已保存',
      disabled: !model.dirty || model.saving,
      on: { click: saveSettings }
    });
    qualityBody.append(h('div', { class: 'set-actions' }, saveBtn));
  }

  function switchRow(title, desc, value, onChange) {
    /* tokens.css 的 .switch 原语：aria-checked 挂在开关元素本身，::after 为滑块。 */
    let checked = Boolean(value);
    const sw = h('button', {
      class: 'switch',
      type: 'button',
      role: 'switch',
      'aria-checked': checked ? 'true' : 'false',
      'aria-label': title,
      on: {
        click: () => {
          checked = !checked;
          sw.setAttribute('aria-checked', checked ? 'true' : 'false');
          onChange(checked);
        }
      }
    });
    return h(
      'div',
      { class: 'set-row' },
      h(
        'div',
        { class: 'set-row-copy' },
        h('div', { class: 'set-row-title', text: title }),
        h('div', { class: 'set-row-desc', text: desc })
      ),
      h('div', { class: 'set-control' }, sw)
    );
  }

  async function loadSettings() {
    model.settingsError = '';
    paintQuality();
    try {
      model.settings = await api.settings();
    } catch (err) {
      model.settingsError = errorText(err, '设置加载失败');
    }
    paintQuality();
  }

  async function saveSettings() {
    if (!model.settings || model.saving) return;
    model.saving = true;
    paintQuality();
    try {
      await api.saveSettings({
        quality_mode: model.settings.quality_mode,
        token_budget: Number(model.settings.token_budget) || 90000,
        lorebook: Boolean(model.settings.lorebook),
        llm_trace: Boolean(model.settings.llm_trace)
      });
      model.dirty = false;
      toast('设置已保存', { tone: 'success', duration: 1800 });
    } catch (err) {
      toast(errorText(err, '保存失败'), { tone: 'danger' });
    }
    model.saving = false;
    paintQuality();
  }

  /* ------------------------------------------------------- 模型与密钥 */

  const provSec = h('div', { class: 'set-section' });
  const provHead = h(
    'div',
    { class: 'set-section-head' },
    h('h3', { text: '模型与密钥' }),
    h(
      'button',
      {
        class: 'btn btn-sm',
        type: 'button',
        html: `${icon('plus', 13)}<span>添加供应商</span>`,
        on: { click: () => providerDialog(null) }
      }
    )
  );
  const provBody = h('div', { class: 'provider-list' });
  provSec.append(provHead, provBody);
  pad.append(provSec);

  function paintProviders() {
    provBody.textContent = '';
    if (model.providerError) {
      provBody.append(errorRow(model.providerError, loadProviders));
      return;
    }
    if (!model.providers) {
      provBody.append(h('div', { class: 'set-loading' }, h('span', { class: 'skeleton skeleton-line w-50' })));
      return;
    }
    const providers = model.providers.providers || [];
    if (!providers.length) {
      provBody.append(
        h(
          'div',
          { class: 'empty-state set-empty' },
          h('div', { class: 'empty-icon', html: icon('plug', 18) }),
          h('h3', { text: '还没有模型供应商' }),
          h('p', { text: '添加 OpenAI 兼容接口（DeepSeek / Kimi / 本地 Ollama 都可以），神笔才能开始写作。' }),
          h('button', { class: 'btn btn-sm', type: 'button', text: '添加供应商', on: { click: () => providerDialog(null) } })
        )
      );
      return;
    }
    for (const p of providers) {
      provBody.append(providerRow(p));
    }
    if (!model.providers.key_ok) {
      provBody.append(
        h('div', { class: 'key-state' }, h('span', { html: icon('key', 13) }), h('span', { text: '还没有配置任何密钥，模型调用会失败。' }))
      );
    }
  }

  function providerRow(p) {
    const isDefault = model.providers.default_provider === p.id;
    const row = h('div', { class: 'provider-row', dataset: { id: p.id } });
    row.append(
      h(
        'div',
        { class: 'prov-main' },
        h(
          'div',
          { class: 'prov-name' },
          h('span', { text: p.name || p.id }),
          isDefault ? h('span', { class: 'badge badge-accent', text: '默认' }) : null,
          p.active === false ? h('span', { class: 'badge', dataset: { tone: 'muted' }, text: '停用' }) : null,
          p.local ? h('span', { class: 'badge', dataset: { tone: 'muted' }, text: '本地' }) : null
        ),
        h(
          'div',
          { class: 'prov-meta' },
          h('span', { class: 'mono', text: p.base_url || '—' }),
          h('span', { class: 'sep' }),
          h('span', { text: `${(p.models || []).length} 个模型` }),
          h('span', { class: 'sep' }),
          h('span', { text: p.has_key ? `密钥 ${p.key_display || '已配置'}` : '未配置密钥' })
        )
      ),
      h(
        'div',
        { class: 'prov-actions' },
        !isDefault
          ? h('button', {
              class: 'btn btn-sm',
              type: 'button',
              text: '设为默认',
              on: { click: () => setDefault(p) }
            })
          : null,
        h('button', {
          class: 'btn btn-sm',
          type: 'button',
          text: p.active === false ? '启用' : '停用',
          on: { click: () => toggleActive(p) }
        }),
        h('button', { class: 'btn btn-sm', type: 'button', text: '测连接', on: { click: (e) => testProvider(e, p) } }),
        h('button', {
          class: 'icon-btn icon-btn-sm',
          type: 'button',
          'aria-label': '编辑供应商',
          html: icon('edit', 14),
          on: { click: () => providerDialog(p) }
        }),
        h('button', {
          class: 'icon-btn icon-btn-sm',
          type: 'button',
          'aria-label': '删除供应商',
          html: icon('trash', 14),
          on: { click: () => removeProvider(p) }
        })
      )
    );
    return row;
  }

  async function loadProviders() {
    model.providerError = '';
    paintProviders();
    try {
      model.providers = await api.providers();
    } catch (err) {
      model.providerError = errorText(err, '供应商加载失败');
    }
    paintProviders();
  }

  async function setDefault(p) {
    try {
      await api.setDefaultProvider(p.id);
      await loadProviders();
      toast(`已把 ${p.name} 设为默认`, { tone: 'success', duration: 1800 });
    } catch (err) {
      toast(errorText(err, '设置默认失败'), { tone: 'danger' });
    }
  }

  async function toggleActive(p) {
    try {
      await api.setProviderActive(p.id, p.active === false);
      await loadProviders();
    } catch (err) {
      toast(errorText(err, '操作失败'), { tone: 'danger' });
    }
  }

  async function testProvider(event, p) {
    const btn = event.currentTarget;
    btn.disabled = true;
    btn.textContent = '测试中…';
    try {
      const res = await api.testProvider(p.id);
      toast(`连接正常${res && res.latency_ms ? ` · ${res.latency_ms}ms` : ''}`, { tone: 'success' });
    } catch (err) {
      toast(errorText(err, '连接失败，请检查地址与密钥'), { tone: 'danger' });
    }
    btn.disabled = false;
    btn.textContent = '测连接';
  }

  async function removeProvider(p) {
    const ok = await confirmDialog({
      title: `删除供应商 ${p.name}？`,
      message: '只删除本机配置，不影响服务端账号。',
      confirmText: '删除',
      danger: true
    });
    if (!ok) return;
    try {
      await api.removeProvider(p.id);
      await loadProviders();
      toast('已删除', { duration: 1600 });
    } catch (err) {
      toast(errorText(err, '删除失败'), { tone: 'danger' });
    }
  }

  function providerDialog(existing) {
    const name = h('input', { class: 'input', value: existing ? existing.name : '', placeholder: '例如 DeepSeek', 'aria-label': '名称' });
    const baseUrl = h('input', {
      class: 'input mono',
      value: existing ? existing.base_url : '',
      placeholder: 'https://api.deepseek.com/v1',
      'aria-label': '接口地址'
    });
    const apiKey = h('input', {
      class: 'input mono',
      type: 'password',
      value: '',
      placeholder: existing && existing.has_key ? '已配置，留空保持不变' : 'sk-…',
      'aria-label': 'API 密钥',
      autocomplete: 'off'
    });
    const models = h('textarea', {
      class: 'textarea',
      rows: 3,
      placeholder: '每行一个模型名，例如\ndeepseek-chat\ndeepseek-reasoner',
      'aria-label': '模型列表'
    });
    models.value = existing ? (existing.models || []).join('\n') : '';

    const body = h(
      'div',
      { class: 'wiz-grid' },
      fieldRow('名称', name),
      fieldRow('接口地址（OpenAI 兼容）', baseUrl, 'span-2'),
      fieldRow('API 密钥', apiKey, 'span-2', existing && existing.has_key ? '已保存的密钥不会回显；留空表示不修改。' : ''),
      fieldRow('模型列表', models, 'span-2', '第一行会成为默认模型。')
    );

    const footer = h(
      'div',
      { class: 'set-actions' },
      h('button', { class: 'btn', type: 'button', text: '取消', on: { click: () => modal.close() } }),
      h('button', {
        class: 'btn btn-primary',
        type: 'button',
        text: existing ? '保存' : '添加',
        on: {
          click: async () => {
            const payload = {
              name: name.value.trim(),
              base_url: baseUrl.value.trim(),
              models: models.value.split('\n').map((m) => m.trim()).filter(Boolean)
            };
            if (!payload.name) {
              toast('请填写供应商名称', { tone: 'danger' });
              return;
            }
            if (apiKey.value.trim()) payload.api_key = apiKey.value.trim();
            try {
              if (existing) await api.updateProvider(existing.id, payload);
              else await api.addProvider(payload);
              modal.close();
              await loadProviders();
              toast(existing ? '已保存' : '已添加供应商', { tone: 'success', duration: 1800 });
            } catch (err) {
              toast(errorText(err, '保存失败'), { tone: 'danger' });
            }
          }
        }
      })
    );
    const modal = openModal({ title: existing ? `编辑 ${existing.name}` : '添加模型供应商', body, footer, size: 'md' });
  }

  function fieldRow(label, control, extra = '', hint = '') {
    const wrap = h('div', { class: `wiz-group${extra ? ` ${extra}` : ''}` });
    wrap.append(h('span', { class: 'wiz-label', text: label }), control);
    if (hint) wrap.append(h('span', { class: 'hint-text', text: hint }));
    return wrap;
  }

  /* ------------------------------------------------------- 模型与岗位 */

  const collabSec = h('div', { class: 'set-section' });
  const collabHead = h(
    'div',
    { class: 'set-section-head' },
    h('h3', { text: '模型与岗位' }),
    h(
      'button',
      {
        class: 'btn btn-sm',
        type: 'button',
        html: `${icon('refresh', 13)}<span>重载</span>`,
        on: { click: () => loadAgents() }
      }
    )
  );
  const collabBody = h('div', {});
  collabSec.append(collabHead, collabBody);
  pad.append(collabSec);

  function collabCall(name, ...args) {
    const fn = api && api[name];
    if (typeof fn !== 'function') return Promise.reject(new Error(`api.${name} 尚未接入（core/api.js）`));
    return Promise.resolve().then(() => fn.apply(api, args));
  }

  function collabSourceBadge(a) {
    if (a.provider_id || a.model) return h('span', { class: 'badge badge-accent', text: '本岗位' });
    if (a.stage) return h('span', { class: 'badge', text: '按阶段' });
    return h('span', { class: 'badge', dataset: { tone: 'muted' }, text: '默认' });
  }

  async function loadAgents() {
    model.collabError = '';
    paintAgentsMatrix();
    try {
      model.collab = await collabCall('agents');
    } catch (err) {
      model.collabError = errorText(err, '岗位数据加载失败');
    }
    paintAgentsMatrix();
    /* 技能标题清单异步到位后重绘一次，让绑定 chip 显示中文标题而不是 slug。 */
    loadSkillCatalog().then(() => {
      if (view.isConnected) paintAgentsMatrix();
    });
  }

  async function saveMaxGlobal(btn, input) {
    if (model.collabSaving) return;
    const n = Math.floor(Number(input.value));
    if (!Number.isFinite(n) || n < 1 || n > 64) {
      toast('并发上限请输入 1-64 的整数', { tone: 'danger' });
      input.focus();
      return;
    }
    model.collabSaving = true;
    btn.disabled = true;
    btn.textContent = '保存中…';
    try {
      const res = await collabCall('agentsMaxGlobal', n);
      toast(`全局并发上限已设为 ${res && res.max_global_agents != null ? res.max_global_agents : n}`, { tone: 'success', duration: 2000 });
      await loadAgents();
    } catch (err) {
      toast(errorText(err, '保存失败'), { tone: 'danger' });
      btn.disabled = false;
      btn.textContent = '保存';
    }
    model.collabSaving = false;
  }

  async function setAgentModel(a, providerId, modelId) {
    try {
      await collabCall('agentsSet', { agent: a.agent, provider_id: providerId || '', model: modelId || '' });
      toast(`${a.label}已改用 ${modelId || providerId || '阶段默认'}`, { tone: 'success', duration: 2200 });
      await loadAgents();
    } catch (err) {
      toast(errorText(err, '换模型失败'), { tone: 'danger' });
    }
  }

  function openAgentModelMenu(anchor, a) {
    const options = (model.collab && Array.isArray(model.collab.options) && model.collab.options) || [];
    if (!options.length) {
      toast('还没有可选供应商：先在上方配置模型与密钥', { tone: 'warn', duration: 2600 });
      return;
    }
    const items = [{ label: '回落 stage/默认（解除绑定）', icon: 'refresh', onSelect: () => setAgentModel(a, '', '') }];
    for (const p of options) {
      items.push({ separator: true });
      items.push({
        label: p.name || p.id,
        sub: p.default_model ? `默认 ${p.default_model}` : '',
        icon: 'plug',
        onSelect: () => {
          const models = Array.isArray(p.models) ? p.models : [];
          const sub = [];
          if (p.default_model) {
            sub.push({
              label: '供应商默认模型',
              sub: p.default_model,
              icon: 'dot',
              checked: a.provider_id === p.id && !a.model,
              onSelect: () => setAgentModel(a, p.id, '')
            });
          }
          for (const m of models) {
            sub.push({
              label: String(m),
              icon: 'dot',
              checked: a.provider_id === p.id && a.model === m,
              onSelect: () => setAgentModel(a, p.id, m)
            });
          }
          popoverMenu(anchor, sub, { label: `${p.name || p.id} · 选模型` });
        }
      });
    }
    popoverMenu(anchor, items, { label: `${a.label} · 选供应商` });
  }

  function openAgentSkillsDialog(a) {
    const selected = new Set((Array.isArray(a.skills) ? a.skills : []).map((x) => String(x).trim()).filter(Boolean));
    let catalog = [];

    const search = h('input', {
      class: 'input',
      type: 'search',
      placeholder: '搜索技能名或标题',
      'aria-label': '搜索技能'
    });
    const picker = h('div', { class: 'chip-row skill-pick' });
    const count = h('span', { class: 'hint-text' });

    function syncCount() {
      count.textContent = selected.size ? `已选 ${selected.size} 个技能` : '未绑定技能（该岗位按文体默认注入即可）';
    }

    function paintPicker() {
      picker.textContent = '';
      const q = search.value.trim().toLowerCase();
      const shown = catalog.filter((s) => !q || `${s.name} ${s.title}`.toLowerCase().includes(q));
      if (!shown.length) {
        picker.append(h('span', { class: 'hint-text', text: catalog.length ? '没有匹配的技能' : '技能清单加载中…' }));
        return;
      }
      for (const s of shown) {
        const on = selected.has(s.name);
        const b = h('button', {
          class: 'wiz-chip',
          type: 'button',
          'aria-pressed': on ? 'true' : 'false',
          title: `${s.medium} · ${s.genre} · ${s.name}`
        });
        b.append(h('span', { text: s.title }));
        b.addEventListener('click', () => {
          if (selected.has(s.name)) selected.delete(s.name);
          else selected.add(s.name);
          syncCount();
          paintPicker();
        });
        picker.append(b);
      }
    }

    search.addEventListener('input', paintPicker);
    syncCount();
    paintPicker();
    loadSkillCatalog().then((list) => {
      catalog = list;
      paintPicker();
    });

    const body = h(
      'div',
      { class: 'wiz-grid' },
      fieldRow(`${a.label} · 绑定技能`, h('div', {}, search, picker, count), 'span-2',
        '点选即可；保存后该岗位写正文/审校时会优先注入这些技能。留空保存即清空绑定。')
    );
    const footer = h(
      'div',
      { class: 'set-actions' },
      h('button', { class: 'btn', type: 'button', text: '取消', on: { click: () => modal.close() } }),
      h('button', {
        class: 'btn btn-primary',
        type: 'button',
        text: '保存',
        on: {
          click: async (event) => {
            const btn = event.currentTarget;
            btn.disabled = true;
            try {
              const skills = Array.from(selected);
              await collabCall('agentsSkills', { agent: a.agent, skills });
              toast(`${a.label}技能已保存（${skills.length} 个）`, { tone: 'success', duration: 2000 });
              modal.close();
              await loadAgents();
            } catch (err) {
              if (err && (err.isMissing || err.status === 404)) {
                toast('后端技能绑定接口尚未上线（/api/agents-skills），请稍后重试', { tone: 'warn', duration: 4200 });
              } else {
                toast(errorText(err, '保存技能失败'), { tone: 'danger' });
              }
            }
            btn.disabled = false;
          }
        }
      })
    );
    const modal = openModal({ title: `绑定技能：${a.label}`, body, footer, size: 'md' });
  }

  function agentMatrixRow(a) {
    const modelText = a.model || (a.provider_id ? '供应商默认模型' : a.effective) || '未绑定';
    const copy = h(
      'div',
      { class: 'set-row-copy' },
      h('div', { class: 'set-row-title' }, h('span', { text: a.label || a.agent }), ' ', collabSourceBadge(a), a.stage ? h('span', { class: 'chip chip-muted', text: a.stage }) : null),
      h('div', { class: 'set-row-desc', text: `${modelText}${a.desc ? ` · ${a.desc}` : ''}` })
    );
    /* skills 是并行同事新增字段：缺失就整块不渲染技能区，不能报错。 */
    if (Array.isArray(a.skills) && a.skills.length) {
      const chips = h('div', { class: 'set-skill-chips' });
      for (const sk of a.skills) chips.append(h('span', { class: 'chip', text: SKILL_TITLES[String(sk)] || String(sk) }));
      copy.append(chips);
    }
    return h(
      'div',
      { class: 'set-row' },
      copy,
      h(
        'div',
        { class: 'set-control' },
        h('button', {
          class: 'btn btn-sm',
          type: 'button',
          text: '改',
          title: '为该岗位单独指定供应商/模型',
          on: { click: (event) => openAgentModelMenu(event.currentTarget, a) }
        }),
        h('button', {
          class: 'btn btn-sm',
          type: 'button',
          text: '绑技能',
          on: { click: () => openAgentSkillsDialog(a) }
        })
      )
    );
  }

  function paintAgentsMatrix() {
    collabBody.textContent = '';
    if (model.collabError) {
      collabBody.append(errorRow(model.collabError, loadAgents));
      return;
    }
    if (!model.collab) {
      collabBody.append(h('div', { class: 'set-loading' }, h('span', { class: 'skeleton skeleton-line w-45' })));
      return;
    }
    const view = model.collab;
    const maxInput = h('input', {
      class: 'input',
      type: 'number',
      min: '1',
      max: '64',
      step: '1',
      value: String(view.max_global_agents ?? 4),
      'aria-label': '全局并发上限'
    });
    collabBody.append(
      h(
        'div',
        { class: 'set-row' },
        h(
          'div',
          { class: 'set-row-copy' },
          h('div', { class: 'set-row-title', text: '全局并发上限' }),
          h('div', { class: 'set-row-desc', text: '多 Agent / 多书同时创作时的在飞上限；超限的请求会排队，防止供应商 429。' })
        ),
        h(
          'div',
          { class: 'set-control' },
          maxInput,
          h('button', {
            class: 'btn btn-primary btn-sm',
            type: 'button',
            text: '保存',
            on: { click: (event) => saveMaxGlobal(event.currentTarget, maxInput) }
          })
        )
      )
    );

    const agents = Array.isArray(view.agents) ? view.agents : [];
    if (!agents.length) {
      collabBody.append(h('div', { class: 'hint-text', text: '后端没有返回岗位数据（/api/agents 可能仍是旧版本）。' }));
      return;
    }
    for (const a of agents) collabBody.append(agentMatrixRow(a));
  }

  /* --------------------------------------------------------- 关于 */

  const aboutSec = h(
    'div',
    { class: 'set-section' },
    h('div', { class: 'set-section-head' }, h('h3', { text: '技能与数据' })),
    h(
      'div',
      { class: 'set-row' },
      h(
        'div',
        { class: 'set-row-copy' },
        h('div', { class: 'set-row-title', text: '技能进化' }),
        h('div', { class: 'set-row-desc', text: '查看候选技能、反馈样本与版本历史；用得越多，写作越贴合你的口味。' })
      ),
      h(
        'div',
        { class: 'set-control' },
        h('button', { class: 'btn btn-sm', type: 'button', text: '打开', on: { click: () => ctx.navigate('/evolution') } })
      )
    ),
    h(
      'div',
      { class: 'set-row' },
      h(
        'div',
        { class: 'set-row-copy' },
        h('div', { class: 'set-row-title', text: '数据都在本地' }),
        h('div', {
          class: 'set-row-desc',
          text: '作品、参考资料与设置保存在本机目录；作品支持导出 Markdown / Word / EPUB。'
        })
      )
    )
  );
  pad.append(aboutSec);

  /* ----------------------------------------------------------- 高级 */

  const advSec = h('div', { class: 'set-section' });
  const advBody = h('div', {});
  advSec.append(h('div', { class: 'set-section-head' }, h('h3', { text: '高级' })), advBody);
  pad.append(advSec);

  function paintAdvanced() {
    advBody.textContent = '';
    advBody.append(
      switchRow(
        '在侧栏显示经典版入口',
        '旧版单栏界面仍然可用，只是默认不再出现在左侧。需要长期切回去写，再把它固定在侧栏。',
        store.get().ui.showClassic,
        (v) => {
          setPref('showClassic', v);
          ctx.rerenderPanel();
        }
      ),
      h(
        'div',
        { class: 'set-row' },
        h(
          'div',
          { class: 'set-row-copy' },
          h('div', { class: 'set-row-title', text: '经典版界面' }),
          h('div', {
            class: 'set-row-desc',
            text: '作品、模型与设置完全共用，只是排版是旧版单栏；打开后停在 /classic，不会改动任何数据。'
          })
        ),
        h(
          'div',
          { class: 'set-control' },
          h('button', {
            class: 'btn btn-sm',
            type: 'button',
            html: `${icon('external-link', 13)}<span>打开经典版</span>`,
            on: { click: () => { window.location.href = '/classic'; } }
          })
        )
      )
    );
  }

  /* ----------------------------------------------------------- start */

  paintTheme();
  paintAdvanced();
  loadSettings();
  loadProviders();
  loadAgents();

  let lastTheme = store.get().theme;
  const unsubTheme = store.subscribe((state) => {
    if (state.theme !== lastTheme) {
      lastTheme = state.theme;
      paintTheme();
    }
  });

  requestAnimationFrame(() => {
    const tab = store.get().ui.settingsTab;
    if (!tab) return;
    const target =
      tab === 'providers' ? provSec : tab === 'quality' ? qualitySec : tab === 'appearance' ? themeSec : null;
    if (target) target.scrollIntoView({ block: 'start' });
    patchUi({ settingsTab: '' });
  });

  return {
    el: view,
    refresh() {
      paintTheme();
      loadSettings();
      loadProviders();
      loadAgents();
    },
    destroy() {
      unsubTheme();
      view.remove();
    }
  };
}
