# ADR-0006: Start Learning Traceability with one grounded golden case

**Status:** Candidate
**Date:** 2026-08-11
**Target:** Local MVP

## Context

Conversation RAG, Casebook, Resume Workspace, and Control Tower already preserve different
parts of engineering evidence, but a user cannot yet follow one capability from reasoning
to code, tests, learning state, interview preparation, and career-claim safety. Expanding
every project and conversation at once would obscure the truth boundaries and delay a
usable learning outcome.

## Decision

Implement exactly one private vertical slice for Conversation RAG chunking and retrieval.
Use shared typed contracts and seven non-collapsible truth stages. Resolve repository,
branch, commit, files, symbols, line ranges, hashes, and committed test definitions from
the live worktree. Record missing raw conversation, CI, attribution, and mastery evidence
explicitly.

Build the evidence graph deterministically. Compile detailed material only for this case,
cache it by evidence hash, and expose progressive disclosure through the existing local
HTTP UI. A bounded source endpoint may open only anchors recorded in the private run.

Engineering completion does not imply human mastery. The initial case is
`ENGINEERING_VERIFIED`, `L0 Seen`, ownership unknown, and resume-ineligible. Explain and
Trace actions expose prompts and accept private response candidates, but never auto-create
mastery receipts. A saved response remains `SUBMITTED_REQUIRES_REVIEW` until a separate
human-reviewed mastery workflow exists.

## Consequences

- One capability is clickable from tracked reasoning to real implementation and tests.
- Private run bodies remain ignored under `.soloscale/` with `0700`/`0600` modes.
- The local runtime performs no automatic web, model, publishing, or application action.
- Raw ignored conversations are not read merely to fill a missing traceability field.
- Authentication, multi-user operation, cloud infrastructure, and other production
  controls are `DEFERRED_PRODUCTION_HARDENING`.
