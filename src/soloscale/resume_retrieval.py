"""High-recall multi-source evidence retrieval for Resume Intelligence.

This stage discovers and classifies candidate evidence. It deliberately does NOT decide
final claim eligibility: final claim truth belongs to the downstream claim-truth gate.

Retrieval combines:
- the durable full-Mac evidence catalog (file/repository/source identity),
- the local KnowledgeStore (lexical FTS over Codex/ChatGPT/BuildLog chunks),
- the EvidenceHub catalog (metadata-only evidence items),
- the Resume application library (application records),
- GitHub when configured (saved inventory metadata; no network required here).

Capability normalization expands JD vocabulary with a curated synonym lexicon so that
semantically related evidence (for example Chroma/FAISS/SBERT for "embedding-based RAG")
is found even when the exact JD wording differs. Retrieval recall and claim truth are
separate layers.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from soloscale.evidence_hub import EvidenceHub
from soloscale.evidence_hub_models import EvidenceItem
from soloscale.github_connect import GitHubRepository
from soloscale.knowledge_models import RetrievalHit, SourceKind
from soloscale.knowledge_store import KnowledgeStore
from soloscale.mac_discovery import DiscoveryEntry, load_discovery_catalog
from soloscale.resume_models import (
    EvidenceCandidateClass,
    ResumeEvidenceCoverageMap,
    ResumeRequirementExpansion,
    ResumeRetrievalCandidate,
    ResumeRetrievalCoverage,
    ResumeRetrievalSourceKind,
    ResumeRetrievalSourceStatus,
)
from soloscale.resume_workspace import parse_requirements

_MAX_TERMS_PER_REQUIREMENT = 10
_MAX_QUERIES_PER_SOURCE = 40
_MAX_RESULTS_PER_QUERY = 8
_MAX_CANDIDATES_PER_SOURCE = 200
_MAX_TOTAL_CANDIDATES = 1000
_EXCERPT_LIMIT = 2000

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]*")

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "by",
        "for",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
        "using",
        "we",
        "you",
        "our",
        "your",
        "ability",
        "able",
        "experience",
        "experiences",
        "knowledge",
        "skills",
        "strong",
        "good",
        "excellent",
        "understanding",
        "familiarity",
        "proficiency",
        "work",
        "working",
        "development",
        "applications",
        "application",
        "basic",
        "related",
        "technologies",
        "technology",
        "tools",
        "software",
        "engineering",
        "etc",
    }
)

_CAPABILITY_LEXICON: dict[str, frozenset[str]] = {
    "rag_embedding_retrieval": frozenset(
        {
            "rag",
            "retrieval",
            "semantic",
            "vector",
            "similarity",
            "retrieval-augmented generation",
            "semantic search",
            "semantic retrieval",
            "vector search",
            "vector retrieval",
            "vector index",
            "embedding",
            "embeddings",
            "embedding-based",
            "faiss",
            "chroma",
            "chromadb",
            "sbert",
            "sentence-transformers",
            "similarity search",
            "cosine similarity",
            "hybrid search",
            "rerank",
            "reranking",
            "retriever",
            "chunking",
            "indexing",
            "grounding",
            "knowledge graph",
            "entity linking",
            "document ai",
            "multilingual retrieval",
        }
    ),
    "agentic_workflows": frozenset(
        {
            "agent",
            "agents",
            "agentic",
            "agentic workflows",
            "multi-agent",
            "tool use",
            "tool calling",
            "function calling",
            "planning",
            "reflection",
            "guardrails",
            "langgraph",
            "langchain",
            "llamaindex",
            "autogen",
            "semantic kernel",
            "smolagents",
            "react",
            "orchestration",
            "structured outputs",
            "prompt",
            "prompts",
            "schema",
            "memory",
        }
    ),
    "evaluation": frozenset(
        {
            "evaluation",
            "evaluations",
            "eval",
            "benchmark",
            "benchmarks",
            "retrieval metrics",
            "answer quality",
            "hallucination",
            "grounding checks",
            "ragas",
            "trulens",
            "golden set",
            "recall",
            "mrr",
            "quality",
            "iterative",
            "observability",
            "monitoring",
            "logging",
            "metrics",
            "tracing",
            "opentelemetry",
            "prometheus",
            "grafana",
            "latency",
        }
    ),
    "backend_api": frozenset(
        {
            "python",
            "python3",
            "flask",
            "django",
            "fastapi",
            "rest",
            "restful",
            "api",
            "apis",
            "web application",
            "backend",
            "microservices",
            "sqlalchemy",
            "alembic",
        }
    ),
    "cloud_infra": frozenset(
        {
            "aws",
            "gcp",
            "google cloud",
            "azure",
            "vertex",
            "vertex ai",
            "cloud run",
            "cloud",
            "gke",
            "kubernetes",
            "k8s",
            "docker",
            "containers",
            "deployment",
            "iam",
            "storage",
            "pub/sub",
            "kafka",
            "github actions",
            "ci/cd",
            "cicd",
            "pipeline",
            "security",
            "security best practices",
        }
    ),
    "data_storage": frozenset(
        {
            "sql",
            "postgres",
            "postgresql",
            "mysql",
            "relational",
            "database",
            "databases",
            "redis",
            "sqlite",
        }
    ),
    "frontend": frozenset(
        {
            "react",
            "next.js",
            "nextjs",
            "javascript",
            "typescript",
            "html",
            "css",
            "streamlit",
            "vue",
            "frontend",
            "web",
        }
    ),
    "testing_reliability": frozenset(
        {
            "testing",
            "test",
            "tests",
            "pytest",
            "unit tests",
            "code quality",
            "code review",
            "reliability",
            "maintainability",
            "reusability",
            "documentation",
            "clean code",
        }
    ),
}

_ALL_LEXICON_TERMS = frozenset(
    term for terms in _CAPABILITY_LEXICON.values() for term in terms
)
_MULTIWORD_LEXICON_TERMS = frozenset(
    term for term in _ALL_LEXICON_TERMS if " " in term
)

_EVIDENCE_FORMS_BY_CAPABILITY: dict[str, tuple[str, ...]] = {
    "rag_embedding_retrieval": ("code", "test", "project_document", "build_ci"),
    "agentic_workflows": ("code", "note", "project_document"),
    "evaluation": ("test", "build_ci", "project_document"),
    "backend_api": ("code", "test", "build_ci"),
    "cloud_infra": ("build_ci", "project_document", "test"),
    "data_storage": ("code", "project_document", "build_ci"),
    "frontend": ("code", "project_document"),
    "testing_reliability": ("test", "build_ci"),
    "general": ("note", "project_document", "code"),
}

_CLUSTER_EXEMPLARS: dict[str, tuple[str, ...]] = {
    "rag_embedding_retrieval": (
        "chroma",
        "chromadb",
        "faiss",
        "sbert",
        "vector index",
        "hybrid search",
        "reranking",
        "grounding",
        "knowledge graph",
    ),
    "agentic_workflows": (
        "langchain",
        "langgraph",
        "llamaindex",
        "autogen",
        "semantic kernel",
        "smolagents",
        "function calling",
        "structured outputs",
    ),
    "evaluation": (
        "ragas",
        "trulens",
        "hallucination",
        "retrieval metrics",
        "answer quality",
    ),
    "backend_api": (
        "fastapi",
        "flask",
        "django",
        "restful",
        "sqlalchemy",
        "alembic",
    ),
    "cloud_infra": (
        "aws",
        "gcp",
        "azure",
        "vertex ai",
        "google cloud",
        "kubernetes",
        "docker",
        "github actions",
        "pub/sub",
        "kafka",
    ),
    "data_storage": ("postgresql", "postgres", "mysql", "redis"),
    "frontend": ("react", "next.js", "typescript", "javascript", "streamlit"),
    "testing_reliability": ("pytest", "unit tests", "code review", "reliability"),
    "general": (),
}

_CLASS_PRIORITY: dict[EvidenceCandidateClass, int] = {
    EvidenceCandidateClass.DIRECT: 6,
    EvidenceCandidateClass.SEMANTIC: 5,
    EvidenceCandidateClass.POTENTIAL_DERIVATION: 4,
    EvidenceCandidateClass.ADJACENT_CAPABILITY: 3,
    EvidenceCandidateClass.WEAK: 2,
    EvidenceCandidateClass.IRRELEVANT: 1,
}

_CLASS_SCORE: dict[EvidenceCandidateClass, float] = {
    EvidenceCandidateClass.DIRECT: 4.0,
    EvidenceCandidateClass.SEMANTIC: 3.0,
    EvidenceCandidateClass.POTENTIAL_DERIVATION: 2.5,
    EvidenceCandidateClass.ADJACENT_CAPABILITY: 2.0,
    EvidenceCandidateClass.WEAK: 1.0,
    EvidenceCandidateClass.IRRELEVANT: 0.0,
}


class KnowledgeSearch(Protocol):
    def search(
        self, query: str, *, limit: int = _MAX_RESULTS_PER_QUERY
    ) -> list[RetrievalHit]: ...


class EvidenceSearch(Protocol):
    def search(
        self, query: str, *, limit: int = _MAX_RESULTS_PER_QUERY
    ) -> list[EvidenceItem]: ...


class GitHubEvidenceSearch(Protocol):
    def search(self, queries: Sequence[str]) -> list[ResumeRetrievalCandidate]: ...


@dataclass(frozen=True)
class CapabilityExpansion:
    capability: str
    technology_terms: tuple[str, ...]
    primary_terms: tuple[str, ...]
    synonyms: tuple[str, ...]
    expanded_terms: tuple[str, ...]
    requirement_tokens: tuple[str, ...]


def _features(text: str) -> set[str]:
    folded = text.casefold()
    features = set(_TOKEN_RE.findall(folded))
    features.update(term for term in _MULTIWORD_LEXICON_TERMS if term in folded)
    return features


def capability_expansion(text: str) -> CapabilityExpansion:
    """Normalize one requirement into capability-aware search vocabulary."""

    requirement_tokens = tuple(sorted(_features(text) - _STOPWORDS))
    technology_terms = tuple(sorted(set(requirement_tokens) & _ALL_LEXICON_TERMS))[:16]
    matched_clusters: list[tuple[str, int]] = []
    for name, terms in _CAPABILITY_LEXICON.items():
        overlap = set(requirement_tokens) & terms
        if overlap:
            matched_clusters.append((name, len(overlap)))
    if matched_clusters:
        matched_clusters.sort(key=lambda item: (-item[1], item[0]))
        capability = matched_clusters[0][0]
        primary_terms = tuple(sorted(_CAPABILITY_LEXICON[capability]))
        synonyms = tuple(
            sorted(
                {
                    term
                    for name, _ in matched_clusters
                    for term in _CAPABILITY_LEXICON[name]
                }
            )
        )
    else:
        capability = "general"
        primary_terms = ()
        synonyms = ()
    expanded_terms = tuple(dict.fromkeys((*technology_terms, *synonyms)))
    return CapabilityExpansion(
        capability=capability,
        technology_terms=technology_terms,
        primary_terms=primary_terms,
        synonyms=synonyms,
        expanded_terms=expanded_terms,
        requirement_tokens=requirement_tokens,
    )


def _evidence_forms(capability: str) -> tuple[str, ...]:
    return _EVIDENCE_FORMS_BY_CAPABILITY.get(capability, _EVIDENCE_FORMS_BY_CAPABILITY["general"])


def normalize_requirements(job_description: str) -> list[ResumeRequirementExpansion]:
    requirements = parse_requirements(job_description)
    normalized: list[ResumeRequirementExpansion] = []
    for requirement in requirements:
        expansion = capability_expansion(requirement.text)
        normalized.append(
            ResumeRequirementExpansion(
                requirement_id=requirement.id,
                text=requirement.text,
                capability=expansion.capability,
                technology_terms=list(expansion.technology_terms),
                synonyms=list(expansion.synonyms),
                expanded_terms=list(expansion.expanded_terms),
                likely_evidence_forms=list(_evidence_forms(expansion.capability)),
            )
        )
    return normalized


def classify_candidate(
    text: str, expansion: CapabilityExpansion
) -> tuple[EvidenceCandidateClass, tuple[str, ...]]:
    """Classify one candidate without destroying it. Claim truth is not decided here."""

    candidate_features = _features(text)
    direct = tuple(sorted(candidate_features & set(expansion.technology_terms)))
    if direct:
        return EvidenceCandidateClass.DIRECT, direct
    synonym_hits = tuple(sorted(candidate_features & set(expansion.primary_terms)))
    if len(synonym_hits) >= 2:
        return EvidenceCandidateClass.SEMANTIC, synonym_hits
    if len(synonym_hits) == 1:
        return EvidenceCandidateClass.POTENTIAL_DERIVATION, synonym_hits
    adjacent = tuple(
        sorted(candidate_features & (_ALL_LEXICON_TERMS - set(expansion.primary_terms)))
    )
    if adjacent:
        return EvidenceCandidateClass.ADJACENT_CAPABILITY, adjacent
    weak = tuple(sorted(candidate_features & set(expansion.requirement_tokens)))
    if weak:
        return EvidenceCandidateClass.WEAK, weak
    return EvidenceCandidateClass.IRRELEVANT, ()


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


def _mac_candidates(entries: Iterable[DiscoveryEntry]) -> list[ResumeRetrievalCandidate]:
    candidates: list[ResumeRetrievalCandidate] = []
    for entry in entries:
        if entry.record_type == "repository":
            text = _truncate(
                f"{entry.path} {entry.kind} {entry.git_branch or ''} {entry.git_remote or ''}",
                _EXCERPT_LIMIT,
            )
            candidates.append(
                ResumeRetrievalCandidate(
                    candidate_id=_stable_id("mac", entry.entry_id),
                    source_kind=ResumeRetrievalSourceKind.LOCAL_GIT,
                    source_identity=entry.path,
                    evidence_id=entry.entry_id,
                    evidence_type="repository",
                    authority=(
                        f"git_head:{entry.git_head[:12]}"
                        if entry.git_head
                        else "git_unborn"
                    ),
                    text=text,
                    rationale="local git repository/worktree identity",
                    score=0.0,
                    provenance={
                        "repository": entry.path,
                        "branch": entry.git_branch or "",
                        "commit": entry.git_head or "",
                        "remote": entry.git_remote or "",
                    },
                )
            )
        elif entry.record_type == "source":
            source_kind = (
                ResumeRetrievalSourceKind.CODEX
                if entry.kind == "codex_local"
                else ResumeRetrievalSourceKind.MAC_CATALOG
            )
            candidates.append(
                ResumeRetrievalCandidate(
                    candidate_id=_stable_id("mac", entry.entry_id),
                    source_kind=source_kind,
                    source_identity=entry.path,
                    evidence_id=entry.entry_id,
                    evidence_type=f"source_catalog:{entry.kind}",
                    authority=f"catalog_fingerprint:{entry.fingerprint[:16]}",
                    text=_truncate(f"{entry.path} {entry.kind}", _EXCERPT_LIMIT),
                    rationale="private source identity, metadata only",
                    score=0.0,
                    provenance={
                        "path": entry.path,
                        "kind": entry.kind,
                        "fingerprint": entry.fingerprint,
                    },
                )
            )
        else:
            candidates.append(
                ResumeRetrievalCandidate(
                    candidate_id=_stable_id("mac", entry.entry_id),
                    source_kind=ResumeRetrievalSourceKind.MAC_CATALOG,
                    source_identity=entry.path,
                    evidence_id=entry.entry_id,
                    evidence_type=f"file:{entry.kind}",
                    authority=f"fingerprint:{entry.fingerprint[:16]}",
                    text=_truncate(f"{entry.path} {entry.kind}", _EXCERPT_LIMIT),
                    rationale="discovered local evidence file",
                    score=0.0,
                    provenance={
                        "path": entry.path,
                        "kind": entry.kind,
                        "fingerprint": entry.fingerprint,
                    },
                )
            )
    return candidates


def _build_coverage_map(
    *,
    job_description: str,
    requirements: list[ResumeRequirementExpansion],
    expansions: list[CapabilityExpansion],
    candidates: list[ResumeRetrievalCandidate],
    source_statuses: list[ResumeRetrievalSourceStatus],
    retrieved_count: int,
) -> ResumeEvidenceCoverageMap:
    expansion_by_id = {
        requirement.requirement_id: expansion
        for requirement, expansion in zip(requirements, expansions, strict=True)
    }
    working: list[ResumeRetrievalCandidate] = []
    irrelevant_count = 0
    for candidate in candidates:
        classes: dict[str, EvidenceCandidateClass] = {}
        signals: dict[str, list[str]] = {}
        best_class = EvidenceCandidateClass.IRRELEVANT
        best_signals: tuple[str, ...] = ()
        best_requirement: ResumeRequirementExpansion | None = None
        for requirement in requirements:
            expansion = expansion_by_id[requirement.requirement_id]
            candidate_class, matched_signals = classify_candidate(
                f"{candidate.text} {candidate.source_identity}", expansion
            )
            classes[requirement.requirement_id] = candidate_class
            signals[requirement.requirement_id] = list(matched_signals)
            if _CLASS_PRIORITY[candidate_class] > _CLASS_PRIORITY[best_class]:
                best_class = candidate_class
                best_signals = matched_signals
                best_requirement = requirement
        if best_class is EvidenceCandidateClass.IRRELEVANT:
            irrelevant_count += 1
            continue
        capability = best_requirement.capability if best_requirement is not None else "general"
        rationale = (
            f"matched {', '.join(best_signals) or 'generic terms'} "
            f"for capability '{capability}'"
        )
        working.append(
            candidate.model_copy(
                update={
                    "candidate_class": best_class,
                    "signals": list(best_signals),
                    "rationale": rationale,
                    "score": _CLASS_SCORE[best_class] + 0.1 * min(len(best_signals), 10),
                    "requirement_classes": classes,
                    "requirement_signals": signals,
                }
            )
        )

    coverage: list[ResumeRetrievalCoverage] = []
    for requirement in requirements:
        buckets: dict[EvidenceCandidateClass, list[str]] = {
            candidate_class: [] for candidate_class in EvidenceCandidateClass
        }
        for candidate in working:
            candidate_class = candidate.requirement_classes.get(
                requirement.requirement_id, EvidenceCandidateClass.IRRELEVANT
            )
            buckets[candidate_class].append(candidate.candidate_id)
        diversity = sorted(
            {
                candidate.source_kind
                for candidate in working
                if candidate.requirement_classes.get(requirement.requirement_id)
                is not EvidenceCandidateClass.IRRELEVANT
            },
            key=lambda kind: kind.value,
        )
        missing: list[str] = []
        if not buckets[EvidenceCandidateClass.DIRECT]:
            if buckets[EvidenceCandidateClass.SEMANTIC]:
                missing.append(
                    f"No DIRECT lexical evidence for '{requirement.capability}'; "
                    "semantic matches exist and downstream claim truth must verify."
                )
            elif buckets[EvidenceCandidateClass.POTENTIAL_DERIVATION] or buckets[
                EvidenceCandidateClass.ADJACENT_CAPABILITY
            ]:
                missing.append(
                    f"No direct evidence for '{requirement.capability}'; only adjacent/"
                    "potential candidates were found."
                )
            else:
                missing.append(
                    f"No evidence found for '{requirement.capability}' across searched sources."
                )
        if (
            requirement.capability == "rag_embedding_retrieval"
            and not buckets[EvidenceCandidateClass.DIRECT]
        ):
            missing.append(
                "Embedding/vector terminology appears only in related components; "
                "do not infer embedding-model ownership without direct proof."
            )
        coverage.append(
            ResumeRetrievalCoverage(
                requirement_id=requirement.requirement_id,
                normalized_capability=requirement.capability,
                direct_evidence=buckets[EvidenceCandidateClass.DIRECT],
                semantic_evidence=buckets[EvidenceCandidateClass.SEMANTIC],
                related_projects=buckets[EvidenceCandidateClass.ADJACENT_CAPABILITY],
                potential_derivations=buckets[
                    EvidenceCandidateClass.POTENTIAL_DERIVATION
                ],
                weak_evidence=buckets[EvidenceCandidateClass.WEAK],
                missing_proof=missing,
                source_diversity=diversity,
            )
        )

    working.sort(key=lambda item: (-item.score, item.candidate_id))
    return ResumeEvidenceCoverageMap(
        job_description_sha256=hashlib.sha256(job_description.encode("utf-8")).hexdigest(),
        requirements=requirements,
        coverage=coverage,
        candidates=working,
        sources=source_statuses,
        retrieved_count=retrieved_count,
        kept_count=len(working),
        irrelevant_count=irrelevant_count,
    )


def build_evidence_coverage_map(
    *,
    job_description: str,
    data_root: Path,
    library_root: Path | None = None,
    mac_entries: dict[str, DiscoveryEntry] | None = None,
    knowledge_search: KnowledgeSearch | None = None,
    knowledge_source_kinds: Sequence[SourceKind] = (),
    evidence_search: EvidenceSearch | None = None,
    github_search: GitHubEvidenceSearch | None = None,
    max_queries_per_source: int = _MAX_QUERIES_PER_SOURCE,
    max_results_per_query: int = _MAX_RESULTS_PER_QUERY,
    max_total_candidates: int = _MAX_TOTAL_CANDIDATES,
) -> ResumeEvidenceCoverageMap:
    """Retrieve broadly over explicitly authorized sources and build a coverage map."""

    requirements = normalize_requirements(job_description)
    expansions = [capability_expansion(item.text) for item in requirements]
    queries = _build_queries(requirements)

    raw_candidates: list[ResumeRetrievalCandidate] = []
    source_statuses: list[ResumeRetrievalSourceStatus] = []

    entries = mac_entries if mac_entries is not None else load_discovery_catalog(data_root)
    mac_candidates = _mac_candidates(entries.values())
    raw_candidates.extend(mac_candidates)
    source_statuses.append(
        ResumeRetrievalSourceStatus(
            source_kind=ResumeRetrievalSourceKind.MAC_CATALOG,
            state="SEARCHED" if mac_candidates else "EMPTY",
            detail=(
                "durable full-Mac catalog"
                if entries
                else "no discovery catalog; run full evidence discovery"
            ),
            candidate_count=len(mac_candidates),
        )
    )
    knowledge_selected = knowledge_search is not None or bool(knowledge_source_kinds)
    if not knowledge_selected:
        source_statuses.append(
            ResumeRetrievalSourceStatus(
                source_kind=ResumeRetrievalSourceKind.KNOWLEDGE_STORE,
                state="NOT_SELECTED",
                detail="private Codex, ChatGPT, and BuildLog history was not selected",
            )
        )
    else:
        if knowledge_search is None:
            knowledge_search = _default_knowledge_search(
                data_root, source_kinds=knowledge_source_kinds
            )
        if knowledge_search is None:
            source_statuses.append(
                ResumeRetrievalSourceStatus(
                    source_kind=ResumeRetrievalSourceKind.KNOWLEDGE_STORE,
                    state="UNAVAILABLE",
                    detail="selected local knowledge index is not present",
                )
            )
        else:
            knowledge_candidates = _search_knowledge(
                knowledge_search,
                queries,
                max_queries_per_source=max_queries_per_source,
                max_results_per_query=max_results_per_query,
            )
            raw_candidates.extend(knowledge_candidates)
            source_statuses.append(
                ResumeRetrievalSourceStatus(
                    source_kind=ResumeRetrievalSourceKind.KNOWLEDGE_STORE,
                    state="SEARCHED" if knowledge_candidates else "EMPTY",
                    detail="explicitly selected local knowledge sources",
                    candidate_count=len(knowledge_candidates),
                )
            )
    if evidence_search is None:
        evidence_search = _default_evidence_search(data_root)
    if evidence_search is None:
        source_statuses.append(
            ResumeRetrievalSourceStatus(
                source_kind=ResumeRetrievalSourceKind.EVIDENCE_HUB,
                state="UNAVAILABLE",
                detail="EvidenceHub catalog not present",
            )
        )
    else:
        hub_candidates = _search_evidence_hub(
            evidence_search,
            queries,
            max_queries_per_source=max_queries_per_source,
            max_results_per_query=max_results_per_query,
        )
        raw_candidates.extend(hub_candidates)
        source_statuses.append(
            ResumeRetrievalSourceStatus(
                source_kind=ResumeRetrievalSourceKind.EVIDENCE_HUB,
                state="SEARCHED" if hub_candidates else "EMPTY",
                candidate_count=len(hub_candidates),
            )
        )
    if library_root is None:
        source_statuses.append(
            ResumeRetrievalSourceStatus(
                source_kind=ResumeRetrievalSourceKind.RESUME_LIBRARY,
                state="UNAVAILABLE",
                detail="application library root not configured",
            )
        )
    else:
        library_candidates = _search_resume_library(library_root)
        raw_candidates.extend(library_candidates)
        source_statuses.append(
            ResumeRetrievalSourceStatus(
                source_kind=ResumeRetrievalSourceKind.RESUME_LIBRARY,
                state="SEARCHED" if library_candidates else "EMPTY",
                candidate_count=len(library_candidates),
            )
        )
    if github_search is None:
        github_search = _default_github_search(data_root)
    if github_search is None:
        source_statuses.append(
            ResumeRetrievalSourceStatus(
                source_kind=ResumeRetrievalSourceKind.GITHUB,
                state="UNAVAILABLE",
                detail="GitHub connection is not configured",
            )
        )
    else:
        github_candidates = list(github_search.search(queries))
        raw_candidates.extend(github_candidates)
        source_statuses.append(
            ResumeRetrievalSourceStatus(
                source_kind=ResumeRetrievalSourceKind.GITHUB,
                state="SEARCHED" if github_candidates else "EMPTY",
                candidate_count=len(github_candidates),
            )
        )
    deduplicated = _dedupe_candidates(raw_candidates)[:max_total_candidates]
    return _build_coverage_map(
        job_description=job_description,
        requirements=requirements,
        expansions=expansions,
        candidates=deduplicated,
        source_statuses=source_statuses,
        retrieved_count=len(deduplicated),
    )


def _build_queries(requirements: list[ResumeRequirementExpansion]) -> tuple[str, ...]:
    technology_lists = [
        list(requirement.technology_terms[:_MAX_TERMS_PER_REQUIREMENT])
        for requirement in requirements
    ]
    exemplar_lists = [
        list(_CLUSTER_EXEMPLARS.get(requirement.capability, ()))
        for requirement in requirements
    ]
    exemplar_set = {
        term for exemplars in exemplar_lists for term in exemplars
    }
    synonym_lists = [
        [
            term
            for term in requirement.synonyms[:_MAX_TERMS_PER_REQUIREMENT * 2]
            if term not in exemplar_set
        ]
        for requirement in requirements
    ]
    technology = _round_robin(technology_lists)
    supplementary: list[str] = []
    for exemplars in exemplar_lists:
        for term in exemplars:
            if term not in supplementary:
                supplementary.append(term)
    supplementary.extend(_round_robin(synonym_lists))
    ordered: list[str] = []
    longest = max(len(technology), len(supplementary))
    for index in range(longest):
        if index < len(technology) and technology[index] not in ordered:
            ordered.append(technology[index])
        if index < len(supplementary) and supplementary[index] not in ordered:
            ordered.append(supplementary[index])
    return tuple(ordered)


def _round_robin(term_lists: Sequence[Sequence[str]]) -> list[str]:
    ordered: list[str] = []
    longest = max((len(items) for items in term_lists), default=0)
    for index in range(longest):
        for items in term_lists:
            if index < len(items) and items[index] not in ordered:
                ordered.append(items[index])
    return ordered


def _dedupe_candidates(
    candidates: Sequence[ResumeRetrievalCandidate],
) -> list[ResumeRetrievalCandidate]:
    seen: dict[str, ResumeRetrievalCandidate] = {}
    for candidate in candidates:
        seen.setdefault(candidate.candidate_id, candidate)
    return list(seen.values())


def _default_knowledge_search(
    data_root: Path, *, source_kinds: Sequence[SourceKind]
) -> KnowledgeSearch | None:
    index_path = Path(data_root) / "knowledge" / "index.sqlite3"
    if index_path.is_symlink() or not index_path.is_file():
        return None
    return _KnowledgeStoreAdapter(
        KnowledgeStore(Path(data_root)), source_kinds=source_kinds
    )


def _default_evidence_search(data_root: Path) -> EvidenceSearch | None:
    if not EvidenceHub.catalog_exists(data_root):
        return None
    return _EvidenceHubAdapter(EvidenceHub(Path(data_root)))


def _default_github_search(data_root: Path) -> GitHubEvidenceSearch | None:
    from soloscale.github_connect import GitHubConnectionStore

    try:
        state = GitHubConnectionStore(Path(data_root)).load()
    except ValueError:
        return None
    if state is None:
        return None
    return _GitHubInventoryAdapter(state.selected_repositories)


def _search_resume_library(library_root: Path) -> list[ResumeRetrievalCandidate]:
    from soloscale.application_record import list_application_records

    candidates: list[ResumeRetrievalCandidate] = []
    for record in list_application_records(library_root):
        text = _truncate(
            f"{record.company or ''} {record.role or ''} "
            f"{' '.join(record.notes)} {record.resume_filename or ''}",
            _EXCERPT_LIMIT,
        )
        candidates.append(
            ResumeRetrievalCandidate(
                candidate_id=_stable_id("resume-library", record.application_id),
                source_kind=ResumeRetrievalSourceKind.RESUME_LIBRARY,
                source_identity=f"{record.company or 'unknown'} {record.role or 'unknown'}",
                evidence_id=record.application_id,
                evidence_type="application_record",
                authority=f"application:{record.application_id}",
                text=text,
                rationale="prior resume application record",
                score=0.0,
                provenance={
                    "application_id": record.application_id,
                    "company": record.company or "",
                    "role": record.role or "",
                    "status": record.status.value,
                },
            )
        )
    return candidates


class _KnowledgeStoreAdapter:
    def __init__(
        self, store: KnowledgeStore, *, source_kinds: Sequence[SourceKind]
    ) -> None:
        self._store = store
        self._source_kinds = tuple(source_kinds)

    def search(
        self, query: str, *, limit: int = _MAX_RESULTS_PER_QUERY
    ) -> list[RetrievalHit]:
        return list(
            self._store.search(
                query, limit=limit, source_kinds=self._source_kinds
            )
        )


class _EvidenceHubAdapter:
    def __init__(self, hub: EvidenceHub) -> None:
        self._hub = hub

    def search(
        self, query: str, *, limit: int = _MAX_RESULTS_PER_QUERY
    ) -> list[EvidenceItem]:
        return list(self._hub.search(query, limit=limit))


class _GitHubInventoryAdapter:
    def __init__(self, repositories: Sequence[GitHubRepository]) -> None:
        self._repositories = list(repositories)

    def search(self, queries: Sequence[str]) -> list[ResumeRetrievalCandidate]:
        candidates: list[ResumeRetrievalCandidate] = []
        for repository in self._repositories:
            full_name = repository.full_name
            html_url = repository.html_url
            candidates.append(
                ResumeRetrievalCandidate(
                    candidate_id=_stable_id("github", full_name),
                    source_kind=ResumeRetrievalSourceKind.GITHUB,
                    source_identity=full_name,
                    evidence_id=full_name,
                    evidence_type="repository_inventory",
                    authority=f"github:{full_name}",
                    text=_truncate(f"{full_name} {html_url}", _EXCERPT_LIMIT),
                    rationale="saved GitHub repository inventory (metadata only)",
                    score=0.0,
                    provenance={"repository": full_name, "url": html_url},
                )
            )
        return candidates


def _search_knowledge(
    search: KnowledgeSearch,
    queries: Sequence[str],
    *,
    max_queries_per_source: int,
    max_results_per_query: int,
) -> list[ResumeRetrievalCandidate]:
    candidates: list[ResumeRetrievalCandidate] = []
    for query in queries[:max_queries_per_source]:
        for hit in search.search(query, limit=max_results_per_query):
            source_kind = {
                "codex_session": ResumeRetrievalSourceKind.CODEX,
                "chatgpt_export": ResumeRetrievalSourceKind.CHATGPT,
                "buildlog_run": ResumeRetrievalSourceKind.BUILDLOG,
            }.get(hit.source_kind.value, ResumeRetrievalSourceKind.KNOWLEDGE_STORE)
            candidates.append(
                ResumeRetrievalCandidate(
                    candidate_id=_stable_id("knowledge", hit.chunk_sha256),
                    source_kind=source_kind,
                    source_identity=hit.title or hit.external_id,
                    evidence_id=hit.chunk_id,
                    evidence_type=f"chunk:{hit.role.value}",
                    authority=f"chunk_sha256:{hit.chunk_sha256[:16]}",
                    text=_truncate(hit.excerpt, _EXCERPT_LIMIT),
                    rationale=f"knowledge retrieval for '{query}'",
                    score=0.0,
                    provenance={
                        "chunk_sha256": hit.chunk_sha256,
                        "document_sha256": hit.document_sha256,
                        "locator": hit.locator,
                        "external_id": hit.external_id,
                        "title": hit.title or "",
                        "source_kind": hit.source_kind.value,
                    },
                )
            )
        if len(candidates) >= _MAX_CANDIDATES_PER_SOURCE:
            break
    return candidates


def _search_evidence_hub(
    search: EvidenceSearch,
    queries: Sequence[str],
    *,
    max_queries_per_source: int,
    max_results_per_query: int,
) -> list[ResumeRetrievalCandidate]:
    candidates: list[ResumeRetrievalCandidate] = []
    for query in queries[:max_queries_per_source]:
        for item in search.search(query, limit=max_results_per_query):
            candidates.append(
                ResumeRetrievalCandidate(
                    candidate_id=_stable_id("evidence-hub", item.evidence_id),
                    source_kind=ResumeRetrievalSourceKind.EVIDENCE_HUB,
                    source_identity=item.project or item.source_id,
                    evidence_id=item.evidence_id,
                    evidence_type=item.evidence_type,
                    authority=f"verification:{item.verification_status}",
                    text=_truncate(item.public_safe_summary, _EXCERPT_LIMIT),
                    rationale=f"EvidenceHub metadata match for '{query}'",
                    score=0.0,
                    provenance={
                        "evidence_id": item.evidence_id,
                        "source_id": item.source_id,
                        "truth_class": item.truth_class.value,
                        "verification_status": item.verification_status,
                    },
                )
            )
        if len(candidates) >= _MAX_CANDIDATES_PER_SOURCE:
            break
    return candidates
