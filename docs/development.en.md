# Development and maintenance

[User guide](../README.en.md) · [Architecture](architecture.md) · [Plugin maintenance](plugin-maintenance.md) · [中文](development.md)

## Repository layout

```text
harness_launcher/        Python backend package
  __main__.py           python -m harness_launcher entry point
  app/                  HTTP, launch coordination, operations and switch transactions
  domain/               Models, state transitions and error types
  plugins/              Discovery, sources, recording, updates and uninstall
  host/                 Official application, runtime repair and special adapters
  storage/              Content objects, settings, versions, checkpoints and garbage collection
  verification/         File views, verification caches and NTFS checkpoints
ui/                     Browser assets; no frontend build step
tests/                  Python regression tests and shared fixtures
  ui/                   Node.js frontend tests
scripts/                Windows source-launch helpers
packaging/              Portable builder and Windows EXE wrapper
docs/                   User, design, development and validation documentation
  images/               Screenshots and architecture diagrams
.github/workflows/      Windows CI
launch.vbs              Double-click entry point for source runs
```

Keep runtime modules in the relevant Python subpackage and tests in `tests/`. The root contains project entry documents. Local diagnostics belong in ignored `diagnostics/`; generated builds belong in ignored `dist/`.

## Finding the right code

Python paths below are relative to `harness_launcher/`.

| Change | Start here | Related tests |
| --- | --- | --- |
| UI and API | Root `ui/`, `app/launcher.py` | `tests/ui/test_ui.cjs`, `tests/test_http_delivery.py` |
| Launch and plugin selection | `app/launcher.py`, `app/switch_plan.py`, `app/operations.py` | `tests/test_launcher.py`, `tests/test_launch_progress.py`, `tests/test_version_plan.py` |
| Discovery and installation | `plugins/catalog.py`, `plugins/install_sources.py`, `plugins/installer_policy.py` | `tests/test_catalog.py`, `tests/test_install_sources.py`, `tests/test_installer_policy.py` |
| Installation recording | `plugins/plugin_tracking.py`, `domain/recording_state.py` | `tests/test_plugin_tracking.py`, `tests/test_official_plugins.py` |
| Updates, uninstall and recovery | `plugins/maintenance.py`, `plugins/update_check.py`, `storage/maintenance_files.py` | `tests/test_maintenance.py` |
| Official updates and runtime repair | `host/official_update.py`, `host/official_package.py`, `host/official_host.py`, `host/runtime_repair.py` | `tests/test_official_update.py`, `tests/test_official_host.py`, `tests/test_runtime_repair.py` |
| Special plugin rules | `host/adapters.py`, `host/patch_modes.py` | `tests/test_patch_modes.py`, `tests/test_host_updates.py` |
| Verification and performance | `verification/`, `host/runtime_integrity.py` | `tests/test_verification_cache.py`, `tests/test_runtime_checkpoints.py`, `tests/test_switch_performance.py` |
| Storage and settings | `storage/` | `tests/test_storage.py`, `tests/test_settings_store.py` |
| Windows entry points and packaging | Root `scripts/` and `packaging/` | `tests/test_entrypoints.py`, `tests/test_launcher.py` |

Use qualified imports such as `from harness_launcher.storage.content_store import ContentStore`. Keep package initializers lightweight. Domain models and basic storage do not depend on HTTP or plugin orchestration; `tests/test_architecture.py` enforces those boundaries. See [module boundaries](architecture.md#boundaries--模块边界) for the detailed relationships.

General plugin workflows live in `plugins/`. Rules specific to a plugin that changes host files live in `host/adapters.py` and participate in the shared switch transaction. New adapters need ownership and rollback tests.

## Running and testing

Use Python 3.12+ and Node.js 22. The backend uses the standard library; there is no pip or npm installation step. Run these commands from the repository root:

```powershell
python -m harness_launcher
python -m unittest discover -s tests -t . -v
node --test tests/ui/test_ui.cjs

# Focused checks
python -m unittest tests.test_storage tests.test_version_plan -v
node --test --test-name-pattern="uninstall" tests/ui/test_ui.cjs
```

Windows source users can also double-click `launch.vbs`. Python tests share fixtures through explicit imports such as `tests.test_launcher` and create temporary installations and profiles. `--no-open --data <directory>` is useful for inspecting a separate launcher service, but only isolates launcher data; manual plugin tests also need a separate Harness executable and `.dsh` directory in settings.

## Data and recovery

Runtime control data accepts format 3 only. Immutable objects keep independent contracts. Preserve `requiredFeatures` validation and reject unsupported formats without rewriting original data.

Before offline backup or restoration, exit both Harness and the launcher. Preserve a matched set of the launcher store, Harness installation and `.dsh` data at their original paths. Configuration backups are not full plugin-code backups. Retain pending transaction journals and maintenance checkpoints; refuse unknown user changes or host identity changes during replay.

## Building and releasing

```powershell
# Separate test artifacts; reuse the verified CPython download cache
python packaging/build_portable.py --output-dir dist/layout-smoke

# Release build, from a clean committed checkout
python packaging/build_portable.py
```

Add new Python modules to `PACKAGE_MODULES` in `packaging/build_portable.py`. Missing or unlisted modules fail the build. Source and portable launches share `python -m harness_launcher`; browser assets live under `ui/` at the application root.

Use isolated installations for failure, update, uninstall and rollback tests. Windows CI covers Python 3.12 and 3.13. See [release validation](startup-validation.md).

Audit the allowlist and hashes, test the portable EXE and verify the source commit and CI before publication. Exclude private settings, credentials, conversations and diagnostics. Building does not publish a release. See [packaging](../packaging/README.md).
