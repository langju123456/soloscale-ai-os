from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from soloscale.casebook_models import (
    AttemptOutcome,
    DerivedCaseStatus,
    EngineeringState,
    EvidenceKind,
    EvidenceReceipt,
    LearningCase,
    MasterySnapshot,
    PracticeAttempt,
    PracticeReceipt,
    PracticeStage,
)
from soloscale.models import ContractModel

SHA256 = "a" * 64


def evidence_payload(
    *,
    receipt_id: str = "evidence-001",
    archived_path: str = "cases/case-001/evidence/test.txt",
) -> dict[str, object]:
    return {
        "id": receipt_id,
        "kind": "test",
        "source_path": "tmp/test-output.txt",
        "archived_path": archived_path,
        "sha256": SHA256,
        "byte_size": 42,
    }


def practice_receipt_payload() -> dict[str, object]:
    return {
        "id": "practice-001",
        "source_path": "tmp/explanation.md",
        "archived_path": "cases/case-001/practice/explanation.md",
        "sha256": SHA256,
        "byte_size": 21,
    }


def learning_case_payload() -> dict[str, object]:
    return {
        "id": "case-001",
        "title": "Trace a stale cache result",
        "project": "SoloScale AI OS",
        "problem": "A cached response survived invalidation.",
        "expected_behavior": "Invalidation should expose the new response.",
        "actual_behavior": "The stale response remained visible.",
        "root_cause": "The mutation did not update the cache tag.",
        "resolution": "Update the tag after the mutation commits.",
        "verification": ["The focused cache test passes."],
        "concepts": ["cache invalidation"],
        "evidence": [evidence_payload()],
    }


def empty_stage_results() -> dict[PracticeStage, AttemptOutcome | None]:
    return {stage: None for stage in PracticeStage}


def captured_snapshot_payload() -> dict[str, object]:
    return {
        "case_id": "case-001",
        "stage_results": empty_stage_results(),
        "passed_stages": [],
        "next_stage": "explain",
        "status": "captured",
        "interview_ready": False,
    }


def test_enum_values_and_practice_stage_order_are_stable() -> None:
    assert [kind.value for kind in EvidenceKind] == [
        "chat",
        "codex",
        "terminal",
        "diff",
        "test",
        "ci",
        "code",
        "document",
        "other",
    ]
    assert [stage.value for stage in PracticeStage] == [
        "explain",
        "trace",
        "rebuild",
        "debug",
        "defend",
    ]
    assert [outcome.value for outcome in AttemptOutcome] == ["pass", "needs-work"]
    assert [status.value for status in DerivedCaseStatus] == [
        "captured",
        "in-practice",
        "self-assessed-interview-ready",
    ]
    assert [state.value for state in EngineeringState] == ["resolved"]


@pytest.mark.parametrize(
    ("model_type", "payload_factory"),
    [
        (EvidenceReceipt, evidence_payload),
        (LearningCase, learning_case_payload),
        (PracticeReceipt, practice_receipt_payload),
        (
            PracticeAttempt,
            lambda: {
                "case_id": "case-001",
                "stage": "explain",
                "outcome": "pass",
                "receipt": practice_receipt_payload(),
            },
        ),
        (MasterySnapshot, captured_snapshot_payload),
    ],
)
def test_casebook_contracts_are_versioned_and_forbid_unknown_fields(
    model_type: type[ContractModel],
    payload_factory: Callable[[], dict[str, object]],
) -> None:
    payload = payload_factory()
    model = model_type.model_validate(payload)
    assert model.model_dump()["schema_version"] == "0.1"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model_type.model_validate({**payload, "unexpected_field": True})

    with pytest.raises(ValidationError, match="Input should be '0.1'"):
        model_type.model_validate({**payload, "schema_version": "0.2"})


def test_receipts_accept_valid_metadata_and_use_aware_capture_times() -> None:
    evidence = EvidenceReceipt.model_validate(evidence_payload())
    practice = PracticeReceipt.model_validate(practice_receipt_payload())

    assert evidence.kind is EvidenceKind.TEST
    assert evidence.captured_at.tzinfo is not None
    assert practice.captured_at.tzinfo is not None


@pytest.mark.parametrize("model_type", [EvidenceReceipt, PracticeReceipt])
@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("id", ""),
        ("id", "../receipt"),
        ("source_path", " \t"),
        ("archived_path", "\n"),
        ("sha256", "a" * 63),
        ("sha256", "A" * 64),
        ("sha256", "g" * 64),
        ("byte_size", 0),
        ("byte_size", -1),
    ],
)
def test_receipts_reject_unsafe_or_invalid_metadata(
    model_type: type[EvidenceReceipt] | type[PracticeReceipt],
    field: str,
    invalid_value: object,
) -> None:
    payload = evidence_payload() if model_type is EvidenceReceipt else practice_receipt_payload()
    payload[field] = invalid_value

    with pytest.raises(ValidationError):
        model_type.model_validate(payload)


def test_learning_case_accepts_a_complete_resolved_incident() -> None:
    case = LearningCase.model_validate(learning_case_payload())

    assert case.engineering_state is EngineeringState.RESOLVED
    assert case.repository is None
    assert case.alternatives_considered == []
    assert case.trade_offs == []
    assert case.unknowns == []
    assert case.created_at.tzinfo is not None


def test_learning_case_collection_defaults_are_not_shared() -> None:
    first = LearningCase.model_validate(learning_case_payload())
    second_payload = learning_case_payload()
    second_payload["id"] = "case-002"
    second = LearningCase.model_validate(second_payload)

    first.unknowns.append("Whether the upstream API also caches the response.")

    assert second.unknowns == []


@pytest.mark.parametrize(
    "case_id",
    ["ab", "Case-001", "case_001", "case--001", "case-001-", "../case-001"],
)
def test_learning_case_id_must_be_a_safe_lowercase_hyphen_slug(case_id: str) -> None:
    payload = learning_case_payload()
    payload["id"] = case_id

    with pytest.raises(ValidationError):
        LearningCase.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    [
        "title",
        "project",
        "problem",
        "expected_behavior",
        "actual_behavior",
        "root_cause",
        "resolution",
    ],
)
def test_learning_case_rejects_blank_required_text(field: str) -> None:
    payload = learning_case_payload()
    payload[field] = "  \n"

    with pytest.raises(ValidationError):
        LearningCase.model_validate(payload)


@pytest.mark.parametrize("field", ["verification", "concepts", "evidence"])
def test_learning_case_requires_nonempty_core_collections(field: str) -> None:
    payload = learning_case_payload()
    payload[field] = []

    with pytest.raises(ValidationError):
        LearningCase.model_validate(payload)


@pytest.mark.parametrize("field", ["verification", "concepts"])
def test_learning_case_rejects_blank_core_collection_items(field: str) -> None:
    payload = learning_case_payload()
    payload[field] = ["valid", " \t"]

    with pytest.raises(ValidationError):
        LearningCase.model_validate(payload)


def test_learning_case_requires_unique_evidence_ids() -> None:
    payload = learning_case_payload()
    payload["evidence"] = [
        evidence_payload(),
        evidence_payload(archived_path="cases/case-001/evidence/other.txt"),
    ]

    with pytest.raises(ValidationError, match="evidence receipt ids must be unique"):
        LearningCase.model_validate(payload)


def test_learning_case_requires_unique_evidence_archived_paths() -> None:
    payload = learning_case_payload()
    payload["evidence"] = [
        evidence_payload(),
        evidence_payload(receipt_id="evidence-002"),
    ]

    with pytest.raises(ValidationError, match="evidence archived paths must be unique"):
        LearningCase.model_validate(payload)


def test_learning_case_rejects_non_resolved_engineering_state() -> None:
    payload = learning_case_payload()
    payload["engineering_state"] = "open"

    with pytest.raises(ValidationError):
        LearningCase.model_validate(payload)


def test_pass_attempt_requires_and_accepts_a_receipt() -> None:
    with pytest.raises(ValidationError, match="pass attempts require a receipt"):
        PracticeAttempt(
            case_id="case-001",
            stage=PracticeStage.EXPLAIN,
            outcome=AttemptOutcome.PASS,
        )

    attempt = PracticeAttempt(
        case_id="case-001",
        stage=PracticeStage.EXPLAIN,
        outcome=AttemptOutcome.PASS,
        receipt=PracticeReceipt.model_validate(practice_receipt_payload()),
    )
    assert attempt.receipt is not None
    assert attempt.created_at.tzinfo is not None


@pytest.mark.parametrize("note", [None, "", " \n"])
def test_needs_work_attempt_requires_a_nonblank_note(note: str | None) -> None:
    with pytest.raises(ValidationError, match="needs-work attempts require a nonblank note"):
        PracticeAttempt(
            case_id="case-001",
            stage=PracticeStage.TRACE,
            outcome=AttemptOutcome.NEEDS_WORK,
            note=note,
        )


def test_needs_work_attempt_accepts_a_note_without_a_receipt() -> None:
    attempt = PracticeAttempt(
        case_id="case-001",
        stage=PracticeStage.TRACE,
        outcome=AttemptOutcome.NEEDS_WORK,
        note="I could not yet identify where the tag is applied.",
    )

    assert attempt.receipt is None


def test_captured_mastery_snapshot_is_consistent_and_ordered() -> None:
    payload = captured_snapshot_payload()
    payload["stage_results"] = dict(reversed(list(empty_stage_results().items())))

    snapshot = MasterySnapshot.model_validate(payload)

    assert list(snapshot.stage_results) == list(PracticeStage)
    assert snapshot.next_stage is PracticeStage.EXPLAIN
    assert snapshot.status is DerivedCaseStatus.CAPTURED
    assert snapshot.interview_ready is False


def test_in_practice_mastery_snapshot_tracks_passes_and_first_unfinished_stage() -> None:
    results = empty_stage_results()
    results[PracticeStage.EXPLAIN] = AttemptOutcome.PASS
    results[PracticeStage.TRACE] = AttemptOutcome.NEEDS_WORK

    snapshot = MasterySnapshot(
        case_id="case-001",
        stage_results=results,
        passed_stages=[PracticeStage.EXPLAIN],
        next_stage=PracticeStage.TRACE,
        status=DerivedCaseStatus.IN_PRACTICE,
        interview_ready=False,
    )

    assert snapshot.stage_results[PracticeStage.TRACE] is AttemptOutcome.NEEDS_WORK


def test_all_passed_stages_are_self_assessed_interview_ready() -> None:
    results: dict[PracticeStage, AttemptOutcome | None] = {
        stage: AttemptOutcome.PASS for stage in PracticeStage
    }

    snapshot = MasterySnapshot(
        case_id="case-001",
        stage_results=results,
        passed_stages=list(PracticeStage),
        status=DerivedCaseStatus.SELF_ASSESSED_INTERVIEW_READY,
        interview_ready=True,
    )

    assert snapshot.next_stage is None


def test_mastery_snapshot_requires_exactly_all_five_stage_results() -> None:
    payload = captured_snapshot_payload()
    results = empty_stage_results()
    del results[PracticeStage.DEFEND]
    payload["stage_results"] = results

    with pytest.raises(
        ValidationError,
        match="stage_results must contain every practice stage exactly once",
    ):
        MasterySnapshot.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "invalid_value", "message"),
    [
        (
            "passed_stages",
            [PracticeStage.EXPLAIN],
            "passed_stages must match pass results",
        ),
        ("next_stage", PracticeStage.TRACE, "next_stage must be the first practice stage"),
        ("status", DerivedCaseStatus.IN_PRACTICE, "status does not match"),
        ("interview_ready", True, "interview_ready must be true exactly"),
    ],
)
def test_mastery_snapshot_rejects_inconsistent_derived_fields(
    field: str,
    invalid_value: object,
    message: str,
) -> None:
    payload = captured_snapshot_payload()
    payload[field] = invalid_value

    with pytest.raises(ValidationError, match=message):
        MasterySnapshot.model_validate(payload)
