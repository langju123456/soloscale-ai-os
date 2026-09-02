# Contributing

SoloScale AI OS is currently dogfooding its first manual-to-automated workflow. Small, evidence-backed changes are welcome.

## Before opening a change

1. Start from a structured Issue or Task Envelope.
2. Treat accepted ADRs and frozen decisions as constraints.
3. Keep unrelated refactors out of the change.
4. Never commit credentials, raw private chats, customer data, or `.soloscale/` run directories.

## Local verification

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
ruff check .
mypy src tests
pytest -q
python -m build
```

Report the exact commands, exit codes, and remaining risks in the pull request. A passing test claim without an observable command result is not evidence.

## Pull requests

- Link the approved Issue or Task Envelope.
- Explain any deviation from the Execution Packet.
- Add or update tests for behavior changes.
- Update a DevLog when the iteration produces a reusable decision or lesson.
- Leave deploy, publish, permission, secret, and destructive actions behind an explicit human gate.
