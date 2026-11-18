/* 按需加载样式表：路由级视图（settings/skills/chatfloat 等）的 CSS 不再
   常驻 index.html，进入对应视图时补上；重复调用幂等。 */

const loaded = new Set();

export function loadCss(href) {
  if (loaded.has(href)) return;
  loaded.add(href);
  /* 同一 href 可能由别的入口（旧页面/手写标签）已经注入过，先查一遍再插入。 */
  if (document.querySelector(`link[rel="stylesheet"][href="${CSS.escape(href)}"]`)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = href;
  document.head.append(link);
}
