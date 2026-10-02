# Changelog

All notable changes will be documented here.

## [0.4.2] — 2026-10-02

### Fixed

- Chinese project editing now requests a change of emphasis grounded in source
  mechanisms and validation, rather than verb substitutions.
- Unmeasured Chinese outcome claims such as improved accuracy are rejected and
  fall back to the source slot. This bounded lexical gate still requires human
  semantic review.
- Python package, desktop metadata, and build defaults agree on 0.4.2 / build 9.
- Desktop identity tests cover detached PR checkouts: an absent branch is `unknown`,
  while the full commit and clean/dirty state must still match the worktree.

### Changed

- Replaced stale sprint gates with the actual closeout scope, acceptance evidence,
  and explicit deferred work.
- Desktop instructions reflect the existing pinned full-Xcode toolchain.

## [Unreleased] — historical foundation

### Added

- Initial deterministic starter
- Task routing contracts
- State machine and event store
- Codex Execution Packet generation
- BuildLog evidence export
- GitHub workflow templates
- Strict, versioned public contracts that reject unknown fields
- Evidence-backed orchestration transitions, persisted state continuity, and mandatory execution approval receipts
- Complete planning fields in the Codex Execution Packet
- CLI coverage for structured planning-contract fields
- GitHub evidence-plane and local-to-cloud deployment guides
- Public-safe conversation distillation and multichannel content templates

### Changed

- Hardened CI with explicit permissions, concurrency, Python 3.11/3.12, and package builds
- Made the installed demo independent of the current working directory

### Fixed

- Removed the `BLOCKED → EXECUTING` approval-bypass transition
- Required blocked work to resume through triage instead of skipping planning
- Closed mutable-state and foreign-enum execution approval bypasses
- Preserved task constraints and schema version in rendered Execution Packets
- Resolved the initial Ruff quality-gate failures
