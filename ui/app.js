'use strict';
const $ = id => document.getElementById(id);
const token = new URLSearchParams(location.search).get('token') || sessionStorage.getItem('harness-menu-token') || '';
sessionStorage.setItem('harness-menu-token', token);
const store = LauncherState.create();
let model = store.value, timer;
const transport = LauncherAPI.create({token});
const runtime = LauncherRuntime.create({store, transport, notify: toast});
const api = runtime.request, refresh = runtime.refresh, act = runtime.act;
const pollConnection = runtime.poll, setConnection = runtime.setConnection;
function ask(message, {title = '确认操作', accept = '确认', cancel = '取消', danger = false} = {}) {
  return new Promise(resolve => {
    const dialog = $('confirmDialog'); let accepted = false;
    $('confirmTitle').textContent = title; $('confirmMessage').textContent = message;
    $('confirmAccept').textContent = accept; $('confirmAccept').className = 'button ' + (danger ? 'danger' : 'primary');
    $('confirmCancel').textContent = cancel;
    $('confirmAccept').onclick = () => { accepted = true; dialog.close(); };
    $('confirmCancel').onclick = () => dialog.close();
    dialog.addEventListener('close', () => resolve(accepted), {once: true});
    dialog.showModal(); $('confirmCancel').focus();
  });
}
function element(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
function svgIcon(name) { const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg'), use = document.createElementNS(ns, 'use'); svg.setAttribute('class', 'icon'); svg.setAttribute('aria-hidden', 'true'); use.setAttribute('href', '#i-' + name); svg.append(use); return svg; }
// The backend reports '未知' when package.json has no readable version.
function versionLabel(v) { return v && v !== '未知' ? 'v' + v : '版本未知'; }
function tone(id) { let h = 2166136261; for (const c of id) h = Math.imul(h ^ c.charCodeAt(0), 16777619) >>> 0; return h % 6; }
// The toast is a popover so it renders above open modal dialogs; re-showing moves it to the top again.
function toast(message, error = false) { clearTimeout(timer); const t = $('toast'); t.textContent = message; t.className = 'toast' + (error ? ' error' : ''); if (t.matches(':popover-open')) t.hidePopover(); t.showPopover(); timer = setTimeout(() => t.hidePopover(), error ? 8500 : 5000); }
function renderConnection() {
  const health = model.server?.runtimeHealth;
  $('runtimeWarning').hidden = !health || health.status === 'ready';
  $('runtimeWarningText').textContent = health?.message || '';
  const host = model.server?.hostHealth;
  $('hostWarning').hidden = !host || host.status === 'ready';
  $('hostWarningText').textContent = host?.message || '';
  const messages = {
    checking: ['正在连接启动器', '正在读取实际配置，请稍候。'],
    offline: ['无法连接启动器后台', '请先点击「重新连接」。如果仍无法连接，请重新双击桌面快捷方式或项目里的 launch.vbs，并使用新打开的菜单。本页勾选会暂时保留；连接恢复后会重新读取实际配置。'],
    expired: ['这个菜单页面已失效', '后台已重启或此页面的访问凭据已失效。请重新双击桌面快捷方式或项目里的 launch.vbs，使用新打开的菜单；刷新这个旧页面无法更新凭据。'],
    stopped: ['启动器后台已退出', '所有启动菜单已断开。下次使用请重新双击桌面快捷方式或项目里的 launch.vbs。'],
    error: ['后台可连接，但状态读取未完成', model.connection.detail + ' 请稍后重新连接；如果持续出现，请在「设置与说明」检查路径或查看 launcher.log。']
  };
  const message = messages[model.connection.status];
  $('runtime').className = 'status' + (message ? model.connection.status === 'checking' ? '' : ' offline' : model.server?.running ? ' running' : '');
  $('runtime').replaceChildren(element('i'), document.createTextNode(message ? message[0] : model.server?.running ? 'Harness 运行中' : 'Harness 未运行'));
  $('connectionWarning').hidden = model.connection.status === 'online' || model.connection.status === 'checking';
  $('connectionTitle').textContent = message?.[0] || ''; $('connectionText').textContent = message?.[1] || '';
  $('reconnect').disabled = model.polling || model.operation.busy;
  $('reconnect').hidden = ['expired', 'stopped'].includes(model.connection.status);
  $('reconnect').textContent = model.polling ? '正在连接…' : '重新连接';
  $('quitMenu').disabled = !LauncherState.derive(model).canQuit;
}
function hasUntracked() { return LauncherState.derive(model).untracked; }
function selection() { return LauncherState.derive(model).selection; }
function isChanged() { return LauncherState.derive(model).changed; }
function needsPatchSwitch() { return LauncherState.derive(model).patchChanged; }
function setPreset(preset) { store.dispatch({type: 'draft/preset', preset}); }
function useScheme(name) {
  if (!model.server || model.operation.busy || !Object.hasOwn(model.server.schemes, name)) return;
  const known = new Set(model.server.plugins.map(plugin => plugin.id));
  const missing = model.server.schemes[name].filter(id => !known.has(id));
  store.dispatch({type: 'draft/scheme', name});
  if (missing.length) toast('此方案中的插件已不在本机：' + missing.join('、'), true);
}
function renderSchemes() {
  $('schemes').replaceChildren();
  document.querySelectorAll('[data-preset]').forEach(n => n.classList.toggle('active', n.dataset.preset === model.draft.mode));
  const names = Object.keys(model.server.schemes);
  if (!names.length) $('schemes').append(element('div', 'no-schemes', '保存常用插件组合\n下次一键选用'));
  for (const name of names) {
    const row = element('div', 'scheme-row'); const button = element('button', 'nav' + (model.draft.scheme === name && model.draft.mode === 'scheme' ? ' active' : ''));
    button.dataset.selectionControl = 'true';
    button.append(svgIcon('bookmark'), element('span', 'scheme-name', name)); button.title = name; button.onclick = () => useScheme(name);
    const remove = element('button', 'scheme-delete'); remove.append(svgIcon('x')); remove.title = '删除方案 ' + name; remove.setAttribute('aria-label', remove.title);
    remove.onclick = async () => { if (await ask('删除方案「' + name + '」？只删除菜单中的组合记录。')) act(async () => { await api('scheme', {name, delete: true}); await refresh(); toast('方案已删除'); }); };
    row.append(button, remove); $('schemes').append(row);
  }
}
function renderPlugins() {
  const query = $('search').value.toLowerCase().trim(); $('plugins').replaceChildren(); $('pluginCount').textContent = model.server.plugins.length;
  const visible = model.server.plugins.filter(p => (p.id + ' ' + p.description).toLowerCase().includes(query));
  if (!visible.length) { $('plugins').append(element('div', 'empty', query ? '没有匹配的已安装插件。试试「发现插件」搜索在线目录。' : '还没有额外插件。点击「发现插件」浏览官方与社区作品，也可手动添加。')); return; }
  for (const p of visible) {
    const card = element('article', 'plugin-card' + (model.draft.ids.includes(p.id) ? ' selected' : ''));
    const icon = element('div', 'plugin-icon tone-' + tone(p.id), p.id.split('/').pop().replace(/^dsh-/, '').slice(0, 1).toUpperCase());
    const info = element('div', 'plugin-info'); const heading = element('div', 'plugin-heading');
    heading.append(element('strong', '', p.id), element('span', 'badge version', versionLabel(p.version)));
    heading.append(element('span', 'badge' + (p.tracking !== 'untracked' ? ' on' : ' warn'), p.id === model.server?.tracking?.session?.plugin ? '安装记录进行中' : p.tracking === 'auto' ? '已自动记录补丁' : p.tracking === 'builtin' ? '支持自动还原补丁' : '未记录安装前状态'));
    if (!p.available) heading.append(element('span', 'badge error', '文件缺失'));
    else if (model.server.enabled.includes(p.id)) heading.append(element('span', 'badge success', '当前已启用'));
    const description = element('p', 'plugin-description', p.description); description.title = p.description;
    const meta = element('div', 'plugin-meta'); meta.append(element('span', '', p.available ? '本机已安装' : p.error));
    const pathButton = element('button', '', '查看路径'); const path = element('p', 'plugin-description plugin-path', p.path);
    pathButton.onclick = () => { path.classList.toggle('expanded'); pathButton.textContent = path.classList.contains('expanded') ? '收起路径' : '查看路径'; };
    meta.append(element('span', '', '·'), pathButton); info.append(heading, description, meta, path);
    const label = element('label', 'switch'); const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = model.draft.ids.includes(p.id); checkbox.dataset.pluginToggle = 'true'; checkbox.dataset.available = String(p.available); checkbox.disabled = model.operation.busy || !p.available && !checkbox.checked; checkbox.setAttribute('aria-label', '加载 ' + p.id);
    checkbox.onchange = () => store.dispatch({type: 'draft/toggle', id: p.id, checked: checkbox.checked});
    label.append(checkbox, element('span', 'switch-track')); card.append(icon, info, label); $('plugins').append(card);
  }
}
function updateSummary() {
  renderConnection();
  const capabilities = LauncherState.derive(model);
  $('launch').disabled = !capabilities.canLaunch;
  $('apply').disabled = !capabilities.canApply;
  $('saveScheme').disabled = $('newScheme').disabled = !capabilities.canSaveScheme;
  $('trackingBegin').disabled = !capabilities.canBeginTracking;
  $('refresh').disabled = model.operation.busy || model.polling;
  $('selectAll').disabled = $('selectNone').disabled = model.operation.busy;
  document.querySelectorAll('[data-preset], [data-selection-control]').forEach(button => button.disabled = model.operation.busy);
  document.querySelectorAll('[data-plugin-toggle]').forEach(input => input.disabled = model.operation.busy || input.dataset.available !== 'true' && !input.checked);
  document.querySelectorAll('[data-restore]').forEach(button => button.disabled = !capabilities.canRestore);
  if (!model.server) return;
  const disconnected = model.connection.status !== 'online';
  $('selectedCount').textContent = model.draft.ids.length;
  const titles = {current: '当前配置', none: '原生模式（全部关闭）', all: '全部可用插件', custom: '自定义组合', scheme: model.draft.scheme};
  $('selectionTitle').textContent = titles[model.draft.mode] || '自定义组合';
  $('selectionTitle').title = $('selectionTitle').textContent;
  $('selectionDescription').textContent = '开关只调整待选组合；点击启动会自动应用。也可仅应用，不启动 Harness。';
  $('selectionList').replaceChildren(...(model.draft.ids.length ? selection().map(id => element('li', '', id)) : [element('li', 'none', '仅加载官方基础组件')]));
  const patch = model.server.patchMode || {};
  const unknown = patch.unknown?.filter(name => name !== model.server?.tracking?.session?.plugin) || [];
  $('patchTitle').textContent = patch.error ? '补丁切换需要处理' : patch.verifiedNative && !model.draft.ids.length ? '已切换原生模式 · 启动前再次核验' : !model.draft.ids.length ? '将还原补丁并切换官方文件' : '补丁与插件开关自动同步';
  $('patchDescription').textContent = patch.error || (unknown.length ? '以下插件缺少安装前记录，已暂停组合切换：' + unknown.join('、') + '。需先恢复干净的安装，再通过启动器自动记录。' : !model.draft.ids.length ? '按逆序还原已记录文件，再切换官方归档；重新启用时按安装顺序恢复。' : '内置适配与自动记录统一切换；版本变化、记录外改动、共享文件冲突时停止覆盖。');
  if (model.server.hostHealth?.status === 'native_ready') {
    $('patchTitle').textContent = model.draft.ids.length ? '旧版插件暂不能用于当前宿主' : '新版原生基线已验证 · 启动前复核';
    $('patchDescription').textContent = '只允许已验证的新版原生启动；旧插件层与回滚数据保留，不跨版本回放。';
  }
  const missing = !model.server.executableExists;
  $('exeWarning').hidden = !missing; $('useDetected').hidden = !model.server.detectedExecutable;
  $('exeWarningText').textContent = '当前设置的路径不存在：' + model.server.executable + (model.server.detectedExecutable ? '。已在系统中检测到 ' + model.server.detectedExecutable + '，确认无误后可直接使用。' : '。请在设置中选择 DeepSeek Harness.exe。') + ' 修正前不能启动、切换补丁或开始安装记录。';
  $('draftStatus').className = 'draft-status' + (isChanged() || needsPatchSwitch() ? ' changed' : '');
  const recording = Boolean(model.server?.tracking?.session || model.server?.tracking?.busy);
  const operationNames = {apply: '应用插件组合', launch: '校验并启动 Harness', restore: '恢复配置', settings: '保存路径设置', scheme: '保存方案', import: '导入方案', 'tracking/begin': '建立安装快照', 'tracking/retry': '重新校验安装', 'tracking/abandon': '检查并取消安装'};
  const currentOperation = model.operation.current;
  const operationText = currentOperation ? '正在执行：' + (operationNames[currentOperation.kind] || currentOperation.kind) + ' · ' + Math.floor((currentOperation.durationMs || 0) / 1000) + ' 秒' : '正在处理，请稍候…';
  $('draftStatus').textContent = disconnected ? '连接恢复并读取实际状态后才能操作' : model.operation.busy ? operationText : missing ? '未找到 Harness 程序 · 请先修正应用路径' : recording ? model.server?.tracking?.session?.error ? '安装记录需要处理' : '安装记录自动完成中' : model.server.running ? '切换前请完全退出 Harness' : hasUntracked() ? '存在未记录插件 · 组合切换已暂停' : isChanged() || needsPatchSwitch() ? '待应用 · 点击启动会自动应用' : '与当前配置一致';
  $('draftStatus').title = $('draftStatus').textContent;
  $('launch').title = disconnected ? '请先恢复连接并读取实际状态' : recording ? '安装记录完成后才能切换组合或重新启动' : model.server.running ? '请先退出 Harness（包括托盘）' : missing ? '请在设置中确认应用路径' : hasUntracked() ? '已有插件缺少安装前记录，组合切换已暂停' : '校验当前组合；如有选择变更，自动应用后启动 Harness';
  $('apply').textContent = model.draft.ids.length ? '仅应用，不启动' : '仅还原，不启动';
  $('apply').title = '把待选组合写入配置并同步补丁，不启动 Harness；直接点击启动也会自动完成这些操作。';
  renderLaunchProgress();
}

function renderLaunchProgress() {
  const operation = model.operation.current;
  const visible = model.operation.busy && (!operation || ['launch', 'apply', 'restore'].includes(operation.kind));
  $('launchProgress').hidden = !visible;
  document.body?.classList.toggle('operation-visible', visible);
  if (!visible) return;
  const p = operation?.progress;
  const valid = p && Number.isInteger(p.step) && p.step >= 1 && p.step <= 5 && p.steps === 5 && typeof p.label === 'string';
  const counted = valid && Number.isInteger(p.completed) && Number.isInteger(p.total) && p.total > 0 && p.completed >= 0 && p.completed <= p.total;
  const units = {files: '个文件', changes: '项改动'};
  const detail = counted ? `${p.completed.toLocaleString()} / ${p.total.toLocaleString()} ${units[p.unit] || '项'}` : '正在处理 · 等待此阶段完成';
  const fraction = valid ? (p.step - 1 + (counted ? p.completed / p.total : 0)) / p.steps : 0;
  const seconds = Math.floor(Math.max(0, operation?.durationMs || 0) / 1000);
  $('progressLabel').textContent = valid ? p.label : '准备操作，等待后台响应';
  $('progressElapsed').textContent = seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  $('progressDetail').textContent = detail;
  $('progressStep').textContent = valid ? `阶段 ${p.step} / ${p.steps}` : '阶段进度';
  $('progressFill').style?.setProperty('width', `${Math.max(4, fraction * 100)}%`);
  $('progressThumb').style?.setProperty('left', `${Math.max(4, fraction * 100)}%`);
  $('progressTrack').setAttribute('aria-valuenow', String(valid ? p.step - 1 + (counted ? p.completed / p.total : 0) : 0));
  $('progressTrack').setAttribute('aria-valuetext', $('progressLabel').textContent + '；' + detail);
  $('progressTrack').classList.toggle('indeterminate', !counted);
  $('progressHint').hidden = seconds < 30;
}

function renderTracking() {
  if (!model.server?.tracking) return;
  const capabilities = LauncherState.derive(model);
  const session = model.server?.tracking.session;
  $('trackingCompleted').hidden = Boolean(session) || !model.server?.tracking.completed;
  $('trackingCompletedTitle').textContent = model.server?.tracking.completed ? model.server?.tracking.completed.plugin + ' 的还原记录已自动保存' : '';
  $('trackingForm').hidden = Boolean(session);
  $('trackingActive').hidden = !session;
  $('trackingBanner').hidden = !session; $('recordsSession').hidden = !session; $('addDot').hidden = !session;
  const step = session ? {preparing: 1, installing: 2, recording: 2, scanning: 3, verifying: 3}[session.phase] ?? 1 : 0;
  [...$('trackingSteps').children].forEach((n, i) => { n.classList.toggle('done', i < step); n.classList.toggle('current', i === step); });
  // The plugin currently being installed is expected to be unrecorded until completion.
  const blockedBaseline = !session && hasUntracked();
  $('trackingBegin').disabled = !capabilities.canBeginTracking;
  $('trackingBegin').title = model.server?.executableExists === false ? '请先在设置中修正 Harness 应用路径' : blockedBaseline ? '已有插件没有安装前记录，需先恢复干净的安装' : model.server?.running ? '请先完全退出 Harness（包括托盘）' : '';
  renderRecords();
  if (!session) { $('trackingOtherActions').open = false; return; }
  const names = {preparing: '自动备份原始文件', installing: '正在安装插件', recording: '首次运行记录中', scanning: '自动整理插件改动', verifying: '自动校验并保存'};
  $('trackingStageTitle').textContent = session.plugin + ' · ' + (session.error ? '需要处理' : names[session.phase] || session.phase);
  $('trackingBannerTitle').textContent = $('recordsSessionTitle').textContent = $('trackingStageTitle').textContent;
  $('openTrackingProgress').textContent = $('recordsContinue').textContent = session.error ? '处理问题' : '查看进度';
  const instructions = {preparing: '正在自动备份安装前的文件，完成后继续安装。首次备份可能较慢，请暂时保持 Harness 关闭。', installing: '正在通过 Harness 官方安装器安装，完成后自动打开 Harness。', recording: session.source ? '原始文件已备份，Harness 会自动打开。正常使用插件即可；完全退出 Harness（包括托盘）后，后台自动校验并保存还原记录。' : '原始文件已备份，Harness 会自动打开。请在里面安装或升级这个插件，正常使用并完全退出后，后台自动校验保存。', scanning: '正在自动对比安装前后的文件，不需要手动扫描或保存。', verifying: '正在检查文件是否稳定、版本和组合是否一致，并校验还原所需的文件副本；通过后自动保存。'};
  const instruction = session.error ? (session.baseline ? '原始快照和当前文件都会保留。' : '原始文件尚未备份完整，安装已暂停。') + '请处理下方的问题，再继续自动记录；异常解决前不会切换插件组合或覆盖文件。' : instructions[session.phase] || '';
  $('trackingInstruction').textContent = $('trackingBannerText').textContent = instruction;
  $('trackingCounter').textContent = session.phase === 'verifying' && session.count !== undefined ? '检测到 ' + session.count + ' 项改动' : (session.phase === 'scanning' ? '已检查 ' : session.phase === 'preparing' ? '已备份 ' : '安装前备份：') + model.server?.tracking.progress.count + ' 个文件 · ' + Math.round(model.server?.tracking.progress.bytes / 1024 / 1024) + ' MB';
  $('trackingPath').textContent = model.server?.tracking.busy ? model.server?.tracking.progress.path : '';
  $('trackingError').hidden = !session.error; $('trackingError').textContent = session.error || '';
  $('trackingTestSteps').hidden = session.phase !== 'recording' || Boolean(session.error);
  $('trackingTestHelp').textContent = model.server?.running ? 'Harness 正在运行，无需回到这里点击确认。使用结束后从托盘中退出，安装记录会自动完成。' : ['running', 'closed'].includes(session.runState) ? '已检测到 Harness 退出，正在等待文件停止写入，随后自动保存。' : '正在等待 Harness 打开。请只安装、使用这一个插件，避免同时编辑其他文件。';
  $('trackingReviewActions').hidden = !session.error;
  $('trackingRetry').disabled = !capabilities.canManageTracking;
  $('trackingRetry').title = model.server?.running ? '请先完全退出 Harness（包括托盘）' : '保留原始快照，重新检查并继续自动记录';
  $('trackingExport').hidden = !session.error || !session.delta;
  $('trackingAbandon').disabled = !capabilities.canManageTracking;
  $('trackingOtherActions').hidden = !session.error || Boolean(session.delta) || Boolean(session.installed);
  $('trackingPauseHint').textContent = session.error ? '可以关闭菜单，备份和异常信息会保留。处理问题后再继续；当前不会自动跳过错误。' : '可以关闭菜单窗口，后台会继续记录。退出 Harness 时请一并退出托盘程序；安装完成前只操作这个插件。';
  $('trackingReview').hidden = !session.error || !session.delta;
  $('trackingReview').replaceChildren();
  if (session.error && session.delta) {
    if (!session.count) $('trackingReview').append(element('p', 'muted', '没有在记录范围内发现额外文件改动。保存后仍会记住这个插件的安装情况。'));
    for (const item of model.server?.tracking.review) {const row = element('div', 'tracking-change'); row.append(element('span', 'badge', item.change), element('code', '', item.path)); $('trackingReview').append(row);}
    if (session.count > model.server?.tracking.review.length) $('trackingReview').append(element('p', 'small muted', '此处显示前 200 项，完整清单可导出查看。'));
  }
}
function recordRow(title, lines, action) { const row = element('div', 'backup'), info = element('div'); info.append(element('strong', '', title), ...lines.map(line => element('p', '', line))); row.append(info); if (action) row.append(action); return row; }
function renderRecords() {
  const session = model.server?.tracking.session, entries = Object.entries(model.server?.tracking.records);
  $('trackingScopes').replaceChildren(...model.server?.tracking.scopes.map(path => element('li', '', path)));
  $('recordedCount').textContent = entries.length;
  $('trackingRecords').replaceChildren(...entries.map(([name, record]) => {
    const upgrade = element('button', 'button', '升级 / 重新记录');
    upgrade.disabled = !LauncherState.derive(model).canBeginTracking;
    upgrade.onclick = () => { openAdd(name, '', true); toast('选择新版来源，或保持「自行安装」后在 Harness 内升级。开始时会先撤销旧版补丁。'); };
    return recordRow(name + ' · v' + record.version, [record.count + ' 处差异 · ' + (record.enabled ? '补丁已启用' : '补丁已停用'), ...(record.baseSelection.length ? ['记录时的基础组合：' + record.baseSelection.join('、')] : []), ...(record.historyCount ? ['保留 ' + record.historyCount + ' 份历史记录'] : [])], upgrade);
  }));
  if (!entries.length) $('trackingRecords').append(element('p', 'empty', '还没有通用记录。通过「手动添加」或「发现插件」安装的插件会出现在这里。'));
  const plugins = model.server?.plugins || [], builtin = plugins.filter(p => p.tracking === 'builtin'), untracked = plugins.filter(p => p.tracking === 'untracked' && p.id !== session?.plugin);
  $('builtinGroup').hidden = !builtin.length; $('untrackedGroup').hidden = !untracked.length; $('untrackedCount').textContent = untracked.length;
  $('builtinRecords').replaceChildren(...builtin.map(p => recordRow(p.id + ' · ' + versionLabel(p.version), ['关闭时使用保留的官方归档并暂存注入文件，启用时恢复同版本补丁快照；版本升级后需要重新校验。'])));
  $('untrackedRecords').replaceChildren(...untracked.map(p => recordRow(p.id + ' · ' + versionLabel(p.version), [(model.server.enabled.includes(p.id) ? '当前已启用 · ' : '') + (p.available ? '本机已安装，没有安装前快照' : p.error)])));
}
function openRecords() { if ($('addDialog').open) $('addDialog').close(); renderTracking(); if (!$('recordsDialog').open) $('recordsDialog').showModal(); }

const sourceKinds = {
  npm: {label: '版本或标签（可选）', placeholder: '例如：1.2.0 或 latest；留空安装最新版', hint: '快照完成后调用 Harness 官方安装器，从包管理器当前配置的 registry 安装。'},
  local: {label: '本地目录', placeholder: '例如：D:\\plugins\\dsh-example', hint: '快照完成后由官方安装器从该目录安装，目录内需要有 package.json。'},
  git: {label: '仓库地址', placeholder: '例如：git+https://github.com/作者/仓库.git#v1.0.0', hint: '仓库地址不锁定 commit，建议在 # 后填写固定的 tag 或 commit。'},
  self: {hint: '先自动备份并打开 Harness。请在里面安装或升级这个插件，正常使用并完全退出后自动保存还原记录。'}
};
function sourceKind() { return document.querySelector('[name=sourceKind]:checked').value; }
function renderSourceKind() { const kind = sourceKind(), info = sourceKinds[kind]; $('sourceField').hidden = kind === 'self'; $('sourceLabel').textContent = info.label || ''; $('trackingSource').placeholder = info.placeholder || ''; $('trackingSource').required = kind === 'local' || kind === 'git'; $('sourceHint').textContent = info.hint; $('trackingBegin').textContent = kind === 'self' ? '备份并打开 Harness' : '安装并打开 Harness'; }
function setSource(kind, value = '') { document.querySelector('[name=sourceKind][value="' + kind + '"]').checked = true; $('trackingSource').value = value; renderSourceKind(); }
function composedSource() { const kind = sourceKind(), name = $('trackingName').value.trim(), value = $('trackingSource').value.trim(); return kind === 'self' ? '' : kind !== 'npm' ? value : !value ? name : value.startsWith(name + '@') ? value : name + '@' + value.replace(/^@/, ''); }
function openAdd(name = '', source = '', upgrade = false) {
  for (const id of ['catalogDialog', 'pluginDetailDialog', 'recordsDialog']) if ($(id).open) $(id).close();
  if (name) {
    $('trackingName').value = name;
    if (!source) setSource(upgrade ? 'self' : 'npm');
    else if (/^(git\+|https?:)/i.test(source)) setSource('git', source);
    else if (source === name || source.startsWith(name + '@')) setSource('npm', source.slice(name.length + 1));
    else setSource('local', source);
  }
  $('addTitle').textContent = upgrade ? '升级并重新记录' : '手动添加插件';
  renderTracking(); if (!$('addDialog').open) $('addDialog').showModal();
  if (!model.server?.tracking?.session) (name ? document.querySelector('[name=sourceKind]:checked') : $('trackingName')).focus();
}

async function trackingAction(action, body = {}) {
  await act(async () => {const result = await api('tracking/' + action, body); store.dispatch({type: 'tracking/received', tracking: await api('tracking')}); toast(result.message); if (!model.server?.tracking.busy && !model.server?.tracking.session) await refresh(true);});
}
function openScheme() { if (!model.server) return; $('schemeName').value = ''; $('schemePreview').textContent = model.draft.ids.length ? '包含：' + selection().join('、') : '此方案不加载额外插件。'; $('schemeError').hidden = true; $('schemeDialog').showModal(); $('schemeName').focus(); }
async function apply(launch) { await act(async () => { const result = await api(launch ? 'launch' : 'apply', {selection: selection(), revision: model.server.revision, native: model.draft.ids.length === 0, scheme: model.draft.mode === 'scheme' ? model.draft.scheme : null}, 0); await refresh(true); toast(result.message + (result.verifiedCurrent ? ' · 当前组合校验通过' : result.patchMode === 'native' ? ' · 已还原补丁' : result.patchMode === 'plugin' ? ' · 补丁快照已恢复' : '')); if (launch) store.dispatch({type: 'harness/started'}); }); }
function renderBackups() {
  $('backupList').replaceChildren();
  if (!model.server.backups.length) $('backupList').append(element('div', 'empty', '暂无备份。首次修改配置前会自动创建。'));
  for (const b of model.server.backups) {
    const row = element('div', 'backup'), info = element('div');
    info.append(element('strong', '', new Date(b.time).toLocaleString('zh-CN', {hour12: false})), element('p', '', b.reason + ' · ' + (b.plugins.length ? b.plugins.join('、') : '不加载额外插件')));
    const restore = element('button', 'button', '恢复'); restore.dataset.restore = 'true'; restore.disabled = !LauncherState.derive(model).canRestore;
    restore.onclick = async () => { if (await ask('恢复此备份的 package.json？恢复前会自动备份当前配置。')) act(async () => { const result = await api('restore', {id: b.id, revision: model.server.revision}); await refresh(true); renderBackups(); toast(result.message); }); };
    row.append(info, restore); $('backupList').append(row);
  }
}
document.querySelectorAll('[data-preset]').forEach(button => button.onclick = () => setPreset(button.dataset.preset));
document.querySelectorAll('[data-close]').forEach(button => button.onclick = () => $(button.dataset.close).close());
document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('click', event => {if (event.target === dialog) {const r = dialog.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) dialog.close();}}));
$('search').oninput = () => model.server && renderPlugins();
$('selectAll').onclick = () => setPreset('all'); $('selectNone').onclick = () => setPreset('none');
$('refresh').onclick = () => act(async () => { await refresh(); toast('插件列表已刷新，保留当前勾选'); });
$('saveScheme').onclick = openScheme; $('newScheme').onclick = openScheme;
$('schemeForm').onsubmit = async event => { event.preventDefault(); await act(async () => { const name = $('schemeName').value.trim(); if (Object.hasOwn(model.server.schemes, name) && !await ask('覆盖已有方案「' + name + '」？')) return; try {await api('scheme', {name, selection: selection()}); await refresh(); store.dispatch({type: 'draft/scheme', name}); $('schemeDialog').close(); toast('方案已保存');} catch (e) {$('schemeError').textContent = e.message; $('schemeError').hidden = false;} }); };
$('apply').onclick = () => apply(false); $('launch').onclick = () => apply(true);
$('addButton').onclick = () => openAdd(); $('openTrackingProgress').onclick = () => openAdd(); $('recordsContinue').onclick = () => openAdd(); $('recordsAdd').onclick = () => openAdd();
$('recordsButton').onclick = openRecords; $('addToRecords').onclick = openRecords;
document.querySelectorAll('[name=sourceKind]').forEach(input => input.onchange = renderSourceKind); renderSourceKind();
$('trackingForm').onsubmit = async event => {event.preventDefault(); await trackingAction('begin', {name: $('trackingName').value.trim(), source: composedSource()});};
$('trackingRetry').onclick = () => trackingAction('retry');
$('trackingAbandon').onclick = async () => {
  if ($('trackingAbandon').disabled || !model.server?.tracking?.session) return;
  const id = model.server?.tracking.session.id;
  if (!await ask('将检查插件是否尚未安装、文件是否没有改动。只有检查通过才取消；如果已经发生改动，会保留记录并要求先处理问题。\n\n只是暂时离开的话，关闭菜单窗口即可，后台会继续记录。', {title: '取消尚未改动的安装？', accept: '检查并取消', cancel: '保留安装记录', danger: true})) return;
  if (!model.server?.tracking?.session || model.server?.tracking.session.id !== id || $('trackingAbandon').disabled) { toast('当前任务已变化，请检查后重试。', true); return; }
  await trackingAction('abandon');
};
$('trackingExport').onclick = () => act(async () => {const data = await api('tracking/diff'); const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}); const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'Harness-文件变更清单.json'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 3000);});

let catalogSource = 'all', catalogPage = 0, catalogLoading = false, catalogRequest = 0, detailRequest = 0, catalogAbort = null, detailAbort = null, currentDetail = null, catalogParameters = null, catalogRetryAppend = false;
function externalLink(label, url) {
  try { const parsed = new URL(url); if (!['https:', 'http:'].includes(parsed.protocol) || parsed.username || parsed.password) return element('span', '', label); }
  catch (_) { return element('span', '', label); }
  const link = element('a', 'button subtle', label); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; return link;
}
function catalogGet(path, parameters, signal) {
  return runtime.request('catalog/' + path + '?' + new URLSearchParams(parameters), undefined, 30000, {signal, reportConnection: 'auth'});
}
function catalogBusy(value) { catalogLoading = value; $('catalogResults').setAttribute('aria-busy', String(value)); $('catalogMore').disabled = value; $('catalogRetry').disabled = value; }
function catalogCard(item) {
  const card = element('article', 'catalog-card'), heading = element('div', 'catalog-card-heading');
  heading.append(element('div', 'catalog-icon', item.kind === 'official' ? 'D' : item.provider === 'github' ? 'G' : 'N'), element('span', 'badge' + (item.kind === 'official' ? ' on' : ''), item.kind === 'official' ? 'DeepSeek 官方' : item.provider === 'github' ? 'GitHub 社区' : 'npm 社区'));
  if (item.stars) heading.append(element('span', 'small muted', '★ ' + item.stars.toLocaleString()));
  card.append(heading, element('h3', '', item.title || item.name), element('code', 'catalog-package', item.name), element('p', 'catalog-description', item.description || '作者未提供描述。'));
  const installed = model.server?.plugins.find(p => p.id === item.name);
  const foot = element('div', 'catalog-card-footer'); foot.append(element('span', 'small muted', installed ? '本机已安装 · ' + versionLabel(installed.version) : item.version ? 'v' + item.version : '详情中检查版本与安装结构'));
  const view = element('button', 'button', '查看详情'); view.onclick = () => openPluginDetail(item); foot.append(view); card.append(foot); return card;
}
async function searchCatalog(append = false) {
  append = append && Boolean(catalogParameters);
  const parameters = append ? catalogParameters : {q: $('catalogQuery').value.trim(), source: catalogSource, registry: $('catalogRegistry').value};
  catalogRetryAppend = append;
  const id = ++catalogRequest; catalogAbort?.abort(); catalogAbort = new AbortController(); catalogBusy(true);
  const page = append ? catalogPage + 1 : 0;
  if (!append) { $('catalogResults').replaceChildren(); $('catalogMore').hidden = true; }
  $('catalogRetry').hidden = true; $('catalogWarning').hidden = true; $('catalogStatus').textContent = '正在搜索公开插件目录…';
  try {
    const data = await catalogGet('search', {...parameters, page}, catalogAbort.signal);
    if (id !== catalogRequest || !$('catalogDialog').open) return;
    catalogPage = page; catalogParameters = parameters;
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
function openCatalog() {for (const id of ['addDialog', 'recordsDialog']) if ($(id).open) $(id).close(); if (!$('catalogDialog').open) $('catalogDialog').showModal(); setCatalogView(catalogView);}

let catalogView = 'browse', board = 'downloads', boardRequest = 0, boardAbort = null;
const boardScopes = {
  downloads: '按近 7 天下载量排名。30 天累计仅作参考，它容易被一次性的批量下载抬高。',
  rising: '近 30 天的下载有一半以上发生在最近 7 天的插件，多为新发布或突然走红；周下载不足 200 的不计入。'
};
function pressed(selector, active) { document.querySelectorAll(selector).forEach(n => { n.classList.toggle('active', n === active); n.setAttribute('aria-pressed', String(n === active)); }); }
function setCatalogView(view) {
  catalogView = view; pressed('[data-catalog-view]', document.querySelector('[data-catalog-view="' + view + '"]'));
  const browse = view === 'browse';
  $('browseView').hidden = $('browseActions').hidden = !browse; $('leaderboardView').hidden = $('boardActions').hidden = browse;
  if (browse) { $('catalogQuery').focus(); if (!$('catalogResults').children.length) searchCatalog(); }
  else if (!$('boardList').children.length) loadBoard();
}
function boardRow(item, kind) {
  const row = element('li', 'board-row'), info = element('div', 'board-info'), title = element('div', 'board-title'), metric = element('div', 'board-metric');
  title.append(element('strong', '', item.name));
  if (item.kind === 'official') title.append(element('span', 'badge on', 'DeepSeek 官方'));
  const installed = model.server?.plugins.find(p => p.id === item.name);
  if (installed) title.append(element('span', 'badge success', '已安装 ' + versionLabel(installed.version)));
  info.append(title, element('p', 'board-desc', item.description || '作者未提供描述。'), element('p', 'board-meta', [item.version && 'v' + item.version, item.published && '最近发布 ' + item.published].filter(Boolean).join(' · ')));
  metric.append(element('b', '', item.weekly.toLocaleString('zh-CN')), element('span', '', '近 7 天下载'), element('small', '', kind === 'rising' ? '占 30 天下载的 ' + Math.round(item.weekly / item.monthly * 100) + '%' : '30 天 ' + item.monthly.toLocaleString('zh-CN')));
  const view = element('button', 'button', '查看详情'); view.onclick = () => openPluginDetail(item);
  row.append(element('span', 'board-rank' + (item.rank <= 3 ? ' top' : ''), String(item.rank)), info, metric, view);
  return row;
}
async function loadBoard() {
  const id = ++boardRequest, kind = board; boardAbort?.abort(); boardAbort = new AbortController();
  $('boardList').replaceChildren(); $('boardList').setAttribute('aria-busy', 'true'); $('boardWarning').hidden = true; $('boardRetry').hidden = true;
  $('boardScope').textContent = boardScopes[kind]; $('boardStatus').textContent = '正在统计 npm 下载量…首次加载需要几秒。';
  try {
    const data = await catalogGet('leaderboard', {board: kind}, boardAbort.signal);
    if (id !== boardRequest || !$('catalogDialog').open) return;
    $('boardList').replaceChildren(...data.items.map(item => boardRow(item, kind)));
    if (!data.items.length) $('boardList').append(element('li', 'empty', kind === 'rising' ? '最近没有符合条件的新晋插件。' : '没有可以排名的插件。'));
    const time = new Date(data.fetchedAt * 1000).toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', hour12: false});
    $('boardStatus').textContent = '统计范围：npm 搜索返回的 ' + data.scanned.toLocaleString('zh-CN') + ' 个 dsh 插件（生态共约 ' + data.total.toLocaleString('zh-CN') + ' 个）· 数据获取于 ' + time + (data.stale ? ' · 离线缓存' : '') + ' · 每小时刷新';
    $('boardWarning').textContent = data.warning || ''; $('boardWarning').hidden = !data.warning;
  } catch (error) {
    if (error.name !== 'AbortError' && id === boardRequest) { $('boardStatus').textContent = '排行榜暂未加载。'; $('boardWarning').textContent = error.message; $('boardWarning').hidden = false; $('boardRetry').hidden = false; }
  } finally { if (id === boardRequest) $('boardList').setAttribute('aria-busy', 'false'); }
}
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
    const existing = model.server?.plugins.find(p => p.id === data.name);
    const blocked = existing && existing.tracking !== 'auto';
    if (blocked) $('detailContent').append(element('p', 'inline-error', existing.tracking === 'builtin' ? '此插件由内置适配管理，升级前需要重新校验补丁。' : '本机已安装，但没有安装前记录；不能直接补回历史改动。'));
    if (model.server?.tracking?.session) $('detailContent').append(element('p', 'inline-error', '已有安装任务，请等待自动保存；如果有异常，请先处理当前任务。'));
    if (data.warning) $('detailContent').append(element('p', 'inline-error', data.warning));
    $('detailContent').append(element('p', 'small muted', '选用后会填入安装表单，由你检查并开始快照。安装会运行插件代码；包结构检查不代表安全审核或兼容性保证。npm 数据源选项仅影响目录搜索，安装沿用 Harness 包管理器配置。'));
    $('detailAdd').disabled = !data.installable || data.stale || Boolean(blocked || model.server?.tracking?.session);
    $('detailAdd').textContent = existing?.tracking === 'auto' ? '准备升级并重新记录' : '选用并准备安装';
  } catch (error) {if (error.name !== 'AbortError' && id === detailRequest) { $('detailStatus').textContent = '无法读取详情：' + error.message; $('detailContent').append(externalLink('查看来源说明', item.url)); }}
}
$('catalogForm').onsubmit = event => {event.preventDefault(); if (!event.isComposing) searchCatalog();};
$('catalogQuery').addEventListener('keydown', event => {if (event.key === 'Enter' && event.isComposing) event.preventDefault();});
for (const id of ['catalogButton', 'discoverPlugins', 'discoverFromTracking']) $(id).onclick = openCatalog;
document.querySelectorAll('[data-catalog-source]').forEach(button => button.onclick = () => {catalogSource = button.dataset.catalogSource; document.querySelectorAll('[data-catalog-source]').forEach(n => {n.classList.toggle('active', n === button); n.setAttribute('aria-pressed', String(n === button));}); searchCatalog();});
$('catalogRegistry').onchange = () => searchCatalog(); $('catalogMore').onclick = () => searchCatalog(true); $('catalogRetry').onclick = () => searchCatalog(catalogRetryAppend); $('catalogManual').onclick = () => openAdd();
// `close` fires asynchronously; skip it if the dialog was reopened in the meantime.
$('catalogDialog').addEventListener('close', () => {if ($('catalogDialog').open) return; catalogRequest++; catalogAbort?.abort(); catalogBusy(false); boardRequest++; boardAbort?.abort(); $('boardList').replaceChildren();});
document.querySelectorAll('[data-catalog-view]').forEach(button => button.onclick = () => setCatalogView(button.dataset.catalogView));
document.querySelectorAll('[data-board]').forEach(button => button.onclick = () => { board = button.dataset.board; pressed('[data-board]', button); loadBoard(); });
$('boardRetry').onclick = loadBoard;
$('pluginDetailDialog').addEventListener('close', () => {if ($('pluginDetailDialog').open) return; detailRequest++; detailAbort?.abort();});
$('detailAdd').onclick = () => {if (!currentDetail || $('detailAdd').disabled) return; const chosen = currentDetail; openAdd(chosen.name, chosen.source, model.server?.plugins.find(p => p.id === chosen.name)?.tracking === 'auto'); toast('已填入插件和来源。检查后点击「安装并打开 Harness」，还原记录会自动保存。');};
async function openSettings() {let paths = model.server; if (!paths) {try {paths = await api('settings');} catch (_) {paths = {};}} $('exePath').value = paths?.executable || ''; $('homePath').value = paths?.home || ''; $('settingsError').hidden = true; $('settingsDialog').showModal(); }
$('settingsButton').onclick = openSettings; $('exeSettings').onclick = openSettings; $('runtimeSettings').onclick = openSettings; $('hostSettings').onclick = openSettings;
$('detectExe').onclick = async () => {try {const found = (await api('settings')).detected; if (found) {$('exePath').value = found; toast('已检测到 Harness：' + found + '。确认后点击「保存路径设置」。');} else toast('没有在安装登记和常见目录中找到 Harness，请手动填写 DeepSeek Harness.exe 的完整路径。', true);} catch (error) {toast(error.message, true);}};
$('useDetected').onclick = () => act(async () => {const result = await api('settings', {executable: model.server.detectedExecutable, home: model.server.home}); await refresh(true); toast(result.message);});
$('settingsForm').onsubmit = async event => {event.preventDefault(); await act(async () => { try {const result = await api('settings', {executable: $('exePath').value.trim(), home: $('homePath').value.trim()}); await refresh(true); toast(result.message); $('settingsDialog').close();} catch (e) {$('settingsError').textContent = e.message; $('settingsError').hidden = false;} });};
$('backupsButton').onclick = () => {if (model.server) {renderBackups(); $('backupsDialog').showModal();}};
for (const [id, target] of [['openProfile', 'profile'], ['openBackups', 'backups'], ['openSource', 'source']]) $(id).onclick = () => act(async () => {await api('open', {target});});
$('exportSchemes').onclick = () => {if (!model.server) return; const blob = new Blob([JSON.stringify({format: 'harness-launcher-schemes-v1', schemes: model.server.schemes}, null, 2)], {type: 'application/json'}); const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'Harness-启动方案.json'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 3000);};
let importPlan = [];
function planImport(schemes) {
  const known = new Map(model.server.plugins.map(p => [p.id, p])), seen = new Set();
  return Object.entries(schemes).map(([raw, ids]) => {
    const name = raw.trim(), row = {name: name || '（空名称）', ids: Array.isArray(ids) ? ids : [], status: 'skip'};
    if (!name || name.length > 40) return {...row, reason: '名称需为 1–40 个字符'};
    if (seen.has(name)) return {...row, reason: '文件中有同名方案，只保留第一个'};
    seen.add(name);
    if (!Array.isArray(ids) || ids.some(x => typeof x !== 'string') || new Set(ids).size !== ids.length) return {...row, reason: '插件列表格式无效'};
    const absent = ids.filter(x => !known.has(x)), broken = ids.filter(x => known.has(x) && !known.get(x).available);
    if (absent.length || broken.length) return {...row, missing: true, reason: [absent.length && '缺少插件：' + absent.join('、'), broken.length && '插件文件缺失：' + broken.join('、')].filter(Boolean).join('；')};
    return {...row, status: Object.hasOwn(model.server.schemes, name) ? 'overwrite' : 'new'};
  });
}
function renderImport(fileName) {
  const ok = importPlan.filter(r => r.status !== 'skip'), added = ok.filter(r => r.status === 'new').length, skipped = importPlan.length - ok.length;
  const total = Object.keys(model.server.schemes).length + added, over = total > 100;
  $('importFile').textContent = fileName + ' · 共 ' + importPlan.length + ' 个方案';
  $('importList').replaceChildren(...importPlan.map(r => {
    const row = element('li', 'import-row ' + r.status), info = element('div');
    info.append(element('strong', '', r.name), element('p', '', r.status === 'skip' ? r.reason : r.ids.length ? r.ids.join('、') : '不加载额外插件'));
    row.append(element('span', 'badge ' + {new: 'success', overwrite: 'warn', skip: 'error'}[r.status], {new: '新增', overwrite: '覆盖', skip: '跳过'}[r.status]), info); return row;
  }));
  const parts = [added && '新增 ' + added + ' 个', ok.length - added && '覆盖 ' + (ok.length - added) + ' 个同名方案'].filter(Boolean).join('、');
  $('importSummary').textContent = over ? '导入后将有 ' + total + ' 个方案，超过上限 100 个。请先删除不用的方案。' : ok.length ? '将导入 ' + ok.length + ' 个方案（' + parts + '）' + (skipped ? '，跳过 ' + skipped + ' 个' : '') + '。' : '没有可以导入的方案。';
  $('importSummary').className = 'import-summary' + (over || !ok.length ? ' blocked' : '');
  $('importHint').hidden = !importPlan.some(r => r.missing); $('importError').hidden = true;
  $('importConfirm').disabled = over || !ok.length; $('importConfirm').textContent = ok.length ? '导入 ' + ok.length + ' 个方案' : '导入';
}
$('importButton').onclick = () => $('importSchemes').click();
$('importSchemes').onchange = async event => {
  const file = event.target.files[0]; event.target.value = ''; if (!file || !model.server) return;
  try {
    if (file.size > 65536) throw new Error('方案文件过大，最多 64 KB。');
    let data; try { data = JSON.parse(await file.text()); } catch (_) { throw new Error('无法读取：这不是有效的 JSON 文件。'); }
    if (data?.format !== 'harness-launcher-schemes-v1' || !data.schemes || typeof data.schemes !== 'object' || Array.isArray(data.schemes)) throw new Error('不是 Harness Launcher 导出的方案文件。');
    const count = Object.keys(data.schemes).length; if (!count) throw new Error('文件中没有方案。'); if (count > 100) throw new Error('单个文件最多包含 100 个方案。');
    importPlan = planImport(data.schemes); renderImport(file.name); $('importDialog').showModal();
  } catch (error) { toast(error.message, true); }
};
$('importConfirm').onclick = () => act(async () => {
  const ok = importPlan.filter(r => r.status !== 'skip'), skipped = importPlan.length - ok.length;
  try { const result = await api('import', {schemes: Object.fromEntries(ok.map(r => [r.name, r.ids]))}); $('importDialog').close(); await refresh(); toast(result.message + (skipped ? ' · 跳过 ' + skipped + ' 个' : '')); }
  catch (error) { $('importError').textContent = error.message; $('importError').hidden = false; }
});
const resumeConnection = () => pollConnection(true);
$('reconnect').onclick = resumeConnection;
$('quitMenu').onclick = async () => {
  if (!await ask('退出后，所有启动菜单都会断开。已启动的 Harness 会继续运行。下次使用请重新双击桌面快捷方式或 launch.vbs。', {title: '退出启动器后台？', accept: '退出后台'})) return;
  await act(async () => {const result = await api('quit', {}); setConnection('stopped'); $('settingsDialog').close(); toast(result.message);});
};
window.addEventListener('focus', resumeConnection);
window.addEventListener('pageshow', resumeConnection);
window.addEventListener('online', resumeConnection);
document.addEventListener('visibilitychange', () => {if (!document.hidden) resumeConnection();});
store.subscribe((next, event) => {
  model = next;
  if (event.type === 'request/start') return;
  if (model.server && (event.type === 'state/received' || event.type.startsWith('draft/'))) {
    $('inlineError').hidden = true;
    $('patchNote').hidden = !model.server.patchMode?.supported && !model.server.patchMode?.unknown?.length;
    $('openProfile').textContent = model.server.home;
    if (model.server.version) $('appVersion').textContent = 'v' + model.server.version;
    renderSchemes(); renderPlugins();
  }
  if (!model.server && model.connection.status !== 'checking') $('plugins').replaceChildren(element('div', 'empty', '尚未读取插件列表，请按上方提示恢复连接。'));
  updateSummary(); renderTracking();
});
const maintenanceUI = {updates: [], status: null, preview: null};
function maintenanceAction(label, fn, disabled = false) {
  const button = element('button', 'button', label); button.disabled = disabled || model.operation.busy;
  button.onclick = () => act(fn); return button;
}
async function loadMaintenance() {
  maintenanceUI.status = await api('maintenance');
  await refresh(); renderMaintenance();
}
function renderMaintenance() {
  const operation = model.operation.current;
  $('maintenanceProgress').hidden = !model.operation.busy;
  $('maintenanceProgress').textContent = operation ? (operation.progress?.label || '正在处理') + ' · ' + Math.floor(operation.durationMs / 1000) + ' 秒' : '正在读取维护状态…';
  for (const id of ['checkUpdates', 'previewCleanup', 'refreshMaintenance']) $(id).disabled = model.operation.busy;
  const pending = maintenanceUI.status?.pending;
  $('maintenancePending').hidden = !pending; $('maintenancePending').replaceChildren();
  if (pending) {
    $('maintenancePending').append(element('h3', '', pending.name + ' · ' + ({waiting: '等待首次运行验证', failed: '维护未完成', installing: '安装中', restoring: '恢复中', prepared: '已保存检查点'}[pending.phase] || pending.phase)), element('p', 'small', pending.error || '正常使用新版并完全退出 Harness，再完成验证。'));
    const actions = element('div', 'help-tools');
    actions.append(maintenanceAction('完成首次运行验证', async () => { const result = await api('maintenance/finish', {}, 0); toast(result.message); await loadMaintenance(); }, pending.phase !== 'waiting' || model.server?.running),
      maintenanceAction('预览恢复升级前', () => showRecovery(), model.server?.running));
    $('maintenancePending').append(actions);
  }
  $('maintenanceList').replaceChildren();
  for (const plugin of model.server?.plugins || []) {
    const update = maintenanceUI.updates.find(row => row.name === plugin.id);
    const row = element('div', 'maintenance-row');
    row.append(element('strong', '', plugin.id + ' · ' + versionLabel(plugin.version)), element('p', 'small muted', update ? ({available: '可更新到 ' + versionLabel(update.latest), 'source-change': '可选择来源与版本', current: '已是最新', manual: '需要手动核实来源', error: '检查失败'}[update.status] || '') + '。' + (update.message || '') : '尚未检查更新'));
    const actions = element('div', 'help-tools');
    if (['available', 'source-change'].includes(update?.status)) actions.append(maintenanceAction('预览升级到 ' + update.latest, () => previewPlugin(plugin.id, 'upgrade', update.target), Boolean(pending)));
    actions.append(maintenanceAction('预览卸载', () => previewPlugin(plugin.id, 'uninstall'), Boolean(pending)));
    row.append(actions); $('maintenanceList').append(row);
  }
  $('maintenanceHistory').replaceChildren();
  for (const item of maintenanceUI.status?.history || []) {
    const row = element('div', 'maintenance-row');
    row.append(element('strong', '', item.name + ' · ' + (item.action === 'upgrade' ? '升级前' : '卸载前')), element('p', 'small muted', new Date(item.at * 1000).toLocaleString() + (item.restored ? ' · 已恢复' : '')));
    if (!item.restored) row.append(maintenanceAction('预览恢复', () => showRecovery(item.id), Boolean(pending) || model.server?.running));
    $('maintenanceHistory').append(row);
  }
  if (!maintenanceUI.status?.history?.length) $('maintenanceHistory').append(element('p', 'small muted', '尚无维护恢复点。'));
}
function showMaintenancePreview(title, data, execute) {
  $('maintenancePreviewTitle').textContent = title;
  $('maintenancePreviewText').textContent = data.message;
  $('maintenancePreserved').replaceChildren(...(data.preserved || []).map(text => element('li', '', '保留：' + text)));
  $('maintenanceFiles').textContent = (data.files || []).slice(0, 300).map(item => typeof item === 'string' ? item : (item.action || item.store || '') + ' · ' + item.path).join('\n') || '没有需要还原的文件差异。';
  if ((data.files || []).length > 300) $('maintenanceFiles').textContent += '\n另有 ' + (data.files.length - 300) + ' 项；执行前后台会重新核验全部项目。';
  $('maintenanceBlocked').hidden = !data.blocked?.length;
  $('maintenanceBlocked').textContent = (data.blocked || []).join('\n');
  $('maintenanceConfirm').disabled = Boolean(data.blocked?.length);
  $('maintenanceConfirm').onclick = () => act(async () => { $('maintenancePreview').close(); const result = await execute(); toast(result.message); await loadMaintenance(); });
  $('maintenancePreview').showModal();
}
async function previewPlugin(name, action, target) {
  const data = await api('maintenance/preview', {name, action, target});
  showMaintenancePreview((action === 'upgrade' ? '升级预览 · ' : '卸载预览 · ') + name, data,
    () => api('maintenance/execute', {name, action, target, token: data.token}, 0));
}
async function showRecovery(id) {
  const data = await api('maintenance/recovery-preview', {id}, 0);
  showMaintenancePreview('恢复检查点 · ' + data.count + ' 项', data,
    () => api('maintenance/restore', {id, token: data.token}, 0));
}
$('maintenanceButton').onclick = () => act(async () => { $('maintenanceDialog').showModal(); await loadMaintenance(); });
$('refreshMaintenance').onclick = () => act(loadMaintenance);
$('checkUpdates').onclick = () => act(async () => {
  $('maintenanceStatus').textContent = '正在查询安装来源…';
  try { const data = await api('updates', undefined, 90000); maintenanceUI.updates = data.updates; $('maintenanceStatus').textContent = '检查完成 · ' + new Date(data.checkedAt * 1000).toLocaleString() + ' · 缓存 5 分钟'; renderMaintenance(); }
  catch (error) { $('maintenanceStatus').textContent = '检查失败，可重试。'; throw error; }
});
$('previewCleanup').onclick = () => act(async () => {
  const data = await api('maintenance/gc-preview', {}, 0);
  showMaintenancePreview('清理预览 · ' + data.count + ' 个对象 · ' + (data.bytes / 1048576).toFixed(1) + ' MB', data,
    () => api('maintenance/gc', {token: data.token}, 0));
});
store.subscribe((value, event) => { if ($('maintenanceDialog').open && ['operation/start', 'operation/observed', 'operation/end', 'state/received'].includes(event.type)) renderMaintenance(); });

let runtimeRepairPlan = null, hostApprovalPlan = null;
function renderRuntimeRepair() {
  const busy = model.operation.busy;
  for (const id of ['runtimeCheck', 'runtimePrepare', 'runtimeDiscard', 'runtimeInstaller', 'runtimeChecksum', 'hostPrepare', 'hostAccept']) $(id).disabled = busy;
  $('hostAccept').hidden = !hostApprovalPlan;
  $('runtimeExecute').hidden = !runtimeRepairPlan;
  $('runtimeDiscard').hidden = !runtimeRepairPlan;
  $('runtimeExecute').disabled = busy || !runtimeRepairPlan?.missing?.length || Boolean(runtimeRepairPlan?.blocked?.length);
  const operation = model.operation.current;
  $('runtimeRepairProgress').hidden = !busy;
  $('runtimeRepairProgress').textContent = operation ? (operation.progress?.label || '正在处理') + ' · ' + Math.floor((operation.durationMs || 0) / 1000) + ' 秒' : '等待后台响应…';
}
$('runtimeCheck').onclick = () => act(async () => {
  const result = await api('runtime/status'); $('runtimeRepairStatus').textContent = result.message;
});
$('hostPrepare').onclick = () => act(async () => {
  hostApprovalPlan = null; renderRuntimeRepair();
  $('hostApprovalStatus').textContent = '正在验证官方签名、安装文件和原生配置；不会修改安装目录。';
  try {
    hostApprovalPlan = await api('official-host/prepare', {installer: $('runtimeInstaller').value.trim() || undefined, sha512: $('runtimeChecksum').value.trim() || undefined}, 0);
    $('hostApprovalStatus').textContent = hostApprovalPlan.message + (hostApprovalPlan.cleanupPending ? ' 本次暂存内容清理未完成，已保留。' : '');
  } catch (error) { $('hostApprovalStatus').textContent = error.message; throw error; }
  finally { renderRuntimeRepair(); }
});
$('hostAccept').onclick = () => act(async () => {
  if (!hostApprovalPlan) return;
  const token = hostApprovalPlan.token;
  hostApprovalPlan = null; renderRuntimeRepair();
  try {
    const result = await api('official-host/accept', {token}, 0);
    $('hostApprovalStatus').textContent = result.message;
    await refresh();
  } catch (error) { $('hostApprovalStatus').textContent = error.message + ' 请刷新状态；不要盲目重复提交。'; throw error; }
});
$('runtimePrepare').onclick = () => act(async () => {
  runtimeRepairPlan = null; renderRuntimeRepair(); $('runtimeRepairFiles').hidden = true;
  $('runtimeRepairStatus').textContent = '正在验证官方安装包和文件；完成后可预览，不会修改安装目录。';
  try {
    runtimeRepairPlan = await api('runtime/prepare', {installer: $('runtimeInstaller').value.trim() || undefined, sha512: $('runtimeChecksum').value.trim() || undefined}, 0);
    $('runtimeRepairStatus').textContent = runtimeRepairPlan.message + ' 版本：' + runtimeRepairPlan.version;
    const lines = [...runtimeRepairPlan.missing.map(p => '缺失 · ' + p), ...runtimeRepairPlan.conflicts.map(p => '冲突（不会覆盖） · ' + p)];
    $('runtimeRepairFiles').textContent = lines.slice(0, 150).join('\n') + (lines.length > 150 ? '\n另有 ' + (lines.length - 150) + ' 项；后台将校验全部文件。' : '');
    $('runtimeRepairFiles').hidden = !lines.length;
  } catch (error) { $('runtimeRepairStatus').textContent = error.message; throw error; }
  finally { renderRuntimeRepair(); }
});
$('runtimeExecute').onclick = () => act(async () => {
  if (!runtimeRepairPlan || runtimeRepairPlan.blocked?.length || !runtimeRepairPlan.missing.length) return;
  try {
    const result = await api('runtime/execute', {token: runtimeRepairPlan.token}, 0);
    runtimeRepairPlan = result.cleanupPending ? {...runtimeRepairPlan, missing: []} : null; $('runtimeRepairStatus').textContent = result.message; $('runtimeRepairFiles').hidden = true;
    toast(result.message); await refresh();
  } catch (error) {
    $('runtimeRepairStatus').textContent = error.message + ' 已有文件未被覆盖；中断后请重新完整检查再继续。';
    if (runtimeRepairPlan) runtimeRepairPlan.blocked = [error.message];
    throw error;
  } finally { renderRuntimeRepair(); }
});
$('runtimeDiscard').onclick = () => act(async () => {
  const result = await api('runtime/discard', {}); runtimeRepairPlan = null;
  $('runtimeRepairStatus').textContent = result.message; $('runtimeRepairFiles').hidden = true; renderRuntimeRepair();
});
store.subscribe(() => renderRuntimeRepair());
pollConnection();
