# Architecture and data contract / 架构与数据契约

This describes the 0.3.0 implementation. Runtime control data uses format 3.
本文说明 0.3.0 的实现；运行时控制数据统一使用格式 3。

## Boundaries / 模块边界

| Layer / 层 | Modules / 模块 | Responsibility / 职责 |
| --- | --- | --- |
| Application / 应用编排 | `launcher.py`, `plugin_tracking.py` | HTTP、配置与安装流程；协调依赖，决定何时启动或保存 |
| Domain / 领域规则 | `domain.py`, `recording_state.py`, `operation_models.py` | 文件身份、快照元信息、安装状态转换、事务命令解码；不执行 IO |
| Planning and execution / 规划与执行 | `file_view.py`, `switch_plan.py`, `patch_modes.py` | 只读推演、输入复核、持久事务、执行与恢复 |
| Adapter / 适配规则 | `adapters.py` | 插件专属路径、标记、归档位置和文件归属规则 |
| Storage / 存储 | `storage.py`, `version_store.py`, `settings_store.py` | 原子写入、内容校验、包与依赖图、严格设置加载 |
| Runtime context / 路径上下文 | `runtime_context.py` | 不可变安装路径；基础模块不通过 Manager 反向寻找依赖 |
| Observability / 操作状态 | `operations.py`, `errors.py` | 有界操作记录、稳定错误代码、恢复建议 |
| Browser / 前端 | `ui/api.js`, `ui/state.js`, `ui/runtime.js`, `ui/app.js` | 请求传输、不可变状态与 reducer、异步调度、DOM 呈现与事件 |
| Offline conversion / 离线迁移 | `migrate.py` | 在独立暂存目录转换和核验旧数据；运行时不导入该模块 |

`ContentStore` does not know about plugins or `Manager`. `VersionStore` receives
paths, the content store and a read-only host archive dependency. Adapter rules
are centralized; adding one requires explicit planning, ownership and recovery
tests, not scattered package-name branches.

`ContentStore` 不知道插件或 Manager；版本库接收路径、内容库及只读宿主归档依赖。
增加适配器时，应补齐其规划、路径归属和恢复测试，避免在通用模块散布插件名判断。

## Invariants / 不变量

- Content identity is file kind plus SHA-256, or a link target. Size, mtime and
  the source sampling stamp are separate snapshot metadata. A changed timestamp
  never proves changed contents. Blob timestamps never stand in for the live file.
- 文件内容身份与快照元信息分离；文件哈希决定内容相同，原文件时间戳只用于采样和恢复。
  恢复副本的时间戳不得代替原文件时间戳。
- Recording phases are `preparing → installing → recording → scanning → verifying`;
  manual installation skips `installing`. `runState` is exactly one of `idle`,
  `launching`, `running`, `closed`. Every transition validates evidence first.
- 安装状态转换集中于 `RecordingSession.transition`。完整基线是安装的前提；观察到运行并确认退出，
  才能扫描和校验。异常保留状态与基线。扫描/校验重试复用已结束的首次运行，不重复启动程序。
- A transaction decodes every command, verifies its owned paths and both recovery
  directions, checks the complete input again, and only then mutates files.
  Unknown operation kinds fail before execution. Recovery uses the persisted
  plan and file evidence, including crashes between a write and cursor update.
- 事务必须先完整预检，再重新核验输入后执行；未知命令、损坏副本、越界路径和外部修改均拒绝覆盖。
  恢复依据持久计划及实际文件，不只依据日志游标。

## Persistence contract / 持久化契约

| Data / 数据 | Version / 版本 | Rule / 规则 |
| --- | --- | --- |
| Settings, registry, adapter state, active switch journal, configuration backup / 设置、登记表、适配状态、切换日志、配置备份 | `format: 3` | Runtime reads only this format / 运行时只读此格式 |
| Immutable packages, revisions, combinations and native baselines / 不可变包、版本、组合和原生基线 | Their existing `format: 1` contracts / 各自原有格式 1 | Content-addressed objects are independent of control-data versions / 对象格式独立于控制数据版本 |
| Snapshot and delta objects / 快照及差异对象 | Stable existing JSON shape / 稳定 JSON 结构 | Explicit domain decoding; identity derives from canonical JSON / 领域解码，规范 JSON 决定内容标识 |
| Portable schemes / 便携方案 | `harness-launcher-schemes-v1` | Package names only; bind local versions on import / 只传包名，导入时绑定本机版本 |

Settings and registry loaders preserve additive fields. Their `requiredFeatures`
declares capabilities a reader must understand; unsupported requirements stop
loading instead of silently losing semantics. New optional fields must preserve
old meaning, have safe defaults and remain intact when existing data is saved.
Session and transaction command fields remain strict because ignoring one can
make replay unsafe.

设置及登记表保留新增可选字段；其必要的新语义通过 `requiredFeatures` 声明，当前程序不支持时拒绝读取。
可选字段须保持原语义、具有安全默认值，并在重新保存时保留。会话及事务命令字段保持严格解码，
避免忽略字段后错误重放。更改必要语义必须提高对应契约版本并提供离线转换；不能承诺未来永不迁移。

## Offline cutover / 离线切换

1. Finish pending installations and transactions in the old version, then stop
   both applications. Only complete v2 stores are accepted; missing history is
   not fabricated, and older incomplete stores are not silently imported.
2. `python migrate.py` builds and verifies a separate sibling stage. The preview
   removes its temporary stage and does not change live Harness files or the source store.
3. `python migrate.py --apply` verifies reachable objects, blobs, versions and
   supported combinations before swapping directories. The complete old store
   becomes `<data>.archive-<timestamp>-<id>` with a checksum manifest.
4. A sibling `<data>.upgrade.json` guards the rename boundary. Use
   `python migrate.py --recover` only if that cutover was interrupted. Keep all
   involved directories until recovery has finished.

对应中文流程：先用旧版结束未完成操作并退出；在独立目录预检；通过 `--apply` 切换新目录；
仅目录切换中断时使用 `--recover`。自定义数据路径用 `--data <目录>`。新版不自动搜寻或合并旧 AppData。
归档只作为离线恢复证据，不参与运行。归档不是 Harness 本体和用户数据的完整备份，需另行配套备份。

## Operation and error API / 操作与错误 API

`GET /api/operations` returns `current` and `last`, each with an operation id,
kind, status and elapsed time. This in-memory endpoint has its own lock and
remains readable during a long application mutation. It is not a durable task
queue and cannot resume work after a restart; the file transaction journal does that.

`/api/operations` 返回当前及最近一次操作，包含编号、类型、状态和耗时。该接口独立加锁，
用于长操作期间显示进度；持久恢复仍由事务日志负责。错误统一返回
`error`, `code`, `details`, `recovery`, `retryable`, `operationId`。
机器分支判断应使用 `code`，不解析中文错误文本。`retryable` 只是提示，前端不得自动重放写请求。

## Validation and capacity / 验证与容量

Use temporary synthetic installations for unit, HTTP, failure and recovery
tests. Add model tests for illegal states and browser tests for stale responses,
timeouts and connection recovery. Fixture success is not whole-platform or
third-party-plugin certification.

`test_architecture.py` checks dependency boundaries, including that runtime
modules do not import the offline migrator and domain/storage modules do not
depend on application coordinators.

测试必须使用临时合成安装目录；重点覆盖非法状态、内容损坏、事务中断和异步旧响应。
`test_architecture.py` 自动检查运行时与离线迁移的隔离，以及领域、存储层的依赖边界。
内容库按哈希去重并提供容量统计；迁移只清理已核验暂存目录中的不可达旧对象。
运行时不自动删除历史；后续垃圾回收必须先证明完整引用可达性并单独测试，不能按文件年龄直接删除。
