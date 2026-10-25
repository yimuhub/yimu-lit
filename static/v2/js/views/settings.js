/* 设置：外观、生成质量、模型与密钥。只发送后端支持的字段，其余只读展示。 */

import { h, icon } from '../core/dom.js';
import { store } from '../core/state.js';
import { api, errorText } from '../core/api.js';
import { toast, confirmDialog, openModal } from '../core/ui.js';
import { setTheme, themeLabel } from '../core/theme.js';

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

export function renderSettings(container, ctx) {
  const view = h('div', { class: 'view-scroll' });
  const pad = h('div', { class: 'view-pad', style: { maxWidth: '760px' } });
  view.append(pad);
  container.append(view);

  const model = {
    settings: null,
    settingsError: '',
    providers: null,
    providerError: '',
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
    h('div', { class: 'set-section-head' }, h('h2', { text: '外观' })),
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
  qualitySec.append(h('div', { class: 'set-section-head' }, h('h2', { text: '生成质量' })), qualityBody);
  pad.append(qualitySec);

  function paintQuality() {
    qualityBody.textContent = '';
    if (model.settingsError) {
      qualityBody.append(
        h(
          'div',
          { class: 'msg-error', role: 'alert' },
          h('span', { html: icon('alert', 14) }),
          h('span', { text: model.settingsError }),
          h('button', { class: 'err-btn', type: 'button', text: '重试', on: { click: loadSettings } })
        )
      );
      return;
    }
    if (!model.settings) {
      qualityBody.append(
        h('div', { class: 'session-loading' }, h('span', { class: 'skeleton skeleton-line', style: { width: '40%' } }))
      );
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
          text: `调用间隔 ${s.call_gap ?? '—'} 秒 · 全局并发上限 ${s.max_global_agents ?? '—'}（由 config.json 控制）`
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
    qualityBody.append(h('div', { class: 'settings-note', style: { display: 'flex', justifyContent: 'flex-end' } }, saveBtn));
  }

  function switchRow(title, desc, value, onChange) {
    const box = h('input', { type: 'checkbox', checked: Boolean(value) });
    box.addEventListener('change', () => onChange(box.checked));
    return h(
      'div',
      { class: 'set-row' },
      h(
        'div',
        { class: 'set-row-copy' },
        h('div', { class: 'set-row-title', text: title }),
        h('div', { class: 'set-row-desc', text: desc })
      ),
      h('div', { class: 'set-control' }, box)
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
    h('h2', { text: '模型与密钥' }),
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
      provBody.append(
        h(
          'div',
          { class: 'msg-error', role: 'alert' },
          h('span', { html: icon('alert', 14) }),
          h('span', { text: model.providerError }),
          h('button', { class: 'err-btn', type: 'button', text: '重试', on: { click: loadProviders } })
        )
      );
      return;
    }
    if (!model.providers) {
      provBody.append(h('div', { class: 'session-loading' }, h('span', { class: 'skeleton skeleton-line', style: { width: '50%' } })));
      return;
    }
    const providers = model.providers.providers || [];
    if (!providers.length) {
      provBody.append(
        h(
          'div',
          { class: 'empty-state', style: { padding: '24px 8px' } },
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
      { style: { display: 'flex', gap: '8px', justifyContent: 'flex-end', width: '100%' } },
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

  /* --------------------------------------------------------- 关于 */

  const aboutSec = h(
    'div',
    { class: 'set-section' },
    h('div', { class: 'set-section-head' }, h('h2', { text: '技能与数据' })),
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

  /* ----------------------------------------------------------- start */

  paintTheme();
  loadSettings();
  loadProviders();

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
    },
    destroy() {
      unsubTheme();
      view.remove();
    }
  };
}
