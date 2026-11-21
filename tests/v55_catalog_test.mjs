// v5.5.x A3 bench 章节导航树（views/bench.js .cat-* 模块，node 直跑，jsdom 仿真）。
//
// 覆盖（易创对齐清单 A3，机制自研）：
// · 树构建纯函数：volumes→弧→章卡分组（状态/字数归一）、章号章列表回落、
//   plan.chapters 平铺回退、空结构、去重排序；
// · 状态徽标映射：done/writing/changed/planned × completed/in_progress/failed/pending
//   两套词表归一（未知值兜底「待写」）；
// · 折叠记忆：localStorage `ew-bench-catalog-folds:<bookKey>` 读写 + LRU 键清单
//   只留最近 50 本（超限删最旧、最近使用的书不被挤掉、损坏 JSON 回空集）；
// · 目录搜索：章题/章号/卷题匹配、命中 <mark> 高亮（先 escapeHtml 再替换——
//   章题含 <script>/<img onerror> 只当文本渲染，防 XSS）、命中卷自动展开、清空恢复折叠；
// · 窄栏 DOM：6 行骨架占位、当前章高亮 + aria-current、行点击回调 onGoto、
//   卷折叠切换并持久化、本会话保存章徽标就地升级、窄容器自动收起/还原、
//   展开态记忆（ew-bench-catalog-open）、destroy 摘除；
// · 结构契约：bench-editor.css 尾部 .cat-* 区块 + @container bench 容器查询就位、
//   cat 区块颜色全部走主题变量（零硬编码色）、bench.js 折叠记忆键与 LRU 常量在位。
import { JSDOM } from 'jsdom';
import path from 'path';
import fs from 'fs';
import { fileURLToPath, pathToFileURL } from 'url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const dom = new JSDOM(
  '<!doctype html><html><body><div id="host"></div></body></html>',
  { url: 'http://127.0.0.1:8848/', pretendToBeVisual: true }
);
global.window = dom.window;
global.document = dom.window.document;
global.Node = dom.window.Node;
global.Element = dom.window.Element;
global.HTMLElement = dom.window.HTMLElement;
global.Event = dom.window.Event;
global.KeyboardEvent = dom.window.KeyboardEvent;
global.CustomEvent = dom.window.CustomEvent;
global.Range = dom.window.Range;
global.getComputedStyle = dom.window.getComputedStyle;
global.requestAnimationFrame = (cb) => setTimeout(cb, 0);
global.sessionStorage = dom.window.sessionStorage;
global.localStorage = dom.window.localStorage;
if (!global.CSS && dom.window.CSS) global.CSS = dom.window.CSS;
if (!global.CSS || !global.CSS.escape) {
  global.CSS = { ...(global.CSS || {}), escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => '\\' + c) };
}
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true, writable: true });

let failed = 0;
function check(name, cond, extra = '') {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${extra ? ' — ' + extra : ''}`);
  if (!cond) failed += 1;
}

/* fetch 桩：bench.js 装载即注入 CSS，但本测试不触发任何请求。 */
global.fetch = async (url) => { throw new Error('unexpected fetch: ' + url); };

const benchMod = await import(pathToFileURL(path.join(root, 'static/v2/js/views/bench.js')).href);
const {
  buildCatalogModel,
  filterCatalogGroups,
  markMatch,
  normCatalogStatus,
  catalogBadge,
  chapterFromCard,
  catFoldKey,
  loadCatFolds,
  saveCatFolds,
  loadCatRailOpen,
  saveCatRailOpen,
  createCatalogRail,
  CAT_FOLD_PREFIX,
  CAT_FOLD_INDEX_KEY,
  CAT_FOLD_MAX_BOOKS,
  CAT_NARROW_PX
} = benchMod;

/* 测试样本：两卷分层规划（章卡带状态与字数 + 纯章号弧 + 跨弧重复章号） */
const VOLUMES_PAYLOAD = {
  ok: true,
  key: 'bk1',
  plan: {
    version: 3,
    volumes: [
      {
        no: 1,
        title: '风起卷',
        arcs: [
          { no: 1, title: '入局', chapter_cards: [
            { n: 1, title: '雨夜来客', status: 'done', word_count: 3200 },
            { n: 2, title: '灯下谈判', status: 'writing', word_count: 1200 },
            { n: 3, title: '旧友重逢', status: 'changed' }
          ] },
          { no: 2, title: '追踪', chapters: [4] }
        ]
      },
      {
        no: 2,
        title: '云涌卷',
        arcs: [
          { no: 3, title: '反击', chapter_cards: [
            { n: 6, title: '雾中行', status: 'planned' },
            { n: 5, title: '夜探', status: 'planned' }
          ] },
          { no: 4, title: '补弧', chapters: [5] }
        ]
      }
    ]
  },
  proposals: []
};

/* ================================================================
   1. 树构建纯函数：卷分组 / 章号回落 / 平铺回退 / 空结构
   ================================================================ */
{
  const m = buildCatalogModel(VOLUMES_PAYLOAD);
  check('树构建：volumes 模式两组卷', m.mode === 'volumes' && m.groups.length === 2, `mode=${m.mode}`);
  check('树构建：卷标题与组 id 取卷号', m.groups[0].title === '风起卷' && m.groups[0].id === 'v1');
  check('树构建：章卡状态归一（done→completed / writing→in_progress / changed→planned）',
    m.groups[0].chapters[0].status === 'completed'
    && m.groups[0].chapters[1].status === 'in_progress'
    && m.groups[0].chapters[2].status === 'planned');
  check('树构建：word_count 映射（3200/1200，缺失为 0）',
    m.groups[0].chapters[0].words === 3200 && m.groups[0].chapters[1].words === 1200
    && m.groups[0].chapters[2].words === 0);
  check('树构建：纯章号弧回落为「第N章/待写/0字」',
    m.groups[0].chapters[3].n === 4 && m.groups[0].chapters[3].title === '第4章'
    && m.groups[0].chapters[3].status === 'planned' && m.groups[0].chapters[3].words === 0);
  check('树构建：跨弧重复章号去重（卷2 的重复 n=5 只保留一条）',
    m.groups[1].chapters.length === 2
    && m.groups[1].chapters.map((c) => c.n).join(',') === '5,6');
  check('树构建：卷内章按章号升序', m.groups[0].chapters.map((c) => c.n).join(',') === '1,2,3,4');
  check('树构建：总章数 = 各卷之和', m.total === 6, `total=${m.total}`);

  const flat = buildCatalogModel({ plan: { chapters: [
    { chapterNumber: 2, title: '乙', status: 'completed', wordCount: 5000 },
    { chapterNumber: 1, title: '甲', status: 'done', wordCount: null }
  ] } });
  check('树构建：无卷结构回落 plan.chapters 平铺（单组 kind=flat）',
    flat.mode === 'flat' && flat.groups.length === 1 && flat.groups[0].kind === 'flat');
  check('树构建：平铺章号升序 + wordCount 映射（null → 0）',
    flat.groups[0].chapters[0].n === 1 && flat.groups[0].chapters[1].words === 5000);

  check('树构建：空规划 → empty 模式', buildCatalogModel({ plan: {} }).mode === 'empty');
  check('树构建：null payload → empty 模式', buildCatalogModel(null).mode === 'empty'
    && buildCatalogModel(null).total === 0);

  check('章行模型：chapterNumber 别名 + 未知状态原样透传',
    (() => { const c = chapterFromCard({ chapterNumber: 7, title: '', status: 'weird-state' });
      return c.n === 7 && c.title === '第7章' && c.status === 'weird-state'; })());
}

/* ================================================================
   2. 状态徽标映射：两套词表归一 + 未知兜底
   ================================================================ */
{
  check('徽标映射：done/completed → completed·完成',
    catalogBadge('done').state === 'completed' && catalogBadge('completed').label === '完成');
  check('徽标映射：writing/in_progress → current·写中',
    catalogBadge('writing').state === 'current' && catalogBadge('in_progress').label === '写中');
  check('徽标映射：error/failed → failed·失败',
    catalogBadge('error').state === 'failed' && catalogBadge('failed').label === '失败');
  check('徽标映射：changed/planned/pending → planned·待写',
    catalogBadge('changed').state === 'planned' && catalogBadge('pending').label === '待写');
  check('徽标映射：未知值与空值兜底「待写」',
    catalogBadge('whatever').label === '待写' && catalogBadge('').label === '待写'
    && normCatalogStatus(null) === 'planned');
}

/* ================================================================
   3. 搜索高亮：先 escapeHtml 再替换 <mark>（防 XSS）
   ================================================================ */
{
  const out = markMatch('雨夜来客', '雨');
  check('搜索高亮：命中词包 <mark class="cat-mark">', out.includes('<mark class="cat-mark">雨</mark>'));
  check('搜索高亮：大小写不敏感（Dark 命中 dark）',
    markMatch('Dark Night', 'dark').includes('<mark class="cat-mark">Dark</mark>'));
  check('搜索高亮：多处命中全部标记',
    (markMatch('雨夜雨夜', '雨').match(/<mark class="cat-mark">/g) || []).length === 2);
  check('搜索高亮：正则元字符按字面（a.b 两处点号）',
    (markMatch('a.b.c', '.').match(/<mark/g) || []).length === 2);
  check('搜索高亮：空查询只做转义（无 mark）',
    markMatch('雨夜来客', '') === '雨夜来客' && markMatch('雨夜来客', '  ').indexOf('<mark') < 0);

  const xss = markMatch('<script>alert(1)</script>', 'script');
  check('搜索高亮（XSS）：章题含 <script> 被转义，不产生可执行标签',
    !xss.includes('<script') && xss.includes('&lt;') && xss.includes('<mark class="cat-mark">script</mark>'),
    xss);
  const img = markMatch('<img src=x onerror=alert(1)>', 'img');
  check('搜索高亮（XSS）：<img onerror> 同样只当文本',
    !img.includes('<img') && img.includes('&lt;') && img.includes('<mark class="cat-mark">img</mark>'));
  check('搜索高亮（XSS）：查询词本身含标签也不注入',
    !markMatch('雨夜来客', '<b>').includes('<b>'));
}

/* ================================================================
   4. 目录搜索过滤：章题 / 章号 / 卷题匹配
   ================================================================ */
{
  const groups = buildCatalogModel(VOLUMES_PAYLOAD).groups;
  check('搜索过滤：空查询透传全部卷章（hits=0）',
    (() => { const r = filterCatalogGroups(groups, '');
      return r.groups.length === 2 && r.hits === 0 && r.groups[0].chapters.length === 4; })());
  check('搜索过滤：章题命中（夜探）带 hit 标记',
    (() => { const r = filterCatalogGroups(groups, '夜探');
      return r.hits === 1 && r.groups.length === 1 && r.groups[0].chapters[0].hit === true; })());
  check('搜索过滤：章号命中（第4章 / n=4）',
    (() => { const r = filterCatalogGroups(groups, '第4章');
      return r.hits >= 1 && r.groups[0].chapters.every((c) => c.n === 4); })());
  check('搜索过滤：卷题命中保留整卷全部章',
    (() => { const r = filterCatalogGroups(groups, '风起卷');
      return r.groups.length === 1 && r.groups[0].match === true && r.groups[0].chapters.length === 4
        && r.groups[0].chapters[1].hit === false; })());
  check('搜索过滤：无命中卷被丢弃', filterCatalogGroups(groups, '不存在的词').groups.length === 0);
  check('搜索过滤：大小写不敏感（英文卷题）',
    (() => { const g2 = [{ id: 'v1', kind: 'volume', no: 1, title: 'Dark Rise', chapters: [{ n: 1, title: 'rain', status: 'planned', words: 0 }] }];
      return filterCatalogGroups(g2, 'dark').groups.length === 1; })());
}

/* ================================================================
   5. 折叠记忆：读写 + LRU 只留最近 50 本
   ================================================================ */
{
  const ls = global.localStorage;
  const bk = 'fold-book-a';
  ls.removeItem(catFoldKey(bk));
  check('折叠记忆：读空键回空集', JSON.stringify(loadCatFolds(bk)) === '[]');

  saveCatFolds(bk, ['v2', 'v3', 'v2'], ls);
  check('折叠记忆：写入后可读回（去重）',
    JSON.stringify(loadCatFolds(bk)) === JSON.stringify(['v2', 'v3'])
    && ls.getItem(catFoldKey(bk)).includes('v2'));
  check('折叠记忆：键名符合 ew-bench-catalog-folds:<bookKey> 约定',
    catFoldKey(bk) === `${CAT_FOLD_PREFIX}${bk}` && CAT_FOLD_PREFIX === 'ew-bench-catalog-folds:');

  ls.setItem(catFoldKey('broken-book'), '{oops');
  check('折叠记忆：损坏 JSON 回空集（不炸）', JSON.stringify(loadCatFolds('broken-book')) === '[]');

  /* LRU：写满 50 本 → 第 51 本挤掉最旧 */
  const base = 'lru-book-';
  for (let i = 0; i < CAT_FOLD_MAX_BOOKS; i += 1) saveCatFolds(base + i, ['v1'], ls);
  const idxMid = JSON.parse(ls.getItem(CAT_FOLD_INDEX_KEY));
  check('折叠记忆 LRU：50 本时键清单长度 = 50', idxMid.length === CAT_FOLD_MAX_BOOKS);
  saveCatFolds(base + CAT_FOLD_MAX_BOOKS, ['v1'], ls);
  const idxAfter = JSON.parse(ls.getItem(CAT_FOLD_INDEX_KEY));
  check('折叠记忆 LRU：第 51 本写入后键清单仍为 50',
    idxAfter.length === CAT_FOLD_MAX_BOOKS, `len=${idxAfter.length}`);
  check('折叠记忆 LRU：最旧的书目键被删除',
    ls.getItem(catFoldKey(base + '0')) === null && idxAfter[0] === base + '1');
  check('折叠记忆 LRU：最新书目键保留且在队尾',
    idxAfter[idxAfter.length - 1] === base + CAT_FOLD_MAX_BOOKS
    && loadCatFolds(base + CAT_FOLD_MAX_BOOKS, ls).length === 1);

  /* LRU 触碰：最近折叠过的书不被挤掉 */
  saveCatFolds(base + '1', ['v9'], ls);   // 触碰 b1 → 移到队尾
  saveCatFolds(base + (CAT_FOLD_MAX_BOOKS + 1), ['v1'], ls);   // 再挤一本
  check('折叠记忆 LRU：最近使用的书存活，次旧被淘汰',
    ls.getItem(catFoldKey(base + '1')) !== null && ls.getItem(catFoldKey(base + '2')) === null);
  check('折叠记忆 LRU：CAT_FOLD_MAX_BOOKS === 50', CAT_FOLD_MAX_BOOKS === 50);
}

/* ================================================================
   6. 窄栏 DOM：骨架 / 高亮 / 徽标 / 折叠 / 搜索 / 窄容器 / 记忆
   ================================================================ */
{
  const ls = global.localStorage;
  const bk = 'rail-book';
  ls.removeItem(catFoldKey(bk));
  ls.removeItem('ew-bench-catalog-open');
  saveCatRailOpen(true, ls);

  const gotos = [];
  const rail = createCatalogRail({ key: bk, onGoto: (n) => gotos.push(n) });
  document.getElementById('host').append(rail.el);

  /* 骨架占位：数据未到 → 6 行骨架，别像「空书」 */
  rail.setData(null, 0);
  check('窄栏 DOM：加载中显示 6 行骨架', rail.el.querySelectorAll('.cat-skel-row').length === 6
    && rail.debug().dataState === 'loading');

  /* 数据到位：卷头 + 章行 + 徽标 + 字数 + 当前章高亮 */
  rail.setData(VOLUMES_PAYLOAD, 2);
  const dbg = rail.debug();
  check('窄栏 DOM：规划到位后渲染 2 卷 6 行', dbg.vols === 2 && dbg.rows === 6, JSON.stringify(dbg));
  check('窄栏 DOM：当前章行高亮（is-current + aria-current）',
    (() => { const cur = rail.el.querySelector('.cat-row.is-current');
      return cur && cur.dataset.n === '2' && cur.getAttribute('aria-current') === 'true'; })());
  check('窄栏 DOM：进行中章徽标 data-state=current·写中',
    rail.el.querySelector('.cat-row[data-n="2"] .cat-badge').dataset.state === 'current'
    && rail.el.querySelector('.cat-row[data-n="2"] .cat-badge').textContent.includes('写中'));
  check('窄栏 DOM：完成章徽标 data-state=completed·完成',
    rail.el.querySelector('.cat-row[data-n="1"] .cat-badge').dataset.state === 'completed');
  check('窄栏 DOM：待写章徽标 data-state=planned·待写',
    rail.el.querySelector('.cat-row[data-n="3"] .cat-badge').dataset.state === 'planned');
  check('窄栏 DOM：字数徽标在行内（3,200 字）',
    rail.el.querySelector('.cat-row[data-n="1"] .cat-words').textContent.includes('3,200'));
  check('窄栏 DOM：头部统计（6 章）', rail.el.querySelector('.cat-count').textContent.includes('6'));

  /* 行点击 → onGoto 回调（bench 侧接 navigate 切章） */
  rail.el.querySelector('.cat-row[data-n="3"]').click();
  check('窄栏 DOM：点击行回调 onGoto(3)', gotos.length === 1 && gotos[0] === 3);

  /* 卷折叠：点击卷头收起并持久化；重建窄栏后记忆恢复 */
  const vol1Head = rail.el.querySelectorAll('.cat-vol-head')[0];
  vol1Head.click();
  check('窄栏 DOM：点击卷头收起（data-fold=true 且章行隐藏）',
    rail.el.querySelectorAll('.cat-vol')[0].dataset.fold === 'true');
  check('折叠记忆：折叠状态写入 localStorage', loadCatFolds(bk, ls).includes('v1'));
  rail.destroy();
  const rail2 = createCatalogRail({ key: bk, onGoto: (n) => gotos.push(n) });
  document.getElementById('host').append(rail2.el);
  rail2.setData(VOLUMES_PAYLOAD, 5);   // 当前章在卷2：卷1 折叠保持，卷2 自动展开
  check('窄栏 DOM：重建窄栏后卷1 折叠记忆恢复',
    rail2.el.querySelectorAll('.cat-vol')[0].dataset.fold === 'true'
    && rail2.el.querySelectorAll('.cat-vol')[1].dataset.fold === 'false');
  check('窄栏 DOM：当前章所在卷自动展开（卷2 含第 5 章）',
    rail2.el.querySelector('.cat-row.is-current').dataset.n === '5');

  /* 本会话保存章：徽标就地升级（第 5 章 planned → completed） */
  rail2.onSaved(5, 4500);
  check('窄栏 DOM：onSaved 后本会话保存章徽标升级为完成',
    rail2.el.querySelector('.cat-row[data-n="5"] .cat-badge').dataset.state === 'completed');
  check('窄栏 DOM：已完成章不被降级',
    rail2.el.querySelector('.cat-row[data-n="1"] .cat-badge').dataset.state === 'completed');

  /* 搜索：命中章保留 + <mark> 高亮 + 命中卷自动展开；无命中给空态 */
  const input = rail2.el.querySelector('.cat-search-input');
  input.value = '雨';
  input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  check('窄栏 DOM：搜索过滤出命中章（雨夜来客）',
    rail2.el.querySelectorAll('.cat-row').length === 1
    && rail2.el.querySelector('.cat-row .cat-title').innerHTML.includes('<mark class="cat-mark">雨</mark>'));
  input.value = '风起卷';
  input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  check('窄栏 DOM：卷题命中自动展开整卷（4 行 + 卷头 <mark>）',
    rail2.el.querySelectorAll('.cat-row').length === 4
    && rail2.el.querySelectorAll('.cat-vol')[0].dataset.fold === 'false'
    && rail2.el.querySelector('.cat-vol-title').innerHTML.includes('<mark'));
  input.value = '查无此章';
  input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  check('窄栏 DOM：无命中给空态提示', rail2.el.querySelectorAll('.cat-row').length === 0
    && rail2.el.querySelector('.cat-empty') !== null);
  input.value = '';
  input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  check('窄栏 DOM：清空搜索恢复折叠记忆（卷1 重新收起）',
    rail2.el.querySelectorAll('.cat-vol')[0].dataset.fold === 'true'
    && rail2.el.querySelectorAll('.cat-row').length === 6);
  check('窄栏 DOM：搜索不触发滚动重置且行数守恒（6 行）', rail2.debug().rows === 6);

  /* 窄容器：自动收起（不写展开偏好），退回宽容器还原 */
  check('窄栏 DOM：默认展开（data-open=true）', rail2.el.dataset.open === 'true');
  rail2.setNarrow(true);
  check('窄栏 DOM：窄容器自动收起（data-narrow=true + data-open=false）',
    rail2.el.dataset.narrow === 'true' && rail2.el.dataset.open === 'false');
  rail2.setNarrow(false);
  check('窄栏 DOM：退回宽容器还原展开态', rail2.el.dataset.narrow === 'false'
    && rail2.el.dataset.open === 'true');
  check('窄栏 DOM：自动收起不写入展开偏好（偏好仍为 1）', ls.getItem('ew-bench-catalog-open') === '1');
  /* 窄容器下展开态记忆为 false：收起后不误还原 */
  saveCatRailOpen(false, ls);
  const rail3 = createCatalogRail({ key: bk, onGoto: () => {} });
  document.getElementById('host').append(rail3.el);
  check('窄栏 DOM：偏好为收起时新窄栏默认收起', rail3.el.dataset.open === 'false');
  rail3.setNarrow(true);
  rail3.setNarrow(false);
  check('窄栏 DOM：原本收起的窄栏跨窄容器不误展开', rail3.el.dataset.open === 'false');
  rail3.destroy();

  /* 展开态记忆：收起按钮点击 → 偏好落盘；重建窄栏保持收起；图标按钮可再展开 */
  rail2.el.querySelector('.cat-collapse').click();
  check('窄栏 DOM：收起按钮写入偏好（ew-bench-catalog-open=0）', ls.getItem('ew-bench-catalog-open') === '0');
  rail2.destroy();
  const rail4 = createCatalogRail({ key: bk, onGoto: () => gotos.push(-1) });
  document.getElementById('host').append(rail4.el);
  check('窄栏 DOM：重建窄栏保持收起态', rail4.el.dataset.open === 'false');
  rail4.el.querySelector('.cat-open-btn').click();
  check('窄栏 DOM：图标条点击展开', rail4.el.dataset.open === 'true');

  /* 规划不可用 / 空规划的空态文案 */
  rail4.setData(null, 2, { failed: true });
  check('窄栏 DOM：规划加载失败给可操作空态（非骨架）',
    rail4.debug().dataState === 'failed' && rail4.el.querySelector('.cat-empty') !== null
    && rail4.el.querySelectorAll('.cat-skel-row').length === 0);
  rail4.setData({ plan: {} }, 2);
  check('窄栏 DOM：空规划给引导文案', rail4.debug().dataState === 'empty'
    && rail4.el.querySelector('.cat-empty').textContent.includes('规划'));

  rail4.destroy();
  check('窄栏 DOM：destroy 摘除根节点', !rail4.el.parentNode);
}

/* ================================================================
   7. 结构契约：CSS 区块 / 容器查询 / 零硬编码色 / 键约定
   ================================================================ */
{
  const css = fs.readFileSync(path.join(root, 'static/v2/css/bench-editor.css'), 'utf-8');
  const catCss = css.slice(css.indexOf('A3 章节导航树'));
  check('样式命名空间：.cat-* 区块在 bench-editor.css 尾部就位',
    catCss.includes('.cat-rail') && catCss.includes('.cat-row') && catCss.includes('.cat-badge')
    && catCss.includes('.cat-skel-row') && catCss.includes('mark.cat-mark'));
  check('样式契约：窄容器收起（@container bench + 920px 阈值）在位',
    catCss.includes('@container bench (max-width: 920px)')
    && catCss.includes('container-name: bench') && catCss.includes('container-type: inline-size'));
  check('样式契约：窄容器浮层展开（绝对定位 + 260px）与图标条（44px）规则在位',
    catCss.includes('position: absolute') && catCss.includes('width: 260px') && catCss.includes('width: 44px'));
  const colorDecls = catCss.match(/(?:^|[;{\n])\s*(?:color|background(?:-color)?|border(?:-color)?|outline(?:-color)?|fill)\s*:[^;}]+/g) || [];
  const varOnly = colorDecls.length > 0
    && colorDecls.every((d) => !/#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/.test(d));
  check('样式契约：cat 区块颜色全部走主题变量（无 hex/rgb 字面量，四主题自适应）', varOnly,
    colorDecls.filter((d) => /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/.test(d)).slice(0, 3).join(' | '));

  const js = fs.readFileSync(path.join(root, 'static/v2/js/views/bench.js'), 'utf-8');
  check('结构契约：bench.js 折叠记忆键（ew-bench-catalog-folds:）与 LRU 常量在位',
    js.includes("'ew-bench-catalog-folds:'") && js.includes('CAT_FOLD_MAX_BOOKS = 50')
    && js.includes("'ew-bench-catalog-folds:_idx'"));
  check('结构契约：窄栏挂载点（insertBefore editorCol）与 store 订阅重画在位',
    js.includes('body.insertBefore(catRail.el, editorCol)') && js.includes('const catUnsub = store.subscribe'));
  check('结构契约：保存链路徽标就地升级 + destroy 清理在位',
    js.includes('catRail.onSaved(model.n, model.words)') && js.includes('catUnsub();')
    && js.includes('catRail.destroy();'));
  check('结构契约：数据源复用既有 refreshPlan 链路（不新增端点）',
    js.includes('ctx.refreshPlan && ctx.refreshPlan()'));
  check('结构契约：窄容器阈值 JS/CSS 对齐（920）', CAT_NARROW_PX === 920);
}

/* ================================================================
   汇总
   ================================================================ */
console.log(failed ? `\n${failed} 项失败` : '\n全部通过');
process.exit(failed ? 1 : 0);
