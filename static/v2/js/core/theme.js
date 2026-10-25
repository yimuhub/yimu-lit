/* 主题：light / dark / system，CSS 变量 + data-theme，记忆用户选择。 */

import { store } from './state.js';

const KEY = 'shenbi.v2.theme';
const VALID = ['light', 'dark', 'system'];
const media = typeof window !== 'undefined' && window.matchMedia
  ? window.matchMedia('(prefers-color-scheme: dark)')
  : null;

function readPref() {
  try {
    const v = localStorage.getItem(KEY);
    return VALID.includes(v) ? v : 'system';
  } catch {
    return 'system';
  }
}

function resolve(pref) {
  if (pref === 'system') return media && media.matches ? 'dark' : 'light';
  return pref;
}

function paint(pref) {
  const resolved = resolve(pref);
  const root = document.documentElement;
  root.dataset.theme = resolved;
  root.dataset.themePreference = pref;
  root.style.colorScheme = resolved;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', resolved === 'dark' ? '#151517' : '#ffffff');
  store.set((state) => ({ ...state, theme: pref, resolvedTheme: resolved }));
}

export function setTheme(pref) {
  const next = VALID.includes(pref) ? pref : 'system';
  try {
    localStorage.setItem(KEY, next);
  } catch {
    /* storage may be unavailable */
  }
  paint(next);
}

export function getTheme() {
  return store.get().theme;
}

export function toggleTheme() {
  const order = ['light', 'dark', 'system'];
  const current = store.get().theme;
  setTheme(order[(order.indexOf(current) + 1) % order.length]);
  return store.get().theme;
}

export function themeLabel(pref) {
  return { light: '浅色', dark: '深色', system: '跟随系统' }[pref] || '跟随系统';
}

export function initTheme() {
  paint(readPref());
  if (media) {
    const onChange = () => {
      if (store.get().theme === 'system') paint('system');
    };
    if (media.addEventListener) media.addEventListener('change', onChange);
    else if (media.addListener) media.addListener(onChange);
  }
}
