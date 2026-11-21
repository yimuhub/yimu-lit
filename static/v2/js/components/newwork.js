/* v5.5.0 新建作品双入口弹窗：侧栏「新建作品」/ 首页工具卡与 hero / 快捷键 N
 * 统一先弹这个选择弹窗，再分岔到两条开书路径：
 *   1) 按开书流程走 → ctx.openWizard(preset)（完整向导，现行为不变）；
 *   2) 直接开聊     → 进入「无作品绑定会话」（#/welcome?chat=1 挂 session 视图，
 *      /api/chat/stream 不带 key 时后端按无绑定新会话处理，总编辑引导开书）。
 * 弹窗复用 core/ui.js 的 openModal（modalStack：Esc 关最顶层 / 点遮罩关闭）。 */

import { h, icon } from '../core/dom.js';
import { openModal, toast } from '../core/ui.js';
import { errorText } from '../core/api.js';
import { patchUi, resetChat } from '../core/state.js';
import { navigate } from '../core/router.js';
import { loadCss } from '../core/loadcss.js';

/* 改 static/v2/css/newwork.css 后把这里 +1（loadCss 幂等按 href 去重） */
const NW_CSS_V = '1';

/* 直接开聊：无绑定会话路由的 query 开关（#/welcome?chat=1） */
export const DIRECT_CHAT_QUERY = 'chat';

/* 直接开聊：composer 预填引导语（只预填不自动发送，用户改完自己发） */
export const DIRECT_CHAT_PREFILL = '我想写一本……（例：一本关于实习生隐藏身份的都市爽文）';

/* 会话区顶部提示卡文案（welcome.js 挂 session 视图时由 session.js 渲染） */
export const DIRECT_CHAT_NOTICE = '直接开聊模式：说想写什么，总编辑会帮你定体裁/书名并开写；书名确认后会自动切到新书任务。';

const ENTRIES = [
  {
    entry: 'wizard',
    icon: 'quill',
    title: '按开书流程走',
    sub: '完整向导：体裁 → 设定 → 大纲 → 规划，适合认真开新书'
  },
  {
    entry: 'chat',
    icon: 'message',
    title: '直接开聊',
    sub: '不建书、不定设定，直接跟总编辑说想写什么，边聊边定'
  }
];

let activeDialog = null;

/** 弹窗是否开着（sidebar 快捷键 N 守卫用） */
export function newworkDialogOpen() {
  return Boolean(activeDialog);
}

/** 路由 query 是否为「直接开聊」无绑定会话（welcome.js 挂载分岔用） */
export function isDirectChatQuery(query) {
  return Boolean(query && query[DIRECT_CHAT_QUERY]);
}

/* 进入无作品绑定的新会话：清空残留对话 → 预填引导语 → 跳无 key 会话路由。
 * nonce 让「已经在直接开聊页再点一次直接开聊」也能开出全新会话
 * （hash 变化 → hashchange → renderView 重挂 session 视图）。 */
export function enterDirectChat(ctx, preset = null) {
  const typed = String((preset && (preset.text || preset.idea)) || '').trim();
  patchUi({ directChatPrefill: typed || DIRECT_CHAT_PREFILL });
  resetChat();
  const nonce = Date.now().toString(36);
  navigate(`/welcome?${DIRECT_CHAT_QUERY}=1&t=${nonce}`);
}

/**
 * 打开「新建作品」双入口弹窗。
 * @param ctx 应用装配上下文（app.js）：需要 openWizard；直接开聊不依赖 ctx。
 * @param opts.preset 透传给向导的预填（hero 输入器 payload / 体裁快捷入口），
 *                    直接开聊时 preset.text/idea 会成为 composer 预填语。
 * @param opts.source 入口来源标记（sidebar / hero / hotkey / welcome-tools / welcome-empty），仅用于排查。
 */
export function openNewWorkDialog(ctx, opts = {}) {
  const { preset = null, source = '' } = opts || {};
  if (activeDialog) return activeDialog;
  loadCss(`/v2/css/newwork.css?v=${NW_CSS_V}`);

  const body = h('div', { class: 'nw-entries' });
  for (const meta of ENTRIES) {
    body.append(
      h('button', {
        class: 'nw-card',
        type: 'button',
        dataset: { entry: meta.entry, source: source || '' },
        'aria-label': `${meta.title}：${meta.sub}`,
        on: {
          click: () => {
            modal.close();
            if (meta.entry === 'wizard') {
              /* 现行为不变：完整开书向导（preset 原样透传） */
              Promise.resolve(ctx.openWizard(preset || {}))
                .catch((err) => toast(errorText(err, '向导打开失败，请稍后重试'), { tone: 'danger' }));
            } else {
              enterDirectChat(ctx, preset);
            }
          }
        }
      },
      h('span', { class: 'nw-card-icon', html: icon(meta.icon, 17) }),
      h('span', { class: 'nw-card-main' },
        h('span', { class: 'nw-card-title', text: meta.title }),
        h('span', { class: 'nw-card-sub', text: meta.sub }))
      )
    );
  }
  body.append(h('div', { class: 'nw-foot', text: '两条路随时能汇合：先聊后建、先建后聊都可以。' }));

  const modal = openModal({
    title: '新建作品',
    subtitle: '选一种开始方式',
    body,
    size: 'sm',
    onClose: () => { activeDialog = null; }
  });
  activeDialog = modal;
  return modal;
}
