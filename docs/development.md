# 开发与维护

[使用指南](../README.md) · [架构与数据契约](architecture.md) · [插件维护](plugin-maintenance.md) · [English](development.en.md)

## 项目目录

```text
harness-launcher/
├── harness_launcher/       Python 后端包
│   ├── __main__.py         python -m harness_launcher 入口
│   ├── app/                HTTP 服务、启动流程、操作状态与切换事务
│   ├── domain/             数据模型、状态转换与错误类型
│   ├── plugins/            插件发现、来源、安装记录与更新卸载
│   ├── host/               官方程序、运行时修复与特殊插件适配
│   ├── storage/            内容对象、配置、版本、恢复点与缓存回收
│   └── verification/       文件视图、增量校验与 NTFS 检查点
├── ui/                     浏览器界面，无前端构建步骤
├── tests/                  Python 回归测试与共享夹具
│   └── ui/                 Node.js 前端测试
├── scripts/                源码版 Windows 启动辅助脚本
├── packaging/              便携包构建器和 Windows EXE 入口
├── docs/                   使用、设计、开发与实测文档
│   └── images/             文档截图和架构图
├── .github/workflows/      Windows CI
└── launch.vbs              双击启动源码版
```

根目录保留 README、贡献指南、许可证等项目入口。测试放进 `tests/`，新后端模块放进相应的 Python 子包；临时诊断写进已忽略的 `diagnostics/`，构建产物写进已忽略的 `dist/`。

## 从哪里开始改

下面的 Python 路径均相对 `harness_launcher/`。

| 要改的功能 | 主要代码 | 先看的测试 |
| --- | --- | --- |
| 页面布局、按钮文案、异步状态 | `ui/`（仓库根目录）；`app/launcher.py` 提供 HTTP API | `tests/ui/test_ui.cjs`、`tests/test_http_delivery.py` |
| 启动与插件组合切换 | `app/launcher.py`、`app/switch_plan.py`、`app/operations.py` | `tests/test_launcher.py`、`tests/test_launch_progress.py`、`tests/test_version_plan.py` |
| 插件目录与安装来源 | `plugins/catalog.py`、`plugins/install_sources.py`、`plugins/installer_policy.py` | `tests/test_catalog.py`、`tests/test_install_sources.py`、`tests/test_installer_policy.py` |
| 安装前后记录、首次运行 | `plugins/plugin_tracking.py`、`domain/recording_state.py` | `tests/test_plugin_tracking.py`、`tests/test_official_plugins.py` |
| 更新、卸载与恢复点 | `plugins/maintenance.py`、`plugins/update_check.py`、`storage/maintenance_files.py` | `tests/test_maintenance.py` |
| 官方更新与缺失运行时修复 | `host/official_update.py`、`host/official_package.py`、`host/official_host.py`、`host/runtime_repair.py` | `tests/test_official_update.py`、`tests/test_official_host.py`、`tests/test_runtime_repair.py` |
| 特殊插件适配规则 | `host/adapters.py`、`host/patch_modes.py` | `tests/test_patch_modes.py`、`tests/test_host_updates.py` |
| 文件校验与启动性能 | `verification/`、`host/runtime_integrity.py` | `tests/test_verification_cache.py`、`tests/test_runtime_checkpoints.py`、`tests/test_switch_performance.py` |
| 内容去重、版本保存、设置与清理 | `storage/` | `tests/test_storage.py`、`tests/test_settings_store.py`、`tests/test_maintenance.py` |
| Windows 启动与发布 | `scripts/bootstrap.ps1`、`packaging/`（仓库根目录） | `tests/test_entrypoints.py`、`tests/test_launcher.py` |

Python 模块使用完整包路径导入，例如 `from harness_launcher.storage.content_store import ContentStore`。包的 `__init__.py` 只放说明和必要元信息，不集中导入业务模块。领域模型和底层存储不依赖 HTTP 或插件流程；这些已有依赖边界由 `tests/test_architecture.py` 检查。更详细的模块关系见[架构文档](architecture.md#boundaries--模块边界)。

普通插件的通用流程放在 `plugins/`；需要修改宿主文件的专属规则放在 `host/adapters.py`，由切换事务协调。新增插件能力优先扩展通用规则，专属适配器需配套路径归属和回退测试。

## 运行与测试

使用 Python 3.12+；前端测试使用 Node.js 22。运行时只依赖 Python 标准库，不需要执行 `pip install` 或 `npm install`。以下命令均从仓库根目录执行：

```powershell
# 启动源码版；Windows 上也可以双击 launch.vbs
python -m harness_launcher

# 全部回归；与 CI 使用相同的发现路径
python -m unittest discover -s tests -t . -v
node --test tests/ui/test_ui.cjs

# 按改动范围运行部分测试
python -m unittest tests.test_storage tests.test_version_plan -v
node --test --test-name-pattern="uninstall" tests/ui/test_ui.cjs
```

Python 测试按模块组织，测试间共用的夹具通过 `tests.test_launcher`、`tests.test_patch_modes` 等明确导入。夹具创建临时宿主、插件与数据目录，故障注入不使用个人安装。

排查后台服务时可使用 `python -m harness_launcher --no-open --data <独立数据目录>`。这只隔离启动器数据；涉及真实插件的手工操作，还要在设置中指定隔离的 Harness 安装与 `.dsh` 目录。

## 数据与恢复

默认数据目录为 `%USERPROFILE%\.harness-launcher`。只接受当前控制数据格式 3；不可变内容对象独立使用其自身契约。格式不支持时拒绝读取，不猜测、不自动转换。保留 `requiredFeatures` 检查，避免忽略必要语义。

备份时退出 Harness 及启动器，配套保存启动器数据目录、Harness 安装目录与 `.dsh` 数据目录。恢复应使用同一时间点的完整副本和原路径，避免版本、绝对路径及链接目标不一致。

操作失败先查看 `launcher.log` 和未完成事务。保留补丁对象、维护检查点、备份与事务日志。宿主变动或用户新改动时不要强制覆盖；按界面的恢复预览检查冲突。配置备份不等同于插件代码检查点。

## 构建与发布

```powershell
# 开发验包：产物写入独立目录，共用已校验的 CPython 下载缓存
python packaging/build_portable.py --output-dir dist/layout-smoke

# 正式构建：从干净、已提交的工作区执行
python packaging/build_portable.py
```

打包器按显式清单复制 Python 包，新增模块时同步更新 `packaging/build_portable.py` 的 `PACKAGE_MODULES`。漏列或缺失模块会使构建报错。源码版与便携版共用 `python -m harness_launcher` 入口，界面资源固定在程序根目录的 `ui/`。

根据改动验证首次启动、相同组合、组合切换、更新冲突、升级失败、卸载和恢复。公开截图应去掉会话、账号和其他个人内容，并标明真实操作或隔离示例。CI 覆盖 Windows Python 3.12 和 3.13。当前发行验证见[验证记录](startup-validation.md)。

发布前核对打包清单、文件哈希、便携 EXE、来源提交与 CI。个人设置、令牌、会话、诊断日志和测试夹具不进入发布包。构建不会自动发布，详见[打包说明](../packaging/README.md)。
