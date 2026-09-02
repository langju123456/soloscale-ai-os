# Changelog

All notable changes will be documented here.

## [Unreleased]

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
