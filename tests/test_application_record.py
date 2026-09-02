from __future__ import annotations

import json
from pathlib import Path

import pytest

from soloscale.application_record import (
    ApplicationStatus,
    applications_root,
    link_learning_case,
    list_application_records,
    load_application_record,
    update_application_status,
)
from soloscale.casebook_models import EvidenceKind
from soloscale.casebook_store import CasebookStore, CaseNotFoundError


def _write_bundle(
    library_root: Path,
    *,
    directory: str = "2026-08-29_Example_Company_Job_No-Job-ID",
    status: str = "DRAFT_REQUIRES_HUMAN_REVIEW",
) -> Path:
    application_dir = applications_root(library_root) / directory
    application_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "company": "Example Company",
        "role": "Job",
        "job_id": None,
        "source_url": None,
        "captured_at": "2026-08-29",
        "jd_filename": "JD.md",
        "resume_filename": "Resume.md",
        "soloscale_run_id": "resume-20260829T010203Z-abcdef1234",
        "status": status,
    }
    (application_dir / "application.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return application_dir


def _create_learning_case(tmp_path: Path) -> str:
    source = tmp_path / "evidence.txt"
    source.write_bytes(b"private evidence")
    store = CasebookStore(tmp_path / "data")
    case = store.create_case(
        case_id="career-link-case",
        title="Cache invalidation failure",
        project="SoloScale AI OS",
        problem="A stale response survived a completed mutation.",
        expected_behavior="The mutation exposes the current response.",
        actual_behavior="The stale response remained visible.",
        root_cause="The mutation omitted cache-tag invalidation.",
        resolution="Invalidate the tag after the transaction commits.",
        verification=["The focused regression test passes."],
        concepts=["cache invalidation"],
        evidence_sources=[(EvidenceKind.TEST, source)],
    )
    return case.id


def test_load_application_record_migrates_legacy_draft_status(tmp_path: Path) -> None:
    application_dir = _write_bundle(tmp_path / "library")
    record = load_application_record(application_dir)

    assert record.status is ApplicationStatus.DRAFT
    assert record.soloscale_run_id == "resume-20260829T010203Z-abcdef1234"
    assert record.company == "Example Company"


def test_update_application_status_persists_operator_outcome(tmp_path: Path) -> None:
    application_dir = _write_bundle(tmp_path / "library")
    record = update_application_status(
        application_dir=application_dir,
        status=ApplicationStatus.APPLIED,
        note="Application submitted after human review.",
        next_action="Prepare for a possible interview.",
    )

    assert record.status is ApplicationStatus.APPLIED
    assert record.next_action == "Prepare for a possible interview."
    assert "Application submitted after human review." in record.notes
    reloaded = load_application_record(application_dir)
    assert reloaded.status is ApplicationStatus.APPLIED
    assert reloaded.notes == ["Application submitted after human review."]


def test_list_application_records_returns_bundles(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    _write_bundle(library_root, directory="2026-08-29_A_One_A")
    _write_bundle(library_root, directory="2026-08-30_B_Two_B")

    records = list_application_records(library_root)
    assert [record.directory_name for record in records] == [
        "2026-08-29_A_One_A",
        "2026-08-30_B_Two_B",
    ]


def test_link_learning_case_sets_interview_readiness_truth(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    application_dir = _write_bundle(library_root)
    case_id = _create_learning_case(tmp_path)

    record = link_learning_case(
        application_dir=application_dir,
        learning_case_id=case_id,
        data_root=tmp_path / "data",
    )

    assert record.learning_case_id == case_id
    assert record.interview_ready is False
    reloaded = load_application_record(application_dir)
    assert reloaded.learning_case_id == case_id
    assert reloaded.interview_ready is False


def test_link_learning_case_rejects_unknown_case(tmp_path: Path) -> None:
    application_dir = _write_bundle(tmp_path / "library")
    with pytest.raises(CaseNotFoundError):
        link_learning_case(
            application_dir=application_dir,
            learning_case_id="does-not-exist",
            data_root=tmp_path / "data",
        )
