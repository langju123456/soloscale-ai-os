"""Private EvidenceHub catalog with atomic metadata-only refreshes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from soloscale.conversation_intake import discover_buildlog_runs, parse_buildlog_run
from soloscale.evidence_hub_models import (
    AssetRecord,
    CasePromotion,
    CaseRecord,
    EvidenceBundle,
    EvidenceHubStatus,
    EvidenceItem,
    OutcomeReceipt,
    ReceiptStatus,
    SourceRecord,
    SyncReceipt,
    TruthClass,
)
from soloscale.knowledge_store import KnowledgeStore

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_MAX_SEARCH_LIMIT = 100
_SCHEMA_VERSION = "1"
_MAX_APPLICATION_RUNS = 500
_MAX_APPLICATION_FILES = 10_000
_MAX_APPLICATION_FILE_BYTES = 64 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 10
_MAX_GIT_COMMIT_EVIDENCE = 12
_GIT_SEARCH_PATH = os.pathsep.join(
    (
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        os.environ.get("PATH", ""),
    )
)


def _ensure_mode(path: Path, mode: int) -> None:
    """Avoid redundant metadata writes while preserving private-mode enforcement."""

    if stat.S_IMODE(path.stat().st_mode) != mode:
        path.chmod(mode)


class EvidenceHubError(Exception):
    """A safe error at the EvidenceHub boundary."""


class EvidenceHub:
    def __init__(self, data_root: Path, *, knowledge_store: KnowledgeStore | None = None) -> None:
        self.data_root = Path(data_root)
        self.knowledge_store = knowledge_store
        self.evidence_root = self.data_root / "evidence"
        self.database_path = self.evidence_root / "catalog.sqlite3"
        self._prepare_storage()
        self._initialize_schema()

    @classmethod
    def catalog_exists(cls, data_root: Path) -> bool:
        database_path = Path(data_root) / "evidence" / "catalog.sqlite3"
        return not database_path.is_symlink() and database_path.is_file()

    def refresh(
        self,
        *,
        knowledge_store: KnowledgeStore | None = None,
        buildlog_roots: Sequence[Path] = (),
        git_root: Path | None = None,
    ) -> SyncReceipt:
        started_at = _now()
        try:
            sources, items = self._collect_snapshot(
                knowledge_store=knowledge_store or self.knowledge_store,
                buildlog_roots=buildlog_roots,
                git_root=git_root,
            )
            _validate_snapshot(sources, items)
            snapshot_sha256 = _snapshot_digest(sources, items)
            with self._connect() as connection:
                sequence = self._next_sequence(connection)
                change_counts = self._change_counts(connection, sources, items)
                receipt = self._receipt(
                    sequence,
                    ReceiptStatus.SUCCEEDED,
                    started_at,
                    snapshot_sha256,
                    sources,
                    items,
                    change_counts=change_counts,
                )
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._archive_lineage(connection, sources, items)
                    connection.execute("DELETE FROM snapshot_items")
                    connection.execute("DELETE FROM snapshot_sources")
                    connection.executemany(
                        "INSERT INTO snapshot_sources (source_id, payload_json) VALUES (?, ?)",
                        [(source.source_id, source.model_dump_json()) for source in sources],
                    )
                    connection.executemany(
                        "INSERT INTO snapshot_items "
                        "(evidence_id, source_id, payload_json) VALUES (?, ?, ?)",
                        [
                            (item.evidence_id, item.source_id, item.model_dump_json())
                            for item in items
                        ],
                    )
                    self._insert_receipt(connection, sequence, receipt)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            return receipt
        except Exception as error:
            with self._connect() as connection:
                sequence = self._next_sequence(connection)
                receipt = self._receipt(
                    sequence,
                    ReceiptStatus.FAILED,
                    started_at,
                    None,
                    (),
                    (),
                    [_error_code(error)],
                )
                self._insert_receipt(connection, sequence, receipt)
            return receipt

    def register_source(
        self, source: SourceRecord, *, items: Sequence[EvidenceItem] = ()
    ) -> SourceRecord:
        """Register one adapter source without exposing the SQLite implementation."""

        receipt = self.sync_source(source, items=items)
        if receipt.status is ReceiptStatus.FAILED:
            raise EvidenceHubError("source registration failed")
        return source

    def sync_source(
        self, source: SourceRecord, *, items: Sequence[EvidenceItem] = ()
    ) -> SyncReceipt:
        """Replace one source's metadata projection and retain a sync receipt."""

        started_at = _now()
        source_items = list(items)
        try:
            _validate_snapshot([source], source_items)
            with self._connect() as connection:
                existing_sources = [
                    SourceRecord.model_validate_json(str(row["payload_json"]))
                    for row in connection.execute(
                        "SELECT payload_json FROM snapshot_sources WHERE source_id != ?",
                        (source.source_id,),
                    )
                ]
                existing_items = [
                    EvidenceItem.model_validate_json(str(row["payload_json"]))
                    for row in connection.execute(
                        "SELECT payload_json FROM snapshot_items WHERE source_id != ?",
                        (source.source_id,),
                    )
                ]
                combined_sources = sorted(
                    [*existing_sources, source], key=lambda item: item.source_id
                )
                combined_items = sorted(
                    [*existing_items, *source_items], key=lambda item: item.evidence_id
                )
                _validate_snapshot(combined_sources, combined_items)
                snapshot_sha256 = _snapshot_digest(combined_sources, combined_items)
                sequence = self._next_sequence(connection)
                change_counts = self._change_counts(connection, [source], source_items)
                receipt = self._receipt(
                    sequence,
                    ReceiptStatus.SUCCEEDED,
                    started_at,
                    snapshot_sha256,
                    combined_sources,
                    combined_items,
                    adapter=source.adapter,
                    change_counts=change_counts,
                )
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._archive_lineage(connection, [source], source_items)
                    connection.execute(
                        "DELETE FROM snapshot_items WHERE source_id = ?", (source.source_id,)
                    )
                    connection.execute(
                        "DELETE FROM snapshot_sources WHERE source_id = ?",
                        (source.source_id,),
                    )
                    connection.execute(
                        "INSERT INTO snapshot_sources (source_id, payload_json) VALUES (?, ?)",
                        (source.source_id, source.model_dump_json()),
                    )
                    connection.executemany(
                        "INSERT INTO snapshot_items "
                        "(evidence_id, source_id, payload_json) VALUES (?, ?, ?)",
                        [
                            (item.evidence_id, item.source_id, item.model_dump_json())
                            for item in source_items
                        ],
                    )
                    self._insert_receipt(connection, sequence, receipt)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            return receipt
        except Exception as error:
            with self._connect() as connection:
                sequence = self._next_sequence(connection)
                receipt = self._receipt(
                    sequence,
                    ReceiptStatus.FAILED,
                    started_at,
                    None,
                    (),
                    (),
                    [_error_code(error)],
                    adapter=source.adapter,
                )
                self._insert_receipt(connection, sequence, receipt)
            return receipt

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        source_types: Sequence[str] | None = None,
        truth_classes: Sequence[TruthClass] | None = None,
        project_id: str | None = None,
    ) -> list[EvidenceItem]:
        return self.search_metadata(
            query,
            limit=limit,
            source_types=source_types,
            truth_classes=truth_classes,
            project_id=project_id,
        )

    def search_metadata(
        self,
        query: str,
        *,
        limit: int = 10,
        source_types: Sequence[str] | None = None,
        truth_classes: Sequence[TruthClass] | None = None,
        project_id: str | None = None,
    ) -> list[EvidenceItem]:
        needle = " ".join(query.split()).casefold()
        if not needle:
            raise ValueError("query must contain a searchable term")
        _validate_limit(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM snapshot_items ORDER BY evidence_id"
            ).fetchall()
        items = [EvidenceItem.model_validate_json(str(row["payload_json"])) for row in rows]
        allowed_source_types = set(source_types or ())
        allowed_truth_classes = set(truth_classes or ())
        with self._connect() as connection:
            source_rows = connection.execute(
                "SELECT source_id, payload_json FROM snapshot_sources"
            ).fetchall()
        source_type_by_id = {
            str(row["source_id"]): SourceRecord.model_validate_json(
                str(row["payload_json"])
            ).source_type
            for row in source_rows
        }
        return [
            item
            for item in items
            if needle in _metadata_text(item).casefold()
            and (
                not allowed_source_types
                or source_type_by_id.get(item.source_id) in allowed_source_types
            )
            and (not allowed_truth_classes or item.truth_class in allowed_truth_classes)
            and (project_id is None or item.project == project_id)
        ][:limit]

    def build_bundle(
        self,
        evidence_ids: Sequence[str],
        *,
        intent: str = "evidence review",
        query: str | None = None,
        coverage: Sequence[str] = (),
        gaps: Sequence[str] = (),
        filters: dict[str, str] | None = None,
    ) -> EvidenceBundle:
        ids = list(dict.fromkeys(evidence_ids))
        if not ids or any(not value.strip() for value in ids):
            raise ValueError("at least one nonblank evidence id is required")
        available = {item.evidence_id for item in self._all_items()}
        if not set(ids).issubset(available):
            raise EvidenceHubError("one or more evidence items are unavailable")
        payload = {
            "intent": intent,
            "query": query,
            "evidence_ids": ids,
            "coverage": list(coverage),
            "gaps": list(gaps),
            "filters": filters or {},
            "version": "1",
        }
        bundle_sha256 = _digest(payload)
        return EvidenceBundle(
            bundle_id=_stable_id("bundle", bundle_sha256),
            intent=intent,
            query=query,
            evidence_ids=ids,
            coverage=list(coverage),
            gaps=list(gaps),
            filters=filters or {},
            created_at=_now(),
            bundle_sha256=bundle_sha256,
        )

    def register_bundle(self, bundle: EvidenceBundle) -> EvidenceBundle:
        payload = bundle.model_dump_json()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO bundles (bundle_id, payload_json) VALUES (?, ?)",
                    (bundle.bundle_id, payload),
                )
                if inserted.rowcount == 0:
                    existing = connection.execute(
                        "SELECT payload_json FROM bundles WHERE bundle_id = ?",
                        (bundle.bundle_id,),
                    ).fetchone()
                    if existing is None or _stable_json(
                        str(existing["payload_json"])
                    ) != _stable_json(payload):
                        raise EvidenceHubError("conflicting immutable bundles record")
                    registered = EvidenceBundle.model_validate_json(
                        str(existing["payload_json"])
                    )
                else:
                    registered = bundle
                    self._archive_bundle_lineage(connection, registered)
                frozen_ids = {
                    str(row["evidence_id"])
                    for row in connection.execute(
                        "SELECT evidence_id FROM bundle_lineage_items "
                        "WHERE bundle_id = ?",
                        (registered.bundle_id,),
                    )
                }
                if frozen_ids != set(registered.evidence_ids):
                    raise EvidenceHubError("bundle immutable lineage is incomplete")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return registered

    def get_bundle(self, bundle_id: str) -> EvidenceBundle | None:
        return cast(
            EvidenceBundle | None,
            self._get_record("bundles", "bundle_id", bundle_id, EvidenceBundle),
        )

    def get_evidence(self, evidence_id: str) -> EvidenceItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM snapshot_items WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT payload_json FROM lineage_items WHERE evidence_id = ?",
                    (evidence_id,),
                ).fetchone()
        return EvidenceItem.model_validate_json(str(row["payload_json"])) if row else None

    def resolve_bundle(self, bundle_id: str) -> tuple[EvidenceBundle, list[EvidenceItem]]:
        """Resolve one application-neutral bundle without returning source bodies."""

        bundle = self.get_bundle(bundle_id)
        if bundle is None:
            raise EvidenceHubError("bundle is unavailable")
        by_id = self._bundle_evidence_records(bundle.bundle_id, bundle.evidence_ids)
        if any(evidence_id not in by_id for evidence_id in bundle.evidence_ids):
            raise EvidenceHubError("bundle evidence is unavailable in the current snapshot")
        return bundle, [by_id[evidence_id] for evidence_id in bundle.evidence_ids]

    def register_case(
        self,
        *,
        bundle_id: str,
        problem: str | None = None,
        title: str | None = None,
        decisions: Sequence[str] = (),
        implementation: Sequence[str] = (),
        failures: Sequence[str] = (),
        recovery: Sequence[str] = (),
        results: Sequence[str] = (),
        unknowns: Sequence[str] = (),
        promotion: str = "draft",
        status: str | None = None,
    ) -> CaseRecord:
        if self.get_bundle(bundle_id) is None:
            raise EvidenceHubError("bundle must be registered before a case")
        problem_text = problem or title
        if not problem_text:
            raise ValueError("case problem is required")
        title_text = title or problem_text
        bundle = self.get_bundle(bundle_id)
        assert bundle is not None
        promotion_value = status or promotion
        record = CaseRecord(
            case_id=_stable_id("case", bundle_id, problem_text),
            bundle_id=bundle_id,
            title=title_text,
            problem=problem_text,
            evidence_ids=bundle.evidence_ids,
            decisions=list(decisions),
            implementation=list(implementation),
            failures=list(failures),
            recovery=list(recovery),
            results=list(results),
            unknowns=list(unknowns),
            promotion=CasePromotion(promotion_value),
            created_at=_now(),
        )
        payload = self._insert_record(
            "cases", "case_id", record.case_id, record.model_dump_json()
        )
        return CaseRecord.model_validate_json(payload)

    def get_case(self, case_id: str) -> CaseRecord | None:
        return cast(
            CaseRecord | None,
            self._get_record("cases", "case_id", case_id, CaseRecord),
        )

    def buildlog_projection(self, case_id: str, *, audience: str) -> dict[str, object]:
        """Project a promoted case into BuildLog's legacy standalone input contract."""

        case = self.get_case(case_id)
        if case is None:
            raise EvidenceHubError("case is unavailable")
        if case.promotion is not CasePromotion.PROMOTED:
            raise EvidenceHubError("case must be promoted before BuildLog projection")
        bundle, items = self.resolve_bundle(case.bundle_id)
        decisions = case.decisions or ["No engineering decision has been promoted yet"]
        return {
            "id": case.case_id,
            "title": case.title,
            "goal": case.problem,
            "context": (
                f"Evidence bundle {bundle.bundle_id} contains {len(items)} metadata-only "
                "evidence references."
            ),
            "problem": case.problem,
            "actions": case.implementation or ["Implementation detail remains unknown"],
            "decisions": [
                {
                    "decision": decision,
                    "reason": "Recorded in the promoted CaseRecord",
                    "alternatives_considered": ["Not recorded"],
                }
                for decision in decisions
            ],
            "trade_offs": case.failures or ["Trade-offs remain unknown"],
            "result": "; ".join(case.results) if case.results else "Outcome remains unknown",
            "lessons": case.recovery or case.unknowns or ["No lesson has been promoted"],
            "evidence": case.evidence_ids,
            "audience": audience,
            "created_at": case.created_at.isoformat(),
            "metadata": {
                "evidence_bundle_id": bundle.bundle_id,
                "evidence_bundle_sha256": bundle.bundle_sha256,
                "promotion_state": case.promotion,
                "explicit_gaps": bundle.gaps,
            },
        }

    def register_asset(
        self,
        *,
        owner: str = "operator",
        asset_type: str | None = None,
        asset_kind: str | None = None,
        content_sha256: str,
        bundle_id: str | None = None,
        case_id: str | None = None,
        private_locator: str | None = None,
        external_locator: str | None = None,
        provenance: dict[str, str] | None = None,
        approval: str = "pending",
        evidence_ids: Sequence[str] = (),
    ) -> AssetRecord:
        case = self.get_case(case_id) if case_id is not None else None
        if case_id is not None and case is None:
            raise EvidenceHubError("asset case is unavailable")
        bundle = self.get_bundle(bundle_id) if bundle_id is not None else None
        if bundle_id is not None and bundle is None:
            raise EvidenceHubError("asset bundle is unavailable")
        if case is not None and bundle_id is not None and case.bundle_id != bundle_id:
            raise EvidenceHubError("asset case and bundle relationships are inconsistent")
        self._require_evidence(evidence_ids)
        lineage_bundle = bundle or (self.get_bundle(case.bundle_id) if case is not None else None)
        if lineage_bundle is not None and not set(evidence_ids).issubset(
            lineage_bundle.evidence_ids
        ):
            raise EvidenceHubError("asset evidence is outside its lineage bundle")
        kind = asset_type or asset_kind
        if not kind:
            raise ValueError("asset type is required")
        record = AssetRecord(
            asset_id=_stable_id(
                "asset",
                owner,
                kind,
                content_sha256,
                bundle_id or "",
                case_id or "",
                private_locator or "",
                external_locator or "",
            ),
            owner=owner,
            bundle_id=bundle_id,
            case_id=case_id,
            asset_type=kind,
            private_locator=private_locator,
            external_locator=external_locator,
            content_sha256=content_sha256,
            provenance=provenance or {},
            approval=approval,
            evidence_ids=list(evidence_ids),
            created_at=_now(),
        )
        payload = self._insert_record(
            "assets", "asset_id", record.asset_id, record.model_dump_json()
        )
        return AssetRecord.model_validate_json(payload)

    def get_asset(self, asset_id: str) -> AssetRecord | None:
        return cast(
            AssetRecord | None,
            self._get_record("assets", "asset_id", asset_id, AssetRecord),
        )

    def register_outcome(
        self,
        *,
        outcome_type: str = "delivery",
        platform: str = "local",
        status: str,
        final_sha256: str | None = None,
        content_sha256: str | None = None,
        external_id: str | None = None,
        url: str | None = None,
        metadata: dict[str, str] | None = None,
        evidence_ids: Sequence[str] = (),
        case_id: str | None = None,
        asset_id: str | None = None,
    ) -> OutcomeReceipt:
        if case_id is not None and self.get_case(case_id) is None:
            raise EvidenceHubError("outcome case is unavailable")
        if asset_id is None:
            raise ValueError("outcome asset is required")
        asset = self.get_asset(asset_id)
        if asset is None:
            raise EvidenceHubError("outcome asset is unavailable")
        if case_id is not None and asset.case_id != case_id:
            raise EvidenceHubError("outcome case and asset relationships are inconsistent")
        self._require_evidence(evidence_ids)
        if not set(evidence_ids).issubset(asset.evidence_ids):
            raise EvidenceHubError("outcome evidence is outside its asset lineage")
        digest = final_sha256 or content_sha256
        if not digest:
            raise ValueError("outcome final hash is required")
        record = OutcomeReceipt(
            outcome_id=_stable_id(
                "outcome",
                outcome_type,
                platform,
                status,
                digest,
                external_id or "",
                case_id or "",
                asset_id or "",
            ),
            outcome_type=outcome_type,
            platform=platform,
            observed_at=_now(),
            external_id=external_id,
            url=url,
            final_sha256=digest,
            status=status,
            metadata=metadata or {},
            evidence_ids=list(evidence_ids),
            case_id=case_id,
            asset_id=asset_id,
        )
        payload = self._insert_record(
            "outcomes", "outcome_id", record.outcome_id, record.model_dump_json()
        )
        return OutcomeReceipt.model_validate_json(payload)

    def get_outcome(self, outcome_id: str) -> OutcomeReceipt | None:
        return cast(
            OutcomeReceipt | None,
            self._get_record("outcomes", "outcome_id", outcome_id, OutcomeReceipt),
        )

    def get_lineage(self, evidence_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM snapshot_items WHERE evidence_id = ?", (evidence_id,)
            ).fetchone()
            if row is not None:
                item = EvidenceItem.model_validate_json(str(row["payload_json"]))
                source_row = connection.execute(
                    "SELECT payload_json FROM snapshot_sources WHERE source_id = ?",
                    (item.source_id,),
                ).fetchone()
            else:
                history_row = connection.execute(
                    "SELECT lineage_source_id, payload_json FROM lineage_items "
                    "WHERE evidence_id = ? ORDER BY rowid DESC LIMIT 1",
                    (evidence_id,),
                ).fetchone()
                if history_row is None:
                    return None
                item = EvidenceItem.model_validate_json(str(history_row["payload_json"]))
                source_row = connection.execute(
                    "SELECT payload_json FROM lineage_sources WHERE lineage_source_id = ?",
                    (history_row["lineage_source_id"],),
                ).fetchone()
        return (
            {
                "item": item,
                "source": SourceRecord.model_validate_json(str(source_row["payload_json"])),
            }
            if source_row
            else None
        )

    def status(self) -> EvidenceHubStatus:
        with self._connect() as connection:
            sources = [
                SourceRecord.model_validate_json(str(row["payload_json"]))
                for row in connection.execute("SELECT payload_json FROM snapshot_sources")
            ]
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("snapshot_items", "bundles", "cases", "assets", "outcomes")
            }
            items = [
                EvidenceItem.model_validate_json(str(item_row["payload_json"]))
                for item_row in connection.execute("SELECT payload_json FROM snapshot_items")
            ]
            row = connection.execute(
                "SELECT payload_json FROM sync_receipts ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            snapshot = connection.execute(
                "SELECT value FROM hub_meta WHERE key = 'snapshot_sha256'"
            ).fetchone()
        return EvidenceHubStatus(
            source_count=len(sources),
            evidence_count=counts["snapshot_items"],
            bundle_count=counts["bundles"],
            case_count=counts["cases"],
            asset_count=counts["assets"],
            outcome_count=counts["outcomes"],
            source_counts=_counts(source.source_type for source in sources),
            truth_class_counts=_counts(item.truth_class.value for item in items),
            snapshot_sha256=str(snapshot["value"]) if snapshot else None,
            last_receipt=SyncReceipt.model_validate_json(str(row["payload_json"])) if row else None,
        )

    def recent_receipts(self, *, limit: int = 10) -> list[SyncReceipt]:
        _validate_limit(limit)
        return cast(
            list[SyncReceipt], self._records("sync_receipts", "sequence DESC", SyncReceipt, limit)
        )

    def recent_evidence(self, *, limit: int = 50) -> list[EvidenceItem]:
        """Return the most recently captured metadata-only evidence items."""

        _validate_limit(limit)
        return cast(
            list[EvidenceItem],
            self._records("snapshot_items", "rowid DESC", EvidenceItem, limit),
        )

    def recent_assets(self, *, limit: int = 10) -> list[AssetRecord]:
        _validate_limit(limit)
        return cast(list[AssetRecord], self._records("assets", "rowid DESC", AssetRecord, limit))

    def recent_outcomes(self, *, limit: int = 10) -> list[OutcomeReceipt]:
        _validate_limit(limit)
        return cast(
            list[OutcomeReceipt], self._records("outcomes", "rowid DESC", OutcomeReceipt, limit)
        )

    def sync_git_repository(self, git_root: Path) -> SyncReceipt:
        """Incrementally replace evidence for one explicitly selected Git repository."""

        source, items = inspect_git_repository(git_root)
        return self.sync_source(source, items=items)

    def git_repository_snapshot(
        self, git_root: Path
    ) -> tuple[SourceRecord, list[EvidenceItem]] | None:
        """Return the stored snapshot for one exact local repository without source text."""

        repository_key = _repository_key(git_root)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT source_id, payload_json FROM snapshot_sources"
            ).fetchall()
            sources = [
                SourceRecord.model_validate_json(str(row["payload_json"]))
                for row in rows
            ]
            source = next(
                (
                    candidate
                    for candidate in sources
                    if candidate.metadata.get("repository_key") == repository_key
                ),
                None,
            )
            if source is None:
                return None
            item_rows = connection.execute(
                "SELECT payload_json FROM snapshot_items WHERE source_id = ?",
                (source.source_id,),
            ).fetchall()
        return source, [
            EvidenceItem.model_validate_json(str(row["payload_json"]))
            for row in item_rows
        ]

    def _collect_snapshot(
        self,
        *,
        knowledge_store: KnowledgeStore | None,
        buildlog_roots: Sequence[Path],
        git_root: Path | None,
    ) -> tuple[list[SourceRecord], list[EvidenceItem]]:
        sources: list[SourceRecord] = []
        items: list[EvidenceItem] = []
        if knowledge_store is not None:
            catalog = knowledge_store.catalog_metadata()
            for document in catalog.documents:
                source = SourceRecord(
                    source_id=_stable_id(
                        "source", document.source_kind.value, document.native_id
                    ),
                    native_id=document.native_id,
                    source_system="knowledge_store",
                    source_type=document.source_kind.value,
                    project=document.project,
                    original_locator=document.locator,
                    captured_at=_now(),
                    source_at=document.observed_at,
                    content_sha256=document.content_sha256,
                    sensitivity="private",
                    truth_class=(
                        TruthClass.PERSONAL_ARTIFACT
                        if document.source_kind.value == "buildlog_run"
                        else TruthClass.PERSONAL_CONTEXT
                    ),
                    raw_available=True,
                    adapter="knowledge_catalog_metadata",
                    metadata=document.metadata,
                )
                sources.append(source)
                items.append(
                    _item(
                        source,
                        document.document_id,
                        document.observed_at,
                        "knowledge_document metadata",
                        document.content_sha256,
                    )
                )
            by_document = {
                document.document_id: source
                for document, source in zip(catalog.documents, sources, strict=True)
            }
            for chunk in catalog.chunks:
                chunk_source = by_document.get(chunk.document_id)
                if chunk_source is None:
                    raise EvidenceHubError("knowledge metadata has an orphaned chunk")
                items.append(
                    _item(
                        chunk_source,
                        chunk.chunk_id,
                        chunk.timestamp,
                        "knowledge_chunk metadata",
                        chunk.text_sha256,
                        relationships=[f"document:{chunk.document_id}", f"ordinal:{chunk.ordinal}"],
                        verification={"metadata_sha256": chunk.metadata_sha256},
                    )
                )
        for root in buildlog_roots:
            for run in discover_buildlog_runs(root):
                parsed = parse_buildlog_run(run)
                build_document = parsed.document
                build_source_id = _stable_id(
                    "source", "buildlog_run", build_document.external_id
                )
                if any(source.source_id == build_source_id for source in sources):
                    sources = [source for source in sources if source.source_id != build_source_id]
                    items = [item for item in items if item.source_id != build_source_id]
                build_source = SourceRecord(
                    source_id=build_source_id,
                    native_id=build_document.external_id,
                    source_system="buildlog",
                    source_type="run",
                    project=build_document.parent_external_id,
                    original_locator=build_document.locator,
                    captured_at=_now(),
                    source_at=build_document.observed_at,
                    content_sha256=build_document.content_sha256,
                    sensitivity="private",
                    truth_class=TruthClass.PERSONAL_ARTIFACT,
                    raw_available=True,
                    adapter="parse_buildlog_run",
                    metadata=build_document.metadata,
                )
                sources.append(build_source)
                items.append(
                    _item(
                        build_source,
                        build_document.id,
                        build_document.observed_at,
                        "BuildLog run metadata",
                        build_document.content_sha256,
                    )
                )
                for build_chunk in parsed.chunks:
                    items.append(
                        _item(
                            build_source,
                            build_chunk.id,
                            build_chunk.timestamp,
                            "BuildLog artifact metadata",
                            build_chunk.text_sha256,
                            relationships=[f"ordinal:{build_chunk.ordinal}"],
                            verification={"metadata_sha256": _digest(build_chunk.metadata)},
                        )
                    )
        application_sources, application_items = _application_records(self.data_root)
        sources.extend(application_sources)
        items.extend(application_items)
        if git_root is not None:
            source, git_items = inspect_git_repository(git_root)
            sources.append(source)
            items.extend(git_items)
        return sorted(sources, key=lambda source: source.source_id), sorted(
            items, key=lambda item: item.evidence_id
        )

    def _all_items(self) -> list[EvidenceItem]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM snapshot_items ORDER BY evidence_id"
            ).fetchall()
        return [EvidenceItem.model_validate_json(str(row["payload_json"])) for row in rows]

    def _evidence_records(self, evidence_ids: Sequence[str]) -> dict[str, EvidenceItem]:
        ids = list(dict.fromkeys(evidence_ids))
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with self._connect() as connection:
            current_rows = connection.execute(
                f"SELECT evidence_id, payload_json FROM snapshot_items "
                f"WHERE evidence_id IN ({placeholders})",
                ids,
            ).fetchall()
            history_rows = connection.execute(
                f"SELECT evidence_id, payload_json FROM lineage_items "
                f"WHERE evidence_id IN ({placeholders}) ORDER BY rowid ASC",
                ids,
            ).fetchall()
        rows = [*history_rows, *current_rows]
        return {
            str(row["evidence_id"]): EvidenceItem.model_validate_json(
                str(row["payload_json"])
            )
            for row in rows
        }

    def _bundle_evidence_records(
        self, bundle_id: str, evidence_ids: Sequence[str]
    ) -> dict[str, EvidenceItem]:
        ids = list(dict.fromkeys(evidence_ids))
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT evidence_id, payload_json FROM bundle_lineage_items "
                f"WHERE bundle_id = ? AND evidence_id IN ({placeholders})",
                [bundle_id, *ids],
            ).fetchall()
        return {
            str(row["evidence_id"]): EvidenceItem.model_validate_json(
                str(row["payload_json"])
            )
            for row in rows
        }

    def _archive_lineage(
        self,
        connection: sqlite3.Connection,
        sources: Sequence[SourceRecord],
        items: Sequence[EvidenceItem],
    ) -> None:
        lineage_ids = {
            source.source_id: _stable_id(
                "lineage-source", source.source_id, source.content_sha256
            )
            for source in sources
        }
        connection.executemany(
            "INSERT OR IGNORE INTO lineage_sources "
            "(lineage_source_id, source_id, payload_json) VALUES (?, ?, ?)",
            [
                (lineage_ids[source.source_id], source.source_id, source.model_dump_json())
                for source in sources
            ],
        )
        connection.executemany(
            "INSERT OR IGNORE INTO lineage_items "
            "(evidence_id, lineage_source_id, payload_json) VALUES (?, ?, ?)",
            [
                (
                    item.evidence_id,
                    lineage_ids[item.source_id],
                    item.model_dump_json(),
                )
                for item in items
            ],
        )

    def _receipt(
        self,
        sequence: int,
        status: ReceiptStatus,
        started_at: datetime,
        snapshot_sha256: str | None,
        sources: Sequence[SourceRecord],
        items: Sequence[EvidenceItem],
        errors: list[str] | None = None,
        *,
        adapter: str = "evidence_hub_refresh",
        change_counts: tuple[int, int, int] = (0, 0, 0),
    ) -> SyncReceipt:
        counts = self._stored_counts()
        return SyncReceipt(
            receipt_id=f"sync-{sequence:08d}-{(snapshot_sha256 or 'failed')[:16]}",
            adapter=adapter,
            status=status,
            started_at=started_at,
            completed_at=_now(),
            snapshot_sha256=snapshot_sha256,
            discovered_sources=len(sources),
            source_count=len(sources),
            evidence_count=len(items),
            bundle_count=counts[0],
            case_count=counts[1],
            asset_count=counts[2],
            outcome_count=counts[3],
            source_counts=_counts(source.source_type for source in sources),
            created_count=change_counts[0],
            updated_count=change_counts[1],
            unchanged_count=change_counts[2],
            error_count=len(errors or []),
            errors=errors or [],
        )

    def _change_counts(
        self,
        connection: sqlite3.Connection,
        sources: Sequence[SourceRecord],
        items: Sequence[EvidenceItem],
    ) -> tuple[int, int, int]:
        existing_sources = {
            str(row["source_id"]): SourceRecord.model_validate_json(str(row["payload_json"]))
            for row in connection.execute("SELECT source_id, payload_json FROM snapshot_sources")
        }
        existing_items = {
            str(row["evidence_id"]): EvidenceItem.model_validate_json(str(row["payload_json"]))
            for row in connection.execute("SELECT evidence_id, payload_json FROM snapshot_items")
        }
        created = updated = unchanged = 0
        for source_record in sources:
            existing_source = existing_sources.get(source_record.source_id)
            if existing_source is None:
                created += 1
            elif _stable_record(source_record) == _stable_record(existing_source):
                unchanged += 1
            else:
                updated += 1
        for evidence_record in items:
            existing_item = existing_items.get(evidence_record.evidence_id)
            if existing_item is None:
                created += 1
            elif _stable_record(evidence_record) == _stable_record(existing_item):
                unchanged += 1
            else:
                updated += 1
        return created, updated, unchanged

    def _stored_counts(self) -> tuple[int, int, int, int]:
        with self._connect() as connection:
            return tuple(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("bundles", "cases", "assets", "outcomes")
            )  # type: ignore[return-value]

    def _next_sequence(self, connection: sqlite3.Connection) -> int:
        return int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM sync_receipts"
            ).fetchone()[0]
        )

    def _insert_receipt(
        self, connection: sqlite3.Connection, sequence: int, receipt: SyncReceipt
    ) -> None:
        connection.execute(
            "INSERT INTO sync_receipts (sequence, payload_json) VALUES (?, ?)",
            (sequence, receipt.model_dump_json()),
        )
        if receipt.snapshot_sha256:
            connection.execute(
                "INSERT INTO hub_meta(key, value) VALUES('snapshot_sha256', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (receipt.snapshot_sha256,),
            )

    def _insert_record(self, table: str, key: str, value: str, payload: str) -> str:
        with self._connect() as connection:
            inserted = connection.execute(
                f"INSERT OR IGNORE INTO {table} ({key}, payload_json) VALUES (?, ?)",
                (value, payload),
            )
            if inserted.rowcount == 0:
                existing = connection.execute(
                    f"SELECT payload_json FROM {table} WHERE {key} = ?", (value,)
                ).fetchone()
                if existing is None:
                    raise EvidenceHubError(f"conflicting immutable {table} record")
                existing_payload = str(existing["payload_json"])
                if _stable_json(existing_payload) != _stable_json(payload):
                    raise EvidenceHubError(f"conflicting immutable {table} record")
                return existing_payload
        return payload

    def _require_evidence(self, evidence_ids: Sequence[str]) -> None:
        ids = list(evidence_ids)
        if len(ids) != len(set(ids)):
            raise ValueError("evidence ids must be unique")
        if ids and set(self._evidence_records(ids)) != set(ids):
            raise EvidenceHubError("record references unavailable evidence")

    def _get_record(
        self,
        table: str,
        key: str,
        value: str,
        model: type[EvidenceBundle] | type[CaseRecord] | type[AssetRecord] | type[OutcomeReceipt],
    ) -> EvidenceBundle | CaseRecord | AssetRecord | OutcomeReceipt | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT payload_json FROM {table} WHERE {key} = ?", (value,)
            ).fetchone()
        return model.model_validate_json(str(row["payload_json"])) if row else None

    def _records(
        self,
        table: str,
        order: str,
        model: (
            type[SyncReceipt] | type[AssetRecord] | type[OutcomeReceipt] | type[EvidenceItem]
        ),
        limit: int,
    ) -> (
        list[SyncReceipt] | list[AssetRecord] | list[OutcomeReceipt] | list[EvidenceItem]
    ):
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT payload_json FROM {table} ORDER BY {order} LIMIT ?", (limit,)
            ).fetchall()
        return cast(
            list[SyncReceipt] | list[AssetRecord] | list[OutcomeReceipt] | list[EvidenceItem],
            [model.model_validate_json(str(row["payload_json"])) for row in rows],
        )

    def _prepare_storage(self) -> None:
        _reject_symlink_ancestry(self.data_root)
        _reject_symlink_ancestry(self.evidence_root)
        if self.database_path.is_symlink():
            raise EvidenceHubError("evidence catalog paths must not be symlinks")
        self.evidence_root.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
        _reject_symlink_ancestry(self.data_root)
        _reject_symlink_ancestry(self.evidence_root)
        _ensure_mode(self.evidence_root, _DIRECTORY_MODE)
        if not self.database_path.exists():
            descriptor = os.open(
                self.database_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE
            )
            os.close(descriptor)
        _ensure_mode(self.database_path, _FILE_MODE)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.database_path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA secure_delete=ON")
            yield connection
        except sqlite3.Error as error:
            raise EvidenceHubError("evidence catalog is unavailable") from error
        finally:
            if connection is not None:
                connection.close()
            _ensure_mode(self.database_path, _FILE_MODE)

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS hub_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshot_sources (
                    source_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshot_items (
                    evidence_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lineage_sources (
                    lineage_source_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lineage_items (
                    evidence_id TEXT PRIMARY KEY, lineage_source_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bundle_lineage_items (
                    bundle_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (bundle_id, evidence_id)
                );
                CREATE TABLE IF NOT EXISTS sync_receipts (
                    sequence INTEGER PRIMARY KEY, payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bundles (
                    bundle_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assets (
                    asset_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outcomes (
                    outcome_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO hub_meta(key, value) VALUES('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
            version = connection.execute(
                "SELECT value FROM hub_meta WHERE key = 'schema_version'"
            ).fetchone()
            if version is None or str(version["value"]) != _SCHEMA_VERSION:
                raise EvidenceHubError("evidence catalog schema is unsupported")

    def _archive_bundle_lineage(
        self, connection: sqlite3.Connection, bundle: EvidenceBundle
    ) -> None:
        placeholders = ", ".join("?" for _ in bundle.evidence_ids)
        rows = connection.execute(
            f"SELECT evidence_id, payload_json FROM snapshot_items "
            f"WHERE evidence_id IN ({placeholders})",
            bundle.evidence_ids,
        ).fetchall()
        if len(rows) != len(bundle.evidence_ids):
            raise EvidenceHubError("bundle references unavailable evidence")
        connection.executemany(
            "INSERT INTO bundle_lineage_items "
            "(bundle_id, evidence_id, payload_json) VALUES (?, ?, ?)",
            [
                (bundle.bundle_id, str(row["evidence_id"]), str(row["payload_json"]))
                for row in rows
            ],
        )


def _reject_symlink_ancestry(path: Path) -> None:
    """Reject lexical ancestors without resolving a managed path through a link."""

    absolute = path.expanduser().absolute()
    if any(candidate.is_symlink() for candidate in (absolute, *absolute.parents)):
        raise EvidenceHubError("evidence catalog paths must not contain symlinks")


def _item(
    source: SourceRecord,
    native_id: str,
    source_at: datetime | None,
    summary: str,
    digest: str,
    *,
    relationships: list[str] | None = None,
    verification: dict[str, str] | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=_stable_id("evidence", source.source_id, native_id, digest),
        source_id=source.source_id,
        native_id=native_id,
        evidence_type=summary.casefold().replace(" ", "_"),
        project=source.project,
        captured_at=_now(),
        source_at=source_at,
        time_start=source_at,
        time_end=source_at,
        provenance_locator=source.original_locator,
        truth_class=source.truth_class,
        trust_state="unverified",
        public_safe_summary=summary,
        relationships=relationships or [],
        verification=verification or {},
        verification_status="metadata_captured",
        content_sha256=digest,
    )


def _repository_key(root: Path) -> str:
    return _digest(str(Path(root).expanduser().absolute()))


def inspect_git_repository(root: Path) -> tuple[SourceRecord, list[EvidenceItem]]:
    """Capture bounded metadata and recent committed facts from one selected repo."""

    root_path = Path(root).expanduser().absolute()
    git_marker = root_path / ".git"
    if (
        not root_path.is_dir()
        or root_path.is_symlink()
        or not git_marker.exists()
        or git_marker.is_symlink()
    ):
        raise EvidenceHubError("choose one regular local Git repository")
    head_one = _git(root_path, "rev-parse", "--verify", "HEAD")
    branch_one = _git(root_path, "branch", "--show-current", optional=True) or "DETACHED"
    status_one = _git(root_path, "status", "--porcelain=v1", "--untracked-files=all")
    tracked_diff = _git(root_path, "diff", "--no-ext-diff", "--binary")
    staged_diff = _git(root_path, "diff", "--cached", "--no-ext-diff", "--binary")
    commit_at = _git(root_path, "show", "-s", "--format=%cI", "HEAD")
    remote = _git(root_path, "config", "--get", "remote.origin.url", optional=True)
    log_output = _git(
        root_path,
        "log",
        f"-{_MAX_GIT_COMMIT_EVIDENCE}",
        "--pretty=format:%H%x1f%cI%x1f%s%x1e",
    )
    head_two = _git(root_path, "rev-parse", "--verify", "HEAD")
    branch_two = _git(root_path, "branch", "--show-current", optional=True) or "DETACHED"
    status_two = _git(root_path, "status", "--porcelain=v1", "--untracked-files=all")
    if (
        head_one != head_two
        or branch_one != branch_two
        or status_one != status_two
        or len(head_one) != 40
    ):
        raise EvidenceHubError("git snapshot changed during capture")
    remote_fingerprint = _digest(remote) if remote else "none"
    repository_key = _repository_key(root_path)
    status_sha256 = _digest(status_one)
    tracked_diff_sha256 = _digest(
        {"unstaged": tracked_diff, "staged": staged_diff}
    )
    status_lines = status_one.splitlines()
    dirty_count = len(status_lines)
    staged_count = sum(line[:1] not in {" ", "?"} for line in status_lines)
    unstaged_count = sum(len(line) > 1 and line[1] not in {" ", "?"} for line in status_lines)
    untracked_count = sum(line.startswith("??") for line in status_lines)
    fingerprint = _digest(
        {
            "head": head_one,
            "branch": branch_one,
            "status_sha256": status_sha256,
            "tracked_diff_sha256": tracked_diff_sha256,
        }
    )
    source = SourceRecord(
        source_id=_stable_id("source", "git", repository_key, remote_fingerprint),
        native_id=head_one,
        source_system="git",
        source_type="repository_snapshot",
        project=root_path.name,
        original_locator=str(root_path),
        captured_at=_now(),
        source_at=datetime.fromisoformat(commit_at),
        content_sha256=fingerprint,
        sensitivity="private",
        truth_class=TruthClass.PERSONAL_ARTIFACT,
        raw_available=False,
        adapter="git_metadata",
        metadata={
            "branch": branch_one,
            "dirty_count": str(dirty_count),
            "staged_count": str(staged_count),
            "unstaged_count": str(unstaged_count),
            "untracked_count": str(untracked_count),
            "remote_fingerprint": remote_fingerprint,
            "repository_key": repository_key,
            "status_sha256": status_sha256,
            "tracked_diff_sha256": tracked_diff_sha256,
            "fingerprint": fingerprint,
        },
    )
    items = [
        _item(
            source,
            source.native_id,
            source.source_at,
            "Git snapshot metadata",
            source.content_sha256,
            verification={
                "repository_key": repository_key,
                "fingerprint": fingerprint,
            },
        )
    ]
    for raw_record in log_output.split("\x1e"):
        record = raw_record.strip()
        if not record:
            continue
        parts = record.split("\x1f", 2)
        if len(parts) != 3:
            raise EvidenceHubError("git history metadata is unavailable")
        commit_sha, committed_at, raw_subject = parts
        subject = " ".join(raw_subject.split())[:300]
        if len(commit_sha) != 40 or not subject:
            raise EvidenceHubError("git history metadata is unavailable")
        subject_sha256 = _digest(subject)
        observed_at = datetime.fromisoformat(committed_at)
        items.append(
            EvidenceItem(
                evidence_id=_stable_id(
                    "evidence", source.source_id, "git_commit", commit_sha
                ),
                source_id=source.source_id,
                native_id=commit_sha,
                evidence_type="git_commit",
                project=source.project,
                captured_at=_now(),
                source_at=observed_at,
                time_start=observed_at,
                time_end=observed_at,
                provenance_locator=f"git:{commit_sha}",
                truth_class=TruthClass.PERSONAL_ARTIFACT,
                trust_state="verified_repository_commit",
                public_safe_summary=f"Repository commit: {subject}",
                relationships=[
                    f"repository:{repository_key}",
                    f"commit:{commit_sha}",
                ],
                verification={
                    "repository_key": repository_key,
                    "commit_sha": commit_sha,
                    "subject_sha256": subject_sha256,
                },
                verification_status="git_object_verified",
                content_sha256=_digest(
                    {
                        "commit_sha": commit_sha,
                        "committed_at": committed_at,
                        "subject_sha256": subject_sha256,
                    }
                ),
            )
        )
    return source, items


def _application_records(data_root: Path) -> tuple[list[SourceRecord], list[EvidenceItem]]:
    discovered: list[tuple[str, Path]] = []
    for source_type, root_name in (
        ("content_run", "content-runs"),
        ("resume_run", "resume-runs"),
        ("learning_run", "learning-runs"),
    ):
        root = data_root / root_name
        if root.is_dir() and not root.is_symlink():
            discovered.extend(
                (source_type, path)
                for path in sorted(root.iterdir(), key=lambda item: item.name)
                if path.is_dir() and not path.is_symlink() and (path / "run.json").is_file()
            )
    editorial_root = data_root / "content" / "editorial"
    if editorial_root.is_dir() and not editorial_root.is_symlink():
        receipt_paths = set(editorial_root.glob("*/receipt.json")) | set(
            editorial_root.glob("*/*/receipt.json")
        )
        for receipt in sorted(receipt_paths):
            if receipt.is_file() and not receipt.is_symlink() and not receipt.parent.is_symlink():
                discovered.append(("editorial_package", receipt.parent))
    if len(discovered) > _MAX_APPLICATION_RUNS:
        raise EvidenceHubError("application metadata scope exceeds the configured run limit")
    sources: list[SourceRecord] = []
    items: list[EvidenceItem] = []
    file_count = 0
    for source_type, run_dir in discovered:
        file_records: list[tuple[Path, str, int, datetime]] = []
        for path in sorted(run_dir.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(run_dir)
            if len(relative.parts) > 3:
                continue
            stat_result = path.stat()
            if stat_result.st_size > _MAX_APPLICATION_FILE_BYTES:
                continue
            file_count += 1
            if file_count > _MAX_APPLICATION_FILES:
                raise EvidenceHubError(
                    "application metadata scope exceeds the configured file limit"
                )
            file_records.append(
                (
                    relative,
                    _sha256_path(path),
                    stat_result.st_size,
                    datetime.fromtimestamp(stat_result.st_mtime, UTC),
                )
            )
        if not file_records:
            continue
        aggregate = _digest(
            [
                {"name": path.as_posix(), "sha256": digest}
                for path, digest, _size, _at in file_records
            ]
        )
        source = SourceRecord(
            source_id=_stable_id("source", "soloscale", source_type, run_dir.name),
            native_id=run_dir.name,
            source_system="soloscale",
            source_type=source_type,
            project="solo-scale-ai-os",
            original_locator=str(run_dir),
            captured_at=_now(),
            source_at=max(recorded_at for _path, _digest_value, _size, recorded_at in file_records),
            content_sha256=aggregate,
            sensitivity="private",
            truth_class=TruthClass.PERSONAL_ARTIFACT,
            raw_available=True,
            adapter="soloscale_application_metadata",
            metadata={"artifact_count": str(len(file_records))},
        )
        sources.append(source)
        for relative, digest, size, recorded_at in file_records:
            is_receipt = "receipt" in relative.name.casefold() or relative.name == "delivery.json"
            items.append(
                EvidenceItem(
                    evidence_id=_stable_id(
                        "evidence", source.source_id, relative.as_posix(), digest
                    ),
                    source_id=source.source_id,
                    native_id=f"{run_dir.name}:{relative.as_posix()}",
                    evidence_type=("outcome_receipt" if is_receipt else "application_artifact"),
                    project=source.project,
                    captured_at=_now(),
                    source_at=recorded_at,
                    time_start=recorded_at,
                    time_end=recorded_at,
                    provenance_locator=str(run_dir / relative),
                    truth_class=(
                        TruthClass.OUTCOME_RECEIPT
                        if is_receipt
                        else TruthClass.PERSONAL_ARTIFACT
                    ),
                    trust_state="recorded_requires_review",
                    public_safe_summary=f"{source_type} metadata: {relative.as_posix()}",
                    relationships=[f"run:{run_dir.name}"],
                    verification={"file_size": str(size)},
                    verification_status="hash_captured",
                    content_sha256=digest,
                )
            )
    return sources, items


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(root: Path, *args: str, optional: bool = False) -> str:
    executable = shutil.which("git", path=_GIT_SEARCH_PATH)
    if executable is None:
        raise EvidenceHubError("git snapshot is unavailable")
    result = subprocess.run(
        [executable, "-C", str(root), *args],
        capture_output=True,
        check=False,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode and not optional:
        raise EvidenceHubError("git snapshot is unavailable")
    return result.stdout.strip() if result.returncode == 0 else ""


def _validate_snapshot(sources: Sequence[SourceRecord], items: Sequence[EvidenceItem]) -> None:
    if len({source.source_id for source in sources}) != len(sources) or len(
        {item.evidence_id for item in items}
    ) != len(items):
        raise EvidenceHubError("snapshot identifiers are not unique")
    known = {source.source_id for source in sources}
    if any(item.source_id not in known for item in items):
        raise EvidenceHubError("snapshot item references an unknown source")


def _metadata_text(item: EvidenceItem) -> str:
    return " ".join(
        [
            item.evidence_id,
            item.source_id,
            item.native_id,
            item.project or "",
            item.public_safe_summary,
            *item.relationships,
            *item.verification.values(),
        ]
    )


def _stable_record(value: SourceRecord | EvidenceItem) -> dict[str, object]:
    payload = value.model_dump(mode="json")
    payload.pop("captured_at")
    return cast(dict[str, object], payload)


def _stable_json(payload: str) -> object:
    value = json.loads(payload)
    if isinstance(value, dict):
        for key in ("created_at", "observed_at"):
            value.pop(key, None)
    return value


def _dump(values: Sequence[SourceRecord] | Sequence[EvidenceItem]) -> list[dict[str, object]]:
    return [value.model_dump(mode="json") for value in values]


def _snapshot_digest(sources: Sequence[SourceRecord], items: Sequence[EvidenceItem]) -> str:
    """Hash stable source facts, not wall-clock collection timestamps."""
    source_payload = []
    for source in sources:
        payload = source.model_dump(mode="json")
        payload.pop("captured_at")
        source_payload.append(payload)
    item_payload = []
    for item in items:
        payload = item.model_dump(mode="json")
        payload.pop("captured_at")
        item_payload.append(payload)
    return _digest({"sources": source_payload, "items": item_payload})


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}-{_digest(list(parts))[:32]}"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _counts(values: Iterable[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return result


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not 1 <= limit <= _MAX_SEARCH_LIMIT:
        raise ValueError(f"limit must be between 1 and {_MAX_SEARCH_LIMIT}")


def _now() -> datetime:
    return datetime.now(UTC)


def _error_code(error: Exception) -> str:
    if isinstance(error, EvidenceHubError):
        return "snapshot-invalid"
    if isinstance(error, (OSError, sqlite3.Error, subprocess.SubprocessError)):
        return "local-read-failed"
    return "refresh-failed"
