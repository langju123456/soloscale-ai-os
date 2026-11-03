"""Private single-operator SQLite store for Review Workbench items.

Write rules:
- every write runs in one ``BEGIN IMMEDIATE`` transaction;
- an action must name the item's current ``revision`` (optimistic concurrency);
- the model's text is version 1 and is never updated; corrections append versions;
- runs, citation snapshots, text versions and the review log are append-only, and the
  schema's triggers enforce that for any writer, not only this class.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import cast

from soloscale.review_intake import (
    RecordedCitation,
    VerifiedDraft,
    check_current_citations,
)
from soloscale.review_models import (
    AuthorKind,
    BatchConfirmEntry,
    CitationCheck,
    EventAction,
    EvidenceStatus,
    ReviewAction,
    ReviewCitation,
    ReviewEvent,
    ReviewItemDetail,
    ReviewItemSummary,
    ReviewRunGaps,
    ReviewRunSummary,
    ReviewState,
    TextVersion,
    VersionDiff,
)
from soloscale.review_workflow import (
    CHECK_RANK,
    EvidenceChangedError,
    LowConfidenceBatchError,
    NoChangeError,
    ReviewError,
    ReviewNotFoundError,
    StaleRevisionError,
    VersionNotFoundError,
    allowed_actions,
    confirm_requirement,
    diff_text,
    initial_state,
    plan_transition,
    queue_priority,
)

SCHEMA_VERSION = 1
_MIGRATIONS = ("0001_init.sql",)
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_REQUIRED_TABLES = frozenset(
    {
        "review_runs",
        "review_items",
        "review_text_versions",
        "review_citations",
        "review_events",
    }
)
_ACTION_EVENTS = {
    ReviewAction.CONFIRM: EventAction.CONFIRMED,
    ReviewAction.CORRECT: EventAction.CORRECTED,
    ReviewAction.REJECT: EventAction.REJECTED,
    ReviewAction.REOPEN: EventAction.REOPENED,
}
MAX_LIST_LIMIT = 1_000


class ReviewStoreError(ReviewError):
    status_code = 503
    code = "REVIEW_STORE_UNAVAILABLE"


class DraftChangedError(ReviewError):
    status_code = 409
    code = "DRAFT_CHANGED_SINCE_IMPORT"


class ReviewStore:
    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        self.review_root = self.data_root / "review"
        self.database_path = self.review_root / "workbench.sqlite3"
        self._prepare_storage()
        self._initialize_schema()

    # ---------------------------------------------------------------- import

    def import_draft(
        self, draft: VerifiedDraft
    ) -> tuple[bool, ReviewRunSummary, list[ReviewItemSummary]]:
        """Create one review item per claim; replaying the same receipt is a no-op."""

        result = draft.result
        now = _now()
        with self._write() as connection:
            existing = connection.execute(
                "SELECT draft_sha256 FROM review_runs WHERE run_id = ?", (result.run_id,)
            ).fetchone()
            if existing is not None:
                if str(existing["draft_sha256"]) != draft.draft_sha256:
                    raise DraftChangedError(
                        "this run was imported from a different receipt; the draft file "
                        "changed after import",
                        run_id=result.run_id,
                    )
                created = False
            else:
                created = True
                connection.execute(
                    """
                    INSERT INTO review_runs (
                        run_id, question, model, prompt_version, draft_created_at,
                        draft_sha256, input_manifest_sha256, retrieval_manifest_sha256,
                        unsupported_json, open_questions_json, limitations_json,
                        knowledge_index_checked, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.run_id,
                        result.question,
                        result.model,
                        result.prompt_version,
                        result.created_at.isoformat(),
                        draft.draft_sha256,
                        result.input_manifest_sha256,
                        result.retrieval_manifest_sha256,
                        _json_list(result.unsupported),
                        _json_list(result.open_questions),
                        _json_list(result.limitations),
                        int(draft.knowledge_index_checked),
                        now,
                    ),
                )
                checked_at = draft.checked_at.isoformat()
                for claim in draft.claims:
                    item_id = item_id_for(result.run_id, claim.claim_index)
                    state = initial_state(claim.evidence_status)
                    connection.execute(
                        """
                        INSERT INTO review_items (
                            item_id, run_id, claim_index, state, evidence_status,
                            revision, current_version, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)
                        """,
                        (
                            item_id,
                            result.run_id,
                            claim.claim_index,
                            state.value,
                            claim.evidence_status.value,
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO review_text_versions (
                            item_id, version, text, text_sha256, author_kind,
                            reviewer, note, created_at
                        ) VALUES (?, 1, ?, ?, 'MODEL', NULL, NULL, ?)
                        """,
                        (item_id, claim.text, _sha256_text(claim.text), now),
                    )
                    for snapshot in claim.citations:
                        reference = snapshot.reference
                        connection.execute(
                            """
                            INSERT INTO review_citations (
                                item_id, ordinal, chunk_id, document_id, source_kind,
                                external_id, title, role, source_timestamp,
                                recorded_chunk_sha256, recorded_document_sha256,
                                model_visible_excerpt, check_result,
                                current_chunk_sha256, current_document_sha256, checked_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                item_id,
                                snapshot.ordinal,
                                snapshot.recorded.chunk_id,
                                snapshot.recorded.document_id,
                                reference.source_kind if reference else None,
                                reference.external_id if reference else None,
                                reference.title if reference else None,
                                reference.role if reference else None,
                                reference.timestamp if reference else None,
                                snapshot.recorded.chunk_sha256,
                                snapshot.recorded.document_sha256,
                                reference.excerpt if reference else None,
                                snapshot.current.check.value,
                                snapshot.current.current_chunk_sha256,
                                snapshot.current.current_document_sha256,
                                checked_at,
                            ),
                        )
                    connection.execute(
                        """
                        INSERT INTO review_events (
                            item_id, action, from_state, to_state, from_revision,
                            to_revision, text_version, reviewer, note, created_at
                        ) VALUES (?, 'IMPORTED', NULL, ?, NULL, 1, 1, NULL, ?, ?)
                        """,
                        (
                            item_id,
                            state.value,
                            f"evidence status {claim.evidence_status.value}",
                            now,
                        ),
                    )
        run = self.get_run(result.run_id)
        return created, run, self.list_items(run_id=result.run_id)

    def imported_run_ids(self) -> set[str]:
        with self._read() as connection:
            rows = connection.execute("SELECT run_id FROM review_runs").fetchall()
        return {str(row["run_id"]) for row in rows}

    # ----------------------------------------------------------------- reads

    def get_run(self, run_id: str) -> ReviewRunSummary:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM review_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise ReviewNotFoundError("review run was not found", run_id=run_id)
            return _run_summary(connection, row)

    def list_runs(self) -> list[ReviewRunSummary]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM review_runs ORDER BY imported_at DESC, run_id DESC"
            ).fetchall()
            return [_run_summary(connection, row) for row in rows]

    def list_items(
        self,
        *,
        state: ReviewState | None = None,
        evidence_status: EvidenceStatus | None = None,
        run_id: str | None = None,
        limit: int = MAX_LIST_LIMIT,
    ) -> list[ReviewItemSummary]:
        """Queue order: NEEDS_REVIEW, PENDING_REVIEW, decided; weakest evidence first."""

        if not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
        clauses: list[str] = []
        parameters: list[str] = []
        if state is not None:
            clauses.append("i.state = ?")
            parameters.append(state.value)
        if evidence_status is not None:
            clauses.append("i.evidence_status = ?")
            parameters.append(evidence_status.value)
        if run_id is not None:
            clauses.append("i.run_id = ?")
            parameters.append(run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._read() as connection:
            rows = connection.execute(
                f"{_ITEM_SELECT} {where} GROUP BY i.item_id",  # noqa: S608 - fixed clauses.
                parameters,
            ).fetchall()
        ordered = sorted(rows, key=lambda row: int(row["claim_index"]))
        ordered.sort(key=lambda row: str(row["imported_at"]), reverse=True)
        ordered.sort(
            key=lambda row: queue_priority(
                ReviewState(str(row["state"])), EvidenceStatus(str(row["evidence_status"]))
            )
        )
        return [_item_summary(row) for row in ordered[:limit]]

    def get_item(self, item_id: str) -> ReviewItemDetail:
        with self._read() as connection:
            return _item_detail(connection, item_id)

    def diff(
        self, item_id: str, *, from_version: int | None = None, to_version: int | None = None
    ) -> VersionDiff:
        with self._read() as connection:
            item = connection.execute(
                "SELECT current_version FROM review_items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if item is None:
                raise ReviewNotFoundError("review item was not found", item_id=item_id)
            selected_from = 1 if from_version is None else from_version
            selected_to = int(item["current_version"]) if to_version is None else to_version
            texts: dict[int, str] = {}
            for version in (selected_from, selected_to):
                row = connection.execute(
                    "SELECT text FROM review_text_versions WHERE item_id = ? AND version = ?",
                    (item_id, version),
                ).fetchone()
                if row is None:
                    raise VersionNotFoundError(
                        f"text version {version} does not exist for this item",
                        item_id=item_id,
                        version=version,
                    )
                texts[version] = str(row["text"])
        return VersionDiff(
            item_id=item_id,
            from_version=selected_from,
            to_version=selected_to,
            from_text=texts[selected_from],
            to_text=texts[selected_to],
            ops=diff_text(texts[selected_from], texts[selected_to]),
        )

    # ---------------------------------------------------------------- writes

    def apply_action(
        self,
        item_id: str,
        action: ReviewAction,
        *,
        expected_revision: int,
        reviewer: str,
        note: str,
        text: str | None = None,
    ) -> ReviewItemDetail:
        if action is ReviewAction.CONFIRM:
            self._require_evidence_unchanged([item_id])
        with self._write() as connection:
            self._apply_in_transaction(
                connection,
                item_id,
                action,
                expected_revision=expected_revision,
                reviewer=reviewer,
                note=note,
                text=text,
            )
        return self.get_item(item_id)

    def confirm_batch(
        self, entries: Sequence[BatchConfirmEntry], *, reviewer: str, note: str
    ) -> list[ReviewItemSummary]:
        """Confirm several items atomically; only fully verified pending items qualify."""

        item_ids = [entry.item_id for entry in entries]
        if len(item_ids) != len(set(item_ids)):
            raise NoChangeError("a batch must not name the same item twice")
        self._require_evidence_unchanged(item_ids)
        with self._write() as connection:
            for entry in entries:
                row = _load_item_row(connection, entry.item_id)
                state = ReviewState(str(row["state"]))
                evidence_status = EvidenceStatus(str(row["evidence_status"]))
                if int(row["revision"]) != entry.expected_revision:
                    raise _stale(entry.item_id, row)
                if not (
                    state is ReviewState.PENDING_REVIEW
                    and evidence_status is EvidenceStatus.FULLY_VERIFIED
                ):
                    raise LowConfidenceBatchError(
                        "batch confirmation accepts only fully verified pending items; "
                        "review this item individually",
                        item_id=entry.item_id,
                        state=state.value,
                        evidence_status=evidence_status.value,
                    )
                self._apply_in_transaction(
                    connection,
                    entry.item_id,
                    ReviewAction.CONFIRM,
                    expected_revision=entry.expected_revision,
                    reviewer=reviewer,
                    note=note,
                    text=None,
                )
        with self._read() as connection:
            placeholders = ", ".join("?" for _ in item_ids)
            rows = connection.execute(
                f"{_ITEM_SELECT} WHERE i.item_id IN ({placeholders}) GROUP BY i.item_id",  # noqa: S608
                item_ids,
            ).fetchall()
        summaries = {str(row["item_id"]): _item_summary(row) for row in rows}
        return [summaries[item_id] for item_id in item_ids]

    def _apply_in_transaction(
        self,
        connection: sqlite3.Connection,
        item_id: str,
        action: ReviewAction,
        *,
        expected_revision: int,
        reviewer: str,
        note: str,
        text: str | None,
    ) -> None:
        row = _load_item_row(connection, item_id)
        revision = int(row["revision"])
        if revision != expected_revision:
            raise _stale(item_id, row)
        state = ReviewState(str(row["state"]))
        evidence_status = EvidenceStatus(str(row["evidence_status"]))
        target = plan_transition(state, action, evidence_status, note=note)
        now = _now()
        current_version = int(row["current_version"])
        if action is ReviewAction.CORRECT:
            if text is None:
                raise NoChangeError("a correction requires replacement text")
            current_text = connection.execute(
                "SELECT text FROM review_text_versions WHERE item_id = ? AND version = ?",
                (item_id, current_version),
            ).fetchone()
            if current_text is not None and str(current_text["text"]) == text:
                raise NoChangeError("the correction is identical to the current text")
            current_version += 1
            connection.execute(
                """
                INSERT INTO review_text_versions (
                    item_id, version, text, text_sha256, author_kind, reviewer, note, created_at
                ) VALUES (?, ?, ?, ?, 'REVIEWER', ?, ?, ?)
                """,
                (item_id, current_version, text, _sha256_text(text), reviewer, note, now),
            )
        updated = connection.execute(
            """
            UPDATE review_items
            SET state = ?, revision = revision + 1, current_version = ?, updated_at = ?
            WHERE item_id = ? AND revision = ?
            """,
            (target.value, current_version, now, item_id, expected_revision),
        )
        if updated.rowcount != 1:
            raise _stale(item_id, _load_item_row(connection, item_id))
        connection.execute(
            """
            INSERT INTO review_events (
                item_id, action, from_state, to_state, from_revision, to_revision,
                text_version, reviewer, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                _ACTION_EVENTS[action].value,
                state.value,
                target.value,
                revision,
                revision + 1,
                current_version,
                reviewer,
                note or None,
                now,
            ),
        )

    def _require_evidence_unchanged(self, item_ids: Sequence[str]) -> None:
        """Refuse confirmation if any citation is weaker now than when it was imported.

        The import snapshot is immutable, so a source re-synced after import would
        otherwise be confirmed against stale lineage.  Improvement is not an error.
        """

        with self._read() as connection:
            placeholders = ", ".join("?" for _ in item_ids)
            rows = connection.execute(
                f"""
                SELECT item_id, chunk_id, document_id, recorded_chunk_sha256,
                       recorded_document_sha256, check_result
                FROM review_citations WHERE item_id IN ({placeholders})
                """,  # noqa: S608 - placeholders contain only generated question marks.
                list(item_ids),
            ).fetchall()
        recorded = [row for row in rows if row["recorded_chunk_sha256"] is not None]
        if not recorded:
            return
        current = check_current_citations(
            self.data_root,
            [
                RecordedCitation(
                    chunk_id=str(row["chunk_id"]),
                    document_id=row["document_id"],
                    chunk_sha256=row["recorded_chunk_sha256"],
                    document_sha256=row["recorded_document_sha256"],
                )
                for row in recorded
            ],
        )
        for row in recorded:
            imported_check = CitationCheck(str(row["check_result"]))
            now_check = current[str(row["chunk_id"])].check
            if CHECK_RANK[now_check] < CHECK_RANK[imported_check]:
                raise EvidenceChangedError(
                    "a cited source changed after this item was imported; correct or "
                    "reject it instead of confirming the model text",
                    item_id=str(row["item_id"]),
                    chunk_id=str(row["chunk_id"]),
                    imported_check=imported_check.value,
                    current_check=now_check.value,
                )

    # --------------------------------------------------------------- storage

    def _prepare_storage(self) -> None:
        for path in (self.data_root, self.review_root):
            _reject_symlink(path, expect_directory=True)
        self.data_root.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
        self.review_root.mkdir(mode=_DIRECTORY_MODE, exist_ok=True)
        _ensure_mode(self.review_root, _DIRECTORY_MODE)
        _reject_symlink(self.database_path, expect_directory=False)
        if not self.database_path.exists():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            os.close(os.open(self.database_path, flags, _FILE_MODE))
        _ensure_mode(self.database_path, _FILE_MODE)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        _reject_symlink(self.database_path, expect_directory=False)
        try:
            connection = sqlite3.connect(self.database_path, timeout=5.0, isolation_level=None)
        except sqlite3.Error:
            raise ReviewStoreError("review store could not be opened") from None
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA secure_delete = ON")
            yield connection
        finally:
            connection.close()
            _ensure_mode(self.database_path, _FILE_MODE)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error:
                raise ReviewStoreError("review store is busy; retry the action") from None
            try:
                yield connection
                connection.execute("COMMIT")
            except ReviewError:
                connection.execute("ROLLBACK")
                raise
            except sqlite3.IntegrityError as error:
                connection.execute("ROLLBACK")
                raise ReviewStoreError(
                    "review store rejected a write that violates a stored invariant"
                ) from error
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            connection.execute("BEGIN DEFERRED")
            try:
                yield connection
            finally:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                objects = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                    ).fetchone()[0]
                )
                if objects:
                    raise ReviewStoreError("unversioned review store is not empty")
                script = "\n".join(_migration_sql(name) for name in _MIGRATIONS)
                try:
                    connection.executescript(
                        f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {SCHEMA_VERSION};\n"
                        "COMMIT;"
                    )
                except sqlite3.Error:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise ReviewStoreError("review store schema could not be created") from None
            elif version != SCHEMA_VERSION:
                raise ReviewStoreError("review store schema version is not supported")
            tables = {
                str(row["name"])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if not _REQUIRED_TABLES.issubset(tables):
                raise ReviewStoreError("review store schema is incomplete")


def item_id_for(run_id: str, claim_index: int) -> str:
    """Stable, readable identity: re-importing the same run maps to the same items."""

    return f"{run_id}.c{claim_index}"


_ITEM_SELECT = """
SELECT i.item_id, i.run_id, i.claim_index, i.state, i.evidence_status, i.revision,
       i.current_version, i.updated_at, r.imported_at, v.text AS current_text,
       COUNT(c.ordinal) AS citation_count,
       COALESCE(SUM(CASE WHEN c.check_result = 'VERIFIED' THEN 1 ELSE 0 END), 0)
           AS verified_citation_count
FROM review_items AS i
JOIN review_runs AS r ON r.run_id = i.run_id
JOIN review_text_versions AS v
    ON v.item_id = i.item_id AND v.version = i.current_version
LEFT JOIN review_citations AS c ON c.item_id = i.item_id
"""


def _load_item_row(connection: sqlite3.Connection, item_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM review_items WHERE item_id = ?", (item_id,)).fetchone()
    if row is None:
        raise ReviewNotFoundError("review item was not found", item_id=item_id)
    return cast(sqlite3.Row, row)


def _stale(item_id: str, row: sqlite3.Row) -> StaleRevisionError:
    return StaleRevisionError(
        "this item changed since it was loaded; reload before acting",
        item_id=item_id,
        current_revision=int(row["revision"]),
        current_state=str(row["state"]),
    )


def _item_summary(row: sqlite3.Row) -> ReviewItemSummary:
    state = ReviewState(str(row["state"]))
    evidence_status = EvidenceStatus(str(row["evidence_status"]))
    return ReviewItemSummary(
        item_id=str(row["item_id"]),
        run_id=str(row["run_id"]),
        claim_index=int(row["claim_index"]),
        state=state,
        evidence_status=evidence_status,
        revision=int(row["revision"]),
        current_version=int(row["current_version"]),
        current_text=str(row["current_text"]),
        citation_count=int(row["citation_count"]),
        verified_citation_count=int(row["verified_citation_count"]),
        confirm_requirement=confirm_requirement(state, evidence_status),
        allowed_actions=allowed_actions(state, evidence_status),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _item_detail(connection: sqlite3.Connection, item_id: str) -> ReviewItemDetail:
    row = connection.execute(
        f"{_ITEM_SELECT} WHERE i.item_id = ? GROUP BY i.item_id", (item_id,)
    ).fetchone()
    if row is None:
        raise ReviewNotFoundError("review item was not found", item_id=item_id)
    run_row = connection.execute(
        "SELECT * FROM review_runs WHERE run_id = ?", (str(row["run_id"]),)
    ).fetchone()
    versions = [
        TextVersion(
            version=int(version["version"]),
            text=str(version["text"]),
            text_sha256=str(version["text_sha256"]),
            author_kind=AuthorKind(str(version["author_kind"])),
            reviewer=version["reviewer"],
            note=version["note"],
            created_at=datetime.fromisoformat(str(version["created_at"])),
        )
        for version in connection.execute(
            "SELECT * FROM review_text_versions WHERE item_id = ? ORDER BY version",
            (item_id,),
        )
    ]
    citations = [
        ReviewCitation(
            ordinal=int(citation["ordinal"]),
            chunk_id=str(citation["chunk_id"]),
            document_id=citation["document_id"],
            source_kind=citation["source_kind"],
            external_id=citation["external_id"],
            title=citation["title"],
            role=citation["role"],
            source_timestamp=citation["source_timestamp"],
            recorded_chunk_sha256=citation["recorded_chunk_sha256"],
            recorded_document_sha256=citation["recorded_document_sha256"],
            model_visible_excerpt=citation["model_visible_excerpt"],
            check=CitationCheck(str(citation["check_result"])),
            current_chunk_sha256=citation["current_chunk_sha256"],
            current_document_sha256=citation["current_document_sha256"],
            document_changed=(
                citation["current_document_sha256"] is not None
                and citation["current_document_sha256"] != citation["recorded_document_sha256"]
            ),
            checked_at=datetime.fromisoformat(str(citation["checked_at"])),
        )
        for citation in connection.execute(
            "SELECT * FROM review_citations WHERE item_id = ? ORDER BY ordinal", (item_id,)
        )
    ]
    events = [
        ReviewEvent(
            event_id=int(event["event_id"]),
            action=EventAction(str(event["action"])),
            from_state=ReviewState(str(event["from_state"])) if event["from_state"] else None,
            to_state=ReviewState(str(event["to_state"])),
            from_revision=event["from_revision"],
            to_revision=int(event["to_revision"]),
            text_version=int(event["text_version"]),
            reviewer=event["reviewer"],
            note=event["note"],
            created_at=datetime.fromisoformat(str(event["created_at"])),
        )
        for event in connection.execute(
            "SELECT * FROM review_events WHERE item_id = ? ORDER BY event_id", (item_id,)
        )
    ]
    return ReviewItemDetail(
        item=_item_summary(row),
        run=_run_summary(connection, run_row),
        original_text=versions[0].text,
        citations=citations,
        versions=versions,
        events=events,
    )


def _run_summary(connection: sqlite3.Connection, row: sqlite3.Row) -> ReviewRunSummary:
    counts = {
        str(count["state"]): int(count["total"])
        for count in connection.execute(
            "SELECT state, COUNT(*) AS total FROM review_items WHERE run_id = ? GROUP BY state",
            (str(row["run_id"]),),
        )
    }
    return ReviewRunSummary(
        run_id=str(row["run_id"]),
        question=str(row["question"]),
        model=str(row["model"]),
        prompt_version=str(row["prompt_version"]),
        draft_created_at=datetime.fromisoformat(str(row["draft_created_at"])),
        draft_sha256=str(row["draft_sha256"]),
        input_manifest_sha256=str(row["input_manifest_sha256"]),
        retrieval_manifest_sha256=str(row["retrieval_manifest_sha256"]),
        knowledge_index_checked=bool(row["knowledge_index_checked"]),
        imported_at=datetime.fromisoformat(str(row["imported_at"])),
        item_count=sum(counts.values()),
        state_counts={state.value: counts.get(state.value, 0) for state in ReviewState},
        gaps=ReviewRunGaps(
            unsupported=json.loads(str(row["unsupported_json"])),
            open_questions=json.loads(str(row["open_questions_json"])),
            limitations=json.loads(str(row["limitations_json"])),
        ),
    )


def _migration_sql(name: str) -> str:
    return (
        resources.files("soloscale")
        .joinpath("review_migrations")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


def _reject_symlink(path: Path, *, expect_directory: bool) -> None:
    if path.is_symlink():
        raise ReviewStoreError("review store paths must not be symlinks")
    if path.exists() and path.is_dir() != expect_directory:
        raise ReviewStoreError("review store path has an unexpected file type")


def _ensure_mode(path: Path, mode: int) -> None:
    if stat.S_IMODE(path.stat().st_mode) != mode:
        path.chmod(mode)


def _json_list(values: Sequence[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()
