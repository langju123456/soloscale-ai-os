from __future__ import annotations

import hashlib
import json
import re
import stat
import subprocess
import time
from pathlib import Path

from soloscale.evidence_hub import EvidenceHub
from soloscale.ui_shell import SourceState, render_source_state
from soloscale.work_ui import (
    CodexImportJobManager,
    CodexImportJobSnapshot,
    import_chatgpt_export,
    import_codex_history,
    load_work_context,
    preflight_work_sources,
    refresh_work_source,
    work_page,
)


def _wait_for_codex_job(
    manager: CodexImportJobManager,
    data_root: Path,
    job_id: str,
) -> CodexImportJobSnapshot:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = manager.get(data_root, job_id)
        if snapshot is not None and snapshot.phase in {"READY", "FAILED"}:
            return snapshot
        time.sleep(0.01)
    raise AssertionError("Codex import job did not finish")


def _chatgpt_export(private_text: str) -> list[object]:
    return [
        {
            "id": "conversation-work-1",
            "title": "Private work conversation",
            "current_node": "assistant",
            "mapping": {
                "root": {
                    "id": "root",
                    "parent": None,
                    "children": ["user"],
                    "message": None,
                },
                "user": {
                    "id": "user",
                    "parent": "root",
                    "children": ["assistant"],
                    "message": {
                        "id": "user-message",
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": [private_text]},
                    },
                },
                "assistant": {
                    "id": "assistant",
                    "parent": "user",
                    "children": [],
                    "message": {
                        "id": "assistant-message",
                        "author": {"role": "assistant"},
                        "content": {
                            "content_type": "text",
                            "parts": ["Use a bounded evidence contract."],
                        },
                    },
                },
            },
        }
    ]


def _codex_session(private_text: str) -> str:
    records = [
        {
            "timestamp": "2026-08-16T01:00:00Z",
            "type": "session_meta",
            "payload": {"id": "work-session-1"},
        },
        {
            "timestamp": "2026-08-16T01:01:00Z",
            "type": "response_item",
            "payload": {
                "id": "work-message-1",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": private_text}],
            },
        },
    ]
    return "".join(json.dumps(record) + "\n" for record in records)


def test_work_page_is_read_only_and_hides_private_implementation_details(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "private-data"

    page = work_page(
        data_root=data_root,
        workspace_root=None,
        desktop_mode=True,
    )

    assert not data_root.exists()
    assert "你的工作资料" in page
    assert "选择 SoloScale 可以使用的资料。以后随时可以修改。" in page
    assert "只读取你明确选择的资料。本地优先，不扫描整台 Mac。" in page
    assert "了解数据边界" in page
    assert "已连接" in page
    assert "添加更多" in page
    assert "暂不可用" in page
    assert 'data-source-state="NOT_CONNECTED"' in page
    assert 'data-source-state="UNAVAILABLE"' in page
    assert 'id="work-processing"' in page
    assert 'data-source-state="PROCESSING"' in page
    assert "soloscale://choose-work-repository" in page
    assert "soloscale://choose-chatgpt-export" in page
    assert "完成并继续" in page
    assert 'href="/?lang=zh-CN"' in page
    assert "EvidenceHub" not in page
    assert "FTS" not in page
    assert "provenance_locator" not in page
    assert str(tmp_path) not in page


def test_chatgpt_import_is_explicit_source_preserving_and_body_free_in_ui(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    private_text = "PRIVATE INTERVIEW PROJECT DETAIL"
    export = tmp_path / "conversations.json"
    export.write_text(json.dumps(_chatgpt_export(private_text)), encoding="utf-8")
    before = hashlib.sha256(export.read_bytes()).hexdigest()

    result = import_chatgpt_export(data_root, export)
    snapshot = load_work_context(data_root)
    page = work_page(
        data_root=data_root,
        workspace_root=None,
        desktop_mode=False,
    )

    assert result.imported == 1
    assert result.trace_id is not None
    assert result.trace_id.startswith("work-import-")
    assert snapshot.chatgpt_exports == 1
    assert snapshot.knowledge_documents == 1
    assert hashlib.sha256(export.read_bytes()).hexdigest() == before
    assert private_text not in page
    assert export.name not in page
    assert str(tmp_path) not in page
    assert 'data-source="chatgpt-export"' in page
    assert 'data-source-state="READY"' in page
    assert "已导入 1 个对话" in page


def test_codex_import_and_selected_git_project_reuse_existing_intake(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    private_text = "PRIVATE CODEX IMPLEMENTATION NOTE"
    codex_home = tmp_path / "home" / ".codex"
    session = codex_home / "sessions" / "2026" / "session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(_codex_session(private_text), encoding="utf-8")
    before = session.read_bytes()
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "feature.txt").write_text("verified feature", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "feature.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "feat: add verified local project evidence",
        ],
        check=True,
    )

    result = import_codex_history(data_root, codex_home=codex_home)
    snapshot = load_work_context(
        data_root,
        workspace_root=project,
        home=tmp_path / "home",
    )

    assert result.imported == 1
    assert snapshot.codex_sessions == 1
    assert snapshot.project_connected is True
    assert snapshot.project_state == "STALE"
    assert snapshot.codex_folder_available is True
    assert session.read_bytes() == before

    page = work_page(
        data_root=data_root,
        workspace_root=project,
        desktop_mode=True,
        notice="本地 Git 项目已连接。",
    )
    assert "本地 Git 项目已连接" in page
    assert "project · 工程证据已过期" in page
    assert "更换项目" in page
    assert "刷新工程证据" in page
    assert 'data-processing-source="Local Git"' in page
    assert "正在准备本地项目快照" in page
    assert 'data-processing-source="Codex"' in page
    assert "正在建立本地索引" in page
    assert "不保存或上传项目源码" in page
    assert 'action="/work/refresh"' in page
    assert str(project) not in page

    EvidenceHub(data_root).sync_git_repository(project)
    ready = load_work_context(data_root, workspace_root=project)
    assert ready.project_state == "READY"
    assert ready.project_fact_count == 1
    assert ready.project_new_commits == 0


def test_codex_background_import_is_incremental_and_isolates_session_failures(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    codex_home = tmp_path / "home" / ".codex"
    sessions = codex_home / "sessions" / "2026"
    sessions.mkdir(parents=True)
    private_text = "PRIVATE BACKGROUND IMPORT DETAIL"
    good = sessions / "good.jsonl"
    good.write_text(_codex_session(private_text), encoding="utf-8")
    bad = sessions / "bad.jsonl"
    bad.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-16T01:00:00Z",
                "type": "session_meta",
                "payload": {"id": "broken-work-session"},
            }
        )
        + "\nnot-json\n",
        encoding="utf-8",
    )
    before = good.read_bytes()
    manager = CodexImportJobManager(parse_workers=2)
    try:
        first_job_id = manager.submit(data_root=data_root, codex_home=codex_home)
        assert first_job_id.startswith("codex-import-")
        first = _wait_for_codex_job(manager, data_root, first_job_id)

        assert first.phase == "READY"
        assert first.total == 2
        assert first.processed == 2
        assert first.running == 0
        assert first.imported == 1
        assert first.failed == 1
        assert "SESSION_PARSE_FAILED" in first.failure_codes

        second_job_id = manager.submit(data_root=data_root, codex_home=codex_home)
        assert second_job_id != first_job_id
        second = _wait_for_codex_job(manager, data_root, second_job_id)

        assert second.phase == "READY"
        assert second.total == 2
        assert second.processed == 2
        assert second.running == 0
        assert second.imported == 0
        assert second.updated == 0
        assert second.skipped == 1
        assert second.failed == 1
        assert "SESSION_PARSE_FAILED" in second.failure_codes
    finally:
        manager.shutdown()

    assert good.read_bytes() == before
    inventory = data_root / "knowledge" / "codex-source-inventory.json"
    first_receipt = (
        data_root / "knowledge" / "import-jobs" / f"{first_job_id}.json"
    )
    persisted_metadata = inventory.read_text() + first_receipt.read_text()
    assert private_text not in persisted_metadata
    assert str(codex_home) not in persisted_metadata
    assert stat.S_IMODE(inventory.stat().st_mode) == 0o600
    assert stat.S_IMODE(first_receipt.stat().st_mode) == 0o600
    restarted_manager = CodexImportJobManager(parse_workers=1)
    try:
        recovered = restarted_manager.get(data_root, first_job_id)
        assert recovered is not None
        assert recovered.phase == "READY"
        assert recovered.processed == first.processed
        assert recovered.failed == first.failed
    finally:
        restarted_manager.shutdown()


def test_work_page_polls_body_free_codex_background_progress(tmp_path: Path) -> None:
    job = CodexImportJobSnapshot(
        job_id="codex-import-20260828T120000Z-0123456789",
        phase="PROCESSING",
        total=12,
        processed=5,
        running=2,
        imported=3,
        updated=1,
        skipped=1,
        failed=0,
        failure_codes=(),
        created_at="2026-08-28T12:00:00+00:00",
        updated_at="2026-08-28T12:00:01+00:00",
    )

    page = work_page(
        data_root=tmp_path / "data",
        workspace_root=None,
        desktop_mode=True,
        codex_job=job,
    )

    assert 'data-source="codex"' in page
    assert 'data-source-state="PROCESSING"' in page
    assert "已处理 5/12" in page
    assert 'data-job-id="codex-import-20260828T120000Z-0123456789"' in page
    assert "/work/import-codex/jobs/${jobId}" in page
    assert "后台增量处理" in page

    failed_page = work_page(
        data_root=tmp_path / "data",
        workspace_root=None,
        desktop_mode=True,
        codex_job=CodexImportJobSnapshot(
            **{
                **job.__dict__,
                "phase": "FAILED",
                "processed": 12,
                "running": 0,
                "failed": 2,
            }
        ),
    )
    assert 'data-source-state="NEEDS_ATTENTION"' in failed_page
    assert "处理未完成" in failed_page


def test_work_page_english_copy_actions_and_states_are_truthful(tmp_path: Path) -> None:
    page = work_page(
        data_root=tmp_path / "data",
        workspace_root=None,
        locale="en",
        desktop_mode=True,
        chatgpt_export_selected=True,
    )

    assert "Your Work Context" in page
    assert "Choose the work sources SoloScale can use. You can change them anytime." in page
    assert "Only work you explicitly choose is read. Local first; no whole-Mac scanning." in page
    assert "Connected" in page
    assert "Add More" in page
    assert "Not Available Yet" in page
    assert "File selected; waiting for your import approval" in page
    assert 'action="/work/import-chatgpt"' in page
    assert "Continue" in page
    assert "Connect GitHub" not in page


def test_shared_source_states_have_stable_semantics() -> None:
    expected: dict[SourceState, str] = {
        "READY": "✓",
        "STALE": "!",
        "PROCESSING": "●",
        "AVAILABLE": "＋",
        "NOT_CONNECTED": "○",
        "UNAVAILABLE": "—",
        "NEEDS_ATTENTION": "!",
    }

    for state, symbol in expected.items():
        rendered = render_source_state(state, "en")
        assert f'data-source-state="{state}"' in rendered
        assert symbol in rendered


def test_work_source_preflight_separates_authorization_freshness_and_trace(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"

    preflight = preflight_work_sources(data_root)
    local_git = preflight.by_kind("local_git")
    github = preflight.by_kind("github")

    assert local_git.authorization_state == "NOT_CONNECTED"
    assert local_git.freshness_state == "UNAVAILABLE"
    assert local_git.action_required is False
    assert github.authorization_state == "NOT_CONNECTED"
    assert github.freshness_state == "UNAVAILABLE"
    assert github.action_required is False
    assert preflight.action_required is False
    assert preflight.trace_id.startswith("work-preflight-")
    assert preflight.preflighted_at.endswith("+00:00")

    snapshot = load_work_context(data_root)
    assert snapshot.project_authorization_state == "NOT_CONNECTED"
    assert snapshot.project_freshness_state == "UNAVAILABLE"
    assert snapshot.project_state == "NOT_CONNECTED"
    assert snapshot.github_authorization_state == "NOT_CONNECTED"
    assert snapshot.github_freshness_state == "UNAVAILABLE"
    assert snapshot.github_state == "NOT_CONNECTED"
    assert snapshot.preflight_trace_id.startswith("work-preflight-")
    assert snapshot.preflight_at.endswith("+00:00")


def test_stale_source_auto_preflight_incremental_refresh_and_continue(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "feature.txt").write_text("verified feature", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "feature.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "feat: add verified local project evidence",
        ],
        check=True,
    )

    preflight = preflight_work_sources(data_root, workspace_root=project)
    local_git = preflight.by_kind("local_git")
    assert local_git.authorization_state == "READY"
    assert local_git.freshness_state == "STALE"
    assert local_git.action_required is True
    assert preflight.action_required is True

    page = work_page(
        data_root=data_root,
        workspace_root=project,
        desktop_mode=True,
        return_path="/resume",
    )
    assert "已自动预检" in page
    assert 'data-preflight-trace-id="work-preflight-' in page
    assert 'data-preflight-at="' in page
    assert 'data-source-kind="local-git"' in page
    assert 'data-authorization-state="READY"' in page
    assert 'data-freshness-state="STALE"' in page
    assert 'href="/resume?lang=zh-CN"' in page

    result = refresh_work_source(data_root, "local_git", workspace_root=project)
    assert result.trace_id is not None
    assert result.trace_id.startswith("work-refresh-")

    refreshed = preflight_work_sources(data_root, workspace_root=project)
    assert refreshed.by_kind("local_git").freshness_state == "READY"
    assert refreshed.by_kind("local_git").action_required is False
    assert refreshed.action_required is False
    ready_page = work_page(
        data_root=data_root,
        workspace_root=project,
        desktop_mode=True,
    )
    assert "已自动预检" not in ready_page


def test_resume_library_row_no_longer_loops_back_to_the_generator(
    tmp_path: Path,
) -> None:
    page = work_page(
        data_root=tmp_path / "data",
        workspace_root=None,
        desktop_mode=True,
    )
    row = re.search(
        r'<article class="source-row" data-source="resume-library".*?</article>',
        page,
        re.DOTALL,
    )
    assert row is not None
    assert "查看" not in row.group(0)
    assert "上传简历" in row.group(0)
    assert 'data-source-kind="resume-library"' in row.group(0)
