# ADR-0007: Web/Vercel is a public acquisition and lightweight entry surface

**Status:** Accepted  
**Date:** 2026-08-29

## Context

Uncommitted Web/Vercel work exists beside the Desktop product: `api/app.py`,
`api/resume.py`, `src/soloscale/app_web.py`, `src/soloscale/resume_web.py`,
`vercel.json`, `requirements.txt`, and their tests. It was intentionally
excluded from the Desktop RC package (the PyInstaller spec filters the untracked
Web modules).

The Desktop app is the canonical private operator surface: local data roots,
Casebook/Learning, Resume evidence, Creator production, and Publish Queue.

## Inspection findings

- `api/*` are Vercel serverless functions that import canonical `soloscale`
  domain modules (`resume_docx`, `resume_gateway_boundary`, `model_gateway`)
  instead of duplicating business logic.
- `app_web.py` renders a stateless public product shell with body-free preview
  views; it does not access private data roots, Casebook, or Publish Queue.
- `resume_web.py` is a stateless public Resume MVP using the canonical resume
  pipeline with its own public input bounds.
- `vercel.json` routes the public site paths to those two thin functions;
  `requirements.txt` is only `pydantic`.

## Decision (Option B)

Web is primarily an acquisition/landing surface plus lightweight stateless
entry points (public Resume MVP and honest preview pages). It shares canonical
backend/domain logic and must never become a second full SoloScale
implementation.

- Keep Web intentionally thin: no local data roots, no private state, no
  duplicate domain models.
- Keep the untracked Web work preserved in its own future branch/thread; do not
  fold it into the Desktop package or the Desktop canonical paths.
- The Desktop package continues to exclude the Web modules.

## Consequences

### Positive

- public surface stays honest and body-free by design;
- canonical Resume truth/evidence boundaries are reused, not forked;
- Desktop release isolation is preserved.

### Negative / deferred

- Web disposition (branch/PR vs defer) still needs an operator decision;
- the public Resume MVP is not dogfooded in this release;
- no Vercel deployment is performed from the Desktop release thread.
