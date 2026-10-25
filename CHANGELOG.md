# Changelog

## 0.4.1 — verified official runtime recovery

- Add inexpensive mandatory-file checks before launching or recording installations and a Settings repair entry independent of plugin host-version guards. Keep verified apply-only restoration available.
- Verify a private official installer copy with SHA-512 and Windows Authenticode, enforce the DeepSeek publisher, and match installed executable / app.asar hashes against the extracted package.
- Preview missing and conflicting files; add only missing files with no-overwrite publication and SHA-256 verification. Preserve all existing files, profiles, plugin records and rollback materials. Interrupted repairs can be checked again and resumed.
- Keep package extraction and complete scans off the daily launch path. Windows x64 repair requires installed 7-Zip; it does not intercept the official updater's own restart.

- Add explicit, source-bound update checks with a five-minute cache and exact npm version / Git commit selection. Preserve private, local and pinned sources unless a reviewed source change is available.
- Add durable maintenance checkpoints, ordinary-plugin and purge upgrade workflows, first-run validation, uninstall previews and conflict-aware rollback. Validate dormant package and patch contents before removal; retain external source directories and stop on shared-dependency drift.
- Collect only unreachable content objects older than one hour after a fresh reference walk. Saved combinations, histories, configuration backups and recovery points remain roots; no global package cache or user data cleanup.
- Keep maintenance off the daily launch path. Preserve host-update guards, independent switch verification, NTFS checkpoints and rollback.
- Ship a Windows x64 portable ZIP with a small GUI launcher, pinned and verified CPython runtime, Chinese quick-start instructions and SHA256SUMS. Official Harness and third-party plugins are not bundled.
- Publish updated screenshots, maintenance guidance, architecture and verification diagrams. Validation and local startup observation bounds are documented in `docs/validation-0.4.0.md`.

- Simplify documentation to the current release and remove obsolete data conversion tools. Keep strict schema validation, backups and transaction rollback.
