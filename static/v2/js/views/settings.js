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
  { key: 'pennames', label: '作者笔名', icon: 'pen', desc: '全局默认 · 多笔名 · 按文体默认' },
  { key: 'workdiag', label: '作品诊断', icon: 'eye', desc: '创作状态 · 卡顿排查 · 导出' },
  { key: 'syslog', label: '系统日志', icon: 'terminal', desc: '版本 · 系统操作 · 服务日志 · 导出' },
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
    if (key === 'syslog' || key === 'workdiag') loadDiagnostics();   // 每次进入日志/诊断分组都刷新
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
      min: '0',
      max: '2000000',
      step: '10000',
      value: String(s.token_budget > 0 ? s.token_budget : 90000),
      'aria-label': '全书累计 token 预算'
    });
    /* v4.15.10：「不限制预算」——token_budget=0 后端即不拦截（budget_blocked cap=0 放行）。
       此前前端保存兜底 || 90000 把 0 强制变回 9 万，不限制状态存不进去。 */
    const unlimited = h('input', { type: 'checkbox', checked: !(s.token_budget > 0), 'aria-label': '不限制预算' });
    function syncBudgetDisabled() {
      budget.disabled = unlimited.checked;
      if (unlimited.checked) budget.value = '90000';
    }
    unlimited.addEventListener('change', () => {
      model.settings.token_budget = unlimited.checked ? 0 : (Number(budget.value) || 90000);
      model.dirty = true;
      syncBudgetDisabled();
    });
    budget.addEventListener('change', () => {
      model.settings.token_budget = Number(budget.value) || 0;
      model.dirty = true;
    });
    syncBudgetDisabled();
    /* v4.18.0：评分专用模型——空 = 跟随创作模型；指定后逐章评分与全书审校都用它 */
    const scoreSel = h('select', { class: 'input', 'aria-label': '评分专用模型', on: {
      change: () => {
        model.settings.score_provider_id = scoreSel.value.split('::')[0] || '';
        model.settings.score_model = scoreSel.value.split('::').slice(1).join('::');
        model.dirty = true;
      }
    } });
    const paintScoreSel = () => {
      const st = model.settings || {};
      const cur = `${st.score_provider_id || ''}::${st.score_model || ''}`;
      scoreSel.textContent = '';
      scoreSel.append(h('option', { value: '', text: '跟随创作模型（默认）' }));
      for (const o of (model.scoreModelOptions || [])) {
        scoreSel.append(h('option', { value: o.value, selected: o.value === cur, text: o.label || o.value }));
      }
    };
    api.agents?.().then((v) => {
      const opts = [];
      for (const prov of ((v && v.options) || [])) {
        for (const m of (prov.models || [])) {
          opts.push({ value: `${prov.id}::${m}`, label: `${prov.name || prov.id} · ${m}` });
        }
      }
      model.scoreModelOptions = opts;
      paintScoreSel();
    }).catch(() => paintScoreSel());
    qualityBody.append(
      h(
        'div',
        { class: 'set-row' },
        h(
          'div',
          { class: 'set-row-copy' },
          h('div', { class: 'set-row-title', text: '评分专用模型' }),
          h('div', { class: 'set-row-desc', text: '逐章评分与全书审校专用的大模型；可选便宜快速的模型降成本，留空跟随创作模型。评分按六维锚点标准（0-10 判分）执行。' })
        ),
        h('div', { class: 'set-control' }, scoreSel)
      )
    );
    qualityBody.append(
      h(
        'div',
        { class: 'set-row' },
        h(
          'div',
          { class: 'set-row-copy' },
          h('div', { class: 'set-row-title', text: '上下文预算' }),
          h('div', { class: 'set-row-desc', text: '单本书累计 token 消耗上限，超出即暂停写作（防止烧穿额度）；百万字长篇建议 90 万以上。' })
        ),
        h('div', { class: 'set-control' },
          h('label', { class: 'set-check', style: 'display:flex;align-items:center;gap:6px;margin-right:10px;font-size:12px' },
            unlimited, h('span', { text: '不限制' })),
          budget)
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
        token_budget: (model.settings.token_budget === 0) ? 0 : (Number(model.settings.token_budget) || 90000),
        score_provider_id: model.settings.score_provider_id || '',
        score_model: model.settings.score_model || '',
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
  const globalModelBody = h('div', {});
  panes.providers.append(
    h('div', { class: 'set-actions set-actions-top' },
      h('button', {
        class: 'btn btn-sm',
        type: 'button',
        html: `${icon('plus', 13)}<span>添加供应商</span>`,
        on: { click: () => providerDialog(null) }
      })
    ),
    globalModelBody,
    provBody
  );

  /* v4.20：全局默认模型（default_provider + 其 default_model），供应商列表加载后重绘。 */
  function paintGlobalModel() {
    globalModelBody.textContent = '';
    if (model.providerError || !model.providers) return;
    const provs = (model.providers.providers || []).filter((p) => p.active !== false);
    const options = [];
    for (const p of provs) {
      for (const m of (p.models || [])) {
        options.push({ value: `${p.id}::${m}`, label: `${p.name || p.id} · ${m}` });
      }
    }
    if (!options.length) return;
    const dp = model.providers.default_provider;
    const dpRow = provs.find((p) => p.id === dp);
    const curModel = dpRow ? (dpRow.default_model || (dpRow.models || [])[0]) : '';
    const curValue = dp && curModel ? `${dp}::${curModel}` : '';
    const sel = h('select', { class: 'input', 'aria-label': '全局默认模型' },
      options.map((o) => h('option', { value: o.value, text: o.label, selected: o.value === curValue })));
    if (curValue) sel.value = curValue;
    globalModelBody.append(
      h('div', { class: 'set-row' },
        h('div', { class: 'set-row-copy' },
          h('div', { class: 'set-row-title' }, h('span', { text: '全局默认模型' }),
            h('span', { class: 'badge badge-accent', text: '默认' })),
          h('div', { class: 'set-row-desc', text: '所有创作岗位默认使用该模型；岗位在「创作岗位」里单独绑定模型时以绑定为准。' })
        ),
        h('div', { class: 'set-control' },
          sel,
          h('button', {
            class: 'btn btn-primary btn-sm',
            type: 'button',
            text: '保存',
            on: { click: (event) => saveGlobalModel(event.currentTarget, sel) }
          })
        )
      )
    );
  }

  async function saveGlobalModel(btn, sel) {
    const v = String(sel.value || '');
    const idx = v.indexOf('::');
    const providerId = idx >= 0 ? v.slice(0, idx) : '';
    const m = idx >= 0 ? v.slice(idx + 2) : '';
    if (!providerId || !m) {
      toast('请先选择全局默认模型', { tone: 'danger' });
      return;
    }
    btn.disabled = true;
    btn.textContent = '保存中…';
    try {
      await api.globalModelSet({ provider_id: providerId, model: m });
      await loadProviders();
      const opt = sel.options[sel.selectedIndex];
      toast(`全局默认模型已设为 ${opt ? opt.textContent : `${providerId} · ${m}`}`, { tone: 'success', duration: 2200 });
    } catch (err) {
      toast(errorText(err, '保存失败'), { tone: 'danger' });
    }
    btn.disabled = false;
    btn.textContent = '保存';
  }

  function paintProviders() {
    provBody.textContent = '';
    paintGlobalModel();
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

  /* v4.19.0：供应商编辑重构——填完地址与密钥一键拉取全部模型，复选框勾选，
     取代旧的「下拉逐个追加」。勾选顺序即优先级，第一个为默认模型；
     /models 拉不到时保留手动输入兜底。 */
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

    /* ---------------- 模型选择器状态 ---------------- */
    let selected = ((existing && existing.models) || []).map((m) => String(m).trim()).filter(Boolean); // 顺序即优先级
    let fetched = [];        // 拉取到的全部模型 id
    let fetching = false;

    const fetchBtn = h('button', {
      class: 'btn btn-primary btn-sm',
      type: 'button',
      text: '⟳ 拉取模型列表'
    });
    const fetchState = h('span', {
      class: 'pe-fetch-state',
      text: existing && existing.has_key
        ? '未重填密钥时，将自动使用已保存的密钥拉取'
        : '填好接口地址（密钥选填）后点左侧按钮，自动获取全部可用模型'
    });
    const searchIn = h('input', { class: 'input mp-search', type: 'search', placeholder: '搜索模型名…', 'aria-label': '搜索模型' });
    const listEl = h('div', { class: 'mp-list', role: 'group', 'aria-label': '候选模型' });
    const pickedCount = h('span', { class: 'mp-count' });
    const chipsEl = h('div', { class: 'mp-chips' });
    const pickerEl = h('div', { class: 'mp-picker', style: 'display:none' },
      h('div', { class: 'mp-head' },
        searchIn,
        h('button', { class: 'btn btn-sm mp-act', type: 'button', text: '全选', title: '勾选当前搜索结果里的全部模型', on: { click: () => { for (const id of visibleIds()) toggleModel(id, true); } } }),
        h('button', { class: 'btn btn-sm mp-act', type: 'button', text: '清空', title: '取消全部勾选', on: { click: () => { selected = []; paintAll(); } } })
      ),
      listEl
    );
    const manualIn = h('input', { class: 'input mono mp-manual', placeholder: '列表拉不到？手动输入模型名后点「添加」', 'aria-label': '手动添加模型' });

    function visibleIds() {
      const q = searchIn.value.trim().toLowerCase();
      return fetched.filter((id) => !q || String(id).toLowerCase().includes(q));
    }

    function toggleModel(id, on) {
      const i = selected.indexOf(id);
      if (on && i < 0) selected.push(id);
      if (!on && i >= 0) selected.splice(i, 1);
      paintList();
      paintChips();
    }

    function paintList() {
      listEl.textContent = '';
      const ids = visibleIds();
      if (!ids.length) {
        listEl.append(h('div', { class: 'mp-empty', text: fetched.length ? '没有匹配的模型' : '尚未拉取模型列表' }));
        return;
      }
      for (const id of ids) {
        const on = selected.includes(id);
        const cb = h('input', { type: 'checkbox', checked: on, 'aria-label': `选择模型 ${id}` });
        cb.addEventListener('change', () => toggleModel(id, cb.checked));
        listEl.append(h('label', { class: 'mp-item', dataset: { on: on ? 'true' : 'false' } }, cb, h('span', { class: 'mono', text: id })));
      }
    }

    function paintChips() {
      chipsEl.textContent = '';
      pickedCount.textContent = selected.length ? `已选 ${selected.length} 个` : '未选择';
      if (!selected.length) {
        chipsEl.append(h('span', { class: 'hint-text', text: '勾选上方列表或手动添加；第一个勾选的模型将作为默认模型。' }));
        return;
      }
      selected.forEach((id, i) => {
        chipsEl.append(h('span', { class: 'mp-chip', dataset: { default: i === 0 ? 'true' : 'false' } },
          i === 0 ? h('span', { class: 'mp-chip-badge', text: '默认' }) : null,
          h('span', { class: 'mp-chip-name mono', text: id, title: id }),
          i !== 0 ? h('button', { class: 'mp-chip-btn', type: 'button', text: '★', title: '设为默认模型', on: { click: () => { selected.splice(i, 1); selected.unshift(id); paintChips(); } } }) : null,
          h('button', { class: 'mp-chip-btn', type: 'button', text: '×', title: '移除', 'aria-label': `移除模型 ${id}`, on: { click: () => toggleModel(id, false) } })
        ));
      });
    }

    function paintAll() { paintList(); paintChips(); }

    async function doFetch() {
      if (fetching) return;
      if (!baseUrl.value.trim()) { toast('请先填写接口地址', { tone: 'danger' }); baseUrl.focus(); return; }
      fetching = true;
      fetchBtn.disabled = true; fetchBtn.textContent = '拉取中…';
      fetchState.textContent = '正在从接口拉取模型列表…';
      fetchState.dataset.tone = '';
      try {
        /* v4.19.1：编辑态密钥框留空时带 provider_id——后端自动回落已保存的密钥。
           此前把空密钥直接发给接口，401 后用户误以为密钥丢失。 */
        const res = await api.fetchModels({
          base_url: baseUrl.value.trim(),
          api_key: apiKey.value.trim(),
          provider_id: existing ? existing.id : ''
        });
        const list = (res && res.models) || [];
        if (!list.length) {
          fetchState.textContent = '接口返回了空列表，请在下方手动输入模型名';
          fetchState.dataset.tone = 'warn';
          toast('接口返回空列表，请手填', { tone: 'warn', duration: 4000 });
          return;
        }
        fetched = list;
        pickerEl.style.display = '';
        paintAll();
        fetchState.textContent = res && res.used_saved_key
          ? `已用已保存的密钥拉取成功：共 ${list.length} 个模型，勾选需要的即可`
          : `拉取成功：接口下共 ${list.length} 个模型，勾选需要的即可`;
        fetchState.dataset.tone = 'success';
        toast(`拉取成功：${list.length} 个模型`, { tone: 'success', duration: 2500 });
      } catch (err) {
        fetchState.textContent = '拉取失败：请检查地址与密钥，或在下方手动输入模型名';
        fetchState.dataset.tone = 'danger';
        toast(errorText(err, '拉取失败'), { tone: 'danger', duration: 5000 });
      } finally {
        fetching = false;
        fetchBtn.disabled = false; fetchBtn.textContent = '⟳ 拉取模型列表';
      }
    }
    fetchBtn.addEventListener('click', doFetch);

    function addManual() {
      const id = manualIn.value.trim();
      if (!id) { toast('请输入模型名', { tone: 'danger' }); return; }
      if (!fetched.includes(id)) fetched.push(id);   // 手填的也进候选列表，便于统一勾选管理
      pickerEl.style.display = '';
      searchIn.value = '';
      if (!selected.includes(id)) selected.push(id);
      manualIn.value = '';
      paintAll();
      toast(`已添加模型 ${id}`, { tone: 'success', duration: 1600 });
    }
    manualIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addManual(); } });
    searchIn.addEventListener('input', paintList);

    const keyHint = existing && existing.has_key
      ? `当前已保存的密钥：${existing.key_display || '已设置'}（出于安全不回显明文）。留空表示不修改；拉取模型与实际调用都会继续使用它。`
      : '';
    const body = h(
      'div',
      { class: 'prov-editor' },
      h('div', { class: 'pe-section' },
        h('div', { class: 'pe-sec-title', text: '基础信息' }),
        h('div', { class: 'wiz-grid' },
          fieldRow('名称', name),
          fieldRow('接口地址（OpenAI 兼容）', baseUrl, 'span-2'),
          fieldRow('API 密钥', apiKey, 'span-2', keyHint)
        )
      ),
      h('div', { class: 'pe-section' },
        h('div', { class: 'pe-sec-title', text: '模型选择' }),
        h('div', { class: 'pe-sec-hint', text: '第 ① 步填好地址与密钥 → 第 ② 步一键拉取 → 第 ③ 步勾选需要的模型。勾选顺序即优先级，第一个为默认模型。' }),
        h('div', { class: 'pe-fetch-row' }, fetchBtn, fetchState),
        h('div', { class: 'mp-picked' },
          h('div', { class: 'mp-picked-head' }, h('span', { class: 'mp-picked-title', text: '已选模型' }), pickedCount),
          chipsEl
        ),
        pickerEl,
        h('div', { class: 'pe-manual-row' }, manualIn,
          h('button', { class: 'btn btn-sm', type: 'button', text: '添加', on: { click: addManual } }))
      )
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
              models: selected.slice()
            };
            if (!payload.name) {
              toast('请填写供应商名称', { tone: 'danger' });
              return;
            }
            if (!payload.models.length) {
              toast('请至少选择 1 个模型（拉取或手动添加）', { tone: 'danger' });
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
    const modal = openModal({ title: existing ? `编辑 ${existing.name}` : '添加模型供应商', body, footer, size: 'lg' });
    /* v4.22.2：编辑已有供应商时自动拉取模型列表（用已存密钥）——
       已选模型立即勾选呈现，不再需要手动点「拉取」才能看到清单。 */
    if (existing) {
      setTimeout(() => { try { doFetch(); } catch { /* ignore */ } }, 150);
    }
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

  /* v4.20：岗位启停/删除。停用 = 流水线跳过该岗位环节；作家是创作引擎不可停用。 */
  async function toggleAgent(a, enabled) {
    try {
      await collabCall('agentsToggle', { agent: a.agent, enabled: !!enabled });
      toast(`${a.label || a.agent} 已${enabled ? '启用' : '停用'}`, { tone: 'success', duration: 1800 });
      await loadAgents();
    } catch (err) {
      toast(errorText(err, '操作失败'), { tone: 'danger' });
    }
  }

  async function removeAgent(a) {
    const ok = await confirmDialog({
      title: `删除岗位「${a.label || a.agent}」？`,
      message: '只删除本机自定义岗位，其模型/技能绑定一并清除。内置岗位只能停用、不能删除。',
      confirmText: '删除',
      danger: true
    });
    if (!ok) return;
    try {
      await collabCall('agentsRemove', a.agent);
      toast(`已删除岗位「${a.label || a.agent}」`, { tone: 'success', duration: 1800 });
      await loadAgents();
    } catch (err) {
      toast(errorText(err, '删除失败'), { tone: 'danger' });
    }
  }

  function agentMatrixRow(a) {
    const modelText = a.model || (a.provider_id ? '供应商默认模型' : a.effective) || '未绑定';
    /* v4.20：状态（后端没返回 enabled 时按启用处理，兼容旧 /api/agents）；custom 标记自定义岗位。 */
    const enabled = a.enabled !== false;
    const isCustom = a.custom === true;
    const isWriter = a.agent === 'writer';
    const copy = h(
      'div',
      { class: 'set-row-copy' },
      h('div', { class: 'set-row-title' },
        h('span', { text: a.label || a.agent }), ' ',
        h('span', { class: enabled ? 'badge badge-accent' : 'badge', dataset: { tone: enabled ? '' : 'muted' }, text: enabled ? '启用中' : '已停用' }),
        ' ',
        collabSourceBadge(a), a.stage ? h('span', { class: 'chip chip-muted', text: a.stage }) : null),
      h('div', { class: 'set-row-desc', text: `${modelText}${a.desc ? ` · ${a.desc}` : ''}` })
    );
    /* skills 是并行同事新增字段：缺失就整块不渲染技能区，不能报错。 */
    if (Array.isArray(a.skills) && a.skills.length) {
      const chips = h('div', { class: 'set-skill-chips' });
      for (const sk of a.skills) chips.append(h('span', { class: 'chip', text: SKILL_TITLES[String(sk)] || String(sk) }));
      copy.append(chips);
    }
    const row = h(
      'div',
      { class: 'set-row', dataset: { agent: a.agent } },
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
        }),
        isWriter
          ? h('button', {
              class: 'btn btn-sm',
              type: 'button',
              text: '停用',
              disabled: true,
              title: '作家岗位是创作引擎，不能停用'
            })
          : h('button', {
              class: 'btn btn-sm',
              type: 'button',
              text: enabled ? '停用' : '启用',
              on: { click: () => toggleAgent(a, !enabled) }
            }),
        isCustom
          ? h('button', {
              class: 'btn btn-sm',
              type: 'button',
              text: '删除',
              on: { click: () => removeAgent(a) }
            })
          : null
      )
    );
    if (!enabled) row.style.opacity = '0.55';   /* 已停用岗位整行降透明度 */
    return row;
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
      h('div', { class: 'set-subhead' }, h('h3', { text: '创作岗位' }), h('span', { class: 'hint-text', text: '核心岗位（规划师/作家/审稿人/去味师）默认自动启用、可单独绑模型与技能；不需要的岗位可停用（作家除外）。' }))
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

  /* ------------------------------------------- 系统日志 / 作品诊断（v4.20.0 重构） */
  /* 「系统日志」：版本信息置顶 → 系统操作日志 → 服务端日志 → 模型调用与错误，全部整合在一页。
     「作品诊断」：各作品创作状态/卡顿/会话级运行日志，独立分组（排在「作者笔名」后，与系统日志同套样式）。
     导出诊断包支持 全部 / 单选 / 多选（复选框），可选附日志全文（可读）。 */

  const OPS_KINDS = [
    { key: '', label: '全部' },
    { key: 'system', label: '系统' },
    { key: 'settings', label: '设置' },
    { key: 'provider', label: '供应商' },
    { key: 'skill', label: '技能' },
    { key: 'agent', label: '岗位' },
    { key: 'penname', label: '笔名' }
  ];
  const OPS_KIND_LABEL = Object.fromEntries(OPS_KINDS.map((k) => [k.key, k.label]));

  /* ---------- 诊断导出弹窗：全部 / 单选 / 多选 + 日志全文 ---------- */

  const EXPORT_SECTIONS = [
    { key: 'works', label: '作品状态诊断', desc: '各书进度、卡顿判断与运行日志尾 40 行' },
    { key: 'sysops', label: '系统操作日志', desc: '设置/供应商/技能/岗位等系统侧动作流水' },
    { key: 'server', label: '服务端日志（尾 60 行）', desc: '' },
    { key: 'llm', label: '模型调用与错误', desc: '最近调用状态 + 模型错误日志尾 40 行' },
    { key: 'providers', label: '供应商配置摘要（脱敏）', desc: '不含密钥明文' }
  ];

  function downloadText(text, filename) {
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const a = h('a', { href: URL.createObjectURL(blob), download: filename });
    document.body.append(a); a.click(); a.remove();
  }

  function openExportDialog() {
    const picked = new Set(EXPORT_SECTIONS.map((s) => s.key));   // 默认=全部诊断
    const logsCb = h('input', { type: 'checkbox', 'aria-label': '附日志全文（可读）' });
    const listEl = h('div', { class: 'export-list' });

    function paintRows() {
      listEl.textContent = '';
      for (const s of EXPORT_SECTIONS) {
        const cb = h('input', { type: 'checkbox', checked: picked.has(s.key), 'aria-label': `选择${s.label}` });
        cb.addEventListener('change', () => {
          if (cb.checked) picked.add(s.key); else picked.delete(s.key);
        });
        listEl.append(h('label', { class: 'export-item' }, cb,
          h('span', { class: 'export-copy' },
            h('span', { class: 'export-label', text: s.label }),
            s.desc ? h('span', { class: 'export-desc', text: s.desc }) : null)));
      }
    }
    paintRows();

    const body = h('div', { class: 'prov-editor' },
      h('div', { class: 'pe-section' },
        h('div', { class: 'pe-sec-title', text: '诊断范围' }),
        h('div', { class: 'pe-sec-hint', text: '默认全选 = 导出全部诊断；只勾需要的项即单项导出，勾多项即打包多选导出。' }),
        h('div', { class: 'ops-chips' },
          h('button', { class: 'ops-chip', type: 'button', text: '全选', on: {
            click: () => { EXPORT_SECTIONS.forEach((s) => picked.add(s.key)); paintRows(); } } }),
          h('button', { class: 'ops-chip', type: 'button', text: '清空', on: {
            click: () => { picked.clear(); paintRows(); } } })
        ),
        listEl,
        h('div', { class: 'pe-sec-title', text: '日志全文（可读）', style: 'margin-top:16px' }),
        h('label', { class: 'export-item', style: 'border:0.5px solid var(--line-2);border-radius:var(--radius-md)' }, logsCb,
          h('span', { class: 'export-copy' },
            h('span', { class: 'export-label', text: '附全部运行日志全文' }),
            h('span', { class: 'export-desc', text: '服务端日志（尾 2000 行）+ 各作品运行日志（每本尾 800 行）+ 系统操作日志全文，全部转成可读纯文本，便于排查；文件较大。' })))
      ));

    const footer = h('div', { class: 'set-actions' },
      h('button', { class: 'btn', type: 'button', text: '取消', on: { click: () => modal.close() } }),
      h('button', {
        class: 'btn btn-primary', type: 'button', text: '导出',
        on: {
          click: async (event) => {
            const btn = event.currentTarget;
            if (!picked.size && !logsCb.checked) { toast('请至少勾选一项', { tone: 'danger' }); return; }
            btn.disabled = true; btn.textContent = '导出中…';
            try {
              const res = await fetch('/api/diagnostics/export', {
                method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sections: Array.from(picked), logs: logsCb.checked })
              });
              if (!res.ok) throw new Error(`HTTP ${res.status}`);
              const text = await res.text();
              const scope = picked.size === EXPORT_SECTIONS.length ? '全部' : `${picked.size}项`;
              downloadText(text, `yimu-lit-诊断包-${scope}${logsCb.checked ? '-含日志全文' : ''}.txt`);
              toast('诊断包已导出', { tone: 'success' });
              modal.close();
            } catch (err) {
              toast(errorText(err, '导出失败'), { tone: 'danger' });
              btn.disabled = false; btn.textContent = '导出';
            }
          }
        }
      })
    );
    const modal = openModal({ title: '导出诊断包', body, footer, size: 'md' });
  }

  /* ---------- 系统操作日志（原 v4.19.0 逻辑保留） ---------- */

  const opsBody = h('div', {});

  let opsKind = '';
  let opsQ = '';
  let opsData = null;   // {items, total}

  function opsRow(e) {
    const wrap = h('div', { class: 'ops-entry' });
    const row = h('div', { class: 'ops-row' },
      h('span', { class: 'ops-time mono', text: String(e.time || '').slice(5, 19) || '—' }),
      h('span', { class: 'ops-badge', dataset: { kind: e.kind || 'system' }, text: OPS_KIND_LABEL[e.kind] || e.kind || '系统' }),
      h('span', { class: 'ops-summary', text: e.summary || e.action || '' })
    );
    let detail = null;
    const hasDetail = e.detail != null && (typeof e.detail !== 'object' || Object.keys(e.detail).length > 0);
    if (hasDetail) {
      detail = h('pre', { class: 'ops-detail', style: 'display:none', text: JSON.stringify(e.detail, null, 2) });
      row.classList.add('ops-expandable');
      row.title = '点击展开/收起详情';
      row.addEventListener('click', () => {
        const show = detail.style.display === 'none';
        detail.style.display = show ? '' : 'none';
      });
    }
    wrap.append(row);
    if (detail) wrap.append(detail);
    return wrap;
  }

  function paintOps() {
    opsBody.textContent = '';
    opsBody.append(
      h('div', { class: 'set-row' },
        h('div', { class: 'set-row-copy' },
          h('div', { class: 'set-row-title', text: '系统操作日志' }),
          h('div', { class: 'set-row-desc', text: '系统侧全部动作的流水：服务启停、设置变更、供应商增删改、技能添加/删除、岗位调整、笔名管理。点击条目可展开详情。' })
        ),
        h('div', { class: 'set-control' },
          h('button', { class: 'btn btn-sm', type: 'button', text: '清空', on: { click: clearOps } }),
          h('button', { class: 'btn btn-sm', type: 'button', text: '刷新', on: { click: () => loadOps() } })
        )
      ),
      h('div', { class: 'ops-chips' },
        OPS_KINDS.map((k) => h('button', {
          class: 'ops-chip', type: 'button',
          'aria-pressed': opsKind === k.key ? 'true' : 'false',
          text: k.label,
          on: { click: () => { opsKind = k.key; loadOps(); } }
        }))
      )
    );
    const search = h('input', {
      class: 'input ops-search', type: 'search',
      placeholder: '搜索日志内容，例如：供应商、质量门禁、技能…',
      value: opsQ, 'aria-label': '搜索系统日志'
    });
    let deb;
    search.addEventListener('input', () => {
      clearTimeout(deb);
      deb = setTimeout(() => { opsQ = search.value.trim(); loadOps(); }, 350);
    });
    opsBody.append(search);

    const list = h('div', { class: 'set-list ops-list' });
    if (!opsData) {
      list.append(h('div', { class: 'set-loading' }, h('span', { class: 'skeleton skeleton-line w-60' })));
    } else if (!opsData.items.length) {
      list.append(h('div', { class: 'hint-text ops-empty', text: '暂无记录。系统侧的动作（改设置、配模型、加技能…）都会自动记录在这里。' }));
    } else {
      for (const e of opsData.items) list.append(opsRow(e));
      if (opsData.total > opsData.items.length) {
        list.append(h('div', { class: 'hint-text ops-more', text: `仅显示最近 ${opsData.items.length} 条（共 ${opsData.total} 条）` }));
      }
    }
    opsBody.append(list);
  }

  async function loadOps() {
    try {
      opsData = await api.syslog({ kind: opsKind, q: opsQ, limit: 200 });
    } catch {
      opsData = { items: [], total: 0 };
    }
    if (view.isConnected) paintOps();
  }

  async function clearOps() {
    const ok = await confirmDialog({
      title: '清空系统操作日志？',
      message: '将删除全部系统级操作记录（会话级创作日志不受影响），此操作不可恢复。',
      confirmText: '清空',
      danger: true
    });
    if (!ok) return;
    try {
      await api.syslogClear();
      opsQ = ''; opsKind = '';
      await loadOps();
      toast('已清空', { duration: 1600 });
    } catch (err) {
      toast(errorText(err, '清空失败'), { tone: 'danger' });
    }
  }

  /* ---------- 系统日志 pane：版本信息置顶 + 操作日志 + 服务端日志 + 模型调用与错误 ---------- */

  const verBody = h('div', {});
  const sysTailBody = h('div', {});
  panes.syslog.append(verBody, opsBody, sysTailBody);

  function paintSysChrome(data) {
    verBody.textContent = '';
    sysTailBody.textContent = '';
    if (!data) {
      verBody.append(errorRow('诊断数据加载失败', loadDiagnostics));
      return;
    }
    const ago = data.llm_state && data.llm_state.ago_seconds != null
      ? `模型最近调用：${Math.round(data.llm_state.ago_seconds / 60)} 分钟前 · ` : '';
    verBody.append(
      h('div', { class: 'set-row' },
        h('div', { class: 'set-row-copy' },
          h('div', { class: 'set-row-title', text: `版本 v${data.version} · ${data.platform}` }),
          h('div', { class: 'set-row-desc', text: `${ago}版本信息、系统操作流水、服务端日志与模型调用状态全部整合在本页。` })
        ),
        h('div', { class: 'set-control' },
          h('button', { class: 'btn btn-sm', type: 'button', text: '导出诊断', on: { click: openExportDialog } }),
          h('button', { class: 'btn btn-sm', type: 'button', text: '刷新', on: { click: () => loadDiagnostics() } })
        )
      )
    );
    if ((data.server_tail || []).length) {
      sysTailBody.append(h('div', { class: 'set-group-subhead', text: '服务端日志（尾 60 行）' }));
      sysTailBody.append(h('pre', { class: 'diag-log', text: data.server_tail.join('\n') }));
    }
    if ((data.llm_errors || []).length) {
      sysTailBody.append(h('div', { class: 'set-group-subhead', text: '模型调用错误（尾 40 行）' }));
      sysTailBody.append(h('pre', { class: 'diag-log', text: data.llm_errors.join('\n') }));
    }
  }

  /* ---------- 作品诊断 pane（独立分组，与系统日志同套样式） ---------- */

  const diagBody = h('div', {});
  panes.workdiag.append(diagBody);

  function paintWorkDiag(data) {
    diagBody.textContent = '';
    if (!data) {
      diagBody.append(errorRow('诊断数据加载失败', loadDiagnostics));
      return;
    }
    diagBody.append(
      h('div', { class: 'set-row' },
        h('div', { class: 'set-row-copy' },
          h('div', { class: 'set-row-title', text: '作品创作状态（卡住的排最前）' }),
          h('div', { class: 'set-row-desc', text: '每本书的会话级创作进度、最后日志与卡顿判断；点击行展开该作品的运行日志。' })
        ),
        h('div', { class: 'set-control' },
          h('button', { class: 'btn btn-sm', type: 'button', text: '导出诊断', on: { click: openExportDialog } }),
          h('button', { class: 'btn btn-sm', type: 'button', text: '刷新', on: { click: () => loadDiagnostics() } })
        )
      )
    );
    const books = [...(data.books || [])].sort((a, b) => (b.stalled_seconds || 0) - (a.stalled_seconds || 0));
    const wrap = h('div', { class: 'set-list' });
    for (const b of books) {
      const stalled = b.stalled_seconds ? `  ⚠️ 疑似卡住 ${Math.round(b.stalled_seconds / 60)} 分钟` : '';
      const detail = h('pre', { class: 'diag-log', style: 'display:none;margin:8px 0 0', text: (b.tail || []).join('\n') || '（无日志）' });
      wrap.append(h('div', { class: 'set-row', style: 'display:block' },
        h('div', { class: 'set-row-copy', style: 'cursor:pointer', on: { click: () => { detail.style.display = detail.style.display === 'none' ? '' : 'none'; } } },
          h('div', { class: 'set-row-title', text: `${b.name}（${b.medium}）${stalled}` }),
          h('div', { class: 'set-row-desc', text: `${b.state} ${b.done}/${b.total} · 最后: ${b.last_line || '—'}` })
        ),
        detail
      ));
    }
    if (!books.length) wrap.append(h('div', { class: 'hint-text', text: '还没有作品。' }));
    diagBody.append(wrap);
  }

  /* ---------- 统一加载：一次诊断接口喂两个分组 ---------- */

  async function loadDiagnostics() {
    loadOps();
    diagBody.textContent = '';
    diagBody.append(h('div', { class: 'set-loading' }, h('span', { class: 'skeleton skeleton-line w-40' })));
    verBody.textContent = '';
    try {
      const data = await api.diagnostics();
      paintWorkDiag(data);
      paintSysChrome(data);
    } catch (err) {
      paintWorkDiag(null);
      paintSysChrome(null);
    }
  }

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

    /* v4.20.0：与其他分组同构——控件统一包 .set-control、用有样式的 .input，
       默认标记用「默认」徽标（与供应商行同款），删除走确认弹窗。 */
    penBody.append(
      h('div', { class: 'set-row' },
        h('div', { class: 'set-row-copy' },
          h('div', { class: 'set-row-title', text: '全局默认笔名' }),
          h('div', { class: 'set-row-desc', text: '新建作品时默认使用的作者名；可在每个向导里单独选择其他笔名。' })
        ),
        h('div', { class: 'set-control' },
          h('select', { class: 'input', 'aria-label': '全局默认笔名', on: {
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
        )
      )
    );

    /* 笔名列表 */
    penBody.append(h('div', { class: 'set-group-subhead', text: '笔名列表' }));
    const listWrap = h('div', { class: 'set-list' });
    if (!list.length) {
      listWrap.append(h('div', { class: 'hint-text', style: 'padding:10px 4px', text: '还没有笔名。点下方「添加笔名」创建第一个。' }));
    }
    for (const c of list) {
      listWrap.append(h('div', { class: 'set-row' },
        h('div', { class: 'set-row-copy' },
          h('div', { class: 'set-row-title' },
            h('span', { text: c.name }),
            c.name === defaultName ? h('span', { class: 'badge badge-accent', text: '默认' }) : null
          ),
          h('div', { class: 'set-row-desc', text: c.desc || '（无描述）' })
        ),
        h('div', { class: 'set-control' },
          h('button', { class: 'btn btn-sm', type: 'button', text: '删除', on: {
            click: async () => {
              const ok = await confirmDialog({
                title: `删除笔名「${c.name}」？`,
                message: '只影响署名选择列表，已创作的作品不受影响。',
                confirmText: '删除',
                danger: true
              });
              if (!ok) return;
              try {
                await api.pennamesDelete({ name: c.name });
                await loadPennames();
                toast('已删除', { duration: 1600 });
              } catch (err) { toast(errorText(err, '删除失败'), { tone: 'danger' }); }
            }
          } })
        )
      ));
    }
    penBody.append(listWrap);

    /* v4.15.6：按文体默认笔名（每个文体一个下拉；未设置=用全局默认） */
    penBody.append(h('div', { class: 'set-group-subhead', text: '按文体默认笔名' }));
    const MEDIUM_LABELS = { novel: '小说', script: '剧本', poetry: '诗词', prose: '散文', thesis: '论文',
                            children: '儿童文学', news: '新闻稿', copy: '文案', other: '其他' };
    const byMed = pn.by_medium || {};
    const medWrap = h('div', { class: 'set-list' });
    for (const [med, label] of Object.entries(MEDIUM_LABELS)) {
      medWrap.append(h('div', { class: 'set-row' },
        h('div', { class: 'set-row-copy' },
          h('div', { class: 'set-row-title', text: label })
        ),
        h('div', { class: 'set-control' },
          h('select', { class: 'input', 'aria-label': `${label}默认笔名`, on: {
            change: async (e) => {
              try {
                await api.pennamesSave({ list, default: defaultName, by_medium: { ...byMed, [med]: e.target.value } });
                await loadPennames();
              } catch (err) { toast(errorText(err, '保存失败'), { tone: 'danger' }); }
            }
          } },
            h('option', { value: '', text: '（用全局默认）' }),
            ...list.map(c => h('option', { value: c.name, selected: byMed[med] === c.name, text: c.name }))
          )
        )
      ));
    }
    penBody.append(medWrap);

    /* 添加笔名（动作行；btn-accent 是无样式的死类，统一用 btn-primary） */
    penBody.append(h('div', { class: 'set-actions' },
      h('button', { class: 'btn btn-sm btn-primary', type: 'button', text: '添加笔名', on: {
        click: () => openAddPennameDialog()
      } })
    ));
  }

  function openAddPennameDialog() {
    /* v4.20.0：改用本页统一的 fieldRow（wiz-label + .input），与供应商/岗位弹窗同款 */
    const nameInput = h('input', { class: 'input', placeholder: '笔名（必填）', 'aria-label': '笔名' });
    const descInput = h('input', { class: 'input', placeholder: '描述（选填），例如：古风短篇', 'aria-label': '描述' });
    const body = h('div', { class: 'wiz-grid' },
      fieldRow('笔名', nameInput),
      fieldRow('描述', descInput, '', '可留空；描述会显示在选择列表里帮助区分。')
    );
    const footer = h('div', { class: 'set-actions' },
      h('button', { class: 'btn', type: 'button', text: '取消', on: { click: () => modal.close() } }),
      h('button', { class: 'btn btn-primary', type: 'button', text: '添加', on: {
        click: async () => {
          const name = nameInput.value.trim();
          if (!name) { toast('笔名不能为空', { tone: 'danger' }); return; }
          if ((model.pennames.list || []).some((c) => c.name === name)) {
            toast('已存在同名笔名', { tone: 'danger' });
            return;
          }
          const newList = [...(model.pennames.list || []), { name, desc: descInput.value.trim() }];
          try {
            await api.pennamesSave({ list: newList, default: model.pennames.default || '' });
            await loadPennames();
            modal.close();
            toast('笔名已添加', { tone: 'success', duration: 1600 });
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
  loadPennames();   /* v4.15.5 修复：笔名面板此前从未在初始化时加载，分组面板永远空白 */
  loadDiagnostics();   /* v4.20.0：系统日志 + 作品诊断两分组共用一次诊断加载 */

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
