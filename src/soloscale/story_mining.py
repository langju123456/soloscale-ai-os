"""Bounded Story Bank mining: approved evidence into deduplicated candidates."""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from soloscale.evidence_hub_models import EvidenceItem
from soloscale.resume_workspace import ResumeWorkspaceStorageError, _atomic_private_write

_CANDIDATE_ROOT = "story-bank/candidates"
_WATERMARK_PATH = "story-bank/mining-watermark.json"
_MIN_CANDIDATES = 1
_MAX_CANDIDATES = 5
_MAX_EVIDENCE_INPUT = 100


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StoryCandidate(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str = Field(pattern=r"^story-candidate-[a-f0-9]{12}$")
    working_title_cn: str = Field(min_length=1, max_length=160)
    working_title_en: str = Field(min_length=1, max_length=160)
    one_sentence_thesis: str = Field(min_length=1, max_length=600)
    source_evidence_ids: list[str] = Field(min_length=1, max_length=8)
    evidence_summaries: list[str] = Field(min_length=1, max_length=8)
    dedup_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["CANDIDATE"] = "CANDIDATE"
    generated_by: Literal[
        "deterministic-story-template-v1"
    ] = "deterministic-story-template-v1"
    model_calls: Literal[0] = 0
    created_at: str

    @model_validator(mode="after")
    def validate_evidence_shape(self) -> StoryCandidate:
        if len(self.source_evidence_ids) != len(set(self.source_evidence_ids)):
            raise ValueError("evidence ids must be unique")
        if len(self.source_evidence_ids) != len(self.evidence_summaries):
            raise ValueError("each evidence id requires one public summary")
        return self


class StoryMiningWatermark(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    last_run_at: str
    evidence_ids: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class MinedStoryDraft:
    working_title_cn: str
    working_title_en: str
    one_sentence_thesis: str
    source_evidence_id: str | None = None


StoryMiner = Callable[[Sequence[EvidenceItem]], Sequence[MinedStoryDraft]]


class StoryMiningError(ValueError):
    """Raised when Story Bank mining cannot preserve its evidence boundary."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _truncate(value: str, limit: int) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def deterministic_story_miner(
    evidence: Sequence[EvidenceItem],
) -> list[MinedStoryDraft]:
    """Boundary-only deterministic miner; never claims semantic AI mining."""

    drafts: list[MinedStoryDraft] = []
    for item in evidence:
        summary = " ".join(item.public_safe_summary.split())
        if not summary:
            continue
        drafts.append(
            MinedStoryDraft(
                working_title_cn=f"真实工作证据：{_truncate(summary, 48)}",
                working_title_en=_truncate(summary, 88),
                one_sentence_thesis=_truncate(summary, 280),
                source_evidence_id=item.evidence_id,
            )
        )
    return drafts


def _private_root(data_root: Path, name: str) -> Path:
    root = data_root.absolute() / name
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise StoryMiningError("Story Bank storage is unsafe")
        root.chmod(0o700)
    except OSError as exc:
        raise StoryMiningError("Story Bank storage is unavailable") from exc
    return root


def _write_model(path: Path, value: BaseModel) -> None:
    try:
        _atomic_private_write(
            path,
            json.dumps(value.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        )
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise StoryMiningError("Story Bank state could not be saved") from exc


def _load_model(path: Path, model_type: type[_StrictModel]) -> _StrictModel:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise StoryMiningError("Story Bank state is unsafe")
        return model_type.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, StoryMiningError):
            raise
        raise StoryMiningError("Story Bank state is invalid") from exc


def load_story_candidates(data_root: Path) -> tuple[StoryCandidate, ...]:
    """Return persisted Story Bank candidates, newest first."""

    root = data_root.absolute() / _CANDIDATE_ROOT
    if root.is_symlink() or not root.is_dir():
        return ()
    return tuple(
        StoryCandidate.model_validate(_load_model(path, StoryCandidate))
        for path in sorted(root.glob("story-candidate-*.json"), reverse=True)
    )


def load_story_candidate(data_root: Path, candidate_id: str) -> StoryCandidate:
    """Resolve one exact Story Bank candidate."""

    path = data_root.absolute() / _CANDIDATE_ROOT / f"{candidate_id}.json"
    if not path.exists():
        raise StoryMiningError("This Story Bank candidate is unavailable")
    return StoryCandidate.model_validate(_load_model(path, StoryCandidate))


def _load_watermark(data_root: Path) -> StoryMiningWatermark | None:
    path = data_root.absolute() / _WATERMARK_PATH
    if not path.exists():
        return None
    return StoryMiningWatermark.model_validate(_load_model(path, StoryMiningWatermark))


def mine_story_candidates(
    data_root: Path,
    *,
    evidence: Sequence[EvidenceItem],
    existing_story_ids: Sequence[str] = (),
    miner: StoryMiner | None = None,
    limit: int = 5,
) -> tuple[StoryCandidate, ...]:
    """Mine a small deduplicated batch of Story Bank candidates.

    The semantic interface is the injected ``miner`` callable; this module only
    validates evidence, applies the watermark, deduplicates, bounds the batch,
    and persists candidates. The deterministic default never calls a model.
    """

    if not _MIN_CANDIDATES <= limit <= _MAX_CANDIDATES:
        raise StoryMiningError("Story mining batch must contain 1 to 5 candidates")
    if len(evidence) > _MAX_EVIDENCE_INPUT:
        raise StoryMiningError("Story mining evidence input exceeds the bounded limit")
    evidence_ids = [item.evidence_id for item in evidence]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise StoryMiningError("Story mining evidence ids must be unique")
    for item in evidence:
        if not item.public_safe_summary.strip():
            raise StoryMiningError("Story mining requires public-safe evidence summaries")
    if not evidence:
        return ()

    existing = load_story_candidates(data_root)
    used_evidence = {
        evidence_id
        for candidate in existing
        for evidence_id in candidate.source_evidence_ids
    }
    used_keys = {candidate.dedup_key for candidate in existing}
    used_existing_ids = set(existing_story_ids)
    watermark = _load_watermark(data_root)
    covered = set(watermark.evidence_ids) if watermark is not None else set()
    fresh = [
        item
        for item in evidence
        if item.evidence_id not in covered
        and item.evidence_id not in used_evidence
        and item.evidence_id not in used_existing_ids
    ]
    if not fresh:
        return ()

    drafts = (miner or deterministic_story_miner)(fresh)
    batch: list[StoryCandidate] = []
    batch_evidence: set[str] = set()
    for index, draft in enumerate(drafts):
        if len(batch) >= limit or index >= len(fresh):
            break
        source_id = draft.source_evidence_id or fresh[index].evidence_id
        if source_id not in {item.evidence_id for item in fresh}:
            continue
        if source_id in batch_evidence:
            continue
        dedup_key = hashlib.sha256(
            f"{draft.working_title_en}\n{draft.one_sentence_thesis}".encode()
        ).hexdigest()
        if dedup_key in used_keys or any(
            candidate.dedup_key == dedup_key for candidate in batch
        ):
            continue
        evidence_item = next(item for item in fresh if item.evidence_id == source_id)
        candidate = StoryCandidate(
            candidate_id=f"story-candidate-{uuid4().hex[:12]}",
            working_title_cn=draft.working_title_cn,
            working_title_en=draft.working_title_en,
            one_sentence_thesis=draft.one_sentence_thesis,
            source_evidence_ids=[source_id],
            evidence_summaries=[evidence_item.public_safe_summary.strip()],
            dedup_key=dedup_key,
            created_at=_now(),
        )
        _write_model(
            _private_root(data_root, _CANDIDATE_ROOT) / f"{candidate.candidate_id}.json",
            candidate,
        )
        batch.append(candidate)
        batch_evidence.add(source_id)
        used_keys.add(dedup_key)

    if batch:
        _write_model(
            data_root.absolute() / _WATERMARK_PATH,
            StoryMiningWatermark(
                last_run_at=_now(),
                evidence_ids=sorted(batch_evidence),
                candidate_ids=[candidate.candidate_id for candidate in batch],
            ),
        )
    return tuple(batch)
