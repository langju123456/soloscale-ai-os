#!/usr/bin/env python3
"""Attach a fresh post-revision Day 1 review to an assembled editorial week."""

from __future__ import annotations

import argparse
from pathlib import Path

from soloscale.editorial_workspace import finalize_post_revision_review


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-root", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    args = parser.parse_args()
    finalize_post_revision_review(
        batch_root=args.batch_root.absolute(),
        review_path=args.review.absolute(),
    )


if __name__ == "__main__":
    main()
