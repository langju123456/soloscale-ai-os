# ADR-0005: Resume Intelligence trust and delivery boundary

**Status:** Accepted for local candidate implementation
**Date:** 2026-08-11

## Context

Conversation RAG can retrieve useful engineering history, but a lexical or model-generated
candidate is not proof that a career claim is true. Resume output is application-facing and
therefore needs a stricter fact boundary than evidence discovery. The optional external
resume library also creates a second persistence destination that can diverge from the
private SoloScale run.

## Decision

1. Resume claims come only from the operator-supplied `CandidateProfile`.
2. KnowledgeStore hits are lexical evidence candidates. They retain chunk, document,
   source, locator, role, timestamp, searchable-metadata, and content-hash lineage, but do
   not claim semantic requirement coverage.
3. The legacy action that rendered Evidence Agent claims as resume experience is disabled.
4. Internal runs use private, atomic files under `.soloscale/resume-runs/`.
5. An optional application bundle is built in a private staging directory and published by
   a platform-native atomic no-replace rename. Existing destinations, including an empty
   directory created during the preflight-to-publication race, are never replaced. A
   platform without this primitive fails closed.
6. `delivery.json` records `INTERNAL_READY`, `APPLICATION_LIBRARY_PENDING`,
   `APPLICATION_LIBRARY_SAVED`, `APPLICATION_LIBRARY_PUBLISHED_DURABILITY_UNCERTAIN`, or
   `APPLICATION_LIBRARY_FAILED`. The uncertain state includes the exact published path. The
   receipt remains available if final `run.json` cannot be written.
7. Managed roots reject symlinks throughout their lexical ancestry. UI-triggered
   application libraries must also be outside the Git repository.
8. No output automatically updates Casebook, BuildLog, a job application, deployment, or a
   publishing surface.

## Consequences

- Evidence discovery can reveal useful project material without silently converting it
  into personal claims.
- Dual-save failures are visible and recoverable instead of being reported as success.
- External application bundles remain intentionally small and human-reviewable.
- Semantic claim rewriting, job submission, cloud sync, and automatic promotion remain out
  of scope. The end-user flow may reorder intact uploaded-template blocks and stage the
  byte-identical generated DOCX inside the same atomic application bundle.
