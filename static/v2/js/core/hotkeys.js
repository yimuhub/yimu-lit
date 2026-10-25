/* 全局快捷键：⌘/Ctrl+K 命令面板、⌘/Ctrl+B 侧栏、⌘/Ctrl+J 工作台、N 新建、/ 聚焦输入。 */

export function isEditable(el) {
  if (!el || !(el instanceof Element)) return false;
  const tag = el.tagName;
  return (
    tag === 'INPUT' ||
    tag === 'TEXTAREA' ||
    tag === 'SELECT' ||
    el.isContentEditable ||
    el.getAttribute('role') === 'textbox'
  );
}

export function installHotkeys(handlers = {}) {
  const onKey = (event) => {
    const mod = event.metaKey || event.ctrlKey;
    const key = event.key.toLowerCase();
    const editable = isEditable(document.activeElement);

    if (mod && key === 'k') {
      event.preventDefault();
      if (handlers.onPalette) handlers.onPalette();
      return;
    }
    if (mod && key === 'b') {
      event.preventDefault();
      if (handlers.onToggleSidebar) handlers.onToggleSidebar();
      return;
    }
    if (mod && key === 'j') {
      event.preventDefault();
      if (handlers.onToggleRight) handlers.onToggleRight();
      return;
    }
    if (event.key === 'Escape') {
      if (handlers.onEscape) handlers.onEscape();
      return;
    }
    if (editable || mod || event.altKey) return;
    if (key === 'n') {
      event.preventDefault();
      if (handlers.onNewWork) handlers.onNewWork();
    } else if (event.key === '/') {
      event.preventDefault();
      if (handlers.onFocusComposer) handlers.onFocusComposer();
    }
  };

  window.addEventListener('keydown', onKey);
  return () => window.removeEventListener('keydown', onKey);
}
