# 0.3.2 功能截图与操作说明

[返回使用指南](../README.md) · [真实启动验收](validation-0.3.2.md) · [架构与原理](architecture.md)

以下 31 张截图重新采集于 0.3.2 的实际前端。截图环境使用隔离安装、示例路径与插件数据，不包含个人会话或凭据。选择、应用、方案、备份读取和宿主变化拒绝使用生产后台逻辑；安装过程、目录结果、运行标记及进度样本为模拟数据，不能证明真实插件兼容性或启动速度。真实计时另见验收记录。

## 选择与启动

开关改变待选组合。「启动 Harness」会自动应用变更后启动；「仅应用，不启动」用于提前保存配置与同步补丁。无需先应用再启动。组合没有变化时，只校验当前组合再启动。

![01 主界面与当前配置](images/feature-tour-0.3.2/01-overview.png)

搜索名称或描述，展开插件路径。勾选后刷新保留草稿；点击「当前配置」撤销草稿。全选、全部关闭及侧栏快捷项可批量选择。

![02 搜索、路径与待选开关](images/feature-tour-0.3.2/02-selection-and-search.png)

原生模式的额外插件数量为 0，官方基础组件始终保留。选中原生模式之后仍需点击启动或仅还原，才会实际切换文件。

![03 原生模式草稿](images/feature-tour-0.3.2/03-native-draft.png)

隔离安装中实际应用原生组合后，补丁状态与配置一致。正常使用时先完全退出 Harness，包括托盘。

![04 已应用原生组合](images/feature-tour-0.3.2/04-native-applied.png)

启动时显示实际阶段、文件计数和已用时间。紫色条表示阶段进度，不估算剩余时间。图中数据为刻意放慢的示例，不能作为速度测试。

![30 启动进度示例](images/feature-tour-0.3.2/30-launch-progress.png)

运行状态会阻止重复启动与修改文件。此处只模拟运行标记；真实窗口重启另有验收记录。

![29 模拟运行状态与操作限制](images/feature-tour-0.3.2/29-simulated-running.png)

## 方案管理

选好组合后保存方案，再从侧栏选用。隔离交互已实际保存新方案。

![05 保存方案](images/feature-tour-0.3.2/05-save-scheme.png)

删除方案需要确认；这里实际取消了删除，保留方案。覆盖同名方案也有确认，覆盖和删除的提交分支由后端与前端测试覆盖，本轮未再次逐项提交。

![06 删除确认](images/feature-tour-0.3.2/06-confirmation.png)

## 发现插件

发现页支持搜索、来源筛选和数据源选择。隔离交互验证了官方来源筛选以及切回全部；图中包和统计为示例。

![07 发现与筛选](images/feature-tour-0.3.2/07-discovery.png)

详情列出来源、许可证、安装结构和版本要求。选用后进入安装表单；安装会执行第三方代码，结构检查不等于兼容性验证。

![08 插件详情](images/feature-tour-0.3.2/08-plugin-detail.png)

排行榜可以切换下载量和新晋热门。本轮验证两种视图正常显示，没有查询真实在线排名。

![09 下载量榜](images/feature-tour-0.3.2/09-download-leaderboard.png)

![10 新晋热门榜](images/feature-tour-0.3.2/10-rising-leaderboard.png)

## 安装来源与自动记录

四种来源的表单均重新操作并截图；本轮没有执行远程安装、克隆或真实插件升级。

![11 npm 包与版本](images/feature-tour-0.3.2/11-add-npm.png)

![12 本地目录](images/feature-tour-0.3.2/12-add-local.png)

![13 Git 仓库与固定版本](images/feature-tour-0.3.2/13-add-git.png)

![14 自行安装](images/feature-tour-0.3.2/14-self-install.png)

安装前建立快照，随后安装并观察首次运行；完全退出 Harness 后，后台扫描、校验并保存记录。以下进度为模拟展示。

![15 建立快照进度](images/feature-tour-0.3.2/15-recording-progress.png)

异常时保留原始快照和差异，先处理原因再继续。隔离交互实际导出包含 3 项改动的 JSON，与屏幕一致；点击继续后进入模拟完成状态。

![16 异常与改动导出](images/feature-tour-0.3.2/16-recording-recovery.png)

![17 自动记录完成](images/feature-tour-0.3.2/17-recording-completed.png)

## 补丁记录与备份

补丁记录区分通用记录、内置适配与缺少安装前记录的插件，提供升级入口。展开范围说明可以查看监控边界。截图读取隔离样本的真实登记记录。

![18 补丁记录](images/feature-tour-0.3.2/18-patch-records.png)

![19 记录范围与限制](images/feature-tour-0.3.2/19-recording-scope.png)

配置备份展示保存原因与插件组合。恢复前再次备份，并检查当前插件及宿主版本；本轮打开并取消恢复确认，实际恢复与失败回滚由自动测试覆盖。

![20 配置备份](images/feature-tour-0.3.2/20-backups.png)

![21 恢复确认](images/feature-tour-0.3.2/21-restore-confirmation.png)

## 设置、导入导出与退出

设置支持程序路径、数据目录、自动检测、打开目录及后台管理。示例路径均为虚构，截图流程未改动真实安装路径。

![22 设置与说明](images/feature-tour-0.3.2/22-settings.png)

方案导出已实际获得可解析 JSON。导入预览区分新增、覆盖和缺少插件而跳过的项目；本轮实际导入有效项目，缺少插件的项目被跳过，实际配置不受影响。

![23 导入方案预览](images/feature-tour-0.3.2/23-import-preview.png)

退出后台与关闭菜单窗口不同：前者结束服务，后者允许后台继续记录。此截图的退出确认已取消；真实后台重启单独验收。

![24 退出后台确认](images/feature-tour-0.3.2/24-exit-confirmation.png)

## 异常提示与窄窗口

宿主程序变化会在大目录扫描前被拒绝，并说明更新后需要重新适配。下面实际替换了隔离测试安装的程序文件，由生产校验代码返回错误；没有升级用户的 Harness，也没有模拟成功结果。详见[官方更新与冲突处理](host-updates.md)。

![31 官方更新或宿主文件变化提示](images/feature-tour-0.3.2/31-host-update.png)

缺少程序时显示路径修复入口，并禁用启动。

![25 程序路径缺失示例](images/feature-tour-0.3.2/25-missing-executable.png)

断线时禁用写入并保留草稿。本轮恢复隔离后台后通过「重新连接」回到正常状态，没有重放写请求。

![28 连接断开与恢复入口](images/feature-tour-0.3.2/28-offline.png)

800 px 与 390 px 视口均检查 `scrollWidth <= clientWidth`，未发现横向溢出；底部固定操作栏保留启动入口。

![26 800 px 视口](images/feature-tour-0.3.2/26-compact-800.png)

![27 390 px 视口](images/feature-tour-0.3.2/27-compact-390.png)

截图流程未出现页面 JavaScript 异常。符号链接权限限制、真实启动样本及回滚测试结果统一列在[验证记录](validation-0.3.2.md)，不以界面截图代替底层验证。
