'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// Run the real UI with a small DOM/HTTP boundary; no browser packages or network.
// IDs come from the real HTML so a missing control fails the test too.
class Element {
  constructor() { this.children = []; this.dataset = {}; this.value = ''; this.disabled = false; this.hidden = false; this.events = {}; this.classList = {toggle() {}, contains() {return false;}}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this.text = ''; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(c => c.textContent || '').join(''); }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, fn) { this.events[name] = fn; }
  querySelectorAll() { return []; }
  matches() { return false; }
  showPopover() {}
  hidePopover() {}
  showModal() { this.open = true; }
  close() { this.open = false; this.events.close?.(); }
  focus() {}
}
const response = (data, status = 200) => ({ok: status < 400, status, json: async () => structuredClone(data)});
async function app() {
  const nodes = new Map([...fs.readFileSync('ui/index.html', 'utf8').matchAll(/\bid="([^"]+)"/g)].map(m => [m[1], new Element()]));
  const document = {
    getElementById: id => {assert.ok(nodes.has(id), 'HTML is missing #' + id); return nodes.get(id);},
    createElement: () => new Element(), createElementNS: () => new Element(),
    createTextNode: text => ({textContent: text}),
    querySelectorAll: () => [], querySelector: () => ({value: 'npm'}),
    addEventListener(name, fn) {this[name] = fn;}, hidden: false
  };
  const window = {addEventListener(name, fn) {this[name] = fn;}};
  const tracking = {session: null, busy: false, completed: null, records: {}, scopes: [], review: [], progress: {count: 0, bytes: 0, path: ''}};
  const state = {enabled: ['plugin-a'], plugins: [{id: 'plugin-a', description: 'Fixture', available: true, tracking: 'auto', version: '1'}], schemes: {}, backups: [], running: false, executableExists: true, home: 'fixture', revision: 'r1', tracking};
  const calls = [], timers = new Map(); let timerId = 0;
  const fixture = {nodes, document, window, state, tracking, calls, timers};
  fixture.respond = path => response(path === 'state' ? state : path === 'tracking' ? tracking : {running: false});
  const context = vm.createContext({document, window, URLSearchParams, URL, AbortController, location: {search: '?token=fixture'}, sessionStorage: {getItem() {}, setItem() {}},
    setTimeout(fn, delay) {timers.set(++timerId, {fn, delay}); return timerId;}, clearTimeout(id) {timers.delete(id);},
    fetch: async (url, options) => {calls.push({url, ...options}); return fixture.respond(url.replace('/api/', ''), options);}
  });
  fixture.run = code => vm.runInContext(code, context);
  for (const file of ['state.js', 'api.js', 'runtime.js', 'app.js']) await vm.runInContext(fs.readFileSync('ui/' + file, 'utf8'), context);
  assert.equal(fixture.run('model.connection.status'), 'online');
  return fixture;
}

test('turning all plugins off stays a draft and does not disconnect or start anything', async () => {
  const ui = await app(); const requests = ui.calls.length;
  ui.nodes.get('selectNone').onclick();
  assert.equal(ui.run('model.draft.ids.length'), 0);
  assert.equal(ui.nodes.get('launch').disabled, false);
  assert.equal(ui.nodes.get('apply').disabled, false);
  assert.equal(ui.run('model.connection.status'), 'online');
  assert.equal(ui.calls.length, requests);
});

test('offline state survives selection changes; reconnect reads actual state and keeps the draft', async () => {
  const ui = await app(); const online = ui.respond;
  ui.respond = () => {throw new TypeError('connection refused');};
  await ui.run('pollConnection()');
  ui.nodes.get('selectNone').onclick();
  assert.equal(ui.run('model.connection.status'), 'offline');
  assert.equal(ui.nodes.get('launch').disabled, true);
  assert.equal(ui.nodes.get('apply').disabled, true);
  assert.equal(ui.nodes.get('connectionWarning').hidden, false);
  assert.match(ui.nodes.get('connectionText').textContent, /launch.vbs/);
  ui.state.enabled = []; ui.state.revision = 'r2'; ui.respond = online;
  await ui.nodes.get('reconnect').onclick();
  assert.equal(ui.run('model.connection.status'), 'online');
  assert.equal(ui.run('model.draft.ids.length'), 0);
  assert.equal(ui.run('model.server.revision'), 'r2');
  assert.equal(ui.nodes.get('connectionWarning').hidden, true);
  assert.equal(ui.nodes.get('launch').disabled, false);
  assert.ok(ui.calls.every(call => call.method === 'GET'));
});

test('a recording validation error is reported as a data error, not a disconnected service', async () => {
  const ui = await app(); const online = ui.respond;
  ui.respond = path => path === 'tracking' ? response({error: 'Fixture record needs repair'}, 400) : online(path);
  await ui.run('pollConnection()');
  assert.equal(ui.run('model.connection.status'), 'error');
  assert.match(ui.nodes.get('connectionText').textContent, /Fixture record needs repair/);
  assert.equal(ui.nodes.get('launch').disabled, true);
  ui.respond = online;
  await ui.run('pollConnection()');
  assert.equal(ui.nodes.get('launch').disabled, false);
});

test('expired credentials require a fresh menu and stop automatic retries', async () => {
  const ui = await app(); ui.respond = () => response({error: 'Forbidden'}, 403);
  await ui.run('pollConnection()');
  assert.equal(ui.run('model.connection.status'), 'expired');
  assert.match(ui.nodes.get('connectionText').textContent, /新打开的菜单/);
  assert.equal(ui.nodes.get('reconnect').hidden, true);
  const count = ui.calls.length;
  await ui.window.focus(); await ui.document.visibilitychange();
  await assert.rejects(ui.run("api('launch', {})"), /恢复.*连接/);
  assert.equal(ui.calls.length, count);
  assert.equal(ui.timers.size, 0);
});

test('focus and timer events share one in-flight poll; resuming refreshes promptly', async () => {
  const ui = await app(); const online = ui.respond; let finish;
  ui.respond = path => path === 'ping' ? new Promise(resolve => {finish = () => resolve(response({running: false}));}) : online(path);
  const count = ui.calls.length, pending = ui.window.focus();
  await ui.window.pageshow(); await ui.window.online(); await ui.document.visibilitychange();
  assert.equal(ui.calls.length, count + 1);
  finish(); await pending;
  assert.equal(ui.calls.length, count + 2);
  assert.equal([...ui.timers.values()].filter(t => t.delay === 4000).length, 1);
});

test('unresponsive ping times out and allows retry instead of leaving an endless request', async () => {
  const ui = await app();
  ui.respond = (path, options) => new Promise((resolve, reject) => options.signal.addEventListener('abort', () => {const error = new Error('timeout'); error.name = 'AbortError'; reject(error);}));
  const pending = ui.run('pollConnection()');
  [...ui.timers.values()].find(t => t.delay === 10000).fn();
  await pending;
  assert.equal(ui.run('model.connection.status'), 'offline');
  assert.equal(ui.nodes.get('reconnect').disabled, false);
  assert.equal(ui.nodes.get('launch').disabled, true);
});

test('lost launch response is never retried automatically on reconnect', async () => {
  const ui = await app(); const online = ui.respond;
  ui.respond = (path, options) => {if (options.method === 'POST') throw new TypeError('response lost'); return online(path);};
  await ui.nodes.get('launch').onclick();
  assert.equal(ui.run('model.connection.status'), 'offline');
  ui.respond = online; ui.state.running = true;
  await ui.run('pollConnection()');
  assert.equal(ui.calls.filter(call => call.method === 'POST').length, 1);
  assert.equal(ui.run('model.server.running'), true);
  assert.equal(ui.nodes.get('launch').disabled, true);
});

test('late reads cannot reenable an explicitly stopped menu', async () => {
  const ui = await app(); const online = ui.respond; let finish;
  ui.respond = path => path === 'tracking' ? new Promise(resolve => {finish = () => resolve(response(ui.tracking));}) : online(path);
  const pending = ui.run('pollConnection()');
  while (!finish) await new Promise(resolve => setImmediate(resolve));
  ui.run("setConnection('stopped')"); finish(); await pending;
  ui.nodes.get('selectNone').onclick();
  assert.equal(ui.run('model.connection.status'), 'stopped');
  assert.equal(ui.nodes.get('launch').disabled, true);
  assert.equal(ui.timers.size, 0);
});

test('a poll started before launch cannot replace the successful launch state', async () => {
  const ui = await app(); const online = ui.respond; let finish;
  ui.respond = path => path === 'tracking' ? new Promise(resolve => {finish = () => resolve(response(ui.tracking));}) : online(path);
  const pending = ui.run('pollConnection()');
  while (!finish) await new Promise(resolve => setImmediate(resolve));
  await ui.nodes.get('launch').onclick();
  assert.equal(ui.run('model.server.running'), true);
  finish(); await pending;
  assert.equal(ui.run('model.server.running'), true);
  assert.equal(ui.nodes.get('launch').disabled, true);
});

test('a failed read started before a successful action cannot mark the new state offline', async () => {
  const ui = await app(); const online = ui.respond; let fail;
  ui.respond = path => path === 'tracking' ? new Promise((resolve, reject) => {fail = () => reject(new TypeError('old connection closed'));}) : online(path);
  const pending = ui.run('pollConnection()');
  while (!fail) await new Promise(resolve => setImmediate(resolve));
  await ui.nodes.get('launch').onclick(); fail(); await pending;
  assert.equal(ui.run('model.connection.status'), 'online');
  assert.equal(ui.run('model.server.running'), true);
});

test('state reads that finish out of order keep the newest refresh', async () => {
  const ui = await app(); const online = ui.respond; let finish;
  ui.respond = path => path === 'state' ? new Promise(resolve => {finish = () => resolve(response({...ui.state, revision: 'old'}));}) : online(path);
  const pending = ui.run('refresh()');
  ui.respond = online; ui.state.revision = 'new'; await ui.run('refresh()');
  finish(); await pending;
  assert.equal(ui.run('model.server.revision'), 'new');
});

test('catalog pagination uses the displayed query and retries the failed page', async () => {
  const ui = await app(); const pages = [];
  ui.nodes.get('catalogDialog').showModal(); ui.nodes.get('catalogQuery').value = 'original'; ui.nodes.get('catalogRegistry').value = 'npm';
  ui.respond = path => {
    const url = new URL('https://fixture/' + path); pages.push(Object.fromEntries(url.searchParams));
    return response({items: [{name: 'dsh-page-' + url.searchParams.get('page'), provider: 'npm', kind: 'community'}], hasMore: true});
  };
  await ui.run('searchCatalog()');
  ui.nodes.get('catalogQuery').value = 'unsubmitted draft';
  const successful = ui.respond; let fail = true;
  ui.respond = path => {const result = successful(path); if (fail) {fail = false; throw new Error('temporary upstream outage');} return result;};
  await ui.nodes.get('catalogMore').onclick();
  assert.equal(ui.nodes.get('catalogResults').children.length, 1);
  await ui.nodes.get('catalogRetry').onclick();
  assert.deepEqual(pages.map(p => [p.q, p.page]), [['original', '0'], ['original', '1'], ['original', '1']]);
  assert.equal(ui.nodes.get('catalogResults').children.length, 2);
});

test('late refresh failures cannot mark a newer successful refresh offline', async () => {
  const ui = await app(); const online = ui.respond; let fail;
  ui.respond = path => path === 'state' ? new Promise((resolve, reject) => {fail = () => reject(new TypeError('old read failed'));}) : online(path);
  const pending = ui.run('refresh()');
  ui.respond = online; ui.state.revision = 'latest'; await ui.run('refresh()');
  fail(); await assert.rejects(pending);
  assert.equal(ui.run('model.server.revision'), 'latest');
  assert.equal(ui.run('model.connection.status'), 'online');
});

test('a poll cannot overwrite a state refresh that began after it', async () => {
  const ui = await app(); const online = ui.respond; let finish;
  ui.respond = path => path === 'tracking' ? new Promise(resolve => {finish = () => resolve(response(ui.tracking));}) : online(path);
  const pending = ui.run('pollConnection()');
  while (!finish) await new Promise(resolve => setImmediate(resolve));
  ui.state.running = true; await ui.run('refresh()'); finish(); await pending;
  assert.equal(ui.run('model.server.running'), true);
  assert.equal(ui.nodes.get('launch').disabled, true);
});

test('malformed tracking never enters the store and a subsequent refresh recovers', async () => {
  const ui = await app(); const online = ui.respond;
  ui.respond = path => path === 'tracking' ? response({busy: false, session: {phase: 'imaginary'}}) : online(path);
  await ui.run('pollConnection()');
  assert.equal(ui.run('model.server.tracking.session'), null);
  assert.equal(ui.run('model.connection.status'), 'error');
  assert.match(ui.nodes.get('connectionText').textContent, /结构不完整/);
  ui.respond = online; await ui.run('pollConnection()');
  assert.equal(ui.run('model.connection.status'), 'online');
});

test('malformed backup and patch lists cannot replace the last usable state snapshot', async () => {
  for (const invalid of [{backups: [{id: 'broken.json', time: '2026-01-01T00:00:00Z', reason: 'Fixture', plugins: null}]}, {patchMode: {unknown: 'not-an-array'}}]) {
    const ui = await app();
    Object.assign(ui.state, invalid, {revision: 'malformed'});
    await assert.rejects(ui.run('refresh()'), error => error.code === 'INVALID_STATE');
    assert.equal(ui.run('model.server.revision'), 'r1');
    assert.equal(ui.run('model.connection.status'), 'error');
    assert.doesNotThrow(() => ui.nodes.get('backupsButton').onclick());
    assert.equal(ui.nodes.get('launch').disabled, true);
  }
});

test('the state snapshot is immutable and terminal connections reject late snapshots', async () => {
  const ui = await app();
  assert.throws(() => ui.run("model.draft.ids.push('foreign-plugin')"), /extensible|read only/);
  assert.throws(() => ui.run("'use strict'; model.server.running = true"), /read only/);
  assert.throws(() => ui.run("store.dispatch({type: 'connection/set', status: 'online'})"), /snapshot/);
  ui.run("setConnection('expired')"); await ui.run('refresh(true)');
  assert.equal(ui.run('model.connection.status'), 'expired');
  assert.equal(ui.nodes.get('launch').disabled, true);
});

test('selection changes are constrained to known available plugins and removed schemes lose their label', async () => {
  const ui = await app();
  ui.state.plugins.push({id: 'broken', available: false, description: '', tracking: 'auto'});
  ui.state.schemes.saved = ['plugin-a', 'missing']; await ui.run('refresh()');
  ui.run("store.dispatch({type: 'draft/toggle', id: 'broken', checked: true}); store.dispatch({type: 'draft/toggle', id: 'unknown', checked: true})");
  assert.deepEqual(Array.from(ui.run('model.draft.ids')), ['plugin-a']);
  ui.run("useScheme('saved')"); assert.equal(ui.run('model.draft.mode'), 'custom');
  assert.equal(ui.run('model.draft.scheme'), '');
  assert.match(ui.nodes.get('toast').textContent, /missing/);
  ui.state.schemes.saved = ['plugin-a']; await ui.run('refresh()');
  ui.run("useScheme('saved')"); assert.equal(ui.run('model.draft.scheme'), 'saved');
  delete ui.state.schemes.saved; await ui.run('refresh()');
  assert.equal(ui.run('model.draft.mode'), 'custom');
  assert.equal(ui.run('model.draft.scheme'), '');
});

test('a scheme missing a removed plugin launches the remaining selection as a custom draft', async () => {
  const ui = await app();
  ui.state.schemes.saved = ['plugin-a', 'removed-plugin']; await ui.run('refresh()');
  ui.run("useScheme('saved')");
  await ui.nodes.get('launch').onclick();
  const write = ui.calls.find(call => call.method === 'POST');
  assert.deepEqual(JSON.parse(write.body), {selection: ['plugin-a'], revision: 'r1', native: false, scheme: null});
});

test('refresh preserves selections but removes preset labels that no longer match', async () => {
  const ui = await app();
  ui.state.enabled = []; await ui.run('refresh()');
  assert.deepEqual(Array.from(ui.run('model.draft.ids')), ['plugin-a']);
  assert.equal(ui.run('model.draft.mode'), 'custom');

  ui.state.schemes.saved = ['plugin-a']; await ui.run('refresh()'); ui.run("useScheme('saved')");
  ui.state.schemes.saved = []; await ui.run('refresh()');
  assert.deepEqual(Array.from(ui.run('model.draft.ids')), ['plugin-a']);
  assert.equal(ui.run('model.draft.mode'), 'custom');
  assert.equal(ui.run('model.draft.scheme'), '');

  ui.nodes.get('selectAll').onclick();
  ui.state.plugins.push({id: 'new-plugin', available: true, description: '', tracking: 'auto'});
  await ui.run('refresh()');
  assert.deepEqual(Array.from(ui.run('model.draft.ids')), ['plugin-a']);
  assert.equal(ui.run('model.draft.mode'), 'custom');
});

test('a removed scheme named like an inherited property safely loses its label', async () => {
  const ui = await app();
  ui.state.schemes.constructor = ['plugin-a']; await ui.run('refresh()'); ui.run("useScheme('constructor')");
  delete ui.state.schemes.constructor; await ui.run('refresh()');
  assert.equal(ui.run('model.draft.mode'), 'custom');
  assert.equal(ui.run('model.connection.status'), 'online');
});

test('resuming an online menu reads changes from another menu while keeping the local draft', async () => {
  const ui = await app();
  ui.state.enabled = []; ui.state.revision = 'changed-elsewhere'; ui.state.schemes.other = [];
  const before = ui.calls.length;
  await ui.window.focus();
  assert.deepEqual(ui.calls.slice(before).map(call => call.url), ['/api/ping', '/api/state']);
  assert.equal(ui.run('model.server.revision'), 'changed-elsewhere');
  assert.deepEqual(Array.from(ui.run('model.server.enabled')), []);
  assert.deepEqual(Array.from(ui.run('model.draft.ids')), ['plugin-a']);
  assert.equal(ui.run('model.draft.mode'), 'custom');
  assert.deepEqual(Array.from(ui.run('Object.keys(model.server.schemes)')), ['other']);
});

test('all write capabilities follow the same disconnected, recording and running constraints', async () => {
  const ui = await app(); ui.nodes.get('selectNone').onclick();
  assert.equal(ui.run('LauncherState.derive(model).canApply'), true);
  for (const obstacle of ['running', 'untracked', 'recording', 'missing']) {
    ui.state.running = obstacle === 'running';
    ui.state.executableExists = obstacle !== 'missing';
    ui.state.plugins[0].tracking = obstacle === 'untracked' ? 'untracked' : 'auto';
    ui.tracking.busy = obstacle === 'recording';
    await ui.run('refresh()');
    const capabilities = ui.run('LauncherState.derive(model)');
    for (const action of ['canLaunch', 'canApply', 'canRestore', 'canBeginTracking']) assert.equal(capabilities[action], false, obstacle + ' must disable ' + action);
  }
  ui.run("setConnection('offline')");
  assert.equal(ui.run('LauncherState.derive(model).canManageTracking'), false);
  assert.equal(ui.nodes.get('saveScheme').disabled, true);
});

test('patch selection derives from the adapter identity instead of a hardcoded plugin', async () => {
  const ui = await app();
  ui.state.patchMode = {supported: true, plugin: 'plugin-a', mode: 'plugin', pending: false};
  await ui.run('refresh()'); assert.equal(ui.run('needsPatchSwitch()'), false);
  ui.nodes.get('selectNone').onclick(); assert.equal(ui.run('needsPatchSwitch()'), true);
});

test('generic records without an installed adapter do not create a permanent pending change', async () => {
  const ui = await app();
  ui.state.patchMode = {supported: true, plugin: 'absent-adapter', mode: 'unmanaged', pending: false};
  await ui.run('refresh()');
  assert.equal(ui.run('needsPatchSwitch()'), false);
  assert.equal(ui.nodes.get('apply').disabled, true);
  ui.state.patchMode.pending = true; await ui.run('refresh()');
  assert.equal(ui.nodes.get('apply').disabled, false);
});

test('a mutation runs once and freezes draft editing until it finishes', async () => {
  const ui = await app(); const online = ui.respond; let finish;
  ui.respond = (path, options) => options.method === 'POST' ? new Promise(resolve => {finish = () => resolve(response({message: 'started'}));}) : online(path);
  const pending = ui.nodes.get('launch').onclick();
  await ui.nodes.get('launch').onclick(); ui.nodes.get('selectNone').onclick();
  assert.equal(ui.calls.filter(call => call.method === 'POST').length, 1);
  assert.deepEqual(Array.from(ui.run('model.draft.ids')), ['plugin-a']);
  assert.equal(ui.nodes.get('selectNone').disabled, true);
  finish(); await pending;
  assert.equal(ui.nodes.get('selectNone').disabled, false);
});

test('structured errors preserve diagnostics and show actionable recovery without losing connection', async () => {
  const ui = await app();
  ui.respond = () => response({error: 'Operation is blocked', code: 'LAYER_CONFLICT', retryable: false, details: {path: 'fixture.txt'}, recovery: 'Inspect the conflicting file.', operationId: 'op-fixture'}, 409);
  await assert.rejects(ui.run("api('apply', {})"), error => error.code === 'LAYER_CONFLICT' && error.operationId === 'op-fixture' && error.details.path === 'fixture.txt' && !error.retryable);
  await ui.nodes.get('launch').onclick();
  assert.match(ui.nodes.get('toast').textContent, /Inspect the conflicting file/);
  assert.equal(ui.run('model.connection.status'), 'online');
});

test('recording presentation consumes the explicit run state and disables recovery while offline', async () => {
  const ui = await app();
  ui.tracking.session = {id: 'session-fixture', plugin: 'plugin-a', phase: 'recording', runState: 'closed', error: 'Inspect this fixture', baseline: 'fixture'};
  await ui.run('refresh()');
  assert.match(ui.nodes.get('trackingTestHelp').textContent, /检测到 Harness 退出/);
  assert.equal(ui.nodes.get('trackingRetry').disabled, false);
  ui.run("setConnection('offline')");
  assert.equal(ui.nodes.get('trackingRetry').disabled, true);
  assert.equal(ui.nodes.get('trackingAbandon').disabled, true);
});

test('slow writes display independent operation status and stop observing when complete', async () => {
  const ui = await app(); const online = ui.respond; let finish;
  ui.respond = (path, options) => path === 'operations' ? response({current: {id: 'fixture-op', kind: 'launch', status: 'running', durationMs: 2400}, last: null})
    : options.method === 'POST' ? new Promise(resolve => {finish = () => resolve(response({message: 'started'}));}) : online(path);
  const pending = ui.nodes.get('launch').onclick();
  await [...ui.timers.values()].find(timer => timer.delay === 2000).fn();
  assert.match(ui.nodes.get('draftStatus').textContent, /校验并启动 Harness.*2 秒/);
  assert.ok(ui.calls.some(call => call.url === '/api/operations' && call.method === 'GET'));
  finish(); await pending;
  assert.equal(ui.run('model.operation.current'), null);
  assert.equal([...ui.timers.values()].some(timer => timer.delay === 5000 && timer.fn.name === 'abort'), false);
});

test('writes have a bounded five-minute wait and a timeout never retries the write', async () => {
  const ui = await app(); const online = ui.respond;
  ui.respond = (path, options) => options.method === 'POST'
    ? new Promise((resolve, reject) => options.signal.addEventListener('abort', () => {const error = new Error('timeout'); error.name = 'AbortError'; reject(error);})) : online(path);
  const pending = ui.nodes.get('launch').onclick();
  const timeout = [...ui.timers.values()].find(timer => timer.delay === 300000); assert.ok(timeout);
  timeout.fn(); await pending;
  assert.match(ui.nodes.get('toast').textContent, /后台可能仍在执行/);
  assert.equal(ui.run('model.connection.status'), 'error');
  await ui.run('pollConnection()');
  assert.equal(ui.calls.filter(call => call.method === 'POST').length, 1);
});
