# 0.3.1 launch progress and performance verification

[简体中文](launch-progress.md) · [Full feature tour](feature-tour.en.md) · [User guide](../README.en.md)

## Switches and Apply without launching

Switches edit a pending combination so that several plugins can be selected together. **Launch Harness** applies changes automatically and starts Harness. **Apply without launching** (仅应用，不启动) synchronizes configuration and patches in advance while keeping Harness closed; an empty selection displays **Restore without launching** (仅还原，不启动). Saving a scheme only saves its name and selection.

## Visible progress

The blue-purple bar uses small light points and a white circular marker. The backend reports five stages: prerequisites, packages/dependencies, file verification, patch synchronization and sending the launch request. Counted work displays processed files or changes alongside elapsed time. Work without a known total uses a waiting animation. The bar does not invent a percentage or remaining time from elapsed seconds. An unchanged combination can skip synchronization.

These screenshots use isolated synthetic data. Progress explicitly labeled 示例 (example) is controlled by the demo service to inspect a long-running layout. The production UI renders and polls it, but **the screenshots do not measure real plugin startup speed**. Paths, counts and selections are illustrative.

![Launch progress in the desktop layout](images/launch-progress-desktop.png)

At 800 pixels, progress and actions move to the bottom:

![Launch progress in the medium-width layout](images/launch-progress-medium.png)

At 390 pixels, buttons stack. After 30 seconds, a hint explains that the backend is still processing files:

![Launch progress in the compact layout](images/launch-progress-compact.png)

## Cause and changes

The investigation found that even an unchanged combination repeated the entire switch: rebuilding the native baseline, undoing and replaying patches, and checking dormant adapter trees and recovery copies. Numerous unpacked files amplified the reads and hashing before process launch. The reported wait was in launcher verification and cannot be attributed directly to the plugin's own initialization.

With a valid saved combination and unchanged selection/configuration, 0.3.1 freshly verifies active files, installed packages/dependencies and host identity, then launches without redundant replay or configuration backup. Changed combinations, restoration, installation and recording keep the full validation path. Edits preserving both size and timestamps remain detectable. Dormant adapter files still undergo full checks before a later switch uses them.

Launch/apply requests no longer abort solely because five minutes elapsed. Status polling remains independent and mutations are never automatically retried. Sending a process launch request does not establish that the Harness window is ready.

## Measurements and limits

The real backend recorded **798,031 ms (about 13 minutes 18 seconds)** for the old launch operation on 2026-10-24, ending with a successful launch request. The active managed scope contained about **1,509 files and 376 MB**. This measures launcher work, not plugin-only initialization or full window readiness.

A single profiled run of the new read-only unchanged-combination verification on the same installation passed in **13.277 seconds**, without launching Harness again. These measurements cover different work and did not control cache conditions. They are not a full-startup benchmark or a guaranteed speedup ratio. First-time sealing, changed combinations and large plugins may still take time; the interface now exposes the actual stage.

See the [0.3.1 validation record](validation-0.3.1.md) for automated checks, browser verification and the local restart.
