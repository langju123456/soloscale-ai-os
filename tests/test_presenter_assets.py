import json
from pathlib import Path

from soloscale.content_models import ClaimStatus, ContentBrief, ContentClaim
from soloscale.content_workspace import run_content_workspace
from soloscale.presenter_assets import (
    PresenterAssetCategory,
    PresenterAssetKind,
    PresenterLayout,
    PresenterMode,
    import_presenter_asset,
    load_presenter_library,
    materialize_reusable_presenter_assets,
    plan_presenter_assets,
    prepare_presenter_plan,
    save_presenter_preferences,
)
from soloscale.video_factory import prepare_heygen_handoff


def _run(data_root: Path):
    return run_content_workspace(
        data_root=data_root,
        brief=ContentBrief(
            topic="Reusable presenter assets",
            audience="AI builders",
            language="English",
            call_to_action="Keep the evidence visible.",
            source_label="git:test",
            claims=[
                ContentClaim(
                    id="CLAIM-01",
                    text="A local render completed.",
                    status=ClaimStatus.VERIFIED,
                    receipt="git:test-1",
                ),
                ContentClaim(
                    id="CLAIM-02",
                    text="The UI remained responsive.",
                    status=ClaimStatus.OBSERVED,
                    receipt="run:test-2",
                ),
            ],
        ),
    )


def _import(data_root: Path, category: PresenterAssetCategory, suffix: bytes):
    return import_presenter_asset(
        data_root=data_root,
        display_name=f"{category.value.title()} pose",
        category=category,
        source_kind=PresenterAssetKind.REAL_FOOTAGE,
        layout=PresenterLayout.PICTURE_IN_PICTURE,
        duration_seconds=8,
        source_filename=f"{category.value.lower()}.mp4",
        content=b"\x00\x00\x00\x18ftypmp42" + suffix,
    )


def test_presenter_plan_reuses_assets_and_limits_dynamic_avatar(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    run = _run(data_root)
    _import(data_root, PresenterAssetCategory.INTRO, b"intro")
    _import(data_root, PresenterAssetCategory.OUTRO, b"outro")

    plan = prepare_presenter_plan(data_root=data_root, run_id=run.run_id)
    assert plan.presenter_scenes == 3
    assert plan.reusable_presenter_scenes == 2
    assert plan.dynamic_avatar_scenes == 1
    assert plan.dynamic_avatar_seconds > 0
    assert plan.reusable_asset_ratio == 2 / 3
    assert sum(item.mode is PresenterMode.REUSABLE_ASSET for item in plan.scenes) == 2

    handoff = json.loads(
        prepare_heygen_handoff(data_root=data_root, run_id=run.run_id).read_text()
    )
    assert len(handoff["segments"]) == 1
    assert handoff["presenter_plan"]["reusable_presenter_scenes"] == 2
    assert handoff["presenter_plan"]["dynamic_avatar_scenes"] == 1
    dynamic_scene = next(
        item.scene_id for item in plan.scenes if item.mode is PresenterMode.DYNAMIC_AVATAR
    )
    save_presenter_preferences(
        data_root=data_root,
        run_id=run.run_id,
        evidence_visual_scene_ids={dynamic_scene},
    )
    updated_handoff = json.loads(
        prepare_heygen_handoff(data_root=data_root, run_id=run.run_id).read_text()
    )
    assert updated_handoff["status"] == "NO_DYNAMIC_AVATAR_REQUIRED"
    assert updated_handoff["segments"] == []


def test_presenter_asset_is_private_idempotent_and_renderable(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    run = _run(data_root)
    first = _import(data_root, PresenterAssetCategory.INTRO, b"same")
    second = _import(data_root, PresenterAssetCategory.INTRO, b"same")
    assert second.asset_id == first.asset_id
    assert len(load_presenter_library(data_root).assets) == 1

    plan = plan_presenter_assets(run=run, library=load_presenter_library(data_root))
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    assets = materialize_reusable_presenter_assets(
        data_root=data_root, plan=plan, public_dir=public_dir
    )
    assert list(assets) == ["SCENE-01"]
    exported = public_dir / assets["SCENE-01"]
    assert exported.is_file()
    assert exported.read_bytes() == b"\x00\x00\x00\x18ftypmp42same"


def test_presenter_scene_can_be_converted_to_evidence_visual(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    run = _run(data_root)
    baseline = plan_presenter_assets(run=run, library=load_presenter_library(data_root))
    dynamic_scene = next(
        item.scene_id for item in baseline.scenes if item.mode is PresenterMode.DYNAMIC_AVATAR
    )
    reduced = plan_presenter_assets(
        run=run,
        library=load_presenter_library(data_root),
        evidence_visual_scene_ids={dynamic_scene},
    )
    assert reduced.dynamic_avatar_scenes == baseline.dynamic_avatar_scenes - 1
    converted = next(item for item in reduced.scenes if item.scene_id == dynamic_scene)
    assert converted.mode is PresenterMode.NONE
