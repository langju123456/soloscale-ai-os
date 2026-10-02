"""Focused tests for macOS build identity and source provenance."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

from soloscale.local_ui import DesktopBuildIdentity, _desktop_build_identity, _page

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = REPOSITORY_ROOT / "scripts" / "build_macos_app.sh"
BACKEND_BUILD_SCRIPT = (
    REPOSITORY_ROOT / "packaging" / "macos" / "build_backend_onedir.sh"
)
INFO_TEMPLATE = REPOSITORY_ROOT / "desktop" / "macos" / "Info.plist.template"
BUILD_ENVIRONMENT_KEYS = {
    "SOLOSCALE_APP_BUNDLE_NAME",
    "SOLOSCALE_BUILD_KIND",
    "SOLOSCALE_BUILD_NUMBER",
    "SOLOSCALE_BUNDLE_IDENTIFIER",
    "SOLOSCALE_DISPLAY_NAME",
    "SOLOSCALE_VERSION",
}


def _printed_build_identity(**overrides: str) -> dict[str, str]:
    environment = os.environ.copy()
    for key in BUILD_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    environment.update(overrides)
    result = subprocess.run(
        ["bash", str(BUILD_SCRIPT), "--print-build-identity"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


def _git_output(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_dirty() -> str:
    return "true" if _git_output("status", "--porcelain", "--untracked-files=normal") else "false"


def _mock_macos_receipt_toolchain(tmp_path: Path) -> Path:
    toolchain_bin = tmp_path / "macos-toolchain-bin"
    toolchain_bin.mkdir()
    uname = toolchain_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    plutil = toolchain_bin / "plutil"
    plutil.write_text(
        """#!/usr/bin/env python3
import json
import sys

key = sys.argv[2]
with open(sys.argv[-1], encoding="utf-8") as stream:
    value = json.load(stream)[key]
if isinstance(value, bool):
    print(str(value).lower())
elif isinstance(value, str):
    print(value)
else:
    raise SystemExit(1)
""",
        encoding="utf-8",
    )
    plutil.chmod(0o755)
    return toolchain_bin


def test_build_identity_is_derived_from_the_exact_worktree() -> None:
    identity = _printed_build_identity()

    assert identity["build_kind"] == "development"
    assert identity["bundle_identifier"] == "local.soloscale.desktop.dev"
    assert identity["display_name"] == "SoloScale AI OS Dev"
    # PR checkouts are detached; the build contract labels the absent branch,
    # while retaining the exact commit and dirty state.
    assert identity["git_branch"] == (_git_output("branch", "--show-current") or "unknown")
    assert identity["git_commit"] == _git_output("rev-parse", "HEAD")
    assert identity["git_dirty"] == _git_dirty()


def test_detached_build_identity_preserves_exact_commit_and_clean_state(tmp_path: Path) -> None:
    repository = tmp_path / "detached-source"
    script = repository / "scripts" / BUILD_SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copy2(BUILD_SCRIPT, script)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "add", "scripts"], cwd=repository, check=True)
    subprocess.run(
        [
            "git", "-c", "user.name=SoloScale Test",
            "-c", "user.email=soloscale@example.com", "commit", "-qm", "identity fixture",
        ],
        cwd=repository,
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    subprocess.run(["git", "checkout", "--detach", "-q", commit], cwd=repository, check=True)
    result = subprocess.run(
        ["bash", str(script), "--print-build-identity"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    identity = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert identity["git_branch"] == "unknown"
    assert identity["git_commit"] == commit
    assert len(identity["git_commit"]) == 40
    assert identity["git_dirty"] == "false"


def test_app_build_refuses_a_dirty_or_uncommitted_source_tree(tmp_path: Path) -> None:
    repository = tmp_path / "source"
    script = repository / "scripts" / BUILD_SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copy2(BUILD_SCRIPT, script)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "add", str(script)], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=SoloScale Test",
            "-c",
            "user.email=soloscale@example.com",
            "commit",
            "-qm",
            "test fixture",
        ],
        cwd=repository,
        check=True,
    )
    script.write_text(script.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(script)],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "source worktree must be clean and fully committed" in result.stderr


def test_backend_build_refuses_a_dirty_or_uncommitted_source_tree(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "source"
    script = repository / "packaging" / "macos" / BACKEND_BUILD_SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copy2(BACKEND_BUILD_SCRIPT, script)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "add", str(script)], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=SoloScale Test",
            "-c",
            "user.email=soloscale@example.com",
            "commit",
            "-qm",
            "test fixture",
        ],
        cwd=repository,
        check=True,
    )
    script.write_text(script.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(script)],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "source worktree must be clean and fully committed" in result.stderr


def test_app_build_rejects_a_sidecar_from_a_different_commit(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "source"
    script = repository / "scripts" / BUILD_SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copy2(BUILD_SCRIPT, script)
    script.write_text(
        script.read_text(encoding="utf-8").replace("/usr/bin/plutil", "plutil"),
        encoding="utf-8",
    )
    (repository / ".gitignore").write_text("dist/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "add", str(script), str(repository / ".gitignore")],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=SoloScale Test",
            "-c",
            "user.email=soloscale@example.com",
            "commit",
            "-qm",
            "test fixture",
        ],
        cwd=repository,
        check=True,
    )
    sidecar_root = repository / "packaging" / "macos" / "dist" / "SoloScaleBackend"
    sidecar_root.mkdir(parents=True)
    sidecar = sidecar_root / "SoloScaleBackend"
    sidecar.write_text("#!/bin/sh\n", encoding="utf-8")
    sidecar.chmod(0o755)
    (sidecar_root / "source-provenance.json").write_text(
        json.dumps(
            {
                "source_sha": "b" * 40,
                "dirty": False,
            }
        ),
        encoding="utf-8",
    )
    toolchain_bin = _mock_macos_receipt_toolchain(tmp_path)
    environment = os.environ.copy()
    environment["PATH"] = f"{toolchain_bin}{os.pathsep}{environment['PATH']}"

    result = subprocess.run(
        ["bash", str(script)],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "sidecar source commit does not match" in result.stderr


def test_production_identity_remains_the_canonical_bundle() -> None:
    production = _printed_build_identity(SOLOSCALE_BUILD_KIND="production")
    with INFO_TEMPLATE.open("rb") as stream:
        template = plistlib.load(stream)

    assert production["bundle_identifier"] == "local.soloscale.desktop"
    assert production["display_name"] == "SoloScale AI OS"
    assert template["SoloScaleBuildKind"] == "production"
    assert template["SoloScaleGitBranch"] == "unknown"
    assert template["SoloScaleGitCommit"] == "unknown"


def test_advanced_page_renders_only_allowlisted_build_provenance(
    tmp_path: Path,
) -> None:
    bundle_path = str(tmp_path / "SoloScale AI OS Gate A.app")
    identity = DesktopBuildIdentity(
        app_version="0.4.1",
        build_number="6",
        build_kind="development",
        bundle_id="local.soloscale.desktop.gatea",
        display_name="SoloScale AI OS Gate A",
        git_branch="codex/canonical-gate-a",
        git_commit="a" * 40,
        git_dirty="false",
        bundle_path=bundle_path,
    )
    page = _page(None, tmp_path / "data", {}, "en", build_identity=identity)

    assert "SoloScale AI OS Gate A" in page
    assert "0.4.1 (build 6)" in page
    assert "codex/canonical-gate-a" in page
    assert "a" * 40 in page
    assert "clean and committed" in page
    assert bundle_path in page
    assert "embedded in the app bundle at build time" in page

    unknown = _desktop_build_identity(
        {
            "OPENAI_API_KEY": "sk-private-must-not-render",
            "SOLOSCALE_DESKTOP_DISPLAY_NAME": "SoloScale AI OS Dev",
        }
    )
    unknown_page = _page(None, tmp_path / "other", {}, "en", build_identity=unknown)
    assert unknown.git_commit == "unknown"
    assert unknown.git_dirty == "unknown"
    assert "sk-private-must-not-render" not in unknown_page
