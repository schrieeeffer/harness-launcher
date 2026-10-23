# 0.3.1 validation / 验证记录

Validated locally on 2026-10-24, Windows, Python 3.12.14 and Node.js 24.20.0.
本页记录本机结果；未把尚未完成的 GitHub CI 结果计入。

## Automated checks / 自动检查

| Check / 检查 | Result / 结果 |
| --- | --- |
| `python -m unittest discover -v` | 259 total, 257 passed, 2 skipped; 698.310 s / 共 259 项，257 通过、2 跳过 |
| `node --test test_ui.cjs` | 32 passed / 32 项通过 |
| Final operation reporter check after joining its test thread / 测试线程等待调整后的复查 | `test_operations`: 6 passed / 6 项通过 |
| JavaScript syntax checks / JavaScript 语法检查 | Passed / 通过 |
| `git diff --check` | Passed / 通过 |

The two skipped storage cases require Windows symbolic-link privileges
(`WinError 1314`). Junction and linked-ancestor rejection cases passed.
两项跳过测试需要 Windows 符号链接创建权限；目录联接及祖先链接防护测试通过。

New regressions cover unchanged-combination launch without replay, changes that
preserve file size and timestamps, package/host drift, active versus dormant
adapter contents, failed process creation, control changes during validation,
full transactions for changed combinations, progress isolation and UI waits
longer than five minutes without a duplicate mutation.

新增回归覆盖相同组合跳过重放、同大小同时间戳的真实内容改动、包与宿主变化、在用及暂存内置文件、
进程创建失败、校验期间控制输入变化、不同组合仍执行完整事务、进度隔离，以及超过五分钟仍保持等待且不重复写请求。
测试使用隔离夹具，不安装真实第三方插件。

## Browser verification / 浏览器检查

Production HTML, CSS and JavaScript were exercised against an isolated demo
service with controlled progress. Desktop, 800-pixel and 390-pixel layouts
displayed the progress bar and action buttons without horizontal overflow.
Counts and elapsed time updated; the long-operation hint appeared; successful
completion hid progress and updated the simulated running state. Developer logs
contained no warning or error entries. Reduced-motion support is implemented in
CSS; a separate OS motion-preference test was not performed.

正式界面连接隔离演示服务检查，受控阶段进度用于观察长操作布局。三种宽度下进度条和按钮完整显示，
无横向溢出；文件计数和耗时更新，长操作提示出现，完成后进度收起并更新模拟运行状态。
控制台未发现 warning 或 error。减少动画样式已实现，未单独切换系统动画偏好测试。
截图和测量边界见[中文说明](launch-progress.md) / [English](launch-progress.en.md)。

## Local restart / 本机重启

The real launcher service was restarted to **0.3.1**. Authenticated inspection
confirmed byte-for-byte equality for the served HTML, stylesheet and all four
JavaScript modules. The desktop manifest hash and enabled selection stayed the
same. Harness was running before and after the restart; the restart did not
stop it. No installation session, pending transaction or active operation
remained in the launcher.

实际启动器已重启到 **0.3.1**；经认证检查，服务提供的 HTML、CSS 与四个 JavaScript 模块均与工作区一致。
desktop 配置哈希及启用选择保持不变。重启前后 Harness 均处于运行状态，本次重启没有结束它。
启动器无安装会话、待恢复事务或未完成操作。新版完整 Harness 启动耗时未重新测量；
只读校验测量与旧版操作记录的区别已在性能说明中列明。
