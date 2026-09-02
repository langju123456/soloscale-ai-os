"""Private reusable presenter assets and deterministic avatar planning."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from soloscale.content_models import ContentRun
from soloscale.content_workspace import content_run_directory, load_content_run
from soloscale.resume_workspace import ResumeWorkspaceStorageError, _atomic_private_write


class PresenterAssetError(ValueError):
    """Raised when a presenter asset or plan cannot be used safely."""


class PresenterMode(StrEnum):
    NONE = "NONE"
    REUSABLE_ASSET = "REUSABLE_ASSET"
    DYNAMIC_AVATAR = "DYNAMIC_AVATAR"


class PresenterLayout(StrEnum):
    FULL_FRAME = "FULL_FRAME"
    PICTURE_IN_PICTURE = "PICTURE_IN_PICTURE"
    SIDE_PANEL = "SIDE_PANEL"


class PresenterAssetKind(StrEnum):
    REAL_FOOTAGE = "REAL_FOOTAGE"
    AVATAR_OUTPUT = "AVATAR_OUTPUT"
    USER_IMPORTED = "USER_IMPORTED"


class PresenterAssetCategory(StrEnum):
    INTRO = "INTRO"
    GESTURE = "GESTURE"
    OUTRO = "OUTRO"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PresenterAsset(_StrictModel):
    asset_id: str = Field(pattern=r"^PRESENTER-[a-f0-9]{12}$")
    display_name: str = Field(min_length=1, max_length=120)
    category: PresenterAssetCategory
    source_kind: PresenterAssetKind
    layout: PresenterLayout = PresenterLayout.PICTURE_IN_PICTURE
    locale: str | None = Field(default=None, pattern=r"^(zh-CN|en-US)$")
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_filename: str = Field(min_length=1, max_length=180)
    duration_seconds: float = Field(gt=0, le=600)
    contains_spoken_audio: bool = False


class PresenterAssetLibrary(_StrictModel):
    schema_version: str = "1.0"
    assets: list[PresenterAsset] = Field(default_factory=list, max_length=200)


class PresenterScenePlan(_StrictModel):
    scene_id: str = Field(pattern=r"^SCENE-[0-9]{2}$")
    mode: PresenterMode
    layout: PresenterLayout
    category: PresenterAssetCategory | None = None
    asset_id: str | None = None
    duration_seconds: int = Field(ge=0, le=600)


class PresenterPlan(_StrictModel):
    schema_version: str = "1.0"
    run_id: str
    total_scenes: int = Field(ge=1, le=12)
    presenter_scenes: int = Field(ge=0, le=12)
    reusable_presenter_scenes: int = Field(ge=0, le=12)
    dynamic_avatar_scenes: int = Field(ge=0, le=12)
    dynamic_avatar_seconds: int = Field(ge=0, le=600)
    reusable_asset_ratio: float = Field(ge=0, le=1)
    scenes: list[PresenterScenePlan] = Field(min_length=1, max_length=12)


class PresenterPreferences(_StrictModel):
    schema_version: str = "1.0"
    run_id: str
    evidence_visual_scene_ids: list[str] = Field(default_factory=list, max_length=12)


_CATALOG_NAME = "catalog.json"
_PLAN_NAME = "28_presenter_plan.json"
_PREFERENCES_NAME = "29_presenter_preferences.json"
MAX_PRESENTER_ASSET_BYTES = 80 * 1024 * 1024


def presenter_library_root(data_root: Path) -> Path:
    return data_root / "media" / "presenter-assets"


def presenter_catalog_path(data_root: Path) -> Path:
    return presenter_library_root(data_root) / _CATALOG_NAME


def _safe_asset_path(data_root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise PresenterAssetError("Presenter asset path is unsafe")
    root = data_root.resolve(strict=False)
    resolved = (root / candidate).resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise PresenterAssetError("Presenter asset path escapes private storage")
    try:
        metadata = resolved.lstat()
    except FileNotFoundError as exc:
        raise PresenterAssetError("Presenter asset is missing") from exc
    if resolved.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise PresenterAssetError("Presenter asset is unsafe")
    return resolved


def load_presenter_library(data_root: Path) -> PresenterAssetLibrary:
    path = presenter_catalog_path(data_root)
    if not path.exists():
        return PresenterAssetLibrary()
    if path.is_symlink() or not path.is_file():
        raise PresenterAssetError("Presenter asset catalog is unsafe")
    try:
        library = PresenterAssetLibrary.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PresenterAssetError("Presenter asset catalog is invalid") from exc
    for asset in library.assets:
        source = _safe_asset_path(data_root, asset.path)
        if hashlib.sha256(source.read_bytes()).hexdigest() != asset.sha256:
            raise PresenterAssetError("Presenter asset hash no longer matches its catalog")
    return library


def import_presenter_asset(
    *,
    data_root: Path,
    display_name: str,
    category: PresenterAssetCategory,
    source_kind: PresenterAssetKind,
    layout: PresenterLayout,
    duration_seconds: float,
    source_filename: str,
    content: bytes,
    locale: str | None = None,
) -> PresenterAsset:
    """Persist one explicitly selected reusable MP4 outside the signed app bundle."""

    normalized_name = " ".join(display_name.split()).strip()
    if not normalized_name:
        raise PresenterAssetError("Presenter asset name is required")
    if (
        not content
        or len(content) > MAX_PRESENTER_ASSET_BYTES
        or b"ftyp" not in content[:64]
    ):
        raise PresenterAssetError("Presenter asset must be a valid MP4 up to 80 MB")
    digest = hashlib.sha256(content).hexdigest()
    asset_id = f"PRESENTER-{digest[:12]}"
    library = load_presenter_library(data_root)
    existing = next((item for item in library.assets if item.asset_id == asset_id), None)
    if existing is not None:
        return existing
    root = presenter_library_root(data_root)
    target = root / "files" / f"{asset_id}.mp4"
    relative = target.relative_to(data_root).as_posix()
    asset = PresenterAsset(
        asset_id=asset_id,
        display_name=normalized_name,
        category=category,
        source_kind=source_kind,
        layout=layout,
        locale=locale,
        path=relative,
        sha256=digest,
        source_filename=Path(source_filename).name[:180] or "presenter.mp4",
        duration_seconds=duration_seconds,
        contains_spoken_audio=False,
    )
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        os.chmod(target.parent, 0o700)
        target.write_bytes(content)
        os.chmod(target, 0o600)
        updated = PresenterAssetLibrary(assets=[*library.assets, asset])
        _atomic_private_write(
            presenter_catalog_path(data_root),
            json.dumps(updated.model_dump(mode="json"), ensure_ascii=False, indent=2)
            + "\n",
        )
    except (OSError, ResumeWorkspaceStorageError) as exc:
        target.unlink(missing_ok=True)
        raise PresenterAssetError("Could not save the presenter asset") from exc
    return asset


def _presenter_scene_categories(run: ContentRun) -> dict[str, PresenterAssetCategory]:
    scenes = run.drafts.storyboard
    selected = {
        0: PresenterAssetCategory.INTRO,
        len(scenes) // 2: PresenterAssetCategory.GESTURE,
        len(scenes) - 1: PresenterAssetCategory.OUTRO,
    }
    return {scenes[index].id: category for index, category in sorted(selected.items())}


def plan_presenter_assets(
    *,
    run: ContentRun,
    library: PresenterAssetLibrary,
    evidence_visual_scene_ids: set[str] | None = None,
) -> PresenterPlan:
    """Prefer reusable assets; reserve paid dynamic avatar for uncovered scenes."""

    evidence_visuals = evidence_visual_scene_ids or set()
    categories = _presenter_scene_categories(run)
    by_category: dict[PresenterAssetCategory, list[PresenterAsset]] = {
        category: [] for category in PresenterAssetCategory
    }
    for asset in library.assets:
        by_category[asset.category].append(asset)
    scene_plans: list[PresenterScenePlan] = []
    for scene in run.drafts.storyboard:
        category = categories.get(scene.id)
        duration = scene.end_second - scene.start_second
        if category is None or scene.id in evidence_visuals:
            scene_plans.append(
                PresenterScenePlan(
                    scene_id=scene.id,
                    mode=PresenterMode.NONE,
                    layout=PresenterLayout.PICTURE_IN_PICTURE,
                    duration_seconds=duration,
                )
            )
            continue
        matching = by_category[category]
        if matching:
            asset = matching[0]
            scene_plans.append(
                PresenterScenePlan(
                    scene_id=scene.id,
                    mode=PresenterMode.REUSABLE_ASSET,
                    layout=asset.layout,
                    category=category,
                    asset_id=asset.asset_id,
                    duration_seconds=duration,
                )
            )
        else:
            scene_plans.append(
                PresenterScenePlan(
                    scene_id=scene.id,
                    mode=PresenterMode.DYNAMIC_AVATAR,
                    layout=PresenterLayout.PICTURE_IN_PICTURE,
                    category=category,
                    duration_seconds=duration,
                )
            )
    presenter = [item for item in scene_plans if item.mode is not PresenterMode.NONE]
    reusable = [item for item in presenter if item.mode is PresenterMode.REUSABLE_ASSET]
    dynamic = [item for item in presenter if item.mode is PresenterMode.DYNAMIC_AVATAR]
    return PresenterPlan(
        run_id=run.run_id,
        total_scenes=len(scene_plans),
        presenter_scenes=len(presenter),
        reusable_presenter_scenes=len(reusable),
        dynamic_avatar_scenes=len(dynamic),
        dynamic_avatar_seconds=sum(item.duration_seconds for item in dynamic),
        reusable_asset_ratio=(len(reusable) / len(presenter) if presenter else 1.0),
        scenes=scene_plans,
    )


def _load_evidence_visual_preferences(data_root: Path, run_id: str) -> set[str]:
    preferences_path = content_run_directory(data_root, run_id) / _PREFERENCES_NAME
    if not preferences_path.exists():
        return set()
    if preferences_path.is_symlink() or not preferences_path.is_file():
        raise PresenterAssetError("Presenter preferences are unsafe")
    try:
        preferences = PresenterPreferences.model_validate_json(
            preferences_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise PresenterAssetError("Presenter preferences are invalid") from exc
    return set(preferences.evidence_visual_scene_ids)


def current_presenter_plan(*, data_root: Path, run_id: str) -> PresenterPlan:
    run = load_content_run(data_root, run_id)
    return plan_presenter_assets(
        run=run,
        library=load_presenter_library(data_root),
        evidence_visual_scene_ids=_load_evidence_visual_preferences(data_root, run_id),
    )


def prepare_presenter_plan(
    *, data_root: Path, run_id: str, evidence_visual_scene_ids: set[str] | None = None
) -> PresenterPlan:
    run = load_content_run(data_root, run_id)
    if evidence_visual_scene_ids is None:
        evidence_visual_scene_ids = _load_evidence_visual_preferences(data_root, run_id)
    plan = plan_presenter_assets(
        run=run,
        library=load_presenter_library(data_root),
        evidence_visual_scene_ids=evidence_visual_scene_ids,
    )
    path = content_run_directory(data_root, run_id) / _PLAN_NAME
    try:
        _atomic_private_write(
            path,
            json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        )
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise PresenterAssetError("Could not save the presenter plan") from exc
    return plan


def save_presenter_preferences(
    *, data_root: Path, run_id: str, evidence_visual_scene_ids: set[str]
) -> PresenterPlan:
    run = load_content_run(data_root, run_id)
    allowed = {scene.id for scene in run.drafts.storyboard}
    if not evidence_visual_scene_ids.issubset(allowed):
        raise PresenterAssetError("Presenter preference references an unknown scene")
    run_dir = content_run_directory(data_root, run_id)
    preferences = PresenterPreferences(
        run_id=run_id,
        evidence_visual_scene_ids=sorted(evidence_visual_scene_ids),
    )
    try:
        _atomic_private_write(
            run_dir / _PREFERENCES_NAME,
            json.dumps(preferences.model_dump(mode="json"), ensure_ascii=False, indent=2)
            + "\n",
        )
    except (OSError, ResumeWorkspaceStorageError) as exc:
        raise PresenterAssetError("Could not save presenter preferences") from exc
    return prepare_presenter_plan(
        data_root=data_root,
        run_id=run_id,
        evidence_visual_scene_ids=evidence_visual_scene_ids,
    )


def materialize_reusable_presenter_assets(
    *, data_root: Path, plan: PresenterPlan, public_dir: Path
) -> dict[str, str]:
    library = load_presenter_library(data_root)
    by_id = {asset.asset_id: asset for asset in library.assets}
    result: dict[str, str] = {}
    for scene in plan.scenes:
        if scene.mode is not PresenterMode.REUSABLE_ASSET or scene.asset_id is None:
            continue
        asset = by_id.get(scene.asset_id)
        if asset is None:
            raise PresenterAssetError("A planned presenter asset is unavailable")
        source = _safe_asset_path(data_root, asset.path)
        filename = f"presenter-{scene.scene_id}.mp4"
        shutil.copyfile(source, public_dir / filename)
        result[scene.scene_id] = filename
    return result
