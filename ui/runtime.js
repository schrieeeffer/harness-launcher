'use strict';

// Coordinates effects and their ordering. A mutation invalidates all older
// reads; reads never replay a write whose response may have been lost.
const LauncherRuntime = (() => {
  const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const strings = value => Array.isArray(value) && value.every(item => typeof item === 'string');
  const invalid = () => new LauncherAPI.RequestError('后台返回的状态结构不完整，请重新打开启动菜单。', {code: 'INVALID_STATE', connection: 'error'});
  function validateTracking(tracking) {
    if (!object(tracking) || typeof tracking.busy !== 'boolean' || !strings(tracking.scopes) || !object(tracking.records) ||
        !object(tracking.progress) || !Number.isFinite(tracking.progress.count) || !Number.isFinite(tracking.progress.bytes) || typeof tracking.progress.path !== 'string' ||
        !Array.isArray(tracking.review) || tracking.review.some(item => !object(item) || typeof item.path !== 'string' || typeof item.change !== 'string') ||
        Object.values(tracking.records).some(record => !object(record) || !strings(record.baseSelection)) ||
        tracking.completed != null && (!object(tracking.completed) || typeof tracking.completed.plugin !== 'string')) throw invalid();
    const session = tracking.session;
    if (session !== null && (!object(session) || typeof session.id !== 'string' || typeof session.plugin !== 'string' ||
        !['preparing', 'installing', 'recording', 'scanning', 'verifying'].includes(session.phase) ||
        !['idle', 'launching', 'running', 'closed'].includes(session.runState))) throw invalid();
    return tracking;
  }
  function validateState(server) {
    if (!Array.isArray(server.plugins) || !strings(server.enabled) || !object(server.schemes) ||
        typeof server.running !== 'boolean' || typeof server.executableExists !== 'boolean' || typeof server.home !== 'string' || typeof server.revision !== 'string' || !Array.isArray(server.backups) ||
        server.plugins.some(plugin => !object(plugin) || typeof plugin.id !== 'string' || typeof plugin.description !== 'string' || typeof plugin.available !== 'boolean' || !['auto', 'builtin', 'untracked'].includes(plugin.tracking)) ||
        server.backups.some(backup => !object(backup) || typeof backup.id !== 'string' || typeof backup.time !== 'string' || typeof backup.reason !== 'string' || !strings(backup.plugins)) ||
        server.patchMode != null && (!object(server.patchMode) || server.patchMode.unknown != null && !strings(server.patchMode.unknown)) ||
        Object.values(server.schemes).some(ids => !strings(ids))) throw invalid();
    validateTracking(server.tracking);
    return server;
  }
  function create({store, transport, notify = () => {}, schedule = setTimeout, cancel = clearTimeout}) {
    let pollTimer, trackingWasWorking = false, refreshOnResume = false;
    const setConnection = (status, detail = '') => store.dispatch({type: 'connection/set', status, detail});
    async function request(path, body, timeoutMs, {signal, reportConnection = true} = {}) {
      if (body !== undefined && !['online', 'error'].includes(store.value.connection.status)) throw new LauncherAPI.RequestError('请先恢复与启动器后台的连接，再执行操作。', {code: 'CONNECTION_REQUIRED'});
      const epoch = store.value.operation.epoch;
      try {
        const data = await transport.request(path, {body, timeoutMs, signal});
        if (path === 'state') return validateState(data);
        if (path === 'tracking') return validateTracking(data);
        if (path === 'ping' && typeof data.running !== 'boolean') throw invalid();
        return data;
      }
      catch (error) {
        if (!signal?.aborted && epoch === store.value.operation.epoch && error.connection && (reportConnection === true || reportConnection === 'auth' && error.connection === 'expired')) setConnection(error.connection, error.message);
        throw error;
      }
    }
    async function refresh(reset = false) {
      const ticket = store.beginRead('state');
      try {
        const server = await request('state', undefined, undefined, {reportConnection: false});
        return store.dispatch({type: 'state/received', ticket, server, reset});
      } catch (error) {
        if (store.accepts(ticket)) setConnection(error.connection || 'error', error.message);
        throw error;
      }
    }
    async function act(fn) {
      if (store.value.operation.busy) return;
      store.dispatch({type: 'operation/start'});
      const epoch = store.value.operation.epoch, controller = new AbortController();
      let statusTimer;
      async function observe() {
        if (controller.signal.aborted) return;
        try {
          const status = await request('operations', undefined, 5000, {signal: controller.signal, reportConnection: 'auth'});
          if (status.current === null || object(status.current) && typeof status.current.kind === 'string') store.dispatch({type: 'operation/observed', epoch, current: status.current});
        } catch (_) { /* Status is advisory; never cancel or replay a write. */ }
        if (!controller.signal.aborted) statusTimer = schedule(observe, 2000);
      }
      statusTimer = schedule(observe, 2000);
      try { return await fn(); }
      catch (error) { notify(error.message + (error.recovery ? '\n' + error.recovery : ''), true); }
      finally { controller.abort(); cancel(statusTimer); store.dispatch({type: 'operation/end'}); }
    }
    async function poll(refreshState = false) {
      // Background polling only reads inexpensive status endpoints. Returning
      // to a menu must also notice changes made in Harness or another menu.
      if (refreshState === true) refreshOnResume = true;
      if (store.value.polling || LauncherState.terminal(store.value.connection)) return;
      cancel(pollTimer); store.dispatch({type: 'poll/start'});
      const ticket = store.beginRead('poll');
      try {
        const ping = await request('ping', undefined, 10000, {reportConnection: false});
        if (!store.accepts(ticket) || store.value.operation.busy) return;
        if (refreshOnResume || store.value.connection.status !== 'online' || !store.value.server) {
          refreshOnResume = false;
          await refresh(!store.value.server);
        }
        else {
          const previousSession = store.value.server?.tracking?.session?.id;
          const tracking = await request('tracking', undefined, undefined, {reportConnection: false});
          if (!store.dispatch({type: 'poll/received', ticket, tracking, running: ping.running})) return;
          if (previousSession && !tracking.session) {
            const completed = tracking.completed;
            await refresh(true);
            if (completed) notify(completed.plugin + ' 的还原记录已自动保存，现在可以切换插件组合。');
          } else if (trackingWasWorking && !tracking.busy) await refresh();
        }
        trackingWasWorking = Boolean(store.value.server?.tracking?.busy);
      } catch (error) {
        if (store.accepts(ticket)) setConnection(error.connection || 'error', error.message);
      } finally {
        store.dispatch({type: 'poll/end'});
        if (!LauncherState.terminal(store.value.connection)) pollTimer = schedule(poll, 4000);
      }
    }
    store.subscribe(model => { if (LauncherState.terminal(model.connection)) cancel(pollTimer); });
    return Object.freeze({request, refresh, act, poll, setConnection});
  }
  return Object.freeze({create});
})();
