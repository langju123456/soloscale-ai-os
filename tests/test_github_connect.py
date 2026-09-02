from __future__ import annotations

import json
import subprocess
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from soloscale.evidence_hub import EvidenceHub
from soloscale.github_connect import GitHubConnectionStore, GitHubReadOnlyClient
from soloscale.local_ui import UploadedFile, _run_user_resume
from soloscale.resume_docx import tailor_resume_docx
from soloscale.resume_evidence_pack import build_candidate_evidence_pack
from soloscale.resume_models import CandidateEvidencePack, CandidateProfile
from soloscale.work_ui import load_work_context, work_page


def test_github_read_only_selection_evidence_and_resume_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = "synthetic-github-token-never-persist"
    account = {"id": 42, "login": "operator"}
    repository = {
        "id": 101,
        "full_name": "operator/solo-scale-ai-os",
        "private": True,
        "default_branch": "main",
        "html_url": "https://github.com/operator/solo-scale-ai-os",
        "updated_at": "2026-08-27T01:00:00Z",
    }
    observed_urls: list[str] = []

    def transport(url: str) -> tuple[int, dict[str, str], bytes]:
        observed_urls.append(url)
        parsed = urllib.parse.urlsplit(url)
        payload: object
        if parsed.path == "/user":
            payload = account
        elif parsed.path == "/user/repos":
            payload = [repository]
        elif parsed.path == "/repos/operator/solo-scale-ai-os":
            payload = repository
        elif parsed.path == "/repos/operator/solo-scale-ai-os/commits":
            start = datetime(2026, 8, 27, 0, 0, tzinfo=UTC)
            payload = [
                {
                    "sha": f"{index:040x}",
                    "commit": {
                        "message": (
                            (
                                "fix: src/private_payload.py diff --git"
                                if index == 3
                                else f"feat: verified committed capability {index}"
                            )
                            + "\nprivate body omitted"
                        ),
                        "committer": {
                            "date": (start - timedelta(minutes=index)).isoformat()
                        },
                    },
                }
                for index in range(1, 14)
            ]
        elif parsed.path in {
            "/repos/operator/solo-scale-ai-os/pulls",
            "/repos/operator/solo-scale-ai-os/issues",
        }:
            payload = []
        elif parsed.path == "/repos/operator/solo-scale-ai-os/actions/runs":
            payload = {"workflow_runs": []}
        else:
            raise AssertionError(f"unexpected GitHub path: {parsed.path}")
        return 200, {}, json.dumps(payload).encode()

    client = GitHubReadOnlyClient(token, transport=transport)
    account_id, login, repositories = client.discover()
    data_root = tmp_path / "data"
    store = GitHubConnectionStore(data_root)
    store.save_inventory(
        account_id=account_id,
        account_login=login,
        repositories=repositories,
    )
    state = store.save_selection([101])
    source, items = client.evidence_snapshot(
        account_id=state.account_id,
        account_login=state.account_login,
        repositories=state.selected_repositories,
    )
    receipt = EvidenceHub(data_root).sync_source(source, items=items)
    store.mark_evidence_refresh(receipt_id=receipt.receipt_id)

    local_repository = tmp_path / "local-project"
    local_repository.mkdir()
    subprocess.run(["git", "init", "-q", str(local_repository)], check=True)
    (local_repository / "feature.txt").write_text("safe", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(local_repository), "add", "feature.txt"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(local_repository),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "feat: local committed summary",
        ],
        check=True,
    )
    EvidenceHub(data_root).sync_git_repository(local_repository)
    pack = build_candidate_evidence_pack(
        CandidateProfile(
            project_bullets=[
                (
                    "Built an evidence-grounded workflow platform that turns project "
                    "evidence into retrieval-backed resume workflows."
                )
            ]
        ),
        data_root=data_root,
        repository_root=local_repository,
    )
    remote_only_pack = build_candidate_evidence_pack(
        CandidateProfile(
            project_bullets=[
                (
                    "Built an evidence-grounded workflow platform that turns project "
                    "evidence into retrieval-backed resume workflows."
                )
            ]
        ),
        data_root=data_root,
    )

    github_facts = [
        fact
        for fact in pack.atomic_facts
        if fact.evidence_id == "EVIDENCE-GITHUB-COMMITS"
    ]
    assert len(github_facts) == 11
    assert all(
        fact.text.startswith("Committed repository summary: feat:")
        for fact in github_facts
    )
    assert all("private body omitted" not in fact.text for fact in github_facts)
    assert {source.evidence_id for source in pack.sources} >= {
        "EVIDENCE-LOCAL-GIT",
        "EVIDENCE-GITHUB-COMMITS",
    }
    assert any(
        source.evidence_id == "EVIDENCE-GITHUB-COMMITS"
        for source in remote_only_pack.sources
    )

    desktop_facts: list[str] = []

    def capture_candidate_pack(
        template: bytes,
        job_description: str,
        **kwargs: object,
    ):
        candidate_pack = cast(
            CandidateEvidencePack, kwargs["candidate_evidence_pack"]
        )
        desktop_facts.extend(
            fact.text
            for fact in candidate_pack.atomic_facts
            if fact.evidence_id == "EVIDENCE-GITHUB-COMMITS"
        )
        return tailor_resume_docx(template, job_description)

    monkeypatch.setattr(
        "soloscale.local_ui.tailor_resume_docx_with_gateway",
        capture_candidate_pack,
    )
    resume_result = _run_user_resume(
        {
            "job_description": "Required: Python, RAG, and agentic workflows",
            "generation_mode": "openai_compatible",
            "approve_resume_processing": "yes",
        },
        {
            "resume_template": UploadedFile(
                filename="resume.txt",
                content_type="text/plain",
                content=(
                    b"LANG JU\nAI Engineer\nlang@example.com\nSUMMARY\n"
                    b"Evidence-grounded engineer.\nPROJECT HIGHLIGHTS\nSoloScale AI OS\n"
                    b"- Built SoloScale AI OS with evidence-grounded workflows.\n"
                    b"EDUCATION\nM.S. Information Systems\nTECHNICAL SKILLS\nPython, RAG\n"
                    b"WORK EXPERIENCE\nExample Company\n- Delivered production systems.\n"
                ),
            )
        },
        data_root,
        tmp_path,
        gateway=object(),  # type: ignore[arg-type]
    )
    assert resume_result.return_code == 0, resume_result.stderr
    assert len(desktop_facts) == 11
    assert all("private body omitted" not in fact for fact in desktop_facts)
    persisted = store.path.read_text(encoding="utf-8")
    assert token not in persisted
    database_bytes = EvidenceHub(data_root).database_path.read_bytes()
    assert token.encode() not in database_bytes
    assert b"src/private_payload.py" not in database_bytes
    assert all(url.startswith("https://api.github.com/") for url in observed_urls)

    snapshot = load_work_context(
        data_root,
        workspace_root=local_repository,
        github_connected=True,
    )
    page = work_page(
        data_root=data_root,
        workspace_root=local_repository,
        desktop_mode=True,
        github_token_configured=True,
        github_connect_available=True,
    )
    assert snapshot.github_state == "READY"
    assert snapshot.github_authorization_state == "READY"
    assert snapshot.github_freshness_state == "READY"
    assert snapshot.github_selected_repositories == 1
    assert "operator · 已选择 1 个仓库" in page
    assert 'href="/work/github?lang=zh-CN"' in page
    assert 'action="/work/github/refresh"' in page
    assert token not in page
