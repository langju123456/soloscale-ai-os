from __future__ import annotations

from soloscale.casebook_models import EvidenceKind, EvidenceReceipt, LearningCase, PracticeStage
from soloscale.casebook_store import derive_mastery
from soloscale.interview_packet import (
    render_interview_packet,
    render_interview_packet_markdown,
)


def _case() -> LearningCase:
    return LearningCase(
        id="structured-output-incident",
        title="Structured output incident",
        project="BuildLog",
        problem="The evaluator stopped before returning a scored result.",
        expected_behavior="The response parses before schema validation.",
        actual_behavior="Parsing failed before schema validation.",
        root_cause="The response was invalid JSON; the raw body is unknown.",
        resolution="Preserve the boundary evidence before selecting recovery work.",
        verification=["The failure is classified at the parser boundary."],
        concepts=["parsing versus validation", "evidence boundaries"],
        repository="example/buildlog",
        alternatives_considered=["Retry without classifying the failure."],
        trade_offs=["Failing closed reduces unsupported recovery guesses."],
        unknowns=["The rejected response body was not retained."],
        evidence=[
            EvidenceReceipt(
                id="evidence-private-session",
                kind=EvidenceKind.CHAT,
                source_path="/Users/private/raw-session-secret.md",
                archived_path=(
                    "cases/structured-output-incident/evidence/"
                    f"{'a' * 64}-selected-session.md"
                ),
                sha256="a" * 64,
                byte_size=321,
            )
        ],
    )


def test_packet_contains_case_reasoning_evidence_index_and_all_five_exercises() -> None:
    case = _case()
    mastery = derive_mastery(case, [])

    packet = render_interview_packet(case, mastery)

    assert "## Case Brief" in packet
    assert "## Expected vs. Actual" in packet
    assert "## Root Cause" in packet
    assert "## Resolution" in packet
    assert "## Verification" in packet
    assert "## Unknowns" in packet
    assert "## Evidence Index" in packet
    assert "## Concept Checklist" in packet
    assert "## Practice Exercises" in packet
    assert "## Mastery Status" in packet
    assert "evidence-private-session" in packet
    assert "selected-session.md" not in packet
    assert "`chat`" in packet
    assert "321 bytes" in packet
    assert "- [ ] parsing versus validation" in packet
    for position, stage in enumerate(PracticeStage, start=1):
        assert f"### {position}. {stage.value.title()}" in packet
    assert packet.count("Pass criterion:") == 5
    assert "Derived status: `captured`" in packet
    assert "Next gate: `explain`" in packet


def test_packet_never_embeds_raw_evidence_or_original_source_path() -> None:
    case = _case()
    packet = render_interview_packet(case, derive_mastery(case, []))

    assert case.evidence[0].source_path not in packet
    assert case.evidence[0].archived_path not in packet
    assert "/Users/private" not in packet
    assert "PRIVATE RAW TRANSCRIPT BODY" not in packet
    assert "source_path" not in packet


def test_packet_omits_local_repository_paths_but_keeps_public_slugs() -> None:
    private_case = _case().model_copy(
        update={"repository": "/Users/alice/PrivateClient/repo"}
    )
    private_packet = render_interview_packet(
        private_case,
        derive_mastery(private_case, []),
    )
    public_case = _case().model_copy(update={"repository": "owner/public-repo"})
    public_packet = render_interview_packet(
        public_case,
        derive_mastery(public_case, []),
    )

    assert "/Users/alice" not in private_packet
    assert "Private reference retained" in private_packet
    assert "owner/public-repo" in public_packet


def test_packet_labels_readiness_as_self_assessment_not_external_validation() -> None:
    case = _case()
    packet = render_interview_packet_markdown(case, derive_mastery(case, []))

    assert "Self-assessment only" in packet
    assert "self-recorded pass" in packet
    assert "not an independent evaluation" in packet
    assert "guarantee of interview performance" in packet
