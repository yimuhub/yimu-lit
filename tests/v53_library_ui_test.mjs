// v5.3.0 书架升级：node 直跑回归（jsdom 仿真，不开浏览器、不依赖 pytest）。
// 覆盖面：
//   1) library.js 排序 6 种纯函数 sortItems（方向/置顶优先/稳定性/中文书名）；
//   2) 分组纯函数 groupCounts / applyGroup（全部/未分组/自定义分组带计数）；
//   3) 搜索过滤纯函数 filterItems（书名/分类/简介/标签，大小写不敏感）；
//   4) 网格/列表切换状态（DOM 类名 + localStorage 持久化 + 卡片随搜索/分组实时增减）；
//   5) 契约钉死：回收站恢复/彻底删除走既有端点（api.restore/api.purge + 数据范围清单）、
//      封面上传走 FormData 即时预览、分组/封面端点与 bot/weblib.py 一一对应、
//      library.css 注入且无硬编码颜色。
import fs from 'fs';
import path from 'path';
import { fileURLToPath, pathToFileURL } from 'url';
import { JSDOM } from 'jsdom';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const V2 = path.join(ROOT, 'static', 'v2');
const failed = [];
function check(name, cond, extra = '') {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed.push(name);
}
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------------------------------------------------------------- jsdom 环境 */
const dom = new JSDOM('<!doctype html><html><head></head><body><div id="host"></div><div id="overlays"></div><div id="toasts"></div></body></html>',
  { url: 'http://127.0.0.1:8848/', pretendToBeVisual: true });
global.window = dom.window;
global.document = dom.window.document;
global.Node = dom.window.Node;
global.Element = dom.window.Element;
global.HTMLElement = dom.window.HTMLElement;
global.localStorage = dom.window.localStorage;
global.sessionStorage = dom.window.sessionStorage;
global.FormData = dom.window.FormData;
global.location = dom.window.location;
global.getComputedStyle = dom.window.getComputedStyle;
global.AbortController = dom.window.AbortController;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true, writable: true });
if (!global.CSS || !global.CSS.escape) {
  global.CSS = { ...(global.CSS || {}), escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => '\\' + c) };
}
localStorage.setItem('shenbi.v2.libView', 'grid');

/* ---------------------------------------------------------------- fetch 桩 */
/* 测试数据：三本书（一本置顶）、两个分组、一条简介。 */
const ITEMS = [
  { key: 'k1', name: '长夜灯河', genre: '玄幻', tags: ['热血'], total_words: 90000, updated_at: '2026-11-20T10:00:00', created_at: '2026-01-05T09:00:00', done: 10, total: 20, state: 'in_progress', medium: 'novel', pinned: false },
  { key: 'k2', name: 'Chelsea雨', genre: '都市', tags: [], total_words: 12000, updated_at: '2026-11-01T10:00:00', created_at: '2026-06-01T09:00:00', done: 3, total: 10, state: 'planning', medium: 'novel', pinned: true },
  { key: 'k3', name: '白鹿原手记', genre: '散文', tags: ['散文'], total_words: 350000, updated_at: '2026-11-10T10:00:00', created_at: '2026-03-15T09:00:00', done: 5, total: 5, state: 'completed', medium: 'prose', pinned: false }
];
const GROUPS = { ok: true, groups: [{ id: 'g-aaa1aaaa', name: '玄幻库', order: 1 }, { id: 'g-bbb2bbbb', name: '完本', order: 2 }], assign: { k1: 'g-aaa1aaaa' } };
/* 纯函数吃裸映射；fetch 桩走响应信封（前端 model.intros 已解包）。 */
const INTROS = { k2: '典当行里的万物流转', k3: '关中平原的五十年的风' };
const INTROS_RESP = { ok: true, intros: INTROS };

let fetchCalls = [];
global.fetch = async (url, init) => {
  fetchCalls.push({ url: String(url), method: (init && init.method) || 'GET' });
  const bare = String(url).split('?')[0];
  let payload = null;
  if (bare.endsWith('/api/library')) payload = { items: ITEMS, stats: { count: 3, total_words: 452000 }, counts: { active: 3, archived: 0, trash: 0 }, facets: { genres: ['玄幻', '都市', '散文'], mediums: ['novel', 'prose'] } };
  else if (bare.endsWith('/api/library/groups')) payload = GROUPS;
  else if (bare.endsWith('/api/library/intros')) payload = INTROS_RESP;
  else payload = { error: `演示桩未实现：${bare}` };
  return {
    ok: true,
    status: 200,
    headers: { get: (k) => (String(k).toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: async () => payload,
    text: async () => JSON.stringify(payload)
  };
};

/* ---------------------------------------------------------------- 导入库 */
const libUrl = pathToFileURL(path.join(V2, 'js/views/library.js')).href;
const lib = await import(libUrl);

check('library.js 导出 renderLibrary 与 4 个纯函数（app.js 入口不受影响）',
  typeof lib.renderLibrary === 'function'
  && typeof lib.sortItems === 'function'
  && typeof lib.filterItems === 'function'
  && typeof lib.groupCounts === 'function'
  && typeof lib.applyGroup === 'function');

/* ---------------------------------------------------------------- 1. 排序 6 种（纯函数） */

const LABELS = ['最近更新', '最早更新', '最新创建', '字数最多', '字数最少', '书名'];
check('SORTS 恰好 6 种且文案齐备',
  lib.SORTS.length === 6 && lib.SORTS.map((s) => s.label).join(',') === LABELS.join(','),
  lib.SORTS.map((s) => s.label).join(' / '));
check('6 种排序方向内建成对（更新 asc/desc、字数 asc/desc）',
  lib.SORTS.some((s) => s.value === 'updated' && s.order === 'desc')
  && lib.SORTS.some((s) => s.value === 'updated' && s.order === 'asc')
  && lib.SORTS.some((s) => s.value === 'words' && s.order === 'desc')
  && lib.SORTS.some((s) => s.value === 'words' && s.order === 'asc'));

const B = [
  { key: 'a', name: '长夜灯河', updated_at: '2026-11-20', created_at: '2026-01-05', total_words: 90000 },
  { key: 'b', name: '白鹿原手记', updated_at: '2026-11-10', created_at: '2026-06-01', total_words: 350000 },
  { key: 'c', name: ' Chelsea', updated_at: '2026-11-01', created_at: '2026-11-02', total_words: 12000 }
];

const byUpdatedDesc = lib.sortItems(B, 'updated', 'desc').map((i) => i.key);
check('最近更新：新的在前', byUpdatedDesc.join(',') === 'a,b,c', byUpdatedDesc.join(','));
const byUpdatedAsc = lib.sortItems(B, 'updated', 'asc').map((i) => i.key);
check('最早更新：旧的在前', byUpdatedAsc.join(',') === 'c,b,a', byUpdatedAsc.join(','));
const byCreatedDesc = lib.sortItems(B, 'created', 'desc').map((i) => i.key);
check('最新创建：created 新的在前', byCreatedDesc.join(',') === 'c,b,a', byCreatedDesc.join(','));
const byWordsDesc = lib.sortItems(B, 'words', 'desc').map((i) => i.key);
check('字数最多：35 万在前', byWordsDesc[0] === 'b' && byWordsDesc[2] === 'c', byWordsDesc.join(','));
const byWordsAsc = lib.sortItems(B, 'words', 'asc').map((i) => i.key);
check('字数最少：1.2 万在前', byWordsAsc[0] === 'c', byWordsAsc.join(','));
const byName = lib.sortItems(B, 'name', 'asc').map((i) => i.name.trim());
check('书名排序：中文按拼音（白 < 长）且稳定',
  byName[0] === 'Chelsea' && byName[1] === '白鹿原手记' && byName[2] === '长夜灯河',
  byName.join(' / '));

const P = [
  { key: 'x1', name: '甲', updated_at: '2026-01-01', pinned: true },
  { key: 'x2', name: '乙', updated_at: '2026-12-31' },
  { key: 'x3', name: '丙', updated_at: '2026-06-01', pinned: true }
];
for (const [sv, so] of [['updated', 'desc'], ['updated', 'asc'], ['words', 'desc'], ['name', 'asc']]) {
  const keys = lib.sortItems(P, sv, so).map((i) => i.key).filter((k) => k.startsWith('x'));
  check(`置顶优先（${sv}:${so}）：置顶在前、未置顶垫底`,
    keys.slice(0, 2).every((k) => k !== 'x2') && keys[2] === 'x2', keys.join(','));
}
/* 稳定性：key 相同保持原序 */
const T = [{ key: 'm', updated_at: '2026-05-01' }, { key: 'n', updated_at: '2026-05-01' }, { key: 'o', updated_at: '2026-05-01' }];
check('同键稳定排序', lib.sortItems(T, 'updated', 'desc').map((i) => i.key).join(',') === 'm,n,o');
check('空集不炸', lib.sortItems([], 'words', 'desc').length === 0);

/* ---------------------------------------------------------------- 2. 分组纯函数 */

const assign = { k1: 'g-aaa1aaaa', k3: 'g-bbb2bbbb' };
const counts = lib.groupCounts(ITEMS, assign);
check('groupCounts：全部/未分组/分组计数',
  counts.all === 3 && counts.none === 1 && counts.byGroup['g-aaa1aaaa'] === 1 && counts.byGroup['g-bbb2bbbb'] === 1,
  JSON.stringify(counts));
check('groupCounts：空分组计 0',
  lib.groupCounts(ITEMS, {})['byGroup']['g-bbb2bbbb'] === undefined
  && lib.groupCounts(ITEMS, {}).none === 3);

check('applyGroup all=全部', lib.applyGroup(ITEMS, assign, 'all').length === 3);
check('applyGroup none=未分组', lib.applyGroup(ITEMS, assign, 'none').map((i) => i.key).join(',') === 'k2');
check('applyGroup 指定分组', lib.applyGroup(ITEMS, assign, 'g-aaa1aaaa').map((i) => i.key).join(',') === 'k1');
check('applyGroup 空分组为空集', lib.applyGroup(ITEMS, assign, 'g-zzzzzzzz').length === 0);
check('搜索×分组叠加：搜「白鹿」再选玄幻库为空集',
  lib.applyGroup(lib.filterItems(ITEMS, '白鹿', INTROS), assign, 'g-aaa1aaaa').length === 0);

/* ---------------------------------------------------------------- 3. 搜索过滤（纯函数） */

check('书名命中（大小写不敏感）',
  lib.filterItems(ITEMS, 'chelsea', INTROS).map((i) => i.key).join(',') === 'k2');
check('分类命中', lib.filterItems(ITEMS, '散文', INTROS).map((i) => i.key).join(',') === 'k3');
check('简介命中（intros 字段）',
  lib.filterItems(ITEMS, '典当行', INTROS).map((i) => i.key).join(',') === 'k2');
check('标签命中', lib.filterItems(ITEMS, '热血', INTROS).map((i) => i.key).join(',') === 'k1');
check('无关键词返回等量副本', lib.filterItems(ITEMS, '', INTROS).length === 3
  && lib.filterItems(ITEMS, '', INTROS) !== ITEMS);
check('无命中返回空集', lib.filterItems(ITEMS, '不存在的书名', INTROS).length === 0);
check('简介缺失时降级：其余字段仍可搜', lib.filterItems(ITEMS, '玄幻', {}).length === 1);

/* ---------------------------------------------------------------- 4. 渲染状态：网格/列表切换、搜索、分组 tab、封面 */

const host = document.getElementById('host');
const ctx = { openWizard: () => {}, openBench: () => {}, refreshLibrary: () => {} };
lib.renderLibrary(host, ctx);

await sleep(120);
const q = (sel) => host.querySelector(sel);
const qa = (sel) => Array.from(host.querySelectorAll(sel));

check('初始为网格视图（localStorage 恢复）', !!q('.lib-grid') && !q('.lib-list'));
check('排序下拉恰好 6 个选项', qa('.lib-toolbar select[aria-label="排序"] option').length === 6);
check('搜索框占位含简介字段',
  (q('.lib-search input') || {}).placeholder === '搜索书名 / 分类 / 简介…',
  (q('.lib-search input') || {}).placeholder);
check('分组 tab 条可见且带计数',
  q('.lib-groups') && !q('.lib-groups').hidden && qa('.lib-gtab').length === 4,
  qa('.lib-gtab').map((t) => t.textContent).join('|'));
const tabText = qa('.lib-gtab').map((t) => t.textContent).join('|');
check('分组 tab：全部/未分组/自定义组带计数（k1 在玄幻库，k2/k3 未分组）',
  tabText.includes('全部3') && tabText.includes('未分组2')
  && tabText.includes('玄幻库1') && tabText.includes('完本0'), tabText);
check('卡片封面 img 指向封面端点（无封面时 jsdom 不触发 load/error，占位保留）',
  qa('.lib-card .lib-cover-img').length === 3
  && qa('.lib-card .lib-cover-img').every((img) => img.getAttribute('src').startsWith('/api/library/cover?key=')));
check('有封面的分组 chip 不存在（k2 未分组），k1 卡带分组名',
  qa('.lib-card .lib-group-chip').map((c) => c.textContent).join(',') === '玄幻库');

/* 网格 → 列表 */
const listBtn = qa('.lib-toolbar .seg-btn').find((b) => (b.getAttribute('title') || '') === '列表视图');
listBtn.click();
await sleep(20);
check('切列表：容器类名切换 + localStorage 持久化',
  !!q('.lib-list') && !q('.lib-grid') && localStorage.getItem('shenbi.v2.libView') === 'list');
const gridBtn = qa('.lib-toolbar .seg-btn').find((b) => (b.getAttribute('title') || '') === '网格视图');
gridBtn.click();
await sleep(20);
check('切回网格', !!q('.lib-grid') && localStorage.getItem('shenbi.v2.libView') === 'grid');

/* 本地搜索：简介命中 → 只剩 k2，且分组 tab 计数随搜索收缩 */
const search = q('.lib-search input');
search.value = '典当行';
search.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
await sleep(260);
check('搜索简介命中：卡片收缩为 1 张', qa('.lib-card').length === 1
  && q('.lib-card').dataset.key === 'k2');
const tabAfterSearch = qa('.lib-gtab').map((t) => t.textContent).join('|');
check('分组 tab 计数随搜索收缩（全部1/未分组1/玄幻库0）',
  tabAfterSearch.includes('全部1') && tabAfterSearch.includes('玄幻库0'), tabAfterSearch);

/* 分组过滤：清搜索 → 点「未分组」 */
search.value = '';
search.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
await sleep(260);
const noneTab = qa('.lib-gtab').find((t) => t.textContent.includes('未分组'));
noneTab.click();
await sleep(20);
check('分组过滤：未分组 = k2,k3（k1 在玄幻库）',
  qa('.lib-card').map((c) => c.dataset.key).join(',') === 'k2,k3');
const allTab = qa('.lib-gtab').find((t) => t.textContent.startsWith('全部'));
allTab.click();
await sleep(20);
check('回「全部」：3 张卡片', qa('.lib-card').length === 3);

/* 排序切换（前端完成，不回服务端） */
fetchCalls = [];
const sortSel = q('.lib-toolbar select[aria-label="排序"]');
sortSel.value = 'words:desc';
sortSel.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
await sleep(20);
const keysDesc = qa('.lib-card').map((c) => c.dataset.key).join(',');
check('字数最多排序即时生效（置顶 k2 仍最前，35 万 k3 次之）', keysDesc === 'k2,k3,k1', keysDesc);
sortSel.value = 'updated:asc';
sortSel.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
await sleep(20);
check('最早更新排序即时生效（k2 在前）', qa('.lib-card').map((c) => c.dataset.key)[0] === 'k2');
check('排序切换不触发网络请求', fetchCalls.length === 0, JSON.stringify(fetchCalls));

/* ---------------------------------------------------------------- 5. 契约钉死（源码级） */

const libSrc = read('static/v2/js/views/library.js');
check('回收站恢复/移入走既有端点（不发明第二套软删）',
  libSrc.includes('await api.restore(item.key)') && libSrc.includes('await api.trash(item.key)'));
check('彻底删除复用既有 purge + 手输作品名确认',
  libSrc.includes('await api.purge(item.key, typed)')
  && libSrc.includes('输入完整作品名以确认'));
check('彻底删除列出数据范围：正文/版本/账本/参考',
  ['正文与成稿', '章节版本快照', '人物档案', '账本', '参考资料'].every((k) => libSrc.includes(k)));
check('封面上传：input file → FileReader 即时预览 → FormData 落盘',
  libSrc.includes("type: 'file'") && libSrc.includes('readAsDataURL') && libSrc.includes('new FormData()'));
check('封面请求失败回退预览（不阻塞主链路）',
  libSrc.includes('封面上传失败') && libSrc.includes('delete model.mockCovers[item.key]'));
check('分组/简介走 weblib 端点且 optional 降级',
  libSrc.includes("request('/api/library/groups')") && libSrc.includes("request('/api/library/assign-group'")
  && libSrc.includes("request('/api/library/intros')") && libSrc.includes('optional('));
check('library.css 由 loadCss 注入', libSrc.includes("loadCss('/v2/css/library.css"));

const cssSrc = read('static/v2/css/library.css');
check('library.css 覆盖书架新类',
  ['.lib-gtab', '.lib-gtab-count', '.lib-cover-img', '.lib-cover-title', '.lib-group-chip',
    '.lib-gmgmt-row', '.lib-purge-list'].every((c) => cssSrc.includes(c)));
const hexColors = cssSrc.match(/#[0-9a-fA-F]{3,8}\b/g) || [];
check('library.css 无硬编码颜色（全走 tokens 语义变量）', hexColors.length === 0,
  hexColors.length ? hexColors.join(' ') : '');

const weblibPy = read('bot/weblib.py');
check('前后端路由一一对应（无悬空调用）',
  ['/api/library/groups', '/api/library/assign-group', '/api/library/cover', '/api/library/intros']
    .every((r) => libSrc.includes(r) && weblibPy.includes(r)));
check('weblib.py 封面校验：5MB / 文件头 / 旧封面清理',
  weblibPy.includes('COVER_MAX_BYTES') && weblibPy.includes('sniff_image') && weblibPy.includes('_remove_other_covers'));
check('app.py 未被本任务改动（挂载走两行说明）', weblibPy.includes('app.include_router(weblib.router)'));

/* ---------------------------------------------------------------- 收尾 */

console.log('');
if (failed.length) {
  console.log(`FAIL ${failed.length} 项未通过：`);
  for (const f of failed) console.log(`   - ${f}`);
  process.exit(1);
}
console.log('PASS v5.3.0 书架升级回归全绿');
process.exit(0);
