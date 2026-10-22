# Changelog

## Unreleased

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
