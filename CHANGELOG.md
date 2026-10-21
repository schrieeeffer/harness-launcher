# Changelog

## Unreleased

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
