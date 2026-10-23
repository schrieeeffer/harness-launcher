'use strict';

// HTTP is a boundary: it does not mutate application state or touch the DOM.
const LauncherAPI = (() => {
  class RequestError extends Error {
    constructor(message, {status = 0, code = 'REQUEST_FAILED', retryable = false, details = {}, recovery = '', operationId = null, connection = null} = {}) {
      super(message); this.name = 'RequestError';
      Object.assign(this, {status, code, retryable, details, recovery, operationId, connection});
    }
  }
  function create({token, fetch: send = globalThis.fetch, schedule = setTimeout, cancel = clearTimeout}) {
    async function request(path, {body, timeoutMs = body === undefined ? 30000 : 300000, signal} = {}) {
      const controller = new AbortController();
      const abort = () => controller.abort();
      signal?.addEventListener('abort', abort, {once: true});
      if (signal?.aborted) abort();
      const timeout = timeoutMs ? schedule(abort, timeoutMs) : null;
      let response, data;
      try {
        response = await send('/api/' + path, {
          signal: controller.signal, method: body === undefined ? 'GET' : 'POST',
          headers: {Authorization: 'Bearer ' + token, ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
          ...(body === undefined ? {} : {body: JSON.stringify(body)})
        });
        data = await response.json();
        if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('Invalid response envelope');
      } catch (cause) {
        if (signal?.aborted) throw cause;
        const timedOut = cause.name === 'AbortError';
        throw new RequestError(timedOut ? body === undefined ? '读取状态超时，后台可能仍在处理文件。' : '操作等待超时，后台可能仍在执行。请先查看操作状态并核实结果。' : response ? '后台返回了无法读取的数据。' : '无法连接启动器后台，请重新连接或重新打开启动菜单。', {
          code: timedOut ? 'REQUEST_TIMEOUT' : response ? 'INVALID_RESPONSE' : 'CONNECTION_LOST',
          retryable: body === undefined, connection: response || timedOut && path !== 'ping' ? 'error' : 'offline'
        });
      } finally { cancel(timeout); signal?.removeEventListener('abort', abort); }
      if (!response.ok) throw new RequestError(typeof data.error === 'string' ? data.error : '请求失败', {
        status: response.status, code: data.code || 'REQUEST_FAILED', retryable: data.retryable === true,
        details: data.details || {}, recovery: data.recovery || '', operationId: data.operationId || null,
        connection: response.status === 403 ? 'expired' : null
      });
      return data;
    }
    return Object.freeze({request});
  }
  return Object.freeze({create, RequestError});
})();
