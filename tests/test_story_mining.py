from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from soloscale.content_canon_pipeline import (
    ContentCanonError,
    content_brief_from_story_candidate,
)
from soloscale.content_workspace import run_content_workspace
from soloscale.evidence_hub_models import EvidenceItem, TruthClass
from soloscale.story_mining import (
    MinedStoryDraft,
    StoryMiningError,
    load_story_candidate,
    load_story_candidates,
    mine_story_candidates,
)


def _evidence(evidence_id: str, summary: str) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        source_id=f"source-{evidence_id}",
        native_id=evidence_id,
        evidence_type="commit",
        project="demo-project",
        captured_at=datetime(2026, 8, 28, tzinfo=UTC),
        truth_class=TruthClass.PERSONAL_ARTIFACT,
        trust_state="verified",
        public_safe_summary=summary,
        verification={"owner": "self"},
        verification_status="verified",
        content_sha256="a" * 64,
    )


def test_mining_produces_bounded_deduplicated_persisted_batch(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    evidence = [
        _evidence(f"ev-{index:02d}", f"Approved engineering evidence {index}.")
        for index in range(1, 9)
    ]

    mined = mine_story_candidates(data_root, evidence=evidence, limit=3)

    assert len(mined) == 3
    assert len({item.candidate_id for item in mined}) == 3
    assert len({item.dedup_key for item in mined}) == 3
    assert [item.source_evidence_ids[0] for item in mined] == [
        "ev-01",
        "ev-02",
        "ev-03",
    ]
    assert all(item.model_calls == 0 for item in mined)
    assert all(item.status == "CANDIDATE" for item in mined)
    assert {item.candidate_id for item in load_story_candidates(data_root)} == {
        item.candidate_id for item in mined
    }
    for item in mined:
        assert load_story_candidate(data_root, item.candidate_id) == item


def test_watermark_prevents_remining_the_same_evidence(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    evidence = [_evidence("ev-01", "Fresh approved evidence.")]

    first = mine_story_candidates(data_root, evidence=evidence, limit=3)
    second = mine_story_candidates(data_root, evidence=evidence, limit=3)

    assert len(first) == 1
    assert second == ()


def test_custom_miner_controls_evidence_mapping_and_skips_unknown_ids(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    evidence = [
        _evidence("ev-01", "First approved evidence."),
        _evidence("ev-02", "Second approved evidence."),
    ]

    def miner(items: Sequence[EvidenceItem]) -> list[MinedStoryDraft]:
        del items
        return [
            MinedStoryDraft(
                working_title_cn="自定义候选",
                working_title_en="Custom candidate",
                one_sentence_thesis="A custom mined thesis.",
                source_evidence_id="ev-02",
            ),
            MinedStoryDraft(
                working_title_cn="未知证据",
                working_title_en="Unknown evidence",
                one_sentence_thesis="Must be skipped.",
                source_evidence_id="missing-id",
            ),
        ]

    mined = mine_story_candidates(
        data_root,
        evidence=evidence,
        miner=miner,
        limit=3,
    )

    assert len(mined) == 1
    assert mined[0].source_evidence_ids == ["ev-02"]
    assert mined[0].working_title_en == "Custom candidate"


def test_mining_rejects_unsafe_input(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    with pytest.raises(StoryMiningError, match="1 to 5"):
        mine_story_candidates(data_root, evidence=[_evidence("ev-01", "Safe.")], limit=6)
    with pytest.raises(StoryMiningError, match="must be unique"):
        mine_story_candidates(
            data_root,
            evidence=[
                _evidence("ev-01", "One."),
                _evidence("ev-01", "Two."),
            ],
        )
    with pytest.raises(StoryMiningError, match="public-safe"):
        mine_story_candidates(
            data_root,
            evidence=[
                _evidence("ev-01", "Safe.").model_copy(
                    update={"public_safe_summary": "   "}
                ),
            ],
        )


def test_story_candidate_becomes_a_grounded_template_run(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    mined = mine_story_candidates(
        data_root,
        evidence=[
            _evidence("ev-01", "A verified resume pipeline improvement."),
        ],
        limit=3,
    )
    assert len(mined) == 1

    brief = content_brief_from_story_candidate(
        data_root,
        mined[0].candidate_id,
        language="English",
    )
    assert brief.claims[0].status.value == "OBSERVED"
    assert brief.claims[0].receipt is not None
    assert "story-candidate:" in brief.claims[0].receipt
    assert brief.evidence_filters["story_candidate_id"] == mined[0].candidate_id

    run = run_content_workspace(data_root=data_root, brief=brief)
    assert run.model_used is False
    assert "CLAIM-" not in run.drafts.linkedin

    with pytest.raises(ContentCanonError, match="unavailable"):
        content_brief_from_story_candidate(
            data_root,
            "story-candidate-000000000000",
            language="English",
        )
