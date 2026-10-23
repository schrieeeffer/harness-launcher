# 维护与 AI 交接说明

**简体中文** · [English](MAINTENANCE.en.md)

本文供项目所有者、Claude Code 及其他维护者接手时使用。源码以 `launcher.py` 的 `VERSION` 为准；0.3.0 的模块边界、格式 3 与演进规则见 [架构契约](docs/architecture.md)，文件还原见 [技术说明](docs/patch-recording.md)，贡献规则见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 1. 项目与运行数据的位置

以下是示例布局，并非固定安装位置；请以启动器「设置与说明」及 `settings.json` 中的实际路径为准。

| 位置 | 用途 | 维护方式 |
| --- | --- | --- |
| `D:\projects\harness-launcher` | Git 仓库示例位置，用于开发、测试和代码提交 | 用编辑器或 AI 打开实际仓库目录 |
| `%USERPROFILE%\.harness-launcher` | 启动器设置、安装前快照、插件版本、组合及事务记录 | 单独备份，不提交 Git |
| `C:\Apps\DeepSeek Harness` | Harness 安装目录示例，包含程序及 `resources` | 按已安装版本保存，日常开发不在这里改启动器源码 |
| `%USERPROFILE%\.dsh` | Harness 会话、设置、desktop profile 和已安装插件 | 包含私人数据，单独备份 |
| `%USERPROFILE%\.harness-launcher.archive-*` | 迁移时完整旧目录的离线归档 | 新运行时不读取；与校验清单配套保存 |

`--data` 修改的是启动器数据位置；`DSH_HOME` / 设置中的 `home` 修改的是 Harness 数据位置；`DSH_EXECUTABLE` / 设置中的 `executable` 指向 Harness 程序。已保存的设置会被后续启动读取。修改路径前先核对实际设置，不能把三个目录混为一谈。

默认启动器数据放在用户主目录，是为了避开 Windows 商店应用的 AppData 虚拟化。不要自行改回旧位置。Git 只管理源码，不能恢复这些运行数据。

## 2. 源码目录结构

```text
harness-launcher/
├── launcher.py                 # HTTP 服务、设置、方案、配置备份、启动入口
├── domain.py / recording_state.py # 文件值类型、严格解码、安装状态机
├── operation_models.py / operations.py # 事务命令类型、操作状态 API
├── storage.py / settings_store.py # 内容寻址、原子写入、设置契约
├── runtime_context.py / errors.py # 路径上下文、稳定错误协议
├── adapters.py                 # 插件专属归属规则与宿主归档路径
├── catalog.py                  # 官方 / npm / GitHub 插件发现与排行榜
├── plugin_tracking.py          # 强制安装记录、首次运行监督、差异校验与保存
├── patch_modes.py              # dsh-purge 适配、官方 ASAR 校验、文件操作原语
├── version_store.py            # 插件安装内容与依赖、不可变版本和组合清单
├── file_view.py                 # 只读文件视图，模拟整个切换结果
├── switch_plan.py               # 组合预检、统一事务、失败和中断恢复
├── migrate.py                   # 独立 v2→v3 暂存校验、目录切换与离线归档
├── launch.vbs / bootstrap.ps1   # Windows 隐藏控制台启动
├── ui/
│   ├── index.html              # 页面与对话框
│   ├── style.css               # 样式与响应式布局
│   ├── api.js / state.js       # HTTP 传输、不可变状态与 reducer
│   └── runtime.js / app.js     # 异步调度、DOM 渲染与事件
├── test_*.py                   # 标准库 unittest；隔离的合成夹具
├── test_ui.cjs                 # Node 内置测试；连接与按钮状态的隔离回归测试
├── .github/workflows/ci.yml     # Windows，Python 3.12 / 3.13，Node.js 22
├── docs/                       # 中英文技术说明与合成数据截图
├── README.md / README.en.md     # 用户说明
├── MAINTENANCE.md / MAINTENANCE.en.md # 本维护说明
├── CONTRIBUTING.md             # 代码、验证、文档与隐私规则
└── CHANGELOG.md / SECURITY.md   # 变更记录与安全报告方式
```

界面无需构建，运行时只有 Python 标准库依赖。不要为了启动项目执行 `npm install` 或安装 pip 包。界面功能主要在 `ui/`；恢复功能跨越记录、版本库、文件视图和事务模块，不能只改界面按钮。

## 3. 环境与启动命令

在 PowerShell 中进入项目：

```powershell
Set-Location 'D:\projects\harness-launcher' # 替换为实际仓库路径
git status --short --branch
python --version
node --version
```

要求 Python **3.12+**；Node.js **22+** 用于 JavaScript 语法检查和界面逻辑测试。若系统 `python` 不满足版本要求，可使用 Harness 自带的解释器。下面是示例安装路径，替换后，后续命令沿用此变量：

```powershell
$launcherPython = 'C:\Apps\DeepSeek Harness\resources\runtime\primary-runtime\dependencies\python\python.exe'
& $launcherPython --version
$env:PYTHONUTF8 = '1'
& $launcherPython launcher.py
```

其他机器可把 `$launcherPython` 换为有效的 Python 3.12+ 绝对路径。已有合适的系统 Python 时，也可以运行 `python launcher.py`。

日常双击 `launch.vbs` 即可；`bootstrap.ps1` 优先从保存的 Harness 路径定位 `pythonw.exe`，否则使用 PATH 中的 `pythonw.exe`。它读取默认数据目录；自定义数据目录请使用命令行：

```powershell
& $launcherPython launcher.py --data 'D:\private\launcher-data'
& $launcherPython launcher.py --no-open
```

以上两行是不同的启动示例。`--no-open` 仍会启动后台服务和记录监督，不是预览或测试模式。`--data` 也只隔离启动器记录，**不会自动隔离真实的 Harness 安装或用户数据**；开发测试优先使用下一节的合成夹具。

服务只监听本机 `127.0.0.1`，默认端口 17643；被占用时可使用临时端口。`instance.json` 中有访问令牌，不要贴到 issue 或提交 Git。修改 Python 代码后需要重启后台服务；重复打开入口会复用原服务，仅刷新网页不能更新已加载的 Python 代码。

关闭菜单后后台会继续运行和记录，不再因缺少网页心跳自动退出。需要停止服务时，在「设置与说明」点击「退出启动器后台」；正在安装、记录或切换时会拒绝退出，已暂停的异常记录保留在磁盘。维护或备份前，先让正常记录自动保存、处理异常，再完全退出 Harness（包括托盘）及启动器后台，确认 `instance.json` 已移除。开发时以前台命令启动，可在没有记录或切换进行时用 `Ctrl+C` 结束。不能批量结束 Python 进程，也不能删除日志来假装服务已经退出。`instance.json` 异常残留时，应先核对其中 PID 对应的命令行及监听端口，确认服务已停止再处理。后台重启后请使用新打开的菜单；旧页面的令牌不能靠刷新更新。

## 4. 测试与日常开发流程

在项目根目录运行，Python 变量沿用上一节：

```powershell
& $launcherPython -m unittest discover -v
node --check ui/api.js
node --check ui/state.js
node --check ui/runtime.js
node --check ui/app.js
node --test test_ui.cjs
git diff --check
```

按模块定位问题时可运行：

```powershell
& $launcherPython -m unittest test_catalog test_launcher -v
& $launcherPython -m unittest test_plugin_tracking test_patch_modes -v
& $launcherPython -m unittest test_version_plan test_migrate -v
```

测试在临时目录中生成合成 profile、ASAR 和插件内容，并模拟安装进程及远程接口。不要把测试夹具改为本机 `.dsh`，不要安装真实插件来运行测试。恢复逻辑改动必须覆盖失败、中断和未知文件变化；界面改动需检查宽窗口及 1120 px、760 px 以下布局，保留 `app.js` 依赖的元素 id。

推荐流程：先查看工作区及相关测试，保留用户未提交的修改；按具体任务建立分支，完成改动后运行相关验证，更新中英文文档与 `CHANGELOG.md`。提交前检查 `git diff` 和 `git status`，只添加相关源码和文档。推送或合并按当前用户的明确要求执行，不使用强制推送或 `git reset --hard` 清理别人的工作。

CI 与本机检查的范围见 `.github/workflows/ci.yml`。测试通过不能替代真实环境的路径和权限核验，也不能宣称所有第三方插件组合都兼容。

## 5. 数据备份方法

### 备份范围

| 类型 | 能恢复什么 | 局限 |
| --- | --- | --- |
| Git 仓库 | 启动器代码与文档 | 不包含用户配置、插件、快照和 Harness |
| 界面中的「配置备份」 | 匹配当前版本约束的 desktop 配置和插件选择，经统一切换恢复 | 不是插件旧版本降级，也不是完整数据备份 |
| 同一时间点的离线副本 | 启动器记录、Harness 本体及其用户数据 | 三者需配套，保留路径、版本、时间戳及链接 |

准备长期维护或修改恢复机制前，保留第三种副本。需要一起备份：

- 启动器数据目录的全部内容，包括 `settings.json`、`patches`、`backups`、迁移报告和事务日志。同级的 `.archive-*` 旧目录、校验清单及未完成切换的 `.upgrade.json` 另行保留。
- 当前 Harness 安装目录，包含 `resources\.harness-launcher-*` 暂存目录和原有归档。
- 当前 Harness 用户数据目录，包含已安装插件及 `.harness-launcher-*` 暂存目录。

插件可能通过 junction / 符号链接指向这三个目录以外的依赖或本地源码；这些外部目标也需备份。下方命令保留链接本身，不跟随它复制外部目标。不能把保留链接误当作已备份外部内容。副本可能包含会话、账户信息和私人文件，应放在可信的备份位置，不上传 GitHub。

### Windows 离线复制示例

**先按第 3 节让服务完全退出，并确认没有安装或切换在进行。以下命令是维护者手动执行的示例，不会由启动器自动运行。** 默认数据目录以外的用户应先修改 `$launcherData`。备份放在这三个源目录以外，使用新的空目录：

```powershell
$ErrorActionPreference = 'Stop'
$launcherData = Join-Path $env:USERPROFILE '.harness-launcher'
if (Test-Path -LiteralPath (Join-Path $launcherData 'instance.json')) {
    throw 'Exit the launcher service first; do not delete its instance file.'
}
if (Get-Process -Name 'DeepSeek Harness' -ErrorAction SilentlyContinue) {
    throw 'Fully exit Harness, including its tray process.'
}
$settings = Get-Content -LiteralPath (Join-Path $launcherData 'settings.json') -Raw -Encoding utf8 | ConvertFrom-Json
$harnessInstall = (Resolve-Path -LiteralPath (Split-Path -Parent $settings.executable)).Path
$harnessHome = (Resolve-Path -LiteralPath $settings.home).Path
$backupBase = 'D:\backups\harness-launcher'
$backupRoot = Join-Path $backupBase ((Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$layout = @(
    @{ Folder = 'launcher-data'; Path = $launcherData },
    @{ Folder = 'harness-install'; Path = $harnessInstall },
    @{ Folder = 'harness-home'; Path = $harnessHome }
)
# Check these paths before copying. Keep the backup outside every source tree.
$layout | Format-Table Folder, Path
New-Item -ItemType Directory -Path $backupRoot | Out-Null
foreach ($item in $layout) {
    & robocopy $item.Path (Join-Path $backupRoot $item.Folder) /E /COPY:DAT /DCOPY:DAT /SJ /SL /R:1 /W:1 /NP
    if ($LASTEXITCODE -ge 8) { throw ('Backup failed: ' + $item.Path) }
}
$layout | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backupRoot 'layout.json') -Encoding utf8
git rev-parse HEAD | Set-Content -LiteralPath (Join-Path $backupRoot 'source-commit.txt') -Encoding utf8
$backupRoot
```

`robocopy` 的 0–7 不是复制失败，8 及以上应停止检查；需要创建符号链接的权限不足时也不能跳过错误。`/COPY:DAT`、`/DCOPY:DAT` 保留数据、属性和时间戳；`/SJ`、`/SL` 保留 junction / 符号链接。此示例不保留所有 ACL，也不是系统镜像；有额外权限、外部目标或整机恢复需求时，应使用能覆盖这些内容的备份工具。不要添加 `/MIR` 或 `/PURGE`，也不要清理暂存目录。未生成 `layout.json` 的副本应按未完成备份处理。源码应另用 Git 保存；`source-commit.txt` 只记录已提交的 HEAD，不能代替未提交工作区的备份。

## 6. 恢复与故障处理

### 日常还原与中断恢复

| 情况 | 处理方法 |
| --- | --- |
| 想关闭插件并还原已记录文件 | 完全退出 Harness，选「原生模式（全部关闭）」并「应用选择」，等待预检和事务完成 |
| 恢复某次配置 | 在「配置备份」中恢复；版本或记录不匹配时先查明原因，不能手动删除版本约束 |
| 安装记录异常 | 保留安装前快照，处理错误后用「继续自动记录」；不要用已修改的现状覆盖基线 |
| 新版组合切换中断 | 保留 `tracking\switch.json` 和关联 `transactions` / blobs；下一次正常切换会先尝试恢复，再按提示刷新重试 |
| v2→v3 目录切换中断 | 保留同级 `.upgrade.json`、stage 与 archive，退出服务后用 `migrate.py --recover` 处理 |
| 更早版本的导入中断 | 保留 `import-pending.json` / `migration-pending.json`，先用对应旧版完成恢复；新版不会猜测旧中间状态 |
| 未知改动、缺少副本或版本变化导致拒绝恢复 | 停止反复尝试覆盖，保留现场和脱敏日志，核对完整备份与版本 |

只在确实需要转换旧记录或继续中断的迁移时运行：

```powershell
& $launcherPython migrate.py
& $launcherPython migrate.py --apply
# Alternative: convert a custom data directory in place.
& $launcherPython migrate.py --data 'D:\private\launcher-data' --apply
# Only when the sibling .upgrade.json indicates interrupted cutover.
& $launcherPython migrate.py --data 'D:\private\launcher-data' --recover
```

前两行针对默认目录，第三行指定另一数据目录。预检只在独立暂存目录转换并验证，不改原目录及 Harness 文件。`--apply` 在完整核验后将旧根目录改名为同级 `.archive-*`，再切入新根目录；只保留可达的新格式活动对象。`--recover` 仅处理目录重命名边界中断，不修复损坏快照或更早的导入状态。当前格式不需反复迁移；不支持 `--source` 或自动 AppData 归并。

长操作可查询 `/api/operations` 的当前编号、类型与耗时，独立于文件操作锁。错误按 `code` 分类，`recovery` 给出处理建议；不要从错误文案解析逻辑或自动重试写请求。`/api/storage` 提供内容库分类容量；运行时不自动删除历史文件。

### 从离线副本恢复

1. 退出 Harness 和启动器服务，核对备份的 `layout.json`、代码提交及三个目录是否来自同一次成功复制。
2. 先保留当前故障现场：将当前三个目录分别在原父目录中改名留存。**不要把旧索引直接覆盖到仍在变化的新应用上，不要把旧目录与新目录混合。**
3. 核对 `layout.json` 中的三个目标是预期的绝对路径，均不是盘符根目录；按原路径恢复。路径改变会影响记录标识、绝对路径和链接目标，不能只改 `settings.json` 就视为迁移完成。
4. 恢复外部链接目标，保持原有 Harness / 插件版本、文件时间戳和暂存目录。以下示例针对本地盘符路径，会拒绝已经存在的目标，需先完成第 2 步。

```powershell
$ErrorActionPreference = 'Stop'
$backupRoot = 'D:\backups\harness-launcher\<the-successful-backup-folder>'
$layout = Get-Content -LiteralPath (Join-Path $backupRoot 'layout.json') -Raw -Encoding utf8 | ConvertFrom-Json
if ($layout.Count -ne 3 -or @($layout.Folder | Select-Object -Unique).Count -ne 3) { throw 'Check the backup layout before restoring.' }
foreach ($item in $layout) {
    if ($item.Folder -notin @('launcher-data', 'harness-install', 'harness-home')) { throw 'Unexpected backup folder.' }
    if (!(Test-Path -LiteralPath (Join-Path $backupRoot $item.Folder))) { throw 'Incomplete backup.' }
    if ($item.Path -notmatch '^[A-Za-z]:\\' -or [IO.Path]::GetPathRoot($item.Path).TrimEnd('\') -eq $item.Path.TrimEnd('\')) { throw 'Check the destination path.' }
    if (Test-Path -LiteralPath $item.Path) { throw ('Keep the current directory separately first: ' + $item.Path) }
}
foreach ($item in $layout) {
    & robocopy (Join-Path $backupRoot $item.Folder) $item.Path /E /COPY:DAT /DCOPY:DAT /SJ /SL /R:1 /W:1 /NP
    if ($LASTEXITCODE -ge 8) { throw ('Restore failed: ' + $item.Path) }
}
```

5. 使用能读取这套记录格式的源码启动，先检查路径、已安装版本、插件选择及记录状态，再在界面执行所需组合或原生模式的预检与应用。出现不匹配时保留副本并排查，不强行覆盖。确认恢复成功后再按自己的保留策略清理现场。

同级 `.archive-*` 是完整旧启动器目录，不包含 Harness 本体及用户数据，不能替代整套离线备份。Git 代码回退不会回退数据格式；必要时使用同一时间点、匹配格式的完整副本。新增可选字段尽量保持含义稳定，必要新语义通过 `requiredFeatures` 拒绝旧读取器；破坏契约的变更仍需版本化离线转换，不能承诺永不迁移。

## 7. 交给 Claude Code 或其他 AI 的提示词

在实际仓库目录（例如 `D:\projects\harness-launcher`）打开项目后，可复制下面这段，并填写具体任务：

```text
你正在维护 Harness Launcher。请先阅读 MAINTENANCE.md、CONTRIBUTING.md、
README.md、docs/patch-recording.md，并查看 git status 和相关模块、测试。
本次任务：<填写希望新增或修复的功能，以及预期行为>。

开发工作在当前仓库完成。使用 Python 3.12+，系统 python 版本不符时使用
Harness 自带的解释器；项目无需 pip/npm 安装，也不需要前端构建。
保留用户已有修改，不重置工作区。没有本次具体授权时，不操作真实的 Harness
安装、用户 profile、插件安装器或恢复数据；测试使用临时目录与合成夹具。

保留“记录强制、过程自动、异常才询问”。文件切换必须先整套只读预检，
校验原生基线、插件及依赖版本，再通过统一事务执行；失败和中断必须可恢复。
不能删除校验、伪造安装前状态、忽略冲突或把旧快照强套到新版本。
运行时只读取当前记录格式；旧格式转换放在 migrate.py，不重新加入运行兼容层。
版本留存不等于已有一键降级，配置恢复也不能绕过版本约束。

修改 UI 时保留已有交互、元素 id、严格 CSP 和纯文本渲染不可信内容；
检查宽窄布局。同步中英文文档与 CHANGELOG，截图只使用合成数据。
运行相关单测、必要的完整回归、node --check ui/app.js 和 git diff --check。
交付时说明改动、验证结果和实际限制；提交、推送、合并遵循本次用户要求，
不提交运行数据、凭据、令牌或私人截图。
```

接手者应先从当前代码和现场证据判断问题，不依赖上一轮聊天里的口头状态。需要恢复时先识别正在处理的是代码、配置、插件文件还是整套数据，再选择相应方法。
