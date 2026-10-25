# Harness Launcher

A local plugin launcher and maintenance UI for DeepSeek Harness. Current release: **0.4.1**, Windows x64 preview. Independently maintained, not affiliated with DeepSeek.

[简体中文 / complete usage guide](README.md) · [Download](https://github.com/schrieeeffer/harness-launcher/releases/tag/v0.4.1) · [Screenshots](docs/feature-tour-0.4.0.md) · [Runtime recovery](docs/runtime-repair.md) · [Validation](docs/validation-0.4.0.md)

![Launcher](docs/images/feature-tour-0.4.0/01-overview.png)

## Get started

Install and open official Harness once. Download `Harness-Launcher-0.4.1-Windows-x64.zip`, extract the complete archive and double-click `Harness Launcher.exe`. Python is bundled. Set the Harness executable in Settings if detection fails. The EXE is unsigned; download from this repository and verify `SHA256SUMS.txt` if needed.

If an official update leaves runtime files missing, open **Settings → Official runtime repair**. Close Harness, verify the same-version official installer, review the missing files and repair. Installed 7-Zip is required. Authenticode publisher checks and SHA-512 precede extraction; installed EXE/ASAR hash comparisons precede repair approval. Repair only adds absent files and refuses conflicting existing contents. It preserves profiles, conversations, plugin records and rollback data. Complete checks are explicit and may take several minutes; daily launch checks only a few mandatory files.

For source use, Python 3.12+ is required: open `launch.vbs` or run `python launcher.py`. Source uses only the Python standard library. The interface is in Simplified Chinese.

## Features

- Toggle plugins to edit a draft. Launch applies the selection and starts Harness; Apply without launching only commits the combination. Progress reports actual phases, counts and elapsed time.
- Save named combinations and import/export schemes with a preview. Version constraints prevent silent substitutions.
- Discover npm/GitHub plugins, browse popularity lists, or add npm, Git and local sources. Record installation and first-run changes before replaying them.
- Inspect patch records and configuration backups. Purge uses its own adapter; ordinary plugins use recorded file deltas.
- Check updates by source, preview upgrades and uninstall, create maintenance checkpoints and restore after verifying conflicts.
- Reclaim unreferenced content objects after a grace period, retaining referenced histories, backups and recovery points. Shared dependencies, external local source directories and user data are protected by preflight checks.

See [maintenance scope](docs/plugin-maintenance.md), [host update behavior](docs/host-updates.md) and the [complete Chinese guide](README.md). Quit Harness and restore native mode before official updates. The launcher does not intercept Harness's updater; changed host identities stop incompatible patch replay.

## Data and limits

Data defaults to `%USERPROFILE%\.harness-launcher`. Preserve it for recovery. The server binds only to `127.0.0.1` with a random token; discovery and update checks access the network. Only the current data format is accepted; unsupported formats and corruption stop operations without rewriting the original data.

Daily launch uses verified content and NTFS checkpoints; update checks and garbage collection are explicit maintenance operations. Local full-window retests took **20.8–35.1 seconds**, with a **42.7-second** slow sample. This is not a strict 30-second guarantee. Initial verification, downloads and changed files may take longer. Screenshots use isolated example data.

Quit the launcher backend before removing its program directory. Removing the launcher does not undo third-party plugin changes; use the maintenance previews first and retain recovery data.

## Development

```powershell
python -m unittest discover -v
node --test test_ui.cjs
python packaging/build_portable.py
```

[Architecture](docs/architecture.md) · [Patch recording](docs/patch-recording.en.md) · [Maintenance](MAINTENANCE.en.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Current release notes](CHANGELOG.md) · [MIT license](LICENSE)
