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

export function setFeature(name, available) {
  store.set((state) => ({ ...state, features: { ...state.features, [name]: available } }));
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

export function setMessages(messages) {
  patchChat({ messages });
}

export const selectors = {
  activeWork: (s) => (s.currentKey ? s.library.items.find((i) => i.key === s.currentKey) || null : null),
  isWide: () => typeof window !== 'undefined' && window.innerWidth >= 1200
};
