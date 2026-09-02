from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from soloscale.evidence_hub import EvidenceHub, EvidenceHubError
from soloscale.evidence_hub_models import ReceiptStatus
from soloscale.knowledge_models import (
    ContentRole,
    NormalizedChunk,
    NormalizedDocument,
    ParsedSource,
    SourceKind,
)
from soloscale.knowledge_store import KnowledgeStore


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _store(
    root: Path,
    *,
    source_kind: SourceKind = SourceKind.CODEX_SESSION,
    document_body: str = "private body",
) -> KnowledgeStore:
    store = KnowledgeStore(root)
    document = NormalizedDocument(
        id="document-1",
        source_kind=source_kind,
        external_id="private-thread",
        locator="/private/thread.jsonl",
        title="Safe title",
        content_sha256=_digest(document_body),
        byte_size=len(document_body.encode("utf-8")),
        observed_at=datetime(2026, 8, 13, tzinfo=UTC),
        metadata={"note": "private"},
    )
    chunk = NormalizedChunk(
        id="chunk-1",
        document_id=document.id,
        ordinal=0,
        role=ContentRole.USER,
        text="private body",
        text_sha256=_digest("private body"),
        metadata={"topic": "private"},
    )
    store.sync([ParsedSource(document=document, chunks=[chunk])])
    return store


def test_refresh_uses_metadata_only_snapshot_and_is_stable(tmp_path: Path) -> None:
    root = tmp_path / ".soloscale"
    store = _store(root)
    hub = EvidenceHub(root, knowledge_store=store)

    first = hub.refresh()
    second = hub.refresh()
    status = hub.status()

    assert first.status is ReceiptStatus.SUCCEEDED
    assert first.snapshot_sha256 == second.snapshot_sha256 == status.snapshot_sha256
    assert first.receipt_id != second.receipt_id
    assert first.created_count == 3
    assert second.unchanged_count == 3
    assert second.updated_count == 0
    with sqlite3.connect(hub.database_path) as connection:
        dump = " ".join(str(row[0]) for row in connection.execute("SELECT sql FROM sqlite_master"))
        assert "body" not in dump and "locator" not in dump and "external_id" not in dump
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute(
            "SELECT value FROM hub_meta WHERE key = 'schema_version'"
        ).fetchone() == ("1",)
    assert "private body" not in hub.database_path.read_bytes().decode("latin1")

    with sqlite3.connect(hub.database_path) as connection:
        connection.execute("DROP TABLE bundle_lineage_items")
        connection.execute("DROP TABLE lineage_items")
        connection.execute("DROP TABLE lineage_sources")
    reopened = EvidenceHub(root, knowledge_store=store)
    assert reopened.status().snapshot_sha256 == status.snapshot_sha256
    with sqlite3.connect(reopened.database_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"lineage_sources", "lineage_items", "bundle_lineage_items"}.issubset(tables)

    buildlog_hub = EvidenceHub(
        tmp_path / "buildlog-evidence",
        knowledge_store=_store(
            tmp_path / "buildlog-knowledge", source_kind=SourceKind.BUILDLOG_RUN
        ),
    )
    buildlog_hub.refresh()
    assert buildlog_hub.status().truth_class_counts["personal_artifact"] == 2


def test_search_bundle_and_lineage_are_metadata_only(tmp_path: Path) -> None:
    root = tmp_path / ".soloscale"
    hub = EvidenceHub(root, knowledge_store=_store(root))
    hub.refresh()
    item = hub.search_metadata("knowledge_chunk")[0]
    bundle = hub.register_bundle(hub.build_bundle([item.evidence_id]))

    assert hub.get_bundle(bundle.bundle_id) == bundle
    assert hub.get_lineage(item.evidence_id) is not None
    assert set(hub.get_lineage(item.evidence_id) or {}) == {"item", "source"}
    assert item.evidence_type == "knowledge_chunk_metadata"
    assert item.verification_status == "metadata_captured"

    _store(root, document_body="revised private document")
    hub.refresh()
    resolved_bundle, resolved_items = hub.resolve_bundle(bundle.bundle_id)
    assert resolved_bundle == bundle
    assert [resolved.evidence_id for resolved in resolved_items] == [item.evidence_id]
    with sqlite3.connect(hub.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM bundle_lineage_items WHERE bundle_id = ?",
            (bundle.bundle_id,),
        ).fetchone() == (1,)


def test_bundle_registration_rolls_back_when_lineage_freeze_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".soloscale"
    hub = EvidenceHub(root, knowledge_store=_store(root))
    hub.refresh()
    item = hub.search_metadata("knowledge_chunk")[0]
    candidate = hub.build_bundle([item.evidence_id], intent="atomic failure test")

    def fail_lineage_freeze(
        _hub: EvidenceHub, _connection: sqlite3.Connection, _bundle: object
    ) -> None:
        raise EvidenceHubError("simulated lineage freeze failure")

    with monkeypatch.context() as patch:
        patch.setattr(EvidenceHub, "_archive_bundle_lineage", fail_lineage_freeze)
        with pytest.raises(EvidenceHubError, match="simulated lineage freeze failure"):
            hub.register_bundle(candidate)

    assert hub.get_bundle(candidate.bundle_id) is None
    with sqlite3.connect(hub.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM bundle_lineage_items WHERE bundle_id = ?",
            (candidate.bundle_id,),
        ).fetchone() == (0,)

    assert hub.register_bundle(candidate) == candidate
    assert hub.register_bundle(candidate) == candidate
    assert hub.resolve_bundle(candidate.bundle_id)[0] == candidate
    with sqlite3.connect(hub.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM bundles WHERE bundle_id = ?", (candidate.bundle_id,)
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM bundle_lineage_items WHERE bundle_id = ?",
            (candidate.bundle_id,),
        ).fetchone() == (1,)

    conflict = candidate.model_copy(update={"intent": "conflicting immutable payload"})
    with pytest.raises(EvidenceHubError, match="conflicting immutable"):
        hub.register_bundle(conflict)

    with sqlite3.connect(hub.database_path) as connection:
        connection.execute(
            "DELETE FROM bundle_lineage_items WHERE bundle_id = ?",
            (candidate.bundle_id,),
        )
    with pytest.raises(EvidenceHubError, match="evidence is unavailable"):
        hub.resolve_bundle(candidate.bundle_id)
    with pytest.raises(EvidenceHubError, match="immutable lineage is incomplete"):
        hub.register_bundle(candidate)


def test_case_asset_and_outcome_records_are_deterministic(tmp_path: Path) -> None:
    buildlog_models = pytest.importorskip("buildlog.models")
    root = tmp_path / ".soloscale"
    hub = EvidenceHub(root, knowledge_store=_store(root))
    hub.refresh()
    bundle = hub.register_bundle(
        hub.build_bundle([hub.search_metadata("knowledge_document")[0].evidence_id])
    )
    draft_case = hub.register_case(bundle_id=bundle.bundle_id, title="Draft recovery")
    with pytest.raises(EvidenceHubError, match="must be promoted"):
        hub.buildlog_projection(draft_case.case_id, audience="solo builders")
    case = hub.register_case(
        bundle_id=bundle.bundle_id, title="Recovery", promotion="promoted"
    )
    asset = hub.register_asset(
        case_id=case.case_id,
        asset_kind="draft",
        content_sha256=_digest("asset"),
        private_locator="private://content/run/draft",
    )
    outcome = hub.register_outcome(
        case_id=case.case_id,
        asset_id=asset.asset_id,
        status="reviewed",
        content_sha256=_digest("outcome"),
    )

    assert hub.get_case(case.case_id) == case
    assert case.evidence_ids == bundle.evidence_ids
    assert hub.get_asset(asset.asset_id) == asset
    assert hub.get_outcome(outcome.outcome_id) == outcome
    assert hub.status().outcome_count == 1
    assert buildlog_models.Iteration.model_validate(
        hub.buildlog_projection(case.case_id, audience="solo builders")
    ).metadata["evidence_bundle_id"] == bundle.bundle_id
    with pytest.raises(ValueError, match="bundle or case"):
        hub.register_asset(
            asset_kind="orphan",
            content_sha256=_digest("orphan"),
            private_locator="private://content/orphan",
        )
    with pytest.raises(ValueError, match="asset is required"):
        hub.register_outcome(status="reviewed", content_sha256=_digest("orphan-outcome"))


def test_failed_refresh_preserves_prior_snapshot_and_records_code(tmp_path: Path) -> None:
    root = tmp_path / ".soloscale"
    hub = EvidenceHub(root, knowledge_store=_store(root))
    successful = hub.refresh()
    failed = hub.refresh(git_root=tmp_path / "not-a-repository")

    assert failed.status is ReceiptStatus.FAILED
    assert failed.error_code == "snapshot-invalid"
    assert hub.status().snapshot_sha256 == successful.snapshot_sha256
    assert hub.recent_receipts()[0] == failed

    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(EvidenceHubError, match="symlinks"):
        EvidenceHub(linked_parent / ".soloscale")


def test_buildlog_and_git_adapters_use_bounded_local_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".soloscale"
    content_run = root / "content-runs" / "content-real-case"
    content_run.mkdir(parents=True)
    (content_run / "run.json").write_text('{"private":"application body"}', encoding="utf-8")
    (content_run / "receipt.json").write_text('{"status":"reviewed"}', encoding="utf-8")
    buildlog = tmp_path / "runs" / "run-one"
    buildlog.mkdir(parents=True)
    (buildlog / "03_draft.md").write_text("not read by the hub", encoding="utf-8")
    git_root = tmp_path / "repository"
    git_root.mkdir()
    subprocess.run(["git", "init", "-q", str(git_root)], check=True)
    (git_root / "note.txt").write_text("private", encoding="utf-8")
    subprocess.run(["git", "-C", str(git_root), "add", "note.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(git_root),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    hub = EvidenceHub(root, knowledge_store=_store(root))
    original_run = subprocess.run
    observed_timeouts: list[float] = []

    def record_git_command(
        command: list[str],
        *,
        capture_output: bool,
        check: bool,
        text: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        assert Path(command[0]).is_absolute()
        observed_timeouts.append(float(timeout))
        return cast(
            subprocess.CompletedProcess[str],
            original_run(
                command,
                capture_output=capture_output,
                check=check,
                text=text,
                timeout=timeout,
            ),
        )

    monkeypatch.setattr(subprocess, "run", record_git_command)

    receipt = hub.refresh(buildlog_roots=[buildlog.parent.parent], git_root=git_root)

    assert receipt.status is ReceiptStatus.SUCCEEDED
    assert hub.status().source_count == 4
    assert hub.status().truth_class_counts["outcome_receipt"] == 1
    assert observed_timeouts and set(observed_timeouts) == {10.0}
    assert "not read by the hub" not in hub.database_path.read_bytes().decode("latin1")
    assert "application body" not in hub.database_path.read_bytes().decode("latin1")
