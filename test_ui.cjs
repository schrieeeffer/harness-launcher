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
const sourceProof = (source='dsh-pet@0.3.8') => ({enabled:true,source,token:'source-proof',expiresAt:Date.now()/1000+300,reason:'verified'});
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

test('unverified sources remain blocked across state refresh and cannot submit', async () => {
  const ui = await app(); ui.nodes.get('trackingName').value = 'dsh-pet';
  await ui.run('refresh()');
  assert.equal(ui.nodes.get('trackingBegin').disabled, true);
  await ui.nodes.get('trackingForm').onsubmit({preventDefault(){}});
  assert.equal(ui.calls.some(c=>c.url==='/api/tracking/begin'), false);
});

test('restarted recording keeps saved snapshot visible without reporting a false zero backup', async () => {
  const ui = await app();
  ui.tracking.session = {id:'session-fixture',plugin:'example-plugin',phase:'installing',runState:'idle',baseline:'snapshots/saved.json',error:'reader conflict'};
  await ui.run('refresh()');
  assert.match(ui.nodes.get('trackingCounter').textContent,/快照已保存/);
  assert.doesNotMatch(ui.nodes.get('trackingCounter').textContent,/0 个文件/);
  ui.tracking.session.phase = 'verifying'; ui.tracking.session.count = 0;
  await ui.run('refresh()');
  assert.equal(ui.nodes.get('trackingCounter').textContent,'检测到 0 项改动');
});

test('plugins suspended after host update are not mislabeled as never recorded', async () => {
  const ui=await app();
  ui.state.plugins[0].tracking='untracked'; ui.state.plugins[0].suspended='缺少新版兼容声明';
  ui.state.patchMode={unknown:['plugin-a']};
  await ui.run('refresh()');
  assert.equal(ui.nodes.get('untrackedCount').textContent,'0');
  assert.equal(ui.nodes.get('suspendedGroup').hidden,false);
  assert.match(ui.nodes.get('suspendedRecords').textContent,/缺少新版兼容声明/);
  assert.doesNotMatch(ui.nodes.get('plugins').textContent,/未记录安装前状态/);
  assert.match(ui.nodes.get('patchDescription').textContent,/重新验证当前版本/);
  assert.doesNotMatch(ui.nodes.get('patchDescription').textContent,/已暂停组合切换/);
});

test('verified npm installs the pinned source and edited version immediately invalidates it', async () => {
  const ui = await app(), online = ui.respond;
  ui.nodes.get('trackingName').value = 'dsh-pet';
  ui.respond = (p,o) => p.startsWith('install-sources?') ? response(sourceProof()) : online(p,o);
  await ui.run("verifySource('npm')");
  assert.equal(ui.nodes.get('trackingBegin').disabled, false);
  await ui.nodes.get('trackingForm').onsubmit({preventDefault(){}});
  const body = JSON.parse(ui.calls.find(c=>c.url==='/api/tracking/begin').body);
  assert.equal(body.source, 'dsh-pet@0.3.8'); assert.equal(body.sourceToken, 'source-proof');
  ui.nodes.get('trackingSource').value='0.3.9'; ui.nodes.get('trackingSource').oninput();
  assert.equal(ui.nodes.get('trackingBegin').disabled,true);
  assert.equal(ui.run('sourceReady()'),false);
});

test('late source results do not enable a different plugin and failures are retryable', async () => {
  const ui = await app(); let finish;
  ui.nodes.get('trackingName').value='dsh-pet';
  ui.respond = () => new Promise(resolve=>{finish=()=>resolve(response(sourceProof()));});
  const pending=ui.run("verifySource('npm')");
  ui.nodes.get('trackingName').value='another'; ui.nodes.get('trackingName').oninput();
  finish(); await pending;
  assert.equal(ui.run('sourceReady()'),false);
  ui.respond=()=>response({error:'missing build'},400);
  await ui.run("verifySource('git')");
  assert.equal(ui.nodes.get('verifyGitSource').disabled,false);
  assert.match(ui.nodes.get('otherSourceFeedback').textContent,/missing build/);
  assert.equal(ui.run('sourceReady()'),false);
});

test('source lookup network failure does not freeze the launcher connection', async () => {
  const ui=await app(); ui.nodes.get('trackingName').value='dsh-pet';
  ui.respond=()=>{throw new TypeError('offline registry');};
  await ui.run("verifySource('npm')");
  assert.equal(ui.run('model.connection.status'),'online');
  assert.equal(ui.nodes.get('verifyNpmSource').disabled,false);
  assert.equal(ui.nodes.get('trackingBegin').disabled,true);
});

test('expired source is blocked and advanced recording must be explicitly chosen', async () => {
  const ui=await app(); ui.nodes.get('trackingName').value='dsh-pet';
  ui.run("selectedSource='npm'; sourceProofs.npm={enabled:true,key:sourceKey('npm'),expiresAt:1}; renderTracking()");
  assert.equal(ui.nodes.get('trackingBegin').disabled,true);
  ui.nodes.get('manualRecordMode').checked=true; ui.nodes.get('manualRecordMode').onchange();
  await ui.nodes.get('trackingForm').onsubmit({preventDefault(){}});
  const body=JSON.parse(ui.calls.find(c=>c.url==='/api/tracking/begin').body);
  assert.equal(body.source,''); assert.equal(body.mode,'record');
});

test('turning all plugins off stays a draft and does not disconnect or start anything', async () => {
  const ui = await app(); const requests = ui.calls.length;
  ui.nodes.get('selectNone').onclick();
  assert.equal(ui.run('model.draft.ids.length'), 0);
  assert.equal(ui.nodes.get('launch').disabled, false);
  assert.equal(ui.nodes.get('apply').disabled, false);
  assert.equal(ui.run('model.connection.status'), 'online');
  assert.equal(ui.calls.length, requests);
});

test('pending maintenance blocks launch without blocking access to recovery', async () => {
  const ui = await app();
  ui.state.maintenance = {pending: {name:'plugin-a', phase:'failed'}, history:[]};
  const online = ui.respond;
  ui.respond = path => path === 'maintenance' ? response(ui.state.maintenance) : online(path);
  await ui.run('refresh()');
  assert.equal(ui.nodes.get('launch').disabled, true);
  await ui.nodes.get('maintenanceButton').onclick();
  assert.equal(ui.nodes.get('maintenanceDialog').open, true);
  assert.match(ui.nodes.get('maintenancePending').textContent, /恢复到维护前/);
});

test('maintenance preview is read-only and execution uses the reviewed exact token', async () => {
  const ui = await app(); const online = ui.respond;
  ui.respond = path => path === 'maintenance/preview'
    ? response({token:'reviewed', files:[], blocked:[], message:'Preview'})
    : path === 'maintenance' ? response({pending:null,history:[]}) : online(path);
  await ui.run("previewPlugin('plugin-a', 'uninstall')");
  assert.equal(ui.calls.some(c=>c.url==='/api/maintenance/execute'), false);
  assert.equal(ui.nodes.get('maintenanceConfirm').textContent, '确认卸载');
  assert.match(ui.nodes.get('maintenancePreviewNotice').textContent, /尚未卸载/);
  assert.equal(ui.nodes.get('maintenanceFileDetails').open, false);
  assert.match(ui.nodes.get('maintenanceFilesLabel').textContent, /插件额外修改的文件（0 项）/);
  ui.nodes.get('maintenancePreview').close();
  assert.equal(ui.calls.some(c=>c.url==='/api/maintenance/execute'), false);
  await ui.run("previewPlugin('plugin-a', 'uninstall')");
  await ui.nodes.get('maintenanceConfirm').onclick();
  const writes = ui.calls.filter(c=>c.url==='/api/maintenance/execute');
  assert.equal(writes.length, 1);
  assert.deepEqual(JSON.parse(writes[0].body), {name:'plugin-a',action:'uninstall',token:'reviewed'});
});

test('a blocked maintenance preview cannot be confirmed in the UI', async () => {
  const ui = await app();
  ui.run("showMaintenancePreview('Preview', {message:'conflict',blocked:['shared dependency'],files:[]}, ()=>{})");
  assert.equal(ui.nodes.get('maintenanceConfirm').disabled, true);
  assert.match(ui.nodes.get('maintenanceBlocked').textContent, /shared dependency/);
  await ui.nodes.get('maintenanceConfirm').onclick();
  assert.equal(ui.calls.some(c=>c.url==='/api/maintenance/execute'), false);
});

test('maintenance cleanup with no candidates never enables a deletion', async () => {
  const ui = await app(); const online = ui.respond;
  ui.respond = path => path === 'maintenance/gc-preview' ? response({token:'empty',count:0,bytes:0,files:[]}) : online(path);
  await ui.nodes.get('previewCleanup').onclick();
  assert.equal(ui.nodes.get('maintenanceConfirm').textContent, '确认清理缓存');
  assert.equal(ui.nodes.get('maintenanceConfirm').disabled, true);
  assert.match(ui.nodes.get('maintenancePreviewNotice').textContent, /没有可清理/);
  await ui.nodes.get('maintenanceConfirm').onclick();
  assert.equal(ui.calls.some(c=>c.url==='/api/maintenance/gc'), false);
});

test('maintenance confirmation uses the current action after switching review dialogs', async () => {
  const ui = await app(); const online = ui.respond;
  ui.respond = path => path === 'maintenance/preview' ? response({token:'upgrade',files:[],blocked:[],message:'update'})
    : path === 'maintenance/gc-preview' ? response({token:'cleanup',count:2,bytes:2097152,files:[{store:'tracking',path:'blobs/a'},{store:'tracking',path:'blobs/b'}]})
    : path === 'maintenance' ? response({pending:null,history:[]}) : online(path);
  await ui.run("previewPlugin('plugin-a', 'upgrade', 'plugin-a@2')");
  assert.equal(ui.nodes.get('maintenanceConfirm').textContent, '确认更新');
  ui.nodes.get('maintenancePreview').close();
  await ui.run("previewPlugin('plugin-a', 'upgrade', 'plugin-a@1', true)");
  assert.equal(ui.nodes.get('maintenanceConfirm').textContent, '确认重新验证');
  ui.nodes.get('maintenancePreview').close();
  await ui.nodes.get('previewCleanup').onclick();
  assert.match(ui.nodes.get('maintenancePreviewNotice').textContent, /尚未清理.*2 项缓存.*2.0 MB/);
  assert.equal(ui.nodes.get('maintenanceFileDetails').open, false);
  assert.equal(ui.calls.some(c=>c.url==='/api/maintenance/gc'), false);
  await ui.nodes.get('maintenanceConfirm').onclick();
  assert.equal(ui.calls.some(c=>c.url==='/api/maintenance/execute'), false);
  const writes = ui.calls.filter(c=>c.url==='/api/maintenance/gc');
  assert.equal(writes.length, 1);
  assert.deepEqual(JSON.parse(writes[0].body), {token:'cleanup'});
});

test('maintenance shows live phase counts while the write is pending and clears afterward', async () => {
  const ui = await app(); const online = ui.respond; let finish;
  ui.nodes.get('maintenanceDialog').showModal();
  ui.respond = (path, options) => path === 'operations' ? response({current: {kind:'maintenance/execute', durationMs:42000,
    progress:{step:2,steps:5,label:'保存检查点',completed:123,total:null,unit:'files'}}})
    : path === 'maintenance/execute' ? new Promise(resolve => {finish = () => resolve(response({message:'complete'}));}) : online(path);
  const pending = ui.run("act(() => api('maintenance/execute', {}, 0))");
  assert.equal(ui.nodes.get('maintenanceProgress').hidden, false);
  await [...ui.timers.values()].find(timer => timer.delay === 2000).fn();
  assert.equal(ui.nodes.get('maintenanceLabel').textContent, '保存检查点');
  assert.match(ui.nodes.get('maintenanceDetail').textContent, /123.*总量待确认/);
  assert.equal(ui.nodes.get('maintenanceTrack')['aria-valuenow'], '1');
  assert.equal(ui.nodes.get('maintenanceElapsed').textContent, '42 秒');
  assert.equal(ui.nodes.get('maintenanceHint').hidden, false);
  finish(); await pending;
  assert.equal(ui.nodes.get('maintenanceProgress').hidden, true);
  assert.equal(ui.calls.filter(c => c.url === '/api/maintenance/execute').length, 1);
});

test('maintenance first run remains visibly unfinished and same-version suspended plugins have recovery action', async () => {
  const ui = await app();
  ui.state.plugins[0].suspended = '需要当前宿主验证';
  await ui.run('refresh()');
  ui.run("maintenanceUI.updates = [{name:'plugin-a',installed:'1',status:'revalidate',latest:'1',target:'plugin-a@1'}]; renderMaintenance()");
  assert.match(ui.nodes.get('maintenanceList').textContent, /重新验证当前版本/);
  ui.run("maintenanceUI.status = {pending:{name:'plugin-a',phase:'waiting'},history:[]}; renderMaintenance()");
  assert.equal(ui.nodes.get('maintenanceProgress').hidden, false);
  assert.equal(ui.nodes.get('maintenanceLabel').textContent, '等待首次运行与退出验证');
  assert.match(ui.nodes.get('maintenanceTrack')['aria-valuetext'], /尚未完成验证/);
  const actions = ui.nodes.get('maintenanceList').children[0].children.find(c => c.children.some(b => b.textContent === '重新验证当前版本…'));
  assert.ok(actions.children[0].disabled);
});

test('maintenance discards outdated update actions after install or revalidation', async () => {
  const ui = await app();
  ui.run("maintenanceUI.updates = [{name:'plugin-a',installed:'1',status:'available',latest:'2',target:'plugin-a@2'}]; renderMaintenance()");
  assert.match(ui.nodes.get('maintenanceList').textContent, /更新到 2/);
  ui.state.plugins[0].version = '2';
  await ui.run('refresh()'); ui.run('renderMaintenance()');
  assert.doesNotMatch(ui.nodes.get('maintenanceList').textContent, /更新到 2/);
  assert.match(ui.nodes.get('maintenanceList').textContent, /安装状态已变化/);
  ui.state.plugins[0].suspended = 'needs validation';
  await ui.run('refresh()');
  ui.run("maintenanceUI.updates = [{name:'plugin-a',installed:'2',status:'revalidate',latest:'2',target:'plugin-a@2'}]; renderMaintenance()");
  assert.match(ui.nodes.get('maintenanceList').textContent, /重新验证当前版本/);
  ui.state.plugins[0].suspended = '';
  await ui.run('refresh()'); ui.run('renderMaintenance()');
  assert.doesNotMatch(ui.nodes.get('maintenanceList').textContent, /重新验证当前版本/);
});

test('failed maintenance refreshes the persisted recovery state without retrying installation', async () => {
  const ui = await app(); const online = ui.respond;
  ui.state.maintenance = {pending:{name:'plugin-a',phase:'failed',error:'fixture conflict'},history:[]};
  ui.respond = path => path === 'maintenance/execute' ? response({error:'fixture conflict'},400)
    : path === 'maintenance' ? response(ui.state.maintenance) : online(path);
  ui.run("showMaintenancePreview('upgrade', {files:[]}, () => api('maintenance/execute', {}, 0))");
  await ui.nodes.get('maintenanceConfirm').onclick();
  assert.match(ui.nodes.get('maintenancePending').textContent, /fixture conflict.*恢复到维护前/);
  assert.equal(ui.calls.filter(c => c.url === '/api/maintenance/execute').length, 1);
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
  const pending = ui.run("act(() => api('scheme', {}))");
  const timeout = [...ui.timers.values()].find(timer => timer.delay === 300000); assert.ok(timeout);
  timeout.fn(); await pending;
  assert.match(ui.nodes.get('toast').textContent, /后台可能仍在执行/);
  assert.equal(ui.run('model.connection.status'), 'error');
  await ui.run('pollConnection()');
  assert.equal(ui.calls.filter(call => call.method === 'POST').length, 1);
});

test('launch shows measured stage counts and does not time out a working file check', async () => {
  const ui = await app(); const online = ui.respond; let finish;
  ui.respond = (path, options) => path === 'operations' ? response({current: {id: 'long-launch', kind: 'launch', durationMs: 361000,
    progress: {step: 3, steps: 5, label: '校验当前组合文件', completed: 20, total: 40, unit: 'files'}}})
    : options.method === 'POST' ? new Promise(resolve => {finish = () => resolve(response({message: 'started', verifiedCurrent: true}));}) : online(path);
  const pending = ui.nodes.get('launch').onclick();
  assert.equal([...ui.timers.values()].some(timer => timer.delay === 300000), false);
  await [...ui.timers.values()].find(timer => timer.delay === 2000).fn();
  assert.equal(ui.nodes.get('launchProgress').hidden, false);
  assert.equal(ui.nodes.get('progressLabel').textContent, '校验当前组合文件');
  assert.equal(ui.nodes.get('progressDetail').textContent, '20 / 40 个文件');
  assert.equal(ui.nodes.get('progressElapsed').textContent, '6 分 1 秒');
  assert.equal(ui.nodes.get('progressTrack')['aria-valuenow'], '2.5');
  assert.equal(ui.nodes.get('progressHint').hidden, false);
  finish(); await pending;
  assert.equal(ui.nodes.get('launchProgress').hidden, true);
  assert.match(ui.nodes.get('toast').textContent, /当前组合校验通过/);
  assert.equal(ui.calls.filter(call => call.method === 'POST').length, 1);
});

test('apply-only control explains that toggles do not immediately modify Harness', async () => {
  const ui = await app();
  assert.equal(ui.nodes.get('apply').textContent, '仅应用，不启动');
  assert.match(ui.nodes.get('selectionDescription').textContent, /开关只调整待选组合/);
  ui.nodes.get('selectNone').onclick();
  assert.equal(ui.nodes.get('apply').textContent, '仅还原，不启动');
});
test('runtime missing disables launch but keeps settings repair accessible', async () => {
  const ui = await app();
  ui.state.runtimeHealth = {status: 'missing', message: 'missing official runtime', missing: ['SKILL.md']};
  await ui.run('refresh()');
  assert.equal(ui.nodes.get('launch').disabled, true);
  assert.equal(ui.nodes.get('runtimeWarning').hidden, false);
  await ui.nodes.get('runtimeSettings').onclick();
  assert.equal(ui.nodes.get('settingsDialog').open, true);
});

test('runtime preview never repairs automatically and conflicts disable execution', async () => {
  const ui = await app(); const online = ui.respond;
  ui.respond = (path, options) => path === 'runtime/prepare' ? response({token: 'p1', version:'0.2.1-rc.3', message:'conflicts', missing:['a'], conflicts:['b'], blocked:['changed']}) : online(path, options);
  await ui.nodes.get('runtimePrepare').onclick();
  assert.equal(ui.nodes.get('runtimeExecute').disabled, true);
  assert.match(ui.nodes.get('runtimeRepairFiles').textContent, /不会覆盖/);
  await ui.nodes.get('runtimeExecute').onclick();
  assert.equal(ui.calls.filter(c=>c.url.includes('runtime/execute')).length, 0);
});

test('runtime repair requires preview token and clears preview after successful commit', async () => {
  const ui = await app(); const online = ui.respond;
  ui.respond = (path, options) => path === 'runtime/prepare' ? response({token:'p2', version:'0.2.1-rc.3', message:'verified', missing:['a'], conflicts:[], blocked:[]})
    : path === 'runtime/execute' ? response({message:'restored', restored:1}) : online(path, options);
  await ui.nodes.get('runtimePrepare').onclick();
  assert.equal(ui.nodes.get('runtimeExecute').disabled, false);
  assert.equal(ui.calls.filter(c=>c.url.includes('runtime/execute')).length, 0);
  await ui.nodes.get('runtimeExecute').onclick();
  const calls = ui.calls.filter(c=>c.url.includes('runtime/execute'));
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(calls[0].body), {token:'p2'});
  assert.equal(ui.nodes.get('runtimeExecute').hidden, true);
});

test('changed official host warns and blocks launch before a request is sent', async () => {
  const ui = await app();
  ui.state.hostHealth = {status:'changed', message:'official update needs verification'};
  await ui.run('refresh()');
  assert.equal(ui.nodes.get('hostWarning').hidden, false);
  assert.equal(ui.nodes.get('launch').disabled, true);
  assert.equal(ui.nodes.get('apply').disabled, true);
  await ui.nodes.get('hostSettings').onclick();
  assert.equal(ui.nodes.get('settingsDialog').open, true);
});

test('native-only approval without official cache keeps recording blocked with an explanation', async () => {
  const ui = await app();
  ui.state.enabled = [];
  ui.state.hostHealth = {status:'native_ready', message:'native approved'};
  await ui.run('refresh(true)');
  ui.nodes.get('selectNone').onclick();
  assert.equal(ui.nodes.get('launch').disabled, false);
  assert.equal(ui.nodes.get('trackingBegin').disabled, true);
  assert.match(ui.nodes.get('trackingGate').textContent, /验证并接续新版基线/);
  assert.equal(ui.nodes.get('trackingSettings').hidden, false);
  ui.nodes.get('selectAll').onclick();
  assert.equal(ui.nodes.get('launch').disabled, true);
  assert.equal(ui.nodes.get('apply').disabled, true);
});

test('native-only approval with a verified-cache path offers baseline preparation before installation', async () => {
  const ui = await app();
  ui.state.enabled = [];
  ui.state.hostHealth = {status:'native_ready', message:'native approved'};
  ui.state.officialUpdate = {pending:null, autoAvailable:true};
  ui.nodes.get('trackingName').value = 'plugin-a';
  ui.run("selectedSource = 'npm'; sourceProofs.npm = {enabled:true, key:sourceKey('npm'), expiresAt:Date.now()/1000+300, source:'plugin-a@1.0.0', reason:'verified', token:'proof'}");
  await ui.run('refresh(true)');
  assert.equal(ui.nodes.get('trackingBegin').disabled, false);
  assert.equal(ui.nodes.get('trackingBegin').textContent, '验证新版并安装');
  assert.match(ui.nodes.get('trackingGate').textContent, /先验证官方缓存/);
  assert.match(ui.nodes.get('draftStatus').textContent, /需验证基线/);
  assert.equal(ui.calls.filter(c=>c.method==='POST').length, 0);
});

test('previous plugin completion is hidden when choosing a different plugin', async () => {
  const ui = await app();
  ui.tracking.completed = {plugin:'dsh-whale-widget', count:0};
  ui.nodes.get('trackingName').value = '@sunshaobo/dsh-session-manager';
  await ui.run('refresh()');
  assert.equal(ui.nodes.get('trackingCompleted').hidden, true);
  ui.nodes.get('trackingName').value = 'dsh-whale-widget';
  ui.nodes.get('trackingName').oninput();
  assert.equal(ui.nodes.get('trackingCompleted').hidden, false);
});

test('installation preparation reports live counts instead of a silent disabled button', async () => {
  const ui = await app();
  ui.run("store.dispatch({type:'operation/start'})");
  ui.run("store.dispatch({type:'operation/observed', epoch:model.operation.epoch, current:{kind:'tracking/begin', durationMs:12000, progress:{label:'复核官方文件',completed:12,total:30}}})");
  assert.equal(ui.nodes.get('trackingBegin').disabled, true);
  assert.match(ui.nodes.get('trackingGate').textContent, /复核官方文件.*12 \/ 30.*12 秒/);
  assert.equal(ui.nodes.get('trackingCompleted').hidden, true);
});

test('failed install preparation keeps the reason in the form and never repeats the write', async () => {
  const ui = await app(); const normal = ui.respond;
  ui.respond = (path, options) => path==='tracking/begin' ? response({error:'新版文件存在冲突，未安装'},400) : normal(path,options);
  await ui.run("trackingAction('begin',{name:'plugin-new',source:'plugin-new@1.0.0'})");
  assert.equal(ui.nodes.get('trackingRequestError').hidden, false);
  assert.match(ui.nodes.get('trackingRequestError').textContent, /冲突/);
  assert.equal(ui.calls.filter(c=>c.url==='/api/tracking/begin').length, 1);
  assert.equal(ui.run('model.operation.busy'), false);
});

test('official host verification only previews until explicit accept with token', async () => {
  const ui = await app(); const online = ui.respond;
  ui.respond = (path, options) => path === 'official-host/prepare' ? response({token:'host1', message:'official verified'})
    : path === 'official-host/accept' ? response({message:'native baseline saved'}) : online(path, options);
  await ui.nodes.get('hostPrepare').onclick();
  assert.equal(ui.nodes.get('hostAccept').hidden, false);
  assert.equal(ui.calls.filter(c=>c.url.includes('official-host/accept')).length, 0);
  await ui.nodes.get('hostAccept').onclick();
  assert.deepEqual(JSON.parse(ui.calls.find(c=>c.url.includes('official-host/accept')).body), {token:'host1'});
  assert.equal(ui.nodes.get('hostAccept').hidden, true);
});

test('official update preview never installs and execute consumes its exact token once', async () => {
  const ui = await app(); const online = ui.respond;
  ui.respond = (path, options) => path === 'official-update/prepare' ? response({token:'upgrade-1', message:'verified preview'})
    : path === 'official-update/execute' ? response({message:'adopted', suspended:{'dsh-purge':'requires new patch'}}) : online(path, options);
  await ui.nodes.get('officialUpdatePrepare').onclick();
  assert.equal(ui.nodes.get('officialUpdateExecute').hidden, false);
  assert.equal(ui.calls.filter(c=>c.url.includes('official-update/execute')).length, 0);
  await ui.nodes.get('officialUpdateExecute').onclick();
  await ui.nodes.get('officialUpdateExecute').onclick();
  const writes = ui.calls.filter(c=>c.url.includes('official-update/execute'));
  assert.equal(writes.length, 1);
  assert.deepEqual(JSON.parse(writes[0].body), {token:'upgrade-1'});
  assert.match(ui.nodes.get('officialUpdateStatus').textContent, /dsh-purge/);
});

test('cached official update enables launch reconciliation while apply remains guarded', async () => {
  const ui = await app();
  ui.state.hostHealth = {status:'changed'};
  ui.state.runtimeHealth = {status:'missing'};
  ui.state.officialUpdate = {pending:null, autoAvailable:true};
  await ui.run('refresh()');
  assert.equal(ui.nodes.get('launch').disabled, false);
  assert.equal(ui.nodes.get('apply').disabled, true);
  assert.equal(ui.calls.filter(c=>c.url.includes('official-update/reconcile')).length, 0);
});

test('pending official install blocks launch and exposes recovery without retrying installation', async () => {
  const ui = await app();
  ui.state.officialUpdate = {pending:{phase:'installing',version:'0.3.0',error:'installer still active'}};
  await ui.run('refresh()');
  assert.equal(ui.nodes.get('launch').disabled, true);
  assert.equal(ui.nodes.get('officialUpdateAbort').hidden, false);
  assert.match(ui.nodes.get('officialUpdateStatus').textContent, /installer still active/);
  assert.equal(ui.calls.filter(c=>c.url.includes('official-update/execute')).length, 0);
});

test('suspended plugins do not block native launch and cannot be selected by select all', async () => {
  const ui = await app();
  ui.state.enabled = [];
  ui.state.plugins[0].suspended = 'not compatible with current host';
  ui.state.plugins[0].tracking = 'untracked';
  await ui.run('refresh(true)');
  ui.nodes.get('selectAll').onclick();
  assert.deepEqual(Array.from(ui.run('model.draft.ids')), []);
  assert.equal(ui.nodes.get('launch').disabled, false);
});
