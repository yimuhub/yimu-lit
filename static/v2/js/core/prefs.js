/* 本机界面偏好（不落后端配置）：localStorage 读写 + store 同步。 */

import { patchUi } from './state.js';

const KEY = 'shenbi.v2.ui';
const DEFAULTS = { showClassic: false, sidebarWidth: 280, rightWidth: 0, sidebarExpanded: true };

function read() {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || '{}');
    return raw && typeof raw === 'object' ? raw : {};
  } catch {
    return {};
  }
}

function num(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

function write(state) {
  try {
    localStorage.setItem(
      KEY,
      JSON.stringify({
        showClassic: !!state.showClassic,
        sidebarWidth: num(state.sidebarWidth, DEFAULTS.sidebarWidth),
        rightWidth: num(state.rightWidth, DEFAULTS.rightWidth),
        sidebarExpanded: state.sidebarExpanded !== false
      })
    );
  } catch {
    /* 隐私模式或存储不可用：偏好只活在本次会话 */
  }
}

/* 启动时调用一次，把本机偏好灌进 store。 */
export function loadPrefs() {
  const saved = read();
  patchUi({
    showClassic: saved.showClassic === true,
    sidebarWidth: num(saved.sidebarWidth, DEFAULTS.sidebarWidth),
    rightWidth: num(saved.rightWidth, DEFAULTS.rightWidth),
    sidebarExpanded: saved.sidebarExpanded !== false
  });
}

/* 分栏宽度与折叠态整体落盘；拖拽只在 pointerup 调一次，避免每帧写 localStorage。 */
export function persistLayout(ui) {
  write({ ...read(), ...ui });
}

export function setPref(name, value) {
  const next = { ...DEFAULTS, ...read(), [name]: !!value };
  write(next);
  patchUi({ [name]: next[name] });
}
