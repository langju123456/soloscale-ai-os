from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from soloscale.resume_api import API_BODY_LIMIT, create_app
from soloscale.resume_docx import read_template_paragraphs
from soloscale.resume_gateway_boundary import MAX_RESUME_FILE_BYTES, normalize_text_resume_to_docx


def _resume_text() -> str:
    return """张三
AI 工程师
个人简介
证据驱动的 AI 工程师。
项目经历
项目 A
- 构建 Python RAG 检索工作流。
项目 B
- 完成 Docker 和 Kubernetes 自动化。
技术技能
- Python, RAG
- Docker, Kubernetes
工作经历
示例公司
- 为利益相关方交付可靠的 AI 功能。
"""


def _payload(*, filename: str = "resume.txt", content: bytes | None = None) -> dict[str, object]:
    return {
        "resume": {
            "filename": filename,
            "content_base64": base64.b64encode(content or _resume_text().encode()).decode(),
        },
        "job_description": "需要 Docker、Kubernetes 与 Python 交付经验。",
    }


def _simple_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    return (
        b"%PDF-1.4\n1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj\n"
        b"2 0 obj <</Type /Pages /Count 1 /Kids [3 0 R]>> endobj\n"
        b"3 0 obj <</Type /Page /Parent 2 0 R /Contents 4 0 R>> endobj\n"
        + f"4 0 obj <</Length {len(stream)}>> stream\n".encode()
        + stream
        + b"\nendstream endobj\n%%EOF"
    )


def test_preview_and_template_tailor_preserve_chinese_claims() -> None:
    client = TestClient(create_app())
    preview = client.post("/resume/preview", json=_payload())
    assert preview.status_code == 200
    assert preview.json()["source"]["format"] == "txt"
    assert preview.json()["profile"]["project_bullet_count"] == 2
    assert preview.json()["model_calls"] == 0

    tailored = client.post("/resume/tailor", json=_payload())
    assert tailored.status_code == 200
    assert tailored.headers["content-disposition"] == 'attachment; filename="tailored-resume.docx"'
    assert tailored.headers["x-soloscale-mode"] == "template-only"
    assert tailored.headers["x-soloscale-model-calls"] == "0"
    assert tailored.headers["x-soloscale-claims-preserved"] == "true"
    docx = tailored.content
    text = [paragraph.text for paragraph in read_template_paragraphs(docx)]
    assert "构建 Python RAG 检索工作流。" in text
    assert "完成 Docker 和 Kubernetes 自动化。" in text
    assert text.index("项目 B") < text.index("项目 A")


def test_preview_uses_canonical_pdf_parser() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/resume/preview",
        json=_payload(filename="resume.pdf", content=_simple_pdf("Resume Python RAG")),
    )
    assert response.status_code == 200
    assert response.json()["source"]["format"] == "pdf"


def test_docx_input_returns_an_attachment_with_its_original_claim() -> None:
    client = TestClient(create_app())
    source = normalize_text_resume_to_docx(_resume_text())
    response = client.post("/resume/tailor", json=_payload(filename="resume.docx", content=source))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    output_text = [item.text for item in read_template_paragraphs(response.content)]
    assert "构建 Python RAG 检索工作流。" in output_text


def test_rejects_bad_inputs_and_bounded_streamed_body() -> None:
    client = TestClient(create_app())
    malformed = client.post(
        "/resume/preview",
        json={
            "resume": {"filename": "resume.txt", "content_base64": "%%"},
            "job_description": "JD",
        },
    )
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "INVALID_BASE64"

    unsupported = client.post(
        "/resume/preview", json=_payload(filename="resume.exe", content=b"bad")
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "INVALID_RESUME"

    chunks = [b"{" + b"x" * (API_BODY_LIMIT + 1) + b"}"]
    oversized = client.post(
        "/resume/preview",
        content=iter(chunks),
        headers={"content-type": "application/json"},
    )
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "REQUEST_TOO_LARGE"


def test_accepts_declared_maximum_chinese_job_description() -> None:
    client = TestClient(create_app())
    payload = _payload()
    payload["job_description"] = "需" * 50_000
    response = client.post("/resume/preview", json=payload)
    assert response.status_code == 200


def test_joint_maximum_fields_with_escaped_non_bmp_text_reach_resume_validation() -> None:
    payload = _payload(filename="😀" * 251 + ".txt", content=b" " * MAX_RESUME_FILE_BYTES)
    payload["job_description"] = "😀" * 50_000
    response = TestClient(create_app()).post(
        "/resume/preview",
        content=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    # The intentionally blank resume must reach canonical validation, not the body cap.
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_RESUME"


def test_demo_rejects_an_occupied_requested_port_without_a_receipt(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        environment = {
            **os.environ,
            "SOLOSCALE_RESUME_API_PORT": str(port),
        }
        result = subprocess.run(
            [sys.executable, Path(__file__).parents[1] / "scripts" / "demo_resume_api.py"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    assert result.returncode != 0
    assert not (tmp_path / ".soloscale").exists()
