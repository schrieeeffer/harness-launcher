# Contributing

Use Python 3.12+ and Node.js 22+ (JavaScript syntax check only). There are no pip or npm dependencies.

Run `python -m unittest discover -v` and `node --check ui/app.js` before submitting changes. Tests use temporary fixtures and mocked public APIs; they must never install a real plugin or change a real Harness profile.

Keep discovery metadata separate from installation. Treat package names, descriptions, links and repository files as untrusted data; render text with textContent. Only fixed public registry/GitHub endpoints may be fetched by the catalog. Retain explicit review before installation and attribution of file changes.

Do not add credentials, local profiles, snapshots, logs or screenshots containing personal data. Use a synthetic profile for UI screenshots. Document supported application versions and platforms; do not describe fixture tests as full platform certification.

For bug reports, include launcher/Harness versions, steps, expected and actual behavior, and a redacted error. Do not attach your full `.dsh` directory or access URL tokens.
