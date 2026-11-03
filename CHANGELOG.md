# Changelog

## 0.4.3 — verified plugin maintenance and official update recovery

- Clarify maintenance actions: review with “卸载插件…” or “清理缓存…”, then explicitly confirm the named operation. Cancelling leaves the installation unchanged. File counts now distinguish a plugin's extra changes from the package itself.
- Invalidate cached update actions after installation or revalidation, and direct suspended plugins to current-version revalidation without incorrectly blocking other verified plugins.
- Keep the same active plugin set during switch planning and commit, so an installed but suspended plugin cannot falsely block an unrelated plugin's enable/disable operation. Suspended plugins remain unavailable for selection.
- Isolate non-target plugin build scripts during install, update and uninstall through the official pnpm configuration interface. Restore their original policy afterward and keep target-specific authorization.
- Limit ordinary-plugin uninstall previews to their own records instead of displaying another archive adapter's files.
- Enumerate content objects once per cache review and avoid repeated disk probes for hashes absent from the cache. Recheck references at execution and retain objects created during the scan.
- Add clearer recovery progress, a scenario-based bilingual README, current maintenance screenshots, and a compact maintenance flow diagram.

- Record ordinary managed-file changes alongside archive adapters, including profile YAML injection. Use the current installation checkpoint for native bytes so old plugin backups cannot replace the current official CLI; preserve conflicting later user edits.
- Recognize pnpm's `github:owner/repo#commit` source notation during same-version revalidation and exact-commit build approval, without following a branch or changing the pinned source.
- Resolve switch and rollback blobs through their owning shared stores after an official-host generation change. Record approved runtime directory links as leaves, validate their targets before replay, and never follow them while backing up or restoring.
- Share the discovery metadata cache with maintenance execution, but re-fetch the exact selected target before installation. Report connection failures as connection failures instead of falsely claiming the target version changed.
- Retry public metadata through Windows certificate trust when Python HTTPS fails; keep certificate validation, origin restrictions, response limits and redirect refusal.
- Verify package files, recovery blobs and maintenance checkpoints with at most eight workers and actual per-phase counts. Reuse verified existing content without rewriting temporary copies or repeating fsync; retain independent reads and corruption/conflict rejection.
- Serialize identical-content publication and briefly retry atomic replacement when Windows readers hold a record open. Permanent access denial remains an error and preserves the original. After restart, a saved installation snapshot is no longer displayed as a zero-file backup.
- Detach a checkpointed external package link before an explicit switch to a downloaded source. Windows pnpm must not replace or clean through the old junction. Preserve its source tree and restore the link on rollback; directory-to-link recovery can resume without following the restored link.
- Disclose and pass exact-version or exact-commit lifecycle-script approval through the official installer. Do not approve every dependency or change global policy; preserve profile policy in maintenance checkpoints and identify build-policy failures clearly.
- Reuse one validated checkpoint path resolver within recovery, while retaining per-file path and content checks. Avoid rebuilding the entire installation context for every checkpoint entry.
- Serialize NTFS verification by physical file identity so parallel aliases of pnpm hardlinks cannot invalidate each other's close barriers. Independent files remain parallel; content and change-log checks are retained.
- Show live phase progress and measured checkpoint counts in the maintenance dialog, including an unfinished first-run state and recovery state after failures. Reuse the independent operation endpoint.
- Replace the exact `dshTarget` upgrade dead end with checkpointed installation and fresh validation against the current host. Keep shared-dependency, host identity, reversible-patch and rollback checks. Offer same-version revalidation for suspended npm and fixed-commit plugins without replaying old host patches or changing pinned branches.
- Enable installation sources only after read-only, package-specific checks. Pin npm versions and Git commits, check local build entries again before installation, and enforce short-lived source-bound authorization on the HTTP endpoint. Keep manual recording as an explicit advanced workflow. Reuse metadata without an extra package download; report unavailable sources and allow retry without freezing the launcher.
- Recognize browser peers registered by the official frontend's static module table when recording web plugins. Pin the frontend entry, manifest, HTML index, ASAR and EXE hashes; do not exempt unknown dependencies, Node-only dependencies or altered files. This fixes dsh-pet's false missing-UI-peer error during recovery recording.
- Keep read-only recording status available while the file worker holds the mutation lock, preventing progress polling from timing out during a long version capture.

- Reuse one authenticated official-package inventory across recovery and host adoption; reuse the updater's download, cache by verified content, refresh Authenticode after 24 hours, and extract only missing files when the inventory is already known.
- Validate regenerated Python caches against verified official source in an isolated, verified interpreter; never execute candidate code or exempt source-less bytecode. Reuse the content-bound result and complete NTFS tree checkpoints to avoid repeated scans.
- Add a reviewed official-installer workflow with checkpoints, native restoration, a persistent journal, post-install verification and resumable activation. Never automatically rerun an installer after a lost response.
- Detect external official updates on explicit launch. Build an independent generation and atomically publish its pointer. Keep old generations, parked purge patches and all recovery objects; share immutable blobs instead of copying entire stores.
- Apply the same authenticated host adoption before starting a new plugin installation. Show blocked-install reasons and live verification progress in the form, and hide another plugin's stale completion banner.
- Rebase only recorded zero-file-delta plugins with an explicit supported compatibility range and unchanged package/dependency content. Suspend purge and unproven plugins with reasons; retain user data and unknown edits.
- Validate with synthetic host upgrades, cache invalidation, installer failure and interrupted activation. A real upgrade to the next official release remains untested; do not interpret simulation as release qualification.


- Detect stale host baselines in the home screen before launch. Do not label an old native snapshot as verified for an updated host.
- Add an explicit signed-installer preview and independent native-launch baseline. Compare official desktop files, CLI and runtime anchors; preserve old native user-side files and all patch/rollback records. Launch only the already-native manifest, without replaying old layers.
- Pin approval to host bytes and control files, recheck before acceptance and each launch, and retain the content-addressed approval during cache collection. The manual native-only fallback still blocks old plugin replay; automatic generation adoption separately qualifies zero-file-delta plugins.

- Add inexpensive mandatory-file checks before launching or recording installations and a Settings repair entry independent of plugin host-version guards. Keep verified apply-only restoration available.
- Verify an official installer under a write-excluding lease with SHA-512 and Windows Authenticode, enforce the DeepSeek publisher, and match installed executable / app.asar hashes against the extracted package.
- Preview missing and conflicting files; add only missing files with no-overwrite publication and SHA-256 verification. Preserve all existing files, profiles, plugin records and rollback materials. Interrupted repairs can be checked again and resumed.
- Keep package extraction and complete scans off the daily launch path. Windows x64 repair requires installed 7-Zip; it does not intercept the official updater's own restart.

- Add explicit, source-bound update checks with a five-minute cache and exact npm version / Git commit selection. Preserve private, local and pinned sources unless a reviewed source change is available.
- Add durable maintenance checkpoints, ordinary-plugin and purge upgrade workflows, first-run validation, uninstall previews and conflict-aware rollback. Validate dormant package and patch contents before removal; retain external source directories and stop on shared-dependency drift.
- Collect only unreachable content objects older than one hour after a fresh reference walk. Saved combinations, histories, configuration backups and recovery points remain roots; no global package cache or user data cleanup.
- Keep maintenance off the daily launch path. Preserve host-update guards, independent switch verification, NTFS checkpoints and rollback.
- Ship a Windows x64 portable ZIP with a small GUI launcher, pinned and verified CPython runtime, Chinese quick-start instructions and SHA256SUMS. Official Harness and third-party plugins are not bundled.
- Publish updated screenshots, maintenance guidance, architecture and verification diagrams. Validation and local startup observation bounds are documented in `docs/validation-0.4.3.md`.

- Simplify documentation to the current release and remove obsolete data conversion tools. Keep strict schema validation, backups and transaction rollback.
