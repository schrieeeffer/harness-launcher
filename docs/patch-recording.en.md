# Patch recording and restoration: technical notes

[简体中文](patch-recording.md) · **English**

These notes are for users and contributors who want to understand how Harness Launcher changes, records and restores files. For everyday use, start with the [README](../README.en.md).

## Contents

- [Three ways a plugin is managed](#three-ways-a-plugin-is-managed)
- [What a switch does](#what-a-switch-does)
- [The built-in dsh-purge adapter](#the-built-in-dsh-purge-adapter)
- [Generic install recording](#generic-install-recording)
- [Plugin versions and combinations](#plugin-versions-and-combinations)
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
| **Unrecorded** | 未记录安装前状态 (no pre-install record) | Combination switches are blocked; originals cannot be inferred | Restore a clean installation first, then record through the launcher |

A plugin without a pre-install record is **not** assumed to be patch-free. While any such plugin exists, or while the `cordis` YAML files still contain manually added plugin entries or dynamic expressions after restoring, native mode refuses to claim a complete official state.

## What a switch does

When you click **Launch Harness** or **Apply**, the launcher runs these steps in order:

1. **Preconditions**: Harness, including its tray icon, has fully exited; the executable path is valid; no install recording is in progress; no file in the recorded scope changed outside a record; and recorded plugins being enabled have not changed version. If the previous switch was interrupted, the unfinished part is rolled back automatically first and you are asked to refresh and retry.
2. **Concurrency check**: `package.json` has not been modified by Harness or another program since it was read (its content digest is compared). Otherwise you are asked to refresh.
3. **Backup**: the current `package.json` is saved byte for byte to the launcher's `backups` folder.
4. **Bundle list**: `@deepseek-ai/dsh-base` and `@deepseek-ai/dsh-web-app` are always kept, existing plugins keep their order, and newly enabled plugins are appended. All other fields in `package.json` are preserved.
5. **Whole-switch preflight**: simulate undoing generic layers, switching the built-in adapter and applying target layers in a read-only file view. Verify the native baseline, installed packages and dependencies, both directions of recovery blobs, directory structure and target hashes. No application/profile files are changed and no full temporary application tree is created.
6. **Recheck**: verify the executable, packages and dependencies, indexes and all input file contents again immediately before execution. Stop if preflight inputs changed.
7. **One transaction**: persist an immutable plan and a small position journal; undo current layers, switch the adapter and bundle configuration, then apply target layers. Shared directories needed by independent plugins are tracked by the combination and cleaned up when everything is off.
8. **Verify and register**: compare actual output with the entire expected combination, save its directory fingerprint, native baseline reference and combination record, then persist the commit marker.
9. **Launch** (Launch Harness only): start Harness with `DSH_HOME` pointing at the current data directory.

Failed preflight leaves live files untouched. Execution or launch failure reverses completed operations and restores the previous files, configuration and combination index. Recovery handles a write before its cursor update and another interruption during rollback. Unknown changes made after interruption stop overwriting and preserve the journal.

Switching never touches accounts, sessions, plugin dependencies or ordinary preferences; native mode does not wipe user data. Disabling a plugin does not uninstall it either: it stays in `node_modules` and `dependencies` and can be enabled again later. Compatibility and dependencies between newly added plugins remain the responsibility of the plugins and Harness.

## The built-in dsh-purge adapter

dsh-purge modifies the application itself, so it uses a dedicated adapter rather than a generic record:

- **First verification**: looks for `app.asar`, the stashed `official.asar` for the current installation and data directory, then `app.asar.bak`, in that order. The archive's embedded integrity information is verified and its version and hash are recorded. A missing patch record does not prevent finding a previously stashed archive; recreating the record still requires integrity checks and a matching patched-folder version.
- **When disabled**: `resources\app` is stashed in a separate folder inside the install folder and the official `app.asar` is used. The plugin's prompts, recognized injected rules, red-team presets and CLI path are restored as well.
- **When enabled**: the captured patch folder and injected files for the same version are restored. Patch scripts that might modify other installations are **not** run again.
- **Journal and rollback**: the whole-switch transaction covers built-in and generic layers together. Failures roll back; an interruption is recovered before the next operation. The runtime uses only the new journal; finish old recovery in the previous version before migrating.
- **Version changes**: when the application or plugin is upgraded, files are modified externally, folders conflict or the official archive is missing, the adapter stops overwriting and reports why. Old patches are never applied to a new application. After an upgrade, re-apply the plugin on the current version and take a new snapshot; old snapshots are kept for inspection.

The extracted folder is guarded with file metadata to detect ordinary edits; the original archive and replayed file contents are verified with content hashes. Installed dsh-purge code is versioned too; host-provided SDKs are bound to the retained official archive. dsh-purge cannot be recorded generically through manual add, and its upgrades do not go through **Upgrade / re-record**. After a Harness upgrade the official baseline must be re-verified as well.

## Generic install recording

### Flow

1. **Before** installing, open **Add manually** and enter the exact plugin package name.
2. Choose a source: an npm package (optionally with a version or tag), a local folder, a Git repository URL, or "install it yourself". The launcher takes a snapshot first, then installs through Harness's official dsh CLI (`plugin --profile desktop add <source>`). With "install it yourself", install the plugin inside Harness after the snapshot finishes.
3. After backup and installation, Harness opens automatically. Use this plugin normally; first-run file changes are included without a separate test-completion confirmation.
4. Fully quit Harness, including its tray process. The background supervisor waits until the process has exited and file metadata has been stable for at least two seconds, then compares and saves automatically. Saving rechecks the entire managed scope, plugin inventory and versions, loading combination, and the Harness executable hash, and verifies file copies for restoration and reapplication. There are no routine manual scan, review or save controls.

The service checks the installation session once a second, independently of the browser. It never assumes a first run completed without observing the program running; a launch still unobserved after 30 seconds pauses recording. Process observation is a conservative Windows check, not a functional test of the plugin.

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

In the exception-page change list and exported JSON, each file is written as `scope|relative path`, where `|.` means the scope itself. For example, `skills|desk-pet/SKILL.md` is `<data directory>\skills\desk-pet\SKILL.md`. The launcher's own `.harness-launcher-*` stash folders are never recorded.

### Excluded by default

Account credentials, sessions, attachments and caches are outside patch recording. `node_modules`, `package.json` and lock files are not replayed as ordinary file layers. New version records separately retain installed plugin and resolvable dependency contents, dependency edges and profile installation metadata as recovery artifacts. Disabling does not uninstall or reinstall dependencies or restore an entire profile.

### Links and special files

Links in a folder, including Windows junctions, are recorded by their target only; external paths are never followed. If a link is added or changed during the recording window, or a path switches between file and folder, automatic recording is refused. Recording also stops on any other unsupported file type.

### Attribution and automatic verification

A record captures file differences **within a time window**, not a process audit; automatic checks cannot prove which process wrote a file. Work with only this plugin and do not edit other files during recording. Other-plugin installations, removals, upgrades or loading-combination changes block registration. New files and changes after scanning also block saving. Stable manual changes within scope may still be included: automatic recording must not be described as complete process attribution.

Normal records save automatically. Installer or launch failures, ongoing writes and verification failures retain the original baseline. Fix the issue and select **Continue automatic recording**. After a crash or service restart, unfinished sessions enter an exception state instead of silently adopting changes made during downtime. Continuing observes another run or rescans. Finish old incomplete sessions in the previous version before running the migration tool.

Cancellation is allowed only when the plugin inventory and original files are unchanged. Once installation or file changes occur, cancellation cannot discard the record. The launcher does not automatically uninstall plugins or delete installed dependencies.

### Shared files, layering and base combinations

When several plugins change the same file, the launcher checks the baseline hash and never forces a merge. If a plugin was recorded on top of files already changed by another patching plugin, **Patch records** shows the **base combination at recording time**. Enabling it may require selecting that base combination as well, or recording it again on a new baseline. When everything is disabled, independent or stackable layers are undone one by one in reverse order.

### Upgrading and re-recording

To upgrade a recorded plugin, click **Upgrade / re-record** in **Patch records** and choose the new source (for example an npm version), or keep "install it yourself" and upgrade inside Harness.

- Verify and retain the currently installed package and dependencies before undoing its old layer and taking a new file baseline.
- Replace the current record only after installation, the first run and verification finish. Old package/dependency contents, the delta and immutable revision remain in history; the new revision references its parent.
- Old files the new version no longer writes are not carried over.
- A version change during verification pauses saving; continuing after fixing the issue rescans.
- **Recording must start before the upgrade.** Upgrading outside the launcher, patches added outside a record by later runs, and other external modifications all trigger the protection; the launcher will not infer or guarantee restoration for them.

A plugin that is already installed without a record cannot have its unknown history recovered from a post-install snapshot, so the launcher refuses to start recording it.

## Plugin versions and combinations

- **Plugin revisions**: immutable content-addressed revisions link source, actual version, before/after snapshots, delta and base-combination revision references. Installed package/dependency files are retained with required, optional and peer dependency edges and existing lock files. Identical contents share blobs. Missing required dependencies, unstable package contents or unsupported nested links pause saving.
- **One-time conversion**: `migrate.py` separately converts old indexes, snapshot/delta references, adapter records, schemes and configuration backups. Original metadata is archived under the data directory's `migrations/`; old blobs and snapshot files are retained. Runtime code accepts only the current index and content-addressed references, with no old-format branches. Incomplete old history remains in the checkpoint; current installed contents enter the new version store. Interrupted conversion restores only known metadata, leaving live files untouched.
- **Native baseline**: keyed by the Harness executable hash, retains managed contents after all known patches are undone and official archive information. Every combination must return to that same base. An extra `app.asar.bak` is not the active official baseline. Accounts, sessions and installed dependencies stay in place.
- **Combinations**: retain actual plugin versions, revision references, layer order, final bundle order, native base and expected file contents. Identical results share a record; failed switches do not register the target as current.
- **Scheme and backup constraints**: newly saved schemes and configuration backups pin plugin versions, record references and Harness version. Changes stop their use; inspect and re-save the scheme. Migrated schemes/backups bind current installed versions, and portable imports bind current local versions again. Exports keep the existing name-only format and do not share local version artifacts or personal paths.

This phase retains versions and reliably switches supported combinations of currently installed versions. It does not yet expose one-click installation of historical versions or automatically merge text/binary patches. Configuration backups are not full plugin downgrades. Actual file conflicts are reported during preflight; this does not establish compatibility of every possible plugin combination.

## Protection and recovery

- **Directory fingerprint**: after a record is saved or a switch completes, the launcher records the directory fingerprint of the managed scope. If changes made outside a record are found before the next switch, automatic overwriting stops.
- **Interrupted switches**: save the full immutable plan once and update a small position journal and final commit marker. The next operation rolls back an unfinished switch and asks you to refresh; an already committed transaction only needs journal cleanup. Recovery also persists its position. Unknown changes stop overwriting, and new installations cannot start before recovery completes.
- **Full verification and deduplication**: automatic recording hashes file contents and reads the entire scope again before saving. Both original and patched copies must verify before registration. Copies are deduplicated by hash; large trees may take time. Replay verifies every required bidirectional copy before mutating its first file.
- **Background jobs**: backup, installation, first-run supervision and saving continue after the menu closes. A normal active recording keeps the service alive until completion; exceptions remain on disk for the next visit. Retrying never replaces the original baseline.

## Storage

Snapshots and state are kept in `%USERPROFILE%\.harness-launcher\patches\<id>\`. The `<id>` is derived from the Harness install folder and Harness data directory. Runtime data is separate from source code. Use `--data <directory>` for another location; keep the blobs and stash folders with their index. The default avoids Windows Store AppData virtualization. `migrate.py --apply` imports the old AppData store once and retains its source. Interrupted copying can resume; links and conflicting contents stop the import.

| Path | Contents |
| --- | --- |
| `state.json`, `blobs\` | State, recovery journal and file contents of the built-in adapter |
| `tracking\registry.json` | Index, history and current session of generic records |
| `tracking\blobs\` | File copies stored by content hash |
| `tracking\snapshots\`, `tracking\deltas\` | Content-addressed snapshots and deltas; convert old UUID lists first |
| `tracking\packages\`, `tracking\revisions\` | Package/dependency manifests and immutable plugin revisions |
| `tracking\baselines\`, `tracking\combinations\` | Native bases, pre-install configuration and combination records |
| `tracking\transactions\`, `tracking\switch.json` | Immutable whole-switch plans and active transaction position |
| `<launcher data directory>\migrations\`, `migration-pending.json` | Pre-migration metadata archives and active one-time conversion journal |
| `<launcher data directory>\import-source.json`, `import-pending.json` | Original store location and resumable import state |
| `tracking\logs\` | Output of the official installer |

Files that are currently inactive are stashed in `resources\.harness-launcher-<id>` in the install folder and `.harness-launcher-<id>` in the data directory; they may hold official archives, plugin folders and presets. **Do not delete these folders by hand.**

## Out of scope

The registry, system services, drivers, network-side changes, other installations and any folders outside the scope above are neither recorded nor restored. Automatic recording is not a whole-system restore.

## Removing the launcher

1. Choose the mode you want to keep and click **Apply**.
2. Deleting the launcher's shortcut and source code does not roll back any application files.
3. The `.harness-launcher-*` stash folders and `patches` snapshots still hold inactive versions; after deleting them you can no longer switch back to those versions. Only delete them when you are sure you no longer need them.
