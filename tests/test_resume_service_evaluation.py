from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest


def _module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_resume_service.py"
    spec = importlib.util.spec_from_file_location("evaluate_resume_service", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_only_disposable_loopback_postgres_uri_is_accepted() -> None:
    module = _module()
    assert module._safe_loopback_postgres("postgresql://x@127.0.0.1/resume_service_eval")
    assert not module._safe_loopback_postgres("postgresql://x@db.example/resume_service_eval")
    assert not module._safe_loopback_postgres("postgresql://x@127.0.0.1/other")
    assert not module._safe_loopback_postgres("postgresql://x@localhost/resume_service_eval")
    assert not module._safe_loopback_postgres("postgresql://x@localhost.evil/resume_service_eval")
    assert not module._safe_loopback_postgres("postgresql://127.0.0.1@evil/resume_service_eval")
    assert not module._safe_loopback_postgres(
        "postgresql://x@127.0.0.1/resume_service_eval?host=evil"
    )
    assert not module._safe_loopback_postgres("postgresql://x@127.0.0.1/other_resume_service_eval")


def test_docx_gate_rejects_non_docx_and_wrong_hash() -> None:
    module = _module()
    assert not module._docx_ok(b"not a docx", "0" * 64)


def test_private_directory_rejects_symlink_without_changing_target(tmp_path: Path) -> None:
    module = _module()
    target = tmp_path / "unrelated"
    target.mkdir(mode=0o755)
    before = target.stat().st_mode
    link = tmp_path / "private-output"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(module.ResumeServiceEvaluationError):
        module._private_directory(link)
    assert target.stat().st_mode == before


def test_missing_explicit_approval_never_connects_to_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setenv("RESUME_CLOUD_DATABASE_URL", "postgresql://x@127.0.0.1/resume_service_eval")
    monkeypatch.setattr(sys, "argv", ["evaluate_resume_service.py"])

    def unexpected_database(_: str) -> bool:
        raise AssertionError("pre-approval database access")

    monkeypatch.setattr(module, "_empty_database", unexpected_database)
    assert module.main() == 2


def test_running_but_expired_claim_cannot_prove_pre_expiry_interruption() -> None:
    module = _module()
    observed = datetime.now(UTC)
    row = {
        "state": "RUNNING",
        "observed_at": observed,
        "lease_expires_at": observed - timedelta(seconds=1),
        "attempt_count": 1,
        "model_call_attempted": False,
        "output_absent": True,
    }
    assert not module._active_claim(row)
    row["lease_expires_at"] = observed + timedelta(seconds=1)
    assert module._active_claim(row)
