"""Run a public-safe real localhost smoke without leaving a server process behind."""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from soloscale.resume_docx import read_template_paragraphs


def _listen_socket(port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", port))
    listener.listen()
    listener.set_inheritable(True)
    return listener


def main() -> None:
    requested_port = int(os.environ.get("SOLOSCALE_RESUME_API_PORT", "0"))
    listener = _listen_socket(requested_port)
    port = int(listener.getsockname()[1])
    base_url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [sys.executable, "-m", "soloscale.resume_api"],
        env={**os.environ, "SOLOSCALE_RESUME_API_FD": str(listener.fileno())},
        pass_fds=(listener.fileno(),),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    listener.close()
    try:
        with httpx.Client(timeout=5) as client:
            for _ in range(30):
                if process.poll() is not None:
                    raise RuntimeError("spawned localhost API exited before readiness")
                try:
                    if client.get(f"{base_url}/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("localhost API did not become ready")
            source = (
                "示例候选人\nAI 工程师\n项目经历\n项目 A\n- 构建 Python RAG 检索工作流。\n"
                "技术技能\n- Python, RAG\n工作经历\n示例公司\n- 交付可靠的 AI 功能。\n"
            )
            payload = {
                "resume": {
                    "filename": "resume.txt",
                    "content_base64": base64.b64encode(source.encode()).decode(),
                },
                "job_description": "需要 Python 和 RAG。",
            }
            preview = client.post(f"{base_url}/resume/preview", json=payload)
            tailored = client.post(f"{base_url}/resume/tailor", json=payload)
            invalid = client.post(
                f"{base_url}/resume/preview",
                json={
                    "resume": {"filename": "resume.txt", "content_base64": "%%"},
                    "job_description": "需要 Python。",
                },
            )
        docx = tailored.content
        claims = [item.text for item in read_template_paragraphs(docx) if item.is_bullet]
        receipt = {
            "health_status": 200,
            "preview_status": preview.status_code,
            "tailor_status": tailored.status_code,
            "invalid_status": invalid.status_code,
            "claims_preserved": tailored.headers["x-soloscale-claims-preserved"] == "true",
            "contains_source_claim": "构建 Python RAG 检索工作流。" in claims,
            "model_calls": int(tailored.headers["x-soloscale-model-calls"]),
        }
        if (
            preview.status_code != 200
            or tailored.status_code != 200
            or invalid.status_code != 400
            or not receipt["claims_preserved"]
            or not receipt["contains_source_claim"]
            or receipt["model_calls"] != 0
        ):
            raise RuntimeError("localhost resume API smoke failed")
        Path(".soloscale").mkdir(exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="demo-resume-api-", dir=".soloscale"))
        (run_dir / "tailored-resume.docx").write_bytes(docx)
        (run_dir / "receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(receipt, ensure_ascii=False))
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
