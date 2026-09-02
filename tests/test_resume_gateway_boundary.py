from __future__ import annotations

import json
import re
import zlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from soloscale.knowledge_models import RetrievalHit
from soloscale.local_ui import UploadedFile, _run_user_resume
from soloscale.resume_docx import extract_candidate_profile
from soloscale.resume_gateway_boundary import (
    MAX_RESUME_FILE_BYTES,
    ExtractedResumeUpload,
    ResumeTemplateMetadata,
    ResumeUploadError,
    ResumeUploadRole,
    SelectedResumeFile,
    _pdf_stream_data,
    _pdf_stream_dictionary,
    extract_selected_resume_files,
    normalize_text_resume_to_docx,
    prepare_resume_gateway_payload,
    restore_role_strategy,
    validate_role_strategy_placeholders,
)
from soloscale.resume_models import (
    CandidateProfile,
    GroundedResumeBulletRewrite,
    RoleStrategy,
)


def _selected(
    role: ResumeUploadRole,
    filename: str,
    content: bytes,
) -> SelectedResumeFile:
    return SelectedResumeFile(
        role=role,
        filename=filename,
        content_type="application/octet-stream",
        content=content,
    )


def _plain_resume() -> str:
    return """Lang Ju
AI Engineer
SUMMARY
Evidence-grounded AI engineer.
PROJECTS
SoloScale
- Built grounded RAG and agent workflows.
EDUCATION
M.S. Information Systems
SKILLS
- Python, RAG, agents
EXPERIENCE
Example Company
- Delivered reliable AI features for stakeholders.
"""


def _simple_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    return (
        b"%PDF-1.4\n"
        b"1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj\n"
        b"2 0 obj <</Type /Pages /Count 1 /Kids [3 0 R]>> endobj\n"
        b"3 0 obj <</Type /Page /Parent 2 0 R /Contents 4 0 R>> endobj\n"
        + f"4 0 obj <</Length {len(stream)}>> stream\n".encode()
        + stream
        + b"\nendstream endobj\n%%EOF"
    )


def test_upload_boundary_accepts_allowlist_and_rejects_unsafe_files(
    tmp_path: Path,
) -> None:
    formats = {
        "resume.docx": normalize_text_resume_to_docx(_plain_resume()),
        "resume.pdf": _simple_pdf("Evidence grounded AI engineer"),
        "resume.txt": _plain_resume().encode(),
        "resume.md": _plain_resume().encode(),
    }
    before = list(tmp_path.iterdir())
    for filename, content in formats.items():
        extracted = extract_selected_resume_files(
            [_selected(ResumeUploadRole.RESUME, filename, content)]
        )[ResumeUploadRole.RESUME]
        assert extracted.text
        assert extracted.source_format == Path(filename).suffix[1:]
    assert list(tmp_path.iterdir()) == before

    reordered = _plain_resume().replace(
        "EDUCATION\nM.S. Information Systems\nSKILLS\n- Python, RAG, agents\nEXPERIENCE",
        "EXPERIENCE",
    ) + "EDUCATION\nM.S. Information Systems\nSKILLS\n- Python, RAG, agents\n"
    profile = extract_candidate_profile(normalize_text_resume_to_docx(reordered))
    claims = profile.project_bullets + profile.experience_bullets
    assert claims == [
        "Built grounded RAG and agent workflows.",
        "Delivered reliable AI features for stakeholders.",
    ]
    assert len(claims) == len(set(claims))
    assert profile.skills == ["Python, RAG, agents"]

    unsafe = [
        _selected(ResumeUploadRole.RESUME, "archive.zip", b"PK\x03\x04unsafe"),
        _selected(ResumeUploadRole.RESUME, "database.txt", b"SQLite format 3\x00private"),
        _selected(ResumeUploadRole.RESUME, "program.md", b"MZprivate"),
    ]
    for item in unsafe:
        with pytest.raises(ResumeUploadError):
            extract_selected_resume_files([item])
    with pytest.raises(ResumeUploadError, match="5 MB"):
        extract_selected_resume_files(
            [
                _selected(
                    ResumeUploadRole.RESUME,
                    "resume.txt",
                    b"x" * (MAX_RESUME_FILE_BYTES + 1),
                )
            ]
        )
    with pytest.raises(ResumeUploadError, match="three"):
        extract_selected_resume_files(
            [
                _selected(ResumeUploadRole.RESUME, "resume.txt", b"resume"),
                _selected(ResumeUploadRole.JOB_DESCRIPTION, "jd.txt", b"jd"),
                _selected(ResumeUploadRole.SUPPORT, "support.txt", b"support"),
                _selected(ResumeUploadRole.SUPPORT, "extra.txt", b"extra"),
            ]
        )
    with pytest.raises(ResumeUploadError, match="Password-protected"):
        extract_selected_resume_files(
            [
                _selected(
                    ResumeUploadRole.RESUME,
                    "resume.pdf",
                    b"%PDF-1.4\n/Encrypt 1 0 R\n%%EOF",
                )
            ]
        )
    with pytest.raises(ResumeUploadError, match="image-only PDFs"):
        extract_selected_resume_files(
            [_selected(ResumeUploadRole.RESUME, "resume.pdf", b"%PDF-1.4\n%%EOF")]
        )
    expanded = b"A" * (9 * 1024 * 1024)
    compressed = zlib.compress(expanded)
    compressed_pdf = (
        b"%PDF-1.4\n1 0 obj <</Type /Page>> endobj\n"
        + f"2 0 obj <</Filter /FlateDecode /Length {len(compressed)}>> stream\n".encode()
        + compressed
        + b"\nendstream endobj\n%%EOF"
    )
    with pytest.raises(ResumeUploadError, match="expands beyond"):
        extract_selected_resume_files(
            [_selected(ResumeUploadRole.RESUME, "resume.pdf", compressed_pdf)]
        )


def test_pdf_parser_respects_declared_stream_length_when_data_ends_in_cr() -> None:
    text_stream = b"(Resume evidence) Tj " + (233).to_bytes(4, "big")
    compressed = zlib.compress(text_stream)
    assert compressed.endswith(b"\r")
    pdf = (
        b"%PDF-1.4\n1 0 obj <</Type /Page>> endobj\n"
        + f"2 0 obj <</Length {len(compressed)} ".encode()
        + (b" " * 450)
        + b"/Filter /FlateDecode>> stream\n"
        + compressed
        + b"\nendstream endobj\n%%EOF"
    )
    stream_match = re.search(
        rb"stream\r?\n(.*?)\r?\nendstream",
        pdf,
        re.DOTALL,
    )
    assert stream_match is not None
    dictionary = _pdf_stream_dictionary(pdf, stream_match)
    assert b"/Length" not in pdf[stream_match.start() - 400 : stream_match.start()]
    extracted_stream = _pdf_stream_data(pdf, stream_match, dictionary)

    uploads = extract_selected_resume_files(
        [_selected(ResumeUploadRole.RESUME, "resume.pdf", pdf)]
    )

    assert len(extracted_stream) == len(compressed)
    assert extracted_stream == compressed
    assert uploads[ResumeUploadRole.RESUME].text == "Resume evidence"


def test_pdf_parser_rejects_declared_length_beyond_the_stream_boundary() -> None:
    compressed = zlib.compress(b"(Resume evidence) Tj")
    pdf = (
        b"%PDF-1.4\n1 0 obj <</Type /Page>> endobj\n"
        + f"2 0 obj <</Filter /FlateDecode /Length {len(compressed) + 5}>> stream\n".encode()
        + compressed
        + b"\nendstream endobj\n%%EOF"
    )

    with pytest.raises(ResumeUploadError, match="invalid declared"):
        extract_selected_resume_files(
            [_selected(ResumeUploadRole.RESUME, "resume.pdf", pdf)]
        )


def test_text_docx_normalization_removes_invalid_xml_characters() -> None:
    source = _plain_resume().replace(
        "Evidence-grounded AI engineer.",
        "Evidence\x00 & < \x12grounded AI engineer.",
    )

    profile = extract_candidate_profile(normalize_text_resume_to_docx(source))

    assert profile.summary == "Evidence & < grounded AI engineer."


def test_gateway_payload_removes_identifiers_and_has_strict_metadata_allowlist() -> None:
    profile = CandidateProfile(
        full_name="Lang Ju",
        summary=(
            "Lang Ju · lang@example.com · +1 (415) 555-0199 · "
            "123 Main Street, San Francisco, CA · /Users/ju.l/private/resume.md"
        ),
        skills=["Python, RAG, agents"],
        experience_bullets=[
            "Lang Ju delivered AI workflows; portfolio https://github.com/langju/private."
        ],
        project_bullets=["Built grounded RAG and agent workflows."],
    )
    support = ExtractedResumeUpload(
        role=ResumeUploadRole.SUPPORT,
        source_format="txt",
        content_sha256="a" * 64,
        text="Supported a customer delivery with approved project outcomes.",
        template_metadata=ResumeTemplateMetadata(source_format="txt"),
    )
    prepared = prepare_resume_gateway_payload(
        profile=profile,
        job_description="AI Engineer requiring RAG and reliable agents.",
        tailoring_instructions="Prioritize AI delivery.",
        template_metadata=ResumeTemplateMetadata(
            source_format="docx",
            section_order=["SUMMARY", "WORK EXPERIENCE"],
            heading_names=["SUMMARY", "WORK EXPERIENCE"],
            style_ids=["Heading1", "LangJuPrivate"],
            font_families=["Aptos", "Lang Ju"],
            colors=["223344"],
        ),
        support_upload=support,
        request_id="resume-request-" + "b" * 24,
    )
    serialized = prepared.payload.model_dump_json()
    for private_value in (
        "Lang Ju",
        "lang@example.com",
        "+1 (415) 555-0199",
        "123 Main Street",
        "/Users/ju.l",
        "https://github.com/langju/private",
    ):
        assert private_value not in serialized
    assert "__SS_PRIVATE_" in serialized
    assert prepared.payload.privacy.zero_data_retention is True
    assert prepared.payload.privacy.disallow_prompt_training is True
    assert prepared.payload.privacy.provider_allowlist == ["zai"]
    assert prepared.payload.template_metadata.style_ids == ["Heading1"]
    assert prepared.payload.template_metadata.font_families == ["Aptos"]
    assert set(prepared.payload.model_dump()) == {
        "schema_version",
        "feature_type",
        "request_id",
        "job_description",
        "tailoring_instructions",
        "positioning_brief",
        "composition_evidence_plan",
        "output_locale",
        "candidate_profile",
        "support_context",
        "template_metadata",
        "privacy",
        "output_contract",
    }
    assert prepared.payload.output_locale == "en-US"
    for forbidden_key in (
        "filename",
        "local_path",
        "author",
        "revision_history",
        "chatgpt",
        "codex",
        "evidence_index",
        "project_file",
    ):
        assert forbidden_key not in serialized.casefold()
    with pytest.raises(ValidationError):
        ResumeTemplateMetadata.model_validate(
            {"source_format": "docx", "author": "private author"}
        )

    sanitized_entry = prepared.payload.candidate_profile.entries[0]
    fact_ids_by_source: dict[str, list[str]] = {}
    for fact in prepared.payload.candidate_profile.atomic_facts:
        fact_ids_by_source.setdefault(fact.profile_entry_id, []).append(fact.fact_id)
    strategy = RoleStrategy(
        role_summary="Grounded AI role.",
        top_hiring_signals=["RAG"],
        evidence_priority=["PROFILE-01", "PROFILE-02"],
        skill_priority=["Python, RAG, agents"],
        bullet_rewrites=[
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-01",
                text=sanitized_entry.text,
                source_fact_ids=fact_ids_by_source["PROFILE-01"],
            ),
            GroundedResumeBulletRewrite(
                profile_entry_id="PROFILE-02",
                text="Built grounded RAG and agent workflows.",
                source_fact_ids=fact_ids_by_source["PROFILE-02"],
            ),
        ],
        rewrite_guidance="Prioritize grounded AI delivery.",
    )
    validate_role_strategy_placeholders(strategy, prepared)
    restored = restore_role_strategy(strategy, prepared.private_replacements)
    assert "Lang Ju" in restored.bullet_rewrites[0].text
    assert "https://github.com/langju/private" in restored.bullet_rewrites[0].text
    malformed_payload = strategy.model_dump(mode="json")
    malformed_payload["bullet_rewrites"][0]["text"] += (
        " __SS_PRIVATE_UNKNOWN_99__"
    )
    with pytest.raises(ResumeUploadError, match="unknown private placeholder"):
        validate_role_strategy_placeholders(
            RoleStrategy.model_validate(malformed_payload), prepared
        )


def test_payload_and_event_artifacts_never_contain_raw_upload_bodies(tmp_path: Path) -> None:
    from soloscale.resume_gateway_boundary import (  # local import keeps the contract explicit
        ResumeFunnelEventType,
        record_resume_funnel_event,
    )

    target = record_resume_funnel_event(
        tmp_path,
        ResumeFunnelEventType.GENERATION_COMPLETED,
        run_id="resume-20260820T000000Z-abcdef1234",
    )
    payload = json.loads(target.read_text())
    assert set(payload) == {
        "schema_version",
        "event_id",
        "event_type",
        "occurred_at",
        "run_id",
    }
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert "resume body" not in target.read_text().casefold()


def test_txt_resume_and_uploaded_jd_complete_offline_without_raw_support_retention(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class EmptyStore:
        def __init__(self, root: Path) -> None:
            self.root = root

        def search(self, query: str, limit: int) -> list[RetrievalHit]:
            del query, limit
            return []

    monkeypatch.setattr("soloscale.local_ui.KnowledgeStore", EmptyStore)
    monkeypatch.setattr(
        "soloscale.local_ui._create_resume_pdf_preview", lambda source, target: False
    )
    data_root = tmp_path / "data"
    support_secret = "SUPPORT-ONLY-RAW-BODY-9917"
    jd_secret = "JD-ONLY-RAW-BODY-4481"
    application_root = tmp_path / "Resume Applications"
    result = _run_user_resume(
        {
            "job_description": "",
            "generation_mode": "template",
            "approve_resume_processing": "yes",
            "retention_mode": "private_persistent",
            "resume_library_root": str(application_root),
        },
        {
            "resume_template": UploadedFile(
                filename="resume.txt",
                content_type="text/plain",
                content=_plain_resume().encode(),
            ),
            "job_description_file": UploadedFile(
                filename="job.md",
                content_type="text/markdown",
                content=(
                    "# AI Engineer\nRequired: Python, RAG, and reliable agents.\n"
                    f"Unmatched requirement: quantum lithography {jd_secret}"
                ).encode(),
            ),
            "support_document": UploadedFile(
                filename="support.txt",
                content_type="text/plain",
                content=support_secret.encode(),
            ),
        },
        data_root,
        tmp_path / "repo",
    )
    assert result.return_code == 0, result.stderr
    run_dir = Path(result.stdout.removeprefix("Resume workspace: "))
    assert (run_dir / "08_resume.docx").is_file()
    metadata = json.loads((run_dir / "09_user_ui.json").read_text())
    assert metadata["template_source_format"] == "txt"
    assert metadata["network_used"] is False
    assert metadata["retention"] == "request_scoped_sources_not_persisted"
    assert "template_filename" not in metadata
    assert not (run_dir / "00_input.json").exists()
    assert not application_root.exists()
    retained_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in run_dir.iterdir()
        if path.suffix in {".json", ".md"}
    )
    assert support_secret not in retained_text
    assert jd_secret not in retained_text
    receipt = json.loads((run_dir / "application_receipt.json").read_text())
    assert receipt["source_inputs_retained"] is False
    assert receipt["source_input_lifetime"] == "request_only"
    assert not (run_dir / "11_role_strategy.json").exists()
    event_files = list((data_root / "analytics" / "resume-funnel").glob("*.json"))
    event_types = {json.loads(path.read_text())["event_type"] for path in event_files}
    assert {
        "resume_upload_started",
        "resume_upload_completed",
        "jd_supplied",
        "generation_started",
        "generation_completed",
    }.issubset(event_types)
    assert all(support_secret not in path.read_text() for path in event_files)
    assert all("resume.txt" not in path.read_text() for path in event_files)
