/* SSE 连接与流式聊天：EventSource 断线指数退避、页面隐藏时暂停；mock 模式本地模拟。 */

import { api, IS_MOCK } from './api.js';

export function streamChat(payload, handlers = {}, opts = {}) {
  const { onStart, onStatus, onDelta, onDone, onError } = handlers;
  let text = '';
  const promise = api
    .streamChat(payload, {
      signal: opts.signal,
      onEvent(event, data) {
        if (event === 'start') {
          if (onStart) onStart(data);
        } else if (event === 'status') {
          if (onStatus) onStatus(data);
        } else if (event === 'delta') {
          const piece = data && typeof data === 'object' ? data.text || '' : String(data || '');
          text += piece;
          if (onDelta) onDelta(piece, text);
        } else if (event === 'done') {
          if (onDone) onDone(data);
        } else if (event === 'error') {
          if (onError) onError(new Error((data && data.error) || '生成失败'));
        }
      }
    })
    .catch((err) => {
      if (err && err.code === 'aborted') throw err;
      if (onError) onError(err);
      throw err;
    });
  return {
    abort: () => {
      if (opts.controller) opts.controller.abort();
    },
    promise,
    get text() {
      return text;
    }
  };
}

export function createLiveWatcher(key, handlers = {}) {
  const { onLog, onStatus, onDone, onError } = handlers;
  let closed = false;
  let source = null;
  let retry = 0;
  let reconnectTimer = 0;
  let paused = false;

  const mockLines = [
    '检索第 46 章人物状态与伏笔',
    '生成第 47 章场景节拍',
    '自检：知情范围冲突 0 处',
    '字数门禁：2860 / 2800 通过',
    '章末钩子评分 91',
    '写入版本快照'
  ];
  let mockIndex = 0;
  let mockTimer = 0;

  function startMock() {
    if (mockTimer) return;
    mockTimer = setInterval(() => {
      if (closed || document.hidden) return;
      const line = mockLines[mockIndex % mockLines.length];
      mockIndex += 1;
      if (onLog) onLog({ message: line, level: 'info', at: Date.now() });
    }, 5200);
  }

  function stopMock() {
    clearInterval(mockTimer);
    mockTimer = 0;
  }

  function connect() {
    if (closed || paused || IS_MOCK) return;
    if (typeof EventSource === 'undefined') {
      if (onError) onError(new Error('当前浏览器不支持实时日志'));
      return;
    }
    closeSource();
    try {
      source = new EventSource(`/api/live/${encodeURIComponent(key)}`);
    } catch (err) {
      scheduleReconnect();
      return;
    }

    source.onopen = () => {
      retry = 0;
      if (onStatus) onStatus({ connected: true });
    };

    const handle = (type) => (event) => {
      if (closed) return;
      let data = event.data;
      try {
        data = JSON.parse(event.data);
      } catch {
        /* 纯文本日志 */
      }
      if (type === 'log') {
        const message =
          typeof data === 'string'
            ? data
            : data && (data.message || data.text || data.line || data.msg) || '';
        if (message && onLog) onLog({ message, level: (data && data.level) || 'info', at: Date.now() });
      } else if (type === 'status') {
        if (onStatus) onStatus(data || {});
      } else if (type === 'done') {
        if (onDone) onDone(data || {});
        closeSource();
      }
    };

    source.addEventListener('log', handle('log'));
    source.addEventListener('status', handle('status'));
    source.addEventListener('done', handle('done'));

    source.onerror = () => {
      if (closed) return;
      if (onStatus) onStatus({ connected: false });
      closeSource();
      scheduleReconnect();
    };
  }

  function closeSource() {
    if (source) {
      source.onopen = null;
      source.onerror = null;
      source.close();
      source = null;
    }
  }

  function scheduleReconnect() {
    if (closed || paused) return;
    clearTimeout(reconnectTimer);
    const wait = Math.min(30000, 1000 * Math.pow(1.8, retry));
    retry += 1;
    reconnectTimer = setTimeout(connect, wait);
  }

  function onVisibility() {
    if (document.hidden) {
      paused = true;
      closeSource();
      clearTimeout(reconnectTimer);
      stopMock();
    } else {
      paused = false;
      if (IS_MOCK) startMock();
      else connect();
    }
  }

  document.addEventListener('visibilitychange', onVisibility);
  if (IS_MOCK) startMock();
  else connect();

  return {
    close() {
      closed = true;
      clearTimeout(reconnectTimer);
      closeSource();
      stopMock();
      document.removeEventListener('visibilitychange', onVisibility);
    },
    reconnect() {
      retry = 0;
      paused = false;
      if (IS_MOCK) startMock();
      else connect();
    }
  };
}

/* 轮询书本 live 数据（1.5s），页面隐藏时自动停。返回 stop()。 */
export function pollBookLive(key, onData, onError, interval = 1500) {
  let timer = 0;
  let stopped = false;
  let busy = false;

  const tick = async () => {
    if (stopped || document.hidden || busy) return;
    busy = true;
    try {
      const data = await api.bookLive(key);
      if (!stopped && data) onData(data);
    } catch (err) {
      if (!stopped && onError && !(err && err.isMissing)) onError(err);
    } finally {
      busy = false;
    }
  };

  const loop = () => {
    if (stopped) return;
    tick();
    timer = setTimeout(loop, interval);
  };

  const onVisibility = () => {
    if (!document.hidden) tick();
  };
  document.addEventListener('visibilitychange', onVisibility);
  loop();

  return () => {
    stopped = true;
    clearTimeout(timer);
    document.removeEventListener('visibilitychange', onVisibility);
  };
}
