"""Deterministic retrieval/context gate over public synthetic bilingual data.

This deliberately does not evaluate semantic faithfulness. Citation membership elsewhere in
SoloScale is a structural containment check only; human review remains required to decide
whether cited evidence actually supports a generated claim.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import NotRequired, TypedDict, cast

from soloscale.knowledge_models import (
    ContentRole,
    NormalizedChunk,
    NormalizedDocument,
    ParsedSource,
    SourceKind,
)
from soloscale.knowledge_store import KnowledgeStore

_FIXTURE = Path(__file__).parent / "fixtures" / "conversation_rag_eval.json"
_TOP_K = 5


class ChunkSpec(TypedDict):
    id: NotRequired[str]
    id_template: NotRequired[str]
    role: str
    text: NotRequired[str]
    text_template: NotRequired[str]
    metadata: NotRequired[dict[str, str]]
    repeat: NotRequired[int]


class DocumentSpec(TypedDict):
    id: str
    external_id: str
    locator: str
    source_kind: str
    title: str | None
    aliases: str
    chunks: list[ChunkSpec]


class DocumentFamilySpec(TypedDict):
    count: int
    id_template: str
    external_id_template: str
    locator_template: str
    source_kind: str
    title: str | None
    aliases: str
    chunks: list[ChunkSpec]


class QuerySpec(TypedDict):
    id: str
    query: str
    relevant_chunk_ids: list[str]
    expect_empty: NotRequired[bool]


class ContextSpec(TypedDict):
    id: str
    query: str
    primary_chunk_id: str
    required_context_ids: list[str]
    forbidden_context_ids: list[str]


class GuardSpec(TypedDict):
    title_query: str
    title_document_id: str
    max_title_document_hits_at_5: int
    duplicate_query: str
    duplicate_text: str
    max_duplicate_text_hits_at_5: int
    required_unique_duplicate_chunk_id: str


class ThresholdSpec(TypedDict):
    recall_at_5: float
    mrr: float
    context_recall: float
    forbidden_context_precision: float
    max_search_latency_ms: float


class LimitationSpec(TypedDict):
    semantic_faithfulness_evaluated: bool
    citation_membership_is_structural_only: bool
    note: str


class EvalFixture(TypedDict):
    schema_version: int
    name: str
    provenance: str
    limitations: LimitationSpec
    document_families: list[DocumentFamilySpec]
    documents: list[DocumentSpec]
    queries: list[QuerySpec]
    context_cases: list[ContextSpec]
    guards: GuardSpec
    thresholds: ThresholdSpec


def _load_fixture() -> EvalFixture:
    return cast(EvalFixture, json.loads(_FIXTURE.read_text(encoding="utf-8")))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _render(value: str, *, index: int = 0, ordinal: int = 0) -> str:
    return value.format(index=index, ordinal=ordinal)


def _expand_chunks(specs: Iterable[ChunkSpec], *, index: int = 0) -> list[dict[str, object]]:
    expanded: list[dict[str, object]] = []
    absolute_ordinal = 0
    for spec in specs:
        repeat = spec.get("repeat", 1)
        for _ in range(repeat):
            chunk_id = spec.get("id") or _render(
                spec["id_template"], index=index, ordinal=absolute_ordinal
            )
            text = spec.get("text") or _render(
                spec["text_template"], index=index, ordinal=absolute_ordinal
            )
            metadata = {
                key: _render(value, index=index, ordinal=absolute_ordinal)
                for key, value in spec.get("metadata", {}).items()
            }
            expanded.append(
                {
                    "id": chunk_id,
                    "ordinal": absolute_ordinal,
                    "role": spec["role"],
                    "text": text,
                    "metadata": metadata,
                }
            )
            absolute_ordinal += 1
    return expanded


def _parsed_source(spec: DocumentSpec, *, observed_offset: int) -> ParsedSource:
    chunks_data = _expand_chunks(spec["chunks"])
    texts = [cast(str, chunk["text"]) for chunk in chunks_data]
    document_body = "\n".join(texts)
    observed_at = datetime(2026, 8, 9, tzinfo=UTC) + timedelta(seconds=observed_offset)
    document = NormalizedDocument(
        id=spec["id"],
        source_kind=SourceKind(spec["source_kind"]),
        external_id=spec["external_id"],
        locator=spec["locator"],
        title=spec["title"],
        content_sha256=_sha256(document_body),
        byte_size=len(document_body.encode("utf-8")),
        observed_at=observed_at,
        metadata={"aliases": spec["aliases"], "dataset": "synthetic-rag-eval-v1"},
    )
    chunks = [
        NormalizedChunk(
            id=cast(str, chunk["id"]),
            document_id=document.id,
            ordinal=cast(int, chunk["ordinal"]),
            role=ContentRole(cast(str, chunk["role"])),
            timestamp=observed_at + timedelta(milliseconds=cast(int, chunk["ordinal"])),
            text=cast(str, chunk["text"]),
            text_sha256=_sha256(cast(str, chunk["text"])),
            metadata=cast(dict[str, str], chunk["metadata"]),
        )
        for chunk in chunks_data
    ]
    return ParsedSource(document=document, chunks=chunks)


def _corpus(fixture: EvalFixture) -> list[ParsedSource]:
    specs = list(fixture["documents"])
    for family in fixture["document_families"]:
        for index in range(family["count"]):
            specs.append(
                DocumentSpec(
                    id=_render(family["id_template"], index=index),
                    external_id=_render(family["external_id_template"], index=index),
                    locator=_render(family["locator_template"], index=index),
                    source_kind=family["source_kind"],
                    title=family["title"],
                    aliases=family["aliases"],
                    chunks=[
                        ChunkSpec(
                            id=_render(chunk["id_template"], index=index),
                            role=chunk["role"],
                            text=_render(chunk["text_template"], index=index),
                            metadata=chunk.get("metadata", {}),
                        )
                        for chunk in family["chunks"]
                    ],
                )
            )
    return [_parsed_source(spec, observed_offset=index) for index, spec in enumerate(specs)]


def _reciprocal_rank(ranked_ids: list[str], relevant_ids: set[str]) -> float:
    for rank, chunk_id in enumerate(ranked_ids, start=1):
        if chunk_id in relevant_ids:
            return 1.0 / rank
    return 0.0


def _search_ids(store: KnowledgeStore, query: str) -> tuple[list[str], float]:
    started = perf_counter()
    hits = store.search(query, limit=_TOP_K)
    elapsed_ms = (perf_counter() - started) * 1000
    return [hit.chunk_id for hit in hits], elapsed_ms


def test_eval_scope_is_explicit_about_unsupported_semantic_claims() -> None:
    fixture = _load_fixture()

    assert fixture["schema_version"] == 1
    assert fixture["limitations"] == {
        "semantic_faithfulness_evaluated": False,
        "citation_membership_is_structural_only": True,
        "note": (
            "This gate evaluates deterministic retrieval and store neighbor expansion. "
            "It does not exercise the Evidence Agent context-byte allocator or prove "
            "that cited text semantically supports a generated claim."
        ),
    }
    assert "no private conversation content" in fixture["provenance"]


def test_conversation_rag_bilingual_retrieval_and_context_gate(
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    fixture = _load_fixture()
    sources = _corpus(fixture)
    first_store = KnowledgeStore(tmp_path / "first" / ".soloscale")
    second_store = KnowledgeStore(tmp_path / "second" / ".soloscale")
    assert first_store.sync(sources).failed == 0
    assert second_store.sync(sources).failed == 0

    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    latencies_ms: list[float] = []
    rankings: dict[str, list[str]] = {}
    for query in fixture["queries"]:
        first_ids, first_latency = _search_ids(first_store, query["query"])
        repeated_ids, repeated_latency = _search_ids(first_store, query["query"])
        rebuilt_ids, rebuilt_latency = _search_ids(second_store, query["query"])
        latencies_ms.extend((first_latency, repeated_latency, rebuilt_latency))
        assert first_ids == repeated_ids == rebuilt_ids, query["id"]
        rankings[query["id"]] = first_ids

        relevant_ids = set(query["relevant_chunk_ids"])
        if query.get("expect_empty"):
            assert first_ids == [], query["id"]
            continue
        recalls.append(len(relevant_ids.intersection(first_ids)) / len(relevant_ids))
        reciprocal_ranks.append(_reciprocal_rank(first_ids, relevant_ids))

    guards = fixture["guards"]
    title_hits = first_store.search(guards["title_query"], limit=_TOP_K)
    assert (
        sum(hit.document_id == guards["title_document_id"] for hit in title_hits)
        <= guards["max_title_document_hits_at_5"]
    )
    duplicate_hits = first_store.search(guards["duplicate_query"], limit=_TOP_K)
    duplicate_hash = _sha256(guards["duplicate_text"])
    assert (
        sum(hit.chunk_sha256 == duplicate_hash for hit in duplicate_hits)
        <= guards["max_duplicate_text_hits_at_5"]
    )
    assert guards["required_unique_duplicate_chunk_id"] in {hit.chunk_id for hit in duplicate_hits}

    required_context_count = 0
    recovered_context_count = 0
    returned_context_count = 0
    returned_forbidden_count = 0
    for context_case in fixture["context_cases"]:
        primary_ids, elapsed_ms = _search_ids(first_store, context_case["query"])
        latencies_ms.append(elapsed_ms)
        assert context_case["primary_chunk_id"] in primary_ids, context_case["id"]
        context_ids = {
            hit.chunk_id for hit in first_store.get_neighbors([context_case["primary_chunk_id"]])
        }
        required_ids = set(context_case["required_context_ids"])
        forbidden_ids = set(context_case["forbidden_context_ids"])
        required_context_count += len(required_ids)
        recovered_context_count += len(required_ids.intersection(context_ids))
        returned_context_count += len(context_ids)
        returned_forbidden_count += len(forbidden_ids.intersection(context_ids))

    recall_at_5 = sum(recalls) / len(recalls)
    mrr = sum(reciprocal_ranks) / len(reciprocal_ranks)
    context_recall = recovered_context_count / required_context_count
    forbidden_context_precision = (
        (returned_context_count - returned_forbidden_count) / returned_context_count
        if returned_context_count
        else 1.0
    )
    max_search_latency_ms = max(latencies_ms)
    metrics = {
        "recall_at_5": round(recall_at_5, 6),
        "mrr": round(mrr, 6),
        "context_recall": round(context_recall, 6),
        "forbidden_context_precision": round(forbidden_context_precision, 6),
        "max_search_latency_ms": round(max_search_latency_ms, 3),
        "deterministic_second_run": True,
        "evaluated_queries": len(fixture["queries"]),
        "evaluated_context_cases": len(fixture["context_cases"]),
    }
    record_property("conversation_rag_eval_metrics", json.dumps(metrics, sort_keys=True))
    record_property("conversation_rag_eval_rankings", json.dumps(rankings, sort_keys=True))

    thresholds = fixture["thresholds"]
    assert recall_at_5 >= thresholds["recall_at_5"], metrics
    assert mrr >= thresholds["mrr"], metrics
    assert context_recall >= thresholds["context_recall"], metrics
    assert forbidden_context_precision >= thresholds["forbidden_context_precision"], metrics
    assert max_search_latency_ms < thresholds["max_search_latency_ms"], metrics
