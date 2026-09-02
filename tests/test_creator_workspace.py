import json
import re
import shutil
from pathlib import Path

from soloscale.content_canon import load_month_one_canon
from soloscale.content_models import ContentReviewDecision
from soloscale.content_ui import content_page, editorial_publishing_page, run_content_form
from soloscale.content_workspace import save_content_review
from soloscale.creator_accounts import normalize_account, save_creator_account
from soloscale.creator_production import (
    CreatorProductionJob,
    CreatorProductionJobManager,
    CreatorProductionRequest,
    create_run_artifacts,
    wait_for_creator_job,
)
from soloscale.creator_workspace import creator_history_page, creator_overview_page
from soloscale.platform_accounts import ConnectedIdentity, save_connected_identity


def _seal_video_files(data_root: Path, run_id: str) -> None:
    run_dir = data_root / "content-runs" / run_id
    for filename in ("21_creator_video_youtube.mp4", "10_creator_video.mp4"):
        (run_dir / filename).write_bytes(b"rendered-video")


def _draft_artifacts(
    data_root: Path, run_id: str, outputs: list[str]
) -> None:
    if "VIDEO" in outputs:
        _seal_video_files(data_root, run_id)
    create_run_artifacts(
        data_root=data_root,
        content_project_id=f"project-{run_id}",
        run_id=run_id,
        outputs=outputs,
    )


def _approved_distribution_run(data_root: Path, run_id: str) -> None:
    _seal_video_files(data_root, run_id)
    save_content_review(
        data_root=data_root,
        run_id=run_id,
        decision=ContentReviewDecision.APPROVED,
    )
    create_run_artifacts(
        data_root=data_root,
        content_project_id=f"project-{run_id}",
        run_id=run_id,
        outputs=["ARTICLE", "VIDEO"],
    )
    (data_root / "content-runs" / run_id / "26_distribution_package.json").write_text(
        json.dumps({"run_id": run_id}), encoding="utf-8"
    )


def _content_form() -> dict[str, str]:
    return {
        "topic": "A verified engineering story",
        "audience": "AI engineers",
        "language": "English",
        "source_label": "git:abc123",
        "verified_claims": "A focused test passed. | git:abc123",
        "observed_claims": "",
        "hypotheses": "",
        "planned": "",
        "call_to_action": "Share your experience.",
        "generation_mode": "template",
    }


def test_creator_overview_uses_existing_account_and_content_state(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    save_creator_account(
        data_root,
        normalize_account(platform="youtube", display_name="SoloScale", status="ACTIVE"),
    )
    result = run_content_form(_content_form(), data_root)
    assert result.run_id is not None

    page = creator_overview_page(data_root)

    assert "创作者工作区" in page
    assert "1 / 7" in page
    assert "A verified engineering story" in page
    assert 'href="/creator/accounts?lang=zh-CN"' in page
    assert 'href="/creator/stories?lang=zh-CN"' in page
    assert 'href="/creator/create?lang=zh-CN"' in page
    assert 'href="/creator/publish?lang=zh-CN"' in page
    assert 'href="/creator/history?lang=zh-CN"' in page


def test_story_bank_and_create_are_distinct_views(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    story = load_month_one_canon().stories[0]

    story_bank = content_page(data_root=data_root, workspace_view="stories")
    create = content_page(
        data_root=data_root,
        workspace_view="create",
        canon_story_id=story.story_id,
        canon_format="video",
    )

    assert "第一个月 · 工程故事库" in story_bank
    assert ".reference-video-upload,.grid{display:none!important}" in story_bank
    assert "target.searchParams.set('canon_story_id'" in story_bank
    assert 'aria-current="page">故事库</a>' in story_bank
    assert ".month-one-canon{display:none!important}" in create
    assert 'name="creator_view" value="create"' in create
    assert f'month-one-canon:{story.story_id}' in create
    assert 'aria-current="page">创作</a>' in create


def test_story_bank_surfaces_mining_result_in_its_own_view(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    story_bank = content_page(
        data_root=data_root,
        workspace_view="stories",
        notice="扫描完成，没有发现新的可用故事。",
    )
    assert "扫描完成，没有发现新的可用故事。" in story_bank
    assert 'data-loading-label="正在扫描…"' in story_bank


def test_publish_and_history_stay_inside_creator_workspace(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    result = run_content_form(_content_form(), data_root)
    assert result.run_id is not None

    publish = editorial_publishing_page(
        data_root=data_root, locale="en", creator_mode=True
    )
    history = creator_history_page(data_root, locale="en")

    assert 'aria-current="page">Publish Queue</a>' in publish
    assert "Artifact + exact account = publication task" in publish
    assert 'aria-current="page">History / Cost</a>' in history
    assert "A verified engineering story" in history
    assert "it adds no Analytics" in history


def test_publish_queue_shows_connected_account_capability_truth(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    result = run_content_form(_content_form(), data_root)
    assert result.run_id is not None
    save_content_review(
        data_root=data_root,
        run_id=result.run_id,
        decision=ContentReviewDecision.APPROVED,
    )
    create_run_artifacts(
        data_root=data_root,
        content_project_id="project-test",
        run_id=result.run_id,
        outputs=["ARTICLE"],
    )
    save_connected_identity(
        data_root,
        ConnectedIdentity(
            platform="linkedin",
            external_account_id="linkedin:member:123",
            display_name="pinball",
            handle="pinball",
            avatar_url=None,
            scopes=("r_liteprofile",),
            token_reference="",
            connected_at="2026-08-28T00:00:00+00:00",
        ),
        token_payload={
            "access_token": "test-token",
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    )

    publish = editorial_publishing_page(
        data_root=data_root,
        locale="en",
        creator_mode=True,
        selected_run_id=result.run_id,
    )

    assert "pinball" in publish
    assert "Connected · needs publishing permission" in publish
    assert "Manage accounts and permissions" in publish


def test_publish_queue_targets_one_persisted_approved_artifact(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    selected_run_id = "content-20260828T120000Z-aaaaaaaaaa"
    other_run_id = "content-20260828T110000Z-bbbbbbbbbb"
    for run_id in (selected_run_id, other_run_id):
        run_dir = data_root / "content-runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "26_distribution_package.json").write_text(
            json.dumps({"run_id": run_id}), encoding="utf-8"
        )

    selected = editorial_publishing_page(
        data_root=data_root,
        locale="zh-CN",
        creator_mode=True,
        selected_run_id=selected_run_id,
    )
    assert f'data-approved-artifact="{selected_run_id}"' in selected
    assert selected_run_id in selected
    assert other_run_id not in selected
    assert "将发布已审核版本" in selected
    assert 'name="verified_claims"' not in selected

    invalid_zh = editorial_publishing_page(
        data_root=data_root,
        locale="zh-CN",
        creator_mode=True,
        selected_run_id="content-missing",
    )
    invalid_en = editorial_publishing_page(
        data_root=data_root,
        locale="en",
        creator_mode=True,
        selected_run_id="content-missing",
    )
    assert "当前内容还没有可发布版本" in invalid_zh
    assert "This content has no publishable version yet" in invalid_en
    assert 'role="alert"' in invalid_zh


def test_publish_center_requires_explicit_package_selection(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    run_id = run_content_form(_content_form(), data_root).run_id
    assert run_id is not None
    _draft_artifacts(data_root, run_id, ["ARTICLE"])

    page = editorial_publishing_page(
        data_root=data_root, locale="zh-CN", creator_mode=True
    )

    assert "请选择一个 Content Package" in page
    assert 'data-artifact-id="' not in page
    assert f"A verified engineering story · DRAFT · {run_id}" in page
    assert "Select content package…" in page


def test_publish_center_selector_scopes_artifacts_by_package(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    article_run = run_content_form(_content_form(), data_root).run_id
    video_run = run_content_form(_content_form(), data_root).run_id
    ready_run = run_content_form(_content_form(), data_root).run_id
    assert article_run and video_run and ready_run

    _draft_artifacts(data_root, article_run, ["ARTICLE"])
    _draft_artifacts(data_root, video_run, ["VIDEO"])
    _approved_distribution_run(data_root, ready_run)

    article_artifacts = (
        f"artifact-{article_run}-linkedin",
        f"artifact-{article_run}-x",
    )
    video_artifacts = (
        f"artifact-{video_run}-youtube",
        f"artifact-{video_run}-douyin",
    )
    ready_artifacts = (
        f"artifact-{ready_run}-linkedin",
        f"artifact-{ready_run}-x",
        f"artifact-{ready_run}-youtube",
        f"artifact-{ready_run}-douyin",
    )

    no_selection = editorial_publishing_page(
        data_root=data_root, locale="zh-CN", creator_mode=True
    )
    for run_id in (article_run, video_run, ready_run):
        assert run_id in no_selection
    assert f"· READY · {ready_run}" in no_selection
    assert f"· DRAFT · {article_run}" in no_selection
    assert f"· DRAFT · {video_run}" in no_selection

    article_page = editorial_publishing_page(
        data_root=data_root,
        locale="zh-CN",
        creator_mode=True,
        selected_run_id=article_run,
    )
    assert "DRAFT · 需要审核后才能发布。" in article_page
    for artifact in article_artifacts:
        assert f'data-artifact-id="{artifact}"' in article_page
    for other in (*video_artifacts, *ready_artifacts):
        assert f'data-artifact-id="{other}"' not in article_page
    assert "当前内容还没有可发布版本" not in article_page

    video_page = editorial_publishing_page(
        data_root=data_root,
        locale="zh-CN",
        creator_mode=True,
        selected_run_id=video_run,
    )
    assert "DRAFT · 需要审核后才能发布。" in video_page
    for artifact in video_artifacts:
        assert f'data-artifact-id="{artifact}"' in video_page
    for other in (*article_artifacts, *ready_artifacts):
        assert f'data-artifact-id="{other}"' not in video_page

    ready_page = editorial_publishing_page(
        data_root=data_root,
        locale="zh-CN",
        creator_mode=True,
        selected_run_id=ready_run,
    )
    assert "将发布已审核版本" in ready_page
    for artifact in ready_artifacts:
        assert f'data-artifact-id="{artifact}"' in ready_page
    for other in (*article_artifacts, *video_artifacts):
        assert f'data-artifact-id="{other}"' not in ready_page
    assert "youtube-publish" in ready_page


def test_publish_center_selector_falls_back_to_run_id_label(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    run_id = run_content_form(_content_form(), data_root).run_id
    assert run_id is not None
    _draft_artifacts(data_root, run_id, ["ARTICLE"])
    shutil.rmtree(data_root / "content-runs" / run_id)

    page = editorial_publishing_page(
        data_root=data_root, locale="zh-CN", creator_mode=True
    )

    assert f"{run_id} · DRAFT · {run_id}" in page


def _persist_history_job(
    data_root: Path,
    job: CreatorProductionJob,
) -> None:
    root = data_root / "creator-projects" / job.job_id
    root.mkdir(parents=True)
    (root / "project.json").write_text(
        json.dumps(job.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )


def test_history_job_card_shows_template_execution_truth(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    job = CreatorProductionJob(
        job_id="creator-job-template-history",
        content_project_id="project-template-history",
        request=CreatorProductionRequest(
            source_kind="CREATE",
            outputs=["ARTICLE"],
            language="中文",
            ai_editorial=False,
        ),
        phase="READY",
        created_at="2026-08-29T12:00:00+00:00",
        updated_at="2026-08-29T12:00:07+00:00",
        stage="Artifacts sealed",
        provider="template",
        model=None,
        model_calls=0,
    )
    _persist_history_job(data_root, job)

    first = creator_history_page(data_root, locale="zh-CN")
    second = creator_history_page(data_root, locale="zh-CN")

    for page in (first, second):
        assert "离线模板" in page
        assert "模型调用: 0" in page
        assert "已用时: 7s" in page


def test_history_job_card_shows_human_readable_ai_provider(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    job = CreatorProductionJob(
        job_id="creator-job-ai-history",
        content_project_id="project-ai-history",
        request=CreatorProductionRequest(
            source_kind="CREATE",
            outputs=["ARTICLE"],
            language="English",
            ai_editorial=True,
        ),
        phase="READY",
        created_at="2026-08-29T12:00:00+00:00",
        updated_at="2026-08-29T12:00:07+00:00",
        stage="Artifacts sealed",
        provider="openai_compatible",
        model="gpt-5.6-sol",
        model_calls=2,
    )
    _persist_history_job(data_root, job)

    page = creator_history_page(data_root, locale="en")

    assert "OpenAI API" in page
    assert "gpt-5.6-sol" in page
    assert "Model calls: 2" in page
    assert "Elapsed: 7s" in page


def test_history_video_ready_uses_canonical_render_outputs(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    video_run = run_content_form(_content_form(), data_root).run_id
    empty_run = run_content_form(_content_form(), data_root).run_id
    legacy_run = run_content_form(_content_form(), data_root).run_id
    assert video_run and empty_run and legacy_run

    (data_root / "content-runs" / video_run / "21_creator_video_youtube.mp4").write_bytes(
        b"rendered"
    )
    (data_root / "content-runs" / video_run / "10_creator_video.mp4").write_bytes(
        b"rendered"
    )
    (data_root / "content-runs" / legacy_run / "youtube-video.mp4").write_bytes(
        b"rendered"
    )
    (data_root / "content-runs" / legacy_run / "creator-video.mp4").write_bytes(
        b"rendered"
    )

    page = creator_history_page(data_root, locale="zh-CN")

    def flags_for(run_id: str) -> list[str]:
        for match in re.finditer(
            r'<article class="history-card">.*?</article>', page, re.S
        ):
            card = match.group(0)
            if run_id in card:
                return re.findall(r"<strong>([^<]*)</strong>", card)
        return []

    assert flags_for(video_run) == ["视频已就绪", "发布包未就绪"]
    assert flags_for(empty_run) == ["暂无视频", "发布包未就绪"]
    assert flags_for(legacy_run) == ["视频已就绪", "发布包未就绪"]


def test_history_card_shows_template_provider_and_zero_model_calls(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    result = run_content_form(_content_form(), data_root)
    assert result.run_id is not None

    manager = CreatorProductionJobManager()
    job = manager.submit(
        data_root=data_root,
        request=CreatorProductionRequest(
            source_kind="STORY",
            source_story_id="M1-13",
            outputs=["ARTICLE"],
            language="English",
            ai_editorial=False,
        ),
        runner=lambda: result.run_id,
        provider="template",
        model=None,
    )
    wait_for_creator_job(manager, data_root, job.job_id)
    manager.shutdown()

    history = creator_history_page(data_root, locale="en")
    assert "Offline template" in history
    assert "Model calls: 0" in history
