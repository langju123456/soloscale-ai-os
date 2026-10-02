"""Focused tests for high-recall multi-source resume evidence retrieval."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from soloscale.evidence_hub_models import EvidenceItem, TruthClass
from soloscale.knowledge_models import (
    ContentRole,
    NormalizedChunk,
    NormalizedDocument,
    ParsedSource,
    RetrievalHit,
    SourceKind,
)
from soloscale.knowledge_store import KnowledgeStore
from soloscale.mac_discovery import DiscoveryEntry
from soloscale.resume_models import (
    EvidenceCandidateClass,
    ResumeRetrievalCandidate,
    ResumeRetrievalSourceKind,
)
from soloscale.resume_retrieval import (
    _build_queries,
    build_evidence_coverage_map,
    capability_expansion,
    classify_candidate,
    normalize_requirements,
)

_HASH = "a" * 64


def _knowledge_source(kind: SourceKind, suffix: str) -> ParsedSource:
    text = f"Python FastAPI evidence from {kind.value} {suffix}."
    document = NormalizedDocument(
        id=f"doc-{suffix}",
        source_kind=kind,
        external_id=f"source-{suffix}",
        locator=f"synthetic://{kind.value}/{suffix}",
        title=f"Synthetic {kind.value}",
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        byte_size=len(text.encode()),
        observed_at=datetime(2026, 9, 13, tzinfo=UTC),
    )
    chunk = NormalizedChunk(
        id=f"chunk-{suffix}",
        document_id=document.id,
        ordinal=0,
        role=ContentRole.ASSISTANT,
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    return ParsedSource(document=document, chunks=[chunk])


def _hit(text: str, *, title: str = "project-a", chunk_id: str = "chunk-1") -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id,
        document_id="doc-1",
        source_kind=SourceKind.CODEX_SESSION,
        external_id="session-1",
        locator="/private/codex/session-1.jsonl",
        title=title,
        role=ContentRole.ASSISTANT,
        timestamp=None,
        excerpt=text,
        chunk_sha256=hashlib.sha256(chunk_id.encode("utf-8")).hexdigest(),
        document_sha256="b" * 64,
        score=1.0,
        channels=["assistant"],
    )


def _item(summary: str, *, evidence_id: str = "ev-1") -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        source_id="src-1",
        native_id="native-1",
        evidence_type="code",
        captured_at=datetime.now(UTC),
        truth_class=TruthClass.PERSONAL_ARTIFACT,
        public_safe_summary=summary,
        content_sha256="c" * 64,
        verification_status="verified",
    )


def _entry(
    *,
    entry_id: str,
    record_type: str,
    path: str,
    kind: str = "code",
    head: str | None = None,
) -> DiscoveryEntry:
    return DiscoveryEntry(
        entry_id=entry_id,
        record_type=record_type,  # type: ignore[arg-type]
        path=path,
        kind=kind,
        fingerprint="d" * 64,
        size=10,
        mtime_ns=1,
        ctime_ns=1,
        git_root=path if head else None,
        git_head=head,
        git_branch="main" if head else None,
        git_remote="https://github.com/me/project.git" if head else None,
        indexed_at="2026-08-29T00:00:00+00:00",
    )


class FakeKnowledgeSearch:
    def __init__(self, hits: list[RetrievalHit]) -> None:
        self._hits = hits

    def search(self, query: str, *, limit: int = 8) -> list[RetrievalHit]:
        del query, limit
        return list(self._hits)


class FakeEvidenceSearch:
    def __init__(self, items: list[EvidenceItem]) -> None:
        self._items = items

    def search(self, query: str, *, limit: int = 8) -> list[EvidenceItem]:
        del query, limit
        return list(self._items)


class FakeGitHubSearch:
    def __init__(self, candidates: list[ResumeRetrievalCandidate]) -> None:
        self._candidates = candidates

    def search(self, queries: Sequence[str]) -> list[ResumeRetrievalCandidate]:
        del queries
        return list(self._candidates)


def test_capability_expansion_for_rag_vocabulary() -> None:
    expansion = capability_expansion(
        "Design and implement RAG pipelines (chunking, embeddings, indexing, retrieval, "
        "reranking, grounding)."
    )
    assert expansion.capability == "rag_embedding_retrieval"
    assert "embeddings" in expansion.technology_terms
    assert {"faiss", "chroma", "sbert", "vector search"} <= set(expansion.synonyms)


def test_query_budget_keeps_synonym_channel_active() -> None:
    requirements = normalize_requirements(
        "Design and implement RAG pipelines (chunking, embeddings, indexing, retrieval, "
        "reranking, grounding)."
    )
    queries = _build_queries(requirements)
    assert "chroma" in queries
    assert "faiss" in queries


def test_lexical_mismatch_but_semantic_relevance() -> None:
    expansion = capability_expansion("embedding-based semantic retrieval")
    candidate_class, signals = classify_candidate(
        "Built similarity search over SBERT vectors in a FAISS index", expansion
    )
    assert candidate_class is EvidenceCandidateClass.SEMANTIC
    assert "sbert" in signals
    assert "faiss" in signals


def test_single_component_is_potential_derivation() -> None:
    expansion = capability_expansion("embedding-based RAG")
    candidate_class, signals = classify_candidate(
        "Stored document vectors in ChromaDB", expansion
    )
    assert candidate_class is EvidenceCandidateClass.POTENTIAL_DERIVATION
    assert "chromadb" in signals


def test_coverage_map_multi_source_with_provenance(tmp_path: Path) -> None:
    entries = {
        "repo-1": _entry(
            entry_id="repo-1",
            record_type="repository",
            path="/work/rag-service",
            kind="engineering",
            head="e" * 12,
        ),
        "file-1": _entry(
            entry_id="file-1",
            record_type="file",
            path="/work/notes/vector-search-notes.md",
            kind="project_document",
        ),
    }
    knowledge = FakeKnowledgeSearch(
        [_hit("Implemented a FAISS vector index with SBERT embeddings")]
    )
    evidence = FakeEvidenceSearch(
        [_item("ChromaDB similarity search implementation", evidence_id="ev-rag")]
    )
    github = FakeGitHubSearch(
        [
            ResumeRetrievalCandidate(
                candidate_id="e" * 64,
                source_kind=ResumeRetrievalSourceKind.GITHUB,
                source_identity="me/rag-service",
                evidence_type="repository_inventory",
                authority="github:me/rag-service",
                text="me/rag-service",
                rationale="saved GitHub repository inventory (metadata only)",
                score=0.0,
            )
        ]
    )

    coverage_map = build_evidence_coverage_map(
        job_description="Build embedding-based RAG systems.",
        data_root=tmp_path,
        mac_entries=entries,
        knowledge_search=knowledge,
        evidence_search=evidence,
        github_search=github,
    )

    kinds = {item.source_kind for item in coverage_map.sources}
    assert ResumeRetrievalSourceKind.GITHUB in kinds
    assert ResumeRetrievalSourceKind.KNOWLEDGE_STORE in kinds
    assert ResumeRetrievalSourceKind.EVIDENCE_HUB in kinds
    assert ResumeRetrievalSourceKind.RESUME_LIBRARY in kinds
    knowledge_candidates = [
        item
        for item in coverage_map.candidates
        if item.source_kind.value in {"codex", "chatgpt", "buildlog"}
    ]
    assert knowledge_candidates
    assert knowledge_candidates[0].provenance["chunk_sha256"] == hashlib.sha256(
        b"chunk-1"
    ).hexdigest()
    assert coverage_map.retrieved_count == coverage_map.kept_count + coverage_map.irrelevant_count
    assert any(item.requirement_id for item in coverage_map.coverage)


def test_verified_evidence_survives_lexical_mismatch(tmp_path: Path) -> None:
    evidence = FakeEvidenceSearch([_item("FAISS vector index implementation")])
    coverage_map = build_evidence_coverage_map(
        job_description="Experience building semantic vector search.",
        data_root=tmp_path,
        evidence_search=evidence,
    )
    hub_candidates = [
        item
        for item in coverage_map.candidates
        if item.source_kind is ResumeRetrievalSourceKind.EVIDENCE_HUB
    ]
    assert hub_candidates
    assert hub_candidates[0].provenance["verification_status"] == "verified"
    assert hub_candidates[0].candidate_class is not EvidenceCandidateClass.IRRELEVANT


def test_github_unavailable_does_not_block_retrieval(tmp_path: Path) -> None:
    coverage_map = build_evidence_coverage_map(
        job_description="Python backend engineer.",
        data_root=tmp_path,
    )
    github_status = next(
        item
        for item in coverage_map.sources
        if item.source_kind is ResumeRetrievalSourceKind.GITHUB
    )
    assert github_status.state == "UNAVAILABLE"
    assert coverage_map.requirements


def test_private_knowledge_sources_require_per_run_opt_in(tmp_path: Path) -> None:
    KnowledgeStore(tmp_path).sync(
        [
            _knowledge_source(SourceKind.CODEX_SESSION, "codex"),
            _knowledge_source(SourceKind.CHATGPT_EXPORT, "chatgpt"),
            _knowledge_source(SourceKind.BUILDLOG_RUN, "buildlog"),
        ]
    )

    default_map = build_evidence_coverage_map(
        job_description="Build Python FastAPI services.",
        data_root=tmp_path,
        mac_entries={},
    )
    default_status = next(
        source
        for source in default_map.sources
        if source.source_kind is ResumeRetrievalSourceKind.KNOWLEDGE_STORE
    )
    assert default_status.state == "NOT_SELECTED"
    assert not {
        ResumeRetrievalSourceKind.CODEX,
        ResumeRetrievalSourceKind.CHATGPT,
        ResumeRetrievalSourceKind.BUILDLOG,
    } & {candidate.source_kind for candidate in default_map.candidates}

    buildlog_map = build_evidence_coverage_map(
        job_description="Build Python FastAPI services.",
        data_root=tmp_path,
        mac_entries={},
        knowledge_source_kinds=(SourceKind.BUILDLOG_RUN,),
    )
    buildlog_kinds = {candidate.source_kind for candidate in buildlog_map.candidates}
    assert ResumeRetrievalSourceKind.BUILDLOG in buildlog_kinds
    assert ResumeRetrievalSourceKind.CODEX not in buildlog_kinds
    assert ResumeRetrievalSourceKind.CHATGPT not in buildlog_kinds

    conversation_map = build_evidence_coverage_map(
        job_description="Build Python FastAPI services.",
        data_root=tmp_path,
        mac_entries={},
        knowledge_source_kinds=(
            SourceKind.CODEX_SESSION,
            SourceKind.CHATGPT_EXPORT,
        ),
    )
    conversation_kinds = {
        candidate.source_kind for candidate in conversation_map.candidates
    }
    assert ResumeRetrievalSourceKind.CODEX in conversation_kinds
    assert ResumeRetrievalSourceKind.CHATGPT in conversation_kinds
    assert ResumeRetrievalSourceKind.BUILDLOG not in conversation_kinds


def test_noisy_evidence_classified_irrelevant_and_counted(tmp_path: Path) -> None:
    knowledge = FakeKnowledgeSearch(
        [
            _hit("quarterly marketing budget for the cafe", chunk_id="noise"),
            _hit("Python FastAPI backend service", chunk_id="signal"),
        ]
    )
    coverage_map = build_evidence_coverage_map(
        job_description="Python backend engineer with FastAPI experience.",
        data_root=tmp_path,
        knowledge_search=knowledge,
    )
    texts = {item.text for item in coverage_map.candidates}
    assert any("marketing budget" in text for text in texts) is False
    assert coverage_map.irrelevant_count == 1
    assert coverage_map.kept_count == 1


def test_cross_project_evidence_is_retrieved(tmp_path: Path) -> None:
    knowledge = FakeKnowledgeSearch(
        [_hit("FAISS vector retrieval in the search service", title="other-project")]
    )
    coverage_map = build_evidence_coverage_map(
        job_description="Build vector retrieval systems.",
        data_root=tmp_path,
        knowledge_search=knowledge,
    )
    candidates = [
        item
        for item in coverage_map.candidates
        if item.source_kind is ResumeRetrievalSourceKind.CODEX
    ]
    assert candidates
    assert candidates[0].source_identity == "other-project"
    assert candidates[0].candidate_class is not EvidenceCandidateClass.IRRELEVANT


def test_rag_embedding_case_finds_evidence_without_inferring_implementation(
    tmp_path: Path,
) -> None:
    knowledge = FakeKnowledgeSearch(
        [
            _hit(
                "Prototyped a ReAct research agent for multi-step retrieval, tool/API use, "
                "memory, and Markdown/PDF reporting with LangChain and ChromaDB."
            )
        ]
    )
    coverage_map = build_evidence_coverage_map(
        job_description=(
            "Design and implement RAG pipelines on Google Cloud / Vertex AI (chunking, "
            "embeddings, indexing, retrieval, reranking, grounding)."
        ),
        data_root=tmp_path,
        knowledge_search=knowledge,
    )
    candidates = [
        item
        for item in coverage_map.candidates
        if item.source_kind is ResumeRetrievalSourceKind.CODEX
    ]
    assert candidates
    assert "embeddings" not in candidates[0].signals
    assert candidates[0].candidate_class is not EvidenceCandidateClass.IRRELEVANT


def test_normalize_requirements_is_bounded() -> None:
    job_description = "\n".join(
        f"Requirement number {index} for Python engineers." for index in range(40)
    )
    requirements = normalize_requirements(job_description)
    assert len(requirements) <= 24
    assert all(item.requirement_id.startswith("REQ-") for item in requirements)
