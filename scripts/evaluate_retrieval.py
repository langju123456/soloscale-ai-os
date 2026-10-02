#!/usr/bin/env python3
"""Write private offline retrieval evaluation reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from soloscale.retrieval_evaluation import RetrievalEvaluationError, run_evaluations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=Path(".soloscale/retrieval-evals"))
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    try:
        path = run_evaluations(args.output_root, compare=args.compare)
        print(path)
        if not json.loads((path / "report.json").read_text(encoding="utf-8"))["passed"]:
            return 1
    except RetrievalEvaluationError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
