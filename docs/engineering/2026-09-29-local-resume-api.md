# Local Resume API

`soloscale.resume_api` adds an optional FastAPI practice surface for the existing local
resume pipeline. It binds only to `127.0.0.1`, accepts one base64-encoded selected resume
and a JD, and holds all material in memory.

`POST /resume/preview` calls `extract_selected_resume_files`, normalizes non-DOCX text
with `normalize_text_resume_to_docx`, then calls `extract_candidate_profile` and the
canonical entry-count check. It returns source format/hash, parsed-profile counters, and
body-free parsing diagnostics. `POST /resume/tailor` uses the resulting DOCX with
`tailor_resume_docx`; that function reorders matching project and skill blocks and rejects
any output whose paragraphs do not equal the input paragraphs. The API returns a binary
DOCX attachment; mode, zero model calls, hashes, reorder counts, and claim preservation are
response headers. Both endpoints state `mode: template-only` and `model_calls: 0`.

The ASGI receive wrapper counts each incoming chunk and rejects a body over the complete
request budget before FastAPI buffers or parses it. The budget includes a 5 MiB resume's
base64 representation, a 255-character filename, a 50,000-character JD escaped as JSON
Unicode, and wrapper overhead. It does not trust `Content-Length`. Canonical parser failures
become stable 4xx error envelopes without input bodies or tracebacks.
Synchronous `def` handlers are intentional: FastAPI runs them in its thread pool, so this
bounded canonical parsing does not block the async event loop. This small loopback utility
has no background queue, provider call, or persistence.

Run `pip install -e '.[dev,api]'`, then `python scripts/demo_resume_api.py`. The demo
starts a real ephemeral-port localhost process, posts synthetic Chinese resume text, checks
the returned DOCX for its original claim, writes a unique `.soloscale/demo-resume-api-*/`
directory with a downloadable synthetic DOCX and receipt, and terminates the child process.

Deferred production concerns include authentication, tenancy, durable storage, asynchronous
workers, public hosting, multipart form uploads, and model-backed rewriting. None are part
of this API.

Verification recorded on 2026-09-29 after the final repair: `7` API tests and the full
suite (`668` passed, `1` existing optional BuildLog skip) passed on local Python 3.14.
Full Ruff, strict mypy (`146` source files), sdist/wheel build, and `git diff --check`
also passed with exit 0. The final real localhost demo received 200 from health,
preview, and binary tailoring; confirmed the original Chinese claim in its DOCX;
and received 400 for invalid base64. Its artifacts are in
`.soloscale/demo-resume-api-hky3ssih/`. Python 3.11/3.12 remain configured CI targets;
remote CI was not triggered. Two third-party deprecation warnings remain.

The first implementation returned JSON/base64 from tailoring; it was repaired to return a
binary DOCX attachment with deterministic diagnostics in headers. A receive-time body-limit
exception initially became FastAPI 400; it was repaired with a bounded ASGI pre-reader that
returns 413 before JSON buffering. The test suite also caught an intermediate typed-preview
syntax error before functional validation.

Fresh review found two additional defects. The demo could validate an unrelated compatible
server when its requested port was occupied, so it now reserves a loopback listener and
passes that exact inherited descriptor to its child; an occupied-port regression exits
nonzero before creating a receipt. The original body allowance covered only base64 plus a
small margin, so a declared maximum Chinese JD could be rejected before parsing; the joint
joint field budget and boundary tests now keep the declared fields consistent. Both
filename and JD budget 12 bytes per Python character for JSON surrogate-pair escapes;
a maximum-size resume plus maximum emoji filename/JD reaches canonical validation.
The independent reviewer confirmed both fixes. These checks demonstrate local engineering
behavior, not operator mastery or recruiting outcomes.

Demo checklist: install `.[dev,api]`; run `python scripts/demo_resume_api.py`; check the
printed 200/400 receipt; open the unique ignored `tailored-resume.docx`; confirm its Chinese
claim; confirm the child localhost process has exited; and verify that a deliberately
occupied `SOLOSCALE_RESUME_API_PORT` fails without a new receipt.

Interview prompts: explain the difference between HTTP body limits and `Content-Length`;
describe why base64 requires an encoded-size budget; and explain how deterministic DOCX
reordering preserves claims without claiming an AI rewrite.

Resume-ready bullet: Built a loopback-only FastAPI resume-preparation API that reuses a
bounded DOCX/PDF parser, enforces streamed request limits, returns typed diagnostics, and
verifies deterministic claim-preserving DOCX output with zero model calls.
