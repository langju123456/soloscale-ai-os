#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python_bin="${SOLOSCALE_PYTHON:-$project_root/.venv/bin/python}"
output_root="${1:-$project_root/packaging/macos/dist}"
spec_file="$project_root/packaging/macos/SoloScaleBackend.spec"
fail() { echo "macOS packaging: $*" >&2; exit 1; }

git_commit="$(git -C "$project_root" rev-parse HEAD 2>/dev/null || true)"
[[ "$git_commit" =~ ^[0-9a-f]{40}$ ]] || fail "source commit is unavailable or not a full Git SHA"
git_status="$(git -C "$project_root" status --porcelain --untracked-files=normal 2>/dev/null)" || fail "source worktree status is unavailable"
[[ -z "$git_status" ]] || fail "source worktree must be clean and fully committed before building the backend"
[[ "$(uname -s)" == "Darwin" ]] || fail "must run on macOS"
[[ -x "$python_bin" ]] || fail "Python environment is missing: $python_bin"
[[ -f "$spec_file" && -f "$project_root/src/soloscale/local_ui.py" ]] || fail "SoloScale packaging inputs are missing"
"$python_bin" -m PyInstaller --version >/dev/null 2>&1 || fail "PyInstaller is not installed in $python_bin; install it outside this script"
[[ ! -e "$output_root/SoloScaleBackend" ]] || fail "output already exists: $output_root/SoloScaleBackend"
mkdir -p "$output_root"
work_root="$(mktemp -d "$output_root/.pyinstaller-work.XXXXXX")"
trap 'rm -rf "$work_root"' EXIT
"$python_bin" -m PyInstaller --noconfirm --distpath "$output_root" --workpath "$work_root" "$spec_file"
sidecar="$output_root/SoloScaleBackend/SoloScaleBackend"
[[ -x "$sidecar" ]] || fail "PyInstaller did not create an executable sidecar"
webpack_cache="$output_root/SoloScaleBackend/_internal/video_factory/node_modules/.cache/webpack"
if [[ -d "$webpack_cache" ]]; then
  rm -rf -- "$webpack_cache"
fi
if find "$output_root/SoloScaleBackend" \( -iname '.env' -o -iname '.env.*' -o -iname '.soloscale' -o -iname 'credentials' -o -iname '*libreoffice*' -o -iname 'Google Chrome.app' -o -iname 'chrome-headless-shell' \) -print -quit | grep -q .; then
  fail "refusing output containing excluded private or unsupported runtime data"
fi
[[ ! -e "$webpack_cache" ]] || fail "Creator Video webpack cache must not be packaged"
[[ -f "$output_root/SoloScaleBackend/_internal/video_factory/render.mjs" ]] || fail "Creator Video renderer was not packaged"
[[ -d "$output_root/SoloScaleBackend/_internal/video_factory/node_modules/@remotion/renderer" ]] || fail "Creator Video dependencies were not packaged"
[[ -f "$output_root/SoloScaleBackend/_internal/media_runtime/qwen_mlx_worker.py" ]] || fail "Local Qwen media worker was not packaged"
receipt="$output_root/SoloScaleBackend/source-provenance.json"
"$python_bin" - "$receipt" "$git_commit" <<'PY'
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

receipt = Path(sys.argv[1])
payload = {
    "source_sha": sys.argv[2],
    "dirty": False,
}
with tempfile.NamedTemporaryFile(
    mode="w", encoding="utf-8", dir=receipt.parent, delete=False
) as stream:
    json.dump(payload, stream, indent=2, sort_keys=True)
    stream.write("\n")
    temporary_path = Path(stream.name)
os.replace(temporary_path, receipt)
PY
[[ -f "$receipt" && ! -L "$receipt" ]] || fail "backend source provenance receipt was not created safely"
echo "$output_root/SoloScaleBackend"
