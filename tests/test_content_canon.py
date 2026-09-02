import json
from pathlib import Path

from soloscale.content_canon import (
    StoryReadiness,
    load_month_one_canon,
    month_one_readiness_counts,
)
from soloscale.content_ui import content_page


def test_month_one_canon_is_complete_and_ordered() -> None:
    canon = load_month_one_canon()

    assert canon.collection_id == "soloscale-month-one"
    assert [story.story_id for story in canon.stories] == [
        f"M1-{index:02d}" for index in range(1, 25)
    ]
    assert [story.sequence for story in canon.stories] == list(range(1, 25))
    assert {week: sum(story.week == week for story in canon.stories) for week in range(1, 5)} == {
        1: 6,
        2: 6,
        3: 6,
        4: 6,
    }
    assert month_one_readiness_counts() == {
        StoryReadiness.READY_FOR_PRODUCTION: 5,
        StoryReadiness.NEEDS_EVIDENCE: 12,
        StoryReadiness.NEEDS_USER_INPUT: 2,
        StoryReadiness.DRAFT: 5,
    }


def test_every_story_has_six_layers_and_safe_publication_boundaries() -> None:
    canon = load_month_one_canon()
    layers = ("fact", "architecture", "decision", "implementation", "failure", "evolution")

    for story in canon.stories:
        assert all(getattr(story, layer).strip() for layer in layers)
        assert story.evidence_candidates
        assert story.overclaim_guardrails
        assert story.video_hook.strip()
        assert story.blog_thesis.strip()
        assert story.primary_format in {"60–90 second video", "technical blog"}

    serialized = json.dumps(canon.model_dump(mode="json"), ensure_ascii=False)
    assert "/Users/" not in serialized
    assert "Resume_LANG-JU" not in serialized
    assert "@gmail.com" not in serialized


def test_content_workspace_exposes_month_one_library_without_generation(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    canon = load_month_one_canon()

    page = content_page(data_root=data_root)

    assert "第一个月：从自动化幻想到可用结果" in page
    assert 'id="canon-status-filter"' in page
    assert 'data-canon-select="M1-01"' in page
    assert "补充证据 / 确认" in page
    assert "打开并编辑输入" in page
    assert "生成文章" in page
    assert "生成视频" in page
    assert "生成并加入队列" in page
    assert "month-one-canon:" in page
    assert "Fact" in page
    assert "Architecture" in page
    assert "Decision" in page
    assert "Implementation" in page
    assert "Failure" in page
    assert "Evolution" in page
    for story in canon.stories:
        assert story.story_id in page
        assert story.title_cn in page

    assert not data_root.exists()

    english = content_page(data_root=data_root, locale="en")
    assert "Month 1: From automation ambition to useful outcomes" in english
    assert "Add evidence / confirm" in english
    assert "Open and edit input" in english
    assert "Generate article" in english
    assert "Generate video" in english
    assert "Generate and queue" in english
