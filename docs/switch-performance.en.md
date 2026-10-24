# 0.3.2 startup verification and file counts

[简体中文](switch-performance.md) · [Feature screenshots](feature-tour.en.md) · [Progress screenshots](launch-progress.en.md)

The acceptance target was subsequently relaxed to approximately 30 seconds. Historical strict-limit failures remain recorded; this is not a hard upper-bound guarantee. See the [validation record](validation-0.3.2.md) for the latest regression and restart results.

## Where the files come from

The installed purge implementation extracts the complete official ASAR into resources/app before patching selected files. The launcher moves that tree between fixed active and parked locations; each toggle does not create another numbered copy.

A read-only comparison against the retained official ASAR integrity table on 2026-10-24 found 12,967 original regular files: 12,912 identical, 55 different, plus two added files and no missing files outside linked directories. This comparison alone does not attribute every difference to a particular program. The runtime directory junction is recorded, not traversed. An earlier 21,251-file traversal included its target and was not the actual regular-file verification count.

The separate local verification-cache.json is not a scan root. Removing it forces content reads next time; it does not reduce the installed application's required file count.

A sample before the final window matrix contains 14,695 entries for 14,695 distinct file identities, occupying 2,318,036 bytes. Atomic configuration replacements create new identities, so the count need not remain identical. No duplicate revisions for the same identity remained, and the 100,000-entry cap still applies. Two directory checkpoints contain 17,375 file/directory entries; their separate limits are 32 trees and 100,000 total entries. These indexes are not additional application copies.

Progress counts include metadata checks. Cache hits still count as verified files even though their contents are not read again; the count alone does not indicate a cache miss.

## Implemented changes

- Exclude unused parked adapter trees when the adapter remains inactive.
- Use manifest-only transactions for recorded plugins with empty file deltas, after verifying the current combination.
- Reuse matching sealed baseline/archive information; verify every recovery blob actually used.
- Use up to 32 read workers and reuse unchanged directory fingerprints within planning overlays.
- Reindex only the moved subtree in a planning overlay rather than rebuilding every unrelated directory index.
- On local NTFS, reuse verified hashes only after checking volume/file identity, creation time, LastWriteTime, ChangeTime and size. Data-read handles with read-only sharing exclude writers/replacement during checks. Unsupported metadata, changed files and damaged caches require full reads.
- Replace obsolete fingerprints for the same file identity and cap the optional cache at 100,000 entries. Application files and recovery objects are retained.
- Reuse verified directory checkpoints only after fresh root-identity and continuous NTFS USN checks. Whole-directory renames can reuse contents; internal edits, journal gaps or unavailable interfaces require full verification.
- Establish checkpoints with exclusive data reads and USN CLOSE barriers, including protection against attribute-only handles retaining a coalesced change cycle. Bound path/index projections to two locations and isolate planning overlays from real checkpoints.
- Build the final inventory from the independently verified output snapshot. Finish operation bookkeeping and release the request lock before network delivery, so slow clients do not block subsequent mutations.
- Reject changed host identities before backups and large tree scans, then recheck before process creation. An obsolete patch attempt does not create new backup history merely to discover a host update. See [updates and conflicts](host-updates.md).

Cache format 2 uses `GENERIC_READ`; attribute-only access does not exclude data writers. Legacy format 1 is discarded and rebuilt once. An existing writer, an unavailable handle or unsupported identity information cannot establish a cache hit. Directory checkpoints also use format 2 and reject the earlier experimental format. See the [checkpoint design](checkpoint-design.md) for the journal and fallback boundaries.

See Microsoft's [FILE_BASIC_INFO documentation](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_basic_info) for ChangeTime. This optimization detects normal filesystem changes; it is not a guarantee against manipulated system metadata or offline disk corruption.

## Acceptance status

The local target was explicitly relaxed from a strict 30-second maximum to approximately 30 seconds. Timing includes verification, combination changes and Harness loading. Initial cache construction, changed files and journal-gap recovery may require a slower full check. Backend completion is not UI readiness.

The final regression has 328 Python tests: 326 passed and two symbolic-link privilege skips; all 32 JavaScript tests passed. After a production launcher restart, four unchanged combinations and four switch directions showed the complete interface by **18.003–31.763 seconds**, with all subsequent menu interactions succeeding.

| Category | Backend stage | Complete-interface observation upper bound |
| --- | ---: | ---: |
| Native, whale, purge and both: unchanged launches | 1.011–3.018 s | 18.003–25.758 s |
| Four combination-switch directions | 2.863–10.667 s | 19.304–31.763 s |

These are single samples for each final-code scenario, not universal maximums. The previous strict-limit failure (native to purge still loading at 31.098 seconds) remains recorded. Earlier backend measurements of 189.297 seconds for a 0.3.1 whale disable and 76.580 seconds for initial purge cache construction are historical, not current routine window times.

See the [validation record](validation-0.3.2.md), [architecture and verification diagrams](architecture.md), and [31 isolated-data feature screenshots](feature-tour-0.3.2.md). Verification, conflict detection and rollback remain enabled. Automatic cross-version patch migration and recovery-history garbage collection are not implemented.
