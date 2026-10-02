# Local delivery closeout — 0.4.2 / macOS build 9

The delivery target is the existing local SoloScale product, the original public
GitHub repository, a source-matched desktop candidate, and the existing Vercel
Showcase. AWS is explicitly deferred. The Showcase is a static evidence surface;
it is not the hosted Resume API.

## Evidence boundary

The starting source was the clean public-sync branch at
`6ef52b414dd51bbe731a0d191c1c368fffa8bc6a`. It passed 717 tests, Ruff, strict mypy
(157 files), wheel/sdist build, and real loopback UI generation/download smoke.
Ten tests skipped locally: nine require an explicit test PostgreSQL URL; one needs
the separate BuildLog models. Those skips are not production deployment proof.
GitHub run [36928212122](https://github.com/langju123456/soloscale-ai-os/actions/runs/36928212122)
passed both supported Python versions and the non-root container smoke.

The baseline [service receipt](../evidence/2026-10-02-resume-service-evaluation.json)
covers synthetic template-mode 20-task/2-worker execution and a killed worker entering
human review, not real AI content quality or cloud availability. The offline Agent
edge-case evaluation similarly measures its specified fixtures, not open-world safety.

The previous real Chinese run exported a document while failing substantive-edit
quality. Closeout found an additional unmeasured accuracy-improvement assertion that
was accepted by the old lexical gate. The release tightens the bounded Chinese
outcome gate and prompt. Human semantic review is still necessary: schema validity,
source IDs, and the 25% text-change proxy do not establish truth or usefulness.
Private operator input/output bodies and model prompts are excluded from this repo.

The repaired gate passed 720 local tests with the same 10 explicit skips, Ruff, strict
mypy and package builds. The [sanitized real Chinese evaluation](../evidence/2026-10-02-chinese-resume-closeout.json)
records three bounded localhost qwen3:8b attempts (zero paid calls), two substantive
source-supported edits in the final candidate, and one-page native Pages/PDF visual
review. Earlier attempts failed quality; one successful case does not establish a
general success rate. Gap labels and role-positioning text remain unverified.
The source resume and formal application materials were not changed.

## Primary workflow

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
soloscale-ui --help
soloscale-ui --host 127.0.0.1 --port 8765 --data-root /absolute/private/run-root
```

Open `/resume`, upload an operator-approved source resume and JD, choose an explicitly
configured provider (or template mode), review gaps/provenance, download the draft,
and approve its content before use. `/resume/intelligence` is a separate preflight.
Learning and Content drafts remain human-controlled. Do not import private histories
or make paid calls merely to complete this release.

## Reproduce verification

```bash
ruff check .
mypy src tests
pytest -q
python -m build
python scripts/resume_api_smoke.py
./scripts/check_macos_toolchain.sh
```

Use isolated data roots and an explicit disposable test database when running DB
integration tests. Keep service evaluation receipts separate from deployment claims.

## Desktop candidate

Build only from a clean committed checkout with the existing pinned Xcode toolchain,
Python desktop dependencies and locked local video dependencies:

```bash
SOLOSCALE_PYTHON="$PWD/.venv/bin/python" ./packaging/macos/build_backend_onedir.sh /absolute/build-root
SOLOSCALE_APP_OUTPUT=/absolute/app-root SOLOSCALE_SIDECAR_ROOT=/absolute/build-root/SoloScaleBackend ./scripts/build_macos_app.sh
python scripts/launch_macos_validation_app.py '/absolute/app-root/SoloScale AI OS Dev.app' --receipt-dir /absolute/private/receipts
```

App and sidecar must record the same full source SHA and `dirty=false`. Version/build
are 0.4.2 / 9. Local ad-hoc signing is an install/test candidate; this procedure does
not assert Apple notarization or store distribution. A candidate may be installed
side by side to preserve the older production app and user data.

## Publication and rollback

Only the existing `langju123456/soloscale-ai-os` repository and linked
`soloscale-showcase` Vercel project are release targets. Review the tracked diff for
private material, push without force, verify exact-SHA CI and mergeability, and keep
main's prior SHA for a revert if needed. Build the desktop from the final source SHA.
Validate a protected preview using Vercel's authenticated curl, then promote the same
deployment. Record the former production deployment ID for Vercel rollback.

No application, raw candidate document, secret, local receipt directory, or build
artifact is part of the public source push. No new hosting target, account, credential,
service purchase, AWS resource, or formal resume change is part of this closeout.
