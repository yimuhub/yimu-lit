/* v5.8.0 人机共同创作模式（逐章确认）：node 直跑回归（jsdom 仿真，不开浏览器）。
   覆盖面：
     1) message.js 协作条纯判定 collabStripState（pausedReason=await_collab:<n> 口径）；
     2) 章节卡真渲染：停在第 7 章待确认时，第 7 章的卡出现协作条（三键），
        其它章的卡 / 未暂停 / 非协作原因 不出现；
     3) 「继续写下一章」点击真的打 /api/resume（复用创作按钮既有机制）+ chatNote 入库；
     4) 契约钉死：settings.js 尾部「创作模式 · 逐章确认」小节走 /api/collab-review、
        无当前书给选书提示；bot/writer.py 的 await_collab 机制、app.py 的
        collab_next 分发与 /api/collab-review 端点、bot/chief.py 的 collab_mode 动作。
   mock 分相不需要——本测试不依赖后端，fetch 一律回空。 */
import fs from 'fs';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';
import { JSDOM } from 'jsdom';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const failed = [];
function check(name, cond, extra = '') {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed.push(name);
}
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

/* ---------------------------------------------------------------- jsdom 环境 */
const dom = new JSDOM(
  '<!doctype html><html><body><div id="root"></div><div id="overlays"></div><div id="toasts"></div></body></html>',
  { url: 'http://127.0.0.1:8848/', pretendToBeVisual: true }
);
global.window = dom.window;
global.document = dom.window.document;
global.Node = dom.window.Node;
global.Element = dom.window.Element;
global.HTMLElement = dom.window.HTMLElement;
global.localStorage = dom.window.localStorage;
global.sessionStorage = dom.window.sessionStorage;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true, writable: true });
Object.defineProperty(global, 'location', { value: dom.window.location, configurable: true, writable: true });
global.getComputedStyle = dom.window.getComputedStyle;
global.CustomEvent = dom.window.CustomEvent;
global.Event = dom.window.Event;
global.requestAnimationFrame = (cb) => setTimeout(cb, 0);
global.cancelAnimationFrame = (id) => clearTimeout(id);

/* fetch 桩：记录调用，一律回成功空体——只验证「打没打对接口」。 */
const calls = [];
global.fetch = async (url, init) => {
  calls.push({ url: String(url), method: (init && init.method) || 'GET', body: init && init.body });
  return {
    ok: true, status: 200,
    headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'application/json' : '') },
    json: async () => ({ ok: true, running: true, paused: false, state: 'in_progress' }),
    text: async () => '{}'
  };
};
dom.window.fetch = global.fetch;

const message = await import(pathToFileURL(path.join(ROOT, 'static/v2/js/components/message.js')).href);
const state = await import(pathToFileURL(path.join(ROOT, 'static/v2/js/core/state.js')).href);
const { collabStripState, renderAssistantMessage } = message;
const { store, patch } = state;

const doc = dom.window.document;
const workAwaiting7 = { key: 'bk-collab', paused: true, pausedReason: 'await_collab:7',
                        running: false, state: 'paused', collab_review: true };
const cardMeta7 = { no: 7, title: '雨夜来客', words: 3200, tokens: 8800, quality: 86, ts: '' };

/* ---------------------------------------------------------------- 1) 纯判定 */
{
  check('纯判定：await_collab:7 + 第7章卡 → 返回协作条描述',
        JSON.stringify(collabStripState(workAwaiting7, cardMeta7)) ===
        JSON.stringify({ no: 7, actions: ['next', 'rewrite', 'bench'] }),
        JSON.stringify(collabStripState(workAwaiting7, cardMeta7)));
  check('纯判定：不是当前待确认章（第6章）→ null',
        collabStripState(workAwaiting7, { ...cardMeta7, no: 6 }) === null);
  check('纯判定：没暂停 → null',
        collabStripState({ ...workAwaiting7, paused: false }, cardMeta7) === null);
  check('纯判定：人工暂停（无 await_collab 原因）→ null',
        collabStripState({ paused: true, pausedReason: '' }, cardMeta7) === null);
  check('纯判定：批量停（batch_stopped 不在 pausedReason）→ null',
        collabStripState({ paused: true, pausedReason: 'paused' }, cardMeta7) === null);
}

/* ---------------------------------------------------------------- 2) 章节卡真渲染 */
function renderCard(meta, work) {
  doc.body.innerHTML = '';
  patch('work', work || null);
  const host = doc.createElement('div');
  doc.body.append(host);
  const el = renderAssistantMessage({
    id: 'hist_test', role: 'assistant', content: '# 第7章 雨夜来客\n\n正文……',
    chapterMeta: meta, collapsed: false
  }, { workKey: 'bk-collab' });
  host.append(el);
  return el;
}

{
  const el = renderCard(cardMeta7, workAwaiting7);
  const strip = el.querySelector('.cc-collab');
  check('协作条：待确认章的卡出现协作条', !!strip);
  const btns = strip ? [...strip.querySelectorAll('button')] : [];
  check('协作条：三个按钮（继续写下一章/重写本章/去工作台）',
        btns.length === 3
        && btns[0].textContent.includes('继续写下一章')
        && btns[1].textContent.includes('重写本章')
        && btns[2].textContent.includes('去工作台'),
        btns.map((b) => b.textContent.trim()).join(' | '));
  check('协作条：标注待确认章号',
        !!strip && strip.getAttribute('aria-label') === '第7章逐章确认'
        && strip.textContent.includes('第7章已完成，等待你把关'));
}
{
  const el = renderCard({ ...cardMeta7, no: 6 }, workAwaiting7);
  check('协作条：其它章的卡不出现', !el.querySelector('.cc-collab'));
}
{
  const el = renderCard(cardMeta7, { ...workAwaiting7, paused: false });
  check('协作条：已采用（暂停位已清）不出现', !el.querySelector('.cc-collab'));
}
{
  const el = renderCard(cardMeta7, { paused: true, pausedReason: '' });
  check('协作条：人工暂停不出现', !el.querySelector('.cc-collab'));
}
{
  const el = renderCard(cardMeta7, null);
  check('协作条：work 未并库（刷新瞬间）不出现', !el.querySelector('.cc-collab'));
}

/* ---------------------------------------------------------------- 3) 点击行为 */
{
  const el = renderCard(cardMeta7, workAwaiting7);
  calls.length = 0;
  const btn = el.querySelector('.cc-collab button');
  btn.click();
  await new Promise((r) => setTimeout(r, 30));
  const urls = calls.map((c) => c.url);
  check('点「继续写下一章」真的打 /api/resume', urls.some((u) => u.startsWith('/api/resume')), urls.join(' , '));
  check('「继续」气泡入库（/api/chat/note，与创作按钮同款）',
        calls.some((c) => c.url.startsWith('/api/chat/note')
                   && String(c.body || '').includes('继续')));
  check('「重写本章」走 /api/rewrite',
        (() => {
          const el2 = renderCard(cardMeta7, workAwaiting7);
          calls.length = 0;
          const btns = [...el2.querySelectorAll('.cc-collab button')];
          btns[1].click();
          return new Promise((done) => setTimeout(() => {
            done(calls.some((c) => c.url.startsWith('/api/rewrite')));
          }, 30));
        })());
}

/* ---------------------------------------------------------------- 4) 契约钉死 */
{
  const settings = read('static/v2/js/views/settings.js');
  check('settings.js：创作模式小节走 /api/collab-review（读+写）',
        settings.includes('/api/collab-review?key=') && settings.includes("'/api/collab-review'"));
  check('settings.js：无当前书给「先选作品」提示',
        settings.includes('请先在左侧选择一部作品'));
  check('settings.js：小节名与三键语义在案',
        settings.includes('创作模式 · 逐章确认') && settings.includes('采用继续'));
  check('settings.js：切书跟随（currentKey 订阅）',
        settings.includes('lastCollabKey') && settings.includes('loadCollabMode'));
}
{
  const writerPy = read('bot/writer.py');
  check('writer.py：await_collab 暂停位格式 + 提示行常量',
        writerPy.includes('COLLAB_REASON_FMT = "await_collab:{n}"')
        && writerPy.includes('COLLAB_CHAT_HINT') && writerPy.includes('等待你把关'));
  check('writer.py：循环检查点在极速与一键完稿时不启用',
        writerPy.includes('not _live_express() and collab_review_on(proj)')
        && writerPy.includes('not in _ONESHOT_DRIVING'));
  check('writer.py：resume_book 采用语义 + 人工暂停/协作停分开记账',
        writerPy.includes('已按你确认采用，继续第')
        && writerPy.includes('stage="await_collab"'));
}
{
  const appPy = read('app.py');
  check('app.py：write mode=collab_next 分发 + collab_next 分支',
        appPy.includes('_mode == "collab_next"') && appPy.includes('intent == "collab_next"'));
  check('app.py：collab_mode 开关分支（关闭时停在待确认章视为放行续写）',
        appPy.includes('intent == "collab_mode"') && appPy.includes('视为已确认'));
  check('app.py：设置页数据源端点 GET/POST /api/collab-review',
        appPy.includes('def api_collab_review_get') && appPy.includes('def api_collab_review_set'));
  check('app.py：_start_rewrite 包裹协作恢复（重写完仍停在本章待确认）',
        appPy.includes('begin_collab_rewrite') && appPy.includes('end_collab_rewrite'));
}
{
  const chiefPy = read('bot/chief.py');
  check('chief.py：collab_mode 动作在 ACTIONS + 别名',
        chiefPy.includes('"collab_mode"') && chiefPy.includes('"逐章确认": "collab_mode"'));
  check('chief.py：write 动作 mode 含 collab_next',
        chiefPy.includes('collab_next=采用当前待确认章并继续写下一章'));
  check('chief.py：系统提示规则 17（协作模式动作语义）',
        chiefPy.includes('人机共同创作（逐章确认）') && chiefPy.includes('write(mode=collab_next)'));
  check('chief.py：上下文带给总编辑（await_collab / collab_review）',
        chiefPy.includes('ctx["await_collab"]') && chiefPy.includes('ctx["collab_review"]'));
}
{
  const messageJs = read('static/v2/js/components/message.js');
  check('message.js：协作条纯判定导出（node 可直跑）',
        messageJs.includes('export function collabStripState'));
  check('message.js：协作条只在 live paused + 章号匹配时渲染',
        messageJs.includes("await_collab:(\\d+)") && messageJs.includes('cc-collab'));
}

console.log(failed.length ? `\n${failed.length} 项失败` : '\n全部通过');
process.exit(failed.length ? 1 : 0);
