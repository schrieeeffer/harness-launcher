# Maintenance and AI handoff

[简体中文](MAINTENANCE.md) · **English**

Use this guide when the project owner, Claude Code or another maintainer takes over. `VERSION` in `launcher.py` is the source of truth. See the [architecture contract](docs/architecture.md) for 0.3.0 boundaries, format 3 and evolution rules, the [technical guide](docs/patch-recording.en.md) for restoration, and [CONTRIBUTING.md](CONTRIBUTING.md) for contribution rules.

## 1. Project and runtime locations

These are example locations, not required installation paths. Check the launcher's settings page and `settings.json` for the actual paths.

| Location | Purpose | Maintenance |
| --- | --- | --- |
| `D:\projects\harness-launcher` | Example Git repository location for development, tests and commits | Open your actual repository folder in your editor or AI tool |
| `%USERPROFILE%\.harness-launcher` | Launcher settings, pre-install snapshots, plugin versions, combinations and transactions | Back up separately; keep out of Git |
| `C:\Apps\DeepSeek Harness` | Example Harness installation containing the application and `resources` | Preserve the installed version; do not develop the launcher here |
| `%USERPROFILE%\.dsh` | Harness conversations, settings, desktop profile and installed plugins | Contains private data; back up separately |
| `%USERPROFILE%\.harness-launcher.archive-*` | Complete previous store retained during migration | Never read by the new runtime; retain with its checksum manifest |

`--data` selects launcher data. `DSH_HOME` / the saved `home` setting selects Harness data. `DSH_EXECUTABLE` / the saved `executable` setting selects the Harness executable. Subsequent starts read saved settings. Check the effective configuration before changing paths; these are three different directories.

The default launcher store is in the user home to avoid Windows Store AppData virtualization. Do not casually move it back to the old location. Git manages source code and cannot restore runtime data.

## 2. Source tree

```text
harness-launcher/
├── launcher.py                 # HTTP service, settings, schemes, configuration backups, launch
├── domain.py / recording_state.py # File value objects, decoding, recording state machine
├── operation_models.py / operations.py # Transaction command types, operation status API
├── storage.py / settings_store.py # Content addressing, atomic writes, settings contract
├── runtime_context.py / errors.py # Installation paths, stable error protocol
├── adapters.py                 # Plugin ownership rules and host archive paths
├── catalog.py                  # Official / npm / GitHub discovery and leaderboards
├── plugin_tracking.py          # Mandatory recording, first-run supervision, validation and saving
├── patch_modes.py              # dsh-purge adapter, official ASAR verification, file primitives
├── version_store.py            # Installed packages/dependencies, immutable revisions and combinations
├── file_view.py                 # Read-only view simulating the complete switch
├── switch_plan.py               # Preflight, unified transactions, failure and crash recovery
├── migrate.py                   # Offline v2→v3 staging, verification, cutover and archival
├── launch.vbs / bootstrap.ps1   # Windows startup without a console
├── ui/
│   ├── index.html              # Page and dialogs
│   ├── style.css               # Styling and responsive layouts
│   ├── api.js / state.js       # HTTP transport, immutable state and reducer
│   └── runtime.js / app.js     # Async coordination, DOM rendering and events
├── test_*.py                   # Standard-library unittest with isolated synthetic fixtures
├── test_ui.cjs                 # Node built-in tests for connection and button-state regressions
├── .github/workflows/ci.yml     # Windows, Python 3.12 / 3.13, Node.js 22
├── docs/                       # Bilingual technical guides and synthetic screenshots
├── README.md / README.en.md     # User guides
├── MAINTENANCE.md / MAINTENANCE.en.md # This guide
├── CONTRIBUTING.md             # Code, validation, documentation and privacy rules
└── CHANGELOG.md / SECURITY.md   # Change history and security reporting
```

The UI has no build step. The runtime uses only the Python standard library. Do not run `npm install` or install pip packages just to start this project. UI work mostly lives in `ui/`; restoration spans recording, version storage, file views and transactions, so changing a button alone is insufficient.

## 3. Environment and startup commands

Open PowerShell in the repository:

```powershell
Set-Location 'D:\projects\harness-launcher' # Replace with your actual repository path
git status --short --branch
python --version
node --version
```

Python **3.12+** is required. Node.js **22+** runs JavaScript syntax checks and UI logic tests. If the system `python` does not meet the requirement, use the Python bundled with Harness. Replace the example installation path below; later commands reuse this variable:

```powershell
$launcherPython = 'C:\Apps\DeepSeek Harness\resources\runtime\primary-runtime\dependencies\python\python.exe'
& $launcherPython --version
$env:PYTHONUTF8 = '1'
& $launcherPython launcher.py
```

On another machine, set `$launcherPython` to a valid Python 3.12+ absolute path. With a suitable system Python, `python launcher.py` also works.

Double-click `launch.vbs` for normal use. `bootstrap.ps1` prefers `pythonw.exe` from the saved Harness installation, falling back to `pythonw.exe` on PATH. It reads the default launcher store. Use the CLI for custom data:

```powershell
& $launcherPython launcher.py --data 'D:\private\launcher-data'
& $launcherPython launcher.py --no-open
```

These are separate startup examples. `--no-open` still starts the service and recording supervisor; it is not a preview or test mode. `--data` isolates launcher records only, **not the actual Harness application or user data**. Prefer the synthetic fixtures in the next section for development testing.

The service binds to `127.0.0.1`, normally port 17643, with a temporary port if occupied. `instance.json` contains the access token; keep it out of issues and Git. Restart the backend after editing Python. Opening the shortcut again reuses an existing service; refreshing a page does not reload Python code.

The service keeps running and recording after the menu closes; missing browser heartbeats no longer stop it. Use **Exit launcher service** in Settings to stop it. Active installation, recording or switching blocks exit; paused exception records remain on disk. Before maintenance or backups, let normal recordings save, resolve exceptions, fully quit Harness including its tray process, exit the launcher service, and confirm `instance.json` was removed. A foreground development service can be stopped with `Ctrl+C` when no recording or switch is active. Never kill all Python processes or delete a journal to pretend shutdown succeeded. For a stale `instance.json`, first check its PID's command line and listening port to establish that the service is stopped. Use the newly opened menu after a service restart; refreshing an old page cannot replace its token.

## 4. Tests and development workflow

Run in the project root, using the Python variable above:

```powershell
& $launcherPython -m unittest discover -v
node --check ui/api.js
node --check ui/state.js
node --check ui/runtime.js
node --check ui/app.js
node --test test_ui.cjs
git diff --check
```

For focused diagnosis:

```powershell
& $launcherPython -m unittest test_catalog test_launcher -v
& $launcherPython -m unittest test_plugin_tracking test_patch_modes -v
& $launcherPython -m unittest test_version_plan test_migrate -v
```

Tests generate synthetic profiles, ASAR archives and plugin files in temporary folders, mocking installation processes and remote interfaces. Do not replace fixtures with a real `.dsh` or install real plugins for tests. Recovery changes need failure, interruption and unknown-file-change coverage. UI changes require wide layouts and layouts below 1120 px and 760 px, retaining element ids used by `app.js`.

Start by examining the working tree and relevant tests, preserving uncommitted user work. Use a branch for the specific task, run suitable validation, and update both language editions and `CHANGELOG.md`. Review `git diff` and `git status` before committing, and stage only relevant code and documentation. Follow the current user's explicit instructions for pushing or merging; do not force-push or use `git reset --hard` to erase someone else's work.

The CI configuration is in `.github/workflows/ci.yml`. Passing fixtures cannot replace real path and permission checks or establish that all third-party plugin combinations are compatible.

## 5. Data backups

### Backup scope

| Type | Restores | Limitations |
| --- | --- | --- |
| Git repository | Launcher source and documentation | No user configuration, plugins, snapshots or Harness |
| UI configuration backup (配置备份) | Desktop configuration and plugin choices under matching version constraints, through unified switching | No historical plugin downgrade or complete data backup |
| Offline copies from the same point in time | Launcher records, Harness application and Harness user data | Keep all three together, preserving paths, versions, timestamps and links |

Keep the third type before long-term maintenance or recovery-engine changes. Back up:

- The complete launcher store, including `settings.json`, `patches`, `backups`, migration reports and transaction journals. Separately retain sibling `.archive-*` directories, checksum manifests and any pending `.upgrade.json` cutover journal.
- The current Harness installation, including `resources\.harness-launcher-*` stashes and retained archives.
- The current Harness data directory, including installed plugins and `.harness-launcher-*` stashes.

Plugins may use junctions or symbolic links to dependencies or local source outside these three directories. Back up those external targets too. The commands below preserve links themselves without following them to copy external contents. A preserved link is not a backup of its target. Copies may include conversations, account information and private files; use a trusted backup location and do not upload them to GitHub.

### Windows offline-copy example

**First stop the service as described in section 3, with no installation or switch in progress. These are commands for a maintainer to run manually, not an automatic launcher feature.** Adjust `$launcherData` if using a custom store. Use a new empty backup folder outside all three source directories:

```powershell
$ErrorActionPreference = 'Stop'
$launcherData = Join-Path $env:USERPROFILE '.harness-launcher'
if (Test-Path -LiteralPath (Join-Path $launcherData 'instance.json')) {
    throw 'Exit the launcher service first; do not delete its instance file.'
}
if (Get-Process -Name 'DeepSeek Harness' -ErrorAction SilentlyContinue) {
    throw 'Fully exit Harness, including its tray process.'
}
$settings = Get-Content -LiteralPath (Join-Path $launcherData 'settings.json') -Raw -Encoding utf8 | ConvertFrom-Json
$harnessInstall = (Resolve-Path -LiteralPath (Split-Path -Parent $settings.executable)).Path
$harnessHome = (Resolve-Path -LiteralPath $settings.home).Path
$backupBase = 'D:\backups\harness-launcher'
$backupRoot = Join-Path $backupBase ((Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$layout = @(
    @{ Folder = 'launcher-data'; Path = $launcherData },
    @{ Folder = 'harness-install'; Path = $harnessInstall },
    @{ Folder = 'harness-home'; Path = $harnessHome }
)
# Check these paths before copying. Keep the backup outside every source tree.
$layout | Format-Table Folder, Path
New-Item -ItemType Directory -Path $backupRoot | Out-Null
foreach ($item in $layout) {
    & robocopy $item.Path (Join-Path $backupRoot $item.Folder) /E /COPY:DAT /DCOPY:DAT /SJ /SL /R:1 /W:1 /NP
    if ($LASTEXITCODE -ge 8) { throw ('Backup failed: ' + $item.Path) }
}
$layout | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backupRoot 'layout.json') -Encoding utf8
git rev-parse HEAD | Set-Content -LiteralPath (Join-Path $backupRoot 'source-commit.txt') -Encoding utf8
$backupRoot
```

Robocopy exit codes 0–7 are not copy failures; stop and investigate 8 or higher. Do not ignore errors when symbolic-link creation privileges are missing. `/COPY:DAT` and `/DCOPY:DAT` preserve data, attributes and timestamps; `/SJ` and `/SL` preserve junctions and symbolic links. This example does not retain all ACLs and is not a system image. Use a backup tool covering additional permissions, external targets or whole-machine recovery when required. Do not add `/MIR` or `/PURGE`, or clean out stashes. Treat a copy without `layout.json` as an incomplete backup. Preserve source separately in Git; `source-commit.txt` records committed HEAD only, not an uncommitted working tree.

## 6. Restoration and troubleshooting

### Normal restoration and interrupted operations

| Situation | Action |
| --- | --- |
| Disable plugins and restore recorded files | Fully exit Harness, select native mode (原生模式 / 全部关闭), apply the selection and wait for preflight and the transaction |
| Restore a configuration | Use configuration backups (配置备份); investigate version/record mismatches rather than removing version constraints |
| Installation recording exception | Keep the pre-install baseline, resolve the error and continue automatic recording (继续自动记录); never replace the baseline with already-modified files |
| Interrupted current-format combination switch | Keep `tracking\switch.json`, its `transactions` and blobs; the next normal switch attempts recovery first, then refresh and retry as directed |
| Interrupted v2→v3 directory cutover | Preserve the sibling `.upgrade.json`, stage and archive; stop the service and run `migrate.py --recover` |
| Interrupted earlier import | Keep `import-pending.json` / `migration-pending.json` and finish recovery in its matching old version; the new runtime does not infer old intermediate states |
| Recovery blocked by unknown changes, missing content or version drift | Stop repeated overwrite attempts, preserve the state and redacted logs, and check complete backups and versions |

Run only when conversion or resuming a migration is actually needed:

```powershell
& $launcherPython migrate.py
& $launcherPython migrate.py --apply
# Alternative: convert a custom data directory in place.
& $launcherPython migrate.py --data 'D:\private\launcher-data' --apply
# Only when the sibling .upgrade.json indicates interrupted cutover.
& $launcherPython migrate.py --data 'D:\private\launcher-data' --recover
```

The first two commands use the default directory; the third selects a custom store. Preview converts and validates a separate stage without changing the original store or Harness files. After verification, `--apply` renames the complete old store to a sibling `.archive-*` and installs the new root, retaining reachable current-format objects. `--recover` handles only the directory rename boundary, not damaged snapshots or earlier imports. Current data does not need repeated migration. There is no `--source` option or automatic AppData merge.

During long operations, `/api/operations` exposes the current id, kind and duration independently of the file-operation lock. Use error `code` for classification and `recovery` for advice; never parse message text or automatically replay writes. `/api/storage` provides content-store capacity by category. The runtime does not automatically delete history.

### Restoring offline copies

1. Stop Harness and the launcher service. Check `layout.json`, the code commit and that all three directories came from the same successful copy.
2. Preserve the current broken state: rename each current directory within its original parent. **Do not overlay old indexes on a changing application or merge old and new directories.**
3. Verify that the three targets in `layout.json` are the intended absolute paths, never drive roots. Restore to original paths. Different paths affect record identities, absolute paths and link targets; editing `settings.json` alone is not a complete migration.
4. Restore external link targets and preserve Harness/plugin versions, timestamps and stashes. This example uses local drive paths and rejects existing destinations; complete step 2 first.

```powershell
$ErrorActionPreference = 'Stop'
$backupRoot = 'D:\backups\harness-launcher\<the-successful-backup-folder>'
$layout = Get-Content -LiteralPath (Join-Path $backupRoot 'layout.json') -Raw -Encoding utf8 | ConvertFrom-Json
if ($layout.Count -ne 3 -or @($layout.Folder | Select-Object -Unique).Count -ne 3) { throw 'Check the backup layout before restoring.' }
foreach ($item in $layout) {
    if ($item.Folder -notin @('launcher-data', 'harness-install', 'harness-home')) { throw 'Unexpected backup folder.' }
    if (!(Test-Path -LiteralPath (Join-Path $backupRoot $item.Folder))) { throw 'Incomplete backup.' }
    if ($item.Path -notmatch '^[A-Za-z]:\\' -or [IO.Path]::GetPathRoot($item.Path).TrimEnd('\') -eq $item.Path.TrimEnd('\')) { throw 'Check the destination path.' }
    if (Test-Path -LiteralPath $item.Path) { throw ('Keep the current directory separately first: ' + $item.Path) }
}
foreach ($item in $layout) {
    & robocopy (Join-Path $backupRoot $item.Folder) $item.Path /E /COPY:DAT /DCOPY:DAT /SJ /SL /R:1 /W:1 /NP
    if ($LASTEXITCODE -ge 8) { throw ('Restore failed: ' + $item.Path) }
}
```

5. Start source code that reads this record format, check paths, installed versions, choices and recording state, then use the UI to preflight and apply the desired combination or native mode. Preserve the copy and investigate mismatches rather than forcing writes. Retain the broken-state copies until recovery is confirmed.

The sibling `.archive-*` holds the complete previous launcher store, not the Harness installation or user data. It does not replace a matched offline backup. Reverting Git does not revert the data schema; restore a complete set matching the desired format when necessary. Additive fields should retain stable meaning, and `requiredFeatures` must reject readers that lack required semantics. Contract-breaking changes still require versioned offline conversion; zero future migrations cannot be promised.

## 7. Prompt for Claude Code or another AI

Open your actual repository folder (for example `D:\projects\harness-launcher`), copy the following and fill in the task:

```text
You are maintaining Harness Launcher. First read MAINTENANCE.en.md,
CONTRIBUTING.md, README.en.md and docs/patch-recording.en.md, then examine
git status and the relevant modules and tests.
Task: <the feature or bug to address, including the expected behavior>.

Work in this repository. Use Python 3.12+; use the Python bundled with
Harness if the system python is unsuitable. No pip/npm install or frontend
build is needed. Preserve existing user changes; do not reset the worktree.
Without specific authorization for this task, do not operate on the real
Harness installation, user profile, plugin installer or recovery data.
Use temporary directories and synthetic fixtures for tests.

Keep recording mandatory, normal steps automatic and intervention limited
to exceptions. Preflight the whole file combination, verify the native
baseline and plugin/dependency identities, then use one transaction with
failure and interruption recovery. Do not remove validation, invent a
pre-install baseline, ignore conflicts or apply old snapshots to new versions.
Runtime code reads only the current format; isolate old conversion in
migrate.py instead of adding runtime compatibility branches. Retaining
versions is not a one-click downgrade, and config restore must check locks.

For UI changes, preserve behavior, element ids, strict CSP and plain-text
rendering of untrusted data; check wide and narrow layouts. Update both
language editions and CHANGELOG, and use only synthetic screenshots.
Run relevant tests, full regression when appropriate, node --check ui/app.js
and git diff --check. Report the changes, validation and actual limitations.
Follow the current user's instructions for commits, pushes and merges.
Never commit runtime data, credentials, tokens or private screenshots.
```

Use current code and local evidence rather than relying on a prior chat's status. Before recovery, identify whether the issue concerns source code, configuration, plugin files or the complete data set, then select the appropriate procedure.
