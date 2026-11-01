/* 小型订阅式 store。所有跨组件状态集中在此，避免散乱 DOM 操作。 */

export function createStore(initial) {
  let state = initial;
  const subscribers = new Set();

  return {
    get() {
      return state;
    },
    set(patch) {
      const next = typeof patch === 'function' ? patch(state) : { ...state, ...patch };
      if (!next || next === state) return;
      state = next;
      for (const fn of subscribers) {
        try {
          fn(state);
        } catch (err) {
          console.error('[store] subscriber failed', err);
        }
      }
    },
    subscribe(fn) {
      subscribers.add(fn);
      return () => subscribers.delete(fn);
    }
  };
}

const MOCK =
  typeof location !== 'undefined' &&
  new URLSearchParams(location.search).get('mock') === '1';

const initialChat = () => ({
  messages: [],
  streaming: false,
  phase: '',
  process: null,
  queue: [],
  approval: null,
  goal: null,
  error: null,
  sessionId: '',
  stopRequested: false,
  lastUserText: ''
});

/* v4.21 创作活动流：steps 存当前打开书的增量步骤（cursor 供轮询续传），
   stream 存进行中的一段实时输出（offset 供轮询续传，text 为本地累积缓冲，
   epoch 标识第几轮流式，doneAt 标记收尾时刻供完成卡展示）。 */
const initialSteps = () => ({ baselined: false, cursor: 0, items: [] });

const initialStream = () => ({
  key: '',
  active: false,
  doneAt: 0,
  epoch: 0,
  agent: '',
  agent_label: '',
  title: '',
  chapter: 0,
  offset: 0,
  text: '',
  thinkOffset: 0,
  thinking: ''
});

export const initialState = {
  booted: false,
  mock: MOCK,
  authRequired: false,
  route: { name: 'welcome', key: '', chapter: null },
  theme: 'system',
  resolvedTheme: 'light',

  projects: [],
  projectsLoaded: false,
  library: { items: [], stats: {}, facets: {}, loading: false, loaded: false, error: null },

  models: { default_provider: null, options: [] },
  mediums: [],
  styles: [],
  metaLoaded: false,

  settings: null,
  settingsError: null,
  providers: null,

  currentKey: '',
  workMedium: '',
  work: null,
  workError: null,
  plan: null,
  planError: null,
  lore: null,
  loreError: null,
  refs: [],
  refsError: null,
  skills: null,
  skillsError: null,

  features: {
    plan: null,
    state: null,
    references: null,
    skills: null,
    chatStream: null
  },

  chat: initialChat(),

  steps: initialSteps(),
  stream: initialStream(),

  live: [],
  liveConnected: false,

  ui: {
    sidebarWidth: 280,
    sidebarMode: 'expanded',
    sidebarExpanded: true,
    drawerOpen: false,
    rightOpen: false,
    rightMode: 'docked',
    rightTab: 'work',
    rightWidth: 0,
    commandOpen: false,
    wizardOpen: false,
    settingsOpen: false,
    feedbackOpen: false,
    benchChapter: null,
    search: '',
    providerId: null,
    model: null,
    medium: 'novel',
    wizardPreset: null,
    mode: 'draft'
  }
};

export const store = createStore(initialState);

/* 调试出口（只读使用）：控制台可 window.__store.get() 检查状态。 */
if (typeof window !== 'undefined') window.__store = store;

/* 创作活动流（steps/stream）只属于当前打开的书：currentKey 变化（进入 / 离开 /
   切换作品）即清空——与 app.js onRoute 里 work/live 等字段的重置保持同一时机。 */
let liveFlowKey = initialState.currentKey;
let liveFlowResetting = false;
store.subscribe((state) => {
  if (liveFlowResetting || state.currentKey === liveFlowKey) return;
  liveFlowKey = state.currentKey;
  const stepsIdle =
    !state.steps || (!state.steps.baselined && !state.steps.cursor && !(state.steps.items || []).length);
  const streamIdle =
    !state.stream ||
    (!state.stream.active && !state.stream.offset && !state.stream.text && !state.stream.epoch);
  if (stepsIdle && streamIdle) return;
  liveFlowResetting = true;
  try {
    store.set({ steps: initialSteps(), stream: initialStream() });
  } finally {
    liveFlowResetting = false;
  }
});

export function patch(key, value) {
  store.set((state) => {
    const base = state[key];
    const next =
      typeof value === 'function'
        ? value(base)
        : base && typeof base === 'object' && !Array.isArray(base)
          ? { ...base, ...value }
          : value;
    return { ...state, [key]: next };
  });
}

export function patchUi(patchValue) {
  patch('ui', patchValue);
}

export function patchChat(patchValue) {
  patch('chat', patchValue);
}

export function resetChat() {
  store.set((state) => ({ ...state, chat: initialChat() }));
}

/* chat message helpers */

export function addMessage(message) {
  const msg = {
    id: message.id || `m-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
    role: 'assistant',
    content: '',
    created_at: new Date().toISOString(),
    ...message
  };
  patchChat((chat) => ({ messages: [...chat.messages, msg] }));
  return msg;
}

export function updateMessage(id, patchValue) {
  patchChat((chat) => ({
    messages: chat.messages.map((m) =>
      m.id === id ? { ...m, ...(typeof patchValue === 'function' ? patchValue(m) : patchValue) } : m
    )
  }));
}

export function removeMessage(id) {
  patchChat((chat) => ({ messages: chat.messages.filter((m) => m.id !== id) }));
}

