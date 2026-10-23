# 0.3.0 validation / 验证记录

Validated on 2026-10-24, Windows, Python 3.12.14 and Node.js 24.20.0.
验证对象为本次工作区中的 0.3.0 实现；以下是本机结果，不代表尚未运行的 CI 结果。

## Automated checks / 自动检查

| Check / 检查 | Result / 结果 |
| --- | --- |
| Python unittest integration suite and supplemental regressions / Python 集成测试及补充回归 | 247 integration cases plus 3 supplemental cases: 250 total, 248 passed, 2 skipped / 247 项集成测试加 3 项补充测试，共 250 项，248 通过、2 跳过 |
| `node --test test_ui.cjs` | 30 passed / 30 项通过 |
| `node --check` for `ui/app.js`, `ui/state.js`, `ui/api.js`, `ui/runtime.js` | Passed / 通过 |
| `git diff --check` | Passed / 通过 |

The Python total combines the integration run and three supplemental regression
cases; it is not presented as a single `unittest discover` run. All tests use
temporary fixtures or mocked services, without installing real plugins or
changing the user's Harness profile.

Python 总数由集成测试和 3 项补充回归组成，并非一次完整发现运行的结果。
自动测试使用临时夹具或模拟服务，不安装真实插件、不修改用户的 Harness 配置。

The two skipped storage tests require Windows symbolic-link creation privileges
(`WinError 1314`): `test_source_link_is_not_stored_as_a_regular_file` and
`test_store_root_or_blob_links_are_rejected`. Actual junction rejection,
directory replacement with a junction, and linked ancestors were tested.
CI is configured for Windows Python 3.12/3.13 and Node.js 22; those matrix runs
are not represented as local results here.

覆盖领域与状态转换、原子设置保存、文件身份、内容副本损坏、路径越界、版本锁、依赖漂移、
完整组合预检、写入与日志游标间中断、回滚再次中断、离线迁移与目录切换恢复。
专项回归包括：

- `root_yaml` / `cordis.yml` 仅更新时间戳时不再误报；保留时间戳与大小的真实内容改动仍被拦截。
- 包捕获期间仅有时间戳变化时，登记表保存最终核验快照，保留后续判断所需的内容证据。
- 设置写入失败不污染内存状态；主操作已完成而偏好保存失败时明确告警，不重复启动、不撤销已提交操作。
- HTTP 响应头或响应体断连不改变真实业务结果，不重复发送响应。
- 长操作状态读取独立于应用锁；前端旧响应、连接恢复和失败重试不覆盖用户草稿或重放写请求。
- 运行时不导入离线迁移模块；领域与存储模块不反向依赖应用编排。
- Windows 路径尾点、尾空格、设备名及祖先目录链接不能绕过托管范围；无清单模块遮蔽依赖时拒绝记录错误的包内容。
- 切换已提交后，日志清理失败不再误报回滚；保留提交标记，便于后续清理。
- 方案显示顺序变化不破坏版本锁；方案被修改或缺少插件时保留草稿并转为自定义组合。
- 重新连接及恢复焦点时读取完整状态；损坏的备份或补丁响应不覆盖最后有效的界面状态。
- 无效操作路径、非布尔操作参数和损坏备份不会误删方案或占住写操作锁。
- Python 版本不足时不创建运行数据；Windows 进程枚举失败不会被误判为 Harness 已退出。

## Earlier browser verification / 前期浏览器验证

The 2026-10-23 browser check used an isolated synthetic installation and a fake
Harness process. It verified
native selection, an eight-second apply with visible progress and disabled
controls, completed-state consistency, saving a scheme, external-revision
conflict with draft preservation, refresh, catalog details, and launch/running
status. No browser console warnings or errors were observed. Temporary servers
and tabs were closed after verification.

上述交互于 2026-10-23 使用合成数据检查，未用本机实际安装反复切换插件。
本轮新增前端逻辑由上表中的 30 项测试覆盖；真实插件的任意第三方安装器行为不在此验证范围内。

## Local startup confirmation / 本机启动确认

The existing local service was safely restarted. Authenticated inspection
confirmed version 0.3.0 and byte-for-byte equality between all four served
JavaScript modules and the workspace files. The desktop manifest and enabled
plugin selection were unchanged; no installation session or pending recovery
transaction remained. This confirmation did not install plugins or switch the
user's selected combination.

现有本机后台已安全重启。经认证的只读检查确认版本为 0.3.0，服务提供的四个
JavaScript 模块与工作区逐字节一致；desktop 配置与启用选择保持不变，且没有安装会话
或待恢复事务。该检查未安装插件，也未切换用户的组合。

Migration and directory-cutover recovery are covered by isolated fixtures.
Archives are not full backups of the Harness installation and user home; see
[MAINTENANCE.md](../MAINTENANCE.md) and [MAINTENANCE.en.md](../MAINTENANCE.en.md).
离线迁移及目录切换恢复通过隔离夹具验证；迁移归档不能替代应用与用户数据的配套备份。

## Limits / 边界

This is validated development-preview software, not a guarantee of zero bugs or
compatibility with every plugin. Future additive fields should preserve their
meaning and use capability declarations where necessary; a breaking semantic
change still needs an explicit contract version and an offline conversion.
Runtime history is retained rather than automatically deleted by age.

本次已删除旧运行时格式读取和隐式导入路径；离线归档不是兼容运行模式。
不能承诺未来永不迁移。后续维护应继续执行架构边界、状态转换及恢复测试，
避免重新引入隐式兼容、根据中文错误文本判断逻辑或自动重试写请求。
