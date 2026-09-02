"""Metadata-only recent-work scan for owner content dogfood.

The scanner deliberately reads product receipts and catalog status, not transcript
bodies or retrieved excerpts. Selecting a candidate only pre-fills a grounded content
brief; it does not invoke a model or publish anything.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal

from soloscale.knowledge_store import KnowledgeStore, KnowledgeStoreError


class ScanRange(StrEnum):
    TODAY = "today"
    LAST_7_DAYS = "7d"


class WorkCategory(StrEnum):
    BUILT = "Built"
    FIXED = "Fixed"
    LEARNED = "Learned"
    DECIDED = "Decided"
    SHIPPED = "Shipped"
    FAILED_CORRECTED = "Failed / corrected"


@dataclass(frozen=True)
class CandidateClaim:
    text: str
    receipt: str
    limit: str


@dataclass(frozen=True)
class RecentWorkCandidate:
    candidate_id: str
    category: WorkCategory
    occurred_at: datetime
    what_happened: str
    why_share: str
    supporting_evidence: tuple[str, ...]
    confidence: Literal["high", "medium", "low"]
    audience_value: str
    topic: str
    source_label: str
    claims: tuple[CandidateClaim, ...]
    planned: tuple[str, ...] = ()

    def content_form(self, *, language: str = "English") -> dict[str, str]:
        return {
            "topic": self.topic,
            "audience": "AI engineers, solo builders, and knowledge workers",
            "language": language,
            "source_label": self.source_label,
            "verified_claims": "\n".join(
                " | ".join((claim.text, claim.receipt, claim.limit))
                for claim in self.claims
            ),
            "observed_claims": "",
            "hypotheses": "",
            "planned": "\n".join(self.planned),
            "call_to_action": "What part of your real work would you turn into reusable evidence?",
            "generation_mode": "template",
        }


@dataclass(frozen=True)
class RecentWorkScan:
    scan_range: ScanRange
    candidates: tuple[RecentWorkCandidate, ...]
    items_scanned: int
    sources_used: tuple[str, ...]


def _stable_candidate_id(*parts: str) -> str:
    raw = "\0".join(parts).encode("utf-8")
    return f"candidate-{hashlib.sha256(raw).hexdigest()[:16]}"


def _cutoff(scan_range: ScanRange, now: datetime) -> datetime:
    duration = timedelta(hours=24) if scan_range is ScanRange.TODAY else timedelta(days=7)
    return now - duration


def _regular_json(path: Path) -> dict[str, object] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _safe_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def _resume_candidates(
    data_root: Path, *, cutoff: datetime
) -> tuple[list[RecentWorkCandidate], int]:
    runs_root = data_root / "resume-runs"
    if runs_root.is_symlink() or not runs_root.is_dir():
        return [], 0
    candidates: list[RecentWorkCandidate] = []
    scanned = 0
    seen_outputs: set[str] = set()
    try:
        paths = sorted(
            runs_root.glob("resume-*/09_user_ui.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return [], 0
    for path in paths:
        payload = _regular_json(path)
        if payload is None:
            continue
        scanned += 1
        occurred_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if occurred_at < cutoff:
            continue
        output_sha = str(payload.get("output_sha256", ""))
        if not output_sha or output_sha in seen_outputs:
            continue
        seen_outputs.add(output_sha)
        run_id = path.parent.name
        approved_claims = payload.get("operator_approved_profile_claims", [])
        approved_count = len(approved_claims) if isinstance(approved_claims, list) else 0
        source_count = _safe_int(payload.get("source_paragraph_count", 0))
        gaps = _safe_int(payload.get("unsupported_requirement_count", 0))
        projects = _safe_int(payload.get("project_blocks_reordered", 0))
        skills = _safe_int(payload.get("skill_bullets_reordered", 0))
        offline = (
            payload.get("generation_mode") == "template"
            and payload.get("network_used") is False
            and payload.get("model_call_performed") is False
        )
        receipt = f"SoloScale resume receipt {run_id}"
        claims = (
            CandidateClaim(
                text=(
                    f"A real SoloScale resume run preserved {approved_count} operator-approved "
                    f"profile claims while reordering {projects} project blocks and "
                    f"{skills} skill bullets."
                ),
                receipt=receipt,
                limit="This proves a private owner run, not an external hiring outcome.",
            ),
            CandidateClaim(
                text=(
                    f"The run processed {source_count} source paragraphs and kept "
                    f"{gaps} unsupported "
                    "job requirements visible as evidence gaps."
                ),
                receipt=receipt,
                limit="The gap count is specific to this private resume and target job.",
            ),
            CandidateClaim(
                text=(
                    "The tailored DOCX and preview were produced without a model or network call."
                    if offline
                    else (
                        "The tailored DOCX and preview were produced and retained "
                        "as private artifacts."
                    )
                ),
                receipt=receipt,
                limit="Artifact creation does not prove application or interview success.",
            ),
        )
        candidates.append(
            RecentWorkCandidate(
                candidate_id=_stable_candidate_id("resume", output_sha),
                category=WorkCategory.BUILT,
                occurred_at=occurred_at,
                what_happened=(
                    "SoloScale completed a grounded, targeted resume run from the "
                    "owner's existing work."
                ),
                why_share=(
                    "It is a concrete local-first dogfood result: real evidence became "
                    "a usable career artifact without invented claims."
                ),
                supporting_evidence=(
                    f"{approved_count} approved profile claims preserved",
                    f"{source_count} source paragraphs processed",
                    f"{gaps} unsupported requirements retained as gaps",
                    "DOCX and preview artifacts created",
                ),
                confidence="high",
                audience_value=(
                    "Shows builders how local evidence can constrain AI-assisted career "
                    "workflows instead of merely generating prose."
                ),
                topic="I used my own AI Work OS to turn real local evidence into a targeted resume",
                source_label=receipt,
                claims=claims,
                planned=(
                    (
                        "Use the same evidence loop on the next real application and record "
                        "what still requires manual editing."
                    ),
                ),
            )
        )
    return candidates, scanned


def _learning_candidates(
    data_root: Path, *, cutoff: datetime
) -> tuple[list[RecentWorkCandidate], int]:
    runs_root = data_root / "learning-runs"
    if runs_root.is_symlink() or not runs_root.is_dir():
        return [], 0
    candidates: list[RecentWorkCandidate] = []
    scanned = 0
    for path in sorted(runs_root.glob("learning-*/run.json"), reverse=True):
        payload = _regular_json(path)
        if payload is None:
            continue
        scanned += 1
        occurred_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if occurred_at < cutoff:
            continue
        run_id = str(payload.get("run_id", path.parent.name))
        engineering_state = str(payload.get("engineering_state", "UNKNOWN"))
        mastery_level = str(payload.get("mastery_level", "UNKNOWN"))
        model_calls = _safe_int(payload.get("model_calls", 0))
        network_used = bool(payload.get("network_used", False))
        receipt = f"SoloScale learning receipt {run_id}"
        candidates.append(
            RecentWorkCandidate(
                candidate_id=_stable_candidate_id("learning", run_id),
                category=WorkCategory.LEARNED,
                occurred_at=occurred_at,
                what_happened=(
                    f"A Learning Tower run kept engineering state ({engineering_state}) "
                    f"separate from personal mastery ({mastery_level})."
                ),
                why_share=(
                    "It makes a common AI-building mistake visible: shipped code and "
                    "personal understanding are different states."
                ),
                supporting_evidence=(
                    f"Engineering state: {engineering_state}",
                    f"Mastery level: {mastery_level}",
                    f"Model calls: {model_calls}; network used: {str(network_used).lower()}",
                ),
                confidence="high",
                audience_value=(
                    "Helps AI builders distinguish verification receipts from "
                    "interview-ready understanding."
                ),
                topic=(
                    "Passing engineering checks did not mean I could defend every "
                    "implementation decision"
                ),
                source_label=receipt,
                claims=(
                    CandidateClaim(
                        text=(
                            "One SoloScale Learning run recorded engineering state "
                            f"{engineering_state} while personal mastery remained "
                            f"{mastery_level}."
                        ),
                        receipt=receipt,
                        limit="This is one private owner run, not a learning-effectiveness study.",
                    ),
                    CandidateClaim(
                        text=(
                            f"The run used {model_calls} model calls and "
                            f"network_used={str(network_used).lower()}."
                        ),
                        receipt=receipt,
                        limit="The receipt records process state, not durable skill improvement.",
                    ),
                ),
                planned=("Revisit the exact project claims before the next interview.",),
            )
        )
    return candidates, scanned


def _knowledge_candidate(
    data_root: Path, *, cutoff: datetime
) -> tuple[list[RecentWorkCandidate], int]:
    database = data_root / "knowledge" / "index.sqlite3"
    if database.is_symlink() or not database.is_file():
        return [], 0
    try:
        status = KnowledgeStore(data_root).status()
    except (KnowledgeStoreError, OSError, ValueError):
        return [], 1
    if status.last_synced_at is None:
        return [], 1
    occurred_at = status.last_synced_at
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    if occurred_at < cutoff:
        return [], 1
    receipt = "SoloScale knowledge catalog status"
    source_summary = ", ".join(
        f"{count} {source.replace('_', ' ')}"
        for source, count in sorted(status.source_counts.items())
        if count
    )
    return [
        RecentWorkCandidate(
            candidate_id=_stable_candidate_id(
                "knowledge", occurred_at.isoformat(), str(status.documents)
            ),
            category=WorkCategory.BUILT,
            occurred_at=occurred_at,
            what_happened=(
                "SoloScale refreshed a local catalog containing "
                f"{status.documents} work records."
            ),
            why_share=(
                "It demonstrates explicit, local refresh instead of a background watcher "
                "or raw-history upload."
            ),
            supporting_evidence=(
                f"{status.documents} documents and {status.chunks} searchable chunks",
                source_summary or "Source counts retained in catalog status",
            ),
            confidence="high",
            audience_value=(
                "Gives solo builders a practical pattern for reusing work context without "
                "rebuilding ingestion infrastructure."
            ),
            topic="Why I chose an explicit local evidence refresh instead of a real-time watcher",
            source_label=receipt,
            claims=(
                CandidateClaim(
                    text=(
                        f"The local SoloScale catalog contained {status.documents} documents "
                        f"and {status.chunks} searchable chunks after its latest explicit sync."
                    ),
                    receipt=receipt,
                    limit="Catalog size does not prove retrieval quality or business value.",
                ),
                CandidateClaim(
                    text=(
                        "SoloScale updates this catalog through an explicit sync rather than "
                        "a continuous background watcher."
                    ),
                    receipt=receipt,
                    limit=(
                        "This describes the current owner workflow, not a production "
                        "ingestion SLA."
                    ),
                ),
            ),
        )
    ], 1


def _git_candidate(
    repository_root: Path | None, *, cutoff: datetime
) -> tuple[list[RecentWorkCandidate], int]:
    if repository_root is None or repository_root.is_symlink() or not repository_root.is_dir():
        return [], 0
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "log",
                "-1",
                "--format=%H%x00%cI%x00%s",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return [], 1
    parts = result.stdout.strip().split("\0", maxsplit=2)
    if len(parts) != 3:
        return [], 1
    commit, occurred_raw, subject = parts
    try:
        occurred_at = datetime.fromisoformat(occurred_raw)
    except ValueError:
        return [], 1
    if occurred_at < cutoff:
        return [], 1
    subject = " ".join(subject.split())[:180]
    receipt = f"Git commit {commit[:12]}"
    return [
        RecentWorkCandidate(
            candidate_id=_stable_candidate_id("git", commit),
            category=WorkCategory.SHIPPED,
            occurred_at=occurred_at,
            what_happened=f"A local Git project recorded a new commit: {subject}",
            why_share=(
                "A committed change can anchor a concrete build story instead of a "
                "general opinion."
            ),
            supporting_evidence=(receipt, "Commit subject only; diff bodies were not scanned"),
            confidence="medium",
            audience_value=(
                "Can become a short build note after the owner confirms the public-safe "
                "details."
            ),
            topic=subject,
            source_label=receipt,
            claims=(
                CandidateClaim(
                    text=(
                        f"A local project recorded commit {commit[:12]} with the subject: "
                        f"{subject}"
                    ),
                    receipt=receipt,
                    limit="The scan did not inspect the diff or prove an external outcome.",
                ),
            ),
        )
    ], 1


def scan_recent_work(
    data_root: Path,
    scan_range: ScanRange | str,
    *,
    repository_root: Path | None = None,
    now: datetime | None = None,
) -> RecentWorkScan:
    """Return at most five deduplicated, content-worthy metadata candidates."""

    selected_range = ScanRange(scan_range)
    selected_now = now or datetime.now(UTC)
    if selected_now.tzinfo is None:
        selected_now = selected_now.replace(tzinfo=UTC)
    cutoff = _cutoff(selected_range, selected_now)
    collectors = (
        ("Resume runs", _resume_candidates(Path(data_root), cutoff=cutoff)),
        ("Learning runs", _learning_candidates(Path(data_root), cutoff=cutoff)),
        ("Knowledge catalog", _knowledge_candidate(Path(data_root), cutoff=cutoff)),
        ("Git metadata", _git_candidate(repository_root, cutoff=cutoff)),
    )
    candidates: list[RecentWorkCandidate] = []
    items_scanned = 0
    sources_used: list[str] = []
    seen_topics: set[str] = set()
    for source, (source_candidates, scanned) in collectors:
        items_scanned += scanned
        if source_candidates:
            sources_used.append(source)
        for candidate in source_candidates:
            normalized_topic = candidate.topic.casefold()
            if normalized_topic in seen_topics:
                continue
            seen_topics.add(normalized_topic)
            candidates.append(candidate)
    confidence_rank = {"high": 0, "medium": 1, "low": 2}
    candidates.sort(
        key=lambda item: (confidence_rank[item.confidence], -item.occurred_at.timestamp())
    )
    return RecentWorkScan(
        scan_range=selected_range,
        candidates=tuple(candidates[:5]),
        items_scanned=items_scanned,
        sources_used=tuple(sources_used),
    )
