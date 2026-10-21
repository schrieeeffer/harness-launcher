# Patch recording and restoration: technical notes

[简体中文](patch-recording.md) · **English**

These notes are for users and contributors who want to understand how Harness Launcher changes, records and restores files. For everyday use, start with the [README](../README.en.md).

## Contents

- [Three ways a plugin is managed](#three-ways-a-plugin-is-managed)
- [What a switch does](#what-a-switch-does)
- [The built-in dsh-purge adapter](#the-built-in-dsh-purge-adapter)
- [Generic install recording](#generic-install-recording)
- [Protection and recovery](#protection-and-recovery)
- [Storage](#storage)
- [Out of scope](#out-of-scope)
- [Removing the launcher](#removing-the-launcher)

## Three ways a plugin is managed

Every installed plugin falls into one of three categories. The badges in the interface and the groups in **Patch records** are based on them:

| Category | Badge | When disabled | When enabled |
| --- | --- | --- | --- |
| **Built-in adapter** | 支持自动还原补丁 (supports automatic restore) | Uses the preserved official archive and stashes the plugin's injected files | Restores the patch snapshot for the same version |
| **Generic record** | 已自动记录补丁 (patch recorded) | Undoes the recorded file differences in reverse install order | Re-applies the file differences in install order |
| **Unrecorded** | 未记录安装前状态 (no pre-install record) | Only removed from the bundle list; files it wrote are **not** restored | Added to the bundle list |

A plugin without a pre-install record is **not** assumed to be patch-free. While any such plugin exists, or while the `cordis` YAML files still contain manually added plugin entries or dynamic expressions after restoring, native mode refuses to claim a complete official state.

## What a switch does

When you click **Launch Harness** or **Apply**, the launcher runs these steps in order:

1. **Preconditions**: Harness, including its tray icon, has fully exited; the executable path is valid; no install recording is in progress; no file in the recorded scope changed outside a record; and recorded plugins being enabled have not changed version. If the previous switch was interrupted, the unfinished part is rolled back automatically first and you are asked to refresh and retry.
2. **Concurrency check**: `package.json` has not been modified by Harness or another program since it was read (its content digest is compared). Otherwise you are asked to refresh.
3. **Backup**: the current `package.json` is saved byte for byte to the launcher's `backups` folder.
4. **Bundle list**: `@deepseek-ai/dsh-base` and `@deepseek-ai/dsh-web-app` are always kept, existing plugins keep their order, and newly enabled plugins are appended. All other fields in `package.json` are preserved.
5. **Undo recorded layers**: every currently enabled generic record is undone in reverse install order.
6. **Switch the built-in adapter**: dsh-purge switches between plugin mode and native mode, and the new `package.json` is written atomically.
7. **Apply recorded layers**: the generic records of the plugins being enabled are re-applied in install order.
8. **Seal the fingerprint**: the current directory fingerprint of the managed scope is recorded for comparison before the next switch.
9. **Launch** (Launch Harness only): Harness starts with `DSH_HOME` pointing at the current data directory.

If any of steps 5–9 fails, the completed operations are rolled back in reverse order and the previous `package.json` is restored. If the rollback itself fails, the launcher stops and asks you to keep the snapshots and logs rather than overwriting anything further.

Switching never touches accounts, sessions, plugin dependencies or ordinary preferences; native mode does not wipe user data. Disabling a plugin does not uninstall it either: it stays in `node_modules` and `dependencies` and can be enabled again later. Compatibility and dependencies between newly added plugins remain the responsibility of the plugins and Harness.

## The built-in dsh-purge adapter

dsh-purge modifies the application itself, so it uses a dedicated adapter rather than a generic record:

- **First verification**: the preserved official `app.asar` is verified, including the integrity information embedded in the archive, and its version and hash are recorded.
- **When disabled**: `resources\app` is stashed in a separate folder inside the install folder and the official `app.asar` is used. The plugin's prompts, recognized injected rules, red-team presets and CLI path are restored as well.
- **When enabled**: the captured patch folder and injected files for the same version are restored. Patch scripts that might modify other installations are **not** run again.
- **Journal and rollback**: a recovery journal is written before each operation. Any failed step is rolled back, and after an interruption the next switch rolls back the unfinished operation first.
- **Version changes**: when the application or plugin is upgraded, files are modified externally, folders conflict or the official archive is missing, the adapter stops overwriting and reports why. Old patches are never applied to a new application. After an upgrade, re-apply the plugin on the current version and take a new snapshot; old snapshots are kept for inspection.

The extracted folder is guarded with file metadata to detect ordinary edits; the original archive and replayed file contents are verified with content hashes. dsh-purge cannot be recorded generically through manual add, and its upgrades do not go through **Upgrade / re-record**. After a Harness upgrade the official baseline must be re-verified as well.

## Generic install recording

### Flow

1. **Before** installing, open **Add manually** and enter the exact plugin package name.
2. Choose a source: an npm package (optionally with a version or tag), a local folder, a Git repository URL, or "install it yourself". The launcher takes a snapshot first, then installs through Harness's official dsh CLI (`plugin --profile desktop add <source>`). With "install it yourself", install the plugin inside Harness after the snapshot finishes.
3. Click **Launch and test** to test only this new plugin and apply its own settings and patches. Then fully quit Harness, including the tray icon, and click **Quit, scan differences**.
4. Review the list of added, modified and deleted files in the recording window, and save once you have confirmed they belong to the plugin. No plugin-specific adapter is needed afterwards: disabling undoes the changes in reverse install order, and enabling re-applies them in install order.

If the installer fails, the pre-install snapshot is kept so you can fix the problem and continue recording; the installer's output is saved in `patches\<id>\tracking\logs`. If the package name you entered cannot be found among the installed results, recording stops and asks you to check the source.

### Recorded scope

| Scope | Location | Notes |
| --- | --- | --- |
| `app` | `<install folder>\resources\app` | Extracted application folder |
| `archive` | `<install folder>\resources\app.asar` | Application archive |
| `archive_backup` | `<install folder>\resources\app.asar.bak` | Archive backup |
| `unpacked` | `<install folder>\resources\app.asar.unpacked` | Native files outside the archive |
| `cli` | `<install folder>\resources\runtime\cli` | Command-line runtime |
| `agents` | `<data directory>\AGENTS.md` | |
| `claude` | `<data directory>\CLAUDE.md` | |
| `prompt` | `<data directory>\prompt-inject.md` | |
| `rules` | `<data directory>\rules` | |
| `skills` | `<data directory>\skills` | |
| `presets` | `<data directory>\.agent-presets` | |
| `user_yaml` | `<data directory>\profiles\desktop\cordis.patch.yml` | |
| `root_yaml` | `<data directory>\profiles\desktop\cordis.yml` | |

In the review list and the exported JSON, each file is written as `scope|relative path`, where `|.` means the scope itself. For example, `skills|desk-pet/SKILL.md` is `<data directory>\skills\desk-pet\SKILL.md`. The launcher's own `.harness-launcher-*` stash folders are never recorded.

### Excluded by default

Account credentials, sessions, attachments, caches, `profiles\desktop\node_modules`, and install metadata such as `package.json` and lock files are outside the recorded scope. Plugin dependencies stay installed; the launcher controls whether a plugin loads through the bundle list, and disabling a plugin does not uninstall its dependencies.

### Links and special files

Links in a folder, including Windows junctions, are recorded by their target only; external paths are never followed. If a link is added or changed during the recording window, or a path switches between file and folder, automatic recording is refused. Recording also stops on any other unsupported file type.

### Attribution and review

A record captures the file differences **within a time window**; it is not a process audit. If other files were changed or several plugins were installed during the window, the attribution may be wrong, which is why you must review the list. Recording is refused if another plugin's version changed during the window.

### Shared files, layering and base combinations

When several plugins change the same file, the launcher checks the baseline hash and never forces a merge. If a plugin was recorded on top of files already changed by another patching plugin, **Patch records** shows the **base combination at recording time**. Enabling it may require selecting that base combination as well, or recording it again on a new baseline. When everything is disabled, independent or stackable layers are undone one by one in reverse order.

### Upgrading and re-recording

To upgrade a recorded plugin, click **Upgrade / re-record** in **Patch records** and choose the new source (for example an npm version), or keep "install it yourself" and upgrade inside Harness.

- The old recorded layer is undone first, then a new baseline is taken.
- The current record is replaced only after installation and testing finish and you confirm the differences; the old record and its difference index are kept as history.
- Old files the new version no longer writes are not carried over.
- If the version changes again during review, a new scan is required.
- **Recording must start before the upgrade.** Upgrading outside the launcher, patches added outside a record by later runs, and other external modifications all trigger the protection; the launcher will not infer or guarantee restoration for them.

A plugin that is already installed without a record cannot have its unknown history recovered from a post-install snapshot, so the launcher refuses to start recording it.

## Protection and recovery

- **Directory fingerprint**: after a record is saved or a switch completes, the launcher records the directory fingerprint of the managed scope. If changes made outside a record are found before the next switch, automatic overwriting stops.
- **Interrupted switches**: each batch of file operations writes its replay position first. After an interruption, the next switch rolls back the unfinished part automatically and asks you to refresh and retry. If files were changed again after the interruption, the launcher stops and asks for a manual check rather than risking a rollback. A new install recording cannot start until recovery is complete.
- **Snapshot reuse**: the first snapshot copies the source files and may be slow; later snapshots reuse unchanged content based on file size and timestamps.
- **Background jobs**: snapshots and installs run in the background with progress shown in the window. Closing the launcher window does not cancel an official installer that is already running, and the background service waits for the job to finish before exiting.

## Storage

Snapshots and state are kept in `%LOCALAPPDATA%\HarnessLauncher\patches\<id>\`. The `<id>` is derived from both the install folder and the data directory, so different installations or data directories never interfere with each other:

| Path | Contents |
| --- | --- |
| `state.json`, `blobs\` | State, recovery journal and file contents of the built-in adapter |
| `tracking\registry.json` | Index, history and current session of generic records |
| `tracking\blobs\` | File copies stored by content hash |
| `tracking\snapshots\`, `tracking\deltas\` | Snapshots and difference lists |
| `tracking\cache.json` | File timestamps used to reuse snapshots |
| `tracking\logs\` | Output of the official installer |

Files that are currently inactive are stashed in `resources\.harness-launcher-<id>` in the install folder and `.harness-launcher-<id>` in the data directory; they may hold official archives, plugin folders and presets. **Do not delete these folders by hand.**

## Out of scope

The registry, system services, drivers, network-side changes, other installations and any folders outside the scope above are neither recorded nor restored. Automatic recording is not a whole-system restore.

## Removing the launcher

1. Choose the mode you want to keep and click **Apply**.
2. Deleting the launcher's shortcut and source code does not roll back any application files.
3. The `.harness-launcher-*` stash folders and `patches` snapshots still hold inactive versions; after deleting them you can no longer switch back to those versions. Only delete them when you are sure you no longer need them.
