from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from soloscale.knowledge_models import (
    ContentRole,
    NormalizedChunk,
    NormalizedDocument,
    ParsedSource,
    SourceKind,
)
from soloscale.knowledge_store import (
    CorruptKnowledgeStoreError,
    InvalidKnowledgeQueryError,
    KnowledgeStore,
    UnsafeKnowledgePathError,
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source(
    *,
    external_id: str = "thread-1",
    document_id: str = "doc-thread-1",
    locator: str = "/private/codex/thread-1.jsonl",
    source_kind: SourceKind = SourceKind.CODEX_SESSION,
    texts: tuple[str, ...] = (
        "SoloScale uses checksum-backed evidence for reliable engineering learning.",
        "BuildLog evaluator returned invalid JSON and stopped before publication.",
    ),
    title: str | None = "SoloScale BuildLog recovery",
    aliases: str = "solo scale ai os, 工程证据, build log",
) -> ParsedSource:
    document_body = "\n".join(texts)
    document = NormalizedDocument(
        id=document_id,
        source_kind=source_kind,
        external_id=external_id,
        locator=locator,
        title=title,
        content_sha256=_sha256(document_body),
        byte_size=len(document_body.encode("utf-8")),
        observed_at=datetime(2026, 8, 9, tzinfo=UTC),
        metadata={"aliases": aliases, "project": "SoloScale"},
    )
    chunks = [
        NormalizedChunk(
            id=f"{document_id}-chunk-{ordinal}",
            document_id=document_id,
            ordinal=ordinal,
            role=ContentRole.USER if ordinal == 0 else ContentRole.ASSISTANT,
            timestamp=datetime(2026, 8, 9, 1, tzinfo=UTC) + timedelta(minutes=ordinal),
            text=text,
            text_sha256=_sha256(text),
            metadata={"topic": "evidence"},
        )
        for ordinal, text in enumerate(texts)
    ]
    return ParsedSource(document=document, chunks=chunks)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_sync_is_private_idempotent_and_updates_current_snapshot(tmp_path: Path) -> None:
    root = tmp_path / ".soloscale"
    store = KnowledgeStore(root)

    first = store.sync([_source()])
    second = store.sync([_source(locator="/private/archive/thread-1.jsonl")])
    changed = _source(
        locator="/private/archive/thread-1.jsonl",
        texts=("SoloScale source changed safely.",),
    )
    third = store.sync([changed])

    assert (first.imported, first.updated, first.skipped, first.chunks_written) == (
        1,
        0,
        0,
        2,
    )
    assert (second.imported, second.updated, second.skipped) == (0, 0, 1)
    assert (third.imported, third.updated, third.skipped, third.chunks_written) == (
        0,
        1,
        0,
        1,
    )
    status = store.status()
    assert (status.documents, status.chunks) == (1, 1)
    assert status.source_counts == {SourceKind.CODEX_SESSION.value: 1}
    assert status.last_synced_at is not None
    assert _mode(root) == 0o700
    assert _mode(root / "knowledge") == 0o700
    assert _mode(root / "knowledge" / "index.sqlite3") == 0o600
    assert store.search("changed")[0].locator == "/private/archive/thread-1.jsonl"


def test_search_fuses_fts_and_alias_channels_with_lineage(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / ".soloscale")
    codex = _source()
    chatgpt = _source(
        external_id="chat-2",
        document_id="doc-chat-2",
        locator="chatgpt-export.zip#chat-2",
        source_kind=SourceKind.CHATGPT_EXPORT,
        texts=("工程证据应该保留哈希和来源定位。",),
        title="证据学习",
        aliases="证据链, learning evidence",
    )
    store.sync([codex, chatgpt])

    english = store.search("SoloScale")
    chinese = store.search("证据链")
    filtered = store.search("evidence", source_kinds=[SourceKind.CHATGPT_EXPORT])

    assert english[0].channels == ["fts", "exact"]
    assert chinese[0].source_kind is SourceKind.CHATGPT_EXPORT
    assert filtered
    assert {hit.source_kind for hit in filtered} == {SourceKind.CHATGPT_EXPORT}
    hit = english[0]
    assert hit.chunk_sha256 == codex.chunks[0].text_sha256
    assert hit.document_sha256 == codex.document.content_sha256
    assert store.get_chunks([hit.chunk_id])[0] == hit.model_copy(
        update={"score": 1.0, "channels": ["direct"], "matched_metadata": None}
    )


def test_search_exposes_only_query_focused_matching_metadata(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / ".soloscale")
    source = _source(
        external_id="metadata-only",
        document_id="doc-metadata-only",
        texts=("unrelated narrative body",),
        title=("T" * 900) + " TAIL_TITLE_SIGNAL",
        aliases="TASK-842",
    )
    store.sync([source])

    title_hit = store.search("TAIL_TITLE_SIGNAL", limit=1)[0]
    alias_hit = store.search("TASK-842", limit=1)[0]

    assert title_hit.matched_metadata is not None
    assert "TAIL_TITLE_SIGNAL" in title_hit.matched_metadata
    assert alias_hit.matched_metadata is not None
    assert "TASK-842" in alias_hit.matched_metadata


def test_search_order_is_stable_and_query_syntax_cannot_escape(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / ".soloscale")
    store.sync([_source()])

    first = [hit.chunk_id for hit in store.search("evidence")]
    second = [hit.chunk_id for hit in store.search("evidence")]

    assert first == second
    assert store.search("' UNION SELECT secret FROM passwords --") == []
    assert store.status().chunks == 2
    with pytest.raises(InvalidKnowledgeQueryError):
        store.search("!? --")
    with pytest.raises(InvalidKnowledgeQueryError):
        store.search("x" * 1_001)


def test_one_bad_source_rolls_back_without_discarding_other_sources(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / ".soloscale")
    bad = _source(external_id="bad", document_id="doc-bad")
    bad.chunks[0].text_sha256 = "0" * 64

    report = store.sync([bad, _source()])

    assert (report.failed, report.imported, report.documents) == (1, 1, 1)
    assert report.failures[0].code == "chunk-hash-mismatch"
    assert report.failures[0].source_locator == bad.document.locator
    assert "SoloScale uses" not in report.model_dump_json()
    assert store.status().documents == 1


@pytest.mark.parametrize("component", ["root", "knowledge", "database"])
def test_managed_symlinks_fail_closed(tmp_path: Path, component: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / ".soloscale"
    if component == "root":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir()
        if component == "knowledge":
            (root / "knowledge").symlink_to(outside, target_is_directory=True)
        else:
            knowledge = root / "knowledge"
            knowledge.mkdir()
            target = outside / "database"
            target.write_bytes(b"do not touch")
            (knowledge / "index.sqlite3").symlink_to(target)

    with pytest.raises(UnsafeKnowledgePathError) as error:
        KnowledgeStore(root)

    assert "do not touch" not in str(error.value)
    assert not (outside / "index.sqlite3").exists()


def test_database_sidecar_symlink_is_rejected_before_sqlite_open(tmp_path: Path) -> None:
    root = tmp_path / ".soloscale"
    store = KnowledgeStore(root)
    outside = tmp_path / "outside.txt"
    outside.write_text("sentinel", encoding="utf-8")
    journal = Path(f"{store.database_path}-journal")
    os.symlink(outside, journal)

    with pytest.raises(UnsafeKnowledgePathError):
        store.status()

    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_corrupt_retrieval_error_does_not_echo_private_body(tmp_path: Path) -> None:
    private_body = "customer private incident evidence"
    store = KnowledgeStore(tmp_path / ".soloscale")
    parsed = _source(texts=(private_body,))
    store.sync([parsed])
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE chunks SET text_sha256 = 'invalid' WHERE chunk_id = ?",
            (parsed.chunks[0].id,),
        )

    with pytest.raises(CorruptKnowledgeStoreError) as error:
        store.get_chunks([parsed.chunks[0].id])

    assert private_body not in str(error.value)


def test_retrieval_detects_body_or_fts_tampering_and_resync_repairs_it(
    tmp_path: Path,
) -> None:
    store = KnowledgeStore(tmp_path / ".soloscale")
    parsed = _source(texts=("verified original evidence",))
    store.sync([parsed])
    chunk_id = parsed.chunks[0].id
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE chunks SET body = 'forged evidence' WHERE chunk_id = ?",
            (chunk_id,),
        )

    with pytest.raises(CorruptKnowledgeStoreError, match="integrity"):
        store.get_chunks([chunk_id])
    repaired = store.sync([parsed])
    assert repaired.updated == 1
    assert store.get_chunks([chunk_id])[0].excerpt == "verified original evidence"

    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE chunk_fts SET body = 'forged index text' WHERE chunk_id = ?",
            (chunk_id,),
        )
    with pytest.raises(CorruptKnowledgeStoreError, match="integrity"):
        store.get_chunks([chunk_id])
    fts_repaired = store.sync([parsed])
    assert fts_repaired.updated == 1
    assert store.get_chunks([chunk_id])[0].excerpt == "verified original evidence"

    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """
            UPDATE chunk_fts
            SET document_id = 'forged-document', title = 'forged title', aliases = 'forgedterm'
            WHERE chunk_id = ?
            """,
            (chunk_id,),
        )
    with pytest.raises(CorruptKnowledgeStoreError, match="integrity"):
        store.search("forgedterm")
    projection_repaired = store.sync([parsed])
    assert projection_repaired.updated == 1
    assert store.search("forgedterm") == []


def test_normalizer_change_replaces_projection_when_raw_source_hash_is_unchanged(
    tmp_path: Path,
) -> None:
    store = KnowledgeStore(tmp_path / ".soloscale")
    old = _source(texts=("password old-secret",))
    store.sync([old])
    improved = _source(texts=("password [REDACTED]",))
    improved.document.content_sha256 = old.document.content_sha256

    report = store.sync([improved])

    assert report.updated == 1
    assert store.search("old-secret") == []
    assert store.search("REDACTED")[0].chunk_id == improved.chunks[0].id


def test_unchanged_raw_source_refreshes_role_and_timestamp_projection(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / ".soloscale")
    original = _source(texts=("role and timestamp projection",))
    store.sync([original])
    improved = _source(texts=("role and timestamp projection",))
    improved.chunks[0].role = ContentRole.ASSISTANT
    improved.chunks[0].timestamp = datetime(2026, 8, 9, 2, tzinfo=UTC)

    report = store.sync([improved])
    resolved = store.get_chunks([improved.chunks[0].id])[0]

    assert report.updated == 1
    assert resolved.role is ContentRole.ASSISTANT
    assert resolved.timestamp == datetime(2026, 8, 9, 2, tzinfo=UTC)


def test_multi_term_retrieval_prefers_full_coverage_in_english_and_chinese(
    tmp_path: Path,
) -> None:
    store = KnowledgeStore(tmp_path / ".soloscale")
    sources = [
        _source(
            external_id=f"alpha-{index}",
            document_id=f"doc-alpha-{index}",
            texts=("alpha distraction",),
            title="Distractor",
            aliases="",
        )
        for index in range(39)
    ]
    relevant = _source(
        external_id="alpha-beta",
        document_id="doc-alpha-beta",
        texts=("alpha beta root cause and verification",),
        title="Relevant",
        aliases="",
    )
    chinese_distractor = _source(
        external_id="hash-only",
        document_id="doc-hash-only",
        texts=("哈希 是一个干扰项",),
        title="中文干扰",
        aliases="",
    )
    chinese_relevant = _source(
        external_id="hash-source",
        document_id="doc-hash-source",
        texts=("哈希 来源 构成完整证据链",),
        title="中文相关",
        aliases="",
    )
    natural_chinese = _source(
        external_id="natural-chinese",
        document_id="doc-natural-chinese",
        texts=("工程证据应该同时保留哈希和来源定位。",),
        title="自然中文",
        aliases="",
    )
    mixed_script = _source(
        external_id="mixed-script",
        document_id="doc-mixed-script",
        texts=("BuildLog 工程证据应该保留来源定位。",),
        title="混合语言",
        aliases="",
    )
    store.sync(
        [
            *sources,
            relevant,
            chinese_distractor,
            chinese_relevant,
            natural_chinese,
            mixed_script,
        ]
    )

    assert store.search("alpha beta", limit=5)[0].document_id == relevant.document.id
    assert store.search("哈希 来源", limit=5)[0].document_id == (chinese_relevant.document.id)
    assert natural_chinese.document.id in {
        hit.document_id for hit in store.search("证据来源", limit=5)
    }
    long_query = "这是一个用于测试中文连续长查询是否能够被正常检索到相关工程证据的完整问题"
    assert store.search(long_query, limit=5)
    assert store.search("BuildLog证据来源", limit=5)[0].document_id == (mixed_script.document.id)


def test_duplicate_text_is_deduplicated_before_channel_limit(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / ".soloscale")
    duplicates = [
        _source(
            external_id=f"duplicate-{index}",
            document_id=f"doc-duplicate-{index}",
            texts=("alpha duplicate",),
            title=None,
            aliases="",
        )
        for index in range(50)
    ]
    unique = _source(
        external_id="unique",
        document_id="doc-unique",
        texts=("alpha unique evidence",),
        title=None,
        aliases="",
    )
    store.sync([*duplicates, unique])

    hits = store.search("alpha", limit=5)

    assert len(hits) == 2
    assert {hit.document_id for hit in hits} >= {duplicates[0].document.id, unique.document.id}


def test_duplicate_text_preserves_distinct_user_and_assistant_roles(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / ".soloscale")
    source = _source(
        external_id="role-lineage",
        document_id="doc-role-lineage",
        texts=("APPROVED MARKER", "APPROVED MARKER"),
        title=None,
        aliases="",
    )
    source.chunks[0].role = ContentRole.USER
    source.chunks[1].role = ContentRole.ASSISTANT
    store.sync([source])

    hits = store.search("APPROVED MARKER", limit=5)

    assert {hit.role for hit in hits} == {ContentRole.USER, ContentRole.ASSISTANT}


def test_document_title_match_does_not_fan_out_to_every_turn(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / ".soloscale")
    titled = _source(
        external_id="long-thread",
        document_id="doc-long-thread",
        texts=tuple(f"ordinary turn {index}" for index in range(100)),
        title="SoloScale BuildLog",
        aliases="",
    )
    decisive = _source(
        external_id="decisive",
        document_id="doc-decisive",
        texts=("SoloScale BuildLog contains the decisive evaluator recovery.",),
        title="Recovery evidence",
        aliases="",
    )
    store.sync([titled, decisive])

    hits = store.search("SoloScale BuildLog", limit=10)

    assert any(hit.document_id == decisive.document.id for hit in hits)
    assert sum(hit.document_id == titled.document.id for hit in hits) == 1


def test_excerpt_preserves_distant_query_terms_and_neighbors_preserve_turn_context(
    tmp_path: Path,
) -> None:
    store = KnowledgeStore(tmp_path / ".soloscale")
    distant = _source(
        external_id="distant",
        document_id="doc-distant",
        texts=(f"alpha {'x' * 2400} beta",),
        title=None,
        aliases="",
    )
    conversation = _source(
        external_id="paired",
        document_id="doc-paired",
        texts=(
            "How did we solve the frobnicator outage?",
            "We added a durable queue and an idempotency key.",
            "The regression suite passed.",
        ),
        title=None,
        aliases="",
    )
    store.sync([distant, conversation])

    excerpt = store.search("alpha beta", limit=1)[0].excerpt
    primary = store.search("frobnicator outage", limit=1)[0]
    neighbors = store.get_neighbors([primary.chunk_id])

    assert "alpha" in excerpt and "beta" in excerpt
    assert [hit.excerpt for hit in neighbors] == [
        "We added a durable queue and an idempotency key."
    ]
    assert neighbors[0].channels == ["neighbor"]


def test_chatgpt_neighbor_expansion_follows_graph_not_flattened_branch_order(
    tmp_path: Path,
) -> None:
    store = KnowledgeStore(tmp_path / ".soloscale")
    branched = _source(
        external_id="branched-chat",
        document_id="doc-branched-chat",
        source_kind=SourceKind.CHATGPT_EXPORT,
        texts=(
            "Which branch should we use?",
            "Old branch answer.",
            "Old branch follow-up marker.",
            "Current branch decisive answer.",
        ),
        title="Branched chat",
        aliases="",
    )
    graph = (
        {"node_id": "u", "parent_node_id": "root", "message_id": "u", "segment": "0"},
        {"node_id": "a", "parent_node_id": "u", "message_id": "a", "segment": "0"},
        {"node_id": "a2", "parent_node_id": "a", "message_id": "a2", "segment": "0"},
        {"node_id": "b", "parent_node_id": "u", "message_id": "b", "segment": "0"},
    )
    for chunk, metadata in zip(branched.chunks, graph, strict=True):
        chunk.metadata = metadata
    store.sync([branched])

    current = store.search("Current branch decisive", limit=1)[0]
    neighbors = store.get_neighbors([current.chunk_id])
    excerpts = [hit.excerpt for hit in neighbors]
    shared_question = store.search("Which branch should", limit=1)[0]
    ambiguous_children = store.get_neighbors([shared_question.chunk_id])

    assert "Which branch should we use?" in excerpts
    assert "Old branch follow-up marker." not in excerpts
    assert ambiguous_children == []


def test_long_message_expansion_includes_bounded_tail_segment(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / ".soloscale")
    long_message = _source(
        external_id="long-message",
        document_id="doc-long-message",
        texts=tuple(
            [
                "frobnicator outage details",
                *[f"filler segment {index}" for index in range(8)],
                "fixed with a durable queue and idempotency key",
            ]
        ),
        title="Long message",
        aliases="",
    )
    for index, chunk in enumerate(long_message.chunks):
        chunk.metadata = {
            "message_id": "one-long-message",
            "segment": str(index),
        }
    store.sync([long_message])

    primary = store.search("frobnicator outage", limit=1)[0]
    neighbors = store.get_neighbors([primary.chunk_id])

    assert len(neighbors) <= 4
    assert any("durable queue" in hit.excerpt for hit in neighbors)


def test_group_neighbors_reserve_next_turn_and_long_answer_tail(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / ".soloscale")
    long_question = _source(
        external_id="long-question",
        document_id="doc-long-question",
        texts=tuple(
            [
                "frobnicator outage question",
                *[f"question segment {index}" for index in range(6)],
                "DECISIVE ANSWER durable queue and idempotency key",
            ]
        ),
        title="Long question",
        aliases="",
    )
    for index, chunk in enumerate(long_question.chunks):
        is_question = index < 7
        chunk.role = ContentRole.USER if is_question else ContentRole.ASSISTANT
        chunk.metadata = {
            "message_id": "question" if is_question else "answer",
            "segment": str(index if is_question else 0),
        }
    long_answer = _source(
        external_id="long-answer",
        document_id="doc-long-answer",
        texts=tuple(
            [
                "INCIDENT_842 question",
                "answer opening",
                *[f"answer filler {index}" for index in range(5)],
                "answer tail uses durable queue",
            ]
        ),
        title="Long answer",
        aliases="",
    )
    for index, chunk in enumerate(long_answer.chunks):
        is_question = index == 0
        chunk.role = ContentRole.USER if is_question else ContentRole.ASSISTANT
        chunk.metadata = {
            "message_id": "question" if is_question else "answer",
            "segment": str(0 if is_question else index - 1),
        }
    store.sync([long_question, long_answer])

    first_question = store.search("frobnicator outage question", limit=1)[0]
    middle_question = store.get_chunks([long_question.chunks[3].id])[0]
    short_question = store.search("INCIDENT_842", limit=1)[0]

    assert any(
        "DECISIVE ANSWER" in hit.excerpt for hit in store.get_neighbors([first_question.chunk_id])
    )
    assert any(
        "DECISIVE ANSWER" in hit.excerpt for hit in store.get_neighbors([middle_question.chunk_id])
    )
    assert any(
        "answer tail uses durable queue" in hit.excerpt
        for hit in store.get_neighbors([short_question.chunk_id])
    )


def test_chatgpt_user_target_prioritizes_child_answer_tail_over_parent(tmp_path: Path) -> None:
    store = KnowledgeStore(tmp_path / ".soloscale")
    source = _source(
        external_id="chat-graph-priority",
        document_id="doc-chat-graph-priority",
        source_kind=SourceKind.CHATGPT_EXPORT,
        texts=tuple(
            [
                *[f"previous assistant segment {index}" for index in range(5)],
                *[f"current user segment {index}" for index in range(6)],
                "current user MID_GRAPH_778 segment",
                "current answer opening",
                "current answer DECISIVE_TAIL durable queue",
            ]
        ),
        title="ChatGPT graph priority",
        aliases="",
    )
    for index, chunk in enumerate(source.chunks):
        if index < 5:
            chunk.role = ContentRole.ASSISTANT
            chunk.metadata = {
                "node_id": "previous",
                "message_id": "previous-message",
                "segment": str(index),
            }
        elif index < 12:
            chunk.role = ContentRole.USER
            chunk.metadata = {
                "node_id": "current-user",
                "parent_node_id": "previous",
                "message_id": "current-user-message",
                "segment": str(index - 5),
            }
        else:
            chunk.role = ContentRole.ASSISTANT
            chunk.metadata = {
                "node_id": "current-answer",
                "parent_node_id": "current-user",
                "message_id": "current-answer-message",
                "segment": str(index - 12),
            }
    store.sync([source])

    target = store.search("MID_GRAPH_778", limit=1)[0]
    neighbors = store.get_neighbors([target.chunk_id])
    excerpts = [hit.excerpt for hit in neighbors]

    assert any("current answer opening" in excerpt for excerpt in excerpts)
    assert any("DECISIVE_TAIL durable queue" in excerpt for excerpt in excerpts)
    assert sum("previous assistant" in excerpt for excerpt in excerpts) <= 1


def test_search_uses_one_sqlite_snapshot_during_concurrent_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".soloscale"
    store = KnowledgeStore(root)
    old_source = _source(texts=("oldterm evidence",))
    new_source = _source(texts=("newterm replacement",))
    store.sync([old_source])
    original_fts = store._fts_candidates
    writer_started = threading.Event()
    writer_errors: list[Exception] = []
    writer: threading.Thread | None = None

    def update_source() -> None:
        writer_started.set()
        try:
            KnowledgeStore(root).sync([new_source])
        except Exception as exc:  # pragma: no cover - asserted below
            writer_errors.append(exc)

    def hooked_fts(
        connection: sqlite3.Connection,
        *,
        fts_query: str,
        source_kinds: tuple[SourceKind, ...] | None,
        candidate_limit: int,
    ) -> list[str]:
        nonlocal writer
        rows = original_fts(
            connection,
            fts_query=fts_query,
            source_kinds=source_kinds,
            candidate_limit=candidate_limit,
        )
        writer = threading.Thread(target=update_source)
        writer.start()
        assert writer_started.wait(timeout=1)
        return rows

    monkeypatch.setattr(store, "_fts_candidates", hooked_fts)

    result = store.search("oldterm", limit=1)
    assert writer is not None
    writer.join(timeout=5)

    assert not writer.is_alive()
    assert writer_errors == []
    assert result[0].excerpt == "oldterm evidence"
    assert store.search("newterm", limit=1)[0].excerpt == "newterm replacement"
