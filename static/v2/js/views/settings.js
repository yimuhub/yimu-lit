/* 设置：分组标签页布局（v2.8.0 重构）。
   左侧竖向导航 + 右侧内容区，分组：外观 / 创作质量 / 模型与供应商 /
   创作岗位 / 对话与上下文 / 技能与数据 / 高级。
   只发送后端支持的字段，其余只读展示。 */

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

/* 分组定义：key 同时是深链（ui.settingsTab）与导航高亮的依据。 */
const GROUPS = [
  { key: 'appearance', label: '外观', icon: 'palette', desc: '主题与显示' },
  { key: 'quality', label: '创作质量', icon: 'shield', desc: '门禁 · 预算 · 记录' },
  { key: 'providers', label: '模型与供应商', icon: 'plug', desc: '接口地址与密钥' },
  { key: 'agents', label: '创作岗位', icon: 'layers', desc: '岗位 × 模型 × 技能' },
  { key: 'chat', label: '对话与上下文', icon: 'message', desc: '会话留档与压缩' },
  { key: 'skills', label: '技能与数据', icon: 'sparkles', desc: '进化与本地数据' },
  { key: 'pennames', label: '作者笔名', icon: 'user', desc: '全局默认 · 多笔名 · 按文体默认' },
  { key: 'advanced', label: '高级', icon: 'sliders', desc: '经典版等' }
];

/* 旧深链值 → 新分组 key（其他页面跳转 settingsTab 的兼容映射）。 */
const LEGACY_TAB = { appearance: 'appearance', quality: 'quality', providers: 'providers' };

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
    pennames: null,
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

  /* --------------------------------------------------- 分组标签页骨架 */

  const layout = h('div', { class: 'set-layout' });
  const nav = h('nav', { class: 'set-nav', 'aria-label': '设置分组' });
  const content = h('div', { class: 'set-content' });
  layout.append(nav, content);
  pad.append(layout);

  const panes = {};   // key -> pane 元素
  const navBtns = {}; // key -> 导航按钮

  for (const g of GROUPS) {
    const pane = h('section', { class: 'set-group', dataset: { group: g.key } });
    pane.append(
      h(
        'div',
        { class: 'set-group-head' },
        h('span', { class: 'set-group-icon', html: icon(g.icon, 15) }),
        h('div', {},
          h('h2', { text: g.label }),
          h('p', { class: 'hint-text', text: g.desc })
        )
      )
    );
    panes[g.key] = pane;
    content.append(pane);
    const btn = h('button', {
      class: 'set-nav-item',
      type: 'button',
      'aria-current': 'false',
      dataset: { group: g.key },
      on: { click: () => showGroup(g.key) }
    },
      h('span', { class: 'set-nav-icon', html: icon(g.icon, 14) }),
      h('span', { class: 'set-nav-copy' },
        h('span', { class: 'set-nav-label', text: g.label }),
        h('span', { class: 'set-nav-desc', text: g.desc })
      )
    );
    navBtns[g.key] = btn;
    nav.append(btn);
  }

  function showGroup(key) {
    if (!panes[key]) key = 'appearance';
    for (const [k, pane] of Object.entries(panes)) {
      pane.hidden = k !== key;
    }
    for (const [k, btn] of Object.entries(navBtns)) {
      btn.setAttribute('aria-current', k === key ? 'true' : 'false');
    }
    patchUi({ settingsTab: key });
  }

  /* ----------------------------------------------------------- 外观 */

  const themeBody = h('div', { class: 'theme-options' });
  panes.appearance.append(
    h('div', { class: 'set-row' },
      h('div', { class: 'set-row-copy' },
        h('div', { class: 'set-row-title', text: '界面主题' }),
        h('div', { class: 'set-row-desc', text: '跟随系统会自动在浅色 / 深色之间切换。' })
      ),
      h('div', { class: 'set-control' }, themeBody)
    )
  );

  function paintTheme() {
    themeBody.textContent = '';
    const current = store.get().theme;
    for (const opt of THEME_OPTIONS) {
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
      themeBody.append(btn);
    }
  }

  /* ------------------------------------------------------- 创作质量 */

  const qualityBody = h('div', {});
  panes.quality.append(qualityBody);

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
          h('div', { class: 'set-row-desc', text: '每次写作调用允许携带的最大 token 数；百万字长篇建议 90k 以上。' })
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
      }),
      h(
        'div',
        { class: 'set-row' },
        h(
          'div',
          { class: 'set-row-copy' },
          h('div', { class: 'set-row-title', text: '调用间隔' }),
          h('div', { class: 'set-row-desc', text: `两次模型调用之间的最小间隔：当前 ${s.call_gap ?? '—'} 秒（防 429 限流；在 config.json 的 defaults.call_gap 调整）。` })
        )
      )
    );

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
    paintChatCtx();
    try {
      model.settings = await api.settings();
    } catch (err) {
      model.settingsError = errorText(err, '设置加载失败');
    }
    paintQuality();
    paintChatCtx();
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
        llm_trace: Boolean(model.settings.llm_trace),
        chat_context_limit: model.settings.chat_context_limit || '200k',
        chat_auto_compress: model.settings.chat_auto_compress !== false
      });
      model.dirty = false;
      paintChatCtx();
      toast('设置已保存', { tone: 'success', duration: 1800 });
    } catch (err) {
      toast(errorText(err, '保存失败'), { tone: 'danger' });
    }
    model.saving = false;
    paintQuality();
  }

  /* ---------------------------------------------------- 模型与供应商 */

  const provBody = h('div', { class: 'provider-list' });
  panes.providers.append(
    h('div', { class: 'set-actions set-actions-top' },
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        html: `${icon('plus', 13)}<span>添加供应商</span>`,
        on: { click: () => providerDialog(null) }
      })
    ),
    provBody
  );

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
          h('p', { text: '添加 OpenAI 兼容接口（DeepSeek / Kimi / 本地 Ollama 都可以），一幕·YIMU 神笔才能开始写作。' }),
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

  /* --------------------------------------------------------- 创作岗位 */

  const collabBody = h('div', {});
  panes.agents.append(
    h('div', { class: 'set-actions set-actions-top' },
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        html: `${icon('refresh', 13)}<span>重载</span>`,
        on: { click: () => loadAgents() }
      }),
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        html: `${icon('plus', 13)}<span>添加自定义岗位</span>`,
        on: { click: () => addAgentDialog() }
      })
    ),
    collabBody
  );

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
      toast('还没有可选供应商：先在「模型与供应商」里配置', { tone: 'warn', duration: 2600 });
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
      /* v3.0.0：按文体分组展示（小说/剧本/诗词/散文/通用），每组一行小标题。 */
      picker.textContent = '';
      const q = search.value.trim().toLowerCase();
      const shown = catalog.filter((s) => !q || `${s.name} ${s.title}`.toLowerCase().includes(q));
      if (!shown.length) {
        picker.append(h('span', { class: 'hint-text', text: catalog.length ? '没有匹配的技能' : '技能清单加载中…' }));
        return;
      }
      const groups = new Map();
      for (const s of shown) {
        const g = s.medium || '通用';
        if (!groups.has(g)) groups.set(g, []);
        groups.get(g).push(s);
      }
      for (const [medium, list] of groups) {
        picker.append(h('div', { class: 'skill-pick-group' },
          h('span', { class: 'skill-pick-group-label', text: `${medium} · ${list.length}` })));
        for (const s of list) {
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
              toast(errorText(err, '保存技能失败'), { tone: 'danger' });
            }
            btn.disabled = false;
          }
        }
      })
    );
    const modal = openModal({ title: `绑定技能：${a.label}`, body, footer, size: 'md' });
  }

  /* v2.8.0：添加自定义创作岗位（调 /api/agents/add，此前只有后端没有入口）。 */
  function addAgentDialog() {
    const label = h('input', { class: 'input', placeholder: '例如 考据师', 'aria-label': '岗位名称' });
    const name = h('input', { class: 'input mono', placeholder: '英文标识，例如 fact_checker（留空自动生成）', 'aria-label': '岗位标识' });
    const desc = h('textarea', {
      class: 'textarea', rows: 3,
      placeholder: '这个岗位负责什么？例如：核对历史细节、官职、器物名称，写考据备注…',
      'aria-label': '职责描述'
    });
    const stage = h('select', { class: 'input', 'aria-label': '工作阶段' },
      h('option', { value: 'planning', text: '规划阶段（大纲/设定）' }),
      h('option', { value: 'writing', text: '写作阶段（正文）', selected: true }),
      h('option', { value: 'review', text: '审校阶段（评分/修订）' })
    );
    const selected = new Set();
    let catalog = [];
    const picker = h('div', { class: 'chip-row skill-pick' });
    function paintPicker() {
      picker.textContent = '';
      if (!catalog.length) { picker.append(h('span', { class: 'hint-text', text: '技能清单加载中…' })); return; }
      for (const s of catalog) {
        const on = selected.has(s.name);
        const b = h('button', { class: 'wiz-chip', type: 'button', 'aria-pressed': on ? 'true' : 'false', title: `${s.medium} · ${s.genre}` },
          h('span', { text: s.title }));
        b.addEventListener('click', () => {
          if (selected.has(s.name)) selected.delete(s.name); else selected.add(s.name);
          paintPicker();
        });
        picker.append(b);
      }
    }
    loadSkillCatalog().then((list) => { catalog = list; paintPicker(); });

    const body = h('div', { class: 'wiz-grid' },
      fieldRow('岗位名称', label),
      fieldRow('岗位标识', name, '', '英文/下划线；留空则由描述自动生成。'),
      fieldRow('职责描述', desc, 'span-2', '会显示在岗位列表，并作为该岗位的系统提示一部分。'),
      fieldRow('工作阶段', stage),
      fieldRow('默认技能', picker, 'span-2', '可选；保存后可随时在岗位行上修改。')
    );
    const footer = h('div', { class: 'set-actions' },
      h('button', { class: 'btn', type: 'button', text: '取消', on: { click: () => modal.close() } }),
      h('button', {
        class: 'btn btn-primary', type: 'button', text: '创建岗位',
        on: {
          click: async (event) => {
            const btn = event.currentTarget;
            if (!label.value.trim() && !desc.value.trim()) {
              toast('请至少填写岗位名称或职责描述', { tone: 'danger' });
              return;
            }
            btn.disabled = true;
            btn.textContent = '创建中…';
            try {
              await api.addAgent({
                name: name.value.trim(),
                label: label.value.trim(),
                desc: desc.value.trim(),
                stage: stage.value,
                skills: Array.from(selected)
              });
              toast(`已创建岗位「${label.value.trim() || desc.value.trim().slice(0, 8)}」`, { tone: 'success' });
              modal.close();
              await loadAgents();
            } catch (err) {
              toast(errorText(err, '创建失败'), { tone: 'danger' });
              btn.disabled = false;
              btn.textContent = '创建岗位';
            }
          }
        }
      })
    );
    const modal = openModal({ title: '添加自定义创作岗位', body, footer, size: 'md' });
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
          text: '改模型',
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
    const viewData = model.collab;
    const maxInput = h('input', {
      class: 'input',
      type: 'number',
      min: '1',
      max: '64',
      step: '1',
      value: String(viewData.max_global_agents ?? 4),
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
      ),
      h('div', { class: 'set-subhead' }, h('h3', { text: '创作岗位' }), h('span', { class: 'hint-text', text: '每个岗位可单独指定模型与技能；绑定在此处全局生效。' }))
    );

    const agents = Array.isArray(viewData.agents) ? viewData.agents : [];
    if (!agents.length) {
      collabBody.append(h('div', { class: 'hint-text', text: '后端没有返回岗位数据（/api/agents 可能仍是旧版本）。' }));
      return;
    }
    for (const a of agents) collabBody.append(agentMatrixRow(a));
  }

  /* ---------------------------------------------------- 对话与上下文 */

  const chatBody = h('div', {});
  panes.chat.append(chatBody);

  function paintChatCtx() {
    chatBody.textContent = '';
    if (model.settingsError || !model.settings) {
      chatBody.append(h('div', { class: 'set-loading' }, h('span', { class: 'skeleton skeleton-line w-40' })));
      return;
    }
    const s = model.settings;
    const seg = h('div', { class: 'segmented', role: 'group', 'aria-label': '上下文窗口' });
    for (const [k, v] of Object.entries({ '200k': '200K', '400k': '400K', '500k': '500K', '1m': '1M' })) {
      seg.append(h('button', {
        class: 'seg-btn', type: 'button',
        'aria-pressed': s.chat_context_limit === k ? 'true' : 'false',
        text: v,
        title: `${v} tokens`,
        on: { click: () => { model.settings.chat_context_limit = k; model.dirty = true; paintChatCtx(); } }
      }));
    }
    chatBody.append(
      h('div', { class: 'set-row' },
        h('div', { class: 'set-row-copy' },
          h('div', { class: 'set-row-title', text: '会话留档' }),
          h('div', { class: 'set-row-desc', text: '小说会话的全部对话（含写作过程中断的回复）都会完整保存在作品目录的 chat_log.jsonl 里，进入作品即可回读。' })
        ),
        h('div', { class: 'set-control' },
          h('span', { class: 'badge badge-accent', text: '已启用' }))
      ),
      switchRow('上下文自动压缩', '对话展示超过上下文窗口后，自动把早期对话压缩成「创作进度摘要」并注入对话 Agent；后台创作不受影响。', s.chat_auto_compress, (v) => {
        model.settings.chat_auto_compress = v;
        model.dirty = true;
        paintQuality();
      }),
      h('div', { class: 'set-row' },
        h('div', { class: 'set-row-copy' },
          h('div', { class: 'set-row-title', text: '上下文窗口' }),
          h('div', { class: 'set-row-desc', text: '超过该 token 数后自动压缩早期对话；按模型上下文能力选择。默认 200K。' })
        ),
        h('div', { class: 'set-control' }, seg)
      )
    );
    const saveBtn = h('button', {
      class: 'btn btn-primary btn-sm',
      type: 'button',
      text: model.dirty ? '保存改动' : '已保存',
      disabled: !model.dirty || model.saving,
      on: { click: saveSettings }
    });
    chatBody.append(h('div', { class: 'set-actions' }, saveBtn));
  }
  paintChatCtx();

  /* ------------------------------------------------------ 技能与数据 */

  panes.skills.append(
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
        h('div', { class: 'set-row-title', text: '技能库' }),
        h('div', { class: 'set-row-desc', text: '浏览与检索全部写作技能（四文体 × 题材）。岗位绑定的技能都来自这里。' })
      ),
      h(
        'div',
        { class: 'set-control' },
        h('button', { class: 'btn btn-sm', type: 'button', text: '打开', on: { click: () => ctx.navigate('/skills') } })
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

  /* ----------------------------------------------------------- 高级 */

  /* ----------------------------------------------------------- 作者笔名 */

  const penBody = h('div', {});
  panes.pennames.append(penBody);

  function paintPennames() {
    penBody.textContent = '';
    if (!model.pennames) {
      penBody.append(errorRow('笔名数据加载失败', loadPennames));
      return;
    }
    const pn = model.pennames;
    const list = pn.list || [];
    const defaultName = pn.default || '';

    // 默认笔名选择
    const defaultRow = h('div', { class: 'set-row' },
      h('div', { class: 'set-row-copy' },
        h('div', { class: 'set-row-title', text: '全局默认笔名' }),
        h('div', { class: 'set-row-desc', text: '新建作品时默认使用的作者名；可在每个向导里单独选择其他笔名。' })
      ),
      h('select', { class: 'set-select', on: {
        change: async (e) => {
          const val = e.target.value;
          try {
            await api.pennamesSave({ list, default: val });
            await loadPennames();
          } catch (err) { toast(errorText(err, '保存失败'), { tone: 'danger' }); }
        }
      } },
        h('option', { value: '', text: '（未设置）' }),
        ...list.map(c => h('option', {
          value: c.name, selected: c.name === defaultName,
          text: c.name + (c.desc ? `（${c.desc}）` : '')
        }))
      )
    );
    penBody.append(defaultRow);

    // 笔名列表
    penBody.append(h('div', { class: 'set-group-subhead', text: '笔名列表' }));
    const listWrap = h('div', { class: 'set-list' });
    if (!list.length) {
      listWrap.append(h('div', { class: 'hint-text', text: '还没有笔名。点击下方按钮添加。' }));
    }
    for (const c of list) {
      const row = h('div', { class: 'set-row' },
        h('div', { class: 'set-row-copy' },
          h('div', { class: 'set-row-title', text: c.name + (c.name === defaultName ? '  ★默认' : '') }),
          h('div', { class: 'set-row-desc', text: c.desc || '（无描述）' })
        ),
        h('div', { class: 'set-control' },
          h('button', { class: 'btn btn-sm btn-danger', type: 'button', text: '删除', on: {
            click: async () => {
              try {
                await api.pennamesDelete({ name: c.name });
                await loadPennames();
              } catch (err) { toast(errorText(err, '删除失败'), { tone: 'danger' }); }
            }
          } })
        )
      );
      listWrap.append(row);
    }
    penBody.append(listWrap);

    // 添加笔名
    const addRow = h('div', { class: 'set-row' },
      h('div', { class: 'set-row-copy' },
        h('div', { class: 'set-row-title', text: '添加笔名' }),
        h('div', { class: 'set-row-desc', text: '可为不同文体风格设置不同笔名，向导中会以备选出现。' })
      ),
      h('div', { class: 'set-control' },
        h('button', { class: 'btn btn-sm btn-accent', type: 'button', text: '添加笔名', on: {
          click: () => openAddPennameDialog()
        } })
      )
    );
    penBody.append(addRow);
  }

  function openAddPennameDialog() {
    const nameInput = h('input', { class: 'set-input', placeholder: '笔名（必填）' });
    const descInput = h('input', { class: 'set-input', placeholder: '描述（选填）' });
    const body = h('div', { class: 'wiz-grid' },
      h('div', { class: 'field' }, h('span', { class: 'field-label', text: '笔名' }), nameInput),
      h('div', { class: 'field' }, h('span', { class: 'field-label', text: '描述' }), descInput)
    );
    const footer = h('div', { class: 'set-actions' },
      h('button', { class: 'btn', type: 'button', text: '取消', on: { click: () => modal.close() } }),
      h('button', { class: 'btn btn-primary', type: 'button', text: '添加', on: {
        click: async () => {
          const name = nameInput.value.trim();
          if (!name) { toast('笔名不能为空', { tone: 'danger' }); return; }
          const newList = [...(model.pennames.list || []), { name, desc: descInput.value.trim() }];
          try {
            await api.pennamesSave({ list: newList, default: model.pennames.default || '' });
            await loadPennames();
            modal.close();
          } catch (err) { toast(errorText(err, '添加失败'), { tone: 'danger' }); }
        }
      } })
    );
    const modal = openModal({ title: '添加作者笔名', body, footer, size: 'sm' });
  }

  async function loadPennames() {
    try {
      model.pennames = await api.pennames();
      paintPennames();
    } catch (err) {
      model.pennames = null;
      paintPennames();
    }
  }

  const advBody = h('div', {});
  panes.advanced.append(advBody);

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

  /* 初始分组：优先恢复上次浏览的分组，其次兼容旧深链值。 */
  const wantTab = store.get().ui.settingsTab;
  showGroup(LEGACY_TAB[wantTab] || (GROUPS.some((g) => g.key === wantTab) ? wantTab : 'appearance'));

  let lastTheme = store.get().theme;
  const unsubTheme = store.subscribe((state) => {
    if (state.theme !== lastTheme) {
      lastTheme = state.theme;
      paintTheme();
    }
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
