# Agent Instructions

## Read first

Before changing code, read:

1. `README.md`
2. `PROJECT.md`
3. `TASK.md`
4. `docs/architecture.md`
5. relevant ADRs

## Operating rules

- Before inventing a workflow, read `.agents/skills/registry.yaml` and prefer a matching
  `ACTIVE` Skill. A `CANDIDATE` or `DRAFT` Skill may be used only with its status visible.
- Compose registered Skills in dependency order instead of copying large workflow prompts.
- Record every selected Skill and exact version in the private Skill Run Receipt.
- After a completed, operator-approved Run, evaluate whether to make no Skill change,
  create a candidate, propose a version update, or deprecate a Skill. Never silently
  rewrite an old version or auto-promote an experimental Run.
- Preserve explicit human approval before publication, paid use, credential or permission
  changes, destructive operations, database migrations, history rewriting, or deployment.
- Make the smallest change that satisfies the approved task.
- Do not redesign the product during implementation.
- Do not read or commit `.env`, credentials, private keys, tokens, or ignored private notes.
- Do not run `git push`, deploy, publish, or modify production data without explicit human approval.
- Do not suppress a failing test merely to make CI green.
- Do not claim a command passed unless its real exit code and output were observed.
- Preserve public development evidence: decision, diff, commands, tests, and result.
- Use structured models for cross-role handoffs.
- Add or update tests for behavior changes.
- Keep generated run artifacts under `.soloscale/`, which is ignored by default.

## Local MVP Product Mode

For bounded local, private, single-operator slices, implement only safeguards needed to
prevent private-data leakage, destructive loss, unsupported career/public claims,
irreversible external actions, and unbounded model/tool usage. Record other production
concerns as `DEFERRED_PRODUCTION_HARDENING`; do not add authentication, multi-tenancy,
queues, distributed locks, rate limiting, cloud deployment, OAuth, or broad framework
migrations unless an approved acceptance criterion requires them.

## Commands

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
ruff check .
mypy src tests
pytest -q
python -m build
```

## macOS Desktop toolchain

- Before a Desktop build, run `./scripts/check_macos_toolchain.sh`.
- Build only through `./scripts/build_macos_app.sh`; it loads the canonical
  `desktop/macos/toolchain.env` instead of trusting ambient `xcode-select`,
  `DEVELOPER_DIR`, or `SDKROOT` state.
- If preflight reports compiler or SDK drift, stop and report it. Do not search for a
  random alternate Swift toolchain or silently change the pinned versions.
- The current pinned Command Line Tools fallback is provisional because full Xcode is not
  installed. After full Xcode is installed, switch the one config to `full-xcode`; never
  silently fall back to Command Line Tools afterward.

## Definition of done

- behavior matches the approved issue or Execution Packet
- targeted and full relevant tests pass
- lint and type checks pass
- no unrelated changes
- development record updated
- remaining risks stated honestly

## Autonomous thread bootstrap

When a thread starts with the SoloScale bootstrap prompt (or says "Execute the
SoloScale autonomous thread bootstrap"), first read:

- `.codex/SOLOSCALE_AGENT_OPERATING_CONTRACT.md` — the operating contract;
- `.codex/handoffs/SOLOSCALE_CURRENT_HANDOFF.md` — current branch/HEAD, dirty-work
  ownership, known issues, and next candidate slices.

Then choose exactly one bounded slice, execute, verify, checkpoint, update the
handoff, and stop.
