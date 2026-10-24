# Changelog

## Unreleased

### Update lifecycle protection

- Cover official updates from native and plugin configurations, interrupted updates, and plugin/dependency replacements without a version bump. Add a dedicated update guide inside Settings; the launcher does not intercept Harness's updater or its automatic restart.
- Check all archive locations against the transaction cursor before any rollback writes, including ASAR-only updates and archive-move crash recovery. Preserve journals on unknown host changes.
- Recheck selected packages, dependencies, configuration and host immediately before process creation. Report `plugin_identity_changed` with recovery steps for changes outside an installation recording.
- Pin host archives and other installed plugin/dependency contents before recording an upgrade. Pause on mixed updates and retain recovery evidence; advertise `recording-update-guards` while the session exists so older clients cannot silently bypass these rules.
- Allow native operation after an inactive purge package update if managed native files are unchanged. Enabling the adapter still checks its complete version and snapshot. Automatic cross-Harness migration and purge upgrade adaptation remain unsupported.

## 0.3.2 — 2026-10-24

### Plugin-switch verification performance

- Fix a rapid-write timestamp collision exposed by Windows CI: include per-file USN and fresh journal identity in content-cache keys, and establish a CLOSE barrier before caching a new hash. Reject legacy file/tree formats 1 and 2. Add deterministic same-timestamp/lingering-handle, journal-reset and unavailable-barrier regressions.

- Check recorded executable/archive identities before backups and large directory scans. Report `host_identity_changed` for possible updates or manual replacements; identical-content replacement and legitimate recorded generic ASAR patches remain accepted by this early filter. Refuse cross-version transaction rollback and retain recovery artifacts. Automatic patch migration across Harness releases is not implemented.
- Add disposable directory checkpoints guarded by continuous unprivileged NTFS USN records and stable root identities. Reuse an unchanged verified tree across purge directory moves; journal gaps, unsupported records and internal edits fall back to complete verification.
- Establish checkpoints with exclusive data reads and explicit per-entry USN CLOSE barriers, including protection against attribute-only handles retaining a coalesced change cycle. Reject experimental tree-cache format 1; retain immutable baselines, delta objects and transaction rollback separately.
- Finish operation bookkeeping and release the mutation lock before sending HTTP responses. Slow or disconnected clients no longer occupy the request lock after a completed mutation; bookkeeping exceptions also release it. Add deterministic ordering and concurrency regressions.
- Reuse bounded path/child-index projections and fingerprints only after fresh directory checkpoint validation. Keep virtual changes isolated, and derive the committed inventory from the independently verified output snapshot rather than adopting a later metadata scan.
- Final full Windows CI: Python 3.12 and 3.13 each passed 331 Python tests without skips and 32 JavaScript tests. Local targeted regression passed 51 tests. Eight production launches after restarting the launcher showed the full interface by 15.115–35.318 seconds; menus responded in subsequent checks. These observation bounds are reported against the user-approved approximate local target, not a strict 30-second guarantee; the slowest observation spans a wider sampling interval. Prior strict-limit failures remain in the published data.
- Avoid traversing inactive adapter trees when ordinary plugins change and the built-in adapter remains native. Validate immutable adapter metadata and active native files, and verify the complete parked tree again before enabling the adapter.
- Reuse a sealed native baseline only after freshly computed active contents, host and archive identities match. Avoid redundant ASAR member parsing and unused baseline-copy reads; all blobs actually needed for replay or rollback are still verified.
- Add configuration-only transactions for recorded plugins with empty file deltas; validate the current sealed contents before changing only the bundle list.
- Hash independent files with up to 32 workers. On local NTFS, reuse verified contents only when volume/file identity, creation time, write time, ChangeTime, size, current journal identity and per-file USN match. Unsupported metadata falls back to full reads; corrupt optional caches are discarded. Commit checks and serialized writes/rollback remain in place.
- Keep one cached version per file identity and cap the optional cache at 100,000 entries. Repeated edits discard obsolete fingerprints without deleting application files or recovery objects. Directory-tree fingerprints are reused only within unchanged planning overlays.
- Require data-read access, not attribute-only access, to exclude writers during NTFS cache validation; reject legacy cache formats 1 and 2 and rebuild format 3. Verify existing-writer fallback and same-size/restored-mtime edits in isolated tests.
- Reindex only moved subtrees in the planning overlay, preserving unrelated directory indexes and empty directories.
- Publish 31 isolated-data feature screenshots, embedded Markdown, architecture and verification diagrams, host-update guidance and measured startup data; see `docs/validation-0.3.2.md`. Actual private Harness conversation screenshots are excluded.
- Count every file in the current verification scope, show separate labels for planning and commit checks, and report move validation separately. A completed live-file count no longer masks a long unreported parked-tree scan.
- Added concurrent-read, inactive-tree isolation, native-switch rollback and corruption regressions. See `docs/switch-performance.md` for measurements and limits.

## 0.3.1

### Launch progress and unchanged-combination performance

- Renamed Apply to `仅应用，不启动` (Apply without launching), clarified that switches edit a draft, and made it explicit that Launch applies changes automatically.
- Added a blue-purple progress bar with stage labels, actual file/change counts and elapsed time. Progress remains readable independently of the manager lock, adapts to narrow windows and respects reduced motion. Launch/apply requests no longer time out merely because five minutes have elapsed; writes are never automatically retried.
- Launching a sealed, unchanged combination now freshly verifies active file contents, installed dependencies and host identity without rebuilding the native baseline, undoing/replaying layers or creating another configuration backup. Switching combinations retains the full transaction and rollback validation. Same-size, same-timestamp content edits still block launch.
- Added regression tests for the unchanged path, active-file and package drift, dormant adapter handling, failed process creation, concurrent control changes, progress isolation and long-running UI operations. See `docs/launch-progress.md` and `docs/launch-progress.en.md` for screenshots, measurements and their limits.

### 0.3.0 regression fixes

- Preserve draft selections when saved schemes change or lose plugins, refresh full state on reconnect or return to the page, and reject malformed backup/patch responses without replacing the last valid UI state. Scheme checks accept display-order differences while retaining version locks.
- Reject invalid mutation routes and non-boolean action flags, skip malformed backup records, normalize configured paths, and fail before creating runtime data on unsupported Python versions. Incomplete Windows process enumeration no longer means Harness is stopped.
- Reject Windows reserved names and filename aliases, scope paths reached through linked ancestors, and dependency modules that would shadow the package captured in version records. Snapshot and operation metadata receive stricter validation.
- Treat committed switches as successful when journal cleanup fails; retain the commit marker for later cleanup and preserve the receipt required to undo a failed launch. Migration verification and recovery retain content and transaction guarantees.
- Added regression coverage for these boundaries. Local validation on 2026-10-24 covered 250 Python tests (248 passed, two skipped for symbolic-link privileges) and 30 JavaScript tests; see `docs/validation-0.3.0.md` for the run scope and limits.

### 0.3.0 architecture and data consolidation

- Runtime settings, registry, adapter state, switch journals and configuration backups now accept format 3 only. Immutable content objects retain their independent contracts. Unsupported required capabilities stop loading; no legacy runtime readers or loader monkey-patching remain.
- Replaced the old importer with offline v2-to-v3 staging, complete content and combination verification, guarded directory cutover and a persistent-data checksum manifest for the archived old store. `--recover` handles an interrupted directory cutover. The runtime never reads archived stores; the migrator rejects unfinished sessions, transactions and earlier imports.
- Added validated file identities and snapshot metadata, a recording state machine with a single `runState`, and typed transaction command decoding. Invalid state combinations fail before persistence or execution. A failed scan or validation after first-run exit now retries without launching Harness again.
- Separated content storage, immutable installation paths, adapter ownership rules, settings validation and application errors from orchestration. Version storage receives explicit paths and storage/archive dependencies. Storage verifies existing content before reuse and provides capacity statistics.
- Split browser transport, immutable reducer state, asynchronous coordination and DOM rendering. Added a bounded operation status endpoint independent of the long-running application lock, and a consistent error contract with codes, operation ids and recovery advice.
- Preserve the final verified snapshot when only timestamps change during package capture. The final metadata inventory keeps trustworthy content evidence, preventing a later identical configuration rewrite from being misclassified as an unknown edit.
- Added focused model, malformed-operation, persistence, retry and asynchronous-state regression coverage. See `docs/architecture.md` for boundaries, extension rules and remaining limits, and `docs/validation-0.3.0.md` for the completed local validation.

### Earlier 0.2.x development

- Fixed false unrecorded-change errors for `root_yaml` / `cordis.yml` after timestamp-only rewrites. Changed metadata is checked against trusted recorded content before blocking a switch; actual edits, additions/deletions and missing evidence remain protected. Added regressions for fresh recordings, sealed combinations, native/plugin round trips and real configuration changes.
- Verify trusted contents before accepting a new installation baseline or planning a combination, including edits that preserve both file size and timestamp. Reuse the existing snapshot/preflight reads rather than adopting unknown changes as the first native baseline.
- Fixed restored-file preflight comparing a snapshot's original timestamp against its blob source. New adapter captures and directory-move recovery verify tree content hashes; existing records remain readable. Package snapshots retain nested asset directories named `node_modules`, and optional peers no longer hide missing required dependencies.
- Fixed stale UI reads overwriting a completed action, catalog pagination mixing unsubmitted search terms and retries dropping previous pages. Invalid catalog responses now use the existing error/cache handling, malformed items are skipped, empty patch paths cannot be installed and decoded GitHub manifests respect the size limit.
- Fixed migration previews/failures leaving staged loaders attached, unrelated profile backups forcing repeated migration, and invalid import sources creating directories. Backup lists count matching entries, failed scheme validation leaves settings unchanged, Unicode tokens return authentication errors, and failed window startup releases service resources. Windows bootstrap now detects installed Harness runtimes before falling back to PATH and rejects Python versions below 3.12.
- Added a data-directory OS lock across the service lifetime so simultaneous launches cannot create multiple writers when the default port is occupied. The lock releases on exit while its file remains. Configuration restoration now rejects duplicated official bundles before writing.
- Fixed Windows occasionally resetting rejected POST connections before clients receive the error. Bounded request bodies are consumed before authentication responses and before acquiring the mutation lock, with a total read deadline so slow clients cannot block all changes.
- Fixed menus becoming unusable after browser suspension or computer sleep: removed heartbeat-based service expiry, added an authenticated explicit exit that refuses active work, and kept shutdown serialized with mutations. Added single-flight, bounded status polling with focus recovery, persistent disconnected button guards, separate data/authentication errors and clear reopen guidance; reconnection preserves draft choices without retrying mutations. Added Python lifecycle and dependency-free JavaScript regression tests to CI.
- Added bilingual maintenance and AI handoff guides covering source/runtime layout, startup and tests, matched offline backups, restoration and interrupted-operation recovery.
- Moved the 0.2.1 default store to `%USERPROFILE%\.harness-launcher` to avoid per-file Windows Store AppData overlays. The offline migrator copies the old logical store to one physical root, retains its source, resumes interrupted atomic copies and refuses linked or conflicting destinations. Multiple migration preflights share content reads; commit still independently verifies live inputs.
- Unified the launcher at 0.2.0 with current-only runtime indexes, content-addressed objects, adapter records and transaction journals. Removed old per-layer transaction/recovery, UUID readers, interrupted-session adoption and manual finish/accept/launch endpoints. Added a standalone checkpointed `migrate.py` to convert existing metadata once, pin schemes/backups to installed versions, retain current artifacts and correct adapter status only after verifying disk contents.
- Versioned the built-in adapter's installed code as well as generic plugins; host-provided peer SDKs are bound to the retained official ASAR. Portable scheme imports now pin local installed versions.

- Added immutable installed-package/dependency revisions, source and lock metadata, parent revision history and content-addressed combination/native-base records. Retain installed current artifacts during explicit one-time conversion, with originals in a pre-migration archive.
- Preflight the whole combination in a read-only overlay, including generic undo, built-in moves and target replay, before touching live files. Share hashes across planning branches, independently recheck disk and package inputs, and track shared supporting directories so independent plugins can toggle separately and return to the same native base.
- Use one immutable transaction plan and a small progress/commit journal across generic, built-in and configuration operations. Verify complete output and recover failures, write/cursor crash gaps and interruptions during rollback. New saved schemes and configuration backups pin versions; portable imports retain the existing name-only format.

- Made installation/upgrade recording mandatory and automatic: back up, install and open Harness, observe its first run and tray exit, wait for stable files, then compare, verify and save without routine prompts. A background supervisor continues after the browser closes. Interrupted sessions and validation failures preserve the baseline and require intervention; changed installations cannot discard records. Unrecorded existing plugins now block all file-combination switches.
- Automatic saving rechecks the full managed scope and plugin/application versions, detects new writes during verification, and verifies both original and patched blobs. Replay verifies both directions before mutating the first file. Upgrade history, conflict refusal and rollback remain enforced.
- Fixed launch failing to find the preserved official archive when a built-in patch record was unavailable but `official.asar` was already stashed. Discovery, first verification and switching now use the same archive locations; integrity and version checks remain enforced.
- Fixed the narrow-window launch bar clipping the launch button in native mode or with long scheme names. The bar now separates the heading from the controls, stacks buttons in smaller windows, and reserves matching space below the page content. Truncated headings and status messages expose their full text on hover.
- Reworked the UI layout: two-column workspace with a sticky launch panel (collapses to a bottom bar on narrow windows), grouped sidebar, consistent SVG icons, readable contrast, and formatted HTML/CSS sources.
- Split "安装与自动记录" into 「手动添加」 (an install flow with explicit npm / local / Git / self-install sources that always records) and 「补丁记录」 (a ledger of recorded, built-in and unrecorded plugins with upgrade entry points).
- Fixed launch being unavailable for custom install folders: the executable is now also detected from the installer's uninstall registry entry (read-only). A missing executable is reported on the main screen with a one-click fix, and switching, restoring and recording now refuse to run against a missing install.
- Scheme import now previews every scheme as new / overwrite / skipped. Schemes that reference plugins not installed on this machine are skipped and the missing plugins are listed, instead of rejecting the whole file. The import button is now keyboard accessible, and the file format is documented in the README.
- Toast messages now appear above open dialogs instead of behind the modal backdrop.
- Added a leaderboard view to plugin discovery: top 50 by npm 7-day downloads, and a "rising" board for plugins whose 30-day downloads mostly happened this week. Both share one cached scan of the first 1000 npm search results (4 requests, 1-hour cache) and always use npm's official counts. A GitHub star board was evaluated and left out because topic-tagged stars are dominated by non-plugin repositories and self-declared repository links.
- Fixed a race where closing and immediately reopening the discovery or detail dialog discarded the new request and left the list empty.
- Rewrote the documentation in Simplified Chinese and English: `README.md` / `README.en.md` with a screenshot for every feature, a restructured technical guide (`docs/patch-recording.md` / `docs/patch-recording.en.md`), and bilingual `CONTRIBUTING.md` and `SECURITY.md`. Screenshots in `docs/images/` use synthetic data only.
- Plugins whose version cannot be read now show “版本未知” instead of “v未知”.

## 0.1.0 — 2026-10-20

- Initial public developer preview for Windows DeepSeek Harness desktop installations.
- Added official starter bundles, community npm search and GitHub topic discovery, package details, registry/mirror choice, pagination, caching and partial-failure messages.
- Kept manual package/local/repository installation sources and added a reviewed handoff from discovery to recording.
- Added launch combinations, configuration backups, guarded dsh-purge patch switching and general installation/upgrade file recording with conflict/crash recovery checks.
- Added first-run path configuration, MIT licensing, contributing/security documentation and Windows Python 3.12/3.13 CI.

Known limits: no universal plugin compatibility or whole-system restoration guarantee; macOS/Linux patch management is not validated; discovery depends on upstream metadata and rate limits. See README and SECURITY.
