/* 内联 SVG 图标集。全部 24×24、1.6 描边，跟随 currentColor。 */

const PATHS = {
  quill:
    '<path d="M19.5 4.5c-4.4.5-8.1 2-10.7 4.9-2.4 2.7-3.7 6.2-4.3 9.8 3.6-.6 7.1-1.9 9.8-4.3 2.9-2.6 4.4-6.3 4.9-10.7Z"/><path d="M4.8 19.2 11.6 12.4"/><path d="M14.2 9.8a1.9 1.9 0 1 0 2.7-2.7"/>',
  pen: '<path d="m15.5 4.5 4 4L8 20H4v-4Z"/><path d="m13.5 6.5 4 4"/>',
  film: '<rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="M7.5 5v14M16.5 5v14M3.5 9.5h4M3.5 14.5h4M16.5 9.5h4M16.5 14.5h4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="6"/><path d="m15.5 15.5 3.5 3.5"/>',
  book:
    '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H10a2 2 0 0 1 2 2v13a1.6 1.6 0 0 0-1.6-1.6H5.5A1.5 1.5 0 0 1 4 15.9Z"/><path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H14a2 2 0 0 0-2 2v13a1.6 1.6 0 0 1 1.6-1.6h4.9a1.5 1.5 0 0 0 1.5-1.5Z"/>',
  library:
    '<path d="M4 5.5h5v14H4zM10.5 5.5h4v14h-4zM16.6 5.8l3.1-.8 3 13.4-3.1.8z"/>',
  sparkles:
    '<path d="M12 4.5 13.4 9l4.5 1.4-4.5 1.4L12 16.3l-1.4-4.5L6.1 10.4 10.6 9Z"/><path d="M18 15.5l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7ZM5.5 3.5l.5 1.4 1.4.5-1.4.5-.5 1.4-.5-1.4L3.6 5.4 5 4.9Z"/>',
  settings:
    '<circle cx="12" cy="12" r="2.7"/><path d="M19.4 14.4a1.7 1.7 0 0 0 .3 1.9l.1.1-2.4 2.4-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-3.4V20a1.7 1.7 0 0 0-1.1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1-2.4-2.4.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 4.3 13H4v-3.4h.3a1.7 1.7 0 0 0 1.6-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1 2.4-2.4.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 11 3v-.4h3.4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1 2.4 2.4-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2V13H20a1.7 1.7 0 0 0-1.6 1.4Z"/>',
  command:
    '<path d="M9 6a2.5 2.5 0 1 0-2.5 2.5H18a2.5 2.5 0 1 0-2.5-2.5v12A2.5 2.5 0 1 0 18 15.5H6.5A2.5 2.5 0 1 0 9 18Z"/>',
  "panel-right":
    '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M14.5 4.5v15"/>',
  "panel-left":
    '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M9.5 4.5v15"/>',
  "chevron-down": '<path d="m7 10 5 5 5-5"/>',
  "chevron-right": '<path d="m10 7 5 5-5 5"/>',
  "chevron-left": '<path d="m14 7-5 5 5 5"/>',
  "chevron-up": '<path d="m7 14 5-5 5 5"/>',
  "arrow-up": '<path d="M12 19V5M6 11l6-6 6 6"/>',
  "arrow-left": '<path d="M19 12H5m6-6-6 6 6 6"/>',
  "arrow-right": '<path d="M5 12h14m-6-6 6 6-6 6"/>',
  sun:
    '<circle cx="12" cy="12" r="3.6"/><path d="M12 3v1.8M12 19.2V21M3 12h1.8M19.2 12H21M5.6 5.6l1.3 1.3M17.1 17.1l1.3 1.3M18.4 5.6l-1.3 1.3M6.9 17.1l-1.3 1.3"/>',
  moon: '<path d="M19.5 14.2A7.6 7.6 0 0 1 9.8 4.5a7.6 7.6 0 1 0 9.7 9.7Z"/>',
  monitor: '<rect x="3.5" y="4.5" width="17" height="12" rx="2"/><path d="M9 20h6M12 16.5V20"/>',
  send: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="1.5"/>',
  paperclip:
    '<path d="m20 11.5-7.6 7.6a5 5 0 0 1-7-7l8-8a3.3 3.3 0 0 1 4.7 4.7l-8 8a1.7 1.7 0 0 1-2.4-2.4l7.3-7.3"/>',
  x: '<path d="m6 6 12 12M18 6 6 18"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  more: '<circle cx="6" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="18" cy="12" r="1"/>',
  copy:
    '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 5.5A2 2 0 0 0 13.5 4h-8a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2"/>',
  refresh:
    '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5V9H15"/>',
  "rotate-ccw":
    '<path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3"/><path d="M4.5 4.5V9H9"/>',
  branch:
    '<circle cx="6.5" cy="6.5" r="2.2"/><circle cx="6.5" cy="17.5" r="2.2"/><circle cx="17.5" cy="9" r="2.2"/><path d="M6.5 8.7v6.6M8.7 6.8h4.2A4.6 4.6 0 0 1 17.5 11v0"/>',
  star:
    '<path d="m12 4 2.4 4.9 5.4.8-3.9 3.8.9 5.4-4.8-2.5-4.8 2.5.9-5.4L4.2 9.7l5.4-.8Z"/>',
  flag: '<path d="M5.5 21V4.5c3.5-1.6 6.5 1.6 10 0v9c-3.5 1.6-6.5-1.6-10 0"/>',
  "thumbs-up":
    '<path d="M7.5 20V10.5l4-6.5a2 2 0 0 1 3.6 1.5L14.5 9h4A2 2 0 0 1 20.5 11l-1.2 6.5a2.5 2.5 0 0 1-2.4 2H7.5Z"/><path d="M7.5 10.5H4v9.5h3.5"/>',
  "thumbs-down":
    '<path d="M16.5 4v9.5l-4 6.5a2 2 0 0 1-3.6-1.5l.6-3.5h-4A2 2 0 0 1 3.5 13l1.2-6.5A2.5 2.5 0 0 1 7.1 4.5h9.4Z"/><path d="M16.5 13.5H20V4h-3.5"/>',
  edit: '<path d="M4.5 19.5h4l10-10a2.1 2.1 0 0 0-3-3l-10 10Z"/><path d="m13.5 7.5 3 3"/>',
  trash:
    '<path d="M4.5 7h15M9.5 4h5M6.5 7l.8 13h9.4l.8-13M10 11v5.5M14 11v5.5"/>',
  archive:
    '<rect x="3.5" y="4.5" width="17" height="4" rx="1"/><path d="M5 8.5v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-10M10 12h4"/>',
  folder:
    '<path d="M3.5 7.5A1.5 1.5 0 0 1 5 6h4l2 2.5h8A1.5 1.5 0 0 1 20.5 10v7.5A1.5 1.5 0 0 1 19 19H5a1.5 1.5 0 0 1-1.5-1.5Z"/>',
  file:
    '<path d="M6 3.5h7l5 5v12H6Z"/><path d="M13 3.5v5h5"/>',
  link:
    '<path d="M10 13.5a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1.6 1.6"/><path d="M14 10.5a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.6-1.6"/>',
  wand:
    '<path d="M4.5 19.5 15 9M14 5V3M19 8h2M17.5 4l1.4-1.4M20 12l1.4 1.4"/><path d="m14 5 2 5 5 2-5 2-2 5-2-5-5-2 5-2Z"/>',
  list: '<path d="M8.5 6.5h11M8.5 12h11M8.5 17.5h11"/><circle cx="4.5" cy="6.5" r=".8"/><circle cx="4.5" cy="12" r=".8"/><circle cx="4.5" cy="17.5" r=".8"/>',
  grid: '<rect x="4" y="4" width="6.5" height="6.5" rx="1.4"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.4"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.4"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.4"/>',
  lock: '<rect x="5" y="10.5" width="14" height="9.5" rx="2"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
  alert: '<path d="M12 4.5 21 20H3Z"/><path d="M12 10v4M12 17v.2"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.7v.2"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  play: '<path d="M8 5.5v13l10-6.5Z"/>',
  pause: '<path d="M9 5.5v13M15 5.5v13"/>',
  target:
    '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4.2"/><circle cx="12" cy="12" r=".8"/>',
  users:
    '<circle cx="9" cy="8.5" r="3"/><path d="M3.5 19.5c0-3 2.5-5 5.5-5s5.5 2 5.5 5"/><path d="M15.5 6a3 3 0 0 1 0 5.5M17 14.7c2.2.5 3.5 2.3 3.5 4.8"/>',
  pin: '<path d="M12 21s6-5.2 6-10.5A6 6 0 0 0 6 10.5C6 15.8 12 21 12 21Z"/><circle cx="12" cy="10.5" r="2.2"/>',
  package:
    '<path d="m12 3 8 4.2v9.6L12 21l-8-4.2V7.2Z"/><path d="m4 7.2 8 4.2 8-4.2M12 11.4V21"/>',
  bookmark:
    '<path d="M6.5 4.5h11v16L12 16.6 6.5 20.5Z"/>',
  download: '<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>',
  upload: '<path d="M12 16V5M7 9.5l5-5 5 5M5 20h14"/>',
  history: '<path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3"/><path d="M4.5 4.5V9H9M12 8v4.5l3 1.8"/>',
  diff: '<path d="M6 4v16M18 4v16M3 8h6M15 16h6M3 16h4M17 8h4"/>',
  eye: '<path d="M2.5 12s3.6-6 9.5-6 9.5 6 9.5 6-3.6 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.8"/>',
  message:
    '<path d="M4 5.5h16v11H12l-5 4v-4H4Z"/>',
  "book-open":
    '<path d="M12 6.5C10 5 7 4.5 4 5v13c3-.5 6 0 8 1.5 2-1.5 5-2 8-1.5V5c-3-.5-6 0-8 1.5Z"/><path d="M12 6.5V19.5"/>',
  feather: '<path d="M19.5 4.5c-4 0-8 1.5-10.5 4S5 14.5 5 19.5c5 0 8.5-1.5 11-4s4-7 3.5-11Z"/><path d="M4.5 19.5 15 9"/>',
  plug: '<path d="M9 3v5M15 3v5M6.5 8h11v3.5a5.5 5.5 0 0 1-11 0Z"/><path d="M12 17v4"/>',
  cpu: '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/><path d="M9.5 3v3.5M14.5 3v3.5M9.5 17.5V21M14.5 17.5V21M3 9.5h3.5M3 14.5h3.5M17.5 9.5H21M17.5 14.5H21"/>',
  database:
    '<ellipse cx="12" cy="6" rx="7.5" ry="2.8"/><path d="M4.5 6v12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8V6M4.5 12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8"/>',
  shield: '<path d="M12 3.5 19.5 6v6c0 4.5-3 7.6-7.5 9-4.5-1.4-7.5-4.5-7.5-9V6Z"/><path d="m9 12 2 2 4-4"/>',
  layers:
    '<path d="m12 3.5 8.5 4.5-8.5 4.5L3.5 8Z"/><path d="m3.5 12.5 8.5 4.5 8.5-4.5M3.5 16.5 12 21l8.5-4.5"/>',
  activity: '<path d="M3 12h4l2.5-6 4 12L16 12h5"/>',
  zap: '<path d="M13 3 5 14h6l-1 7 8-11h-6Z"/>',
  calendar:
    '<rect x="3.5" y="5" width="17" height="15.5" rx="2"/><path d="M3.5 9.5h17M8 3v4M16 3v4"/>',
  globe:
    '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.4 2.4 3.6 5.3 3.6 8.5S14.4 18.1 12 20.5c-2.4-2.4-3.6-5.3-3.6-8.5S9.6 5.9 12 3.5Z"/>',
  type: '<path d="M5 6.5V4.5h14v2M12 4.5V20M8.5 20h7"/>',
  palette:
    '<path d="M12 3.5a8.5 8.5 0 0 0 0 17c1.4 0 2-1 2-2 0-1.4.9-2 2-2h1.5a3 3 0 0 0 3-3A8.5 8.5 0 0 0 12 3.5Z"/><circle cx="8" cy="10" r="1"/><circle cx="12" cy="7.5" r="1"/><circle cx="16" cy="10" r="1"/>',
  sliders:
    '<path d="M4 7h10M18 7h2M4 12h4M12 12h8M4 17h12M20 17h0"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="17" r="2"/>',
  terminal: '<path d="m5 7 5 5-5 5M12.5 17H19"/>',
  key: '<circle cx="8" cy="12" r="3.5"/><path d="M11.5 12H20M17 12v3M20 12v2"/>',
  bell: '<path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15Z"/><path d="M10 19.5a2 2 0 0 0 4 0"/>',
  "log-out": '<path d="M9.5 4.5H5v15h4.5M14 8l4 4-4 4M7.5 12H18"/>',
  maximize: '<path d="M4.5 9V4.5H9M15 4.5h4.5V9M19.5 15v4.5H15M9 19.5H4.5V15"/>',
  "file-text":
    '<path d="M6 3.5h7l5 5v12H6Z"/><path d="M13 3.5v5h5M9 12h6M9 15.5h6"/>',
  "git-commit":
    '<circle cx="12" cy="12" r="3"/><path d="M3.5 12h5.5M15 12h5.5"/>',
  filter: '<path d="M4 5.5h16l-6.2 7.3V19l-3.6-2v-4.2Z"/>',
  sort: '<path d="M7 4.5v15M4 7.5l3-3 3 3M17 19.5v-15M14 16.5l3 3 3-3"/>',
  sun_off: '<path d="M4 4l16 16"/>',
  stop_circle: '<circle cx="12" cy="12" r="8.5"/><rect x="9" y="9" width="6" height="6" rx="1"/>',
  "arrow-down": '<path d="M12 5v14m-6-6 6 6 6-6"/>',
  "external-link": '<path d="M14 4.5h5.5V10M19 5l-8 8M18 14v5.5H4.5V6H10"/>',
  "corner-down-left": '<path d="M20 5v6a4 4 0 0 1-4 4H4m5-5-5 5 5 5"/>',
  "more-horizontal": '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  "volume": '<path d="M4 9.5h3l5-4v13l-5-4H4ZM16 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11"/>',
  dot: '<circle cx="12" cy="12" r="2"/>'
};

const SVG_NS = 'http://www.w3.org/2000/svg';

export function icon(name, size = 16, cls = '') {
  const body = PATHS[name] || PATHS.dot;
  return `<svg class="icon${cls ? ' ' + cls : ''}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${body}</svg>`;
}

export function iconEl(name, size = 16, cls = '') {
  const wrap = document.createElement('span');
  wrap.innerHTML = icon(name, size, cls);
  return wrap.firstElementChild;
}

export { SVG_NS };
