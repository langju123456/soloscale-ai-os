# Creator Video Factory MVP

## Decision

Add a local, opt-in MP4 renderer after the Content Studio storyboard gate. Preserve the
claim-ledger boundary: the renderer may only consume saved scene fields from a selected
private content run.

## Implementation

- `video_factory/` contains a pinned Remotion 4.0.421 composition and renderer.
- `video_factory.py` creates a non-overwriting render input, MP4, and render receipt.
- `/content` exposes an explicit **生成 MP4 视频** action and serves the finished local MP4.
- macOS reuses an installed Google Chrome executable when available; otherwise Remotion's
  normal browser discovery applies.

## Validation

- TypeScript check passed.
- A temporary one-scene storyboard rendered to a 1.2 MB ISO MP4 artifact locally.
- Python targeted and full tests, Ruff, and `git diff --check` passed.

## Boundary

No model calls, generated narration, asset fetching, uploads, social-account access, or
publication actions are included. Remotion dependency installation is explicit and pinned.
