'use strict';
const $ = id => document.getElementById(id);
const token = new URLSearchParams(location.search).get('token') || sessionStorage.getItem('harness-menu-token') || '';
sessionStorage.setItem('harness-menu-token', token);
let state = null, selected = new Set(), mode = 'current', activeScheme = '', busy = false, timer, tracking = null, trackingWasWorking = false;
function ask(message) { return new Promise(resolve => { const dialog = $('confirmDialog'); $('confirmMessage').textContent = message; let accepted = false; $('confirmAccept').onclick = () => { accepted = true; dialog.close(); }; $('confirmCancel').onclick = () => dialog.close(); dialog.addEventListener('close', () => resolve(accepted), {once: true}); dialog.showModal(); $('confirmCancel').focus(); }); }
function element(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
function toast(message, error = false) { clearTimeout(timer); $('toast').textContent = message; $('toast').className = 'toast' + (error ? ' error' : ''); $('toast').hidden = false; timer = setTimeout(() => $('toast').hidden = true, error ? 8500 : 5000); }
async function api(path, body) {
  const response = await fetch('/api/' + path, {method: body === undefined ? 'GET' : 'POST', headers: {'Authorization': 'Bearer ' + token, ...(body === undefined ? {} : {'Content-Type': 'application/json'})}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
  const data = await response.json(); if (!response.ok) throw new Error(data.error || '请求失败'); return data;
}
async function act(fn) { if (busy) return; busy = true; updateSummary(); try { await fn(); } catch (error) { toast(error.message, true); } finally { busy = false; updateSummary(); renderTracking(); } }
function availableIds() { return state.plugins.filter(p => p.available).map(p => p.id); }
function selection() { return state.plugins.filter(p => selected.has(p.id)).map(p => p.id); }
function isChanged() { return state && (state.enabled.length !== selected.size || state.enabled.some(x => !selected.has(x))); }
function needsPatchSwitch() { const p = state?.patchMode; return p?.supported && (p.pending || p.mode !== (selected.has('dsh-purge') ? 'plugin' : 'native')); }
async function refresh(reset = false) {
  const next = await api('state'); state = next;
  tracking = state.tracking;
  if (reset) { selected = new Set(state.enabled); mode = 'current'; activeScheme = ''; }
  else { const ids = new Set(state.plugins.map(p => p.id)); selected = new Set([...selected].filter(x => ids.has(x))); }
  $('inlineError').hidden = true;
  $('patchNote').hidden = !state.patchMode?.supported && !state.patchMode?.unknown?.length;
  $('openProfile').textContent = state.home;
  renderSchemes(); renderPlugins(); updateSummary(); renderTracking();
}
function setPreset(preset) {
  if (!state) return;
  mode = preset; activeScheme = '';
  selected = new Set(preset === 'none' ? [] : preset === 'all' ? availableIds() : state.enabled);
  renderSchemes(); renderPlugins(); updateSummary();
}
function useScheme(name) {
  mode = 'scheme'; activeScheme = name;
  const wanted = state.schemes[name], known = new Set(state.plugins.map(p => p.id));
  selected = new Set(wanted.filter(x => known.has(x)));
  const missing = wanted.filter(x => !known.has(x));
  if (missing.length) toast('此方案中的插件已不在本机：' + missing.join('、'), true);
  renderSchemes(); renderPlugins(); updateSummary();
}
function renderSchemes() {
  $('schemes').replaceChildren();
  document.querySelectorAll('[data-preset]').forEach(n => n.classList.toggle('active', n.dataset.preset === mode));
  const names = Object.keys(state.schemes);
  if (!names.length) $('schemes').append(element('div', 'no-schemes', '保存常用插件组合\n下次一键选用'));
  for (const name of names) {
    const row = element('div', 'scheme-row'); const button = element('button', 'nav' + (activeScheme === name && mode === 'scheme' ? ' active' : ''));
    button.append(element('span', '', '▱'), element('div', 'scheme-name', name)); button.title = name; button.onclick = () => useScheme(name);
    const remove = element('button', 'scheme-delete', '×'); remove.title = '删除方案 ' + name; remove.setAttribute('aria-label', remove.title);
    remove.onclick = async () => { if (await ask('删除方案「' + name + '」？只删除菜单中的组合记录。')) act(async () => { await api('scheme', {name, delete: true}); if (activeScheme === name) {mode = 'custom'; activeScheme = '';} await refresh(); toast('方案已删除'); }); };
    row.append(button, remove); $('schemes').append(row);
  }
}
function renderPlugins() {
  const query = $('search').value.toLowerCase().trim(); $('plugins').replaceChildren(); $('pluginCount').textContent = state.plugins.length;
  const visible = state.plugins.filter(p => (p.id + ' ' + p.description).toLowerCase().includes(query));
  if (!visible.length) { $('plugins').append(element('div', 'empty', query ? '没有匹配的已安装插件。试试「发现插件」搜索在线目录。' : '还没有额外插件。点击「发现插件」浏览官方与社区作品，也可手动添加。')); return; }
  for (const p of visible) {
    const card = element('article', 'plugin-card' + (selected.has(p.id) ? ' selected' : ''));
    const icon = element('div', 'plugin-icon', p.id === 'dsh-purge' ? 'P' : p.id.split('/').pop().replace(/^dsh-/, '').slice(0, 1).toUpperCase());
    const info = element('div', 'plugin-info'); const heading = element('div', 'plugin-heading');
    heading.append(element('strong', '', p.id), element('span', 'badge', 'v' + p.version));
    heading.append(element('span', 'badge' + (p.tracking !== 'untracked' ? ' on' : ''), p.tracking === 'auto' ? '已自动记录补丁' : p.id === 'dsh-purge' ? '支持自动还原补丁' : '未记录安装前状态'));
    if (!p.available) heading.append(element('span', 'badge error', '文件缺失'));
    else if (state.enabled.includes(p.id)) heading.append(element('span', 'badge on', '当前已启用'));
    const description = element('p', 'plugin-description', p.description); description.title = p.description;
    const meta = element('div', 'plugin-meta'); meta.append(element('span', '', p.available ? '本机已安装' : p.error));
    const pathButton = element('button', '', '查看路径'); const path = element('p', 'plugin-description plugin-path', p.path);
    pathButton.onclick = () => { path.classList.toggle('expanded'); pathButton.textContent = path.classList.contains('expanded') ? '收起路径' : '查看路径'; };
    meta.append(element('span', '', '·'), pathButton); info.append(heading, description, meta, path);
    const label = element('label', 'switch'); const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = selected.has(p.id); checkbox.disabled = !p.available && !checkbox.checked; checkbox.setAttribute('aria-label', '加载 ' + p.id);
    checkbox.onchange = () => { if (checkbox.checked) selected.add(p.id); else selected.delete(p.id); mode = 'custom'; activeScheme = ''; renderSchemes(); renderPlugins(); updateSummary(); };
    label.append(checkbox, element('span', 'switch-track')); card.append(icon, info, label); $('plugins').append(card);
  }
}
function updateSummary() {
  if (!state) return;
  $('selectedCount').textContent = selected.size;
  const titles = {current: '当前配置', none: '原生模式（全部关闭）', all: '全部可用插件', custom: '自定义组合', scheme: activeScheme};
  $('selectionTitle').textContent = titles[mode] || '自定义组合';
  $('selectionDescription').textContent = selected.size ? '启用插件时自动恢复已校验的补丁快照。' : '应用或启动时还原已适配补丁，保留账号与会话。';
  const patch = state.patchMode || {};
  $('patchTitle').textContent = patch.error ? '补丁切换需要处理' : patch.verifiedNative && !selected.size ? '已切换原生模式 · 启动前再次核验' : !selected.size ? '将还原补丁并切换官方文件' : '补丁与插件开关自动同步';
  $('patchDescription').textContent = patch.error || (patch.unknown?.length ? '以下插件缺少安装前记录，不能保证还原：' + patch.unknown.join('、') : !selected.size ? '按逆序还原已记录文件，再切换官方归档；重新启用时按安装顺序恢复。' : '内置适配与自动记录统一切换；版本变化、记录外改动、共享文件冲突时停止覆盖。');
  $('runtime').className = 'status' + (state.running ? ' running' : ''); $('runtime').replaceChildren(element('i'), document.createTextNode(state.running ? 'Harness 运行中' : 'Harness 未运行'));
  $('draftStatus').className = 'draft-status' + (isChanged() || needsPatchSwitch() ? ' changed' : '');
  $('draftStatus').textContent = busy ? '正在校验与切换补丁…' : state.running ? '切换前请完全退出 Harness' : isChanged() || needsPatchSwitch() ? '待应用 · 同步插件与补丁' : '与当前配置一致';
  const recording = Boolean(tracking?.session || tracking?.busy);
  $('launch').disabled = busy || recording || state.running || !state.executableExists;
  $('launch').title = state.running ? '请先退出 Harness（包括托盘）' : !state.executableExists ? '请在设置中确认应用路径' : '备份配置并启动 Harness';
  $('apply').disabled = busy || recording || state.running || !(isChanged() || needsPatchSwitch());
  $('apply').textContent = selected.size ? '应用选择' : '还原为原生';
  $('saveScheme').disabled = busy; $('refresh').disabled = busy;
  document.querySelectorAll('[data-restore]').forEach(button => button.disabled = state.running || busy || recording);
}

function renderTracking() {
  if (!tracking) return;
  const session = tracking.session;
  $('trackingForm').hidden = Boolean(session);
  $('trackingActive').hidden = !session;
  $('trackingBanner').hidden = !session;
  $('trackingBegin').disabled = busy || tracking.busy || Boolean(state?.running);
  $('trackingScopes').replaceChildren(...tracking.scopes.map(path => element('li', '', path)));
  $('trackingRecords').replaceChildren();
  const entries = Object.entries(tracking.records);
  if (!entries.length) $('trackingRecords').append(element('p', 'small muted', '还没有通用记录。dsh-purge 继续使用已有的内置适配。'));
  for (const [name, record] of entries) {
    const row = element('div', 'backup'), info = element('div');
    info.append(element('strong', '', name + ' · v' + record.version), element('p', '', record.count + ' 处差异 · ' + (record.enabled ? '补丁已启用' : '补丁已停用')));
    if (record.baseSelection.length) info.append(element('p', '', '记录时的基础组合：' + record.baseSelection.join('、')));
    if (record.historyCount) info.append(element('p', '', '保留 ' + record.historyCount + ' 份历史记录'));
    const upgrade = element('button', 'button', '升级 / 重新记录');
    upgrade.disabled = busy || tracking.busy || Boolean(session) || Boolean(state?.running);
    upgrade.onclick = () => { $('trackingName').value = name; $('trackingSource').value = ''; $('trackingSource').focus(); toast('填写新版安装来源，或留空后自行升级。开始时会先撤销旧版补丁。'); };
    row.append(info, upgrade); $('trackingRecords').append(row);
  }
  if (!session) return;
  const names = {preparing: '建立安装前快照', installing: '通过官方安装器安装', recording: '记录安装与首次测试', scanning: '对比文件差异', review: '复核变更归属'};
  $('trackingStageTitle').textContent = session.plugin + ' · ' + (names[session.phase] || session.phase);
  $('trackingBannerTitle').textContent = $('trackingStageTitle').textContent;
  const instructions = {preparing: '请等待快照准备完成后再安装插件。首次可能需要复制较多文件。', installing: '正在调用官方插件安装命令，快照与安装日志会保留在本机。', recording: '现在只安装 / 测试此插件。可以启动测试并应用插件自身的设置，完全退出 Harness 后再扫描差异。', scanning: '正在比较快照；未变化的文件会复用已有内容。', review: '以下是记录窗口内的全部差异，并不等于已证明由插件进程独自写入。确认期间只操作了此插件、清单归属正确后保存。'};
  $('trackingInstruction').textContent = instructions[session.phase] || '';
  $('trackingBannerText').textContent = instructions[session.phase] || '';
  $('trackingCounter').textContent = session.phase === 'review' ? session.count + ' 处变更' : tracking.progress.count + ' 个文件 · ' + Math.round(tracking.progress.bytes / 1024 / 1024) + ' MB';
  $('trackingPath').textContent = tracking.busy ? tracking.progress.path : '';
  $('trackingError').hidden = !session.error; $('trackingError').textContent = session.error || '';
  const canFinish = session.baseline && !tracking.busy && !state?.running;
  $('trackingLaunch').disabled = tracking.busy || session.phase !== 'recording' || Boolean(state?.running);
  $('trackingFinish').disabled = !canFinish;
  $('trackingAccept').hidden = session.phase !== 'review'; $('trackingAccept').disabled = tracking.busy || Boolean(state?.running);
  $('trackingExport').hidden = session.phase !== 'review';
  $('trackingAbandon').disabled = tracking.busy;
  $('trackingReview').hidden = session.phase !== 'review';
  $('trackingReview').replaceChildren();
  if (session.phase === 'review') {
    if (!session.count) $('trackingReview').append(element('p', 'muted', '托管范围没有文件补丁。保存后将作为已记录的加载型插件管理。'));
    for (const item of tracking.review) {const row = element('div', 'tracking-change'); row.append(element('span', 'badge', item.change), element('code', '', item.path)); $('trackingReview').append(row);}
    if (session.count > tracking.review.length) $('trackingReview').append(element('p', 'small muted', '此处显示前 200 项，完整清单可导出查看。'));
  }
}

async function trackingAction(action, body = {}) {
  await act(async () => {const result = await api('tracking/' + action, body); tracking = await api('tracking'); if (state) state.tracking = tracking; renderTracking(); updateSummary(); toast(result.message); if (!tracking.busy && !tracking.session) await refresh(true);});
}
function openScheme() { if (!state) return; $('schemeName').value = ''; $('schemePreview').textContent = selected.size ? '包含：' + selection().join('、') : '此方案不加载额外插件。'; $('schemeError').hidden = true; $('schemeDialog').showModal(); $('schemeName').focus(); }
async function apply(launch) { await act(async () => { const result = await api(launch ? 'launch' : 'apply', {selection: selection(), revision: state.revision, native: selected.size === 0}); await refresh(true); toast(result.message + (result.patchMode === 'native' ? ' · 已还原补丁' : result.patchMode === 'plugin' ? ' · 补丁快照已恢复' : '')); if (launch) { state.running = true; updateSummary(); } }); }
function renderBackups() {
  $('backupList').replaceChildren();
  if (!state.backups.length) $('backupList').append(element('div', 'empty', '暂无备份。首次修改配置前会自动创建。'));
  for (const b of state.backups) {
    const row = element('div', 'backup'), info = element('div');
    info.append(element('strong', '', new Date(b.time).toLocaleString('zh-CN', {hour12: false})), element('p', '', b.reason + ' · ' + (b.plugins.length ? b.plugins.join('、') : '不加载额外插件')));
    const restore = element('button', 'button', '恢复'); restore.dataset.restore = 'true'; restore.disabled = state.running || busy;
    restore.onclick = async () => { if (await ask('恢复此备份的 package.json？恢复前会自动备份当前配置。')) act(async () => { const result = await api('restore', {id: b.id, revision: state.revision}); await refresh(true); renderBackups(); toast(result.message); }); };
    row.append(info, restore); $('backupList').append(row);
  }
}
document.querySelectorAll('[data-preset]').forEach(button => button.onclick = () => setPreset(button.dataset.preset));
document.querySelectorAll('[data-close]').forEach(button => button.onclick = () => $(button.dataset.close).close());
document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('click', event => {if (event.target === dialog) {const r = dialog.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) dialog.close();}}));
$('search').oninput = () => state && renderPlugins();
$('selectAll').onclick = () => setPreset('all'); $('selectNone').onclick = () => setPreset('none');
$('refresh').onclick = () => act(async () => { await refresh(); toast('插件列表已刷新，保留当前勾选'); });
$('saveScheme').onclick = openScheme; $('newScheme').onclick = openScheme;
$('schemeForm').onsubmit = async event => { event.preventDefault(); await act(async () => { const name = $('schemeName').value.trim(); if (Object.hasOwn(state.schemes, name) && !await ask('覆盖已有方案「' + name + '」？')) return; try {await api('scheme', {name, selection: selection()}); await refresh(); mode = 'scheme'; activeScheme = name; renderSchemes(); updateSummary(); $('schemeDialog').close(); toast('方案已保存');} catch (e) {$('schemeError').textContent = e.message; $('schemeError').hidden = false;} }); };
$('apply').onclick = () => apply(false); $('launch').onclick = () => apply(true);
$('trackingButton').onclick = () => {renderTracking(); $('trackingDialog').showModal();};
$('openTrackingProgress').onclick = () => {renderTracking(); $('trackingDialog').showModal();};
$('trackingForm').onsubmit = async event => {event.preventDefault(); await trackingAction('begin', {name: $('trackingName').value.trim(), source: $('trackingSource').value.trim()});};
$('trackingLaunch').onclick = () => trackingAction('launch');
$('trackingFinish').onclick = () => trackingAction('finish');
$('trackingAccept').onclick = () => trackingAction('accept');
$('trackingAbandon').onclick = async () => {if (await ask('放弃记录并保留当前文件和安装结果？此操作不会还原安装期间的改动。')) await trackingAction('abandon');};
$('trackingExport').onclick = () => act(async () => {const data = await api('tracking/diff'); const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}); const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'Harness-文件变更清单.json'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 3000);});

let catalogSource = 'all', catalogPage = 0, catalogLoading = false, catalogRequest = 0, detailRequest = 0, catalogAbort = null, detailAbort = null, currentDetail = null;
function externalLink(label, url) {
  try { const parsed = new URL(url); if (!['https:', 'http:'].includes(parsed.protocol) || parsed.username || parsed.password) return element('span', '', label); }
  catch (_) { return element('span', '', label); }
  const link = element('a', 'button subtle', label); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; return link;
}
async function catalogGet(path, parameters, signal) {
  const response = await fetch('/api/catalog/' + path + '?' + new URLSearchParams(parameters), {headers: {Authorization: 'Bearer ' + token}, signal});
  const data = await response.json(); if (!response.ok) throw new Error(data.error || '搜索服务暂不可用'); return data;
}
function catalogBusy(value) { catalogLoading = value; $('catalogResults').setAttribute('aria-busy', String(value)); $('catalogMore').disabled = value; $('catalogRetry').disabled = value; }
function catalogCard(item) {
  const card = element('article', 'catalog-card'), heading = element('div', 'catalog-card-heading');
  heading.append(element('div', 'catalog-icon', item.kind === 'official' ? 'D' : item.provider === 'github' ? 'G' : 'N'), element('span', 'badge' + (item.kind === 'official' ? ' on' : ''), item.kind === 'official' ? 'DeepSeek 官方' : item.provider === 'github' ? 'GitHub 社区' : 'npm 社区'));
  if (item.stars) heading.append(element('span', 'small muted', '★ ' + item.stars.toLocaleString()));
  card.append(heading, element('h3', '', item.title || item.name), element('code', 'catalog-package', item.name), element('p', 'catalog-description', item.description || '作者未提供描述。'));
  const installed = state?.plugins.find(p => p.id === item.name);
  const foot = element('div', 'catalog-card-footer'); foot.append(element('span', 'small muted', installed ? '本机已安装 · v' + installed.version : item.version ? 'v' + item.version : '详情中检查版本与安装结构'));
  const view = element('button', 'button', '查看详情'); view.onclick = () => openPluginDetail(item); foot.append(view); card.append(foot); return card;
}
async function searchCatalog(append = false) {
  const id = ++catalogRequest; catalogAbort?.abort(); catalogAbort = new AbortController(); catalogBusy(true);
  const page = append ? catalogPage + 1 : 0;
  if (!append) { $('catalogResults').replaceChildren(); $('catalogMore').hidden = true; }
  $('catalogRetry').hidden = true; $('catalogWarning').hidden = true; $('catalogStatus').textContent = '正在搜索公开插件目录…';
  try {
    const data = await catalogGet('search', {q: $('catalogQuery').value.trim(), source: catalogSource, page, registry: $('catalogRegistry').value}, catalogAbort.signal);
    if (id !== catalogRequest || !$('catalogDialog').open) return;
    catalogPage = page;
    $('catalogResults').querySelectorAll('.catalog-empty').forEach(n => n.remove());
    const known = new Set([...$('catalogResults').children].map(n => n.dataset.catalogKey));
    for (const item of data.items) {const key = item.provider + ':' + item.name; if (known.has(key)) continue; const card = catalogCard(item); card.dataset.catalogKey = key; $('catalogResults').append(card); known.add(key);}
    const count = $('catalogResults').children.length;
    if (!count) $('catalogResults').append(element('p', 'empty catalog-empty', '没有找到结果。试试英文关键词、完整包名，或切换搜索来源；也可手动添加。'));
    $('catalogStatus').textContent = '已展示 ' + count + ' 项' + (data.stale ? ' · 离线缓存' : data.cached ? ' · 近期缓存' : ' · 在线结果') + '。详情会检查能否独立安装。';
    $('catalogMore').hidden = !data.hasMore || page >= 20;
    $('catalogWarning').textContent = data.warning || ''; $('catalogWarning').hidden = !data.warning;
  } catch (error) {
    if (error.name !== 'AbortError' && id === catalogRequest) { $('catalogStatus').textContent = '搜索暂未完成，已有结果保留。'; $('catalogWarning').textContent = error.message; $('catalogWarning').hidden = false; $('catalogRetry').hidden = false; }
  } finally {if (id === catalogRequest) catalogBusy(false);}
}
function openCatalog() {if ($('trackingDialog').open) $('trackingDialog').close(); if (!$('catalogDialog').open) $('catalogDialog').showModal(); $('catalogQuery').focus(); if (!$('catalogResults').children.length) searchCatalog();}
function openManual() { $('pluginDetailDialog').close(); $('catalogDialog').close(); renderTracking(); if (!$('trackingDialog').open) $('trackingDialog').showModal(); $('trackingName').focus(); }
async function openPluginDetail(item) {
  const id = ++detailRequest; detailAbort?.abort(); detailAbort = new AbortController(); currentDetail = null;
  $('detailTitle').textContent = item.title || item.name; $('detailStatus').textContent = '正在读取包元数据…'; $('detailContent').replaceChildren(); $('detailAdd').disabled = true; $('detailAdd').textContent = '选用并准备安装';
  if (!$('pluginDetailDialog').open) $('pluginDetailDialog').showModal();
  try {
    const data = await catalogGet('detail', {name: item.name, provider: item.provider, registry: $('catalogRegistry').value}, detailAbort.signal);
    if (id !== detailRequest || !$('pluginDetailDialog').open) return;
    currentDetail = data;
    $('detailStatus').textContent = data.name + (data.version ? ' · v' + data.version : '') + (data.stale ? ' · 缓存，重新联网核实后再安装' : '');
    const description = element('p', 'muted', data.description || '作者未提供描述。');
    const meta = element('dl', 'detail-meta');
    for (const [label, value] of [['来源', data.provider === 'github' ? 'GitHub 社区仓库' : data.kind === 'official' ? 'npm @deepseek-ai 官方作用域' : 'npm 社区'], ['许可证', data.license], ['安装结构', data.bundle ? '已声明 dsh.bundle.patch' : '组件库 / 未声明组合包'], ['版本要求', Object.entries(data.compatibility).map(([k,v]) => k + ': ' + v).join('；') || '未声明 Harness 版本要求']]) meta.append(element('dt', '', label), element('dd', '', value));
    const links = element('div', 'help-tools'); links.append(externalLink(data.provider === 'github' ? '查看仓库' : '查看 npm', data.url)); if (data.repository) links.append(externalLink('源代码', data.repository)); if (data.homepage && data.homepage !== data.repository) links.append(externalLink('项目主页', data.homepage));
    $('detailContent').append(description, meta, links);
    if (data.notice) $('detailContent').append(element('p', 'inline-error', data.notice));
    const existing = state?.plugins.find(p => p.id === data.name);
    const blocked = existing && existing.tracking !== 'auto';
    if (blocked) $('detailContent').append(element('p', 'inline-error', existing.tracking === 'builtin' ? '此插件由内置适配管理，升级前需要重新校验补丁。' : '本机已安装，但没有安装前记录；不能直接补回历史改动。'));
    if (tracking?.session) $('detailContent').append(element('p', 'inline-error', '已有安装记录任务，请先完成或放弃当前任务。'));
    if (data.warning) $('detailContent').append(element('p', 'inline-error', data.warning));
    $('detailContent').append(element('p', 'small muted', '选用后会填入安装表单，由你检查并开始快照。安装会运行插件代码；包结构检查不代表安全审核或兼容性保证。npm 数据源选项仅影响目录搜索，安装沿用 Harness 包管理器配置。'));
    $('detailAdd').disabled = !data.installable || data.stale || Boolean(blocked || tracking?.session);
    $('detailAdd').textContent = existing?.tracking === 'auto' ? '准备升级并重新记录' : '选用并准备安装';
  } catch (error) {if (error.name !== 'AbortError' && id === detailRequest) { $('detailStatus').textContent = '无法读取详情：' + error.message; $('detailContent').append(externalLink('查看来源说明', item.url)); }}
}
$('catalogForm').onsubmit = event => {event.preventDefault(); if (!event.isComposing) searchCatalog();};
$('catalogQuery').addEventListener('keydown', event => {if (event.key === 'Enter' && event.isComposing) event.preventDefault();});
for (const id of ['catalogButton', 'discoverPlugins', 'discoverFromTracking']) $(id).onclick = openCatalog;
document.querySelectorAll('[data-catalog-source]').forEach(button => button.onclick = () => {catalogSource = button.dataset.catalogSource; document.querySelectorAll('[data-catalog-source]').forEach(n => {n.classList.toggle('active', n === button); n.setAttribute('aria-pressed', String(n === button));}); searchCatalog();});
$('catalogRegistry').onchange = () => searchCatalog(); $('catalogMore').onclick = () => searchCatalog(true); $('catalogRetry').onclick = () => searchCatalog(); $('catalogManual').onclick = openManual;
$('catalogDialog').addEventListener('close', () => {catalogRequest++; catalogAbort?.abort(); catalogBusy(false);});
$('pluginDetailDialog').addEventListener('close', () => {detailRequest++; detailAbort?.abort();});
$('detailAdd').onclick = () => {if (!currentDetail || $('detailAdd').disabled) return; const chosen = currentDetail; openManual(); $('trackingName').value = chosen.name; $('trackingSource').value = chosen.source; toast('已填入插件和来源。检查后点击「建立快照并开始」。');};
$('settingsButton').onclick = async () => {let paths = state; if (!paths) {try {paths = await api('settings');} catch (_) {paths = {};}} $('exePath').value = paths?.executable || ''; $('homePath').value = paths?.home || ''; $('settingsError').hidden = true; $('settingsDialog').showModal(); };
$('settingsForm').onsubmit = async event => {event.preventDefault(); await act(async () => { try {const result = await api('settings', {executable: $('exePath').value.trim(), home: $('homePath').value.trim()}); await refresh(true); toast(result.message); $('settingsDialog').close();} catch (e) {$('settingsError').textContent = e.message; $('settingsError').hidden = false;} });};
$('backupsButton').onclick = () => {if (state) {renderBackups(); $('backupsDialog').showModal();}};
for (const [id, target] of [['openProfile', 'profile'], ['openBackups', 'backups'], ['openSource', 'source']]) $(id).onclick = () => act(async () => {await api('open', {target});});
$('exportSchemes').onclick = () => {if (!state) return; const blob = new Blob([JSON.stringify({format: 'harness-launcher-schemes-v1', schemes: state.schemes}, null, 2)], {type: 'application/json'}); const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'Harness-启动方案.json'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 3000);};
$('importSchemes').onchange = async event => { const file = event.target.files[0]; event.target.value = ''; if (!file || !state) return; await act(async () => {if (file.size > 65536) throw new Error('方案文件过大，最多 64 KB。'); const data = JSON.parse(await file.text()); if (data.format !== 'harness-launcher-schemes-v1' || !data.schemes || typeof data.schemes !== 'object' || Array.isArray(data.schemes)) throw new Error('不是有效的 Harness 方案文件。'); const entries = Object.entries(data.schemes); if (entries.length > 100) throw new Error('最多导入 100 个方案。'); const valid = new Set(availableIds()); for (const [name, ids] of entries) {if (!name.trim() || name.trim().length > 40 || !Array.isArray(ids) || new Set(ids).size !== ids.length || ids.some(x => typeof x !== 'string' || !valid.has(x))) throw new Error('方案「' + name + '」包含无效或未安装的插件，请检查文件。');} const duplicates = entries.filter(([name]) => Object.hasOwn(state.schemes, name.trim())); if (duplicates.length && !await ask('导入将覆盖 ' + duplicates.length + ' 个同名方案，继续？')) return; const result = await api('import', {schemes: data.schemes}); await refresh(); toast(result.message); });};
setInterval(async () => {try {const data = await api('ping'); tracking = await api('tracking'); if (state) {state.running = data.running; state.tracking = tracking; updateSummary();} renderTracking(); if (trackingWasWorking && !tracking.busy) {await refresh();} trackingWasWorking = tracking.busy;} catch (_) { $('runtime').textContent = '菜单服务已断开'; $('launch').disabled = true; $('apply').disabled = true; }}, 4000);
refresh(true).catch(error => { $('plugins').replaceChildren(element('div', 'empty', '无法读取配置，请打开「设置与说明」检查路径。')); $('inlineError').textContent = error.message; $('inlineError').hidden = false; $('draftStatus').textContent = '配置读取失败'; $('launch').disabled = true; $('apply').disabled = true; toast(error.message, true); });
