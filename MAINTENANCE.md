# 当前版本维护手册

[使用指南](README.md) · [架构契约](docs/architecture.md) · [插件维护](docs/plugin-maintenance.md)

## 代码职责

`launcher.py` 管理本机 HTTP、启动和配置；`operations.py` 管理操作状态与锁；`switch_plan.py` 负责文件切换事务；`plugin_tracking.py` 记录安装及首次运行；`patch_modes.py` 与 `adapters.py` 处理 purge；`version_store.py` 留存版本及组合；`maintenance.py`、`maintenance_files.py`、`update_check.py`、`cache_gc.py` 处理升级、卸载、检查点和引用回收。

## 数据与恢复

默认数据目录为 `%USERPROFILE%\.harness-launcher`。只接受当前控制数据格式 3；不可变内容对象独立使用其自身契约。格式不支持时拒绝读取，不猜测、不自动转换。保留 `requiredFeatures` 检查，避免忽略必要语义。

备份时退出 Harness 及启动器，配套保存启动器数据目录、Harness 安装目录与 `.dsh` 数据目录。恢复应使用同一时间点的完整副本和原路径，避免版本、绝对路径及链接目标不一致。

操作失败先查看 `launcher.log` 和未完成事务。保留补丁对象、维护检查点、备份与事务日志。宿主变动或用户新改动时不要强制覆盖；按界面的恢复预览检查冲突。配置备份不等同于插件代码检查点。

## 验证与发布

```powershell
python -m unittest discover -v
node --test test_ui.cjs
python packaging/build_portable.py
```

检查首次启动、相同组合、组合切换、更新冲突、升级失败、卸载和恢复。故障测试使用隔离安装；公开截图只用示例数据。CI 覆盖 Windows Python 3.12 和 3.13。当前发行验证见[验证记录](docs/validation-0.4.0.md)。

发布前审计打包清单和哈希，验证便携 EXE，核对来源提交与 CI。不要将个人设置、令牌、会话、诊断日志或测试夹具打入发布包。详见[打包说明](packaging/README.md)。
