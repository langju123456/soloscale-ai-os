"""Focused tests for deterministic macOS validation launches."""

from __future__ import annotations

import importlib.util
import json
import plistlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "launch_macos_validation_app.py"


def _load_script() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "launch_macos_validation_app", SCRIPT_PATH
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _write_bundle(
    root: Path,
    *,
    sidecar_commit: str | None = None,
    sidecar_dirty: object = False,
    **overrides: str,
) -> Path:
    app_path = root / "SoloScale AI OS Test.app"
    executable = app_path / "Contents" / "MacOS" / "SoloScaleDesktop"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    payload = {
        "CFBundleDisplayName": "SoloScale AI OS Test",
        "CFBundleExecutable": "SoloScaleDesktop",
        "CFBundleIdentifier": "local.soloscale.desktop.test",
        "CFBundleShortVersionString": "0.4.1",
        "CFBundleVersion": "10",
        "SoloScaleBuildKind": "development",
        "SoloScaleGitBranch": "codex/test",
        "SoloScaleGitCommit": "a" * 40,
        "SoloScaleGitDirty": "false",
    }
    payload.update(overrides)
    with (app_path / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump(payload, stream)
    backend_root = app_path / "Contents" / "Resources" / "SoloScaleBackend"
    backend_root.mkdir(parents=True)
    backend = backend_root / "SoloScaleBackend"
    backend.write_text("#!/bin/sh\n", encoding="utf-8")
    backend.chmod(0o755)
    provenance = {
        "source_sha": sidecar_commit or payload["SoloScaleGitCommit"],
        "dirty": sidecar_dirty,
    }
    (backend_root / "source-provenance.json").write_text(
        json.dumps(provenance), encoding="utf-8"
    )
    return app_path


def test_extracts_app_bundle_from_frontend_and_backend_commands(tmp_path: Path) -> None:
    module = _load_script()
    app_path = _write_bundle(tmp_path / "path with spaces")

    frontend = module.app_bundle_from_command(
        f"{app_path}/Contents/MacOS/SoloScaleDesktop"
    )
    backend = module.app_bundle_from_command(
        f"{app_path}/Contents/Resources/SoloScaleBackend/SoloScaleBackend "
        "--desktop-mode"
    )

    assert frontend == (app_path.resolve(), "app")
    assert backend == (app_path.resolve(), "backend")


def test_target_requires_complete_build_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    app_path = _write_bundle(
        tmp_path,
        sidecar_commit="a" * 40,
        SoloScaleGitCommit="unknown",
    )
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)

    with pytest.raises(module.ValidationLaunchError, match="invalid SoloScaleGitCommit"):
        module.inspect_target(app_path)


def test_target_rejects_a_bundle_marked_as_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    app_path = _write_bundle(tmp_path, SoloScaleGitDirty="true")
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)

    with pytest.raises(module.ValidationLaunchError, match="clean source worktree"):
        module.inspect_target(app_path)


def test_target_rejects_a_sidecar_from_a_different_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    app_path = _write_bundle(tmp_path, sidecar_commit="b" * 40)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)

    with pytest.raises(module.ValidationLaunchError, match="does not match"):
        module.inspect_target(app_path)


@pytest.mark.parametrize("dirty_value", [True, "false", 0, None])
def test_target_rejects_a_sidecar_without_an_exact_clean_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dirty_value: object,
) -> None:
    module = _load_script()
    app_path = _write_bundle(tmp_path, sidecar_dirty=dirty_value)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)

    with pytest.raises(module.ValidationLaunchError, match="clean source worktree"):
        module.inspect_target(app_path)


def test_target_rejects_a_missing_or_malformed_sidecar_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    missing_app = _write_bundle(tmp_path / "missing")
    missing_receipt = (
        missing_app
        / "Contents"
        / "Resources"
        / "SoloScaleBackend"
        / "source-provenance.json"
    )
    missing_receipt.unlink()
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)

    with pytest.raises(module.ValidationLaunchError, match="missing or unsafe"):
        module.inspect_target(missing_app)

    malformed_app = _write_bundle(tmp_path / "malformed")
    malformed_receipt = (
        malformed_app
        / "Contents"
        / "Resources"
        / "SoloScaleBackend"
        / "source-provenance.json"
    )
    malformed_receipt.write_text("not-json", encoding="utf-8")

    with pytest.raises(module.ValidationLaunchError, match="invalid backend"):
        module.inspect_target(malformed_app)


def test_target_rejects_a_symlinked_sidecar_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    app_path = _write_bundle(tmp_path / "bundle")
    receipt = (
        app_path
        / "Contents"
        / "Resources"
        / "SoloScaleBackend"
        / "source-provenance.json"
    )
    external_receipt = tmp_path / "external-source-provenance.json"
    external_receipt.write_text(receipt.read_text(encoding="utf-8"), encoding="utf-8")
    receipt.unlink()
    receipt.symlink_to(external_receipt)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)

    with pytest.raises(module.ValidationLaunchError, match="missing or unsafe"):
        module.inspect_target(app_path)


def test_target_records_missing_icon_without_accepting_unrelated_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    app_path = _write_bundle(tmp_path)

    class SuccessfulVerification:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(
        module.subprocess, "run", lambda *args, **kwargs: SuccessfulVerification()
    )
    identity = module.inspect_target(app_path)
    assert identity.app_icon == "missing"
    assert identity.backend_git_commit == identity.git_commit
    assert identity.backend_git_dirty == "false"

    unrelated = _write_bundle(
        tmp_path / "other", CFBundleIdentifier="com.example.unrelated"
    )
    with pytest.raises(module.ValidationLaunchError, match="not a verified SoloScale"):
        module.inspect_target(unrelated)


def test_process_cleanup_fails_closed_for_unverified_soloscale_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()

    class ProcessSnapshot:
        stdout = "123 1 /deleted/Test.app/Contents/MacOS/SoloScaleDesktop\n"

    monkeypatch.setattr(
        module.subprocess, "run", lambda *args, **kwargs: ProcessSnapshot()
    )
    with pytest.raises(module.ValidationLaunchError, match="nothing was terminated"):
        module.list_managed_processes()

    assert module.list_managed_processes(allow_unverified_pids=frozenset({123})) == []
