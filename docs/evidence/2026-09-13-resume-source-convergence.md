# Resume source-convergence verification

Date: 2026-09-13
Status: local integration candidate; not pushed, merged, released, or installed

## Source identity

- Public integration base: `06d8cc8215d5a05e052561b773e0173745b8cccd`
- Integration branch: `codex/resume-intelligence-public-sync`
- Intended canonical remote: `langju123456/soloscale-ai-os`
- Old repository history, private run data, credentials, generated media, and build
  products were not imported.

## Integrated boundary

This candidate brings the current Resume Intelligence domain modules, their provider
prerequisites, tests, and macOS build-provenance support onto the sanitized public
baseline. It preserves the public strict-type fixes and the public external-component
packaging boundary.

The `/resume/intelligence` page is **preflight only**. It analyzes a job description,
retrieves authorized evidence, grades claim truth, and prepares a generation contract
without making a provider call. Private Codex/ChatGPT history and BuildLog history are
not searched unless the operator selects them for that run. The existing `/resume`
generation and export flow remains unchanged.

The candidate Resume generator is not yet wired into that export flow. Recovery from
provider schema/length failures, including the existing 600/800-character boundaries,
remains separate follow-up product work and is not claimed as complete here.

## Truth and scale controls

- Generated bullets, headline, summary, and skills pass deterministic claim checks.
- Numbers already present in an authorized source claim may survive validation; newly
  introduced numbers are rejected or deterministically repaired.
- Unsupported technology, outcome, scale, deployment, ownership, and contribution-mode
  implications remain blocked.
- Candidate profiles and optional expert-review patches share the 120-entry ceiling,
  including three-digit identities such as `PROFILE-100`.

## App provenance controls

The backend sidecar and macOS App builds now refuse dirty or uncommitted source trees.
The sidecar emits a two-field receipt containing its full 40-character Git commit and
clean-source marker. The App build accepts that sidecar
only when its receipt matches the App's exact source commit. The deterministic
validation launcher then reads the embedded receipt again and rejects missing,
abbreviated, dirty, mismatched, malformed, or symlinked provenance before launch.

## Local verification

- Focused Resume/provider/privacy/provenance tests: 175 passed.
- Complete Python suite: 655 passed; one third-party deprecation warning.
- Ruff: passed over `src`, `tests`, and `scripts`.
- Strict mypy: zero issues across 144 source files.
- Python sdist and wheel build: passed.
- Package SHA-256: wheel
  `a189587fa77ead6685824bf1508876dfe842a7c684f2ddccf939f7280bf172fe`;
  sdist `d21587eca23c07501dc8a3e07509c226196e79e38319f20e009ab41fe8911562`.
- macOS toolchain preflight: passed with full Xcode.
- Swift package compilation: passed.
- `git diff --check`: passed.

No real provider call, credential read, publication, deployment, Git push/merge, App
installation, or installed-App replacement was performed for this verification.
