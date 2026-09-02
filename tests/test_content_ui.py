import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from soloscale.content_models import ContentReviewDecision
from soloscale.content_scan import scan_recent_work
from soloscale.content_ui import ContentFormStatus, content_page, run_content_form
from soloscale.content_workspace import load_content_run, save_content_review
from soloscale.creator_production import CreatorProductionJob, CreatorProductionRequest
from soloscale.media_quality import MediaQualityChecklist, save_media_quality_review


def _form() -> dict[str, str]:
    return {
        "topic": "Evidence-first product integration",
        "audience": "AI engineers and solo builders",
        "language": "English",
        "source_label": "https://github.com/example/solo-scale/pull/8",
        "verified_claims": (
            "Python 3.11 and 3.12 CI checks passed. | "
            "https://github.com/example/solo-scale/actions/runs/8 | "
            "This does not prove production readiness."
        ),
        "observed_claims": (
            "The local UI exposes three product routes. | git:c39fb61"
        ),
        "hypotheses": "Evidence-first drafts may reduce human edit distance.",
        "planned": "Measure edits across the first three published assets.",
        "call_to_action": "Follow the next measured iteration.",
        "generation_mode": "template",
    }


def test_content_page_is_an_end_user_multichannel_workflow(tmp_path: Path) -> None:
    page = content_page(data_root=tmp_path / ".soloscale")
    assert 'action="/content/generate"' in page
    assert 'name="verified_claims"' in page
    assert "LinkedIn、X 和短视频草稿" in page
    assert 'href="/?lang=zh-CN"' in page
    assert 'href="/resume?lang=zh-CN"' in page
    assert 'href="/learning?lang=zh-CN"' in page
    assert 'href="/advanced?lang=zh-CN"' in page
    assert "自动发布" in page
    assert 'name="generation_mode" value="soloscale_hosted"' in page
    assert "button.disabled = true" not in page
    assert 'form.dataset.submitting = "true"' in page
    assert "event.submitter instanceof HTMLButtonElement" in page
    assert "SoloScale 托管 AI · 推荐" in page
    assert "更换 AI 服务" in page
    assert 'name="ollama_model"' not in page
    assert "127.0.0.1:11434" not in page
    assert "DRAFT_REQUIRES_HUMAN_APPROVAL" not in page
    assert 'href="/work?lang=zh-CN"' in page
    assert "使用我的工作资料" in page
    assert "扫描我最近做的事" in page
    assert "扫描今天" in page

    english = content_page(data_root=tmp_path / ".soloscale", locale="en")
    assert '<html lang="en">' in english
    assert "Turn verified work into content you can publish with confidence." in english
    assert 'name="ui_locale" value="en"' in english


def test_content_form_generates_preview_copy_and_downloads(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    result = run_content_form(_form(), data_root)
    assert result.status is ContentFormStatus.GENERATED
    assert result.error is None
    assert result.run_id is not None

    run = load_content_run(data_root, result.run_id)
    page = content_page(data_root=data_root, run_id=result.run_id)
    assert "一个主故事，五种渠道适配" in page
    assert run.drafts.linkedin.strip() in page
    assert run.drafts.canonical_story.strip() in page
    assert run.drafts.blog.strip() in page
    assert run.drafts.x_post.strip() in page
    assert "复制全部" in page
    assert f"/content/downloads/{result.run_id}/canonical-story.md" in page
    assert f"/content/downloads/{result.run_id}/blog.md" in page
    assert f"/content/downloads/{result.run_id}/linkedin.md" in page
    assert f"/content/downloads/{result.run_id}/video-script.md" in page
    assert "已私有保存" in page
    assert "编辑流程溯源" in page
    assert "deterministic-content-template-v1" in page
    assert "流程：撰写 → 独立复核 → 修订 → 人工发布确认。" in page
    assert "没有连接或操作你的社交账号" in page
    assert result.run_id in page
    assert _form()["topic"] in page
    assert _form()["verified_claims"] in page
    assert f'action="/content/render/{result.run_id}"' in page
    assert "生成 YouTube + Short 成片" in page
    assert f"/content/downloads/{result.run_id}/youtube-script.md" in page
    assert f'action="/content/avatar-handoff/{result.run_id}"' in page
    assert f'action="/content/presenter-asset/{result.run_id}"' in page
    assert f'action="/content/presenter-plan/{result.run_id}"' in page
    assert "可复用人物素材 · 0" in page
    assert "新 Avatar 3" in page
    assert f'action="/content/review/{result.run_id}"' in page
    assert "/content/buildlog/" not in page

    rendering = content_page(
        data_root=data_root,
        run_id=result.run_id,
        creator_video_phase="RENDERING",
    )
    assert "正在生成旁白、字幕与双尺寸成片" in rendering
    assert 'data-video-job-active="true"' in rendering
    assert "window.setTimeout" in rendering

    save_content_review(
        data_root=data_root,
        run_id=result.run_id,
        decision=ContentReviewDecision.APPROVED,
    )
    approved = content_page(data_root=data_root, run_id=result.run_id)
    assert f'action="/content/buildlog/{result.run_id}/linkedin"' not in approved
    assert "先生成 YouTube 与 Short 成片" in approved

    english = content_page(
        data_root=data_root,
        run_id=result.run_id,
        locale="en",
        creator_video_available=False,
    )
    assert "Copy all" in english
    assert "Download script" in english
    assert "The desktop app does not bundle the experimental Remotion runtime" in english
    assert f'action="/content/render/{result.run_id}"' not in english
    assert "button.textContent = \"Copied\"" in english

    run_dir = data_root / "content-runs" / result.run_id
    for filename in (
        "10_creator_video.mp4",
        "21_creator_video_youtube.mp4",
        "22_creator_video_thumbnail.png",
        "25_creator_video_subtitles.srt",
    ):
        (run_dir / filename).write_bytes(b"artifact")
    rendered = content_page(data_root=data_root, run_id=result.run_id)
    assert f'/content/downloads/{result.run_id}/youtube-video.mp4" download' in rendered
    assert f'/content/downloads/{result.run_id}/creator-video.mp4" download' in rendered
    assert f'/content/downloads/{result.run_id}/video-subtitles.srt" download' in rendered
    assert f'/content/downloads/{result.run_id}/video-thumbnail.png" download' in rendered
    assert "下载封面" in rendered
    assert "成本预览" in rendered
    assert f'action="/content/media-quality/{result.run_id}"' in rendered
    assert "先完成并通过八项人工媒体质量检查" in rendered

    legacy_package = run_dir / "26_distribution_package.json"
    legacy_package.write_text("{}", encoding="utf-8")
    legacy_page = content_page(data_root=data_root, run_id=result.run_id)
    assert "media-quality-review.json" not in legacy_page
    assert (
        f'href="/creator/publish?run_id={result.run_id}&lang=zh-CN"'
        in legacy_page
    )
    assert f'data-approved-artifact="{result.run_id}"' in legacy_page
    assert "将发布已审核版本" in legacy_page
    assert "/content/buildlog/" not in legacy_page
    assert 'name="verified_claims" required' in legacy_page
    legacy_package.unlink()

    save_media_quality_review(
        data_root=data_root,
        run_id=result.run_id,
        checklist=MediaQualityChecklist(
            voice_natural=True,
            pacing_natural=True,
            no_static_visual_too_long=True,
            presenter_adds_value=True,
            language_natural=True,
            claims_evidence_backed=True,
            reference_influenced_without_copying=True,
            would_publish=True,
        ),
    )
    quality_approved = content_page(data_root=data_root, run_id=result.run_id)
    assert "人工媒体质量" in quality_approved
    assert "已批准 · r1" in quality_approved
    assert f'action="/content/distribution/{result.run_id}"' in quality_approved

    rerendered = content_page(data_root=data_root, run_id=result.run_id)
    assert 'name="generation_mode" value="template"' in rerendered
    assert "安全离线草稿" in rerendered


def test_content_form_keeps_errors_user_facing_and_writes_nothing(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    invalid = _form()
    invalid["verified_claims"] = "A claim without a receipt"
    result = run_content_form(invalid, data_root)
    assert result.status is ContentFormStatus.FAILED
    assert result.run_id is None
    assert result.error is not None
    assert "需要填写" in result.error
    assert not data_root.exists()

    page = content_page(data_root=data_root, form=invalid, error=result.error)
    assert invalid["topic"] in page
    assert 'role="alert"' in page

    hosted = _form()
    hosted["generation_mode"] = "soloscale_hosted"
    unavailable = run_content_form(hosted, tmp_path / "hosted")
    assert unavailable.status is ContentFormStatus.PROVIDER_NOT_CONFIGURED
    assert unavailable.error is None
    assert "没有发送或保存任何内容" in (unavailable.message or "")
    assert not (tmp_path / "hosted").exists()

    byo = _form()
    byo["generation_mode"] = "openai_compatible"
    unavailable_byo = run_content_form(byo, tmp_path / "byo")
    assert unavailable_byo.status is ContentFormStatus.PROVIDER_NOT_CONFIGURED
    assert not (tmp_path / "byo").exists()


def test_content_page_scans_metadata_and_prefills_selected_candidate(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    run_dir = data_root / "resume-runs" / "resume-20260820T100000Z-aaaaaaaaaa"
    run_dir.mkdir(parents=True)
    receipt = run_dir / "09_user_ui.json"
    receipt.write_text(
        json.dumps(
            {
                "output_sha256": "b" * 64,
                "generation_mode": "template",
                "network_used": False,
                "model_call_performed": False,
                "operator_approved_profile_claims": [
                    {"id": "PROFILE-01", "sha256": "a" * 64}
                ],
                "source_paragraph_count": 33,
                "unsupported_requirement_count": 14,
                "project_blocks_reordered": 2,
                "skill_bullets_reordered": 3,
            }
        ),
        encoding="utf-8",
    )
    timestamp = datetime.now(UTC).timestamp()
    os.utime(receipt, (timestamp, timestamp))
    scan = scan_recent_work(data_root, "today")
    candidate = scan.candidates[0]

    page = content_page(
        data_root=data_root,
        scan_range="today",
        candidate_id=candidate.candidate_id,
    )

    assert "工作扫描" in page
    assert "已选择" in page
    assert candidate.topic in page
    assert "14 unsupported job requirements" in page
    assert 'name="generation_mode" value="template"' in page
    assert str(tmp_path) not in page


def test_creator_page_shares_canonical_work_project_context(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    project = tmp_path / "selected-project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "feature.txt").write_text("verified feature", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "feature.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "feat: add verified local project evidence",
        ],
        check=True,
    )

    connected = content_page(
        data_root=data_root,
        repository_root=project,
        workspace_view="create",
        locale="zh-CN",
    )
    assert "1 个本地项目" in connected

    disconnected = content_page(
        data_root=data_root,
        repository_root=None,
        workspace_view="create",
        locale="zh-CN",
    )
    assert "1 个本地项目" not in disconnected


def test_creator_page_uses_singular_english_project_copy(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    project = tmp_path / "selected-project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "feature.txt").write_text("verified feature", encoding="utf-8")
    subprocess.run(["git", "-C", str(project), "add", "feature.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "feat: add verified local project evidence",
        ],
        check=True,
    )

    page = content_page(
        data_root=data_root,
        repository_root=project,
        workspace_view="create",
        locale="en",
    )
    assert "1 local project" in page
    assert "1 local projects" not in page


def test_template_job_displays_zero_model_calls_and_stable_elapsed(
    tmp_path: Path,
) -> None:
    created = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
    job = CreatorProductionJob(
        job_id="creator-job-display",
        content_project_id="project-display",
        request=CreatorProductionRequest(
            source_kind="STORY",
            source_story_id="M1-13",
            outputs=["ARTICLE"],
            language="中文",
            ai_editorial=False,
        ),
        phase="READY",
        created_at=created.isoformat(),
        updated_at=(created + timedelta(seconds=7)).isoformat(),
        stage="Artifacts sealed",
        provider="template",
        model=None,
        model_calls=0,
    )
    page = content_page(
        data_root=tmp_path / ".soloscale",
        creator_job=job,
        workspace_view="create",
        locale="zh-CN",
    )
    assert "模型调用: 0" in page
    assert "已用时: 7s" in page


def test_creator_job_error_cause_shows_actionable_voice_message(
    tmp_path: Path,
) -> None:
    created = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
    job = CreatorProductionJob(
        job_id="creator-job-voice",
        content_project_id="project-voice",
        request=CreatorProductionRequest(
            source_kind="STORY",
            source_story_id="M1-13",
            outputs=["VIDEO"],
            language="中文",
            ai_editorial=False,
        ),
        phase="FAILED",
        created_at=created.isoformat(),
        updated_at=created.isoformat(),
        stage="Failed",
        provider="template",
        model=None,
        model_calls=0,
        error_code="CREATORVIDEOERROR",
        error_cause="VOICE_NOT_CONFIGURED",
    )
    page = content_page(
        data_root=tmp_path / ".soloscale",
        creator_job=job,
        workspace_view="create",
        locale="zh-CN",
    )
    assert "未配置可用的语音服务" in page
    assert "CREATORVIDEOERROR" not in page
