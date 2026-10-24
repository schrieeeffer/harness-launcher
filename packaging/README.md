# Windows portable build

Run `python packaging/build_portable.py` on Windows with Python 3.12+ and the .NET Framework C# compiler installed. The build uses a public-source allowlist, compiles `Launcher.cs`, checks runtime imports and writes a versioned ZIP plus `SHA256SUMS.txt` under `dist/`.

CPython 3.13.17 comes from [python.org](https://www.python.org/downloads/release/python-31317/). Its embedded x64 archive must match the pinned official SHA-256 before extraction or execution. The Python license is retained in the runtime directory. `BUILD.json` records the source commit and packaged file hashes. Build from a clean, committed checkout for release provenance.

The GUI wrapper starts bundled `pythonw.exe` without a console. It does not contain DeepSeek Harness, plugins, configuration, credentials or conversations. The release targets Windows 10/11 x64 and is currently unsigned. Whole-archive extraction is required; the executable alone is not a single-file distribution.

Publishing is separate from building. Review the ZIP allowlist, run the portable application, verify the matching GitHub commit and checks, then attach the ZIP and checksum to the release. Never include `diagnostics/`, user profile data or test fixtures in the portable archive.
