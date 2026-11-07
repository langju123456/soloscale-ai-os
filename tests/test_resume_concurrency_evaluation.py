from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from soloscale.model_gateway import ModelGatewayTimeoutError
from soloscale.resume_cloud_models import CreateTaskRequest
from soloscale.resume_cloud_service import prepare_cloud_input
from soloscale.resume_docx import tailor_resume_docx_with_gateway


def _load() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/evaluate_resume_concurrency.py"
    spec = importlib.util.spec_from_file_location("evaluate_resume_concurrency", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


evaluation = _load()
FakeGateway = evaluation.FakeGateway
distribution = evaluation.distribution
evidence_is_private = evaluation.evidence_is_private
payload = evaluation.payload
peak = evaluation.peak
render_report = evaluation.render_report
safe_loopback_dsn = evaluation.safe_loopback_dsn


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u@db/resume_service_eval",
        "postgresql://u@127.0.0.1/postgres",
        "postgresql://u@127.0.0.1/resume_service_eval?host=evil",
        "postgresql://u@127.0.0.1:99999/resume_service_eval",
    ],
)
def test_refuses_unsafe_database(dsn: str) -> None:
    assert not safe_loopback_dsn(dsn)


def test_allows_only_disposable_loopback_names() -> None:
    assert safe_loopback_dsn("postgresql://u@127.0.0.1/resume_service_eval_run1")


@pytest.mark.parametrize("error_rate", [0.0, 1.0])
def test_fake_exercises_canonical_pipeline_and_records_no_content(
    tmp_path: Path, error_rate: float
) -> None:
    audit = tmp_path / "audit.jsonl"
    fake = FakeGateway(audit, 0, error_rate, 7)
    template, jd, *_ = prepare_cloud_input(CreateTaskRequest.model_validate(payload(1)))
    if error_rate:
        with pytest.raises(ModelGatewayTimeoutError):
            tailor_resume_docx_with_gateway(template, jd, gateway=fake)
    else:
        result = tailor_resume_docx_with_gateway(template, jd, gateway=fake)
        assert result.content.startswith(b"PK")
    assert fake.calls == 1
    record = json.loads(audit.read_text())
    assert record["error_category"] == ("gateway_timeout" if error_rate else None)
    assert record["duration_seconds"] >= 0
    assert set(record) == {
        "ordinal",
        "pid",
        "phase",
        "start_ns",
        "end_ns",
        "duration_seconds",
        "error_category",
    }
    assert "Python" not in audit.read_text()


def test_peak_counts_overlaps_including_boundary_and_duration_percentiles() -> None:
    assert (
        peak(
            [
                {"start_ns": 0, "end_ns": 5},
                {"start_ns": 2, "end_ns": 7},
                {"start_ns": 5, "end_ns": 6},
            ]
        )
        == 2
    )
    assert distribution([1.0, 2.0, 3.0]) == {"count": 3, "p50": 2.0, "p95": 2.9, "max": 3.0}
    assert distribution([])["p95"] is None


def test_report_displays_observed_values_and_failed_assertions() -> None:
    text = render_report(
        {
            "status": "FAILED",
            "source": {"head": "abc", "dirty": False},
            "model": {"calls": 7, "aggregate_peak": 3},
            "assertions": {"cache_hit_zero_model_calls": False},
        }
    )
    assert "| model.calls | `7` |" in text
    assert "| cache_hit_zero_model_calls | False |" in text


def test_evidence_scan_rejects_raw_content_and_credentials(tmp_path: Path) -> None:
    artifact = tmp_path / "report.json"
    artifact.write_text('{"calls": 3}')
    assert evidence_is_private(tmp_path, "secret-bearer-value")
    artifact.write_text("Built a Python retrieval system.")
    assert not evidence_is_private(tmp_path, "secret-bearer-value")
    artifact.write_text("secret-bearer-value")
    assert not evidence_is_private(tmp_path, "secret-bearer-value")
