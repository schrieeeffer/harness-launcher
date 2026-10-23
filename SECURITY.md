# 安全与数据说明 · Security and data handling

[简体中文](#简体中文) · [English](#english)

## 简体中文

本启动器是一个本机运行的开发预览版工具。它只监听 `127.0.0.1`，每个进程使用随机访问令牌，校验请求的 Host 和 Origin，界面资源全部来自本地，不依赖任何 CDN。**请不要通过隧道或公网反向代理暴露它的端口。**

插件发现会把你输入的搜索词发送到 npm（或你明确选择的 npmmirror 镜像）以及 GitHub。启动器使用匿名的公开接口，不上传配置文件、会话或凭据。搜索结果不代表安全审核。只有在你开始一个记录窗口之后，启动器才会通过 Harness 包管理器安装插件，这一步会运行第三方代码。

如果你把私人内容放在受记录的规则、技能或配置文件中，快照里也会包含这些内容。请妥善保管启动器的数据目录和补丁暂存目录。记录范围默认排除标准的凭据和会话位置，但无法识别嵌在受记录文件中的任意机密信息。

dsh-purge 新建记录通过内容哈希校验解包目录与预设；缺少历史内容哈希的旧记录保留元数据校验。原始归档、重放的文件内容和新建目录移动事务同样使用内容哈希校验。这是一个恢复工具，不是防篡改的系统审计，也不是运行不可信插件的沙箱。

发现漏洞时，请优先使用仓库的私密安全报告功能（如果已开启）；否则请联系维护者，不要在公开 issue 中附上利用凭据或个人文件。

## English

This launcher is a local developer-preview tool. It binds to `127.0.0.1`, uses a per-process random token, checks Host and Origin, and serves local UI assets without CDN dependencies. **Do not expose its port through a tunnel or public reverse proxy.**

Discovery sends the entered search query to npm (or the explicitly selected npmmirror endpoint) and/or GitHub. It uses anonymous public APIs and does not upload profile files, sessions or credentials. Search results are not a security review. Installation runs third-party code through the Harness package manager only after the user starts a recording window.

Snapshots can contain private material if you place it in a tracked rules, skills or configuration file. Keep the launcher data directory and patch stashes private. File scopes exclude standard credential and session locations; they do not detect arbitrary secrets embedded in tracked files.

New dsh-purge records verify extracted folders and presets using content hashes; older records without historical hashes retain metadata checks. The original archive, replayed file blobs and new directory-move transactions also use content hashes. This is a recovery tool, not a tamper-proof system audit or a sandbox for untrusted plugins.

Report vulnerabilities through the repository's private security reporting feature if available; otherwise contact the maintainer without including exploit credentials or personal files in a public issue.
