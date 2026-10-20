# Security and data handling

This launcher is a local development-preview tool. It binds to `127.0.0.1`, uses a per-process random token, checks Host and Origin, and serves local UI assets without CDN dependencies. Do not expose its port through a tunnel or public reverse proxy.

Discovery sends the entered search query to npm (or the explicitly selected npmmirror endpoint) and/or GitHub. It uses anonymous public APIs and does not upload profile files, sessions or credentials. Search results are not a security review. Installation runs third-party code through the Harness package manager only after the user starts a recording window.

Snapshots can contain private material if you place it in a tracked rules/skills/configuration file. Keep the launcher data directory and patch stashes private. File scopes exclude standard credential and session locations; they do not detect arbitrary secrets embedded in tracked files.

The dsh-purge extracted directory guard uses file metadata to detect ordinary edits. The original archive and replayed file blobs use content hashes. This is a recovery tool, not a tamper-proof system audit or a sandbox for untrusted plugins.

Report vulnerabilities through the repository's private security reporting feature if available; otherwise contact the maintainer without including exploit credentials or personal files in a public issue.
