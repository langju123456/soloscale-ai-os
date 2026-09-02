"""Compact verified evidence and deterministic JD positioning for Resume."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from soloscale.content_canon import CanonicalStory, StoryReadiness, load_month_one_canon
from soloscale.evidence_hub import EvidenceHub, EvidenceHubError, inspect_git_repository
from soloscale.github_connect import GitHubConnectionStore, is_resume_safe_commit_summary
from soloscale.knowledge_models import RetrievalHit
from soloscale.resume_models import (
    CandidateEvidencePack,
    CandidateEvidenceSource,
    CandidateProfile,
    CompositionEvidencePlan,
    CompositionRequirementPlan,
    JDPositioningBrief,
    ResumeAtomicFact,
    ResumeEvidenceAdoptionTrace,
    ResumeEvidenceRetrievalHit,
    ResumeEvidenceRetrievalTrace,
    ResumeEvidenceSourceSummary,
    ResumeRequirementCoverage,
    admit_resume_atomic_facts,
)

_HEADING_LINES = {
    "about the job",
    "job description",
    "job responsibilities",
    "preferred qualifications",
    "qualifications",
    "requirements",
    "responsibilities",
}
_THEMES = (
    "Python",
    "RAG",
    "retrieval",
    "reranking",
    "embeddings",
    "structured outputs",
    "agentic workflows",
    "evaluation",
    "FastAPI",
    "Flask",
    "Django",
    "Vertex AI",
    "GCP",
    "AWS",
    "PostgreSQL",
    "CI/CD",
    "observability",
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]{2,}")
_MAX_SENT_FACTS = 80
_MIN_SENT_FACTS = 30
_MAX_RETRIEVAL_HITS = 32
_FACTS_PER_REQUIREMENT = 4
_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?%?\+?(?!\w)")


def deterministic_hiring_signals(job_description: str) -> list[str]:
    """Return bounded exact JD spans without asking a model to restate them."""

    lines = [
        line.strip(" -•\t")
        for line in job_description.splitlines()
        if line.strip(" -•\t")
    ]
    if len(lines) == 1:
        lines = [
            fragment.strip()
            for fragment in re.split(r"[.;；]", lines[0])
            if fragment.strip()
        ]
    candidates = [
        line
        for line in lines
        if line.casefold().rstrip(":") not in _HEADING_LINES
        and 3 <= len(line) <= 360
    ]
    return list(dict.fromkeys(candidates[:8])) or [
        job_description.strip()[:360]
    ]


def _entry_terms(value: str) -> set[str]:
    return {
        normalized
        for token in _TOKEN_RE.findall(value)
        if (normalized := token.casefold().strip("./-"))
    }


_HIGH_SIGNAL_TERMS = {
    term for theme in _THEMES for term in _entry_terms(theme)
}


def _soloscale_target(profile: CandidateProfile) -> str | None:
    entries = profile.experience_bullets + profile.project_bullets
    for index, text in enumerate(entries, start=1):
        folded = text.casefold()
        if "soloscale" in folded or "solo scale" in folded:
            return f"PROFILE-{index:02d}"
    # The real Resume template keeps the project name in a non-bullet heading,
    # while CandidateProfile intentionally retains only approved bullets. Match
    # the first distinctive SoloScale project bullet without weakening the
    # general profile truth boundary.
    markers = (
        "evidence-grounded",
        "retrieval-backed resume",
        "conversation rag",
        "evidence agent",
        "resume workspace",
    )
    for index, text in enumerate(entries, start=1):
        folded = text.casefold()
        if sum(marker in folded for marker in markers) >= 2:
            return f"PROFILE-{index:02d}"
    return None


def _story_fact_texts(story: CanonicalStory) -> list[tuple[str, str | None]]:
    facts: list[tuple[str, str | None]] = [
        (story.fact, None),
        (story.architecture, None),
        (story.decision, None),
        (story.implementation, None),
        (story.higher_level_insight, None),
    ]
    facts.extend((metric, metric) for metric in story.verified_metrics)
    return facts[:8]


def _allowed_numbers(*values: str | None) -> list[str]:
    return list(
        dict.fromkeys(
            match.group(0).casefold()
            for value in values
            if value
            for match in _NUMBER_RE.finditer(value)
        )
    )


def _compact_verified_facts(
    *,
    job_description: str,
    atomic_facts: list[ResumeAtomicFact],
    hiring_signals: list[str] | None = None,
) -> list[ResumeAtomicFact]:
    """Keep a compact, requirement-balanced set of already approved facts."""

    profile_facts = [
        fact for fact in atomic_facts if fact.source_kind == "PROFILE_ENTRY"
    ]
    candidate_facts = [
        fact for fact in atomic_facts if fact.source_kind == "CANDIDATE_EVIDENCE"
    ]
    if not job_description.strip():
        return atomic_facts
    candidate_limit = max(0, _MAX_SENT_FACTS - len(profile_facts))
    if candidate_limit == 0:
        return profile_facts

    requirements = [
        terms
        for signal in (hiring_signals or deterministic_hiring_signals(job_description))
        if (terms := _entry_terms(signal))
    ]
    if not requirements:
        requirements = [_entry_terms(job_description)]
    fact_terms = {fact.fact_id: _entry_terms(fact.text) for fact in candidate_facts}
    tag_terms = {
        fact.fact_id: {
            term
            for tag in fact.capability_tags
            for term in _entry_terms(tag)
        }
        for fact in candidate_facts
    }
    original_order = {fact.fact_id: index for index, fact in enumerate(candidate_facts)}

    def authority(fact: ResumeAtomicFact) -> int:
        if fact.metric is not None and fact.evidence_id.startswith("EVIDENCE-M1-"):
            return 5
        if fact.evidence_id.startswith("EVIDENCE-M1-"):
            return 4
        if "verified-commit" in fact.capability_tags:
            return 3
        if "committed-summary" in fact.capability_tags:
            return 1
        return 0

    def requirement_score(
        fact: ResumeAtomicFact, requirement_terms: set[str]
    ) -> tuple[int, int, int, int]:
        lexical = len(fact_terms[fact.fact_id] & requirement_terms)
        tags = len(tag_terms[fact.fact_id] & requirement_terms)
        high_signal_match = bool(
            (fact_terms[fact.fact_id] | tag_terms[fact.fact_id])
            & requirement_terms
            & _HIGH_SIGNAL_TERMS
        )
        fact_authority = authority(fact)
        return (
            int(
                lexical >= 3
                or tags >= 1
                or high_signal_match
                # Verified evidence (metrics, rich events, and committed repository
                # summaries) is never silently dropped by JD relevance compaction.
                or fact_authority > 0
            ),
            lexical,
            tags,
            fact_authority,
        )

    selected: list[ResumeAtomicFact] = []
    selected_ids: set[str] = set()
    for requirement_terms in requirements:
        ranked = sorted(
            candidate_facts,
            key=lambda fact: (
                *(-value for value in requirement_score(fact, requirement_terms)),
                original_order[fact.fact_id],
                fact.fact_id,
            ),
        )
        added = 0
        for fact in ranked:
            relevant, _lexical, _tags, _authority = requirement_score(
                fact, requirement_terms
            )
            if relevant == 0:
                break
            if fact.fact_id in selected_ids:
                continue
            selected.append(fact)
            selected_ids.add(fact.fact_id)
            added += 1
            if added == _FACTS_PER_REQUIREMENT or len(selected) == candidate_limit:
                break
        if len(selected) == candidate_limit:
            break

    def global_score(fact: ResumeAtomicFact) -> tuple[int, int, int, int]:
        scores = [requirement_score(fact, terms) for terms in requirements]
        return max(scores, default=(0, 0, 0, authority(fact)))

    ranked_remaining = sorted(
        (fact for fact in candidate_facts if fact.fact_id not in selected_ids),
        key=lambda fact: (
            *(-value for value in global_score(fact)),
            original_order[fact.fact_id],
            fact.fact_id,
        ),
    )
    minimum_candidate_count = min(
        candidate_limit,
        max(0, _MIN_SENT_FACTS - len(profile_facts)),
    )
    for fact in ranked_remaining:
        relevant, _lexical, _tags, _authority = global_score(fact)
        if relevant == 0 and len(selected) >= minimum_candidate_count:
            break
        selected.append(fact)
        selected_ids.add(fact.fact_id)
        if len(selected) == candidate_limit:
            break
    return [*profile_facts, *selected]


def build_composition_evidence_plan(
    job_description: str,
    facts: list[ResumeAtomicFact],
    *,
    hiring_signals: list[str] | None = None,
) -> CompositionEvidencePlan:
    """Allocate only relevant verified facts to exact JD requirements."""

    signals = hiring_signals or deterministic_hiring_signals(job_description)
    fact_terms = {fact.fact_id: _entry_terms(fact.text) for fact in facts}
    tag_terms = {
        fact.fact_id: {
            term for tag in fact.capability_tags for term in _entry_terms(tag)
        }
        for fact in facts
    }

    def rank(fact: ResumeAtomicFact, terms: set[str]) -> tuple[int, ...]:
        lexical = len(fact_terms[fact.fact_id] & terms)
        tags = len(tag_terms[fact.fact_id] & terms)
        high_signal = int(
            bool(
                (fact_terms[fact.fact_id] | tag_terms[fact.fact_id])
                & terms
                & _HIGH_SIGNAL_TERMS
            )
        )
        relevant = int(lexical >= 2 or tags >= 1 or high_signal > 0)
        authority = (
            5
            if fact.metric is not None and fact.evidence_id.startswith("EVIDENCE-M1-")
            else 4
            if fact.evidence_id.startswith("EVIDENCE-M1-")
            else 3
            if "verified-commit" in fact.capability_tags
            else 2
            if fact.source_kind == "PROFILE_ENTRY"
            else 1
        )
        differentiation = int(fact.source_kind == "CANDIDATE_EVIDENCE") + int(
            len(fact_terms[fact.fact_id]) >= 8
        )
        freshness = int("verified-commit" in fact.capability_tags)
        return relevant, high_signal, authority, lexical, tags, freshness, differentiation

    requirements: list[CompositionRequirementPlan] = []
    prioritized: list[str] = []
    for index, signal in enumerate(signals, start=1):
        terms = _entry_terms(signal)
        ranked = sorted(
            facts,
            key=lambda fact: (
                *(-value for value in rank(fact, terms)),
                fact.fact_id,
            ),
        )
        relevant_ids = [fact.fact_id for fact in ranked if rank(fact, terms)[0]][:8]
        primary = relevant_ids[:2]
        secondary = relevant_ids[2:6]
        for fact_id in (*primary, *secondary):
            if fact_id not in prioritized:
                prioritized.append(fact_id)
        requirements.append(
            CompositionRequirementPlan(
                requirement_id=f"REQ-{index:02d}",
                requirement_sha256=hashlib.sha256(signal.encode()).hexdigest(),
                primary_fact_ids=primary,
                secondary_fact_ids=secondary,
            )
        )
    return CompositionEvidencePlan(
        job_description_sha256=hashlib.sha256(job_description.encode()).hexdigest(),
        requirements=requirements,
        prioritized_fact_ids=prioritized,
    )


def build_resume_evidence_retrieval_trace(
    *,
    job_description: str,
    facts: list[ResumeAtomicFact],
    knowledge_hits: list[RetrievalHit],
    adoption: list[ResumeEvidenceAdoptionTrace] | None = None,
) -> ResumeEvidenceRetrievalTrace:
    """Build a local, body-free trace after the model input is frozen."""

    if not job_description.strip():
        raise ValueError("job description must not be empty")
    signals = deterministic_hiring_signals(job_description)
    requirement_terms = {
        f"REQ-{index:02d}": _entry_terms(signal)
        for index, signal in enumerate(signals, start=1)
    }
    fact_terms = {fact.fact_id: _entry_terms(fact.text) for fact in facts}
    requirements: list[ResumeRequirementCoverage] = []
    for index, signal in enumerate(signals, start=1):
        requirement_id = f"REQ-{index:02d}"
        terms = requirement_terms[requirement_id]
        ranked_fact_ids = [
            fact_id
            for fact_id, _score in sorted(
                (
                    (fact_id, len(terms & terms_for_fact))
                    for fact_id, terms_for_fact in fact_terms.items()
                    if len(terms & terms_for_fact) >= 3
                    or bool(terms & terms_for_fact & _HIGH_SIGNAL_TERMS)
                ),
                key=lambda item: (-item[1], item[0]),
            )[:12]
        ]
        requirements.append(
            ResumeRequirementCoverage(
                requirement_id=requirement_id,
                requirement_sha256=hashlib.sha256(signal.encode()).hexdigest(),
                status=(
                    "STRONG"
                    if len(ranked_fact_ids) >= 2
                    else "MEDIUM"
                    if ranked_fact_ids
                    else "GAP"
                ),
                matched_fact_ids=ranked_fact_ids,
            )
        )
    trace_hits: list[ResumeEvidenceRetrievalHit] = []
    for hit in knowledge_hits[:_MAX_RETRIEVAL_HITS]:
        hit_terms = _entry_terms(hit.excerpt)
        matching_requirements = [
            requirement_id
            for requirement_id, terms in requirement_terms.items()
            if terms & hit_terms
        ]
        matched_fact_ids = [
            fact_id
            for fact_id, terms in fact_terms.items()
            if len(terms & hit_terms) >= 2
        ][:12]
        trace_hits.append(
            ResumeEvidenceRetrievalHit(
                retrieval_id=hashlib.sha256(
                    f"{hit.source_kind.value}\0{hit.chunk_id}".encode()
                ).hexdigest(),
                source_kind=hit.source_kind.value,
                chunk_sha256=hit.chunk_sha256,
                document_sha256=hit.document_sha256,
                score=hit.score,
                requirement_ids=matching_requirements,
                matched_fact_ids=matched_fact_ids,
            )
        )
    fact_ids = [fact.fact_id for fact in facts]
    source_type_for_evidence: dict[str, str] = {}
    for fact in facts:
        if fact.source_kind == "PROFILE_ENTRY":
            source_type_for_evidence[fact.fact_id] = "EXISTING_RESUME"
        elif fact.evidence_id == "EVIDENCE-LOCAL-GIT":
            source_type_for_evidence[fact.fact_id] = "LOCAL_GIT"
        elif fact.evidence_id == "EVIDENCE-GITHUB-COMMITS":
            source_type_for_evidence[fact.fact_id] = "GITHUB"
        elif fact.evidence_id.startswith("EVIDENCE-M1-"):
            source_type_for_evidence[fact.fact_id] = "CONTENT_CANON"
    fact_counts = Counter(source_type_for_evidence.values())
    knowledge_source_map = {
        "buildlog_run": "BUILDLOG",
        "codex_session": "CODEX",
        "chatgpt_export": "CHATGPT",
    }
    context_counts = Counter(
        knowledge_source_map[hit.source_kind.value]
        for hit in knowledge_hits
        if hit.source_kind.value in knowledge_source_map
    )
    source_summaries = [
        ResumeEvidenceSourceSummary(
            source_type=source_type,  # type: ignore[arg-type]
            state=(
                "UNAVAILABLE"
                if source_type in {"LEARNING", "RESUME_HISTORY"}
                else "MATCHED"
                if fact_counts[source_type] or context_counts[source_type]
                else "NO_MATCH"
            ),
            retrieved_count=fact_counts[source_type] + context_counts[source_type],
            admitted_count=fact_counts[source_type],
            context_only_count=context_counts[source_type],
            sent_count=fact_counts[source_type],
        )
        for source_type in (
            "EXISTING_RESUME",
            "LOCAL_GIT",
            "GITHUB",
            "BUILDLOG",
            "CODEX",
            "CHATGPT",
            "CONTENT_CANON",
            "LEARNING",
            "RESUME_HISTORY",
        )
    ]
    return ResumeEvidenceRetrievalTrace(
        job_description_sha256=hashlib.sha256(job_description.encode()).hexdigest(),
        source_counts=dict(
            sorted(Counter(hit.source_kind.value for hit in knowledge_hits).items())
        ),
        hits=trace_hits,
        requirements=requirements,
        sources=source_summaries,
        retrieved_count=len(facts) + len(knowledge_hits),
        admitted_count=len(fact_ids),
        sent_count=len(fact_ids),
        admitted_fact_ids=fact_ids,
        sent_fact_ids=fact_ids,
        adoption=adoption or [],
    )


def _append_repository_facts(
    *,
    profile: CandidateProfile,
    atomic_facts: list[ResumeAtomicFact],
    sources: list[CandidateEvidenceSource],
    data_root: Path,
    repository_root: Path,
    snapshot_is_current: bool = False,
) -> None:
    target_id = _soloscale_target(profile)
    if target_id is None:
        return
    if not EvidenceHub.catalog_exists(data_root):
        raise EvidenceHubError("local project evidence has not been refreshed")
    stored_snapshot = EvidenceHub(data_root).git_repository_snapshot(repository_root)
    if stored_snapshot is None:
        raise EvidenceHubError("local project evidence has not been refreshed")
    stored_source, stored_items = stored_snapshot
    if not snapshot_is_current:
        current_source, _ = inspect_git_repository(repository_root)
        if stored_source.content_sha256 != current_source.content_sha256:
            raise EvidenceHubError("local project evidence is stale")
    commit_items = sorted(
        (
            item
            for item in stored_items
            if item.evidence_type == "git_commit"
            and item.verification_status == "git_object_verified"
        ),
        key=lambda item: (item.source_at or item.captured_at, item.native_id),
        reverse=True,
    )[:12]
    if not commit_items:
        return
    insert_at = len(atomic_facts)
    evidence_id = "EVIDENCE-LOCAL-GIT"
    sources.insert(
        0,
        CandidateEvidenceSource(
            evidence_id=evidence_id,
            project=stored_source.project or repository_root.name,
            source_kind="REPOSITORY",
            source_sha256=stored_source.content_sha256,
            source_refs=[item.native_id for item in commit_items],
        ),
    )
    repository_facts: list[ResumeAtomicFact] = []
    safe_commit_items = [
        item
        for item in commit_items
        if is_resume_safe_commit_summary(
            item.public_safe_summary.removeprefix("Repository commit: ").strip()
        )
    ]
    for index, item in enumerate(safe_commit_items, start=1):
        subject = item.public_safe_summary.removeprefix("Repository commit: ").strip()
        fact_id = f"FACT-{evidence_id}-{index:02d}"
        text = f"Verified repository commit: {subject}"
        repository_facts.append(
            ResumeAtomicFact(
                fact_id=fact_id,
                profile_entry_id=target_id,
                evidence_id=evidence_id,
                source_kind="CANDIDATE_EVIDENCE",
                project=stored_source.project or repository_root.name,
                capability_tags=["repository", "verified-commit"],
                source_refs=[item.native_id],
                text=text,
                source_sha256=stored_source.content_sha256,
                fact_sha256=hashlib.sha256(
                    f"{fact_id}\0{target_id}\0{text}".encode()
                ).hexdigest(),
            )
        )
    atomic_facts[insert_at:insert_at] = repository_facts


def _append_github_commit_facts(
    *,
    profile: CandidateProfile,
    atomic_facts: list[ResumeAtomicFact],
    sources: list[CandidateEvidenceSource],
    data_root: Path,
) -> None:
    """Admit only bounded committed summaries from explicitly selected GitHub repos."""

    target_id = _soloscale_target(profile)
    if target_id is None or not EvidenceHub.catalog_exists(data_root):
        return
    try:
        connection = GitHubConnectionStore(data_root).load()
    except (OSError, ValueError):
        return
    if connection is None or connection.evidence_refreshed_at is None:
        return
    hub = EvidenceHub(data_root)
    candidates = []
    for repository in connection.selected_repositories:
        candidates.extend(
            hub.search_metadata(
                "GitHub commit:",
                limit=12,
                source_types=["selected_repository_snapshot"],
                project_id=repository.full_name,
            )
        )
    local_commit_ids = {
        reference
        for source in sources
        if source.evidence_id == "EVIDENCE-LOCAL-GIT"
        for reference in source.source_refs
    }
    commits = sorted(
        (
            item
            for item in candidates
            if item.evidence_type == "github_commit"
            and item.verification_status == "github_api_observed_not_role_verified"
            and item.native_id not in local_commit_ids
        ),
        key=lambda item: (item.source_at or item.captured_at, item.native_id),
        reverse=True,
    )
    commits = [
        item
        for item in commits
        if is_resume_safe_commit_summary(
            item.public_safe_summary.removeprefix("GitHub commit: ").strip()
        )
    ][:12]
    if not commits:
        return
    source_sha256 = hashlib.sha256(
        json.dumps(
            [
                {
                    "evidence_id": item.evidence_id,
                    "content_sha256": item.content_sha256,
                }
                for item in commits
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    evidence_id = "EVIDENCE-GITHUB-COMMITS"
    sources.append(
        CandidateEvidenceSource(
            evidence_id=evidence_id,
            project="Selected GitHub repositories",
            source_kind="REPOSITORY",
            source_sha256=source_sha256,
            source_refs=[item.native_id for item in commits],
        )
    )
    for index, item in enumerate(commits, start=1):
        subject = item.public_safe_summary.removeprefix("GitHub commit: ").strip()
        fact_id = f"FACT-{evidence_id}-{index:02d}"
        text = f"Committed repository summary: {subject}"
        atomic_facts.append(
            ResumeAtomicFact(
                fact_id=fact_id,
                profile_entry_id=target_id,
                evidence_id=evidence_id,
                source_kind="CANDIDATE_EVIDENCE",
                project=item.project,
                capability_tags=["repository", "committed-summary"],
                source_refs=[item.native_id],
                text=text,
                source_sha256=source_sha256,
                fact_sha256=hashlib.sha256(
                    f"{fact_id}\0{target_id}\0{text}".encode()
                ).hexdigest(),
            )
        )


def build_candidate_evidence_pack(
    profile: CandidateProfile,
    *,
    data_root: Path | None = None,
    repository_root: Path | None = None,
    job_description: str = "",
    repository_snapshot_is_current: bool = False,
    timing: Callable[[str, int], None] | None = None,
) -> CandidateEvidencePack:
    """Retrieve locally, admit verified sources, and build compact model context."""

    admission_started = time.perf_counter()
    atomic_facts, fact_admission = admit_resume_atomic_facts(profile)
    sources: list[CandidateEvidenceSource] = []
    if repository_root is not None and data_root is None:
        raise ValueError("data_root is required when repository_root is provided")
    if data_root is not None and repository_root is not None:
        _append_repository_facts(
            profile=profile,
            atomic_facts=atomic_facts,
            sources=sources,
            data_root=Path(data_root),
            repository_root=Path(repository_root),
            snapshot_is_current=repository_snapshot_is_current,
        )
    if data_root is not None:
        _append_github_commit_facts(
            profile=profile,
            atomic_facts=atomic_facts,
            sources=sources,
            data_root=Path(data_root),
        )
    target_id = _soloscale_target(profile)
    if target_id is not None:
        for story in load_month_one_canon().stories:
            if story.status != StoryReadiness.READY_FOR_PRODUCTION:
                continue
            story_id = story.story_id
            evidence_id = f"EVIDENCE-{story_id}"
            source_payload = story.model_dump_json()
            source_sha256 = hashlib.sha256(source_payload.encode()).hexdigest()
            sources.append(
                CandidateEvidenceSource(
                    evidence_id=evidence_id,
                    project="SoloScale AI OS",
                    source_kind="TRACKED_CANON",
                    source_sha256=source_sha256,
                    source_refs=story.evidence_candidates,
                )
            )
            for index, (text, metric) in enumerate(
                _story_fact_texts(story), start=1
            ):
                fact_id = f"FACT-{evidence_id}-{index:02d}"
                atomic_facts.append(
                    ResumeAtomicFact(
                        fact_id=fact_id,
                        profile_entry_id=target_id,
                        evidence_id=evidence_id,
                        source_kind="CANDIDATE_EVIDENCE",
                        project="SoloScale AI OS",
                        capability_tags=[
                            "desktop-ai",
                            "background-jobs",
                            "performance",
                            "evidence-grounding",
                        ],
                        metric=metric,
                        allowed_numbers=_allowed_numbers(text, metric),
                        source_refs=story.evidence_candidates,
                        text=text,
                        source_sha256=source_sha256,
                        fact_sha256=hashlib.sha256(
                            f"{fact_id}\0{target_id}\0{text}".encode()
                        ).hexdigest(),
                    )
                )
    if timing is not None:
        timing(
            "evidence_admission_ms",
            int((time.perf_counter() - admission_started) * 1000),
        )
    requirement_started = time.perf_counter()
    hiring_signals = deterministic_hiring_signals(job_description)
    if timing is not None:
        timing(
            "requirement_extraction_ms",
            int((time.perf_counter() - requirement_started) * 1000),
        )
    fusion_started = time.perf_counter()
    atomic_facts = _compact_verified_facts(
        job_description=job_description,
        atomic_facts=atomic_facts,
        hiring_signals=hiring_signals,
    )
    composition_plan = build_composition_evidence_plan(
        job_description,
        atomic_facts,
        hiring_signals=hiring_signals,
    )
    if timing is not None:
        timing(
            "fusion_ms",
            int((time.perf_counter() - fusion_started) * 1000),
        )
    pack_started = time.perf_counter()
    selected_evidence_ids = {
        fact.evidence_id
        for fact in atomic_facts
        if fact.source_kind == "CANDIDATE_EVIDENCE"
    }
    sources = [
        source for source in sources if source.evidence_id in selected_evidence_ids
    ]
    canonical = json.dumps(
        {
            "sources": [source.model_dump(mode="json") for source in sources],
            "atomic_facts": [fact.model_dump(mode="json") for fact in atomic_facts],
            "fact_admission": fact_admission.model_dump(mode="json"),
            "composition_plan": composition_plan.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    pack = CandidateEvidencePack(
        sources=sources,
        atomic_facts=atomic_facts,
        fact_admission=fact_admission,
        composition_plan=composition_plan,
        pack_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )
    if timing is not None:
        timing(
            "candidate_pack_build_ms",
            int((time.perf_counter() - pack_started) * 1000),
        )
    return pack


def build_jd_positioning_brief(
    job_description: str,
    facts: list[ResumeAtomicFact],
) -> JDPositioningBrief:
    """Rank verified facts against exact JD language without model inference."""

    signals = deterministic_hiring_signals(job_description)
    jd_terms = _entry_terms(job_description)
    ranked = sorted(
        facts,
        key=lambda fact: (
            -len(_entry_terms(fact.text) & jd_terms),
            fact.fact_id,
        ),
    )
    technical_themes = [
        theme for theme in _THEMES if theme.casefold() in job_description.casefold()
    ]
    projects = [fact.project for fact in ranked if fact.project]
    lines = [line.strip() for line in job_description.splitlines() if line.strip()]
    role_title = next(
        (
            line.strip(" -•\t")
            for line in lines
            if line.casefold().strip(" :") not in _HEADING_LINES
            and len(line.strip(" -•\t")) <= 120
            and not line.lstrip().startswith(("●", "-", "•"))
        ),
        "Target role",
    )
    return JDPositioningBrief(
        job_description_sha256=hashlib.sha256(job_description.encode()).hexdigest(),
        role_title=role_title,
        top_hiring_signals=signals,
        technical_themes=technical_themes[:12],
        priority_fact_ids=[fact.fact_id for fact in ranked[:48]],
        first_resume_focus=list(dict.fromkeys(projects))[:8],
    )


__all__ = [
    "build_candidate_evidence_pack",
    "build_composition_evidence_plan",
    "build_resume_evidence_retrieval_trace",
    "build_jd_positioning_brief",
    "deterministic_hiring_signals",
]
