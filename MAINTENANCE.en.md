# Current maintenance guide

See [architecture](docs/architecture.md), [plugin maintenance](docs/plugin-maintenance.md) and the [detailed Chinese maintenance guide](MAINTENANCE.md).

Runtime control data accepts format 3 only. Immutable objects keep independent contracts. Preserve `requiredFeatures` validation and reject unsupported formats without rewriting original data.

Before offline backup or restoration, exit both Harness and the launcher. Preserve a matched set of the launcher store, Harness installation and `.dsh` data at their original paths. Configuration backups are not full plugin-code backups. Retain pending transaction journals and maintenance checkpoints; refuse unknown user changes or host identity changes during replay.

Run `python -m unittest discover -v` and `node --test test_ui.cjs`. Use isolated installations for failure, update, uninstall and rollback tests. Windows CI covers Python 3.12 and 3.13. See [release validation](docs/validation-0.4.0.md).

Build with `python packaging/build_portable.py`, audit the allowlist and hashes, test the portable EXE and verify the source commit and CI before publication. Exclude private settings, credentials, conversations and diagnostics. See [packaging](packaging/README.md).
