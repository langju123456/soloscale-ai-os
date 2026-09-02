from __future__ import annotations

import hashlib
import io
import subprocess
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path

from soloscale.evidence_hub import EvidenceHub, inspect_git_repository
from soloscale.evidence_ui import ensure_local_project_evidence
from soloscale.knowledge_models import (
    ContentRole,
    NormalizedChunk,
    NormalizedDocument,
    ParsedSource,
    SourceKind,
)
from soloscale.knowledge_store import KnowledgeStore
from soloscale.local_ui import SoloScaleLocalUIHandler


class _CapturingHandler(SoloScaleLocalUIHandler):
    captured_headers: dict[str, str]

    def send_response(self, code: int, message: str | None = None) -> None:
        self.captured_headers["status"] = str(code)

    def send_header(self, keyword: str, value: str) -> None:
        self.captured_headers[keyword] = value

    def end_headers(self) -> None:
        return None


def _seed_knowledge(root: Path) -> None:
    digest = hashlib.sha256(b"private body").hexdigest()
    document = NormalizedDocument(
        id="document-1", source_kind=SourceKind.CODEX_SESSION, external_id="private-thread",
        locator="/private/thread.jsonl", title="Safe title", content_sha256=digest,
        byte_size=12, observed_at=datetime(2026, 8, 13, tzinfo=UTC), metadata={"note": "private"},
    )
    chunk = NormalizedChunk(
        id="chunk-1", document_id=document.id, ordinal=0, role=ContentRole.USER,
        text="private body", text_sha256=digest,
    )
    KnowledgeStore(root).sync([ParsedSource(document=document, chunks=[chunk])])


def _handler(
    data_root: Path, repository_root: Path, path: str, body: bytes = b""
) -> tuple[_CapturingHandler, dict[str, str], io.BytesIO]:
    _CapturingHandler.ui_data_root = data_root
    _CapturingHandler.repo_root = repository_root
    handler = object.__new__(_CapturingHandler)
    headers = Message()
    headers["Content-Length"] = str(len(body))
    handler.path = path
    handler.headers = headers
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.captured_headers = {}
    return handler, handler.captured_headers, handler.wfile


def test_evidence_get_does_not_create_catalog_or_render_private_paths(tmp_path: Path) -> None:
    root = tmp_path / ".soloscale"
    handler, _, output = _handler(root, tmp_path, "/evidence")
    handler.do_GET()
    body = output.getvalue().decode()

    assert not (root / "evidence" / "catalog.sqlite3").exists()
    assert "尚未初始化" in body
    assert str(root) not in body
    assert "/private/" not in body


def test_evidence_refresh_post_redirects_and_renders_metadata_only(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repository_root)], check=True)
    subprocess.run(
        [
            "git", "-C", str(repository_root), "-c", "user.name=Test",
            "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "Initial",
        ],
        check=True,
    )
    root = tmp_path / ".soloscale"
    _seed_knowledge(root)
    handler, response_headers, _ = _handler(root, repository_root, "/evidence/refresh")
    handler.do_POST()
    page_handler, _, output = _handler(root, repository_root, "/evidence?refresh=complete")
    page_handler.do_GET()
    body = output.getvalue().decode()

    assert response_headers == {
        "status": "303",
        "Location": "/evidence?refresh=complete&lang=zh-CN",
        "Content-Length": "0",
    }
    assert "来源" in body and "证据" in body and "Codex 对话" in body
    assert "private body" not in body
    assert "/private/thread.jsonl" not in body


def test_local_project_evidence_preflight_refreshes_only_when_fingerprint_changes(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    subprocess.run(["git", "init", "-q", str(repository_root)], check=True)
    tracked = repository_root / "feature.txt"
    tracked.write_text("first version", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository_root), "add", "feature.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(repository_root), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm",
            "feat: add current project evidence",
        ],
        check=True,
    )
    data_root = tmp_path / "data"

    assert ensure_local_project_evidence(
        data_root,
        repository_root=repository_root,
    ) is True
    assert ensure_local_project_evidence(
        data_root,
        repository_root=repository_root,
    ) is False

    tracked.write_text("second version", encoding="utf-8")
    assert ensure_local_project_evidence(
        data_root,
        repository_root=repository_root,
    ) is True

    current_source, _ = inspect_git_repository(repository_root)
    stored_snapshot = EvidenceHub(data_root).git_repository_snapshot(repository_root)
    assert stored_snapshot is not None
    stored_source, _ = stored_snapshot
    assert stored_source.content_sha256 == current_source.content_sha256
    assert stored_source.metadata["status_sha256"] == current_source.metadata["status_sha256"]
    assert (
        stored_source.metadata["tracked_diff_sha256"]
        == current_source.metadata["tracked_diff_sha256"]
    )
