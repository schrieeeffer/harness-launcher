'use strict';

// This module owns application state. Views receive immutable snapshots and
// dispatch intentions; only the reducer can change the connection or draft.
const LauncherState = (() => {
  const official = plugin => plugin.management === 'official';
  const preserveOfficial = (server, ids) => [...new Set([
    ...ids.filter(id => !server.plugins.some(p => p.id === id && official(p))),
    ...server.plugins.filter(p => official(p) && server.enabled.includes(p.id)).map(p => p.id)
  ])];
  const terminal = connection => ['stopped', 'expired'].includes(connection.status);
  const sameSelection = (left, right) => left.length === right.length && left.every(id => right.includes(id));
  const freeze = value => {
    if (value && typeof value === 'object' && !Object.isFrozen(value)) {
      Object.values(value).forEach(freeze);
      Object.freeze(value);
    }
    return value;
  };
  const initial = () => freeze({
    server: null,
    draft: {ids: [], mode: 'current', scheme: ''},
    connection: {status: 'checking', detail: ''},
    operation: {busy: false, epoch: 0, current: null}, polling: false,
    requests: {state: 0, poll: 0}
  });
  const accepts = (model, ticket) => Boolean(ticket &&
    ticket.epoch === model.operation.epoch &&
    ticket.id === model.requests[ticket.channel] &&
    (ticket.channel !== 'poll' || ticket.stateRequest === model.requests.state) && !terminal(model.connection));
  function reduce(model, event) {
    switch (event.type) {
      case 'connection/set': {
        if (!['checking', 'online', 'offline', 'error', 'expired', 'stopped'].includes(event.status)) throw new Error('Unknown connection state');
        if (terminal(model.connection)) return model;
        // Only a successfully decoded state may establish an online connection.
        if (event.status === 'online') throw new Error('An online connection requires a state snapshot');
        return {...model, connection: {status: event.status, detail: event.detail || ''}};
      }
      case 'request/start':
        if (!Object.hasOwn(model.requests, event.channel)) throw new Error('Unknown request channel');
        return {...model, requests: {...model.requests, [event.channel]: model.requests[event.channel] + 1}};
      case 'state/received': {
        if (!accepts(model, event.ticket)) return model;
        const server = event.server, known = new Set(server.plugins.map(plugin => plugin.id));
        const draft = event.reset || !model.server
          ? {ids: [...server.enabled], mode: 'current', scheme: ''}
          : {...model.draft, ids: preserveOfficial(server, model.draft.ids.filter(id => known.has(id)))};
        // A refresh preserves the user's draft, but a preset name must still
        // describe that draft. In particular, sending an outdated scheme name
        // makes the backend reject an otherwise valid custom selection.
        const preset = draft.mode === 'scheme' ? (Object.hasOwn(server.schemes, draft.scheme) ? server.schemes[draft.scheme] : null)
          : draft.mode === 'current' ? server.enabled
          : draft.mode === 'all' ? preserveOfficial(server, server.plugins.filter(plugin => plugin.available && !plugin.suspended).map(plugin => plugin.id))
          : draft.mode === 'none' ? preserveOfficial(server, []) : null;
        if (draft.mode !== 'custom' && (!preset || !sameSelection(draft.ids, preset))) {
          draft.mode = 'custom'; draft.scheme = '';
        }
        return {...model, server, draft, connection: {status: 'online', detail: ''}};
      }
      case 'poll/start':
        return model.polling || terminal(model.connection) ? model : {...model, polling: true};
      case 'poll/end': return {...model, polling: false};
      case 'poll/received':
        if (!accepts(model, event.ticket) || model.operation.busy || !model.server) return model;
        return {...model, server: {...model.server, running: event.running, tracking: event.tracking}};
      case 'operation/start':
        return model.operation.busy ? model : {...model, operation: {busy: true, epoch: model.operation.epoch + 1, current: null}};
      case 'operation/observed':
        return !model.operation.busy || event.epoch !== model.operation.epoch || terminal(model.connection) ? model : {...model, operation: {...model.operation, current: event.current}};
      case 'operation/end': return {...model, operation: {...model.operation, busy: false, current: null}};
      case 'tracking/received':
        if (terminal(model.connection)) return model;
        return {...model, server: model.server && {...model.server, tracking: event.tracking}};
      case 'harness/started':
        return model.server ? {...model, server: {...model.server, running: true}} : model;
      case 'draft/preset': {
        if (!model.server || model.operation.busy) return model;
        if (!['current', 'none', 'all'].includes(event.preset)) throw new Error('Unknown selection preset');
        const proposed = event.preset === 'none' ? [] : event.preset === 'all'
          ? model.server.plugins.filter(plugin => plugin.available && !plugin.suspended).map(plugin => plugin.id) : [...model.server.enabled];
        const ids = preserveOfficial(model.server, proposed);
        return {...model, draft: {ids, mode: event.preset, scheme: ''}};
      }
      case 'draft/toggle': {
        if (model.operation.busy) return model;
        const plugin = model.server?.plugins.find(plugin => plugin.id === event.id);
        if (!plugin || official(plugin) || event.checked && (!plugin.available || plugin.suspended)) return model;
        const ids = new Set(model.draft.ids);
        if (event.checked) ids.add(event.id); else ids.delete(event.id);
        return {...model, draft: {ids: [...ids], mode: 'custom', scheme: ''}};
      }
      case 'draft/scheme': {
        if (!model.server || !Object.hasOwn(model.server.schemes, event.name)) return model;
        const known = new Set(model.server.plugins.map(plugin => plugin.id));
        const saved = model.server.schemes[event.name], ids = preserveOfficial(model.server, saved.filter(id => known.has(id)));
        const complete = sameSelection(ids, saved);
        return {...model, draft: {ids, mode: complete ? 'scheme' : 'custom', scheme: complete ? event.name : ''}};
      }
      default: throw new Error('Unknown state event: ' + event.type);
    }
  }
  function derive(model) {
    const server = model.server, ids = new Set(model.draft.ids), tracking = server?.tracking;
    const selection = (server?.plugins || []).filter(plugin => ids.has(plugin.id)).map(plugin => plugin.id);
    const changed = Boolean(server && (server.enabled.length !== ids.size || server.enabled.some(id => !ids.has(id))));
    const patch = server?.patchMode;
    const adapterManaged = patch && (patch.mode !== 'unmanaged' || server.plugins.some(plugin => plugin.id === patch.plugin));
    const patchChanged = Boolean(patch?.supported && (patch.pending || adapterManaged && patch.mode !== (ids.has(patch.plugin) ? 'plugin' : 'native')));
    const untracked = Boolean(server?.plugins.some(plugin => plugin.tracking === 'untracked' && !official(plugin) && !plugin.suspended && plugin.id !== tracking?.session?.plugin));
    const externalActive = Boolean(server?.plugins.some(plugin => official(plugin) && server.enabled.includes(plugin.id)));
    const recording = Boolean(tracking?.session || tracking?.busy || tracking?.pending || server?.maintenance?.pending || server?.officialUpdate?.pending);
    const connected = model.connection.status === 'online';
    const autoUpdate = Boolean(server?.officialUpdate?.autoAvailable);
    const runtimeReady = !server?.runtimeHealth || server.runtimeHealth.status === 'ready' || autoUpdate && server.runtimeHealth.status === 'missing';
    const hostStatus = server?.hostHealth?.status || 'ready';
    const hostReady = hostStatus === 'ready';
    const prepareTracking = autoUpdate && ['changed', 'native_ready'].includes(hostStatus);
    const trackingBlockedReason = !server ? '正在读取安装条件，请稍候。'
      : !connected ? '与启动器后台的连接尚未恢复，请先重新连接。'
      : model.operation.busy ? '正在处理当前操作，请等待完成。'
      : server.officialUpdate?.pending ? '官方更新尚未完成，请到设置中继续检查更新结果。'
      : server.maintenance?.pending ? '插件维护尚未完成，请到“更新与卸载”处理恢复。'
      : recording ? '安装记录或组合切换尚未完成，请先处理当前任务。'
      : server.running ? '请先完全退出 Harness（包括托盘），再建立快照并安装。'
      : !server.executableExists ? '未找到 Harness 程序，请先在设置中修正应用路径。'
      : untracked ? '已有插件缺少安装前记录，需先恢复干净的安装。'
      : externalActive ? '请先在 Harness 内停用自行管理的插件，再记录新补丁；在 Harness 内安装插件不受此限制。'
      : !runtimeReady ? (server.runtimeHealth?.message || '运行时文件需要修复，请先在设置中检查。')
      : !hostReady && !prepareTracking ? '官方程序已更新，需先在设置中验证并接续新版基线，才能安装插件。' : '';
    const nativeReady = hostStatus === 'native_ready' && selection.length === 0 && !server.enabled.length && model.draft.mode !== 'scheme';
    const canSwitch = Boolean(server && connected && !model.operation.busy && !recording && !server.running && server.executableExists && !untracked && (!externalActive || !changed && !patchChanged));
    return {
      selection, changed, patchChanged, untracked, recording, connected, externalActive,
      canLaunch: canSwitch && runtimeReady && (hostReady || nativeReady || autoUpdate && hostStatus === 'changed'), canApply: canSwitch && hostReady && (changed || patchChanged), canRestore: canSwitch && hostReady && !externalActive,
      canSaveScheme: Boolean(server && connected && !model.operation.busy && !externalActive),
      canBeginTracking: !trackingBlockedReason, trackingBlockedReason, prepareTracking,
      canManageTracking: connected && !model.operation.busy && !tracking?.busy && !server?.running,
      canQuit: ['online', 'error'].includes(model.connection.status) && !model.operation.busy && !tracking?.busy && !(tracking?.session && !tracking.session.error)
    };
  }
  function create() {
    let value = initial(); const listeners = new Set();
    return {
      get value() { return value; },
      dispatch(event) {
        const next = freeze(reduce(value, event));
        if (next === value) return false;
        value = next; listeners.forEach(listener => listener(value, event)); return true;
      },
      subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
      beginRead(channel) {
        this.dispatch({type: 'request/start', channel});
        return Object.freeze({channel, id: value.requests[channel], epoch: value.operation.epoch, stateRequest: value.requests.state});
      },
      accepts(ticket) { return accepts(value, ticket); }
    };
  }
  return Object.freeze({create, derive, terminal});
})();
