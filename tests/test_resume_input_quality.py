from __future__ import annotations

import io
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import soloscale.resume_gateway_boundary as resume_boundary
from soloscale.local_ui import UploadedFile, _run_user_resume
from soloscale.resume_docx import extract_candidate_profile
from soloscale.resume_evidence_pack import build_candidate_evidence_pack
from soloscale.resume_gateway_boundary import (
    ResumeSourceParseError,
    ResumeUploadRole,
    SelectedResumeFile,
    extract_selected_resume_files,
    normalize_text_resume_to_docx,
)
from soloscale.resume_models import (
    CandidateProfile,
    ResumeAtomicFactAdmissionError,
    ResumeAtomicFactQuarantineReason,
    ResumeProfileLimitError,
    admit_resume_atomic_facts,
    validate_resume_profile_entry_count,
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


def _positioned_pdf(lines: list[str], *, fragmented: bool = False) -> bytes:
    """Synthetic text with real PDF positioning, not a private resume fixture."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {
                    NameObject("/F1"): DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/Type1"),
                            NameObject("/BaseFont"): NameObject("/Helvetica"),
                        }
                    ),
                }
            ),
        }
    )
    operators = ["BT /F1 10 Tf 12 TL 72 750 Td"]
    for line in lines:
        parts = [line]
        if fragmented:
            # A few whole words must not mask hundreds of one/two-letter runs.
            parts = [line[:3], *[line[i : i + 2] for i in range(3, len(line), 2)]]
        literals = [
            part.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for part in parts
        ]
        operators.append("[" + " ".join(f"({part})" for part in literals) + "] TJ T*")
    operators.append("ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(operators).encode("ascii"))
    page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_mixed_printable_fragments_recover_structure_via_real_fallback() -> None:
    lines = [
        "Example Candidate",
        "AI Engineer",
        "SUMMARY",
        "Built reliable local retrieval workflows.",
        "PROJECTS",
        "Evidence Tool",
        "- Built a grounded retrieval workflow with deterministic tests.",
        "- Measured retrieval quality using a labelled synthetic fixture.",
        "EDUCATION",
        "Example University - M.S. Information Systems",
        "SKILLS",
        "- Python, SQL, RAG",
        "EXPERIENCE",
        "Example Company",
        "- Delivered reliable features with an explicit review process.",
    ]
    content = _positioned_pdf(lines, fragmented=True)
    primary, _ = resume_boundary._primary_pdf_text(content)
    assert "\ufffd" not in primary
    assert len(primary.splitlines()) > 120
    assert any(len(line) >= 3 for line in primary.splitlines())

    resume = extract_selected_resume_files([_selected_pdf(content)])[ResumeUploadRole.RESUME]

    assert resume.text.splitlines() == lines
    trace = resume.source_parse_trace
    assert trace is not None
    assert trace.primary_quality == "UNRELIABLE"
    assert trace.fallback_used is True
    assert trace.fallback_quality == "USABLE"
    assert trace.source_parse_status == "USABLE"
    profile = extract_candidate_profile(normalize_text_resume_to_docx(resume.text))
    assert profile.project_bullets == [lines[6][2:], lines[7][2:]]
    assert profile.experience_bullets == [lines[14][2:]]
    assert profile.skills == ["Python, SQL, RAG"]
    assert profile.education == [lines[9]]
    validate_resume_profile_entry_count(profile)


@pytest.mark.parametrize(
    "text",
    [
        "SUMMARY\nBuilt reliable retrieval workflows.\nSKILLS\nAI\nGo\nUI\nUX",
        "个人简介\n构建可靠的检索工作流，并用测试验证结果。\n技能\n开发\n检索\n评估\nAI",
    ],
)
def test_pdf_quality_preserves_short_skill_labels_in_natural_resume(text: str) -> None:
    resume = extract_selected_resume_files([_selected_pdf(_simple_pdf(text))])[
        ResumeUploadRole.RESUME
    ]
    assert resume.text == text
    assert resume.source_parse_trace is not None
    assert resume.source_parse_trace.fallback_used is False


@pytest.mark.parametrize("locale", ["en-US", "zh-CN"])
def test_unrecoverable_mixed_fragments_stop_before_normalization_or_model(
    locale: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fragmented = "\n".join(["Re", "su", "me", "AI", "Python"] * 30)
    monkeypatch.setattr(resume_boundary, "_primary_pdf_text", lambda _: (fragmented, 1))
    monkeypatch.setattr(resume_boundary, "_pypdf_text", lambda _: (fragmented, 1))

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("Unreliable PDF must stop before normalization or model selection")

    monkeypatch.setattr("soloscale.local_ui.normalize_text_resume_to_docx", unexpected)
    monkeypatch.setattr("soloscale.local_ui.model_gateway_for", unexpected)
    result = _run_user_resume(
        {
            "job_description": "Build reliable AI workflows.",
            "generation_mode": "deepseek",
            "resume_output_language": locale,
            "approve_resume_processing": "yes",
        },
        {"resume_template": UploadedFile("resume.pdf", "application/pdf", b"%PDF-1.4\n%%EOF")},
        tmp_path / "data",
        tmp_path,
        create_preview=False,
    )
    assert result.return_code != 0
    assert "无法可靠读取此 PDF" in result.stderr
    assert "120" not in result.stderr


@pytest.mark.parametrize("count", [120, 121])
def test_readable_pdf_preserves_real_bullets_and_entry_limit(count: int) -> None:
    bullets = [f"Delivered verified result {i}." for i in range(count)]
    lines = [
        "Example Candidate",
        "AI Engineer",
        "SUMMARY",
        "Reliable engineer.",
        "EXPERIENCE",
        "Example Company",
        *[f"- {bullet}" for bullet in bullets],
    ]
    resume = extract_selected_resume_files([_selected_pdf(_positioned_pdf(lines))])[
        ResumeUploadRole.RESUME
    ]
    assert resume.source_parse_trace is not None
    assert resume.source_parse_trace.fallback_used is False
    profile = extract_candidate_profile(normalize_text_resume_to_docx(resume.text))
    assert profile.experience_bullets == bullets
    if count == 120:
        validate_resume_profile_entry_count(profile)
    else:
        with pytest.raises(ResumeProfileLimitError, match="at most 120.*contains 121"):
            validate_resume_profile_entry_count(profile)


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
