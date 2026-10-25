/* ?mock=1 演示后端：不依赖 FastAPI / LLM，覆盖全界面所需的读接口与主要写操作。
   数据保存在模块内存中，刷新页面即还原。 */

import {
  mockLibraryItems,
  mockProjects,
  mockChapters,
  mockChapterText,
  mockVersions,
  mockPlan,
  mockState,
  mockReferences,
  mockSkills,
  mockLearn,
  mockStyles,
  mockSettings,
  mockProviders,
  mockModels,
  mockMediums,
  mockUsage,
  mockSearchHits,
  mockCandidateRef,
  mockAssistantReplyFor,
  DEMO_KEY
} from './fixtures.js';

const clone = (value) =>
  typeof structuredClone === 'function' ? structuredClone(value) : JSON.parse(JSON.stringify(value));

const nowIso = () => new Date().toISOString().slice(0, 19);

const db = {
  library: clone(mockLibraryItems).map((item) => ({
    ...item,
    progress: item.total ? Math.round((item.done / item.total) * 1000) / 10 : 0
  })),
  projects: clone(mockProjects),
  chapters: clone(mockChapters),
  texts: {},
  versions: clone(mockVersions),
  plan: clone(mockPlan),
  state: clone(mockState),
  refs: clone(mockReferences),
  skills: clone(mockSkills),
  settings: clone(mockSettings),
  providers: clone(mockProviders),
  usage: clone(mockUsage)
};

db.texts[47] = mockChapterText;

function mockError(message, status = 404) {
  const err = new Error(message);
  err.status = status;
  err.code = status === 404 ? 'mock_not_found' : 'mock_error';
  return err;
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function jitter(base = 120) {
  return delay(base + Math.random() * base * 0.9);
}

function findBook(key) {
  return db.library.find((i) => i.key === key) || null;
}

function requireBook(key) {
  const book = findBook(key);
  if (!book) throw mockError('作品不存在（演示数据）', 404);
  return book;
}

function libraryStats(items) {
  const totalWords = items.reduce((sum, i) => sum + (Number(i.total_words) || 0), 0);
  const qs = items.map((i) => i.quality).filter((q) => q != null);
  const byState = {};
  const genres = {};
  for (const item of items) {
    byState[item.state] = (byState[item.state] || 0) + 1;
    genres[item.genre] = (genres[item.genre] || 0) + 1;
  }
  return {
    count: items.length,
    by_state: byState,
    passing: items.filter((i) => i.passing === true).length,
    unrated: items.filter((i) => i.quality == null).length,
    avg_quality: qs.length ? Math.round((qs.reduce((a, b) => a + b, 0) / qs.length) * 10) / 10 : null,
    total_words: totalWords,
    genres
  };
}

function chapterCards() {
  return db.chapters.map((c) => ({
    n: c.chapterNumber,
    title: c.title,
    status: c.status,
    words: c.wordCount,
    volume: Math.floor((c.chapterNumber - 1) / 30) + 1,
    arc: Math.floor(((c.chapterNumber - 1) % 30) / 10) + 1,
    filePath: c.filePath,
    retries: c.retryCount
  }));
}

function planPayload() {
  const plan = clone(db.plan.plan);
  const cards = chapterCards();
  for (const volume of plan.volumes || []) {
    for (const arc of volume.arcs || []) {
      arc.chapter_cards = arc.chapters
        .map((n) => cards.find((c) => c.n === n))
        .filter(Boolean);
    }
  }
  plan.chapters = cards;
  const digestBase = {
    chapters_written: db.chapters.filter((c) => c.status === 'completed').length,
    chapters_planned: db.chapters.length,
    words: db.chapters.reduce((sum, c) => sum + (c.wordCount || 0), 0)
  };
  return {
    ok: true,
    key: db.plan.key,
    plan,
    digest: { ...digestBase, summary: db.plan.plan.digest, version: plan.version },
    proposals: clone(db.plan.proposals)
  };
}

function statePayload(key) {
  const book = findBook(key) || findBook(DEMO_KEY);
  const done = book ? book.done : 46;
  const total = book ? book.total : 120;
  const cur = Math.min(done + 1, total || done + 1);
  const due = db.state.foreshadowing.filter((f) => f.due <= cur && f.status !== 'done');
  return {
    ok: true,
    key,
    state: clone(db.state),
    current_chapter: cur,
    chapters: { done, total },
    characters: clone(db.state.characters),
    locations: clone(db.state.locations),
    items: clone(db.state.items),
    factions: [],
    threads: clone(db.state.threads),
    foreshadowing: clone(db.state.foreshadowing),
    timeline: clone(db.state.timeline),
    issues: clone(db.state.issues),
    foreshadow_due: { due: clone(due), due_count: due.length },
    excerpt: ''
  };
}

function shapeRefs() {
  return db.refs.map((r) => ({
    ...r,
    chars: r.chars || 0,
    usage_mode: r.usage_mode || 'facts'
  }));
}

function refSearch(q) {
  const query = String(q || '').trim();
  if (!query) return [];
  return mockSearchHits.map((hit) => ({ ...hit, query }));
}

function updateChapterWords(n, text) {
  const words = String(text).replace(/\s+/g, '').length;
  const ch = db.chapters.find((c) => c.chapterNumber === Number(n));
  if (ch) {
    ch.wordCount = words;
    if (words > 0 && ch.status === 'pending') ch.status = 'in_progress';
  }
  return words;
}

function mockVersionItem(v) {
  return {
    id: v.id,
    ts: v.at,
    size: v.words,
    label: v.label
  };
}

function applyPlanProposal(id, status) {
  const prop = db.plan.proposals.find((p) => p.id === id);
  if (!prop) throw mockError('提案不存在（演示数据）', 404);
  prop.status = status;
  if (status === 'applied') {
    db.plan.plan.version = Number(db.plan.plan.version || 0) + 1;
    db.plan.plan.digest = `${prop.summary} 已应用到 ${prop.scope}。`;
  }
  return prop;
}

function providerPublic(p) {
  return {
    ...p,
    key_display: p.has_key ? 'sk-****' : '',
    has_key: Boolean(p.has_key)
  };
}

function providersPayload() {
  return {
    ...clone(db.providers),
    providers: db.providers.providers.map(providerPublic),
    key_ok: db.providers.providers.some((p) => p.has_key)
  };
}

const handlers = [
  ['GET', /^\/api\/capabilities$/, async () => ({
    ok: true,
    features: {
      classic_ui: true,
      v2_ui: true,
      chat_stream: true,
      longform: true,
      references: true,
      feedback: true,
      skill_evolution: true,
      eval: true
    },
    mediums: mockMediums,
    mock: true
  })],

  ['GET', /^\/api\/projects$/, async () => ({ items: clone(db.projects), projects: clone(db.projects) })],

  ['GET', /^\/api\/library$/, async (m, opts) => {
    const q = opts.query || {};
    let items = db.library.slice();
    if (q.include_archived === 'true' || q.include_archived === true) {
      /* 全部 */
    } else {
      items = items.filter((i) => !i.archived);
    }
    if (q.status === 'archived') items = db.library.filter((i) => i.archived);
    else if (q.status === 'passing') items = items.filter((i) => i.passing === true);
    else if (q.status === 'unrated') items = items.filter((i) => i.quality == null);
    else if (q.status) items = items.filter((i) => i.state === q.status || i.status_label === q.status);
    if (q.genre) items = items.filter((i) => i.genre === q.genre);
    if (q.q) {
      const needle = String(q.q).toLowerCase();
      items = items.filter(
        (i) =>
          i.name.toLowerCase().includes(needle) ||
          String(i.genre || '').toLowerCase().includes(needle) ||
          (i.tags || []).some((t) => String(t).toLowerCase().includes(needle))
      );
    }
    const sort = q.sort || 'updated';
    const dir = q.order === 'asc' ? 1 : -1;
    const val = (i) => {
      if (sort === 'quality') return i.quality == null ? -1 : i.quality;
      if (sort === 'words' || sort === 'total_words') return i.total_words || 0;
      if (sort === 'progress' || sort === 'completion') return i.progress || 0;
      if (sort === 'created' || sort === 'created_at') return i.created_at || '';
      if (sort === 'chapters' || sort === 'total') return i.total || 0;
      if (sort === 'genre') return i.genre || '';
      return i.updated_at || i.created_at || '';
    };
    items.sort((a, b) => {
      const va = val(a);
      const vb = val(b);
      if (va === vb) return 0;
      return va > vb ? dir : -dir;
    });
    return {
      items: clone(items),
      stats: libraryStats(items),
      facets: { genres: Array.from(new Set(db.library.map((i) => i.genre))).sort() },
      sort,
      order: q.order || 'desc',
      filters: { genre: q.genre || '', status: q.status || '', q: q.q || '' }
    };
  }],

  ['POST', /^\/api\/library\/([^/]+)\/favorite$/, async (m, opts) => {
    const book = requireBook(decodeURIComponent(m[1]));
    book.favorite = Boolean(opts.body && opts.body.on);
    book.updated_at = nowIso();
    return { ok: true, favorite: book.favorite };
  }],

  ['POST', /^\/api\/library\/([^/]+)\/archive$/, async (m, opts) => {
    const book = requireBook(decodeURIComponent(m[1]));
    book.archived = Boolean(opts.body && opts.body.on);
    book.updated_at = nowIso();
    return { ok: true, archived: book.archived };
  }],

  ['POST', /^\/api\/library\/([^/]+)\/rename$/, async (m, opts) => {
    const book = requireBook(decodeURIComponent(m[1]));
    const name = String((opts.body && opts.body.name) || '').trim();
    if (!name) throw mockError('书名不能为空', 400);
    book.name = name.slice(0, 60);
    book.updated_at = nowIso();
    return { ...book };
  }],

  ['POST', /^\/api\/library\/([^/]+)\/tags$/, async (m, opts) => {
    const book = requireBook(decodeURIComponent(m[1]));
    book.tags = Array.isArray(opts.body && opts.body.tags) ? opts.body.tags.slice(0, 12) : [];
    return { ...book };
  }],

  ['DELETE', /^\/api\/library\/([^/]+)$/, async (m, opts) => {
    const key = decodeURIComponent(m[1]);
    requireBook(key);
    if ((opts.query || {}).hard) db.library = db.library.filter((i) => i.key !== key);
    else {
      const book = findBook(key);
      book.archived = true;
      book.updated_at = nowIso();
    }
    return { ok: true };
  }],

  ['POST', /^\/api\/library\/batch$/, async (m, opts) => {
    const keys = (opts.body && opts.body.keys) || [];
    const action = (opts.body && opts.body.action) || '';
    const results = [];
    for (const key of keys) {
      const book = findBook(key);
      if (!book) {
        results.push({ key, ok: false, note: '作品不存在' });
        continue;
      }
      if (action === 'restore') book.archived = false;
      else if (action === 'favorite') book.favorite = true;
      else if (action === 'archive' || action === 'delete') book.archived = true;
      else {
        results.push({ key, ok: false, note: `未知操作 ${action}` });
        continue;
      }
      book.updated_at = nowIso();
      results.push({ key, ok: true });
    }
    return { ok: true, action, results };
  }],

  ['GET', /^\/api\/book-live$/, async (m, opts) => {
    const key = (opts.query || {}).key;
    const book = findBook(key) || findBook(DEMO_KEY);
    if (!book) throw mockError('作品不存在（演示数据）', 404);
    const phase =
      book.state === 'completed' ? 'done' : book.state === 'failed' ? 'failed' : book.running ? 'writing' : 'idle';
    return {
      key: book.key,
      name: book.name,
      phase,
      state: book.state,
      current_chapter: Math.min(book.done + 1, book.total),
      progress: { done: book.done, total: book.total, pct: book.total ? (book.done / book.total) * 100 : 0 },
      running: Boolean(book.running),
      paused: !book.running && book.state === 'in_progress',
      parallel: book.running && book.done % 7 === 0,
      rewriting: false,
      releasing: false,
      live_log: [
        `[${new Date().toLocaleTimeString('zh-CN', { hour12: false })}] 检索第 ${book.done} 章上下文`,
        `[${new Date().toLocaleTimeString('zh-CN', { hour12: false })}] 生成第 ${book.done + 1} 章场景节拍`,
        `[${new Date().toLocaleTimeString('zh-CN', { hour12: false })}] 自检：伏笔到期 / 知情范围`
      ],
      updated_at: Math.floor(Date.now() / 1000),
      mock: true
    };
  }],

  ['GET', /^\/api\/status$/, async (m, opts) => {
    const book = findBook((opts.query || {}).key);
    if (!book) throw mockError('作品不存在（演示数据）', 404);
    return {
      key: book.key,
      name: book.name,
      state: book.state,
      status_label: book.status_label,
      done: book.done,
      total: book.total,
      running: book.running,
      paused: !book.running && book.state === 'in_progress',
      quality: book.quality,
      pass_line: book.pass_line,
      total_words: book.total_words
    };
  }],

  ['GET', /^\/api\/plan$/, async () => planPayload()],

  ['POST', /^\/api\/plan\/ensure$/, async () => planPayload()],

  ['POST', /^\/api\/plan\/replan$/, async (m, opts) => {
    const body = opts.body || {};
    const id = `prop-${2010 + db.plan.proposals.length}`;
    const proposal = {
      id,
      scope: body.scope === 'book' ? '全书' : body.scope === 'volume' ? '当前卷' : '当前窗口',
      reason: body.reason || '手动触发重规划',
      instruction: body.instruction || '按当前读者反馈重排接下来的章节。',
      created_at: nowIso(),
      status: 'pending',
      summary: '演示提案：压缩重复取证，提前一次可验证的小胜。',
      diff: '- 合并两章重复调查\n+ 新增一次对峙\n~ 线人提前出场'
    };
    db.plan.proposals.unshift(proposal);
    return { ok: true, proposal };
  }],

  ['POST', /^\/api\/plan\/apply$/, async (m, opts) => {
    const body = opts.body || {};
    const prop = applyPlanProposal(String(body.proposal_id || ''), 'applied');
    return { ok: true, result: { proposal: prop, version: db.plan.plan.version } };
  }],

  ['POST', /^\/api\/plan\/reject$/, async (m, opts) => {
    const body = opts.body || {};
    const prop = applyPlanProposal(String(body.proposal_id || ''), 'rejected');
    prop.reject_note = body.note || '';
    return { ok: true, result: { proposal: prop } };
  }],

  ['POST', /^\/api\/plan\/window$/, async (m, opts) => {
    const body = opts.body || {};
    return {
      ok: true,
      result: {
        start: body.start,
        end: body.end,
        chapters: chapterCards().filter((c) => c.n >= body.start && c.n <= body.end)
      }
    };
  }],

  ['GET', /^\/api\/state$/, async (m, opts) => statePayload((opts.query || {}).key)],

  ['GET', /^\/api\/references$/, async () => ({ ok: true, items: shapeRefs() })],

  ['POST', /^\/api\/references\/text$/, async (m, opts) => {
    const body = opts.body || {};
    const text = String(body.text || '');
    if (!text.trim()) throw mockError('缺少参考资料正文', 400);
    const item = {
      ...clone(mockCandidateRef),
      id: `ref-${Date.now()}`,
      title: String(body.title || '未命名资料').slice(0, 60),
      kind: 'text',
      source: '粘贴文本',
      chars: text.length,
      usage_mode: body.usage_mode || 'facts',
      created_at: nowIso(),
      summary: `已入库 ${text.length} 字，等待分析生成摘要。`,
      tags: []
    };
    db.refs.unshift(item);
    return { ok: true, item };
  }],

  ['POST', /^\/api\/references\/url$/, async (m, opts) => {
    const body = opts.body || {};
    const url = String(body.url || '');
    if (!/^https?:\/\//i.test(url)) throw mockError('只支持 http/https 网址', 400);
    const item = {
      ...clone(mockCandidateRef),
      id: `ref-${Date.now()}`,
      title: body.title || url.replace(/^https?:\/\//, '').slice(0, 40),
      kind: 'url',
      source: url,
      chars: 3800,
      usage_mode: body.usage_mode || 'facts',
      created_at: nowIso(),
      summary: '已抓取网页正文（演示），等待分析。',
      tags: []
    };
    db.refs.unshift(item);
    return { ok: true, item };
  }],

  ['POST', /^\/api\/references\/upload$/, async (m, opts) => {
    const form = opts.form;
    if (!form || typeof form.get !== 'function') throw mockError('缺少文件', 400);
    const file = form.get('file');
    const title = String(form.get('title') || (file && file.name) || '上传的参考资料');
    const usage = String(form.get('usage_mode') || 'facts');
    if (!file) throw mockError('缺少文件', 400);
    const item = {
      ...clone(mockCandidateRef),
      id: `ref-${Date.now()}`,
      title: title.slice(0, 60),
      kind: 'file',
      source: file.name || '上传文件',
      chars: Math.max(1200, Math.round((file.size || 24000) / 2)),
      usage_mode: usage,
      created_at: nowIso(),
      summary: `已接收 ${file.name || '文件'}，正文抽取完成（演示）。`,
      copyright_confirmed: true
    };
    db.refs.unshift(item);
    await jitter(280);
    return { ok: true, item };
  }],

  ['PATCH', /^\/api\/references\/([^/]+)$/, async (m, opts) => {
    const id = decodeURIComponent(m[1]);
    const item = db.refs.find((r) => r.id === id);
    if (!item) throw mockError('参考资料不存在', 404);
    Object.assign(item, opts.body || {});
    return { ok: true, item: clone(item) };
  }],

  ['DELETE', /^\/api\/references\/([^/]+)$/, async (m) => {
    const id = decodeURIComponent(m[1]);
    db.refs = db.refs.filter((r) => r.id !== id);
    return { ok: true };
  }],

  ['POST', /^\/api\/references\/([^/]+)\/analyze$/, async (m) => {
    const id = decodeURIComponent(m[1]);
    const item = db.refs.find((r) => r.id === id);
    if (!item) throw mockError('参考资料不存在', 404);
    await jitter(420);
    item.summary = `分析完成：可用于「${item.usage_mode}」，提取 6 个可用事实与 2 个场景细节（演示）。`;
    return { ok: true, analysis: { summary: item.summary, tags: ['演示', '已分析'] } };
  }],

  ['GET', /^\/api\/references\/([^/]+)\/chunks$/, async (m, opts) => {
    const id = decodeURIComponent(m[1]);
    const item = db.refs.find((r) => r.id === id);
    if (!item) throw mockError('参考资料不存在', 404);
    return {
      ok: true,
      items: [
        { no: 1, text: '1998 年 7 月 14 日夜，南岸航道因暴雨临时封闭，渡轮“白鹭号”偏离航线。' },
        { no: 2, text: '灯塔值守人员每四小时记录一次潮汐与能见度，记录本按年份装订。' }
      ],
      total: 2,
      offset: (opts.query || {}).offset || 0
    };
  }],

  ['GET', /^\/api\/references\/search$/, async (m, opts) => {
    const q = (opts.query || {}).q;
    return {
      ok: true,
      items: refSearch(q),
      block: refSearch(q)
        .map((h) => h.snippet)
        .join('\n')
    };
  }],

  ['POST', /^\/api\/references\/link$/, async () => ({ ok: true, result: { linked: true } })],
  ['POST', /^\/api\/references\/unlink$/, async () => ({ ok: true, result: { linked: false } })],
  ['POST', /^\/api\/references\/overlap$/, async () => ({
    ok: true,
    overlaps: [{ ref_id: 'ref-1', score: 0.18, excerpt: '南岸航道因暴雨临时封闭' }],
    max_score: 0.18
  })],

  ['POST', /^\/api\/feedback$/, async (m, opts) => ({
    ok: true,
    record: { ...(opts.body || {}), at: nowIso() },
    summary: clone(db.skills.feedback)
  })],

  ['GET', /^\/api\/feedback$/, async () => ({
    ok: true,
    items: [],
    summary: clone(db.skills.feedback),
    focus: { tags: ['钩子', '人物一致'] }
  })],

  ['GET', /^\/api\/skills\/evolution$/, async () => ({
    ok: true,
    candidates: db.skills.candidates.map((c) => ({ ...c })),
    history: db.skills.versions.map((v) => ({ ...v })),
    feedback: clone(db.skills.feedback),
    stats: { skills: db.skills.versions.length, active: db.skills.versions.filter((v) => v.active).length }
  })],

  ['POST', /^\/api\/skills\/evolution\/propose$/, async (m, opts) => {
    const body = opts.body || {};
    await jitter(500);
    const candidate = {
      id: `cand-${Date.now()}`,
      name: body.title || body.name || '新候选技能',
      target: body.stage || 'style',
      delta: '+?.?',
      reason: body.reason || '根据最近的反馈生成。',
      pending: true,
      diff: body.content || '+ 演示候选规则'
    };
    db.skills.candidates.unshift(candidate);
    return { ok: true, candidate };
  }],

  ['POST', /^\/api\/skills\/evolution\/evaluate$/, async (m, opts) => {
    const id = (opts.body || {}).id;
    await jitter(650);
    const cand = db.skills.candidates.find((c) => c.id === id);
    if (cand) cand.delta = `+${(3 + Math.random() * 4).toFixed(1)}`;
    return { ok: true, result: { id, delta: cand ? cand.delta : '+3.0', cases: 12, passed: 9 } };
  }],

  ['POST', /^\/api\/skills\/evolution\/promote$/, async (m, opts) => {
    const id = (opts.body || {}).id;
    const cand = db.skills.candidates.find((c) => c.id === id);
    if (!cand) throw mockError('候选不存在', 404);
    cand.pending = false;
    cand.promoted = true;
    db.skills.versions.unshift({
      name: cand.target,
      version: `v${db.skills.versions.length + 1}`,
      active: true,
      at: nowIso(),
      note: `${cand.name} 转正（演示）`
    });
    return { ok: true, result: { id } };
  }],

  ['POST', /^\/api\/skills\/evolution\/reject$/, async (m, opts) => {
    const id = (opts.body || {}).id;
    const cand = db.skills.candidates.find((c) => c.id === id);
    if (cand) cand.pending = false;
    return { ok: true, result: { id } };
  }],

  ['POST', /^\/api\/skills\/evolution\/rollback$/, async (m, opts) => {
    const body = opts.body || {};
    for (const v of db.skills.versions) {
      if (v.name === body.name) v.active = v.version === body.version;
    }
    return { ok: true, result: body };
  }],

  ['GET', /^\/api\/chapter$/, async (m, opts) => {
    const q = opts.query || {};
    const n = Number(q.n) || 47;
    requireBook(q.key);
    const meta = db.chapters.find((c) => c.chapterNumber === n);
    if (!meta) throw mockError('章节不存在（演示数据）', 404);
    return {
      chapter: n,
      title: meta.title,
      text: db.texts[n] != null ? db.texts[n] : `第${n}章 · ${meta.title.replace(/^第\d+章\s*/, '')}\n\n（演示数据：本章正文尚未生成。）`,
      words: meta.wordCount,
      updatedAt: nowIso(),
      versions: db.versions.length
    };
  }],

  ['PUT', /^\/api\/chapter$/, async (m, opts) => {
    const body = opts.body || {};
    const n = Number(body.n) || 47;
    requireBook(body.key);
    const text = String(body.text == null ? '' : body.text);
    if (text.length > 2000000) throw mockError('单章超过 200 万字符，请分段保存', 413);
    if (!db.texts[n]) db.versions.unshift(mockVersionItem({ id: `v-${Date.now()}`, label: '保存前快照', at: nowIso(), words: text.length }));
    db.texts[n] = text;
    const words = updateChapterWords(n, text);
    return { ok: true, chapter: n, words, updatedAt: nowIso() };
  }],

  ['GET', /^\/api\/chapter\/versions$/, async (m, opts) => {
    const q = opts.query || {};
    requireBook(q.key);
    return { chapter: Number(q.n) || 47, items: db.versions.map(mockVersionItem) };
  }],

  ['GET', /^\/api\/chapter\/version$/, async (m, opts) => {
    const q = opts.query || {};
    const n = Number(q.n) || 47;
    const ver = db.versions.find((v) => v.id === q.id);
    if (!ver) throw mockError('版本不存在（演示数据）', 404);
    return {
      chapter: n,
      id: ver.id,
      text:
        db.texts[n] ||
        `${mockChapterText}\n\n（演示：这是「${ver.label}」的历史版本，词数约 ${ver.words}。）`
    };
  }],

  ['GET', /^\/api\/chapter\/diff$/, async (m, opts) => {
    const q = opts.query || {};
    return {
      diff: [
        '@@ -2,3 +2,4 @@',
        ' 雨是后半夜落下来的。',
        '-铁皮屋顶被敲得很响。',
        '+铁皮屋顶被敲得发响，像有人在上面一粒一粒地数着什么。',
        ' 陈砚把车停在第七码头尽头。',
        '+他没有立刻下车。',
        ' 灯塔的光柱每十二秒扫过一次。'
      ].join('\n'),
      chapter: Number(q.n) || 47,
      id: q.id
    };
  }],

  ['POST', /^\/api\/chapter\/restore$/, async (m, opts) => {
    const body = opts.body || {};
    const n = Number(body.n) || 47;
    const ver = db.versions.find((v) => v.id === body.id);
    if (!ver) throw mockError('版本不存在（演示数据）', 404);
    db.versions.unshift(
      mockVersionItem({ id: `v-${Date.now()}`, label: '回滚前快照', at: nowIso(), words: (db.texts[n] || '').length })
    );
    db.texts[n] = db.texts[n] || mockChapterText;
    return { ok: true, chapter: n, words: updateChapterWords(n, db.texts[n]) };
  }],

  ['GET', /^\/api\/settings$/, async () => ({ ...clone(db.settings), mock: true })],

  ['POST', /^\/api\/settings$/, async (m, opts) => {
    const body = opts.body || {};
    if (body.quality_mode && !['off', 'report', 'auto'].includes(body.quality_mode)) {
      throw mockError('quality_mode 只能是 off/report/auto', 400);
    }
    Object.assign(db.settings, body);
    return { ...clone(db.settings), mock: true };
  }],

  ['GET', /^\/api\/providers$/, async () => providersPayload()],

  ['POST', /^\/api\/providers$/, async (m, opts) => {
    const body = opts.body || {};
    if (!String(body.name || '').trim()) throw mockError('供应商名称不能为空', 400);
    const provider = {
      id: `prov-${Date.now()}`,
      name: String(body.name).trim(),
      base_url: String(body.base_url || ''),
      models: body.models || [],
      default_model: (body.models || [])[0] || '',
      active: true,
      local: /127\.0\.0\.1|localhost/.test(String(body.base_url || '')),
      has_key: Boolean(body.api_key),
      key_display: body.api_key ? 'sk-****' : ''
    };
    db.providers.providers.push(provider);
    return { ok: true, provider: providerPublic(provider) };
  }],

  ['PUT', /^\/api\/providers\/([^/]+)$/, async (m, opts) => {
    const id = decodeURIComponent(m[1]);
    const provider = db.providers.providers.find((p) => p.id === id);
    if (!provider) throw mockError('供应商不存在', 404);
    const body = opts.body || {};
    for (const key of ['name', 'base_url', 'models', 'default_model']) {
      if (body[key] !== undefined) provider[key] = body[key];
    }
    if (body.api_key) {
      provider.has_key = true;
      provider.key_display = 'sk-****';
    }
    return { ok: true, provider: providerPublic(provider) };
  }],

  ['DELETE', /^\/api\/providers\/([^/]+)$/, async (m) => {
    const id = decodeURIComponent(m[1]);
    db.providers.providers = db.providers.providers.filter((p) => p.id !== id);
    if (db.providers.default_provider === id) {
      db.providers.default_provider = (db.providers.providers[0] || {}).id || null;
    }
    return { ok: true };
  }],

  ['POST', /^\/api\/providers\/([^/]+)\/default$/, async (m) => {
    const id = decodeURIComponent(m[1]);
    if (!db.providers.providers.some((p) => p.id === id)) throw mockError('供应商不存在', 404);
    db.providers.default_provider = id;
    return { ok: true, default_provider: id };
  }],

  ['POST', /^\/api\/providers\/([^/]+)\/active$/, async (m, opts) => {
    const id = decodeURIComponent(m[1]);
    const provider = db.providers.providers.find((p) => p.id === id);
    if (!provider) throw mockError('供应商不存在', 404);
    provider.active = Boolean((opts.body || {}).active);
    return { ok: true, provider: providerPublic(provider) };
  }],

  ['POST', /^\/api\/providers\/([^/]+)\/test$/, async () => {
    await jitter(420);
    return { ok: true, latency_ms: 268, models: 2, message: '连接正常（演示）' };
  }],

  ['POST', /^\/api\/providers\/([^/]+)\/models$/, async () => ({ ok: true, models: [], from_api: false, msg: '演示模式不请求外部供应商' })],

  ['GET', /^\/api\/model_options$/, async () => clone(mockModels)],

  ['GET', /^\/api\/mediums$/, async () => ({ items: clone(mockMediums), mediums: clone(mockMediums) })],

  ['GET', /^\/api\/learn$/, async () => clone(mockLearn)],

  ['GET', /^\/api\/styles$/, async () => ({ items: clone(mockStyles), styles: clone(mockStyles) })],

  ['POST', /^\/api\/distill$/, async (m, opts) => {
    await jitter(500);
    return {
      ok: true,
      style: {
        name: (opts.body && opts.body.name) || '新蒸馏风格',
        genre: (opts.body && opts.body.genre) || '悬疑',
        summary: '短句、低温、以物写人；避免总结式抒情（演示）。',
        created_at: nowIso(),
        fingerprint_words: ['雨', '灯', '潮']
      }
    };
  }],

  ['GET', /^\/api\/usage$/, async () => clone(db.usage)],

  ['GET', /^\/api\/titles$/, async (m, opts) => {
    const key = (opts.query || {}).key;
    return {
      key,
      candidates: [
        { title: '长夜灯河', technique: '意象对仗', why: '与灯塔、河流的核心意象一致' },
        { title: '名单上的第七个人', technique: '悬念前置', why: '直接抛出核心谜面' },
        { title: '守灯人', technique: '人物指代', why: '简洁、可系列化' },
        { title: '灯灭之前', technique: '时间压力', why: '暗示倒计时结构' }
      ],
      selected: null,
      planning_running: false,
      status: 'awaiting_title'
    };
  }],

  ['POST', /^\/api\/pick-title$/, async (m, opts) => {
    const key = (opts.body || {}).key;
    const book = findBook(key);
    if (book) {
      book.name = (opts.body || {}).title || book.name;
      book.state = 'in_progress';
      book.status_label = '创作中';
      book.running = true;
      book.updated_at = nowIso();
    }
    return { ok: true, title: (opts.body || {}).title };
  }],

  ['POST', /^\/api\/create$/, async (m, opts) => {
    await jitter(600);
    const body = opts.body || {};
    const key = `20261025-${String(Date.now()).slice(-6)}-${String(body.title || body.idea || '新作品').slice(0, 12)}`;
    const book = {
      key,
      name: body.title || '未命名新作',
      genre: body.genre || '悬疑',
      state: body.title ? 'in_progress' : 'awaiting_title',
      status_label: body.title ? '创作中' : '选书名',
      done: 0,
      total: Number(body.total_chapters) || 60,
      total_words: 0,
      running: Boolean(body.title),
      quality: null,
      passing: null,
      pass_line: 85,
      released: false,
      created_at: nowIso(),
      updated_at: nowIso(),
      favorite: false,
      tags: [],
      archived: false,
      progress: 0,
      medium: body.medium || 'novel'
    };
    db.library.unshift(book);
    return { project: key, pid: key, key, awaiting_title: !body.title, async: true, candidates: [] };
  }],

  ['POST', /^\/api\/sample$/, async () => {
    const book = clone(findBook(DEMO_KEY));
    book.key = `sample-${Date.now()}`;
    book.name = `长夜灯河（示例 ${db.library.length}）`;
    db.library.unshift(book);
    return { ok: true, key: book.key };
  }],

  ['POST', /^\/api\/continue$/, async (m, opts) => {
    const key = (opts.body || {}).key;
    const book = requireBook(key);
    book.running = true;
    book.state = 'in_progress';
    book.status_label = '创作中';
    book.updated_at = nowIso();
    return { ok: true, msg: `继续写作《${book.name}》…（演示）` };
  }],

  ['POST', /^\/api\/pause$/, async (m, opts) => {
    const book = requireBook((opts.body || {}).key);
    book.running = false;
    return { ok: true, paused: true };
  }],

  ['POST', /^\/api\/resume$/, async (m, opts) => {
    const book = requireBook((opts.body || {}).key);
    book.running = true;
    return { ok: true, paused: false };
  }],

  ['GET', /^\/api\/queue$/, async () => ({ items: [{ key: DEMO_KEY, chapter: 47, state: 'queued', note: '等待上一章完成' }] })],

  ['POST', /^\/api\/queue\/action$/, async () => ({ ok: true })],

  ['POST', /^\/api\/chat$/, async (m, opts) => {
    await jitter(500);
    const text = String((opts.body || {}).message || '');
    return {
      session_id: (opts.body || {}).session_id || `mock-${Date.now()}`,
      reply: mockAssistantReplyFor(text),
      project: null
    };
  }],

  ['POST', /^\/api\/cmd$/, async (m, opts) => {
    await jitter(260);
    const text = String((opts.body || {}).text || '');
    return {
      key: (opts.body || {}).key,
      text,
      action: 'chat',
      reply: mockAssistantReplyFor(text),
      note: '演示模式：按聊天处理'
    };
  }]
];

export async function mockRequest(path, opts = {}) {
  const method = String(opts.method || 'GET').toUpperCase();
  const bare = String(path).split('?')[0];
  await jitter(90);
  for (const [verb, pattern, handler] of handlers) {
    if (verb !== method) continue;
    const match = bare.match(pattern);
    if (match) return handler(match, opts);
  }
  throw mockError(`演示模式未实现接口：${method} ${bare}`, 404);
}

/* 令牌级流式输出模拟。handlers: {onEvent(event, data)}。 */
export function mockChatStream(payload, { onEvent, signal } = {}) {
  const text = mockAssistantReplyFor(payload && payload.message);
  const sessionId = `mock-${Date.now().toString(36)}`;
  const emit = (event, data) => {
    if (typeof onEvent === 'function') onEvent(event, data);
  };

  return new Promise((resolve, reject) => {
    let aborted = false;
    let i = 0;
    let timer = 0;

    const cleanup = () => {
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', onAbort);
    };
    const finishAbort = () => {
      aborted = true;
      cleanup();
      const err = new Error('已取消');
      err.code = 'aborted';
      reject(err);
    };
    const onAbort = () => finishAbort();
    if (signal) {
      if (signal.aborted) {
        finishAbort();
        return;
      }
      signal.addEventListener('abort', onAbort, { once: true });
    }

    const step = () => {
      if (aborted) return;
      if (i === 0) {
        emit('start', { session_id: sessionId, ts: Date.now(), mock: true });
        emit('status', { stage: 'thinking', text: '正在检索作品记忆与规划…' });
      } else if (i === 1) {
        emit('status', { stage: 'writing', text: '正在组织回复…' });
      } else {
        const chunk = text.slice(i * 9, i * 9 + 9);
        if (chunk) emit('delta', { text: chunk });
      }
      i += 1;
      if (i * 9 < text.length + 9) {
        timer = setTimeout(step, 16 + Math.random() * 22);
      } else {
        cleanup();
        const done = { ok: true, text, session_id: sessionId, elapsed: 1.6, mock: true };
        emit('done', done);
        resolve(done);
      }
    };

    timer = setTimeout(step, 120);
  });
}
