from __future__ import annotations

import hashlib
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from soloscale.casebook_models import AttemptOutcome, EvidenceKind, LearningCase, PracticeStage
from soloscale.casebook_store import CasebookStore
from soloscale.control_tower import build_control_tower
from soloscale.knowledge_models import (
    ContentRole,
    NormalizedChunk,
    NormalizedDocument,
    ParsedSource,
    SourceKind,
)
from soloscale.knowledge_store import KnowledgeStore


def _create_case(
    store: CasebookStore,
    tmp_path: Path,
    *,
    case_id: str,
    title: str = "Structured output boundary",
    project: str = "Casebook",
    concept: str = "Parsing before validation",
    raw_evidence: str = "PRIVATE RAW EVIDENCE BODY",
) -> LearningCase:
    source = tmp_path / f"{case_id}-private-source.txt"
    source.write_text(raw_evidence, encoding="utf-8")
    return store.create_case(
        case_id=case_id,
        title=title,
        project=project,
        problem="An evaluator stopped before schema validation.",
        expected_behavior="A valid JSON value reaches the schema boundary.",
        actual_behavior="Parsing failed before the schema boundary.",
        root_cause="The response was not valid JSON.",
        resolution="Classify the parse failure before choosing recovery work.",
        verification=["The failure is attributed to the parser boundary."],
        concepts=[concept],
        evidence_sources=[(EvidenceKind.CHAT, source)],
        repository="/private/repositories/never-render-this",
        unknowns=["The rejected response body was not retained."],
    )


def _pass_all_stages(store: CasebookStore, tmp_path: Path, case_id: str) -> None:
    for stage in PracticeStage:
        receipt = tmp_path / f"{case_id}-{stage.value}-practice.md"
        receipt.write_text(f"Unaided {stage.value} practice receipt.\n", encoding="utf-8")
        store.record_attempt(
            case_id,
            stage,
            AttemptOutcome.PASS,
            receipt_path=receipt,
        )


def _metric_value(document: str, label: str) -> int:
    match = re.search(
        rf"<dt>{re.escape(label)}</dt>\s*<dd><strong>(\d+)</strong>",
        document,
    )
    assert match is not None
    return int(match.group(1))


def test_control_tower_reports_separate_states_and_summary_metrics(tmp_path: Path) -> None:
    store = CasebookStore(tmp_path / ".soloscale")
    captured = _create_case(store, tmp_path, case_id="captured-case")
    _create_case(store, tmp_path, case_id="practice-case")
    _create_case(store, tmp_path, case_id="ready-case")
    practice_receipt = tmp_path / "practice-case-explain.md"
    practice_receipt.write_text("Unaided explanation receipt.\n", encoding="utf-8")
    store.record_attempt(
        "practice-case",
        PracticeStage.EXPLAIN,
        AttemptOutcome.PASS,
        receipt_path=practice_receipt,
    )
    _pass_all_stages(store, tmp_path, "ready-case")

    archived = store.root / captured.evidence[0].archived_path
    archived.write_text("changed archived evidence", encoding="utf-8")

    output = build_control_tower(store)
    document = output.read_text(encoding="utf-8")

    assert _metric_value(document, "Active cases") == 3
    assert _metric_value(document, "Evidence gaps") == 1
    assert _metric_value(document, "Practices waiting") == 9
    assert _metric_value(document, "Interview-ready") == 1
    assert "Engineering evidence" in document
    assert "Learning mastery" in document
    assert "Resolved" in document
    assert "Captured" in document
    assert "In practice" in document
    assert "Self-assessed interview-ready" in document
    assert "Evidence integrity: Attention required" in document
    assert "Repair archived evidence integrity" in document
    assert "Trace the failure from symptom to root cause" in document
    assert "Explain → Trace → Rebuild → Debug → Defend" in document
    assert "5 of 5 current passes" in document
    assert "not externally verified mastery" in document
    assert document.index('class="focus-panel"') < document.index('class="tracks"')
    assert ".stage--waiting { color: var(--muted); }" in document


def test_control_tower_visualizes_private_rag_position_without_embedding_bodies(
    tmp_path: Path,
) -> None:
    casebook = CasebookStore(tmp_path / ".soloscale")
    knowledge = KnowledgeStore(casebook.root)
    private_body = "PRIVATE CONVERSATION BODY MUST NOT RENDER"
    digest = hashlib.sha256(private_body.encode()).hexdigest()
    knowledge.sync(
        [
            ParsedSource(
                document=NormalizedDocument(
                    id="doc-control-tower-rag",
                    source_kind=SourceKind.CODEX_SESSION,
                    external_id="thread-control-tower-rag",
                    locator="/private/codex/session.jsonl",
                    title="Conversation evidence",
                    content_sha256=digest,
                    byte_size=len(private_body),
                    observed_at=datetime(2026, 8, 9, tzinfo=UTC),
                ),
                chunks=[
                    NormalizedChunk(
                        id="chunk-control-tower-rag",
                        document_id="doc-control-tower-rag",
                        ordinal=0,
                        role=ContentRole.USER,
                        text=private_body,
                        text_sha256=digest,
                    )
                ],
            )
        ]
    )
    failed_run = casebook.root / "knowledge" / "agent-runs" / "failed-run"
    failed_run.mkdir(parents=True)
    (failed_run / "failure.json").write_text("{}\n", encoding="utf-8")

    document = build_control_tower(casebook).read_text(encoding="utf-8")

    assert "Conversation RAG" in document
    assert "Selected conversations" in document
    assert "Bounded Evidence Agent" in document
    assert "Recovery review" in document
    assert "<dt>Documents</dt><dd>1</dd>" in document
    assert "<dt>Chunks</dt><dd>1</dd>" in document
    assert "codex_session: 1" in document
    assert private_body not in document
    assert "/private/codex/session.jsonl" not in document


def test_control_tower_escapes_fields_and_excludes_private_evidence(tmp_path: Path) -> None:
    store = CasebookStore(tmp_path / ".soloscale")
    raw_body = "RAW-EVIDENCE-DO-NOT-EMBED <script>private()</script>"
    case = _create_case(
        store,
        tmp_path,
        case_id="hostile-case",
        title='<script>alert("title")</script>',
        project='<img src=x onerror="project()">',
        concept="<svg/onload=concept()>",
        raw_evidence=raw_body,
    )

    output = build_control_tower(store)
    document = output.read_text(encoding="utf-8")

    assert "<script>" not in document
    assert '<img src=x onerror="project()">' not in document
    assert "<svg/onload=concept()>" not in document
    assert "&lt;script&gt;alert(&quot;title&quot;)&lt;/script&gt;" in document
    assert "&lt;img src=x onerror=&quot;project()&quot;&gt;" in document
    assert "&lt;svg/onload=concept()&gt;" in document
    assert raw_body not in document
    assert case.evidence[0].source_path not in document
    assert case.repository is not None
    assert case.repository not in document
    assert case.evidence[0].archived_path not in document
    assert case.evidence[0].sha256 in document
    assert re.findall(r'href="([^"]+)"', document) == ["#main-content"]


def test_control_tower_writes_default_and_requested_output_paths(tmp_path: Path) -> None:
    store = CasebookStore(tmp_path / ".soloscale")

    default_output = build_control_tower(store)
    assert default_output == store.root / "control-tower" / "index.html"
    assert default_output.is_file()
    assert "Capture a learning case to begin" in default_output.read_text(encoding="utf-8")

    requested_output = store.root / "control-tower" / "derived" / "tower.html"
    assert build_control_tower(store, requested_output) == requested_output
    assert requested_output.is_file()
    document = requested_output.read_text(encoding="utf-8")
    assert "Generated " not in document
    assert "Source snapshot " not in document
    assert "schema 0.1" in document
    assert "Source of truth: local case JSON and append-only practice JSONL" in document


def test_control_tower_rebuild_is_deterministic_from_persisted_timestamps(
    tmp_path: Path,
) -> None:
    store = CasebookStore(tmp_path / ".soloscale")
    case = _create_case(store, tmp_path, case_id="stable-dashboard")
    receipt = tmp_path / "stable-dashboard-explain.md"
    receipt.write_text("Unaided explanation receipt.\n", encoding="utf-8")
    attempt_result = store.record_attempt(
        case.id,
        PracticeStage.EXPLAIN,
        AttemptOutcome.PASS,
        receipt_path=receipt,
    )

    output = build_control_tower(store)
    first = output.read_bytes()
    second = build_control_tower(store).read_bytes()

    assert second == first
    expected_snapshot = max(
        case.created_at,
        attempt_result.attempt.created_at,
    ).astimezone(UTC)
    assert (
        f"Source snapshot {expected_snapshot.replace(microsecond=0).isoformat()}"
        in first.decode("utf-8")
    )
    assert b"Generated " not in first


def test_control_tower_rejects_source_record_and_preserves_its_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CasebookStore(tmp_path / ".soloscale")
    case = _create_case(store, tmp_path, case_id="protected-case")
    case_file = store.root / "cases" / case.id / "case.json"
    original = case_file.read_bytes()

    with pytest.raises(ValueError, match="Control Tower output"):
        build_control_tower(store, case_file)
    assert case_file.read_bytes() == original

    dashboard = build_control_tower(store).parent
    monkeypatch.chdir(dashboard)
    relative_escape = Path("..") / "cases" / case.id / "case.json"
    with pytest.raises(ValueError, match="Control Tower output"):
        build_control_tower(store, relative_escape)
    assert case_file.read_bytes() == original


def test_control_tower_rejects_symlinks_that_resolve_outside_dashboard(
    tmp_path: Path,
) -> None:
    store = CasebookStore(tmp_path / ".soloscale")
    dashboard = build_control_tower(store).parent
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = dashboard / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="Control Tower output"):
        build_control_tower(store, linked_parent / "tower.html")

    assert not (outside / "tower.html").exists()


def test_control_tower_enforces_private_modes_without_chmodding_custom_ancestor(
    tmp_path: Path,
) -> None:
    store = CasebookStore(tmp_path / ".soloscale")
    previous_umask = os.umask(0)
    try:
        output = build_control_tower(store)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    custom_parent = output.parent / "existing-custom-parent"
    custom_parent.mkdir(mode=0o755)
    custom_parent.chmod(0o755)
    custom_output = custom_parent / "tower.html"
    build_control_tower(store, custom_output)

    assert stat.S_IMODE(custom_parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(custom_output.stat().st_mode) == 0o600
