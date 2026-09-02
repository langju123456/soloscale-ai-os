from __future__ import annotations

import pytest

import soloscale.resume_gateway_boundary as resume_boundary
from soloscale.resume_evidence_pack import build_candidate_evidence_pack
from soloscale.resume_gateway_boundary import (
    ResumeSourceParseError,
    ResumeUploadRole,
    SelectedResumeFile,
    extract_selected_resume_files,
)
from soloscale.resume_models import (
    CandidateProfile,
    ResumeAtomicFactAdmissionError,
    ResumeAtomicFactQuarantineReason,
    admit_resume_atomic_facts,
)


def _selected_pdf(content: bytes) -> SelectedResumeFile:
    return SelectedResumeFile(
        role=ResumeUploadRole.RESUME,
        filename="resume.pdf",
        content_type="application/pdf",
        content=content,
    )


def _simple_pdf(*values: str) -> bytes:
    operators = " ".join(f"({value}) Tj" for value in values).encode("utf-8")
    stream = b"BT /F1 12 Tf 72 720 Td " + operators + b" ET"
    return (
        b"%PDF-1.4\n"
        b"1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj\n"
        b"2 0 obj <</Type /Pages /Count 1 /Kids [3 0 R]>> endobj\n"
        b"3 0 obj <</Type /Page /Parent 2 0 R /Contents 4 0 R>> endobj\n"
        + f"4 0 obj <</Length {len(stream)}>> stream\n".encode()
        + stream
        + b"\nendstream endobj\n%%EOF"
    )


def test_pdf_quality_gate_rejects_glyph_fragments_at_source_parse() -> None:
    corrupted = _simple_pdf("4A", "B1", "C2", "D3", "E4", "F5")

    with pytest.raises(ResumeSourceParseError) as caught:
        extract_selected_resume_files([_selected_pdf(corrupted)])

    assert caught.value.code == "SOURCE_PARSE_UNRELIABLE_TEXT"
    assert "请改用 DOCX" in str(caught.value)
    assert "AtomicFact" not in str(caught.value)
    assert caught.value.trace is not None
    assert caught.value.trace.primary_quality == "UNRELIABLE"
    assert caught.value.trace.fallback_used is True
    assert caught.value.trace.fallback_quality == "UNRELIABLE"


@pytest.mark.parametrize(
    "text",
    (
        "Evidence grounded AI engineer with reliable retrieval workflows",
        "人工智能产品经理，构建可靠的检索工作流",
    ),
)
def test_pdf_quality_gate_preserves_natural_language_sources(text: str) -> None:
    extracted = extract_selected_resume_files([_selected_pdf(_simple_pdf(text))])

    assert extracted[ResumeUploadRole.RESUME].text == text
    trace = extracted[ResumeUploadRole.RESUME].source_parse_trace
    assert trace is not None
    assert trace.primary_quality == "USABLE"
    assert trace.fallback_used is False
    assert trace.fallback_quality == "NOT_USED"


def test_pdf_quality_gate_accepts_one_complete_fallback_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_text = "4A\nB1\nC2\nD3\nE4\nF5"
    fallback_text = "Evidence grounded AI engineer with reliable retrieval workflows"
    monkeypatch.setattr(
        resume_boundary,
        "_primary_pdf_text",
        lambda content: (primary_text, 1),
    )
    monkeypatch.setattr(
        resume_boundary,
        "_pypdf_text",
        lambda content: (fallback_text, 1),
    )

    extracted = extract_selected_resume_files([_selected_pdf(b"%PDF-1.4\n%%EOF")])
    resume = extracted[ResumeUploadRole.RESUME]

    assert resume.text == fallback_text
    assert primary_text not in resume.text
    assert resume.source_parse_trace is not None
    assert resume.source_parse_trace.primary_quality == "UNRELIABLE"
    assert resume.source_parse_trace.fallback_used is True
    assert resume.source_parse_trace.fallback_extractor == "pypdf"
    assert resume.source_parse_trace.fallback_quality == "USABLE"
    assert resume.source_parse_trace.source_parse_status == "USABLE"


def test_atomic_fact_admission_quarantines_one_fragment_and_continues() -> None:
    profile = CandidateProfile(
        skills=["AI", "Go"],
        project_bullets=["Built a reliable RAG workflow; x"],
    )

    facts, trace = admit_resume_atomic_facts(profile)

    assert [fact.text for fact in facts] == ["Built a reliable RAG workflow"]
    assert profile.skills == ["AI", "Go"]
    assert trace.candidate_facts_total == 2
    assert trace.candidate_facts_admitted == 1
    assert trace.candidate_facts_quarantined == 1
    assert trace.quarantine_reason_counts == {
        ResumeAtomicFactQuarantineReason.LOW_INFORMATION: 1
    }

    pack = build_candidate_evidence_pack(profile)
    assert pack.fact_admission == trace


def test_atomic_fact_admission_fails_clearly_when_all_fragments_are_invalid() -> None:
    profile = CandidateProfile(project_bullets=["x; -"])

    with pytest.raises(ResumeAtomicFactAdmissionError) as caught:
        admit_resume_atomic_facts(profile)

    assert caught.value.trace.candidate_facts_total == 2
    assert caught.value.trace.candidate_facts_admitted == 0
    assert caught.value.trace.candidate_facts_quarantined == 2
    assert caught.value.trace.quarantine_reason_counts == {
        ResumeAtomicFactQuarantineReason.LOW_INFORMATION: 1,
        ResumeAtomicFactQuarantineReason.STRUCTURAL_FRAGMENT: 1,
    }
