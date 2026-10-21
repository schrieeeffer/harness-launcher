# 参与贡献 · Contributing

[简体中文](#简体中文) · [English](#english)

## 简体中文

### 开发环境

使用 Python 3.12+ 和 Node.js 22+（Node 只用于 JavaScript 语法检查）。项目没有任何 pip 或 npm 依赖。

提交前请运行：

```powershell
python -m unittest discover -v
node --check ui/app.js
```

测试使用临时目录、合成数据和模拟的公开接口，**绝不能**安装真实插件或修改真实的 Harness 配置。

### 代码原则

- 插件发现的元数据与安装流程保持分离。包名、描述、链接和仓库文件都视为不可信数据，界面中一律用 `textContent` 渲染。
- 插件目录只能访问固定的公开 registry 和 GitHub 接口，不能请求用户提供的任意地址。
- 安装前必须由用户明确确认，文件变更必须经过归属复核，不能跳过。
- 界面在严格的内容安全策略下运行：不要使用内联脚本、内联 `style` 属性或外部 CDN 资源。修改 `ui/index.html` 时，保留 `ui/app.js` 使用的元素 id。
- 改动界面后，请同时检查宽窗口、1120 px 以下和 760 px 以下的布局。

### 文档与截图

- 用户文档有中英两个版本：`README.md` / `README.en.md`，以及 `docs/patch-recording.md` / `docs/patch-recording.en.md`。**修改时请同时更新两种语言**，并保持标题结构一致。
- 截图放在 `docs/images/`，使用 PNG 格式。截图**只能**使用合成的 profile 和示例数据（例如 `C:\Users\demo`、`@demo/...` 这类插件名），不能出现真实的用户名、路径、插件配置或访问令牌。
- 界面变化影响到截图时，请重新截取对应的图片。
- 用户可见的变化请记录在 `CHANGELOG.md` 的 Unreleased 部分。
- 请写明支持的应用版本和平台，不要把基于测试夹具的测试描述成完整的平台认证。

### 不要提交

凭据、本机 profile、快照、日志，以及任何包含个人数据的截图。

### 报告问题

请附上启动器和 Harness 的版本、操作步骤、预期行为、实际行为，以及去除敏感信息后的错误信息。**不要**附上完整的 `.dsh` 目录或带访问令牌的地址。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

## English

### Development setup

Use Python 3.12+ and Node.js 22+ (Node is only used for the JavaScript syntax check). There are no pip or npm dependencies.

Before submitting, run:

```powershell
python -m unittest discover -v
node --check ui/app.js
```

Tests use temporary folders, synthetic data and mocked public APIs. They must **never** install a real plugin or change a real Harness profile.

### Code principles

- Keep discovery metadata separate from installation. Treat package names, descriptions, links and repository files as untrusted data and render them with `textContent`.
- The catalog may only fetch fixed public registry and GitHub endpoints, never arbitrary user-supplied URLs.
- Keep the explicit user confirmation before installation and the review of file-change attribution.
- The UI runs under a strict Content Security Policy: no inline scripts, inline `style` attributes or CDN assets. When editing `ui/index.html`, keep the element ids that `ui/app.js` relies on.
- After UI changes, check the wide layout as well as the layouts below 1120 px and 760 px.

### Documentation and screenshots

- User documentation exists in Chinese and English: `README.md` / `README.en.md` and `docs/patch-recording.md` / `docs/patch-recording.en.md`. **Update both languages together** and keep their heading structure aligned.
- Screenshots live in `docs/images/` as PNG files. They must **only** use a synthetic profile and sample data (for example `C:\Users\demo` and plugin names like `@demo/...`); real user names, paths, plugin configurations or access tokens must never appear.
- Retake affected screenshots when the UI changes.
- Record user-visible changes under Unreleased in `CHANGELOG.md`.
- Document supported application versions and platforms, and do not describe fixture-based tests as full platform certification.

### Do not commit

Credentials, local profiles, snapshots, logs, or screenshots containing personal data.

### Reporting bugs

Include the launcher and Harness versions, steps, expected and actual behavior, and a redacted error. Do **not** attach your full `.dsh` directory or URLs containing access tokens. Report security issues privately as described in [SECURITY.md](SECURITY.md).
