#!/usr/bin/env python3
"""Launch exactly one provenance-stamped SoloScale macOS validation build."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

FRONTEND_MARKER = "/Contents/MacOS/SoloScaleDesktop"
BACKEND_MARKER = "/Contents/Resources/SoloScaleBackend/SoloScaleBackend"
BUNDLE_ID_PREFIX = "local.soloscale.desktop"
DISPLAY_NAME_PREFIX = "SoloScale AI OS"
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class ValidationLaunchError(RuntimeError):
    """Raised when a deterministic validation launch cannot be guaranteed."""


@dataclass(frozen=True)
class BundleIdentity:
    app_path: str
    bundle_identifier: str
    display_name: str
    version: str
    build_number: str
    build_kind: str
    git_branch: str
    git_commit: str
    git_dirty: str
    backend_git_commit: str
    backend_git_dirty: str
    app_icon: str


@dataclass(frozen=True)
class ManagedProcess:
    pid: int
    parent_pid: int
    role: str
    app_path: str


def _canonical_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=True)


def _load_plist(app_path: Path) -> dict[str, object]:
    plist_path = app_path / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as stream:
            payload = plistlib.load(stream)
    except (FileNotFoundError, plistlib.InvalidFileException) as exc:
        raise ValidationLaunchError(f"invalid app bundle metadata: {plist_path}") from exc
    if not isinstance(payload, dict):
        raise ValidationLaunchError(f"invalid app bundle metadata: {plist_path}")
    return payload


def _text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def _is_soloscale_bundle(payload: dict[str, object]) -> bool:
    bundle_identifier = _text(payload, "CFBundleIdentifier")
    display_name = _text(payload, "CFBundleDisplayName")
    executable = _text(payload, "CFBundleExecutable")
    return (
        executable == "SoloScaleDesktop"
        and (
            bundle_identifier == BUNDLE_ID_PREFIX
            or bundle_identifier.startswith(f"{BUNDLE_ID_PREFIX}.")
        )
        and display_name.startswith(DISPLAY_NAME_PREFIX)
    )


def _app_icon_status(app_path: Path, payload: dict[str, object]) -> str:
    icon_name = _text(payload, "CFBundleIconFile") or _text(
        payload, "CFBundleIconName"
    )
    if not icon_name:
        return "missing"
    candidates = [icon_name]
    if not Path(icon_name).suffix:
        candidates.append(f"{icon_name}.icns")
    resources = app_path / "Contents" / "Resources"
    if any((resources / candidate).is_file() for candidate in candidates):
        return "present"
    return f"declared-but-missing:{icon_name}"


def _load_backend_provenance(app_path: Path) -> tuple[str, str]:
    sidecar_root = app_path / "Contents" / "Resources" / "SoloScaleBackend"
    executable = sidecar_root / "SoloScaleBackend"
    receipt_path = sidecar_root / "source-provenance.json"
    if not executable.is_file() or executable.is_symlink() or not os.access(executable, os.X_OK):
        raise ValidationLaunchError(f"backend sidecar is missing or unsafe: {executable}")
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise ValidationLaunchError(
            f"backend source provenance receipt is missing or unsafe: {receipt_path}"
        )
    try:
        with receipt_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationLaunchError(
            f"invalid backend source provenance receipt: {receipt_path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValidationLaunchError(
            f"invalid backend source provenance receipt: {receipt_path}"
        )
    commit = payload.get("source_sha")
    dirty = payload.get("dirty")
    if not isinstance(commit, str) or COMMIT_PATTERN.fullmatch(commit) is None:
        raise ValidationLaunchError("backend source provenance commit is invalid")
    if dirty is not False:
        raise ValidationLaunchError(
            "backend sidecar was not built from a clean source worktree"
        )
    return commit, "false"


def inspect_target(app_path: Path) -> BundleIdentity:
    canonical_app = _canonical_path(app_path)
    if canonical_app.suffix != ".app" or not canonical_app.is_dir():
        raise ValidationLaunchError(f"target is not an app bundle: {canonical_app}")
    payload = _load_plist(canonical_app)
    if not _is_soloscale_bundle(payload):
        raise ValidationLaunchError(
            "target is not a verified SoloScale desktop bundle: "
            f"{canonical_app}"
        )

    executable = canonical_app / "Contents" / "MacOS" / "SoloScaleDesktop"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValidationLaunchError(f"app executable is missing: {executable}")

    backend_git_commit, backend_git_dirty = _load_backend_provenance(canonical_app)

    identity = BundleIdentity(
        app_path=str(canonical_app),
        bundle_identifier=_text(payload, "CFBundleIdentifier"),
        display_name=_text(payload, "CFBundleDisplayName"),
        version=_text(payload, "CFBundleShortVersionString"),
        build_number=_text(payload, "CFBundleVersion"),
        build_kind=_text(payload, "SoloScaleBuildKind"),
        git_branch=_text(payload, "SoloScaleGitBranch"),
        git_commit=_text(payload, "SoloScaleGitCommit"),
        git_dirty=_text(payload, "SoloScaleGitDirty"),
        backend_git_commit=backend_git_commit,
        backend_git_dirty=backend_git_dirty,
        app_icon=_app_icon_status(canonical_app, payload),
    )
    missing = [
        field
        for field in (
            "version",
            "build_number",
            "build_kind",
            "git_branch",
            "git_dirty",
        )
        if not getattr(identity, field) or getattr(identity, field) == "unknown"
    ]
    if missing:
        raise ValidationLaunchError(
            "validation bundle is missing provenance metadata: " + ", ".join(missing)
        )
    if COMMIT_PATTERN.fullmatch(identity.git_commit) is None:
        raise ValidationLaunchError(
            "validation bundle has an invalid SoloScaleGitCommit: "
            f"{identity.git_commit or 'missing'}"
        )
    if identity.git_dirty != "false":
        raise ValidationLaunchError(
            "validation bundle was not built from a clean source worktree"
        )
    if identity.backend_git_commit != identity.git_commit:
        raise ValidationLaunchError(
            "backend sidecar source commit does not match the App source commit"
        )

    verification = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(canonical_app)],
        check=False,
        capture_output=True,
        text=True,
    )
    if verification.returncode != 0:
        detail = verification.stderr.strip() or verification.stdout.strip()
        raise ValidationLaunchError(f"codesign verification failed: {detail}")
    return identity


def app_bundle_from_command(command: str) -> tuple[Path, str] | None:
    for marker, role in ((FRONTEND_MARKER, "app"), (BACKEND_MARKER, "backend")):
        marker_index = command.find(marker)
        if marker_index < 0:
            continue
        prefix = command[:marker_index].strip()
        if not prefix.startswith("/"):
            return None
        try:
            return Path(prefix).resolve(strict=True), role
        except FileNotFoundError:
            return None
    return None


def list_managed_processes(
    *, allow_unverified_pids: frozenset[int] = frozenset()
) -> list[ManagedProcess]:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
    managed: list[ManagedProcess] = []
    unverified: list[str] = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) != 3:
            continue
        pid_text, parent_text, command = fields
        if "SoloScaleDesktop" not in command and "SoloScaleBackend" not in command:
            continue
        extracted = app_bundle_from_command(command)
        if extracted is None:
            if int(pid_text) not in allow_unverified_pids:
                unverified.append(f"pid={pid_text} command={command}")
            continue
        app_path, role = extracted
        try:
            payload = _load_plist(app_path)
        except ValidationLaunchError:
            if int(pid_text) not in allow_unverified_pids:
                unverified.append(f"pid={pid_text} command={command}")
            continue
        if not _is_soloscale_bundle(payload):
            if int(pid_text) not in allow_unverified_pids:
                unverified.append(f"pid={pid_text} command={command}")
            continue
        managed.append(
            ManagedProcess(
                pid=int(pid_text),
                parent_pid=int(parent_text),
                role=role,
                app_path=str(app_path),
            )
        )
    if unverified:
        raise ValidationLaunchError(
            "found SoloScale-named processes without verifiable bundle metadata; "
            "nothing was terminated: " + "; ".join(unverified)
        )
    return sorted(managed, key=lambda process: (process.role != "app", process.pid))


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_no_managed_processes(
    timeout_seconds: float, terminating_pids: frozenset[int]
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        managed = list_managed_processes(allow_unverified_pids=terminating_pids)
        if not managed and not any(_pid_exists(pid) for pid in terminating_pids):
            list_managed_processes()
            return
        time.sleep(0.2)
    remaining = list_managed_processes()
    details = ", ".join(
        f"pid={process.pid} role={process.role} app={process.app_path}"
        for process in remaining
    )
    raise ValidationLaunchError(
        "SoloScale processes did not terminate cleanly; launch aborted: " + details
    )


def terminate_existing_instances(timeout_seconds: float) -> list[ManagedProcess]:
    existing = list_managed_processes()
    for process in existing:
        print(
            "TERMINATING_EXISTING_INSTANCE="
            f"pid={process.pid} role={process.role} app={process.app_path}"
        )
        try:
            os.kill(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    _wait_for_no_managed_processes(
        timeout_seconds, frozenset(process.pid for process in existing)
    )
    return existing


def _wait_for_single_target(
    target: Path, timeout_seconds: float
) -> tuple[ManagedProcess, list[ManagedProcess]]:
    deadline = time.monotonic() + timeout_seconds
    target_text = str(target)
    last_seen: list[ManagedProcess] = []
    while time.monotonic() < deadline:
        last_seen = list_managed_processes()
        app_processes = [process for process in last_seen if process.role == "app"]
        backend_processes = [
            process for process in last_seen if process.role == "backend"
        ]
        if (
            len(app_processes) == 1
            and len(backend_processes) == 1
            and app_processes[0].app_path == target_text
            and backend_processes[0].app_path == target_text
        ):
            return app_processes[0], last_seen
        time.sleep(0.2)
    details = ", ".join(
        f"pid={process.pid} role={process.role} app={process.app_path}"
        for process in last_seen
    )
    raise ValidationLaunchError(
        "did not observe exactly one intended SoloScale app/backend pair after launch: "
        + (details or "no managed process found")
    )


def _write_receipt(
    receipt_dir: Path,
    identity: BundleIdentity,
    launch_gate_fix_commit: str,
    terminated: list[ManagedProcess],
    running: list[ManagedProcess],
) -> Path:
    receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(receipt_dir, 0o700)
    timestamp = datetime.now(UTC)
    payload = {
        "schema_version": 1,
        "validated_at": timestamp.isoformat(),
        "launch_command": ["/usr/bin/open", identity.app_path],
        "open_new_instance_flag": False,
        "launch_gate_fix_commit": launch_gate_fix_commit,
        "identity": asdict(identity),
        "terminated_existing_processes": [asdict(process) for process in terminated],
        "stale_instances_before_launch": 0,
        "running_processes": [asdict(process) for process in running],
        "intended_app_instances": sum(
            process.role == "app" and process.app_path == identity.app_path
            for process in running
        ),
        "stale_instances_after_test": sum(
            process.app_path != identity.app_path for process in running
        ),
    }
    receipt_name = timestamp.strftime("%Y%m%dT%H%M%S.%fZ.json")
    receipt_path = receipt_dir / receipt_name
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=receipt_dir, delete=False
    ) as stream:
        stream.write(encoded)
        temporary_path = Path(stream.name)
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, receipt_path)

    latest_path = receipt_dir / "latest.json"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=receipt_dir, delete=False
    ) as stream:
        stream.write(encoded)
        latest_temporary_path = Path(stream.name)
    os.chmod(latest_temporary_path, 0o600)
    os.replace(latest_temporary_path, latest_path)
    return receipt_path


def _launch_gate_fix_commit() -> str:
    script_path = Path(__file__).resolve()
    repository_root = script_path.parents[1]
    relative_path = script_path.relative_to(repository_root)
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", str(relative_path)],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0 or status.stdout.strip():
        return "UNCOMMITTED"
    revision = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", str(relative_path)],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    commit = revision.stdout.strip()
    return commit if revision.returncode == 0 and len(commit) == 40 else "UNKNOWN"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Terminate verified SoloScale desktop instances, confirm a clean process "
            "state, and launch exactly one provenance-stamped app bundle."
        )
    )
    parser.add_argument("app_path", type=Path, help="Exact .app bundle to validate")
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Seconds to wait for termination and launch (default: 15)",
    )
    parser.add_argument(
        "--receipt-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / ".soloscale"
        / "desktop-validation",
        help="Private receipt directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.timeout <= 0:
        raise ValidationLaunchError("--timeout must be positive")

    identity = inspect_target(arguments.app_path)
    if identity.app_icon != "present":
        print(f"WARNING_APP_ICON={identity.app_icon}", file=sys.stderr)

    terminated = terminate_existing_instances(arguments.timeout)
    before_launch = list_managed_processes()
    if before_launch:
        raise ValidationLaunchError("stale SoloScale process remains; launch aborted")

    subprocess.run(["/usr/bin/open", identity.app_path], check=True)
    target = Path(identity.app_path)
    app_process, running = _wait_for_single_target(target, arguments.timeout)
    stale_count = sum(process.app_path != identity.app_path for process in running)
    launch_gate_fix_commit = _launch_gate_fix_commit()
    receipt_path = _write_receipt(
        arguments.receipt_dir,
        identity,
        launch_gate_fix_commit,
        terminated,
        running,
    )

    print(f"VALIDATED_APP_PATH={identity.app_path}")
    print(f"VERSION_BUILD={identity.version} ({identity.build_number})")
    print(f"VALIDATED_SOURCE_COMMIT={identity.git_commit}")
    print(f"LAUNCH_GATE_FIX_COMMIT={launch_gate_fix_commit}")
    print(f"BUNDLE_IDENTIFIER={identity.bundle_identifier}")
    print(f"RUNNING_APP_PID={app_process.pid}")
    print("STALE_INSTANCES_BEFORE_LAUNCH=0")
    print(f"STALE_INSTANCES_AFTER_TEST={stale_count}")
    print(f"APP_ICON={identity.app_icon}")
    print(f"VALIDATION_RECEIPT={receipt_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.SubprocessError, ValidationLaunchError) as exc:
        print(f"macOS validation launch: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
