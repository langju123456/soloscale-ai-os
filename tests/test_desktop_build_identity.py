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


def test_build_identity_is_derived_from_the_exact_worktree() -> None:
    identity = _printed_build_identity()

    assert identity["build_kind"] == "development"
    assert identity["bundle_identifier"] == "local.soloscale.desktop.dev"
    assert identity["display_name"] == "SoloScale AI OS Dev"
    assert identity["git_branch"] == _git_output("branch", "--show-current")
    assert identity["git_commit"] == _git_output("rev-parse", "HEAD")
    assert identity["git_dirty"] == _git_dirty()


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

    result = subprocess.run(
        ["bash", str(script)],
        cwd=repository,
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
