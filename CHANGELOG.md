# Changelog

## Unreleased

- Organize the backend into the `harness_launcher` package, grouped by application, domain, plugins, host, storage and verification responsibilities. Move regression tests into `tests/` and Windows launch helpers into `scripts/`.
- Use `python -m harness_launcher` for source and portable entry points. Update CI discovery, the portable file allowlist and resource paths together; keep existing user data and recovery formats.
- Add a bilingual development guide with a directory map, common change locations and focused test commands. Allow separate portable test builds with `--output-dir` while reusing the verified runtime download.

## 0.4.4 — everyday startup and official plugin coexistence

- Keep adapter build scripts disabled during ordinary official plugin installs. Explicit adapter maintenance temporarily authorizes only its pinned source, then restores isolation.
- Preserve plugins created and managed inside Harness, including local source links. Unrecorded ordinary plugins no longer prevent launching the official current configuration.
- Allow unrelated user preferences to change while a patch is disabled. Program files and paths owned by active or requested patches retain conflict checks and rollback.
- Inspect actual loader entries with the official YAML parser; model/provider display names no longer block native mode. Reuse verified archive contents when only user preferences change.
- Restore the previous combination if build-policy preparation fails after a switch.

- Reuse NTFS journal-certified directory checkpoints for installed plugin packages and their resolved dependencies. A changed directory or incomplete journal falls back to reading its files.
- Give package directory exclusions their own checkpoint identity. Dependencies beneath `node_modules` are resolved explicitly; a partial inventory cannot satisfy a whole-tree check.
- Separate runtime and CLI verification scopes, avoiding invalidation caused by checking the same directory through two paths.
- Remove a redundant package pass from configuration-only planning. Execution still validates both combinations before writing the manifest, then checks packages and host identity before launching.
- Reuse dependency directory lookups within one capture, followed by a fresh resolution pass that detects added shadow modules and changed dependency targets.
- Decode an unchanged ASAR header once per capture. Every lookup reads the actual header and checks file contents, including unpacked dependencies.
- Keep full recovery-blob verification for installation and package restoration. Launch checks validate installed files and recovery-object presence; file switching verifies the actual forward and rollback bytes it uses.
- Update the README, screenshots, architecture and verification diagrams. Real pet 0.4.1 samples reached a usable window in 28.1–28.8 seconds on the tested machine. See [startup validation](docs/startup-validation.md).

The Windows x64 portable package includes Python and a GUI entry point. Extract the whole ZIP and open `Harness Launcher.exe`.

Startup switching used pet on the real installation. A local custom plugin was also installed through the official CLI and confirmed running after restart. The existing purge installation and records were preserved. Full uninstall restoration and the next official Harness upgrade have separate validation records and are not claimed as newly tested here.
