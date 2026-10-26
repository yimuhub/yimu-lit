/* 本机界面偏好（不落后端配置）：localStorage 读写 + store 同步。 */

import { store, patchUi } from './state.js';

const KEY = 'shenbi.v2.ui';
const DEFAULTS = { showClassic: false };

function read() {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || '{}');
    return raw && typeof raw === 'object' ? raw : {};
  } catch {
    return {};
  }
}

function write(state) {
  try {
    localStorage.setItem(KEY, JSON.stringify({ showClassic: !!state.showClassic }));
  } catch {
    /* 隐私模式或存储不可用：偏好只活在本次会话 */
  }
}

/* 启动时调用一次，把本机偏好灌进 store。 */
export function loadPrefs() {
  const saved = read();
  patchUi({ showClassic: saved.showClassic === true });
}

export function setPref(name, value) {
  const next = { ...DEFAULTS, ...read(), [name]: !!value };
  write(next);
  patchUi({ [name]: next[name] });
}
