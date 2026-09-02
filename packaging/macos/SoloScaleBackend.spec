# ruff: noqa: F821
# This template packages only tracked application resources. It never discovers user data.
import os
import subprocess
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def tracked_files(*prefixes):
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--", *prefixes],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    records = []
    for raw_relative in completed.stdout.split(b"\0"):
        if not raw_relative:
            continue
        relative = Path(raw_relative.decode("utf-8"))
        source = ROOT / relative
        if source.is_file():
            records.append((str(source), str(relative.parent)))
    return records


datas = tracked_files(".agents")
datas.extend(collect_data_files("soloscale", includes=["content_data/*.json"]))
datas.extend(tracked_files("video_factory/render.mjs", "video_factory/src", "video_factory/package.json", "video_factory/package-lock.json", "video_factory/tsconfig.json"))
datas.append((str(ROOT / "media_runtime" / "qwen_mlx_worker.py"), "media_runtime"))
datas.append((str(ROOT / "video_factory" / "node_modules"), "video_factory/node_modules"))
excluded_untracked_web_modules = {"soloscale.app_web", "soloscale.resume_web"}
youtube_hidden_imports = [
    "google.auth.exceptions",
    "google.auth.transport.requests",
    "google.oauth2.credentials",
    "google_auth_httplib2",
    "google_auth_oauthlib.flow",
    "googleapiclient.discovery",
    "googleapiclient.errors",
    "googleapiclient.http",
    "httplib2",
]
a = Analysis(
    [str(ROOT / "src" / "soloscale" / "local_ui.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=collect_submodules(
        "soloscale", filter=lambda name: name not in excluded_untracked_web_modules
    )
    + youtube_hidden_imports,
    excludes=["node", "playwright", "selenium"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SoloScaleBackend",
    console=False,
    codesign_identity=os.environ.get("SOLOSCALE_CODESIGN_IDENTITY") or None,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="SoloScaleBackend")
