from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from soloscale.knowledge_models import (
    ContentRole,
    KnowledgeCatalogChunk,
    KnowledgeCatalogDocument,
    KnowledgeCatalogSnapshot,
    KnowledgeStatus,
    ParsedSource,
    RetrievalHit,
    SourceFailure,
    SourceKind,
    SyncReport,
)

_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_SCHEMA_VERSION = 1
_RRF_K = 60
_MAX_SEARCH_LIMIT = 100
_MAX_DIRECT_CHUNKS = 200
_MAX_NEIGHBOR_RADIUS = 2
_MAX_CONTEXT_EXPANSIONS_PER_CHUNK = 4
_MAX_QUERY_CHARACTERS = 1_000
_MAX_QUERY_TOKENS = 32
_EXCERPT_LIMIT = 1200
_QUERY_TOKEN = re.compile(r"[^\W_]+", flags=re.UNICODE)
_CJK_TOKEN = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+$")


def _ensure_mode(path: Path, mode: int) -> None:
    """Avoid redundant metadata writes while preserving private-mode enforcement."""

    if stat.S_IMODE(path.stat().st_mode) != mode:
        path.chmod(mode)


class KnowledgeStoreError(Exception):
    """Base error for the local private knowledge store."""


class UnsafeKnowledgePathError(KnowledgeStoreError, OSError):
    """Raised before a managed symlink or unexpected file can be traversed."""


class InvalidKnowledgeQueryError(KnowledgeStoreError, ValueError):
    """Raised when a retrieval query has no searchable terms."""


class CorruptKnowledgeStoreError(KnowledgeStoreError, OSError):
    """Raised when the on-disk schema is incompatible or incomplete."""


class _SourceSyncError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class KnowledgeStore:
    """Single-writer SQLite/FTS store for private normalized conversation evidence."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.knowledge_root = self.root / "knowledge"
        self.database_path = self.knowledge_root / "index.sqlite3"
        self._prepare_storage()
        self._initialize_schema()

    def sync(self, sources: Sequence[ParsedSource]) -> SyncReport:
        """Import current source snapshots with one savepoint per source.

        Source bodies and exception text are deliberately excluded from failure receipts.
        A malformed source is rolled back without discarding unrelated valid sources.
        """

        discovered = len(sources)
        imported = 0
        updated = 0
        skipped = 0
        chunks_written = 0
        failures: list[SourceFailure] = []
        run_id = f"sync-{uuid4().hex}"
        started_at = _utc_now_text()

        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for index, source in enumerate(sources):
                        savepoint = f"source_{index}"
                        connection.execute(f"SAVEPOINT {savepoint}")
                        try:
                            disposition, written = self._sync_source(connection, source)
                        except Exception as error:
                            connection.execute(f"ROLLBACK TO {savepoint}")
                            connection.execute(f"RELEASE {savepoint}")
                            code = (
                                error.code
                                if isinstance(error, _SourceSyncError)
                                else "source-write-failed"
                            )
                            failures.append(
                                SourceFailure(
                                    source_locator=source.document.locator,
                                    code=code,
                                    source_kind=source.document.source_kind,
                                )
                            )
                            continue

                        connection.execute(f"RELEASE {savepoint}")
                        chunks_written += written
                        if disposition == "imported":
                            imported += 1
                        elif disposition == "updated":
                            updated += 1
                        else:
                            skipped += 1

                    completed_at = _utc_now_text()
                    connection.execute(
                        """
                        INSERT INTO sync_runs (
                            run_id, started_at, completed_at, discovered, imported,
                            updated, skipped, failed, documents, chunks_written
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            started_at,
                            completed_at,
                            discovered,
                            imported,
                            updated,
                            skipped,
                            len(failures),
                            imported + updated,
                            chunks_written,
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO sync_failures (
                            run_id, source_locator, source_kind, code, ordinal
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                run_id,
                                failure.source_locator,
                                failure.source_kind.value if failure.source_kind else None,
                                failure.code,
                                ordinal,
                            )
                            for ordinal, failure in enumerate(failures)
                        ],
                    )
                    connection.execute(
                        """
                        INSERT INTO store_meta (key, value) VALUES ('last_synced_at', ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (completed_at,),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except (UnsafeKnowledgePathError, CorruptKnowledgeStoreError):
            raise
        except sqlite3.Error:
            raise KnowledgeStoreError("knowledge sync failed") from None

        return SyncReport(
            discovered=discovered,
            imported=imported,
            updated=updated,
            skipped=skipped,
            failed=len(failures),
            documents=imported + updated,
            chunks_written=chunks_written,
            failures=failures,
        )

    def status(self) -> KnowledgeStatus:
        """Return metadata-only store counts and the last committed sync timestamp."""

        try:
            with self._connect() as connection:
                documents = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
                chunks = int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
                source_counts = {
                    str(row["source_kind"]): int(row["source_count"])
                    for row in connection.execute(
                        """
                        SELECT source_kind, COUNT(*) AS source_count
                        FROM documents
                        GROUP BY source_kind
                        ORDER BY source_kind
                        """
                    )
                }
                last_row = connection.execute(
                    "SELECT value FROM store_meta WHERE key = 'last_synced_at'"
                ).fetchone()
        except (UnsafeKnowledgePathError, CorruptKnowledgeStoreError):
            raise
        except sqlite3.Error:
            raise KnowledgeStoreError("knowledge status read failed") from None

        last_synced_at = None
        if last_row is not None:
            try:
                last_synced_at = datetime.fromisoformat(str(last_row["value"]))
            except ValueError:
                raise CorruptKnowledgeStoreError(
                    "knowledge store contains an invalid sync timestamp"
                ) from None
        return KnowledgeStatus(
            documents=documents,
            chunks=chunks,
            source_counts=source_counts,
            last_synced_at=last_synced_at,
        )

    def catalog_metadata(self) -> KnowledgeCatalogSnapshot:
        """Return a consistent metadata-only projection for downstream catalogs.

        This query intentionally does not select chunk bodies or retrieval excerpts.
        Native IDs and locators are private provenance metadata for local catalogs.
        """

        try:
            with self._connect() as connection:
                with _read_snapshot(connection):
                    document_rows = connection.execute(
                        """
                        SELECT document_id, source_kind, external_id, locator, parent_external_id,
                               title, content_sha256, byte_size, observed_at, metadata_json
                        FROM documents
                        ORDER BY document_id
                        """
                    ).fetchall()
                    chunk_rows = connection.execute(
                        """
                        SELECT chunk_id, document_id, ordinal, role, timestamp,
                               text_sha256, metadata_json
                        FROM chunks
                        ORDER BY document_id, ordinal, chunk_id
                        """
                    ).fetchall()
        except (UnsafeKnowledgePathError, CorruptKnowledgeStoreError):
            raise
        except sqlite3.Error:
            raise KnowledgeStoreError("knowledge catalog metadata read failed") from None

        try:
            return KnowledgeCatalogSnapshot(
                documents=[
                    KnowledgeCatalogDocument(
                        document_id=str(row["document_id"]),
                        native_id=str(row["external_id"]),
                        source_kind=SourceKind(str(row["source_kind"])),
                        project=(
                            str(row["parent_external_id"])
                            if row["parent_external_id"] is not None
                            else None
                        ),
                        locator=str(row["locator"]),
                        title=str(row["title"]) if row["title"] is not None else None,
                        content_sha256=str(row["content_sha256"]),
                        byte_size=int(row["byte_size"]),
                        observed_at=_parse_catalog_datetime(row["observed_at"]),
                        metadata_sha256=hashlib.sha256(
                            str(row["metadata_json"]).encode("utf-8")
                        ).hexdigest(),
                        metadata={
                            str(key): str(value)
                            for key, value in json.loads(str(row["metadata_json"])).items()
                            if isinstance(key, str) and isinstance(value, str)
                        },
                    )
                    for row in document_rows
                ],
                chunks=[
                    KnowledgeCatalogChunk(
                        chunk_id=str(row["chunk_id"]),
                        document_id=str(row["document_id"]),
                        ordinal=int(row["ordinal"]),
                        role=ContentRole(str(row["role"])),
                        timestamp=_parse_catalog_datetime(row["timestamp"]),
                        text_sha256=str(row["text_sha256"]),
                        metadata_sha256=hashlib.sha256(
                            str(row["metadata_json"]).encode("utf-8")
                        ).hexdigest(),
                    )
                    for row in chunk_rows
                ],
            )
        except (TypeError, ValueError):
            raise CorruptKnowledgeStoreError(
                "knowledge store contains invalid catalog metadata"
            ) from None

    def reset_index(self) -> None:
        """Delete only the derived SQLite index; preserve private agent-run receipts."""

        type(self).reset_derived_index(self.root)

    @classmethod
    def reset_derived_index(cls, root: Path) -> KnowledgeStore:
        """Recreate the derived index even when the existing SQLite file is corrupt."""

        managed_root = Path(root)
        knowledge_root = managed_root / "knowledge"
        database_path = knowledge_root / "index.sqlite3"
        sidecars = tuple(
            Path(f"{database_path}{suffix}") for suffix in ("-journal", "-wal", "-shm")
        )
        try:
            _reject_symlink_or_wrong_type(managed_root, expected_directory=True)
            managed_root.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
            _reject_symlink_or_wrong_type(managed_root, expected_directory=True)
            managed_root.chmod(_PRIVATE_DIRECTORY_MODE)

            _reject_symlink_or_wrong_type(knowledge_root, expected_directory=True)
            knowledge_root.mkdir(mode=_PRIVATE_DIRECTORY_MODE, exist_ok=True)
            _reject_symlink_or_wrong_type(knowledge_root, expected_directory=True)
            knowledge_root.chmod(_PRIVATE_DIRECTORY_MODE)

            for managed_file in (*sidecars, database_path):
                _reject_symlink_or_wrong_type(managed_file, expected_directory=False)
                if managed_file.exists():
                    managed_file.unlink()
            return cls(managed_root)
        except (UnsafeKnowledgePathError, CorruptKnowledgeStoreError):
            raise
        except OSError:
            raise KnowledgeStoreError("knowledge index reset failed") from None

    def search(
        self,
        query: str,
        limit: int = 10,
        source_kinds: Sequence[SourceKind] | None = None,
    ) -> list[RetrievalHit]:
        """Search through safe FTS and exact/alias channels, then fuse with RRF."""

        if isinstance(limit, bool) or not 1 <= limit <= _MAX_SEARCH_LIMIT:
            raise ValueError(f"limit must be between 1 and {_MAX_SEARCH_LIMIT}")
        normalized_query = " ".join(query.split())
        if len(normalized_query) > _MAX_QUERY_CHARACTERS:
            raise InvalidKnowledgeQueryError("query exceeds the local search size limit")
        lexical_tokens = _lexical_query_tokens(normalized_query)
        if not lexical_tokens:
            raise InvalidKnowledgeQueryError("query must contain a searchable term")
        if len(lexical_tokens) > _MAX_QUERY_TOKENS:
            raise InvalidKnowledgeQueryError("query contains too many searchable terms")
        tokens = _expanded_query_tokens(lexical_tokens)

        normalized_kinds = _normalize_source_kinds(source_kinds)
        if normalized_kinds == ():
            return []
        candidate_limit = min(max(limit * 8, 40), 400)
        fts_query = " OR ".join(f'"{token}"' for token in tokens)

        try:
            with self._connect() as connection:
                with _read_snapshot(connection):
                    fts_rows = self._fts_candidates(
                        connection,
                        fts_query=fts_query,
                        source_kinds=normalized_kinds,
                        candidate_limit=candidate_limit,
                    )
                    exact_rows = self._exact_candidates(
                        connection,
                        normalized_query=normalized_query,
                        tokens=tokens,
                        source_kinds=normalized_kinds,
                        candidate_limit=candidate_limit,
                    )
                    ranked = _reciprocal_rank_fusion(fts_rows, exact_rows)
                    ranked_ids = [chunk_id for chunk_id, _score, _channels in ranked]
                    detail_rows = self._load_chunk_rows(connection, ranked_ids)
        except (UnsafeKnowledgePathError, CorruptKnowledgeStoreError):
            raise
        except sqlite3.Error:
            raise KnowledgeStoreError("knowledge search failed") from None

        details = {str(row["chunk_id"]): row for row in detail_rows}
        hits: list[RetrievalHit] = []
        seen_text_projections: set[tuple[str, str, str]] = set()
        for chunk_id, score, channels in ranked:
            row = details.get(chunk_id)
            if row is None:
                raise CorruptKnowledgeStoreError(
                    "a retrieval index entry does not resolve to a stored chunk"
                )
            text_projection = (
                str(row["text_sha256"]),
                str(row["role"]),
                str(row["source_kind"]),
            )
            if text_projection in seen_text_projections:
                continue
            seen_text_projections.add(text_projection)
            hits.append(
                self._retrieval_hit(
                    row,
                    score=score,
                    channels=channels,
                    query_tokens=tokens,
                )
            )
            if len(hits) == limit:
                break
        return hits

    def get_chunks(self, ids: Sequence[str]) -> list[RetrievalHit]:
        """Resolve chunk identifiers directly while preserving caller order."""

        unique_ids = list(dict.fromkeys(ids))
        if len(unique_ids) > _MAX_DIRECT_CHUNKS:
            raise ValueError(f"at most {_MAX_DIRECT_CHUNKS} chunk ids may be resolved")
        if not unique_ids:
            return []
        if any(not chunk_id or "\x00" in chunk_id for chunk_id in unique_ids):
            raise ValueError("chunk ids must be nonblank and cannot contain NUL")

        try:
            with self._connect() as connection:
                rows = self._load_chunk_rows(connection, unique_ids)
        except (UnsafeKnowledgePathError, CorruptKnowledgeStoreError):
            raise
        except sqlite3.Error:
            raise KnowledgeStoreError("knowledge chunk read failed") from None

        by_id = {str(row["chunk_id"]): row for row in rows}
        return [
            self._retrieval_hit(
                by_id[chunk_id],
                score=1.0,
                channels=("direct",),
                query_tokens=(),
            )
            for chunk_id in unique_ids
            if chunk_id in by_id
        ]

    def get_neighbors(
        self,
        ids: Sequence[str],
        *,
        radius: int = 1,
    ) -> list[RetrievalHit]:
        """Resolve adjacent chunks without turning neighbor expansion into a new search tool."""

        unique_ids = list(dict.fromkeys(ids))
        if len(unique_ids) > _MAX_DIRECT_CHUNKS:
            raise ValueError(f"at most {_MAX_DIRECT_CHUNKS} chunk ids may be expanded")
        if isinstance(radius, bool) or not 1 <= radius <= _MAX_NEIGHBOR_RADIUS:
            raise ValueError(f"neighbor radius must be between 1 and {_MAX_NEIGHBOR_RADIUS}")
        if not unique_ids:
            return []
        if any(not chunk_id or "\x00" in chunk_id for chunk_id in unique_ids):
            raise ValueError("chunk ids must be nonblank and cannot contain NUL")

        try:
            with self._connect() as connection:
                with _read_snapshot(connection):
                    targets = self._load_chunk_rows(connection, unique_ids)
                    target_by_id = {str(row["chunk_id"]): row for row in targets}
                    neighbor_ids: list[str] = []
                    seen = set(unique_ids)
                    for chunk_id in unique_ids:
                        target = target_by_id.get(chunk_id)
                        if target is None:
                            continue
                        self._retrieval_hit(
                            target,
                            score=1.0,
                            channels=("direct",),
                            query_tokens=(),
                        )
                        document_rows = list(
                            connection.execute(
                                """
                        SELECT chunk_id, ordinal, metadata_json
                        FROM chunks
                        WHERE document_id = ?
                        ORDER BY ordinal ASC, chunk_id ASC
                        """,
                                (str(target["document_id"]),),
                            )
                        )
                        target_metadata = _chunk_metadata(target["metadata_json"])
                        candidates = _context_neighbor_ids(
                            target_id=chunk_id,
                            target_ordinal=int(target["ordinal"]),
                            target_role=ContentRole(str(target["role"])),
                            source_kind=SourceKind(str(target["source_kind"])),
                            target_metadata=target_metadata,
                            rows=document_rows,
                            radius=radius,
                        )
                        for neighbor_id in candidates[:_MAX_CONTEXT_EXPANSIONS_PER_CHUNK]:
                            if neighbor_id in seen:
                                continue
                            seen.add(neighbor_id)
                            neighbor_ids.append(neighbor_id)
                    details = self._load_chunk_rows(connection, neighbor_ids)
        except (UnsafeKnowledgePathError, CorruptKnowledgeStoreError):
            raise
        except sqlite3.Error:
            raise KnowledgeStoreError("knowledge neighbor read failed") from None

        by_id = {str(row["chunk_id"]): row for row in details}
        return [
            self._retrieval_hit(
                by_id[chunk_id],
                score=0.0,
                channels=("neighbor",),
                query_tokens=(),
            )
            for chunk_id in neighbor_ids
            if chunk_id in by_id
        ]

    def _sync_source(
        self,
        connection: sqlite3.Connection,
        source: ParsedSource,
    ) -> tuple[str, int]:
        document = source.document
        self._validate_source_hashes(source)
        identity_row = connection.execute(
            """
            SELECT document_id, content_sha256
            FROM documents
            WHERE source_kind = ? AND external_id = ?
            """,
            (document.source_kind.value, document.external_id),
        ).fetchone()
        id_row = connection.execute(
            """
            SELECT source_kind, external_id
            FROM documents
            WHERE document_id = ?
            """,
            (document.id,),
        ).fetchone()

        if id_row is not None and (
            str(id_row["source_kind"]) != document.source_kind.value
            or str(id_row["external_id"]) != document.external_id
        ):
            raise _SourceSyncError("document-id-conflict")
        if identity_row is not None and str(identity_row["document_id"]) != document.id:
            raise _SourceSyncError("document-identity-conflict")

        aliases = _searchable_metadata(source)
        metadata_json = _canonical_json(document.metadata)
        observed_at = _datetime_text(document.observed_at)
        now = _utc_now_text()

        if (
            identity_row is not None
            and str(identity_row["content_sha256"]) == document.content_sha256
            and self._stored_chunks_match(connection, source)
        ):
            connection.execute(
                """
                UPDATE documents
                SET locator = ?, title = ?, byte_size = ?, observed_at = ?,
                    parent_external_id = ?, metadata_json = ?, aliases = ?,
                    last_seen_at = ?
                WHERE document_id = ?
                """,
                (
                    document.locator,
                    document.title,
                    document.byte_size,
                    observed_at,
                    document.parent_external_id,
                    metadata_json,
                    aliases,
                    now,
                    document.id,
                ),
            )
            for chunk in source.chunks:
                connection.execute(
                    "UPDATE chunk_fts SET title = ?, aliases = ? WHERE chunk_id = ?",
                    ("", "", chunk.id),
                )
            return "skipped", 0

        if identity_row is None:
            connection.execute(
                """
                INSERT INTO documents (
                    document_id, source_kind, external_id, locator, title,
                    content_sha256, byte_size, observed_at, parent_external_id,
                    metadata_json, aliases, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document.id,
                    document.source_kind.value,
                    document.external_id,
                    document.locator,
                    document.title,
                    document.content_sha256,
                    document.byte_size,
                    observed_at,
                    document.parent_external_id,
                    metadata_json,
                    aliases,
                    now,
                    now,
                ),
            )
            disposition = "imported"
        else:
            connection.execute(
                """
                DELETE FROM chunk_fts
                WHERE document_id = ?
                   OR chunk_id IN (
                       SELECT chunk_id FROM chunks WHERE document_id = ?
                   )
                """,
                (document.id, document.id),
            )
            connection.execute("DELETE FROM chunks WHERE document_id = ?", (document.id,))
            connection.execute(
                """
                UPDATE documents
                SET locator = ?, title = ?, content_sha256 = ?, byte_size = ?,
                    observed_at = ?, parent_external_id = ?, metadata_json = ?,
                    aliases = ?, last_seen_at = ?
                WHERE document_id = ?
                """,
                (
                    document.locator,
                    document.title,
                    document.content_sha256,
                    document.byte_size,
                    observed_at,
                    document.parent_external_id,
                    metadata_json,
                    aliases,
                    now,
                    document.id,
                ),
            )
            disposition = "updated"

        self._insert_chunks(connection, source)
        return disposition, len(source.chunks)

    def _insert_chunks(
        self,
        connection: sqlite3.Connection,
        source: ParsedSource,
    ) -> None:
        document = source.document
        for chunk in source.chunks:
            collision = connection.execute(
                "SELECT document_id FROM chunks WHERE chunk_id = ?",
                (chunk.id,),
            ).fetchone()
            if collision is not None:
                raise _SourceSyncError("chunk-id-conflict")
            connection.execute(
                """
                INSERT INTO chunks (
                    chunk_id, document_id, ordinal, role, timestamp, body,
                    text_sha256, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk.id,
                    document.id,
                    chunk.ordinal,
                    chunk.role.value,
                    _datetime_text(chunk.timestamp),
                    chunk.text,
                    chunk.text_sha256,
                    _canonical_json(chunk.metadata),
                ),
            )
            connection.execute(
                """
                INSERT INTO chunk_fts (chunk_id, document_id, body, title, aliases)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chunk.id, document.id, chunk.text, "", ""),
            )

    def _validate_source_hashes(self, source: ParsedSource) -> None:
        for chunk in source.chunks:
            actual = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
            if actual != chunk.text_sha256:
                raise _SourceSyncError("chunk-hash-mismatch")

    def _stored_chunks_match(
        self,
        connection: sqlite3.Connection,
        source: ParsedSource,
    ) -> bool:
        stored = [
            (
                str(row["chunk_id"]),
                int(row["ordinal"]),
                str(row["role"]),
                str(row["timestamp"]) if row["timestamp"] is not None else None,
                str(row["text_sha256"]),
                hashlib.sha256(str(row["body"]).encode("utf-8")).hexdigest(),
                str(row["metadata_json"]),
                int(row["fts_row_count"]),
                str(row["fts_document_id"]) if row["fts_document_id"] is not None else None,
                (
                    hashlib.sha256(str(row["fts_body"]).encode("utf-8")).hexdigest()
                    if row["fts_body"] is not None
                    else None
                ),
                str(row["fts_title"]) if row["fts_title"] is not None else None,
                str(row["fts_aliases"]) if row["fts_aliases"] is not None else None,
            )
            for row in connection.execute(
                """
                SELECT c.chunk_id, c.ordinal, c.role, c.timestamp,
                       c.body, c.text_sha256, c.metadata_json,
                       f.document_id AS fts_document_id, f.body AS fts_body,
                       f.title AS fts_title, f.aliases AS fts_aliases,
                       COUNT(f.rowid) OVER (PARTITION BY c.chunk_id) AS fts_row_count
                FROM chunks AS c
                LEFT JOIN chunk_fts AS f ON f.chunk_id = c.chunk_id
                WHERE c.document_id = ?
                ORDER BY c.ordinal
                """,
                (source.document.id,),
            )
        ]
        supplied = [
            (
                chunk.id,
                chunk.ordinal,
                chunk.role.value,
                _datetime_text(chunk.timestamp),
                chunk.text_sha256,
                chunk.text_sha256,
                _canonical_json(chunk.metadata),
                1,
                source.document.id,
                chunk.text_sha256,
                "",
                "",
            )
            for chunk in source.chunks
        ]
        return stored == supplied

    def _fts_candidates(
        self,
        connection: sqlite3.Connection,
        *,
        fts_query: str,
        source_kinds: tuple[SourceKind, ...] | None,
        candidate_limit: int,
    ) -> list[str]:
        filter_sql, filter_values = _source_filter_sql(source_kinds, alias="d")
        rows = connection.execute(
            f"""
            WITH matched AS (
                SELECT f.chunk_id, bm25(chunk_fts) AS relevance,
                       d.document_id, c.ordinal, c.text_sha256,
                       c.role, d.source_kind
                FROM chunk_fts AS f
                JOIN chunks AS c ON c.chunk_id = f.chunk_id
                JOIN documents AS d ON d.document_id = c.document_id
                WHERE chunk_fts MATCH ? {filter_sql}
            ), distinct_text AS (
                SELECT chunk_id, relevance, document_id, ordinal,
                       ROW_NUMBER() OVER (
                           PARTITION BY text_sha256, role, source_kind
                           ORDER BY relevance ASC, document_id ASC,
                                    ordinal ASC, chunk_id ASC
                       ) AS text_rank
                FROM matched
            )
            SELECT chunk_id, relevance, document_id, ordinal
            FROM distinct_text
            WHERE text_rank = 1
            ORDER BY relevance ASC, document_id ASC, ordinal ASC, chunk_id ASC
            LIMIT ?
            """,  # noqa: S608 - filter_sql is generated only from a fixed internal template.
            (fts_query, *filter_values, candidate_limit),
        )
        return _unique_chunk_ids(rows)

    def _exact_candidates(
        self,
        connection: sqlite3.Connection,
        *,
        normalized_query: str,
        tokens: tuple[str, ...],
        source_kinds: tuple[SourceKind, ...] | None,
        candidate_limit: int,
    ) -> list[str]:
        filter_sql, filter_values = _source_filter_sql(source_kinds, alias="d")
        lowered = normalized_query.casefold()
        body_token_score = " + ".join(
            "CASE WHEN instr(body_blob, ?) > 0 THEN 1 ELSE 0 END" for _token in tokens
        )
        metadata_token_score = " + ".join(
            "CASE WHEN instr(metadata_blob, ?) > 0 THEN 1 ELSE 0 END" for _token in tokens
        )
        rows = connection.execute(
            f"""
            WITH base AS (
                SELECT c.chunk_id, d.document_id, c.ordinal, c.text_sha256,
                       c.role, d.source_kind,
                       lower(d.external_id) AS external_id,
                       lower(COALESCE(d.title, '')) AS title,
                       lower(c.body) AS body_blob,
                       lower(d.external_id || ' ' || COALESCE(d.title, '') || ' ' || d.aliases)
                           AS metadata_blob,
                       ROW_NUMBER() OVER (
                           PARTITION BY d.document_id
                           ORDER BY c.ordinal ASC, c.chunk_id ASC
                       ) AS document_rank
                FROM chunks AS c
                JOIN documents AS d ON d.document_id = c.document_id
                WHERE 1 = 1 {filter_sql}
            ), scored AS (
                SELECT chunk_id, document_id, ordinal, text_sha256, role, source_kind,
                    document_rank,
                    CASE
                        WHEN external_id = ? THEN 0
                        WHEN title = ? THEN 1
                        WHEN instr(body_blob, ?) > 0 THEN 2
                        WHEN instr(metadata_blob, ?) > 0 THEN 3
                        ELSE 4
                    END AS phrase_rank,
                    ({body_token_score}) AS body_matches,
                    ({metadata_token_score}) AS metadata_matches
                FROM base
            ), eligible AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY text_sha256, role, source_kind
                           ORDER BY phrase_rank ASC, body_matches DESC,
                                    metadata_matches DESC, document_id ASC,
                                    ordinal ASC, chunk_id ASC
                       ) AS text_rank
                FROM scored
                WHERE phrase_rank = 2 OR body_matches > 0
                   OR (document_rank = 1 AND (phrase_rank < 2 OR phrase_rank = 3
                                              OR metadata_matches > 0))
            )
            SELECT chunk_id, document_id, ordinal
            FROM eligible
            WHERE text_rank = 1
            ORDER BY phrase_rank ASC, body_matches DESC, metadata_matches DESC,
                     document_id ASC, ordinal ASC, chunk_id ASC
            LIMIT ?
            """,  # noqa: S608 - filter_sql is generated only from a fixed internal template.
            (
                *filter_values,
                lowered,
                lowered,
                lowered,
                lowered,
                *(token.casefold() for token in tokens),
                *(token.casefold() for token in tokens),
                candidate_limit,
            ),
        )
        return _unique_chunk_ids(rows)

    def _load_chunk_rows(
        self,
        connection: sqlite3.Connection,
        chunk_ids: Sequence[str],
    ) -> list[sqlite3.Row]:
        if not chunk_ids:
            return []
        placeholders = ", ".join("?" for _ in chunk_ids)
        return list(
            connection.execute(
                f"""
                SELECT c.chunk_id, c.document_id, c.ordinal, c.role, c.timestamp,
                       c.body, c.text_sha256, c.metadata_json,
                       d.source_kind, d.external_id,
                       d.locator, d.title, d.content_sha256,
                       d.aliases AS document_aliases,
                       f.document_id AS indexed_document_id,
                       f.body AS indexed_body, f.title AS indexed_title,
                       f.aliases AS indexed_aliases,
                       COUNT(f.rowid) OVER (PARTITION BY c.chunk_id) AS fts_row_count
                FROM chunks AS c
                JOIN documents AS d ON d.document_id = c.document_id
                LEFT JOIN chunk_fts AS f ON f.chunk_id = c.chunk_id
                WHERE c.chunk_id IN ({placeholders})
                """,  # noqa: S608 - placeholders contain only generated question marks.
                tuple(chunk_ids),
            )
        )

    def _retrieval_hit(
        self,
        row: sqlite3.Row,
        *,
        score: float,
        channels: tuple[str, ...],
        query_tokens: tuple[str, ...],
    ) -> RetrievalHit:
        body = str(row["body"])
        stored_hash = str(row["text_sha256"])
        document_id = str(row["document_id"])
        indexed_document_id = row["indexed_document_id"]
        indexed_body = row["indexed_body"]
        indexed_title = row["indexed_title"]
        indexed_aliases = row["indexed_aliases"]
        if (
            hashlib.sha256(body.encode("utf-8")).hexdigest() != stored_hash
            or int(row["fts_row_count"]) != 1
            or indexed_document_id is None
            or str(indexed_document_id) != document_id
            or indexed_body is None
            or str(indexed_body) != body
            or indexed_title is None
            or str(indexed_title) != ""
            or indexed_aliases is None
            or str(indexed_aliases) != ""
        ):
            raise CorruptKnowledgeStoreError(
                "knowledge chunk projection failed integrity verification"
            )
        timestamp = None
        if row["timestamp"] is not None:
            try:
                timestamp = datetime.fromisoformat(str(row["timestamp"]))
            except ValueError:
                raise CorruptKnowledgeStoreError(
                    "knowledge store contains an invalid chunk timestamp"
                ) from None
        try:
            external_id = str(row["external_id"])
            title = str(row["title"]) if row["title"] is not None else None
            document_aliases = str(row["document_aliases"])
            metadata_values = " ".join(
                value
                for value in (
                    external_id,
                    title or "",
                    document_aliases,
                )
                if value
            )
            matched_metadata = (
                _excerpt(metadata_values, query_tokens)
                if any(token.casefold() in metadata_values.casefold() for token in query_tokens)
                else None
            )
            return RetrievalHit(
                chunk_id=str(row["chunk_id"]),
                document_id=document_id,
                source_kind=SourceKind(str(row["source_kind"])),
                external_id=external_id,
                locator=str(row["locator"]),
                title=title,
                matched_metadata=matched_metadata,
                searchable_metadata_sha256=_searchable_metadata_digest(
                    external_id,
                    title,
                    document_aliases,
                ),
                role=ContentRole(str(row["role"])),
                timestamp=timestamp,
                excerpt=_excerpt(body, query_tokens),
                chunk_sha256=stored_hash,
                document_sha256=str(row["content_sha256"]),
                score=score,
                channels=list(channels),
            )
        except (TypeError, ValueError):
            raise CorruptKnowledgeStoreError(
                "knowledge store contains an invalid retrieval record"
            ) from None

    def _prepare_storage(self) -> None:
        _reject_symlink_or_wrong_type(self.root, expected_directory=True)
        self.root.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        _reject_symlink_or_wrong_type(self.root, expected_directory=True)
        _ensure_mode(self.root, _PRIVATE_DIRECTORY_MODE)

        _reject_symlink_or_wrong_type(self.knowledge_root, expected_directory=True)
        self.knowledge_root.mkdir(mode=_PRIVATE_DIRECTORY_MODE, exist_ok=True)
        _reject_symlink_or_wrong_type(self.knowledge_root, expected_directory=True)
        _ensure_mode(self.knowledge_root, _PRIVATE_DIRECTORY_MODE)

        _reject_symlink_or_wrong_type(self.database_path, expected_directory=False)
        if not self.database_path.exists():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.database_path, flags, _PRIVATE_FILE_MODE)
            os.close(descriptor)
        _reject_symlink_or_wrong_type(self.database_path, expected_directory=False)
        _ensure_mode(self.database_path, _PRIVATE_FILE_MODE)
        self._tighten_sidecars()

    def _validate_storage(self) -> None:
        _reject_symlink_or_wrong_type(self.root, expected_directory=True)
        _reject_symlink_or_wrong_type(self.knowledge_root, expected_directory=True)
        _reject_symlink_or_wrong_type(self.database_path, expected_directory=False)
        for sidecar in self._sidecar_paths():
            _reject_symlink_or_wrong_type(sidecar, expected_directory=False)

    def _tighten_sidecars(self) -> None:
        for sidecar in self._sidecar_paths():
            _reject_symlink_or_wrong_type(sidecar, expected_directory=False)
            if sidecar.exists():
                _ensure_mode(sidecar, _PRIVATE_FILE_MODE)

    def _sidecar_paths(self) -> tuple[Path, ...]:
        return tuple(
            Path(f"{self.database_path}{suffix}") for suffix in ("-journal", "-wal", "-shm")
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self._validate_storage()
        try:
            connection = sqlite3.connect(
                self.database_path,
                timeout=5.0,
                isolation_level=None,
            )
        except sqlite3.Error:
            raise KnowledgeStoreError("knowledge store could not be opened") from None
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            mode = str(connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0])
            if mode.casefold() != "delete":
                raise CorruptKnowledgeStoreError(
                    "knowledge store could not enforce DELETE journal mode"
                )
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("PRAGMA temp_store = MEMORY")
            yield connection
        finally:
            connection.close()
            _ensure_mode(self.database_path, _PRIVATE_FILE_MODE)
            self._tighten_sidecars()

    def _initialize_schema(self) -> None:
        try:
            with self._connect() as connection:
                user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if user_version not in (0, _SCHEMA_VERSION):
                    raise CorruptKnowledgeStoreError(
                        "knowledge store schema version is not supported"
                    )
                if user_version == 0:
                    existing_objects = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM sqlite_master
                            WHERE name NOT LIKE 'sqlite_%'
                            """
                        ).fetchone()[0]
                    )
                    if existing_objects:
                        raise CorruptKnowledgeStoreError("unversioned knowledge store is not empty")
                    connection.executescript(
                        """
                    CREATE TABLE IF NOT EXISTS store_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS documents (
                        document_id TEXT PRIMARY KEY,
                        source_kind TEXT NOT NULL,
                        external_id TEXT NOT NULL,
                        locator TEXT NOT NULL,
                        title TEXT,
                        content_sha256 TEXT NOT NULL,
                        byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
                        observed_at TEXT,
                        parent_external_id TEXT,
                        metadata_json TEXT NOT NULL,
                        aliases TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        UNIQUE (source_kind, external_id)
                    );

                    CREATE TABLE IF NOT EXISTS chunks (
                        chunk_id TEXT PRIMARY KEY,
                        document_id TEXT NOT NULL REFERENCES documents(document_id)
                            ON DELETE CASCADE,
                        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                        role TEXT NOT NULL,
                        timestamp TEXT,
                        body TEXT NOT NULL,
                        text_sha256 TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        UNIQUE (document_id, ordinal)
                    );

                    CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
                        chunk_id UNINDEXED,
                        document_id UNINDEXED,
                        body,
                        title,
                        aliases,
                        tokenize = 'unicode61 remove_diacritics 2'
                    );

                    CREATE TABLE IF NOT EXISTS sync_runs (
                        run_id TEXT PRIMARY KEY,
                        started_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        discovered INTEGER NOT NULL,
                        imported INTEGER NOT NULL,
                        updated INTEGER NOT NULL,
                        skipped INTEGER NOT NULL,
                        failed INTEGER NOT NULL,
                        documents INTEGER NOT NULL,
                        chunks_written INTEGER NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS sync_failures (
                        run_id TEXT NOT NULL REFERENCES sync_runs(run_id) ON DELETE CASCADE,
                        source_locator TEXT NOT NULL,
                        source_kind TEXT,
                        code TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        PRIMARY KEY (run_id, ordinal)
                    );

                    CREATE INDEX IF NOT EXISTS idx_documents_source_kind
                        ON documents(source_kind);
                    CREATE INDEX IF NOT EXISTS idx_chunks_document
                        ON chunks(document_id, ordinal);
                    """
                    )
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

                required_objects = {
                    "store_meta",
                    "documents",
                    "chunks",
                    "chunk_fts",
                    "sync_runs",
                    "sync_failures",
                }
                actual_objects = {
                    str(row["name"])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                    )
                }
                if not required_objects.issubset(actual_objects):
                    raise CorruptKnowledgeStoreError("knowledge store schema is incomplete")
        except (UnsafeKnowledgePathError, CorruptKnowledgeStoreError):
            raise
        except sqlite3.Error:
            raise KnowledgeStoreError("knowledge store initialization failed") from None


@contextmanager
def _read_snapshot(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN DEFERRED")
    try:
        yield
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    else:
        connection.commit()


def _reject_symlink_or_wrong_type(path: Path, *, expected_directory: bool) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise UnsafeKnowledgePathError("knowledge storage contains a managed symlink")
    if expected_directory and not stat.S_ISDIR(mode):
        raise UnsafeKnowledgePathError("knowledge directory has an unexpected file type")
    if not expected_directory and not stat.S_ISREG(mode):
        raise UnsafeKnowledgePathError("knowledge database has an unexpected file type")


def _datetime_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).isoformat()
    return value.astimezone(UTC).isoformat()


def _parse_catalog_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _utc_now_text() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: dict[str, str]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _chunk_metadata(value: object) -> dict[str, str]:
    try:
        loaded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        raise CorruptKnowledgeStoreError(
            "knowledge store contains invalid chunk metadata"
        ) from None
    if not isinstance(loaded, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in loaded.items()
    ):
        raise CorruptKnowledgeStoreError("knowledge store contains invalid chunk metadata")
    return loaded


def _context_neighbor_ids(
    *,
    target_id: str,
    target_ordinal: int,
    target_role: ContentRole,
    source_kind: SourceKind,
    target_metadata: dict[str, str],
    rows: Sequence[sqlite3.Row],
    radius: int,
) -> list[str]:
    parsed = [
        (str(row["chunk_id"]), int(row["ordinal"]), _chunk_metadata(row["metadata_json"]))
        for row in rows
    ]
    candidates: list[tuple[int, int, str]] = []

    def add_group_representatives(
        members: Sequence[tuple[str, int]],
        *,
        priority: int,
        reverse: bool = False,
    ) -> None:
        ordered = sorted(members, key=lambda item: (item[1], item[0]))
        if reverse:
            ordered.reverse()
        if not ordered:
            return
        # Put the middle representative first. The bounded agent may only be
        # able to reserve one chunk for an adjacent long answer; a middle
        # segment is the least biased single representative, while first/last
        # remain available when the context budget permits them.
        representatives = [ordered[len(ordered) // 2], ordered[0], ordered[-1]]
        representatives = list(dict.fromkeys(representatives))
        for rank, (chunk_id, _ordinal) in enumerate(representatives):
            candidates.append((priority, rank, chunk_id))

    group_key = "artifact" if source_kind is SourceKind.BUILDLOG_RUN else "message_id"
    group_value = target_metadata.get(group_key)
    if group_value:
        grouped = [
            (chunk_id, ordinal)
            for chunk_id, ordinal, metadata in parsed
            if chunk_id != target_id and metadata.get(group_key) == group_value
        ]
        same_group_limit = 1 if target_role is ContentRole.USER else 2
        close_group_members = [
            (chunk_id, ordinal)
            for chunk_id, ordinal in sorted(
                grouped,
                key=lambda item: (abs(item[1] - target_ordinal), item[1], item[0]),
            )
            if abs(ordinal - target_ordinal) <= radius
        ][:same_group_limit]
        for chunk_id, ordinal in close_group_members:
            distance = abs(ordinal - target_ordinal)
            candidates.append((0, distance, chunk_id))
        if (
            grouped
            and len({chunk_id for _priority, _distance, chunk_id in candidates}) < same_group_limit
        ):
            for edge_rank, edge in enumerate(
                (
                    min(grouped, key=lambda item: (item[1], item[0])),
                    max(grouped, key=lambda item: (item[1], item[0])),
                )
            ):
                candidates.append((1, edge_rank, edge[0]))
                if (
                    len({chunk_id for _priority, _distance, chunk_id in candidates})
                    >= same_group_limit
                ):
                    break

        # Keep same-message edges as a low-priority fallback. A user turn must
        # reserve the bounded neighbor budget for a distinct answer when one
        # exists, but an isolated segmented message still needs its tail to be
        # recoverable when there is no adjacent turn at all.
        if grouped:
            for edge_rank, edge in enumerate(
                (
                    min(grouped, key=lambda item: (item[1], item[0])),
                    max(grouped, key=lambda item: (item[1], item[0])),
                )
            ):
                candidates.append((10, edge_rank, edge[0]))

        if source_kind is not SourceKind.CHATGPT_EXPORT:
            groups: dict[str, list[tuple[str, int]]] = {}
            for chunk_id, ordinal, metadata in parsed:
                candidate_group = metadata.get(group_key)
                if candidate_group:
                    groups.setdefault(candidate_group, []).append((chunk_id, ordinal))
            ordered_groups = sorted(
                groups,
                key=lambda key: (
                    min(ordinal for _chunk_id, ordinal in groups[key]),
                    key,
                ),
            )
            if group_value in ordered_groups:
                target_group_index = ordered_groups.index(group_value)
                offsets = (1, -1) if target_role is ContentRole.USER else (-1, 1)
                for direction_rank, offset in enumerate(offsets):
                    neighbor_index = target_group_index + offset
                    if 0 <= neighbor_index < len(ordered_groups):
                        add_group_representatives(
                            groups[ordered_groups[neighbor_index]],
                            priority=2 + direction_rank,
                            reverse=offset < 0,
                        )

    if source_kind is SourceKind.CHATGPT_EXPORT:
        node_id = target_metadata.get("node_id")
        if node_id:
            node_parent = {
                metadata.get("node_id"): metadata.get("parent_node_id")
                for _chunk_id, _ordinal, metadata in parsed
                if metadata.get("node_id")
            }
            frontier = {node_id}
            visited = {node_id}
            related: dict[str, int] = {}
            for distance in range(1, radius + 1):
                next_frontier: set[str] = set()
                for current in frontier:
                    parent = node_parent.get(current)
                    if parent and parent not in visited:
                        next_frontier.add(parent)
                    children = {
                        child
                        for child, candidate_parent in node_parent.items()
                        if child and candidate_parent == current and child not in visited
                    }
                    if len(children) == 1:
                        next_frontier.update(children)
                for related_node in next_frontier:
                    related[related_node] = distance
                visited.update(next_frontier)
                frontier = next_frontier
            for related_node, graph_distance in sorted(
                related.items(), key=lambda item: (item[1], item[0])
            ):
                members = [
                    (chunk_id, ordinal)
                    for chunk_id, ordinal, metadata in parsed
                    if chunk_id != target_id and metadata.get("node_id") == related_node
                ]
                is_descendant = False
                cursor = related_node
                seen_lineage: set[str] = set()
                while cursor not in seen_lineage:
                    seen_lineage.add(cursor)
                    cursor_parent = node_parent.get(cursor)
                    if cursor_parent == node_id:
                        is_descendant = True
                        break
                    if cursor_parent is None:
                        break
                    cursor = cursor_parent
                preferred = is_descendant if target_role is ContentRole.USER else not is_descendant
                direction_rank = 0 if preferred else 1
                add_group_representatives(
                    members,
                    priority=2 + (graph_distance - 1) * 4 + direction_rank * 2,
                    reverse=node_parent.get(node_id) == related_node,
                )
    elif not group_value:
        for chunk_id, ordinal, _metadata in parsed:
            distance = abs(ordinal - target_ordinal)
            if chunk_id != target_id and 0 < distance <= radius:
                candidates.append((4, distance, chunk_id))

    return list(
        dict.fromkeys(
            chunk_id
            for _priority, _distance, chunk_id in sorted(
                candidates, key=lambda item: (item[0], item[1], item[2])
            )
        )
    )


def _searchable_metadata(source: ParsedSource) -> str:
    document = source.document
    values = [document.external_id, document.parent_external_id or ""]
    values.extend(document.metadata.values())
    return " ".join(value for value in values if value)


def _searchable_metadata_digest(
    external_id: str,
    title: str | None,
    aliases: str,
) -> str:
    payload = _canonical_json(
        {
            "external_id": external_id,
            "title": title or "",
            "aliases": aliases,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _lexical_query_tokens(query: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for match in _QUERY_TOKEN.finditer(query):
        raw = match.group(0)
        start = 0
        previous_is_cjk = bool(_CJK_TOKEN.fullmatch(raw[0]))
        for index, character in enumerate(raw[1:], start=1):
            is_cjk = bool(_CJK_TOKEN.fullmatch(character))
            if is_cjk == previous_is_cjk:
                continue
            tokens.append(raw[start:index].casefold())
            start = index
            previous_is_cjk = is_cjk
        tokens.append(raw[start:].casefold())
    return tuple(dict.fromkeys(tokens))


def _expanded_query_tokens(lexical_tokens: Sequence[str]) -> tuple[str, ...]:
    base = list(dict.fromkeys(lexical_tokens))
    generated = list(
        dict.fromkeys(
            token[index : index + 2]
            for token in base
            if len(token) >= 4 and _CJK_TOKEN.fullmatch(token)
            for index in range(len(token) - 1)
        )
    )
    remaining = _MAX_QUERY_TOKENS - len(base)
    if remaining <= 0 or not generated:
        return tuple(base)
    if len(generated) <= remaining:
        selected = generated
    elif remaining == 1:
        selected = [generated[len(generated) // 2]]
    else:
        selected = [
            generated[round(index * (len(generated) - 1) / (remaining - 1))]
            for index in range(remaining)
        ]
    return tuple(dict.fromkeys([*base, *selected]))


def _normalize_source_kinds(
    source_kinds: Sequence[SourceKind] | None,
) -> tuple[SourceKind, ...] | None:
    if source_kinds is None:
        return None
    return tuple(dict.fromkeys(SourceKind(kind) for kind in source_kinds))


def _source_filter_sql(
    source_kinds: tuple[SourceKind, ...] | None,
    *,
    alias: str,
) -> tuple[str, tuple[str, ...]]:
    if source_kinds is None:
        return "", ()
    placeholders = ", ".join("?" for _ in source_kinds)
    return (
        f"AND {alias}.source_kind IN ({placeholders})",
        tuple(kind.value for kind in source_kinds),
    )


def _unique_chunk_ids(rows: Iterable[sqlite3.Row]) -> list[str]:
    return list(dict.fromkeys(str(row["chunk_id"]) for row in rows))


def _reciprocal_rank_fusion(
    fts_ids: Sequence[str],
    exact_ids: Sequence[str],
) -> list[tuple[str, float, tuple[str, ...]]]:
    scores: dict[str, float] = {}
    channels: dict[str, list[str]] = {}
    for channel, ranked_ids in (("fts", fts_ids), ("exact", exact_ids)):
        for rank, chunk_id in enumerate(ranked_ids, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
            channels.setdefault(chunk_id, []).append(channel)
    return sorted(
        ((chunk_id, score, tuple(channels[chunk_id])) for chunk_id, score in scores.items()),
        key=lambda item: (-item[1], item[0]),
    )


def _excerpt(text: str, query_tokens: Sequence[str]) -> str:
    compact = " ".join(text.split())
    if len(compact) <= _EXCERPT_LIMIT:
        return compact
    lowered = compact.casefold()
    positions = sorted(
        {position for token in query_tokens if (position := lowered.find(token.casefold())) >= 0}
    )
    if not positions:
        return f"{compact[: _EXCERPT_LIMIT - 1]}…"

    span = positions[-1] - positions[0]
    if span <= _EXCERPT_LIMIT // 2:
        center = (positions[0] + positions[-1]) // 2
        start = max(0, center - _EXCERPT_LIMIT // 2)
        end = min(len(compact), start + _EXCERPT_LIMIT)
        start = max(0, end - _EXCERPT_LIMIT)
        prefix = "…" if start else ""
        suffix = "…" if end < len(compact) else ""
        return f"{prefix}{compact[start:end]}{suffix}"

    selected_positions = positions[:6]
    separator = " … "
    available = _EXCERPT_LIMIT - len(separator) * (len(selected_positions) - 1)
    window_size = max(80, available // len(selected_positions))
    windows: list[str] = []
    for position in selected_positions:
        start = max(0, position - window_size // 3)
        end = min(len(compact), start + window_size)
        start = max(0, end - window_size)
        windows.append(compact[start:end])
    combined = separator.join(windows)
    if len(combined) > _EXCERPT_LIMIT:
        return f"{combined[: _EXCERPT_LIMIT - 1]}…"
    return combined
