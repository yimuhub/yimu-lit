/* Hash 路由：#/welcome、#/work/<key>、#/work/<key>/bench/<n>、#/library、#/skills、#/settings、#/evolution */

const ROUTES = ['welcome', 'work', 'library', 'skills', 'evolution', 'settings'];

export function parseHash(hash = location.hash) {
  const raw = String(hash || '').replace(/^#\/?/, '');
  const [pathPart, queryPart = ''] = raw.split('?');
  const parts = pathPart.split('/').filter(Boolean);
  const query = Object.fromEntries(new URLSearchParams(queryPart));

  if (!parts.length || parts[0] === 'welcome') return { name: 'welcome', key: '', chapter: null, query };
  if (parts[0] === 'library') return { name: 'library', key: '', chapter: null, query };
  if (parts[0] === 'skills') return { name: 'skills', key: '', chapter: null, query };
  if (parts[0] === 'evolution') return { name: 'evolution', key: '', chapter: null, query };
  if (parts[0] === 'settings') return { name: 'settings', key: '', chapter: null, query };
  if (parts[0] === 'work' && parts[1]) {
    const key = decodeURIComponent(parts[1]);
    if (parts[2] === 'bench') {
      const n = Number(parts[3]) || 1;
      return { name: 'bench', key, chapter: n, query };
    }
    return { name: 'work', key, chapter: null, query };
  }
  return { name: 'welcome', key: '', chapter: null, query };
}

export function routePath(name, opts = {}) {
  if (name === 'work' && opts.key) return `/work/${encodeURIComponent(opts.key)}`;
  if (name === 'bench' && opts.key) return `/work/${encodeURIComponent(opts.key)}/bench/${opts.chapter || 1}`;
  if (ROUTES.includes(name) && name !== 'work' && name !== 'bench') return `/${name}`;
  return '/welcome';
}

export function navigate(path, { replace = false } = {}) {
  const next = String(path).startsWith('#') ? String(path) : `#${path.startsWith('/') ? path : '/' + path}`;
  if (location.hash === next) return;
  if (replace) history.replaceState(null, '', next);
  else location.hash = next;
}

/* 四种文体都走 #/work/<key>：小说由对话视图渲染，其余文体由 app.js 按文体换成各自的工作台视图。 */
export function openProject(item) {
  if (!item || !item.key) return;
  navigate(`/work/${encodeURIComponent(item.key)}`);
}

export function startRouter(onRoute) {
  const emit = () => {
    const route = parseHash();
    onRoute(route);
  };
  window.addEventListener('hashchange', emit);
  if (!location.hash) {
    history.replaceState(null, '', '#/welcome');
  }
  emit();
  return emit;
}
