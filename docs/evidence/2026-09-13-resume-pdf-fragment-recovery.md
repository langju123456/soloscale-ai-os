# Resume PDF mixed-fragment recovery

Date: 2026-09-13
Status: verified source fix; local commit and candidate packaging approved
Base commit: `71aa95e4ec31643a1c5fa57afbd54bade5d8f25f`

## Defect and bounded fix

The bounded stream extractor can turn positioned PDF text into many one/two-character
lines. Occasional readable words previously let this fragmented output pass the source
quality gate. Without intact headings, normalization then treated each fragment as a
work-experience bullet and hit the existing 120-entry limit before model execution.

The shared PDF quality check now rejects predominantly fragmented text: at least 20
nonblank lines, with at least 75% containing no more than two characters. This selects
the existing bounded pypdf fallback. The fallback must pass the same quality check or
the request stops with the existing source-parse error. Outputs are not concatenated,
truncated, summarized, or sent to a model for recovery.

No schema, dependency, credential, security limit, model, or 120-entry cap changed.
This remains a conservative heuristic, not an OCR or general PDF-layout guarantee;
unusual short-label-only documents may still need another supported input format.

## Verification

- Added seven parameterized regression cases. Three failed on the original code,
  proving mixed-fragment admission and failure to stop before normalization.
- Real, public-safe synthetic PDF positioning fixture exercises actual pypdf recovery
  and checks exact paragraph, project, experience, skill, and education content.
- Normal English/Chinese text and short skill labels remain accepted.
- Unrecoverable mixed fragments stop before normalization and model selection for
  both output locales. Exactly 120 real bullets remain accepted; 121 are rejected.
- Focused input-quality and upload-boundary suite: **22 passed**, exit 0.
- Full Python suite: **661 passed, 1 skipped**, exit 0. The skip requires the optional
  non-vendored `buildlog.models`. One existing third-party deprecation warning remains.
- Ruff over `src tests scripts`: passed, exit 0.
- Strict mypy over `src tests`: no issues in 144 source files, exit 0.
- `git diff --check`: passed, exit 0.
- Private one-page operator PDF: previously 2,117 false experience entries; after the
  fix, 57 extracted lines, six project bullets and two experience bullets. All eight
  bullets remain exact substrings of the recovered source. No private PDF/text is
  included in the tracked tests or this record.

The first sandboxed full run hit five local-loopback permission failures; rerunning
the unchanged suite with local network permission produced the final passing result.
Existing local/cached development tooling was reused without installing dependencies.

## Verification boundary and remaining gates

At the time of the verification above, no real provider call, paid generation, Git write
operation, candidate build, installed App replacement, or publication had occurred.
The operator subsequently approved a local commit and separate candidate App build;
packaging and launch results are recorded separately, not implied by source tests.
No push, installed App replacement, or publication is included. Actual model generation, 600/800-character
recovery, failed-input retention, and complete bilingual output remain separate work.
