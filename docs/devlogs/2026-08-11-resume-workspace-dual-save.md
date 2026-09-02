# Resume Workspace dual-save

## Problem

Resume Workspace runs were complete and auditable under ignored
`.soloscale/resume-runs/`, but the operator's job-application documents lived in a separate
library. A successful run therefore required a manual copy step to keep the resume and its
controlling JD together.

## Decision

Preserve the internal evidence artifacts and add one delivery-state receipt plus an optional,
explicitly scoped
application-library sink. The local UI enables it by default at
`~/Documents/Resume Applications`; direct Python callers must opt in with
`application_library_root`.

The external bundle contains only:

- `JD.md`;
- the generated Markdown resume;
- the generated DOCX when the end-user template flow is used; and
- `application.json` with job identity, source, run ID, and review status.

The library must be outside the Git repository in the UI workflow. Repeated runs never
overwrite an existing application directory. The first run uses the
human-readable date/company/role/job-ID name; a later collision receives the unique
SoloScale run ID suffix. Files are built in a private staging directory and published with
a platform-native atomic no-replace rename; unsupported platforms fail closed. Managed
roots reject symlinks throughout their lexical ancestry and wrong types,
existing roots are tightened to private POSIX modes, and `delivery.json` records pending,
saved, published-but-durability-uncertain, or failed state with an exact published path.

## Boundary

This change does not semantically rewrite resume claims, call a network service, apply to a
job, publish data, or copy Conversation RAG evidence bodies into the application library.
The DOCX template workflow only reorders intact uploaded content and remains human-reviewed.
Resume facts come only from the operator's
Candidate Profile. Retrieval matches are lineage-backed lexical candidates, not semantic
coverage verification. The older Evidence-Agent-to-resume renderer is disabled.

## Verification

Targeted tests cover both destinations, non-overwrite behavior, private modes, UI wiring,
and the visible external path. Full repository verification is recorded in the task result.
