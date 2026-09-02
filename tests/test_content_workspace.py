import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from soloscale.buildlog_handoff import (
    buildlog_handoff_status,
    preview_for_buildlog,
    publish_via_buildlog,
    stage_for_buildlog,
)
from soloscale.content_canon_pipeline import (
    ContentCanonError,
    content_brief_from_month_one_story,
)
from soloscale.content_distribution import (
    ContentDistributionError,
    load_distribution_package,
    prepare_distribution_package,
)
from soloscale.content_models import (
    ClaimStatus,
    ContentBrief,
    ContentClaim,
    ContentDrafts,
    ContentReviewDecision,
    StoryboardScene,
)
from soloscale.content_workspace import (
    ContentWorkspaceError,
    content_download,
    load_content_review,
    load_content_run,
    parse_claim_ledger,
    run_content_workspace,
    run_content_workspace_with_ollama,
    save_content_review,
)
from soloscale.evidence_agent import Reasoner
from soloscale.evidence_hub import EvidenceHub
from soloscale.media_cost import load_cost_receipts
from soloscale.media_quality import MediaQualityChecklist, save_media_quality_review
from soloscale.video_factory import (
    CreatorVideoJobManager,
    creator_video_ready,
    import_avatar_segment,
    prepare_heygen_handoff,
    render_creator_video,
)
from soloscale.voice_provider import NarrationResult


def _brief() -> ContentBrief:
    return ContentBrief(
        topic="Evidence-first product integration",
        audience="AI engineers and solo builders",
        language="English",
        call_to_action="Follow the next measured iteration.",
        source_label="https://github.com/example/solo-scale/pull/8",
        claims=[
            ContentClaim(
                id="CLAIM-01",
                text="Python 3.11 and 3.12 CI checks passed.",
                status=ClaimStatus.VERIFIED,
                receipt="https://github.com/example/solo-scale/actions/runs/8",
                limits="This does not prove production readiness.",
            ),
            ContentClaim(
                id="CLAIM-02",
                text="A unified local UI now exposes Resume, Learning, and Content routes.",
                status=ClaimStatus.OBSERVED,
                receipt="git:c39fb61",
            ),
            ContentClaim(
                id="CLAIM-03",
                text="The next experiment will measure human edit distance.",
                status=ClaimStatus.PLANNED,
            ),
        ],
    )


def _model_drafts(brief: ContentBrief) -> ContentDrafts:
    claim_lines = [
        f"{claim.status.value} · {claim.id} — {claim.text}"
        for claim in brief.claims
    ]
    x_posts = [
        f"{index}/{len(claim_lines) + 1} {line}"
        for index, line in enumerate(claim_lines, start=1)
    ]
    x_posts.append(
        f"{len(x_posts) + 1}/{len(claim_lines) + 1} {brief.call_to_action}"
    )
    scenes = [
        StoryboardScene(
            id=f"SCENE-{index:02d}",
            start_second=(index - 1) * 6,
            end_second=index * 6,
            purpose=f"{claim.status.value} · {claim.id}",
            visual="Evidence card",
            voiceover=claim.text,
            on_screen_text=f"{claim.status.value} · {claim.id}",
            claim_ids=[claim.id],
        )
        for index, claim in enumerate(brief.claims, start=1)
    ]
    scenes.append(
        StoryboardScene(
            id=f"SCENE-{len(scenes) + 1:02d}",
            start_second=len(scenes) * 6,
            end_second=(len(scenes) + 1) * 6,
            purpose="CTA",
            visual="CTA card",
            voiceover=brief.call_to_action,
            on_screen_text=brief.call_to_action,
            claim_ids=[],
        )
    )
    joined = "\n".join([*claim_lines, brief.call_to_action])
    return ContentDrafts(
        linkedin=joined,
        x_thread=x_posts,
        youtube_script=joined,
        video_script=joined,
        storyboard=scenes,
    )


class _FakeContentReasoner:
    def __init__(self, drafts: ContentDrafts) -> None:
        self.drafts = drafts
        self.system = ""
        self.user = ""

    def complete(
        self, schema: object, *, system: str, user: str
    ) -> ContentDrafts:
        assert schema is not ContentDrafts
        self.system = system
        self.user = user
        return self.drafts


def test_parse_claim_ledger_requires_receipts_for_grounded_statuses() -> None:
    claims = parse_claim_ledger(
        "VERIFIED | 285 tests passed. | https://example.test/ci | Local run only.\n"
        "HYPOTHESIS | Evidence-first drafts may reduce editing time."
    )
    assert [claim.id for claim in claims] == ["CLAIM-01", "CLAIM-02"]
    assert claims[0].status is ClaimStatus.VERIFIED
    assert claims[1].receipt is None

    with pytest.raises(ContentWorkspaceError, match="does not satisfy"):
        parse_claim_ledger("OBSERVED | Users preferred the new flow.")


def test_ready_month_one_story_produces_grounded_multiformat_bundle(
    tmp_path: Path,
) -> None:
    brief = content_brief_from_month_one_story("M1-15", language="中文")
    assert brief.evidence_filters["canon_story_id"] == "M1-15"
    assert len(brief.claims) == 8
    assert all(
        claim.receipt
        for claim in brief.claims
        if claim.status in {ClaimStatus.VERIFIED, ClaimStatus.OBSERVED}
    )

    run = run_content_workspace(data_root=tmp_path / ".soloscale", brief=brief)
    run_dir = tmp_path / ".soloscale" / "content-runs" / run.run_id
    assert (run_dir / "20_youtube_script.md").is_file()
    assert "CLAIM-" not in run.drafts.youtube_script
    assert "VERIFIED · CLAIM" not in run.drafts.youtube_script

    with pytest.raises(ContentCanonError, match="needs evidence or owner input"):
        content_brief_from_month_one_story("M1-23", language="中文")


def test_month_one_bilingual_variants_share_facts_but_persist_separately(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    chinese_brief = content_brief_from_month_one_story("M1-15", language="中文")
    english_brief = content_brief_from_month_one_story("M1-15", language="English")

    chinese = run_content_workspace(data_root=data_root, brief=chinese_brief)
    english = run_content_workspace(data_root=data_root, brief=english_brief)

    assert chinese.run_id != english.run_id
    assert chinese.locale_variant is not None
    assert english.locale_variant is not None
    assert chinese.locale_variant.locale == "zh-CN"
    assert english.locale_variant.locale == "en-US"
    assert chinese.locale_variant.variant_group_id == english.locale_variant.variant_group_id
    assert chinese.locale_variant.canonical_story_id == "M1-15"
    assert (
        chinese.locale_variant.fact_contract_sha256
        == english.locale_variant.fact_contract_sha256
    )
    assert chinese.brief.claims == english.brief.claims
    assert chinese.brief.topic != english.brief.topic
    assert chinese.brief.call_to_action != english.brief.call_to_action

    for run in (chinese, english):
        run_dir = data_root / "content-runs" / run.run_id
        verification = json.loads((run_dir / "08_verification.json").read_text())
        publish_pack = json.loads((run_dir / "06_publish_pack.json").read_text())
        assert verification["locale"] == run.locale_variant.locale
        assert publish_pack["locale_variant"] == run.locale_variant.model_dump(mode="json")
        assert publish_pack["publication_performed"] is False


def test_content_workspace_writes_private_reviewable_multichannel_pack(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    run = run_content_workspace(data_root=data_root, brief=_brief())
    run_dir = data_root / "content-runs" / run.run_id

    assert run.status == "DRAFT_REQUIRES_HUMAN_APPROVAL"
    assert run.network_used is False
    assert run.model_used is False
    assert run.publication_performed is False
    assert run.editorial_provenance[0].provider.kind.value == "template"
    assert run.editorial_provenance[0].exact_model == "deterministic-content-template-v1"
    assert set(run.artifact_paths) == {path.name for path in run_dir.iterdir()}
    assert run_dir.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in run_dir.iterdir())
    assert EvidenceHub(data_root).status().asset_count == len(run.artifact_paths)

    linkedin = (run_dir / "02_linkedin.md").read_text(encoding="utf-8")
    x_thread = (run_dir / "03_x_thread.md").read_text(encoding="utf-8")
    video = (run_dir / "04_video_script.md").read_text(encoding="utf-8")
    verification = json.loads((run_dir / "08_verification.json").read_text())
    assert "CLAIM-" not in linkedin
    assert (run_dir / "15_canonical_story.md").is_file()
    assert (run_dir / "16_blog.md").is_file()
    assert run.drafts.x_post.strip() == (run_dir / "03_x_post.md").read_text().strip()
    assert 60 <= run.drafts.storyboard[-1].end_second <= 120
    assert "This does not prove production readiness" in linkedin
    assert "1/5" in x_thread
    assert "Claim anchors" not in video
    assert run.locale_variant is not None
    assert verification == {
        "claim_count": 3,
        "credential_shape_scan_passed": True,
        "every_claim_has_anchor": True,
        "editorial_provenance_recorded": True,
        "evidence_bundle_used": False,
        "evidence_gap_count": 0,
        "evidence_item_count": 0,
        "execution_state": "AI_NOT_EXECUTED",
        "fact_contract_sha256": run.locale_variant.fact_contract_sha256,
        "fallback_used": False,
        "latency_ms": None,
        "locale": "en-US",
        "model_calls": 0,
        "model_used": False,
        "network_used": False,
        "private_path_scan_passed": True,
        "publication_performed": False,
        "status": "PASS",
        "token_usage": None,
        "verified_and_observed_have_receipts": True,
        "variant_group_id": run.locale_variant.variant_group_id,
        "cost_usd": None,
    }

    loaded = load_content_run(data_root, run.run_id)
    assert loaded == run
    artifact_name, content = content_download(data_root, run.run_id, "linkedin.md")
    assert artifact_name == "02_linkedin.md"
    assert content.decode("utf-8") == linkedin
    artifact_name, provenance = content_download(
        data_root, run.run_id, "editorial-provenance.json"
    )
    assert artifact_name == "12_editorial_provenance.json"
    assert b'"fresh_context": true' in provenance
    run_path = data_root / "content-runs" / run.run_id / "run.json"
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload.pop("editorial_provenance")
    run_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_content_run(data_root, run.run_id)
    assert loaded.editorial_provenance == []


def test_content_workspace_uses_local_ollama_and_rejects_unanchored_output(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    brief = _brief()
    fake = _FakeContentReasoner(_model_drafts(brief))
    run = run_content_workspace_with_ollama(
        data_root=data_root,
        brief=brief,
        model="qwen3:8b",
        reasoner=cast(Reasoner, fake),
    )

    assert run.model_used is True
    assert run.network_used is True
    assert run.editorial_provenance[0].provider.kind.value == "ollama"
    assert run.editorial_provenance[0].exact_model == "qwen3:8b"
    assert run.editorial_provenance[0].network_used is True
    assert "required_claim_markers" in fake.user
    prompt = json.loads(fake.user)
    assert prompt["locale_policy"] == {
        "adaptation": "native editorial variant, not literal translation",
        "locale": "en-US",
        "shared_facts_only": True,
        "single_locale_output": True,
    }
    verification = json.loads(
        (
            data_root / "content-runs" / run.run_id / "08_verification.json"
        ).read_text(encoding="utf-8")
    )
    assert verification["model_used"] is True
    assert verification["network_used"] is True

    loosely_structured = _model_drafts(brief).model_copy(
        update={
            "linkedin": brief.claims[0].text,
            "x_thread": [f"N/1 {brief.claims[0].text}"],
            "video_script": brief.claims[0].text,
        }
    )
    repaired = run_content_workspace_with_ollama(
        data_root=data_root,
        brief=brief,
        model="qwen3:8b",
        reasoner=cast(Reasoner, _FakeContentReasoner(loosely_structured)),
    )
    for claim in brief.claims:
        marker = f"{claim.status.value} · {claim.id}"
        assert marker not in repaired.drafts.linkedin
        assert marker not in "\n".join(repaired.drafts.x_thread)
        assert marker not in repaired.drafts.video_script
    assert all(
        post.startswith(f"{index}/{len(repaired.drafts.x_thread)} ")
        for index, post in enumerate(repaired.drafts.x_thread, start=1)
    )

    invalid = _model_drafts(brief).model_copy(
        update={"linkedin": "VERIFIED · CLAIM-99\n" + brief.call_to_action}
    )
    with pytest.raises(ContentWorkspaceError, match="claim ID"):
        run_content_workspace_with_ollama(
            data_root=data_root,
            brief=brief,
            model="qwen3:8b",
            reasoner=cast(Reasoner, _FakeContentReasoner(invalid)),
        )
    assert len(list((data_root / "content-runs").iterdir())) == 2


def test_content_workspace_repeat_runs_never_overwrite(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    first = run_content_workspace(data_root=data_root, brief=_brief())
    hub = EvidenceHub(data_root)
    item = hub.search_metadata("content run input metadata")[0]
    bundle = hub.register_bundle(
        hub.build_bundle(
            [item.evidence_id],
            intent="Draft a bounded product update",
            coverage=["One private run input is hash-captured"],
            gaps=["No external user outcome has been observed"],
        )
    )
    second = run_content_workspace(
        data_root=data_root,
        brief=_brief().model_copy(update={"evidence_bundle_id": bundle.bundle_id}),
        evidence_hub=hub,
    )
    assert first.run_id != second.run_id
    assert len(list((data_root / "content-runs").iterdir())) == 2
    assert second.brief.evidence_item_ids == [item.evidence_id]
    assert second.brief.evidence_gaps == ["No external user outcome has been observed"]
    context = json.loads(
        (
            data_root / "content-runs" / second.run_id / "14_evidence_context.json"
        ).read_text(encoding="utf-8")
    )
    assert context["bundle_id"] == bundle.bundle_id
    assert context["items"][0]["public_safe_summary"] == item.public_safe_summary
    assert context["gaps"] == bundle.gaps

    unsafe_coverage_values = [
        "/Users/synthetic/private-evidence.txt",
        "Bearer syntheticcredential12345",
    ]
    for unsafe_coverage in unsafe_coverage_values:
        unsafe_bundle = hub.register_bundle(
            hub.build_bundle(
                [item.evidence_id],
                intent="Reject private coverage before creating an editorial run",
                coverage=[unsafe_coverage],
            )
        )
        with pytest.raises(ContentWorkspaceError, match="private|credential"):
            run_content_workspace(
                data_root=data_root,
                brief=_brief().model_copy(
                    update={"evidence_bundle_id": unsafe_bundle.bundle_id}
                ),
                evidence_hub=hub,
            )
    unsafe_gap = "/private/synthetic/evidence-gap.txt"
    unsafe_gap_bundle = hub.register_bundle(
        hub.build_bundle(
            [item.evidence_id],
            intent="Reject private gaps before creating an editorial run",
            gaps=[unsafe_gap],
        )
    )
    with pytest.raises(ContentWorkspaceError, match="private absolute path"):
        run_content_workspace(
            data_root=data_root,
            brief=_brief().model_copy(
                update={"evidence_bundle_id": unsafe_gap_bundle.bundle_id}
            ),
            evidence_hub=hub,
        )
    assert len(list((data_root / "content-runs").iterdir())) == 2
    assert all(
        unsafe_value.encode("utf-8") not in path.read_bytes()
        for unsafe_value in [*unsafe_coverage_values, unsafe_gap]
        for run_dir in (data_root / "content-runs").iterdir()
        for path in run_dir.iterdir()
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_label", "/Users/private/secret.json"),
        ("source_label", "file:///tmp/private.json"),
        ("call_to_action", "Use sk-secretcredential12345"),
        ("evidence_gaps", ["/Users/private/evidence-gap.txt"]),
    ],
)
def test_content_workspace_rejects_private_paths_and_credential_shapes(
    tmp_path: Path, field: str, value: str | list[str]
) -> None:
    brief = _brief().model_copy(update={field: value})
    with pytest.raises(ContentWorkspaceError, match="private|credential"):
        run_content_workspace(data_root=tmp_path / ".soloscale", brief=brief)
    assert not (tmp_path / ".soloscale").exists()


def test_content_workspace_rejects_symlinked_data_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    data_root = tmp_path / ".soloscale"
    data_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ContentWorkspaceError, match="symlink"):
        run_content_workspace(data_root=data_root, brief=_brief())
    assert list(outside.iterdir()) == []


def test_content_download_rejects_unknown_names_and_symlinks(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    run = run_content_workspace(data_root=data_root, brief=_brief())

    with pytest.raises(ContentWorkspaceError, match="not downloadable"):
        content_download(data_root, run.run_id, "run.json")

    run_dir = data_root / "content-runs" / run.run_id
    original = run_dir / "02_linkedin.md"
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    original.unlink()
    original.symlink_to(outside)
    with pytest.raises(ContentWorkspaceError, match="unsafe"):
        content_download(data_root, run.run_id, "linkedin.md")


def test_creator_video_render_uses_only_saved_storyboard_and_is_non_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / ".soloscale"
    run = run_content_workspace(data_root=data_root, brief=_brief())
    repository_root = tmp_path / "repo"
    factory_root = repository_root / "video_factory"
    renderer = factory_root / "render.mjs"
    (factory_root / "node_modules" / "@remotion" / "renderer").mkdir(
        parents=True
    )
    bundled_node = factory_root / "runtime" / "node"
    bundled_node.parent.mkdir(parents=True)
    bundled_node.write_text("#!/bin/sh\n", encoding="utf-8")
    bundled_node.chmod(0o700)
    fake_chrome = tmp_path / "Google Chrome"
    fake_chrome.write_bytes(b"chrome")
    renderer.write_text("// test renderer", encoding="utf-8")

    def fake_run(command: list[str], **_: object) -> object:
        output = Path(command[command.index("--output") + 1])
        output.write_bytes(b"mp4")
        if "--thumbnail" in command:
            Path(command[command.index("--thumbnail") + 1]).write_bytes(b"png")
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("soloscale.video_factory.subprocess.run", fake_run)
    monkeypatch.setattr(
        "soloscale.video_factory.create_narration_assets",
        lambda **_: NarrationResult(
            assets={},
            provider="test",
            model="test-voice",
            locale="en-US",
            reference_audio_sha256=None,
        ),
    )
    monkeypatch.setattr("soloscale.video_factory._MACOS_CHROME", fake_chrome)
    output = render_creator_video(
        data_root=data_root, run_id=run.run_id, repository_root=repository_root
    )

    assert output.name == "10_creator_video.mp4"
    assert creator_video_ready(data_root, run.run_id) is True
    input_payload = json.loads((output.parent / "09_creator_video_input.json").read_text())
    assert input_payload["scenes"][0]["claim_ids"] == ["CLAIM-01"]
    youtube_payload = json.loads(
        (output.parent / "21_creator_video_youtube_input.json").read_text()
    )
    assert youtube_payload["width"] == 1920
    assert input_payload["height"] == 1920
    assert (output.parent / "21_creator_video_youtube.mp4").is_file()
    assert (output.parent / "22_creator_video_thumbnail.png").is_file()
    assert (output.parent / "25_creator_video_subtitles.srt").is_file()
    assert (output.parent / "11_creator_video_render.json").is_file()
    render_receipt = json.loads(
        (output.parent / "11_creator_video_render.json").read_text()
    )
    assert render_receipt["local_api_cost_usd"] == "0"
    cost_receipts = load_cost_receipts(data_root)
    assert len(cost_receipts) == 1
    assert cost_receipts[0].service == "remotion"
    with pytest.raises(ValueError, match="already has"):
        render_creator_video(
            data_root=data_root,
            run_id=run.run_id,
            repository_root=repository_root,
        )
    artifact_name, artifact = content_download(data_root, run.run_id, "creator-video.mp4")
    assert artifact_name == output.name
    assert artifact == b"mp4"


def test_creator_video_job_runs_without_blocking_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def fake_render(**_: object) -> Path:
        entered.set()
        release.wait(timeout=2)
        return tmp_path / "video.mp4"

    monkeypatch.setattr("soloscale.video_factory.render_creator_video", fake_render)
    manager = CreatorVideoJobManager()
    try:
        snapshot = manager.start(
            data_root=tmp_path / ".soloscale",
            run_id="content-test",
            repository_root=tmp_path,
        )
        assert snapshot.phase in {"QUEUED", "RENDERING"}
        assert entered.wait(timeout=1)
        assert manager.get("content-test").phase == "RENDERING"  # type: ignore[union-attr]
        release.set()
        for _ in range(100):
            completed = manager.get("content-test")
            if completed is not None and completed.phase == "COMPLETE":
                break
            time.sleep(0.005)
        assert completed is not None
        assert completed.phase == "COMPLETE"
    finally:
        release.set()
        manager.shutdown()


def test_heygen_handoff_and_avatar_import_are_scene_scoped(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    run = run_content_workspace(data_root=data_root, brief=_brief())
    handoff = prepare_heygen_handoff(data_root=data_root, run_id=run.run_id)
    payload = json.loads(handoff.read_text(encoding="utf-8"))
    assert payload["network_used"] is False
    assert payload["external_submission_preview"]["raw_conversations_included"] is False
    assert len(payload["segments"]) == 3

    content = b"\x00\x00\x00\x18ftypmp42" + b"safe-avatar"
    imported = import_avatar_segment(
        data_root=data_root,
        run_id=run.run_id,
        scene_id="SCENE-01",
        source_filename="presenter.mp4",
        content=content,
    )
    assert imported.read_bytes() == content
    mapping = json.loads((imported.parent.parent / "24_avatar_segments.json").read_text())
    assert mapping["segments"]["SCENE-01"]["sha256"]
    with pytest.raises(ValueError, match="valid storyboard scene"):
        import_avatar_segment(
            data_root=data_root,
            run_id=run.run_id,
            scene_id="SCENE-99",
            source_filename="presenter.mp4",
            content=content,
        )


def test_distribution_package_requires_approval_and_seals_exact_media(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    run = run_content_workspace(data_root=data_root, brief=_brief())
    with pytest.raises(ContentDistributionError, match="Approve"):
        prepare_distribution_package(data_root=data_root, run_id=run.run_id)

    review = save_content_review(
        data_root=data_root,
        run_id=run.run_id,
        decision=ContentReviewDecision.APPROVED,
    )
    run_dir = data_root / "content-runs" / run.run_id
    for filename, body in {
        "21_creator_video_youtube.mp4": b"youtube-video",
        "10_creator_video.mp4": b"short-video",
        "22_creator_video_thumbnail.png": b"thumbnail",
        "25_creator_video_subtitles.srt": b"1\n00:00:00,000 --> 00:00:05,000\nSafe\n",
    }.items():
        (run_dir / filename).write_bytes(body)

    with pytest.raises(ContentDistributionError, match="media-quality"):
        prepare_distribution_package(data_root=data_root, run_id=run.run_id)
    quality = save_media_quality_review(
        data_root=data_root,
        run_id=run.run_id,
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

    path = prepare_distribution_package(data_root=data_root, run_id=run.run_id)
    package = load_distribution_package(data_root, run.run_id)
    assert package is not None
    assert package["publication_performed"] is False
    assert package["locale"] == "en-US"
    assert package["variant_group_id"].startswith("fact-contract:")
    assert package["review_revision"] == review.revision
    assert package["media_quality_review"]["revision"] == quality.revision  # type: ignore[index]
    assert package["channels"]["youtube"]["direct_upload_enabled"] is True  # type: ignore[index]
    assert package["channels"]["youtube"]["adapter"] == "youtube-data-api-v3"  # type: ignore[index]
    artifacts = package["artifacts"]
    assert artifacts["video"]["filename"] == "21_creator_video_youtube.mp4"  # type: ignore[index]
    assert artifacts["video"]["download_path"].endswith("/youtube-video.mp4")  # type: ignore[index]
    assert artifacts["short"]["download_path"].endswith("/creator-video.mp4")  # type: ignore[index]
    assert artifacts["thumbnail"]["download_path"].endswith("/video-thumbnail.png")  # type: ignore[index]
    assert artifacts["subtitles"]["download_path"].endswith("/video-subtitles.srt")  # type: ignore[index]
    assert path.stat().st_mode & 0o777 == 0o600
    youtube_name, youtube_body = content_download(
        data_root, run.run_id, "youtube-upload.json"
    )
    assert youtube_name == "27_youtube_upload.json"
    youtube_payload = json.loads(youtube_body)
    assert youtube_payload["locale"] == "en-US"
    assert youtube_payload["upload_performed"] is False
    with pytest.raises(ContentWorkspaceError, match="sealed"):
        save_content_review(
            data_root=data_root,
            run_id=run.run_id,
            decision=ContentReviewDecision.APPROVED,
        )


def test_buildlog_handoff_stages_exact_artifact_and_persists_returned_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / ".soloscale"
    run = run_content_workspace(data_root=data_root, brief=_brief())
    with pytest.raises(ContentWorkspaceError, match="Approve"):
        stage_for_buildlog(data_root=data_root, run_id=run.run_id, channel="linkedin")
    approved_linkedin = run.drafts.linkedin + "\nOwner reviewed this exact adaptation.\n"
    browser_approved_linkedin = approved_linkedin.replace("\n", "\r\n")
    review = save_content_review(
        data_root=data_root,
        run_id=run.run_id,
        updates={"linkedin": browser_approved_linkedin},
        decision=ContentReviewDecision.APPROVED,
    )
    assert review.decision is ContentReviewDecision.APPROVED
    assert load_content_review(data_root, run.run_id) is not None

    class FakeGateway:
        def stage(self, **kwargs: object) -> str:
            assert Path(str(kwargs["source_path"])).read_text() == approved_linkedin
            return "soloscale-linkedin-123"

        def preview(self, run_id: str) -> SimpleNamespace:
            assert run_id == "soloscale-linkedin-123"
            return SimpleNamespace(
                platform=SimpleNamespace(value="linkedin"),
                account_reference="linkedin:member:123",
                account_display_name="Test Member",
                content=approved_linkedin,
                content_hash="a" * 64,
                content_length=len(approved_linkedin),
                duplicate_found=False,
                indeterminate_found=False,
            )

        def publish(self, run_id: str, **kwargs: object) -> SimpleNamespace:
            assert kwargs["confirmation"] == "PUBLISH"
            return SimpleNamespace(
                receipt_id="receipt-123",
                platform=SimpleNamespace(value="linkedin"),
                status=SimpleNamespace(value="succeeded"),
                external_post_id="urn:li:share:123",
                published_at=SimpleNamespace(isoformat=lambda: "2026-08-12T00:00:00+00:00"),
                run_id=run_id,
            )

    monkeypatch.setattr("soloscale.buildlog_handoff._gateway", lambda *args: FakeGateway())

    handoff = stage_for_buildlog(data_root=data_root, run_id=run.run_id, channel="linkedin")
    preview = preview_for_buildlog(data_root=data_root, run_id=run.run_id, channel="linkedin")
    receipt = publish_via_buildlog(
        data_root=data_root,
        run_id=run.run_id,
        channel="linkedin",
        confirmation="PUBLISH",
    )

    assert handoff["buildlog_run_id"] == "soloscale-linkedin-123"
    assert receipt["external_post_id"] == "urn:li:share:123"
    assert buildlog_handoff_status(data_root, run.run_id, "linkedin") == (
        handoff,
        preview,
        receipt,
    )
    hub = EvidenceHub(data_root)
    outcome = hub.recent_outcomes()[0]
    assert outcome.asset_id is not None
    published_asset = hub.get_asset(outcome.asset_id)
    assert published_asset is not None
    assert published_asset.private_locator is not None
    assert published_asset.private_locator.endswith("/reviews/review-0001/linkedin.md")
    assert published_asset.content_sha256 == outcome.final_sha256


def test_content_review_is_versioned_and_regenerates_only_one_adaptation(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    run = run_content_workspace(data_root=data_root, brief=_brief())
    edited_blog = run.drafts.blog + "\nA human edit that keeps every evidence anchor.\n"

    first = save_content_review(
        data_root=data_root,
        run_id=run.run_id,
        updates={"blog": edited_blog},
    )
    second = save_content_review(
        data_root=data_root,
        run_id=run.run_id,
        updates={},
        regenerate_target="blog",
    )

    assert first.revision == 1
    assert second.revision == 2
    assert second.reset_target == "blog"
    latest = load_content_review(data_root, run.run_id)
    assert latest is not None
    assert latest[1]["blog"] == run.drafts.blog
    assert latest[1]["linkedin"] == run.drafts.linkedin
    review_dirs = sorted((data_root / "content-runs" / run.run_id / "reviews").iterdir())
    assert [path.name for path in review_dirs] == ["review-0001", "review-0002"]
    assert all(path.stat().st_mode & 0o777 == 0o700 for path in review_dirs)
    assert all(
        artifact.stat().st_mode & 0o777 == 0o600
        for path in review_dirs
        for artifact in path.iterdir()
    )


def test_public_projections_strip_internal_provenance_labels(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    brief = ContentBrief(
        topic="A grounded engineering story",
        audience="AI engineers",
        language="English",
        call_to_action="Share a similar trade-off.",
        source_label="git:abc123",
        claims=[
            ContentClaim(
                id="CLAIM-01",
                text="A focused test passed.",
                status=ClaimStatus.VERIFIED,
                receipt="git:abc123",
                limits="This does not prove production readiness.",
            ),
            ContentClaim(
                id="CLAIM-02",
                text="I observed this behavior.",
                status=ClaimStatus.OBSERVED,
                receipt="git:abc123",
                limits="Keep this local.",
            ),
        ],
    )
    run = run_content_workspace(data_root=data_root, brief=brief)
    public_blob = "\n".join(
        [
            run.drafts.linkedin,
            run.drafts.x_post,
            "\n".join(run.drafts.x_thread),
            run.drafts.canonical_story,
            run.drafts.blog,
            run.drafts.youtube_script,
            run.drafts.video_script,
            *[scene.on_screen_text for scene in run.drafts.storyboard],
            *[scene.voiceover for scene in run.drafts.storyboard],
            *[scene.purpose for scene in run.drafts.storyboard],
        ]
    )
    for banned in (
        "VERIFIED",
        "OBSERVED",
        "HYPOTHESIS",
        "PLANNED",
        "CLAIM-",
        "Limit:",
        "Facts source:",
        "Reference pattern applied:",
        "Explicit boundaries:",
        "Evidence map",
        "Proof links:",
    ):
        assert banned not in public_blob, banned
