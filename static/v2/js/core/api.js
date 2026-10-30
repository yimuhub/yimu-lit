/* 后端适配层：统一 fetch、中文错误、401 通知、404/501 优雅降级，以及 ?mock=1 演示模式。
   约定：所有接口返回 JSON；错误优先读取 {error} 或 {detail}。 */

import { mockRequest, mockChatStream } from '../../mock/mock.js';

export const IS_MOCK =
  typeof location !== 'undefined' && new URLSearchParams(location.search).get('mock') === '1';

export class ApiError extends Error {
  constructor(message, { status = 0, code = '', payload = null } = {}) {
    super(message || '请求失败');
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.payload = payload;
  }

  get isAbort() {
    return this.code === 'aborted';
  }

  get isMissing() {
    return this.status === 404 || this.status === 405 || this.status === 501;
  }
}

function fieldMessage(payload, status) {
  if (payload && typeof payload === 'object') {
    if (typeof payload.error === 'string' && payload.error.trim()) return payload.error.trim();
    if (typeof payload.detail === 'string' && payload.detail.trim()) return payload.detail.trim();
    if (Array.isArray(payload.detail) && payload.detail.length) {
      const first = payload.detail[0];
      if (first && typeof first.msg === 'string') return first.msg;
    }
  }
  if (status === 401) return '登录状态已失效，请从 http://127.0.0.1 重新打开创作台';
  if (status === 403) return '没有权限执行这个操作';
  if (status === 404) return '接口或资源不存在（后端可能还是旧版本）';
  if (status === 413) return '内容或文件超过大小限制';
  if (status === 429) return '请求太频繁，请稍后再试';
  if (status >= 500) return '服务端处理失败，请查看后台日志后重试';
  return `请求失败（HTTP ${status}）`;
}

export function notifyUnauthorized() {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent('shenbi:unauthorized'));
}

function queryString(query) {
  if (!query) return '';
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue;
    params.set(key, String(value));
  }
  const s = params.toString();
  return s ? `?${s}` : '';
}

function joinUrl(path, query) {
  return `${path}${queryString(query)}`;
}

export async function request(path, opts = {}) {
  if (IS_MOCK) {
    try {
      return await mockRequest(path, opts);
    } catch (err) {
      if (err instanceof ApiError) throw err;
      throw new ApiError(err && err.message ? err.message : '演示数据请求失败', {
        status: (err && err.status) || 0,
        code: (err && err.code) || 'mock'
      });
    }
  }

  const {
    method = 'GET',
    query,
    body,
    form,
    signal,
    timeout = 45000,
    headers = {}
  } = opts;

  const ctrl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    ctrl.abort();
  }, timeout);
  const forwardAbort = () => ctrl.abort();
  if (signal) {
    if (signal.aborted) ctrl.abort();
    else signal.addEventListener('abort', forwardAbort, { once: true });
  }

  try {
    const init = {
      method,
      credentials: 'same-origin',
      headers: { Accept: 'application/json', ...headers },
      signal: ctrl.signal
    };
    if (form) {
      init.body = form;
    } else if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }

    const res = await fetch(joinUrl(path, query), init);
    const ct = res.headers.get('content-type') || '';
    let payload = null;
    if (ct.includes('application/json')) {
      payload = await res.json().catch(() => null);
    } else if (res.status !== 204) {
      payload = await res.text().catch(() => '');
    }

    if (!res.ok) {
      const err = new ApiError(fieldMessage(payload, res.status), {
        status: res.status,
        payload
      });
      if (res.status === 401) notifyUnauthorized();
      throw err;
    }
    if (payload && typeof payload === 'object' && payload.ok === false && payload.error) {
      throw new ApiError(fieldMessage(payload, 200), { status: res.status, payload });
    }
    return payload;
  } catch (err) {
    if (err instanceof ApiError) throw err;
    if (err && err.name === 'AbortError') {
      if (signal && signal.aborted) throw new ApiError('已取消', { code: 'aborted' });
      throw new ApiError(
        timedOut ? '请求超时，服务可能正在生成内容，请稍后重试' : '请求已取消',
        { code: timedOut ? 'timeout' : 'aborted' }
      );
    }
    throw new ApiError('无法连接本地服务：请确认一幕·YIMU 神笔正在运行，并从 http://127.0.0.1 打开', {
      code: 'network'
    });
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener('abort', forwardAbort);
  }
}

/* 新接口缺失（404/501）时返回兜底值，让界面优雅降级而不是报错。 */
export async function optional(promise, fallback = null) {
  try {
    return await promise;
  } catch (err) {
    if (err instanceof ApiError && err.isMissing) return fallback;
    throw err;
  }
}

function jsonBody(payload = {}) {
  return { method: 'POST', body: payload };
}

/* 通用 SSE 客户端：主对话与剧本副驾共用同一套帧解析。 */
export async function streamSSE(path, payload, { onEvent, signal } = {}) {
  if (signal && signal.aborted) throw new ApiError('已取消', { code: 'aborted' });

  let res;
  try {
    res = await fetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(payload),
      signal
    });
  } catch (err) {
    if (err && err.name === 'AbortError') throw new ApiError('已取消', { code: 'aborted' });
    throw new ApiError('无法连接流式接口，请确认服务正在运行', { code: 'network' });
  }

  if (!res.ok) {
    let payloadData = null;
    try {
      payloadData = await res.json();
    } catch {
      payloadData = null;
    }
    if (res.status === 401) notifyUnauthorized();
    const code = res.status === 404 || res.status === 501 ? 'stream_unavailable' : 'stream_error';
    throw new ApiError(fieldMessage(payloadData, res.status), { status: res.status, code, payload: payloadData });
  }

  if (!res.body || !res.body.getReader) {
    throw new ApiError('当前浏览器不支持流式输出，请升级浏览器', { code: 'stream_unsupported' });
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let final = null;

  const emitFrame = (frame) => {
    const lines = frame.split(/\r?\n/);
    let event = 'message';
    const dataLines = [];
    for (const line of lines) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return;
    const raw = dataLines.join('\n');
    let data = raw;
    try {
      data = JSON.parse(raw);
    } catch {
      /* 纯文本 delta */
    }
    if (onEvent) onEvent(event, data);
    if (event === 'done') final = data;
  };

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx = buffer.indexOf('\n\n');
      while (idx >= 0) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        if (frame.trim()) emitFrame(frame);
        idx = buffer.indexOf('\n\n');
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) emitFrame(buffer);
    return final;
  } catch (err) {
    if (err && err.name === 'AbortError') throw new ApiError('已取消', { code: 'aborted' });
    throw new ApiError('流式连接中断，请重试', { code: 'stream_broken' });
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* ignore */
    }
  }
}

function enc(value) {
  return encodeURIComponent(String(value == null ? '' : value));
}

export const api = {
  capabilities: () => request('/api/capabilities', { timeout: 8000 }),
  projects: () => request('/api/projects'),
  status: (key) => request('/api/status', { query: { key } }),
  continueWork: (key, extra = 0) => request('/api/continue', jsonBody({ key, extra })),
  pause: (key) => request('/api/pause', jsonBody({ key })),
  resume: (key) => request('/api/resume', jsonBody({ key })),
  queue: (key) => request('/api/queue', { query: { key } }),
  queueAction: (payload) => request('/api/queue/action', jsonBody(payload)),
  bookLive: (key) => request('/api/book-live', { query: { key }, timeout: 12000 }),

  library: (params = {}) => request('/api/library', { query: params }),
  /* 冷启动只有 key：用它解析作品属于哪种文体，再决定渲染哪套工作台。 */
  project: (key) => request(`/api/library/${enc(key)}`),
  favorite: (key, on) => request(`/api/library/${enc(key)}/favorite`, jsonBody({ on: Boolean(on) })),
  /* 归档 = 收进作品库「归档」页；回收站 = 可恢复删除；purge 需作品名确认，不可逆。 */
  archive: (key, on) => request(`/api/library/${enc(key)}/archive`, jsonBody({ on: Boolean(on) })),
  trash: (key) => request(`/api/library/${enc(key)}/trash`, jsonBody({})),
  restore: (key) => request(`/api/library/${enc(key)}/restore`, jsonBody({})),
  purge: (key, confirm) => request(`/api/library/${enc(key)}/purge`, { method: 'DELETE', query: { confirm } }),
  rename: (key, name) => request(`/api/library/${enc(key)}/rename`, jsonBody({ name })),
  remove: (key) => request(`/api/library/${enc(key)}`, { method: 'DELETE' }),
  batch: (keys, action) => request('/api/library/batch', jsonBody({ keys, action })),
  tags: (key, tags) => request(`/api/library/${enc(key)}/tags`, jsonBody({ tags })),
  compare: (keys) => request('/api/compare', { query: { keys: keys.join(',') } }),

  chat: (payload) => request('/api/chat', { method: 'POST', body: payload, timeout: 180000 }),
  cmd: (payload) => request('/api/cmd', { method: 'POST', body: payload, timeout: 60000 }),

  /* ---- 剧本/诗词/散文工作台（v2 原生渲染）----
     三套引擎各有接口面：剧本按文档名分路由，诗词/散文用 part 参数。 */
  scriptStatus: (key) => request('/api/script/status', { query: { key } }),
  scriptDoc: (tab, key) => request(`/api/script/${tab}`, { query: { key } }),
  scriptWrite: (key, sceneNo = 0) => request('/api/script/write', jsonBody(sceneNo ? { key, sceneNo } : { key })),
  /* 单场正文与整本预览：预览一次取回全部场次（未写完的场 text 为空串）。 */
  scriptScene: (key, no) => request('/api/script/scene', { query: { key, no } }),
  scriptPreview: (key) => request('/api/script/preview', { query: { key } }),
  scriptRevise: (key) => request('/api/script/revise', jsonBody({ key })),
  /* 世界观一致性体检 + 按设定重写违规场次（先落快照）。 */
  scriptCheck: (key) => request('/api/script/check', jsonBody({ key }), { timeout: 180000 }),
  scriptAudit: (key) => request('/api/script/audit', jsonBody({ key }), { timeout: 120000 }),
  scriptAuditGet: (key) => request('/api/script/audit', { query: { key } }),
  scriptStoryboard: (key) => request('/api/script/storyboard', { query: { key } }),
  thesisStatus: (key) => request('/api/thesis/status', { query: { key } }),
  thesisPart: (key, part) => request('/api/thesis/part', { query: { key, part } }),
  thesisRefsCheck: (key) => request('/api/thesis/refs-check', jsonBody({ key })),
  miniStatus: (key) => request('/api/mini/status', { query: { key } }),
  miniPart: (key, part) => request('/api/mini/part', { query: { key, part } }),
  aiGenerate: (payload) => request('/api/ai-generate', jsonBody(payload)),
  regenPlan: (payload) => request('/api/plan/regen', jsonBody(payload), { timeout: 30000 }),
  scriptStoryboardRun: (key, sceneNo) => request('/api/script/storyboard', jsonBody({ key, sceneNo }), { timeout: 20000 }),
  novelToDrama: (payload) => request('/api/novel-to-drama', jsonBody(payload), { timeout: 30000 }),
  scriptRewriteScenes: (key, sceneNos, instruction = '') =>
    request('/api/script/rewrite-scenes', jsonBody({ key, sceneNos, instruction, force: true }), { timeout: 600000 }),
  /* 通用副驾（剧本/诗词/散文）：讨论或定向改写；改写只回 diff，应用走 assistApply。 */
  assistStream: (payload, opts) => streamSSE('/api/assist/stream', payload, opts),
  assistApply: (payload) => request('/api/assist/apply', jsonBody(payload), { timeout: 60000 }),
  assistVersions: (key, medium, doc) =>
    request('/api/assist/versions', { query: { key, medium, doc } }),
  assistRestore: (payload) => request('/api/assist/restore', jsonBody(payload), { timeout: 60000 }),
  /* 对话历史 + 改稿记录（作品目录 .assist/ 留档） */
  assistHistory: (key, medium) => request('/api/assist/history', { query: { key, medium } }),

  /* 对话副驾：讨论或定向改写；改写只回 diff，应用走 assistApply。 */
  scriptAssistStream: (payload, opts) => streamSSE('/api/script/assist/stream', payload, opts),
  scriptAssistApply: (payload) => request('/api/script/assist/apply', jsonBody(payload), { timeout: 60000 }),
  scriptAssistVersions: (key, scene) =>
    request('/api/script/assist/versions', { query: { key, scene } }),
  scriptAssistRestore: (key, scene, version) =>
    request('/api/script/assist/restore', jsonBody({ key, scene, version }), { timeout: 60000 }),

  poetryStatus: (key) => request('/api/poetry/status', { query: { key } }),
  poetryPart: (key, part) => request('/api/poetry/part', { query: { key, part } }),
  poetryRecheck: (key) => request('/api/poetry/recheck', { ...jsonBody({ key }), timeout: 180000 }),

  proseStatus: (key) => request('/api/prose/status', { query: { key } }),
  prosePart: (key, part) => request('/api/prose/part', { query: { key, part } }),

  create: (payload) => request('/api/create', { method: 'POST', body: payload, timeout: 30000 }),
  titles: (key) => request('/api/titles', { query: { key } }),
  pickTitle: (key, title) => request('/api/pick-title', jsonBody({ key, title })),
  /* v2.3 书名改名与候选：set 返回改名后的新 key；generate 同步调 LLM（10-40 秒）。 */
  title: {
    set: (key, title) => request('/api/title/set', { ...jsonBody({ key, title }), timeout: 60000 }),
    generate: (key, n = 3) => request('/api/title/generate', { ...jsonBody({ key, n }), timeout: 120000 }),
    candidates: (key) => request('/api/title/candidates', { query: { key } })
  },
  /* 全书一致性体检：同步返回 {ok, issues[str], at, scope}。 */
  audit: (key, scope = 'book') => request('/api/audit', { ...jsonBody({ key, scope }), timeout: 120000 }),
  sample: () => request('/api/sample', jsonBody({})),

  settings: () => request('/api/settings'),
  saveSettings: (payload) => request('/api/settings', jsonBody(payload)),
  pennames: () => request('/api/pennames'),
  pennamesSave: (payload) => request('/api/pennames/save', jsonBody(payload)),
  pennamesDelete: (payload) => request('/api/pennames/delete', jsonBody(payload)),
  usage: (key = '') => request('/api/usage', { query: { key } }),
  llmCalls: (limit = 50) => request('/api/llm-calls', { query: { limit } }),

  providers: () => request('/api/providers'),
  addProvider: (payload) => request('/api/providers', jsonBody(payload)),
  updateProvider: (id, payload) => request(`/api/providers/${enc(id)}`, { method: 'PUT', body: payload }),
  removeProvider: (id) => request(`/api/providers/${enc(id)}`, { method: 'DELETE' }),
  setDefaultProvider: (id) => request(`/api/providers/${enc(id)}/default`, jsonBody({})),
  setProviderActive: (id, active) => request(`/api/providers/${enc(id)}/active`, jsonBody({ active })),
  testProvider: (id) => request(`/api/providers/${enc(id)}/test`, jsonBody({ probe_chat: false }), { timeout: 30000 }),
  fetchProviderModels: (id) => request(`/api/providers/${enc(id)}/models`, jsonBody({}), { timeout: 30000 }),
  modelOptions: () => request('/api/model_options'),

  mediums: () => request('/api/mediums'),
  learn: () => request('/api/learn'),
  styles: () => request('/api/styles'),
  distill: (payload) => request('/api/distill', jsonBody(payload), { timeout: 180000 }),
  onboarding: () => optional(request('/api/onboarding'), null),

  plan: (key) => request('/api/plan', { query: { key }, timeout: 60000 }),
  planEnsure: (key) => request('/api/plan/ensure', jsonBody({ key }), { timeout: 120000 }),
  replan: (payload) => request('/api/plan/replan', jsonBody(payload), { timeout: 180000 }),
  planMulti: (payload) => request('/api/plan/multi', jsonBody(payload), { timeout: 600000 }),
  applyPlan: (key, proposalId, allowPast = false) =>
    request('/api/plan/apply', jsonBody({ key, proposal_id: proposalId, allow_past: allowPast }), { timeout: 60000 }),
  rejectPlan: (key, proposalId, note = '') =>
    request('/api/plan/reject', jsonBody({ key, proposal_id: proposalId, note })),
  planWindow: (key, start, end) => request('/api/plan/window', jsonBody({ key, start, end }), { timeout: 180000 }),

  state: (key, q = '') => request('/api/state', { query: { key, q }, timeout: 30000 }),

  references: (key) => request('/api/references', { query: { key } }),
  addRefText: (payload) => request('/api/references/text', jsonBody(payload), { timeout: 120000 }),
  addRefUrl: (payload) => request('/api/references/url', jsonBody(payload), { timeout: 120000 }),
  uploadRef: (form) => request('/api/references/upload', { method: 'POST', form, timeout: 180000 }),
  patchRef: (id, payload) => request(`/api/references/${enc(id)}`, { method: 'PATCH', body: payload }),
  deleteRef: (id) => request(`/api/references/${enc(id)}`, { method: 'DELETE' }),
  analyzeRef: (id) => request(`/api/references/${enc(id)}/analyze`, jsonBody({}), { timeout: 180000 }),
  refChunks: (id, offset = 0, limit = 20) =>
    request(`/api/references/${enc(id)}/chunks`, { query: { offset, limit } }),
  searchRefs: (key, q, k = 6) => request('/api/references/search', { query: { key, q, k } }),
  linkRef: (key, refId, usageMode = 'facts') =>
    request('/api/references/link', jsonBody({ key, ref_id: refId, usage_mode: usageMode })),
  unlinkRef: (key, refId) => request('/api/references/unlink', jsonBody({ key, ref_id: refId })),
  refOverlap: (key, text) => request('/api/references/overlap', jsonBody({ key, text }), { timeout: 60000 }),

  feedback: (payload) => request('/api/feedback', jsonBody(payload)),
  feedbackList: (params = {}) => request('/api/feedback', { query: params }),
  skillsEvolution: (status = '') => request('/api/skills/evolution', { query: { status } }),
  proposeSkill: (payload) => request('/api/skills/evolution/propose', jsonBody(payload), { timeout: 180000 }),
  evaluateSkill: (payload) => request('/api/skills/evolution/evaluate', jsonBody(payload), { timeout: 300000 }),
  promoteSkill: (id, force = false) =>
    request('/api/skills/evolution/promote', jsonBody({ id, force, approver: 'user' })),
  rejectSkill: (id, note = '') => request('/api/skills/evolution/reject', jsonBody({ id, note })),
  rollbackSkill: (name, version) => request('/api/skills/evolution/rollback', jsonBody({ name, version })),
  toggleSkill: (payload) => request('/api/skills/evolution/toggle', jsonBody(payload)),
  fixConsistency: (issueId, key) => request('/api/fix-consistency', jsonBody({ id: issueId, key })),
  branchBook: (key, chapter) => request('/api/branch', jsonBody({ key, chapter }), { timeout: 60000 }),
  chapterNamesBackfill: (key) => request('/api/chapter-names/backfill', jsonBody({ key }), { timeout: 20000 }),
  bookSkills: (key) => request('/api/book-skills', { query: { key } }),
  setBookSkills: (payload) => request('/api/book-skills', jsonBody(payload)),
  chatHistory: (key) => request(`/api/chat/history?key=${encodeURIComponent(key)}`),
  chatClear: (key) => request('/api/chat/clear', jsonBody({ key })),
  addAgent: (data) => request('/api/agents/add', jsonBody(data)),
  scoreBook: (key) => request('/api/score', jsonBody({ key }), { timeout: 120000 }),

  /* 技能库（#/skills）：树、单文件、统计、本书命中与注入预览 */
  skillkitTree: (medium = '') => request('/api/skills/tree', { query: { medium } }),
  skillkitFile: (relpath) => request('/api/skills/file', { query: { path: relpath } }),
  skillkitStats: () => request('/api/skillkit/stats'),
  skillsApplicable: (key) => request('/api/skills/applicable', { query: { key } }),
  skillkitAdd: (payload) => request('/api/skillkit/add', jsonBody(payload), { timeout: 60000 }),
  skillkitDelete: (name) => request(`/api/skillkit/custom/${enc(name)}`, { method: 'DELETE' }),

  /* 多 Agent：岗位×模型×技能绑定 + 单书并行创作 */
  agents: () => request('/api/agents'),
  agentsSet: (payload) => request('/api/agents-set', jsonBody(payload)),
  agentsMaxGlobal: (n) => request('/api/agents-maxglobal', jsonBody({ n })),
  agentsSkills: (payload) => request('/api/agents-skills', jsonBody(payload)),
  parallelWrite: (payload) => request('/api/parallel-write', jsonBody(payload), { timeout: 60000 }),
  parallelLog: (key) => request('/api/parallel-log', { query: { key } }),
  jobs: (key = '') => request('/api/jobs', { query: { key } }),

  chapter: (key, n) => request('/api/chapter', { query: { key, n } }),
  saveChapter: (key, n, text) => request('/api/chapter', { method: 'PUT', body: { key, n, text }, timeout: 60000 }),
  chapterVersions: (key, n) => request('/api/chapter/versions', { query: { key, n } }),
  chapterVersion: (key, n, id) => request('/api/chapter/version', { query: { key, n, id } }),
  chapterDiff: (key, n, id) => request('/api/chapter/diff', { query: { key, n, id } }),
  restoreChapter: (key, n, id) => request('/api/chapter/restore', jsonBody({ key, n, id }), { timeout: 60000 }),

  exportUrl(key, fmt = 'docx') {
    return joinUrl('/api/export', { key, fmt });
  },

  /* 版本与更新检查 */
  version: () => request('/api/version'),
  checkUpdate: () => request('/api/check-update', { timeout: 12000 }),

  /* 聊天 SSE：mock 模式模拟分段输出；真实模式走通用 streamSSE。 */
  async streamChat(payload, { onEvent, signal } = {}) {
    if (IS_MOCK) {
      try {
        return await mockChatStream(payload, { onEvent, signal });
      } catch (err) {
        if (err && err.code === 'aborted') throw new ApiError('已取消', { code: 'aborted' });
        throw new ApiError((err && err.message) || '演示流式输出失败', { code: 'mock' });
      }
    }
    return streamSSE('/api/chat/stream', payload, { onEvent, signal });
  }
};

export function errorText(err, fallback = '操作失败，请稍后重试') {
  if (!err) return fallback;
  if (err instanceof ApiError) return err.message || fallback;
  if (typeof err === 'string') return err;
  return err.message || fallback;
}
