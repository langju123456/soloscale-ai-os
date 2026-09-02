from __future__ import annotations

import pytest

from soloscale.local_ui import FormSubmission, UploadedFile, _prepare_resume_template_preview
from soloscale.resume_gateway_boundary import ResumeUploadError
from soloscale.resume_template_intake import (
    ResumeTemplateSourceType,
    inspect_template_file,
    inspect_template_html,
    inspect_template_url,
    template_metadata_from_receipt,
)


def test_html_template_retains_structure_but_never_candidate_copy() -> None:
    html = """
    <html><body>
      <h1>Senior Engineer at Example Template Company</h1>
      <h2>Summary</h2><p>Led a $10M platform for millions of users.</p>
      <h2>Projects</h2><p>Built an invented system.</p>
      <script><h2>Work Experience</h2></script>
      <h2>Skills</h2>
    </body></html>
    """
    receipt = inspect_template_html(html)

    assert receipt.source_type is ResumeTemplateSourceType.HTML
    assert receipt.detected_sections == [
        "SUMMARY",
        "PROJECT HIGHLIGHTS",
        "TECHNICAL SKILLS",
    ]
    assert receipt.source_text_retained is False
    assert receipt.candidate_facts_imported is False
    serialized = receipt.model_dump_json()
    assert "$10M" not in serialized
    assert "millions of users" not in serialized
    assert "invented system" not in serialized
    assert "WORK EXPERIENCE" not in receipt.detected_sections
    metadata = template_metadata_from_receipt(receipt)
    assert metadata.section_order == receipt.detected_sections


def test_file_template_reads_only_detected_structure() -> None:
    receipt = inspect_template_file(
        "layout.md",
        (
            b"SUMMARY\nplaceholder copy\nPROJECTS\nplaceholder\n"
            b"EDUCATION\nplaceholder\nSKILLS\nplaceholder\n"
        ),
    )
    assert receipt.source_type is ResumeTemplateSourceType.FILE
    assert receipt.source_format == "md"
    assert receipt.detected_sections == [
        "SUMMARY",
        "PROJECT HIGHLIGHTS",
        "EDUCATION",
        "TECHNICAL SKILLS",
    ]
    assert "placeholder copy" not in receipt.model_dump_json()


def test_template_preview_requires_exactly_one_explicit_source() -> None:
    upload = UploadedFile(
        filename="layout.html",
        content_type="text/html",
        content=b"<h2>Summary</h2><h2>Skills</h2>",
    )
    with pytest.raises(ResumeUploadError, match="exactly one"):
        _prepare_resume_template_preview(
            FormSubmission(
                fields={"layout_template_html": "<h2>Summary</h2>"},
                files={"layout_template_file": upload},
            )
        )
    with pytest.raises(ResumeUploadError, match="exactly one"):
        _prepare_resume_template_preview(FormSubmission(fields={}, files={}))


def test_url_template_rejects_private_network_without_fetching() -> None:
    with pytest.raises(ResumeUploadError, match="public host"):
        inspect_template_url("http://127.0.0.1/template")


def test_url_template_reads_public_html_and_drops_query_from_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Headers:
        @staticmethod
        def get_content_type() -> str:
            return "text/html"

    class Response:
        headers = Headers()

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def read(_limit: int) -> bytes:
            return b"<h2>Summary</h2><p>private body</p><h2>Skills</h2>"

        @staticmethod
        def geturl() -> str:
            return "https://example.com/layout?private=query"

    class Opener:
        @staticmethod
        def open(_request: object, *, timeout: int) -> Response:
            assert timeout == 8
            return Response()

    monkeypatch.setattr(
        "soloscale.resume_template_intake.socket.getaddrinfo",
        lambda *_args: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(
        "soloscale.resume_template_intake.urllib.request.build_opener",
        lambda *_args: Opener(),
    )

    receipt = inspect_template_url("https://example.com/layout?token=secret")
    assert receipt.source_type is ResumeTemplateSourceType.URL
    assert receipt.source_url == "https://example.com/layout"
    assert receipt.detected_sections == ["SUMMARY", "TECHNICAL SKILLS"]
    assert "private body" not in receipt.model_dump_json()
