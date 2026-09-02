# ruff: noqa: E501
from __future__ import annotations

import html
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast

from pydantic import ValidationError

from soloscale.content_canon import StoryReadiness, load_month_one_canon
from soloscale.content_canon_pipeline import (
    ContentCanonError,
    content_brief_from_month_one_story,
    content_brief_from_story_candidate,
)
from soloscale.content_distribution import (
    ContentDistributionError,
    load_distribution_package,
    recent_distribution_packages,
)
from soloscale.content_models import ContentBrief, ContentReviewDecision, ContentRun
from soloscale.content_scan import RecentWorkScan, ScanRange, scan_recent_work
from soloscale.content_workspace import (
    ContentWorkspaceError,
    content_run_directory,
    load_content_review,
    load_content_run,
    parse_claim_ledger,
    run_content_workspace,
    run_content_workspace_with_gateway,
)
from soloscale.creator_production import (
    CreatorProductionError,
    CreatorProductionJob,
    job_elapsed_seconds,
    load_creator_production_jobs,
    load_publication_artifacts,
    load_publish_queue,
    sync_distribution_artifacts,
)
from soloscale.editorial_publishing_handoff import editorial_publishing_status
from soloscale.evidence_agent import Reasoner
from soloscale.media_cost import (
    MediaCostError,
    PricingStatus,
    aggregate_costs,
    estimate_avatar_seconds,
    evaluate_budget,
    load_budget_policy,
    load_cost_receipts,
    load_pricing_catalog,
)
from soloscale.media_profile import (
    MediaProfileError,
    VoiceProviderId,
    load_media_profile,
)
from soloscale.media_quality import (
    MediaQualityChecklist,
    MediaQualityError,
    load_media_quality_review,
    require_approved_media_quality_review,
)
from soloscale.model_gateway import (
    ModelGateway,
    ModelGatewayNotConfigured,
    ModelProviderId,
    model_gateway_for,
)
from soloscale.platform_accounts import (
    ConnectedIdentity,
    PlatformKey,
    eligible_publish_identities,
    platform_snapshot,
)
from soloscale.presenter_assets import (
    PresenterAssetError,
    PresenterMode,
    current_presenter_plan,
    load_presenter_library,
    plan_presenter_assets,
)
from soloscale.reference_intelligence import ReferenceSourceKind, extract_content_pattern
from soloscale.reference_video import (
    ReferenceVideoError,
    load_reference_video,
    recent_reference_videos,
)
from soloscale.story_mining import StoryMiningError, load_story_candidates
from soloscale.ui_shell import (
    DEFAULT_UI_LOCALE,
    UILocale,
    render_app_shell,
    render_creator_nav,
    ui_bool,
    ui_display_value,
    ui_text,
    ui_url,
)
from soloscale.video_factory import creator_video_ready
from soloscale.work_ui import load_work_context, render_use_my_work
from soloscale.youtube_publishing import (
    YouTubeJobSnapshot,
    YouTubePublishingError,
    load_upload_defaults,
    load_youtube_receipt,
)


class ContentFormStatus(StrEnum):
    GENERATED = "generated"
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    FAILED = "failed"


@dataclass(frozen=True)
class ContentFormResult:
    status: ContentFormStatus
    run_id: str | None
    message: str | None
    elapsed_ms: int

    @property
    def error(self) -> str | None:
        """Compatibility view for callers that render only actual form failures."""

        return self.message if self.status is ContentFormStatus.FAILED else None


def _escape(value: str) -> str:
    return html.escape(value or "", quote=True)


def _provider_display_label(provider: str | None, locale: UILocale) -> str:
    """Human-readable service label for a Creator production job."""

    if provider == "template":
        return ui_text(locale, "离线模板", "Offline template")
    if provider == ModelProviderId.SOLOSCALE_HOSTED.value:
        return ui_text(locale, "SoloScale 托管 AI", "SoloScale Hosted AI")
    if provider == ModelProviderId.OLLAMA.value:
        return ui_text(locale, "本地 Ollama", "Local Ollama")
    if provider == ModelProviderId.OPENAI_COMPATIBLE.value:
        return "OpenAI API"
    return _escape(provider or ui_text(locale, "未知", "Unknown"))


def _creator_error_cause_message(cause: str | None, locale: UILocale) -> str | None:
    """Return the actionable primary message for a normalized job error cause."""

    if cause == "VOICE_NOT_CONFIGURED":
        return ui_text(
            locale,
            "未配置可用的语音服务；请配置 Qwen 参考音频，或选择 macOS 系统语音。",
            "No usable voice service configured; configure Qwen reference audio or select macOS system voice.",
        )
    return None


def _creator_job_state_detail(job: CreatorProductionJob, locale: UILocale) -> str:
    """Concise observable state for a running/finished Creator production job."""

    parts: list[str] = []
    if job.stage:
        parts.append(
            f"{_escape(ui_text(locale, '阶段', 'Stage'))}: {_escape(job.stage)}"
        )
    if job.provider:
        parts.append(
            f"{_escape(ui_text(locale, '服务', 'Service'))}: {_provider_display_label(job.provider, locale)}"
        )
    if job.model:
        parts.append(f"{_escape(ui_text(locale, '模型', 'Model'))}: {_escape(job.model)}")
    elapsed = job_elapsed_seconds(job)
    parts.append(
        f"{_escape(ui_text(locale, '已用时', 'Elapsed'))}: {elapsed}s"
    )
    parts.append(
        f"{_escape(ui_text(locale, '模型调用', 'Model calls'))}: {job.model_calls}"
    )
    cause_message = _creator_error_cause_message(job.error_cause, locale)
    if cause_message is not None:
        parts.append(cause_message)
    if job.timeout_seconds is not None:
        parts.append(
            f"{_escape(ui_text(locale, '超时', 'Timeout'))}: {job.timeout_seconds}s"
        )
    return " · ".join(parts)


def _grounded_lines(raw: str, status: str) -> list[str]:
    lines: list[str] = []
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|", maxsplit=2)]
        if len(parts) < 2 or not parts[1]:
            raise ContentWorkspaceError(
                f"{status} 第 {line_number} 行需要填写：事实 | 证据链接 | 边界（可选）"
            )
        limit = parts[2] if len(parts) == 3 else ""
        lines.append(f"{status} | {parts[0]} | {parts[1]} | {limit}")
    return lines


def _ungrounded_lines(raw: str, status: str) -> list[str]:
    return [f"{status} | {line.strip()}" for line in raw.splitlines() if line.strip()]


def _brief_from_form(
    form: dict[str, str], data_root: Path
) -> tuple[ContentBrief, str | None]:
    ledger_lines = [
        *_grounded_lines(form.get("verified_claims", ""), "VERIFIED"),
        *_grounded_lines(form.get("observed_claims", ""), "OBSERVED"),
        *_ungrounded_lines(form.get("hypotheses", ""), "HYPOTHESIS"),
        *_ungrounded_lines(form.get("planned", ""), "PLANNED"),
    ]
    language: Literal["English", "中文"] = "中文" if form.get("language") == "中文" else "English"
    reference_text = form.get("reference_text", "").strip()
    reference_asset = None
    content_pattern = None
    normalized_reference: str | None = None
    reference_id = form.get("reference_id", "").strip()
    if reference_text and reference_id:
        raise ContentWorkspaceError(
            "Choose either one analyzed reference video or pasted reference text"
        )
    if reference_id:
        try:
            analyzed = load_reference_video(data_root, reference_id)
        except ReferenceVideoError as exc:
            raise ContentWorkspaceError(str(exc)) from exc
        reference_asset = analyzed.asset
        content_pattern = analyzed.pattern
    elif reference_text:
        reference_asset, content_pattern, normalized_reference = extract_content_pattern(
            reference_text,
            title=form.get("reference_title", ""),
            author=form.get("reference_author", ""),
            visual_notes=form.get("reference_visual_notes", ""),
        )
    elif any(
        form.get(field, "").strip()
        for field in ("reference_title", "reference_author", "reference_visual_notes")
    ):
        raise ContentWorkspaceError(
            "Paste the reference text before adding reference metadata"
        )
    brief = ContentBrief(
        topic=form.get("topic", "").strip(),
        audience=form.get("audience", "").strip(),
        language=language,
        call_to_action=form.get("call_to_action", "").strip(),
        source_label=form.get("source_label", "").strip(),
        claims=parse_claim_ledger("\n".join(ledger_lines)),
        reference_asset=reference_asset,
        content_pattern=content_pattern,
    )
    return brief, normalized_reference


def run_content_form(
    form: dict[str, str],
    data_root: Path,
    *,
    reasoner: Reasoner | None = None,
    gateway: ModelGateway | None = None,
) -> ContentFormResult:
    started = time.perf_counter()
    try:
        brief, reference_source_text = _brief_from_form(form, data_root)
        generation_mode = form.get(
            "generation_mode", ModelProviderId.SOLOSCALE_HOSTED.value
        ).strip().lower()
        if generation_mode == "template":
            run = run_content_workspace(
                data_root=data_root,
                brief=brief,
                reference_source_text=reference_source_text,
            )
        else:
            selected_gateway = gateway or model_gateway_for(
                generation_mode,
                model=form.get(
                    "provider_model", form.get("ollama_model", "qwen3:8b")
                ),
                reasoner=reasoner,
            )
            run = run_content_workspace_with_gateway(
                data_root=data_root,
                brief=brief,
                gateway=selected_gateway,
                reference_source_text=reference_source_text,
            )
    except ModelGatewayNotConfigured:
        provider = form.get("generation_mode", ModelProviderId.SOLOSCALE_HOSTED.value)
        if provider == ModelProviderId.OPENAI_COMPATIBLE.value:
            message = ui_text(
                "en" if form.get("ui_locale") == "en" else "zh-CN",
                "自定义模型尚未配置。没有发送或保存任何内容。",
                "The custom model is not configured. Nothing was sent or saved.",
            )
        else:
            message = ui_text(
                "en" if form.get("ui_locale") == "en" else "zh-CN",
                "SoloScale 托管 AI 尚未连接到这个本地版本。没有发送或保存任何内容。",
                "SoloScale Hosted AI is not connected in this local build. Nothing was sent or saved.",
            )
        return ContentFormResult(
            status=ContentFormStatus.PROVIDER_NOT_CONFIGURED,
            run_id=None,
            message=message,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    except (ContentWorkspaceError, OSError, ValidationError, ValueError) as exc:
        message = str(exc) or "输入不完整，请检查后重试。"
        if form.get("ui_locale") != "en":
            if message.startswith("Cannot reach local Ollama"):
                message = (
                    "无法连接本机 Ollama。请先启动 Ollama，并确认已安装所选模型。"
                )
            elif message.startswith("Local Ollama returned"):
                message = "本机模型输出没有通过内容安全结构校验，请重试或改用离线模板。"
        return ContentFormResult(
            status=ContentFormStatus.FAILED,
            run_id=None,
            message=message,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    return ContentFormResult(
        status=ContentFormStatus.GENERATED,
        run_id=run.run_id,
        message=None,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


def run_month_one_story(
    story_id: str,
    data_root: Path,
    *,
    language: Literal["English", "中文"],
    gateway: ModelGateway | None,
) -> ContentFormResult:
    """Produce the complete Content Studio bundle from one READY canon story."""

    started = time.perf_counter()
    try:
        brief = content_brief_from_month_one_story(story_id, language=language)
        if gateway is None:
            run = run_content_workspace(data_root=data_root, brief=brief)
        else:
            run = run_content_workspace_with_gateway(
                data_root=data_root,
                brief=brief,
                gateway=gateway,
            )
    except ModelGatewayNotConfigured:
        return ContentFormResult(
            status=ContentFormStatus.PROVIDER_NOT_CONFIGURED,
            run_id=None,
            message=ui_text(
                "zh-CN" if language == "中文" else "en",
                "当前 AI 服务尚未配置；故事仍保留在 Content Canon 中。",
                "The current AI service is not configured. The story remains in the Content Canon.",
            ),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    except (
        ContentCanonError,
        ContentWorkspaceError,
        OSError,
        ValidationError,
        ValueError,
    ) as exc:
        return ContentFormResult(
            status=ContentFormStatus.FAILED,
            run_id=None,
            message=str(exc) or "Content production stopped safely.",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    return ContentFormResult(
        status=ContentFormStatus.GENERATED,
        run_id=run.run_id,
        message=None,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


def run_story_candidate(
    candidate_id: str,
    data_root: Path,
    *,
    language: Literal["English", "中文"],
    gateway: ModelGateway | None,
) -> ContentFormResult:
    """Produce the complete Content Studio bundle from one mined Story Bank candidate."""

    started = time.perf_counter()
    try:
        brief = content_brief_from_story_candidate(
            data_root,
            candidate_id,
            language=language,
        )
        if gateway is None:
            run = run_content_workspace(data_root=data_root, brief=brief)
        else:
            run = run_content_workspace_with_gateway(
                data_root=data_root,
                brief=brief,
                gateway=gateway,
            )
    except ModelGatewayNotConfigured:
        return ContentFormResult(
            status=ContentFormStatus.PROVIDER_NOT_CONFIGURED,
            run_id=None,
            message=ui_text(
                "zh-CN" if language == "中文" else "en",
                "当前 AI 服务尚未配置；候选故事仍保留在 Story Bank 中。",
                "The current AI service is not configured. The candidate remains in the Story Bank.",
            ),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    except (
        ContentCanonError,
        ContentWorkspaceError,
        OSError,
        ValidationError,
        ValueError,
    ) as exc:
        return ContentFormResult(
            status=ContentFormStatus.FAILED,
            run_id=None,
            message=str(exc) or "Content production stopped safely.",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    return ContentFormResult(
        status=ContentFormStatus.GENERATED,
        run_id=run.run_id,
        message=None,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


def _recent_runs(data_root: Path) -> list[str]:
    runs_root = data_root / "content-runs"
    if runs_root.is_symlink() or not runs_root.is_dir():
        return []
    candidates: list[str] = []
    for path in sorted(runs_root.iterdir(), key=lambda item: item.name, reverse=True):
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            load_content_run(data_root, path.name)
        except ContentWorkspaceError:
            continue
        candidates.append(path.name)
        if len(candidates) == 6:
            break
    return candidates


def _form_from_run(run: ContentRun) -> dict[str, str]:
    grouped: dict[str, list[str]] = {
        "VERIFIED": [],
        "OBSERVED": [],
        "HYPOTHESIS": [],
        "PLANNED": [],
    }
    for claim in run.brief.claims:
        if claim.status.value in {"VERIFIED", "OBSERVED"}:
            parts = [claim.text, claim.receipt or ""]
            if claim.limits:
                parts.append(claim.limits)
            grouped[claim.status.value].append(" | ".join(parts))
        else:
            grouped[claim.status.value].append(claim.text)
    provider_kind = (
        run.editorial_provenance[0].provider.kind.value
        if run.editorial_provenance
        else "template"
    )
    generation_mode = (
        provider_kind
        if provider_kind
        in {
            ModelProviderId.SOLOSCALE_HOSTED.value,
            ModelProviderId.OLLAMA.value,
            ModelProviderId.OPENAI_COMPATIBLE.value,
        }
        else "template"
    )
    return {
        "topic": run.brief.topic,
        "audience": run.brief.audience,
        "language": run.brief.language,
        "call_to_action": run.brief.call_to_action,
        "source_label": run.brief.source_label,
        "verified_claims": "\n".join(grouped["VERIFIED"]),
        "observed_claims": "\n".join(grouped["OBSERVED"]),
        "hypotheses": "\n".join(grouped["HYPOTHESIS"]),
        "planned": "\n".join(grouped["PLANNED"]),
        "generation_mode": generation_mode,
        "provider_model": (
            run.editorial_provenance[0].exact_model
            if run.editorial_provenance
            and provider_kind != "template"
            else "qwen3:8b"
        ),
        # Raw reference text is never rendered back into HTML. A new run must either
        # omit reference fields or receive a freshly pasted source.
        "reference_title": "",
        "reference_author": "",
        "reference_text": "",
        "reference_visual_notes": "",
        "reference_id": (
            run.brief.reference_asset.reference_id
            if run.brief.reference_asset is not None
            and run.brief.reference_asset.source_kind is ReferenceSourceKind.LOCAL_VIDEO
            else ""
        ),
    }


def _format_usd(value: Decimal) -> str:
    return f"${value.quantize(Decimal('0.0001'))}"


def _media_cost_panel(
    *,
    data_root: Path,
    run_id: str,
    dynamic_avatar_seconds: float,
    reusable_asset_ratio: float,
    locale: UILocale,
) -> str:
    seconds = Decimal(str(max(0.0, dynamic_avatar_seconds)))
    try:
        receipts = [
            receipt
            for receipt in load_cost_receipts(data_root)
            if receipt.run_id == run_id
        ]
        summary = aggregate_costs(receipts)
        avatar_receipts = [receipt for receipt in receipts if receipt.service == "avatar"]
        estimate = estimate_avatar_seconds(
            seconds=seconds,
            catalog=load_pricing_catalog(data_root),
        )
        budget = evaluate_budget(
            estimate=estimate,
            policy=load_budget_policy(data_root),
        )
    except MediaCostError:
        return f'''<section class="media-cost-panel locked"><span class="kicker">{_escape(ui_text(locale, '成本预览', 'Cost preview'))}</span>
        <p>{_escape(ui_text(locale, '成本台账需要检查；不会自动执行任何付费操作。', 'The cost ledger needs attention. No paid operation will run automatically.'))}</p></section>'''

    projected = summary.estimated_spend_usd
    unknown = summary.unknown_cost_receipts
    if not avatar_receipts and seconds > 0:
        if estimate.pricing_status is PricingStatus.UNKNOWN:
            unknown += 1
        else:
            projected += estimate.estimated_cost_usd or Decimal(0)
    cost_label = (
        f"UNKNOWN + {_format_usd(projected)} {_escape(ui_text(locale, '已知成本', 'known'))}"
        if unknown
        else _format_usd(projected)
    )
    paid_calls = sum(
        receipt.request_count
        for receipt in receipts
        if receipt.pricing_status is not PricingStatus.NOT_BILLABLE
    )
    planned_operations = 2 + (1 if seconds > 0 else 0)
    local_ratio = Decimal(2) / Decimal(planned_operations) * Decimal(100)
    reuse_percent = Decimal(str(max(0.0, min(1.0, reusable_asset_ratio)))) * Decimal(100)
    avatar_price = (
        ui_text(locale, "不需要新 Avatar · $0 API", "No new Avatar needed · $0 API")
        if seconds == 0
        else (
            ui_text(locale, "UNKNOWN · 需先配置价格或单次授权", "UNKNOWN · configure pricing or approve once")
            if estimate.pricing_status is PricingStatus.UNKNOWN
            else _format_usd(estimate.estimated_cost_usd or Decimal(0))
        )
    )
    return f'''<section class="media-cost-panel"><div class="result-head"><div><span class="kicker">{_escape(ui_text(locale, '成本预览', 'Cost preview'))}</span>
      <h3>{_escape(ui_text(locale, '本地优先，付费步骤先确认', 'Local first, paid steps require confirmation'))}</h3></div><span class="review-status">{_escape(budget.decision.value)}</span></div>
      <div class="reference-pattern-grid">
        <div><strong>{_escape(ui_text(locale, '每个故事 / 视频', 'Cost / story and video'))}</strong><p>{cost_label}</p></div>
        <div><strong>{_escape(ui_text(locale, '付费调用 / 故事', 'Paid calls / story'))}</strong><p>{paid_calls}</p></div>
        <div><strong>{_escape(ui_text(locale, '新 Avatar', 'Dynamic Avatar'))}</strong><p>{seconds}s · {_escape(avatar_price)}</p></div>
        <div><strong>{_escape(ui_text(locale, '素材复用率', 'Reusable asset ratio'))}</strong><p>{reuse_percent.quantize(Decimal('0.1'))}%</p></div>
        <div><strong>{_escape(ui_text(locale, '本地处理率（计划）', 'Local processing ratio (planned)'))}</strong><p>{local_ratio.quantize(Decimal('0.1'))}%</p></div>
        <div><strong>{_escape(ui_text(locale, '本地步骤', 'Local steps'))}</strong><p>Qwen3-TTS + Remotion · $0 API</p></div>
      </div></section>'''


def _media_quality_section(
    *,
    data_root: Path,
    run_id: str,
    locale: UILocale,
    sealed: bool,
) -> str:
    error = ""
    try:
        receipt = load_media_quality_review(data_root, run_id)
    except MediaQualityError as exc:
        receipt = None
        error = str(exc)
    checklist = receipt.checklist if receipt is not None else MediaQualityChecklist()
    labels = (
        ("voice_natural", ui_text(locale, "声音自然", "Voice sounds natural")),
        ("pacing_natural", ui_text(locale, "节奏自然", "Pacing feels natural")),
        ("no_static_visual_too_long", ui_text(locale, "没有静态画面停留过久", "No static visual stays too long")),
        ("presenter_adds_value", ui_text(locale, "人物出镜确实增加价值", "Presenter footage adds value")),
        ("language_natural", ui_text(locale, "中文表达自然", "English sounds native")),
        ("claims_evidence_backed", ui_text(locale, "事实都有证据支持", "Claims remain evidence-backed")),
        ("reference_influenced_without_copying", ui_text(locale, "参考影响结构但没有复制", "Reference shaped structure without copying")),
        ("would_publish", ui_text(locale, "我愿意发布这条成片", "I would publish this finished video")),
    )
    checks = "".join(
        f'<label><input type="checkbox" name="{name}" value="on" '
        f'{"checked" if getattr(checklist, name) else ""} />{_escape(label)}</label>'
        for name, label in labels
    )
    status = receipt.decision.value if receipt is not None else "PENDING"
    status_label = _escape(ui_display_value(locale, status))
    revision = f" · r{receipt.revision}" if receipt is not None else ""
    notice = (
        f'<p class="error" role="alert">{_escape(error)}</p>'
        if error
        else ""
    )
    if sealed:
        form = f'<p>{_escape(ui_text(locale, "该检查已绑定到已封装的媒体文件。", "This review is bound to the sealed media files."))}</p>'
    else:
        notes = receipt.notes if receipt is not None else ""
        form = f'''<form method="post" action="/content/media-quality/{run_id}" class="media-quality-form">
          <input type="hidden" name="ui_locale" value="{locale}" />
          <div class="quality-checklist">{checks}</div>
          <label>{_escape(ui_text(locale, '需要修正的具体问题（可选）', 'Concrete issues to fix (optional)'))}<textarea name="notes" maxlength="2000">{_escape(notes)}</textarea></label>
          <button class="secondary" type="submit">{_escape(ui_text(locale, '保存媒体质量检查', 'Save media-quality review'))}</button>
        </form>'''
    return f'''<section class="media-quality-review"><div class="result-head"><div><span class="kicker">{_escape(ui_text(locale, '人工媒体质量', 'Human Media Quality'))}</span>
      <h2>{_escape(ui_text(locale, '这条成片值得发布吗？', 'Is this finished video worth publishing?'))}</h2></div><span class="review-status">{status_label}{revision}</span></div>
      <p>{_escape(ui_text(locale, '全部八项通过后才会解锁统一发布包；这里不会执行发布。', 'All eight checks must pass before the distribution package unlocks. Nothing is published here.'))}</p>{notice}{form}</section>'''


def _result_html(
    run: ContentRun,
    *,
    data_root: Path,
    video_ready: bool,
    creator_video_available: bool,
    creator_video_phase: str | None = None,
    creator_video_error: str | None = None,
    locale: UILocale = DEFAULT_UI_LOCALE,
) -> str:
    run_id = _escape(run.run_id)
    review = load_content_review(data_root, run.run_id)
    review_values = (
        review[1]
        if review is not None
        else {
            "canonical_story": run.drafts.canonical_story,
            "linkedin": run.drafts.linkedin,
            "x_thread": "\n\n".join(run.drafts.x_thread).strip() + "\n",
            "x_post": run.drafts.x_post,
            "blog": run.drafts.blog,
            "youtube_script": run.drafts.youtube_script,
            "video_script": run.drafts.video_script,
        }
    )
    decision = (
        review[0].decision if review is not None else ContentReviewDecision.DRAFT
    )
    revision = review[0].revision if review is not None else 0
    scenes = "".join(
        '<article class="scene">'
        f"<span>{scene.start_second:02d}–{scene.end_second:02d}s</span>"
        f"<strong>{_escape(scene.purpose)}</strong>"
        f"<p>{_escape(scene.voiceover)}</p>"
        f"<small>{_escape(scene.visual)}</small>"
        "</article>"
        for scene in run.drafts.storyboard
    )
    downloads: tuple[tuple[str, str], ...] = (
        (ui_text(locale, "主故事", "Canonical story"), "canonical-story.md"),
        ("LinkedIn", "linkedin.md"),
        ("X Thread", "x-thread.md"),
        ("X Post", "x-post.md"),
        (ui_text(locale, "博客", "Blog"), "blog.md"),
        ("YouTube", "youtube-script.md"),
        (ui_text(locale, "视频脚本", "Video script"), "video-script.md"),
        ("Storyboard", "storyboard.json"),
        ("Publish Pack", "publish-pack.json"),
    )
    if run.brief.content_pattern is not None:
        downloads += (
            (ui_text(locale, "参考 Pattern", "Reference pattern"), "reference-pattern.json"),
        )
    video_download = f"/content/downloads/{run_id}/creator-video.mp4"
    youtube_video_download = f"/content/downloads/{run_id}/youtube-video.mp4"
    thumbnail_download = f"/content/downloads/{run_id}/video-thumbnail.png"
    subtitles_download = f"/content/downloads/{run_id}/video-subtitles.srt"
    run_dir = content_run_directory(data_root, run.run_id)
    distribution_ready = (run_dir / "26_distribution_package.json").is_file()
    handoff_ready = (run_dir / "23_heygen_handoff.json").is_file()
    avatar_map_ready = (run_dir / "24_avatar_segments.json").is_file()
    scene_options = "".join(
        f'<option value="{_escape(scene.id)}">{_escape(scene.id)} · {_escape(scene.purpose)}</option>'
        for scene in run.drafts.storyboard
    )
    dynamic_avatar_seconds = 0.0
    reusable_asset_ratio = 0.0
    try:
        media_profile = load_media_profile(data_root)
        voice_label = (
            ui_text(locale, "你的声音 · 本地 Qwen3-TTS", "Your voice · Local Qwen3-TTS")
            if media_profile.voice_provider is VoiceProviderId.QWEN3_TTS_MLX
            else ui_text(locale, "macOS 系统音色 · 手动选择", "macOS system voice · Explicit fallback")
        )
        avatar_label = (
            ui_text(locale, "Ju 数字分身已连接", "Ju Digital Twin connected")
            if media_profile.heygen_avatar_look_id
            else ui_text(locale, "数字分身尚未连接", "Digital Twin not connected")
        )
    except MediaProfileError:
        voice_label = ui_text(locale, "本地声音尚未配置", "Local voice not configured")
        avatar_label = ui_text(locale, "数字分身尚未连接", "Digital Twin not connected")
    try:
        presenter_library = load_presenter_library(data_root)
        baseline_presenter_plan = plan_presenter_assets(
            run=run, library=presenter_library
        )
        presenter_plan = current_presenter_plan(data_root=data_root, run_id=run.run_id)
        dynamic_avatar_seconds = presenter_plan.dynamic_avatar_seconds
        reusable_asset_ratio = presenter_plan.reusable_asset_ratio
        presenter_library_label = ui_text(
            locale,
            f"可复用人物素材 · {len(presenter_library.assets)}",
            f"Reusable presenter assets · {len(presenter_library.assets)}",
        )
        evidence_visual_options = "".join(
            f'<label><input type="checkbox" name="evidence_visual_scene" value="{_escape(item.scene_id)}" '
            f'{"checked" if next(current.mode for current in presenter_plan.scenes if current.scene_id == item.scene_id) is PresenterMode.NONE else ""} />'
            f'{_escape(item.scene_id)} · {_escape(ui_text(locale, "改用证据画面", "Use evidence visual"))}</label>'
            for item in baseline_presenter_plan.scenes
            if item.mode is PresenterMode.DYNAMIC_AVATAR
        )
        presenter_plan_summary = ui_text(
            locale,
            f"人物场景 {presenter_plan.presenter_scenes} · 复用 {presenter_plan.reusable_presenter_scenes} · 新 Avatar {presenter_plan.dynamic_avatar_scenes} / {presenter_plan.dynamic_avatar_seconds}s",
            f"Presenter scenes {presenter_plan.presenter_scenes} · reused {presenter_plan.reusable_presenter_scenes} · new Avatar {presenter_plan.dynamic_avatar_scenes} / {presenter_plan.dynamic_avatar_seconds}s",
        )
    except PresenterAssetError:
        presenter_library_label = ui_text(
            locale, "人物素材库需要检查", "Presenter library needs attention"
        )
        evidence_visual_options = ""
        presenter_plan_summary = ui_text(
            locale, "暂时无法计算人物计划", "Presenter plan is unavailable"
        )
    cost_panel = _media_cost_panel(
        data_root=data_root,
        run_id=run.run_id,
        dynamic_avatar_seconds=dynamic_avatar_seconds,
        reusable_asset_ratio=reusable_asset_ratio,
        locale=locale,
    )
    media_quality_approved = False
    media_quality_review_available = False
    if video_ready:
        try:
            media_quality_review = load_media_quality_review(data_root, run.run_id)
        except MediaQualityError:
            pass
        else:
            media_quality_review_available = media_quality_review is not None
        try:
            require_approved_media_quality_review(data_root, run.run_id)
        except MediaQualityError:
            pass
        else:
            media_quality_approved = True
    media_quality_section = (
        _media_quality_section(
            data_root=data_root,
            run_id=run.run_id,
            locale=locale,
            sealed=distribution_ready,
        )
        if video_ready
        else ""
    )
    avatar_controls = f'''<section class="avatar-handoff"><span class="kicker">HeyGen Avatar · controlled handoff</span>
      <div class="channel-pills"><span>{_escape(voice_label)}</span><span>{_escape(avatar_label)}</span><span>{_escape(presenter_library_label)}</span></div>
      <p><strong>{_escape(presenter_plan_summary)}</strong></p>
      <p>{_escape(ui_text(locale, 'SoloScale 只导出选定场景的精确旁白；不会上传原始对话或项目文件。', 'SoloScale exports only the exact selected-scene narration. Raw conversations and project files are never uploaded.'))}</p>
      <details><summary>{_escape(ui_text(locale, '管理可复用人物素材', 'Manage reusable presenter assets'))}</summary>
        <form method="post" action="/content/presenter-asset/{run_id}" enctype="multipart/form-data" class="avatar-import-form">
          <input type="hidden" name="ui_locale" value="{locale}" />
          <label>{_escape(ui_text(locale, '素材名称', 'Asset name'))}<input name="display_name" maxlength="120" required /></label>
          <label>{_escape(ui_text(locale, '类型', 'Category'))}<select name="category"><option value="INTRO">Intro</option><option value="GESTURE">Gesture</option><option value="OUTRO">Outro</option></select></label>
          <label>{_escape(ui_text(locale, '来源', 'Source'))}<select name="source_kind"><option value="REAL_FOOTAGE">Real footage</option><option value="AVATAR_OUTPUT">Existing avatar output</option><option value="USER_IMPORTED">Other user import</option></select></label>
          <label>{_escape(ui_text(locale, '布局', 'Layout'))}<select name="layout"><option value="PICTURE_IN_PICTURE">Picture in picture</option><option value="SIDE_PANEL">Side panel</option><option value="FULL_FRAME">Full frame</option></select></label>
          <label>{_escape(ui_text(locale, '素材时长（秒）', 'Asset duration (seconds)'))}<input type="number" name="duration_seconds" min="0.1" max="600" step="0.1" required /></label>
          <label>{_escape(ui_text(locale, '语言（可选）', 'Locale (optional)'))}<select name="locale"><option value="">Any</option><option value="zh-CN">zh-CN</option><option value="en-US">en-US</option></select></label>
          <label>{_escape(ui_text(locale, '选择 MP4（最多 80 MB）', 'Choose MP4 (up to 80 MB)'))}<input type="file" name="presenter_asset" accept="video/mp4,.mp4" required /></label>
          <button class="secondary" type="submit">{_escape(ui_text(locale, '加入素材库', 'Add to library'))}</button>
        </form>
      </details>
      {f'<form method="post" action="/content/presenter-plan/{run_id}" class="avatar-import-form"><input type="hidden" name="ui_locale" value="{locale}" /><strong>{_escape(ui_text(locale, "降低 Avatar 使用", "Reduce Avatar usage"))}</strong>{evidence_visual_options}<button class="secondary" type="submit">{_escape(ui_text(locale, "保存人物计划", "Save presenter plan"))}</button></form>' if evidence_visual_options else ''}
      {f'<a class="secondary-button" href="/content/downloads/{run_id}/heygen-handoff.json" download>{_escape(ui_text(locale, "下载 HeyGen 分段包", "Download HeyGen segment handoff"))}</a>' if handoff_ready else f'<form method="post" action="/content/avatar-handoff/{run_id}"><button class="secondary" type="submit">{_escape(ui_text(locale, "准备 HeyGen 分段包", "Prepare HeyGen segment handoff"))}</button></form>'}
      <form method="post" action="/content/avatar-import/{run_id}" enctype="multipart/form-data" class="avatar-import-form">
        <input type="hidden" name="ui_locale" value="{locale}" />
        <label>{_escape(ui_text(locale, '映射到场景', 'Map to scene'))}<select name="scene_id">{scene_options}</select></label>
        <label>{_escape(ui_text(locale, '选择下载的 Avatar MP4（最多 12 MB）', 'Choose the downloaded Avatar MP4 (up to 12 MB)'))}<input type="file" name="avatar_clip" accept="video/mp4,.mp4" required /></label>
        <button class="secondary" type="submit">{_escape(ui_text(locale, '导入 Avatar 分段', 'Import Avatar segment'))}</button>
      </form>
      {f'<p class="notice">{_escape(ui_text(locale, "已导入 Avatar 分段；下一次渲染会自动映射。", "Avatar segments are imported and will be mapped into the next render."))}</p>' if avatar_map_ready else ''}
    </section>'''
    if video_ready:
        video_action = (
        f'''<video class="creator-video" controls preload="metadata">
          <source src="{video_download}" type="video/mp4" />
        </video>
        <div class="video-actions"><a class="text-link" href="{youtube_video_download}" download>{_escape(ui_text(locale, '下载 16:9 YouTube MP4', 'Download 16:9 YouTube MP4'))}</a>
        <a class="text-link" href="{video_download}" download>{_escape(ui_text(locale, '下载 9:16 Short MP4', 'Download 9:16 Short MP4'))}</a>
        <a class="text-link" href="{subtitles_download}" download>{_escape(ui_text(locale, '下载字幕', 'Download subtitles'))}</a>
        <a class="text-link" href="{thumbnail_download}" download>{_escape(ui_text(locale, '下载封面', 'Download thumbnail'))}</a></div>'''
        )
    elif creator_video_phase in {"QUEUED", "RENDERING"}:
        phase_label = ui_text(
            locale,
            "正在排队"
            if creator_video_phase == "QUEUED"
            else "正在生成旁白、字幕与双尺寸成片",
            "Queued"
            if creator_video_phase == "QUEUED"
            else "Rendering narration, subtitles, and both video sizes",
        )
        video_action = f'''<div class="video-job" data-video-job-active="true" role="status"><span class="status-badge">{_escape(phase_label)}</span>
        <div class="progress-track"><span></span></div><p>{_escape(ui_text(locale, '你可以继续使用其他页面；完成后这里会自动刷新。', 'You can keep using other pages; this view refreshes automatically when complete.'))}</p></div>'''
    elif creator_video_phase == "FAILED":
        video_action = f'''<div class="error" role="alert">{_escape(creator_video_error or ui_text(locale, '本地视频生成失败。', 'Local video render failed.'))}</div>
        <form method="post" action="/content/render/{run_id}" class="render-form"><button class="secondary" type="submit">{_escape(ui_text(locale, '重新生成', 'Try again'))}</button></form>'''
    elif creator_video_available:
        video_action = f"""<form method="post" action="/content/render/{run_id}" class="render-form">
          <button class="primary" type="submit">{_escape(ui_text(locale, '生成 YouTube + Short 成片', 'Render YouTube + Short videos'))}</button>
          <small>{_escape(ui_text(locale, '本机 Remotion 会生成 16:9、9:16 和封面；只使用本次 storyboard，不会发布。', 'Local Remotion creates 16:9, 9:16, and a thumbnail from this storyboard. Nothing is published.'))}</small>
        </form>"""
    else:
        video_action = f"""<p class="hint">{_escape(ui_text(locale, '桌面安装包不包含实验性 Remotion 运行时；请使用上方云端视频入口。', 'The desktop app does not bundle the experimental Remotion runtime. Use the cloud-video entry above.'))}</p>"""
    download_links = "".join(
        f'<a href="/content/downloads/{run_id}/{name}" download>{label}</a>'
        for label, name in downloads
    )
    editorial_trace = "".join(
        "<li>"
        f"<strong>{_escape(item.role.value.title())}</strong> · "
        f"{_escape(item.provider.kind.value)} · {_escape(item.exact_model)} · "
        f"{_escape(item.status.value)}"
        "</li>"
        for item in run.editorial_provenance
    )
    if not editorial_trace:
        editorial_trace = f"<li>{_escape(ui_text(locale, '历史记录 · 模型溯源未知', 'Historical run · model provenance unknown'))}</li>"
    writer = run.editorial_provenance[0] if run.editorial_provenance else None
    engine_labels = {
        "soloscale_hosted": ui_text(locale, "SoloScale 托管 AI", "SoloScale Hosted AI"),
        "ollama": ui_text(locale, "本地 / 自定义 AI", "Local / custom AI"),
        "openai_compatible": ui_text(locale, "自定义 AI", "Custom AI"),
        "template": ui_text(locale, "安全离线草稿", "Safe offline draft"),
    }
    engine_label = engine_labels.get(
        writer.provider.kind.value if writer is not None else "template",
        ui_text(locale, "生成方式未知", "Generation method unknown"),
    )
    if run.execution_state == "AI_EXECUTED":
        usage_suffix = ""
        if run.token_usage:
            usage_suffix = (
                f" · {run.token_usage.get('prompt_eval_tokens', '?')}/"
                f"{run.token_usage.get('output_tokens', '?')} tok"
            )
        latency_suffix = f" · {run.latency_ms}ms" if run.latency_ms is not None else ""
        cost_suffix = f" · ${run.cost_usd:.4f}" if run.cost_usd is not None else ""
        execution_truth = (
            f"AI_EXECUTED · {run.model_calls} call"
            f"{'s' if run.model_calls != 1 else ''}{usage_suffix}{latency_suffix}{cost_suffix}"
        )
    else:
        execution_truth = "AI_NOT_EXECUTED · 0 model calls"
    reference_card = ""
    if run.brief.reference_asset is not None and run.brief.content_pattern is not None:
        asset = run.brief.reference_asset
        pattern = run.brief.content_pattern
        progression = "".join(
            f"<li>{_escape(step)}</li>" for step in pattern.structure.progression
        )
        visuals = ", ".join(pattern.video.visual_elements) or ui_text(
            locale, "未提供视觉备注", "No visual notes supplied"
        )
        reference_card = f'''<section class="reference-card"><div class="result-head"><div>
        <span class="kicker">{_escape(ui_text(locale, '参考内容洞察', 'Reference Intelligence'))}</span>
        <h3>{_escape(asset.title or ui_text(locale, '已蒸馏的参考表达模式', 'Distilled reference pattern'))}</h3>
        <p>{_escape(ui_text(locale, '只使用高层结构、节奏和呈现方式；事实仍只来自你的 Claim Ledger。', 'Only high-level structure, pacing, and presentation are used. Facts still come only from your claim ledger.'))}</p></div>
        <span class="reference-badge">{_escape(ui_text(locale, '原文私有', 'Raw text private'))}</span></div>
        <div class="reference-pattern-grid"><div><strong>{_escape(ui_text(locale, '开场钩子', 'Hook'))}</strong><p>{_escape(pattern.structure.hook)}</p></div>
        <div><strong>{_escape(ui_text(locale, '节奏 / 语气', 'Pacing / tone'))}</strong><p>{_escape(pattern.video.shot_cadence)} · {_escape(pattern.language.tone)}</p></div>
        <div><strong>{_escape(ui_text(locale, '视觉模式', 'Visual pattern'))}</strong><p>{_escape(visuals)}</p></div>
        <div><strong>{_escape(ui_text(locale, '行动引导', 'CTA'))}</strong><p>{_escape(pattern.structure.cta)}</p></div></div>
        <details><summary>{_escape(ui_text(locale, '查看叙事结构', 'View narrative progression'))}</summary><ol>{progression}</ol></details>
        <p class="reference-boundary">{_escape(ui_text(locale, '不会复用参考内容的事实、例子或独特措辞。', 'Reference facts, examples, and distinctive wording are not reused.'))}</p></section>'''
    review_fields = (
        ("canonical_story", ui_text(locale, "主故事", "Canonical story")),
        ("linkedin", "LinkedIn"),
        ("x_thread", "X Thread"),
        ("x_post", "X standalone"),
        ("blog", ui_text(locale, "博客", "Blog")),
        ("youtube_script", "YouTube 4–6 min"),
        ("video_script", ui_text(locale, "视频脚本", "Video script")),
    )
    review_editors = "".join(
        f'''<section class="review-editor"><div class="panel-title"><h3>{_escape(label)}</h3>
        <button class="text-link" type="submit" name="review_action" value="regenerate:{key}">{_escape(ui_text(locale, '安全重新生成这一版', 'Regenerate this adaptation safely'))}</button></div>
        <textarea name="{key}" required>{_escape(review_values[key])}</textarea></section>'''
        for key, label in review_fields
    )
    quality_receipt_download = (
        f'<a class="text-link" href="/content/downloads/{run_id}/media-quality-review.json" download>{_escape(ui_text(locale, "下载媒体质量回执", "Download media-quality receipt"))}</a>'
        if media_quality_review_available
        else ""
    )
    if distribution_ready:
        distribution_section = f'''<section class="distribution-package"><span class="kicker">{_escape(ui_text(locale, '统一发布包', 'Unified distribution package'))}</span>
        <h3>{_escape(ui_text(locale, '视频、封面、字幕和已批准文案已封装', 'Video, thumbnail, subtitles, and approved copy are sealed'))}</h3>
        <p>{_escape(ui_text(locale, '这里只准备精确文件；没有执行任何平台发布。', 'This prepares exact files only; no platform publication was performed.'))}</p>
        <div class="video-actions"><a class="text-link" href="/content/downloads/{run_id}/distribution-package.json" download>{_escape(ui_text(locale, '下载发布清单', 'Download manifest'))}</a>
        <a class="text-link" href="/content/downloads/{run_id}/youtube-upload.json" download>{_escape(ui_text(locale, '下载 YouTube 上传信息', 'Download YouTube upload metadata'))}</a>
        {quality_receipt_download}
        <a class="text-link" data-approved-artifact="{_escape(run_id)}" href="{ui_url('/creator/publish', locale, run_id=run_id)}">{_escape(ui_text(locale, '发布已审核版本', 'Publish approved version'))}</a></div>
        <p class="hint">{_escape(ui_text(locale, '将发布已审核版本；当前未保存的编辑不会进入发布包。', 'The approved version will be published; unsaved editor changes will not enter the distribution package.'))}</p></section>'''
    elif (
        decision is ContentReviewDecision.APPROVED
        and video_ready
        and media_quality_approved
    ):
        distribution_section = f'''<section class="distribution-package"><span class="kicker">{_escape(ui_text(locale, '统一发布包', 'Unified distribution package'))}</span>
        <h3>{_escape(ui_text(locale, '已满足发布包条件', 'Ready to prepare a distribution package'))}</h3>
        <p>{_escape(ui_text(locale, '将已批准文案、双尺寸视频、封面和字幕封成一次可追溯交接；不会发布。', 'Seal approved copy, both videos, the thumbnail, and subtitles into one traceable handoff. Nothing is published.'))}</p>
        <form method="post" action="/content/distribution/{run_id}"><button class="secondary" type="submit">{_escape(ui_text(locale, '准备统一发布包', 'Prepare distribution package'))}</button></form></section>'''
    elif decision is ContentReviewDecision.APPROVED and video_ready:
        distribution_section = f'''<section class="distribution-package locked"><span class="kicker">{_escape(ui_text(locale, '统一发布包', 'Unified distribution package'))}</span>
        <p>{_escape(ui_text(locale, '先完成并通过八项人工媒体质量检查，再封装发布包。', 'Complete and pass the eight-item human media-quality review before preparing the package.'))}</p></section>'''
    elif decision is ContentReviewDecision.APPROVED:
        distribution_section = f'''<section class="distribution-package locked"><span class="kicker">{_escape(ui_text(locale, '统一发布包', 'Unified distribution package'))}</span>
        <p>{_escape(ui_text(locale, '先生成 YouTube 与 Short 成片，随后即可封装发布包。', 'Render the YouTube and Short videos first, then prepare the distribution package.'))}</p></section>'''
    else:
        distribution_section = ""
    return f"""<section id="results" class="result-panel">
      <div class="result-head">
        <div><span class="kicker">{_escape(ui_text(locale, '已生成', 'Generated'))}</span><span class="engine-badge">{_escape(engine_label)}</span><span class="engine-badge">{_escape(execution_truth)}</span><span class="engine-badge">{_escape('zh-CN' if run.brief.language == '中文' else 'en-US')}</span><h2>{_escape(ui_text(locale, '一个主故事，五种渠道适配', 'One canonical story, five adaptations'))}</h2>
          <p>{_escape(ui_text(locale, '内容已私有保存；复制或下载后，人工检查再发布。', 'Drafts are saved privately. Review them after copying or downloading and before publishing.'))}</p></div>
        <div class="downloads">{download_links}</div>
      </div>
      {reference_card}
      <div class="tabs" role="tablist">
        <button type="button" class="tab active" data-tab="canonical">{_escape(ui_text(locale, '主故事', 'Story'))}</button>
        <button type="button" class="tab" data-tab="linkedin">LinkedIn</button>
        <button type="button" class="tab" data-tab="x-thread">{_escape(ui_text(locale, 'X 帖子串', 'X Thread'))}</button>
        <button type="button" class="tab" data-tab="x-post">{_escape(ui_text(locale, 'X 单帖', 'X Post'))}</button>
        <button type="button" class="tab" data-tab="blog">{_escape(ui_text(locale, '博客', 'Blog'))}</button>
        <button type="button" class="tab" data-tab="youtube">YouTube</button>
        <button type="button" class="tab" data-tab="video">{_escape(ui_text(locale, '短视频脚本', 'Short-video script'))}</button>
      </div>
      <div class="tab-panel active" data-panel="canonical"><pre>{_escape(review_values['canonical_story'])}</pre></div>
      <div class="tab-panel" data-panel="linkedin">
        <div class="panel-title"><h3>{_escape(ui_text(locale, 'LinkedIn 草稿', 'LinkedIn Draft'))}</h3>
          <button type="button" class="copy" data-copy="linkedin-copy">{_escape(ui_text(locale, '复制', 'Copy'))}</button></div>
        <pre id="linkedin-copy">{_escape(review_values['linkedin'])}</pre>
      </div>
      <div class="tab-panel" data-panel="x-thread">
        <div class="panel-title"><h3>{_escape(ui_text(locale, 'X 帖子串', 'X Thread'))}</h3>
          <button type="button" class="copy" data-copy="x-copy">{_escape(ui_text(locale, '复制全部', 'Copy all'))}</button></div>
        <pre id="x-copy">{_escape(review_values['x_thread'])}</pre>
      </div>
      <div class="tab-panel" data-panel="x-post"><pre>{_escape(review_values['x_post'])}</pre></div>
      <div class="tab-panel" data-panel="blog"><pre>{_escape(review_values['blog'])}</pre></div>
      <div class="tab-panel" data-panel="youtube"><pre>{_escape(review_values['youtube_script'])}</pre></div>
      <div class="tab-panel" data-panel="video">
        <div class="panel-title"><h3>{run.drafts.storyboard[-1].end_second} {_escape(ui_text(locale, '秒视频设计', 'second video design'))}</h3>
          <a class="text-link" href="/content/downloads/{run_id}/video-script.md"
            download>{_escape(ui_text(locale, '下载脚本', 'Download script'))}</a>
        </div>
        <pre>{_escape(review_values['video_script'])}</pre>
        <div class="storyboard">{scenes}</div>
        {cost_panel}
        <div class="creator-video-result">
          <p><a href="{ui_url('/video', locale, content_run_id=run_id)}">{_escape(ui_text(locale, '在下游视频页使用 Google Vertex AI 生成云端视频', 'Generate a cloud video with Google Vertex AI on the downstream video page'))}</a></p>
          <details><summary>{_escape(ui_text(locale, '实验性本地 Remotion 渲染器', 'Experimental local Remotion renderer'))}</summary>
            {video_action}
          </details>
          {avatar_controls}
        </div>
      </div>
      <section class="unified-review"><div class="result-head"><div><span class="kicker">{_escape(ui_text(locale, '统一审核', 'Unified review'))}</span>
        <h2>{_escape(ui_text(locale, '编辑一次，再决定整个内容包', 'Edit once, then decide the whole bundle'))}</h2></div>
        <span class="review-status">{_escape(ui_display_value(locale, decision.value))} · r{revision}</span></div>
        <form method="post" action="/content/review/{run_id}"><input type="hidden" name="ui_locale" value="{locale}" />
          {review_editors}
          <div class="review-actions"><button class="secondary" type="submit" name="review_action" value="save">{_escape(ui_text(locale, '保存修改', 'Save edits'))}</button>
          <button class="primary" type="submit" name="review_action" value="approve">{_escape(ui_text(locale, '批准内容包', 'Approve bundle'))}</button>
          <button class="danger" type="submit" name="review_action" value="reject">{_escape(ui_text(locale, '拒绝', 'Reject'))}</button></div>
        </form></section>
      {media_quality_section}
      <p class="review-note">{_escape(ui_text(locale, 'SoloScale 没有连接或操作你的社交账号，也没有自动发布。', 'SoloScale did not connect to or operate your social accounts, and nothing was published automatically.'))}</p>
      <details class="editorial-trace"><summary>{_escape(ui_text(locale, '编辑流程溯源', 'Editorial provenance'))}</summary>
        <ol>{editorial_trace}</ol>
        <p>{_escape(ui_text(locale, '流程：撰写 → 独立复核 → 修订 → 人工发布确认。', 'Workflow: Writer → Fresh Reviewer → Reviser → Human publication gate.'))}</p>
        <a class="text-link" href="/content/downloads/{run_id}/editorial-provenance.json"
          download>{_escape(ui_text(locale, '下载溯源记录', 'Download provenance record'))}</a>
      </details>
      {distribution_section}
    </section>"""


def _connected_account_states(
    data_root: Path,
    platform: str,
    *,
    locale: UILocale,
    eligible_ids: set[str],
) -> list[dict[str, str]]:
    """Truthful capability states for connected accounts that cannot publish yet."""

    try:
        snapshot = platform_snapshot(data_root, cast(PlatformKey, platform))
    except Exception:
        return []
    states: list[dict[str, str]] = []
    for identity in snapshot.identities:
        if identity.external_account_id in eligible_ids:
            continue
        if snapshot.connection_state == "REAUTH_REQUIRED":
            label = ui_text(
                locale,
                "已连接 · 需要重新授权",
                "Connected · needs reauthorization",
            )
        else:
            label = ui_text(
                locale,
                "已连接 · 需要补充发布权限",
                "Connected · needs publishing permission",
            )
        states.append({"name": identity.display_name, "label": label})
    return states


def _content_package_entries(data_root: Path) -> list[dict[str, str]]:
    """Ordered, deduplicated Content Packages from the canonical artifact store.

    A Content Package is selectable as soon as it has persisted PublicationArtifacts,
    before any approved DistributionPackage exists. Its displayed status remains
    truthful: DRAFT until a distribution package seals it.
    """

    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        artifacts = load_publication_artifacts(data_root)
    except CreatorProductionError:
        return entries
    for artifact in artifacts:
        run_id = artifact.source_run_id
        if run_id in seen:
            continue
        seen.add(run_id)
        try:
            topic = load_content_run(data_root, run_id).brief.topic
        except ContentWorkspaceError:
            topic = ""
        try:
            ready = load_distribution_package(data_root, run_id) is not None
        except (ContentDistributionError, ContentWorkspaceError):
            ready = False
        entries.append(
            {
                "run_id": run_id,
                "topic": topic.strip() or run_id,
                "status": "READY" if ready else "DRAFT",
            }
        )
    return entries


def _creator_publish_queue_html(
    data_root: Path,
    *,
    packages: list[dict[str, object]],
    locale: UILocale,
    selected_run_id: str | None,
) -> str:
    for package in packages:
        run_id = str(package.get("run_id", ""))
        if not run_id:
            continue
        try:
            sync_distribution_artifacts(data_root, run_id)
        except (CreatorProductionError, ContentWorkspaceError):
            continue
    if selected_run_id is None:
        empty = f'''<article class="publish-queue-row empty"><strong>{_escape(ui_text(locale, '请选择一个 Content Package', 'Please select a Content Package'))}</strong><span>{_escape(ui_text(locale, '选择内容包后，这里只显示该包的 artifacts 与发布上下文。', 'After selecting a content package, only its artifacts and publication context are shown here.'))}</span></article>'''
        return f'''<section class="panel canonical-publish-queue"><span class="kicker">Publish Queue</span><h2>{_escape(ui_text(locale, 'Artifact + 精确账号 = 发布任务', 'Artifact + exact account = publication task'))}</h2><p>{_escape(ui_text(locale, '队列不重新生成内容；只有已审核 Artifact 才能绑定真实 ChannelAccount。', 'The queue never regenerates content; only approved artifacts can bind to a real ChannelAccount.'))}</p><div class="publish-queue-head"><span>{_escape(ui_text(locale, '内容', 'Content'))}</span><span>{_escape(ui_text(locale, '类型', 'Type'))}</span><span>{_escape(ui_text(locale, '平台', 'Platform'))}</span><span>{_escape(ui_text(locale, '账号', 'Account'))}</span><span>{_escape(ui_text(locale, '状态', 'Status'))}</span></div>{empty}</section>'''
    artifacts = load_publication_artifacts(data_root, run_id=selected_run_id)
    queue_by_artifact = {item.artifact_id: item for item in load_publish_queue(data_root)}
    identities = eligible_publish_identities(data_root)
    rows: list[str] = []
    for artifact in artifacts:
        queued = queue_by_artifact.get(artifact.artifact_id)
        if queued is not None:
            destination = f'''<strong>{_escape(queued.channel_account_name)}</strong><small>{_escape(queued.channel_account_id)}</small>'''
            action = f'''<span class="status-badge">{_escape(ui_display_value(locale, queued.status))}</span>'''
        elif artifact.review_status != "APPROVED":
            destination = f'''<span>{_escape(ui_text(locale, '审核后选择账号', 'Choose an account after review'))}</span>'''
            action = f'''<a href="{ui_url('/creator/create', locale, run_id=artifact.source_run_id)}">{_escape(ui_text(locale, '继续审核', 'Continue review'))} →</a>'''
        else:
            accounts = identities.get(artifact.platform, ())
            if accounts:
                options = "".join(
                    f'''<option value="{_escape(item.external_account_id)}">{_escape(item.display_name)} · {_escape(item.external_account_id)}</option>'''
                    for item in accounts
                )
                placeholder = f'''<option value="" disabled selected>{_escape(ui_text(locale, '选择账号…', 'Choose account…'))}</option>'''
                destination = f'''<form method="post" action="/creator/publish/queue"><input type="hidden" name="ui_locale" value="{locale}" /><input type="hidden" name="artifact_id" value="{_escape(artifact.artifact_id)}" /><select name="channel_account_id" aria-label="{_escape(ui_text(locale, '发布账号', 'Publishing account'))}" required>{placeholder}{options}</select><button type="submit">{_escape(ui_text(locale, '确认账号', 'Assign account'))}</button></form>'''
                action = f'''<span class="status-badge">{_escape(ui_text(locale, '等待账号', 'Destination required'))}</span>'''
            else:
                connected = _connected_account_states(
                    data_root,
                    artifact.platform,
                    locale=locale,
                    eligible_ids={item.external_account_id for item in accounts},
                )
                if connected:
                    entries = "".join(
                        f'''<div class="connected-account"><strong>{_escape(item["name"])}</strong><span>{_escape(item["label"])}</span></div>'''
                        for item in connected
                    )
                    destination = f'''<div class="connected-accounts">{entries}<a href="{ui_url('/creator/accounts', locale)}">{_escape(ui_text(locale, '管理账号与权限', 'Manage accounts and permissions'))} →</a></div>'''
                    action = f'''<span class="status-badge">{_escape(ui_text(locale, '需要权限', 'Permission required'))}</span>'''
                else:
                    destination = f'''<a href="{ui_url('/creator/accounts', locale)}">{_escape(ui_text(locale, '连接具备发布权限的账号', 'Connect a publish-capable account'))} →</a>'''
                    action = f'''<span class="status-badge">{_escape(ui_text(locale, '需要账号', 'Account required'))}</span>'''
        rows.append(
            f'''<article class="publish-queue-row" data-artifact-id="{_escape(artifact.artifact_id)}"><div><strong>{_escape(artifact.source_run_id)}</strong><small>{_escape(artifact.artifact_id)}</small></div><span>{_escape(ui_display_value(locale, artifact.artifact_type))}</span><span>{_escape(ui_display_value(locale, artifact.platform))}</span><div class="queue-destination">{destination}</div>{action}</article>'''
        )
    empty = f'''<article class="publish-queue-row empty"><strong>{_escape(ui_text(locale, '还没有可分发 Artifact', 'No distribution artifact yet'))}</strong><span>{_escape(ui_text(locale, '先从故事库或创作页生成并审核内容。', 'Generate and review content from Story Bank or Create first.'))}</span></article>'''
    return f'''<section class="panel canonical-publish-queue"><span class="kicker">Publish Queue</span><h2>{_escape(ui_text(locale, 'Artifact + 精确账号 = 发布任务', 'Artifact + exact account = publication task'))}</h2><p>{_escape(ui_text(locale, '队列不重新生成内容；只有已审核 Artifact 才能绑定真实 ChannelAccount。', 'The queue never regenerates content; only approved artifacts can bind to a real ChannelAccount.'))}</p><div class="publish-queue-head"><span>{_escape(ui_text(locale, '内容', 'Content'))}</span><span>{_escape(ui_text(locale, '类型', 'Type'))}</span><span>{_escape(ui_text(locale, '平台', 'Platform'))}</span><span>{_escape(ui_text(locale, '账号', 'Account'))}</span><span>{_escape(ui_text(locale, '状态', 'Status'))}</span></div>{''.join(rows) or empty}</section>'''


def editorial_publishing_page(
    *,
    data_root: Path,
    error: str | None = None,
    locale: UILocale = DEFAULT_UI_LOCALE,
    creator_mode: bool = False,
    youtube_job: YouTubeJobSnapshot | None = None,
    selected_run_id: str | None = None,
) -> str:
    """Render the separate, sealed-editorial-package publishing flow."""

    publish_identities = eligible_publish_identities(data_root)
    packages = recent_distribution_packages(data_root)
    content_packages = _content_package_entries(data_root)
    selection_notice = ""
    draft_notice = ""
    if selected_run_id:
        distribution_packages = [
            package
            for package in packages
            if str(package.get("run_id", "")) == selected_run_id
        ]
        has_artifacts = any(
            entry["run_id"] == selected_run_id for entry in content_packages
        )
        if distribution_packages:
            packages = distribution_packages
            selection_notice = f'''<p class="notice" role="status" data-approved-artifact="{_escape(selected_run_id)}">{_escape(ui_text(locale, '将发布已审核版本；编辑页中未保存的内容不会进入本次发布。', 'Using the approved version; unsaved editor changes will not enter this publication.'))}</p>'''
        elif has_artifacts:
            packages = []
            draft_notice = f'''<p class="notice warning" role="status">{_escape(ui_text(locale, 'DRAFT · 需要审核后才能发布。', 'DRAFT · Review is required before publishing.'))}</p>'''
        elif error is None:
            error = ui_text(
                locale,
                "当前内容还没有可发布版本，请先完成内容审核并准备统一发布包。",
                "This content has no publishable version yet. Complete review and prepare a distribution package first.",
            )
    error_html = f'<p class="error" role="alert">{_escape(error)}</p>' if error else ""
    package_cards = "".join(
        f'''<article class="package-history-card" data-approved-artifact="{_escape(str(package['run_id']))}"><span class="status-badge">{_escape(ui_text(locale, '预览就绪', 'Preview ready'))}</span>
        <strong>{_escape(str(package['run_id']))}</strong>
        <p>{_escape(ui_text(locale, 'LinkedIn / X 经发布队列绑定精确 ChannelAccount；YouTube 可选择已授权频道上传。', 'LinkedIn / X run through the Publish Queue against an exact ChannelAccount; YouTube can upload to a selected authorized channel.'))}</p>
        <a href="{ui_url('/creator/create' if creator_mode else '/content', locale, run_id=str(package['run_id']))}">{_escape(ui_text(locale, '打开完整内容包', 'Open full content package'))} →</a></article>'''
        for package in packages
    ) or f'''<article class="package-history-card empty"><strong>{_escape(ui_text(locale, '还没有统一发布包', 'No unified distribution package yet'))}</strong>
    <p>{_escape(ui_text(locale, '在 Content 中批准文案并生成双尺寸视频后即可准备。', 'Approve copy and render both video sizes in Content to prepare one.'))}</p></article>'''
    youtube_panel = _youtube_publishing_html(
        data_root,
        packages=packages,
        locale=locale,
        job=youtube_job,
        accounts=publish_identities.get("youtube", ()),
    )
    queue_panel = _creator_publish_queue_html(
        data_root,
        packages=packages,
        locale=locale,
        selected_run_id=selected_run_id,
    )
    creator_nav = render_creator_nav(active="publish", locale=locale) if creator_mode else ""
    package_options = "".join(
        f'<option value="{_escape(entry["run_id"])}" {"selected" if selected_run_id == entry["run_id"] else ""}>{_escape(entry["topic"])} · {_escape(entry["status"])} · {_escape(entry["run_id"])}</option>'
        for entry in content_packages
    )
    package_selector = (
        f'''<section class="panel package-selector"><span class="kicker">{_escape(ui_text(locale, '内容包', 'Content Packages'))}</span>
<h2>{_escape(ui_text(locale, '选择要准备发布的内容包', 'Select a content package to prepare'))}</h2>
<p class="muted">{_escape(ui_text(locale, '先明确选择内容包，再查看其可发布 Artifact 与精确 ChannelAccount；系统不会自动推断要发布哪一个。', 'Choose a content package explicitly before viewing its publishable artifacts and exact ChannelAccount; the system never infers which one to publish.'))}</p>
<form method="get" action="{ui_url('/creator/publish' if creator_mode else '/publishing', locale)}" class="package-select-form"><select name="run_id" aria-label="{_escape(ui_text(locale, '选择内容包', 'Select content package'))}"><option value="">{_escape(ui_text(locale, 'Select content package…', 'Select content package…'))}</option>{package_options}</select><button type="submit">{_escape(ui_text(locale, '选择', 'Select'))}</button></form></section>'''
        if content_packages
        else ""
    )
    body = f"""{creator_nav}{selection_notice}{draft_notice}{error_html}{package_selector}{queue_panel}<section class="panel package-history"><span class="kicker">{_escape(ui_text(locale, 'SoloScale 内容历史', 'SoloScale content history'))}</span>
<h2>{_escape(ui_text(locale, '已批准、可继续处理的发布包', 'Approved packages ready for the next action'))}</h2>
<div class="package-history-grid">{package_cards}</div></section>{youtube_panel}"""
    return render_app_shell(
        active="content" if creator_mode else "publishing",
        locale=locale,
        current_url="/creator/publish" if creator_mode else "/publishing",
        title=f"SoloScale · {ui_text(locale, '发布中心', 'Publishing Center')}",
        eyebrow=ui_text(locale, "发布中心", "Publishing center"),
        heading=ui_text(locale, "把发布前的每一步，放在你手里。", "Keep every pre-publish decision in your hands."),
        description=ui_text(locale, "先校验、再看精确预览，只有你明确确认后，发布队列才会把已审核 Artifact 交给精确 ChannelAccount 执行。", "Verify first, inspect the exact preview, and only after your explicit confirmation does the Publish Queue hand an approved artifact to an exact ChannelAccount."),
        body=body,
        extra_css="""
.canonical-publish-queue{margin-bottom:22px}.publish-queue-head,.publish-queue-row{display:grid;grid-template-columns:minmax(190px,1.4fr) .55fr .6fr minmax(220px,1.3fr) .7fr;gap:12px;align-items:center}.publish-queue-head{padding:0 14px 8px;color:var(--text-muted);font-size:11px;font-weight:900}.publish-queue-row{padding:14px;border-top:1px solid var(--border)}.publish-queue-row small,.queue-destination small{display:block;color:var(--text-muted)}.queue-destination form{display:grid;grid-template-columns:1fr auto;gap:7px}.queue-destination button{padding:8px 10px}.package-history{margin-bottom:22px}.package-history h2{margin:10px 0 16px}.package-history-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.package-history-card{display:grid;gap:8px;padding:16px;border:1px solid var(--border);border-radius:14px;background:var(--surface-subtle)}.package-history-card p{margin:0;color:var(--text-muted)}.package-history-card a{font-weight:800;text-decoration:none}
.package-selector{margin-bottom:18px;padding:18px;border:1px solid var(--border);border-radius:16px;background:var(--surface-subtle)}.package-selector h2{margin:8px 0 8px}.package-selector .muted{margin:0 0 12px}.package-select-form{display:grid;grid-template-columns:1fr auto;gap:9px;max-width:640px}.package-select-form select{min-width:0}.package-select-form button{padding:9px 14px}
.youtube-publish{margin-bottom:22px}.youtube-publish-head{display:flex;justify-content:space-between;gap:16px;align-items:start}.youtube-publish-head h2{margin:8px 0}.youtube-upload-list{display:grid;gap:14px;margin-top:16px}.youtube-upload-card{padding:17px;border:1px solid var(--border);border-radius:14px;background:var(--surface-subtle)}.youtube-upload-card form{display:grid;grid-template-columns:1fr 1fr;gap:10px}.youtube-upload-card label{display:grid;gap:5px}.youtube-upload-card .wide{grid-column:1/-1}.youtube-upload-card textarea{min-height:110px}.youtube-upload-card button{justify-self:start}.youtube-job{padding:12px 14px;border-radius:12px;background:var(--brand-soft);color:var(--brand);font-weight:800}.youtube-receipt{padding:10px 12px;border-radius:10px;background:var(--success-soft);color:var(--success)}
@media(max-width:900px){.publish-queue-head{display:none}.publish-queue-row,.package-history-grid{grid-template-columns:1fr}}
""",
    )


def _youtube_publishing_html(
    data_root: Path,
    *,
    packages: list[dict[str, object]],
    locale: UILocale,
    job: YouTubeJobSnapshot | None,
    accounts: tuple[ConnectedIdentity, ...],
) -> str:
    job_html = ""
    refresh_script = ""
    if job is not None:
        label = {
            "WAITING": ui_text(locale, "等待后台任务", "Waiting for background worker"),
            "AUTHENTICATING": ui_text(locale, "请在浏览器完成 Google 授权", "Complete Google authorization in your browser"),
            "UPLOADING": ui_text(locale, "正在上传", "Uploading"),
            "SUCCESS": ui_text(locale, "YouTube 上传成功", "YouTube upload succeeded"),
            "FAILED": job.error_message or ui_text(locale, "YouTube 操作失败", "YouTube operation failed"),
        }.get(job.phase, ui_display_value(locale, job.phase))
        progress = f" · {job.progress_percent}%" if job.progress_percent is not None else ""
        link = (
            f' · <a href="{_escape(job.video_url or "")}" target="_blank" rel="noopener noreferrer">{_escape(job.video_id or "YouTube")}</a>'
            if job.phase == "SUCCESS" and job.video_url
            else ""
        )
        job_html = f'<p class="youtube-job" data-phase="{job.phase}">{_escape(label)}{progress}{link}</p>'
        if job.phase in {"WAITING", "AUTHENTICATING", "UPLOADING"}:
            refresh_script = "<script>setTimeout(()=>location.reload(),1500)</script>"
    if not accounts:
        return f'''<section class="panel youtube-publish"><div class="youtube-publish-head"><div><span class="status-badge">YouTube</span><h2>{_escape(ui_text(locale, '先连接发布频道', 'Connect a publishing channel'))}</h2><p>{_escape(ui_text(locale, '一个 OAuth Client 可以保存多个频道授权；每次上传只选择一个目标频道。', 'One OAuth Client can store multiple channel authorizations; each upload targets one selected channel.'))}</p></div><a class="secondary-button" href="{ui_url('/creator/accounts', locale)}">{_escape(ui_text(locale, '打开账号中心', 'Open Account Center'))}</a></div>{job_html}</section>{refresh_script}'''
    account_options = "".join(
        f'<option value="{_escape(item.external_account_id)}">{_escape(item.display_name)} · {_escape(item.external_account_id)}</option>'
        for item in accounts
    )
    account_select_placeholder = f'<option value="" disabled selected>{_escape(ui_text(locale, "选择频道…", "Choose channel…"))}</option>'
    cards: list[str] = []
    for package in packages:
        run_id = str(package.get("run_id", ""))
        try:
            defaults = load_upload_defaults(data_root, run_id)
        except YouTubePublishingError:
            continue
        receipts: list[str] = []
        for account in accounts:
            try:
                receipt = load_youtube_receipt(
                    data_root, run_id, account.external_account_id
                )
            except YouTubePublishingError:
                receipt = None
            if receipt is not None:
                receipts.append(
                    f'<p class="youtube-receipt">{_escape(account.display_name)} · <a href="{_escape(str(receipt.get("video_url", "")))}" target="_blank" rel="noopener noreferrer">{_escape(str(receipt.get("video_id", "")))}</a> · {_escape(str(receipt.get("privacy_status", "")))}</p>'
                )
        title = _escape(str(defaults.get("title", "")))
        description = _escape(str(defaults.get("description", "")))
        cards.append(f'''<article class="youtube-upload-card"><strong>{_escape(run_id)}</strong>{"".join(receipts)}<form method="post" action="/publishing/youtube/upload"><input type="hidden" name="ui_locale" value="{locale}" /><input type="hidden" name="run_id" value="{_escape(run_id)}" /><label>{_escape(ui_text(locale, '目标频道', 'Target channel'))}<select name="channel_id" required>{account_select_placeholder}{account_options}</select></label><label>{_escape(ui_text(locale, '可见性', 'Privacy'))}<select name="privacy_status"><option value="private">{_escape(ui_text(locale, '私密', 'Private'))}</option><option value="unlisted">{_escape(ui_text(locale, '不公开列出', 'Unlisted'))}</option><option value="public">{_escape(ui_text(locale, '公开', 'Public'))}</option></select></label><label class="wide">{_escape(ui_text(locale, '标题', 'Title'))}<input name="title" maxlength="100" value="{title}" required /></label><label class="wide">{_escape(ui_text(locale, '描述', 'Description'))}<textarea name="description" maxlength="5000">{description}</textarea></label><label class="wide">{_escape(ui_text(locale, '标签（逗号分隔）', 'Tags (comma-separated)'))}<input name="tags" maxlength="2000" /></label><label class="wide">{_escape(ui_text(locale, '输入 UPLOAD 才会真正上传', 'Type UPLOAD to perform the real upload'))}<input name="confirmation" autocomplete="off" required /></label><button type="submit">{_escape(ui_text(locale, '上传到所选频道', 'Upload to selected channel'))}</button></form></article>''')
    empty = f'<p>{_escape(ui_text(locale, "还没有可上传的 Distribution Package。", "No Distribution Package is ready for upload."))}</p>'
    return f'''<section class="panel youtube-publish"><div class="youtube-publish-head"><div><span class="status-badge">YouTube</span><h2>{_escape(ui_text(locale, '选择频道并上传', 'Select a channel and upload'))}</h2><p>{_escape(ui_text(locale, '上传在独立后台任务中执行；不会自动重试。', 'Upload runs in a bounded background job with no automatic retry.'))}</p></div><a class="secondary-button" href="{ui_url('/creator/accounts', locale)}">{_escape(ui_text(locale, '管理频道', 'Manage channels'))}</a></div>{job_html}<div class="youtube-upload-list">{"".join(cards) or empty}</div></section>{refresh_script}'''


def _editorial_channel_html(
    data_root: Path,
    channel: Literal["linkedin", "x"],
    *,
    locale: UILocale = DEFAULT_UI_LOCALE,
    identities: tuple[ConnectedIdentity, ...] = (),
) -> str:
    label = "LinkedIn" if channel == "linkedin" else "X"
    if not identities:
        return f'''<section class="channel empty-state" data-platform="{channel}"><span class="kicker">{label}</span>
<h2>{_escape(ui_text(locale, '没有具备发布权限的账号', 'No publish-capable account'))}</h2>
<p>{_escape(ui_text(locale, '账号中心必须先验证真实身份与发布 scope；手动 ACTIVE 状态不会解锁发布。', 'Account Center must verify a real identity and publishing scope first; a manual ACTIVE state cannot unlock publishing.'))}</p>
<a class="secondary-button" href="{ui_url('/creator/accounts', locale)}">{_escape(ui_text(locale, '打开账号中心', 'Open Account Center'))}</a></section>'''
    try:
        preview, receipt = editorial_publishing_status(data_root, channel)
    except ValueError:
        preview, receipt = None, None
    if preview is None:
        return f'''<section class="channel empty-state"><span class="kicker">{label}</span>
<h2>{_escape(ui_text(locale, '还没有可审核的发布计划', 'No plan ready for review'))}</h2>
<p>{_escape(ui_text(locale, '先在 Content Studio 完成内容，再把最终包带到这里校验。', 'Finalize a package in Content Studio, then bring it here for verification.'))}</p>
<a class="secondary-button" href="{ui_url('/content', locale)}">{_escape(ui_text(locale, '打开 Content Studio', 'Open Content Studio'))}</a></section>'''
    if receipt is not None and receipt.get("plan_id") != preview.get("plan_id"):
        receipt = None
    parts = preview.get("parts", [])
    exact_parts = "".join(
        f"<article><h3>{_escape(ui_text(locale, '第', 'Part'))} {index}{_escape(ui_text(locale, ' 段', ''))}</h3><pre>{_escape(str(part))}</pre></article>"
        for index, part in enumerate(parts if isinstance(parts, list) else [], start=1)
    )
    image = cast(dict[str, object], preview.get("image")) if isinstance(preview.get("image"), dict) else {}
    duplicate_value = preview.get("duplicate")
    duplicate = _escape(
        ui_bool(locale, duplicate_value)
        if isinstance(duplicate_value, bool)
        else ui_display_value(locale, duplicate_value or "UNKNOWN")
    )
    indeterminate_value = preview.get("indeterminate")
    indeterminate = _escape(
        ui_bool(locale, indeterminate_value)
        if isinstance(indeterminate_value, bool)
        else ui_display_value(locale, indeterminate_value or "UNKNOWN")
    )
    image_path = _escape(str(preview.get("source_image_path", "BuildLog-staged image")))
    image_hash = _escape(str(image.get("sha256", "")))
    image_size = _escape(f"{image.get('width', '?')}×{image.get('height', '?')}")
    alt = _escape(str(image.get("alt_text", "")))
    account = _escape(str(preview.get("account_display_name", "")))
    plan_hash = _escape(str(preview.get("plan_hash", "")))
    blocked = preview.get("duplicate") is True or preview.get("indeterminate") is True
    receipt_html = (
        f"<p><strong>{_escape(ui_text(locale, '发布回执（BuildLog 投影）：', 'Publication receipt (BuildLog projection):'))}</strong> {_escape(ui_display_value(locale, receipt.get('status', 'UNKNOWN')))} · "
        f"{_escape(ui_text(locale, '计划', 'plan'))} {_escape(str(receipt.get('plan_id', '')))}</p>"
        if receipt is not None
        else (
            f'<p class="error">{_escape(ui_text(locale, "检测到重复内容或尚未解决的先前尝试，发布已阻止。", "Publication is blocked by a duplicate or unresolved prior attempt."))}</p>'
            if blocked
            else f'''<form method="post" action="/publishing/editorial/{channel}/publish"><input type="hidden" name="ui_locale" value="{locale}" />
<label>{_escape(ui_text(locale, f'输入 PUBLISH 批准这份精确的 {label} 计划', f'Type PUBLISH to approve this exact {label} plan'))}<input name="confirmation" autocomplete="off" required></label>
<button type="submit">PUBLISH {label}</button></form>'''
        )
    )
    return f"""<section class="channel"><h2>{label} {_escape(ui_text(locale, '计划预览', 'plan preview'))}</h2>
<p class="meta">{_escape(ui_text(locale, '账号：', 'Account:'))} {_escape(str(preview.get("account_reference", "")))} · {_escape(ui_text(locale, '显示名称：', 'Display name:'))} {account}<br>{_escape(ui_text(locale, '聚合计划哈希：', 'Aggregate plan hash:'))} <code>{plan_hash}</code><br>{_escape(ui_text(locale, '重复：', 'Duplicate:'))} {duplicate} · {_escape(ui_text(locale, '状态不确定：', 'Indeterminate:'))} {indeterminate}</p>
{exact_parts}<h3>{_escape(ui_text(locale, '图片', 'Image'))}</h3><img src="/publishing/editorial/{channel}/image" alt="{alt}" style="display:block;max-width:100%;height:auto;border-radius:12px;border:1px solid #334155"><p class="meta">{_escape(ui_text(locale, '路径：', 'Path:'))} {image_path}<br>SHA-256: <code>{image_hash}</code> · {_escape(ui_text(locale, '尺寸：', 'Dimensions:'))} {image_size}<br>{_escape(ui_text(locale, '替代文本：', 'Alt text:'))} {alt}</p>{receipt_html}</section>"""


def _scan_html(
    scan: RecentWorkScan | None,
    *,
    selected_candidate_id: str | None,
    locale: UILocale,
) -> str:
    if scan is None:
        return f'''<section class="scan-work"><div><span class="kicker">{_escape(ui_text(locale, '真实工作 → 内容', 'Real work → content'))}</span>
        <h2>{_escape(ui_text(locale, '扫描我最近做的事', 'Scan my recent work'))}</h2>
        <p>{_escape(ui_text(locale, '只读取现有本地回执和元数据，不重新索引、不读取聊天正文、也不调用模型。', 'Reads existing local receipts and metadata only. No re-indexing, transcript bodies, or model call.'))}</p></div>
        <div class="scan-actions"><a class="primary-button" href="{ui_url('/content', locale, scan_range='today')}">{_escape(ui_text(locale, '扫描今天', 'Scan today'))}</a>
        <a class="secondary-button" href="{ui_url('/content', locale, scan_range='7d')}">{_escape(ui_text(locale, '扫描最近 7 天', 'Scan last 7 days'))}</a></div></section>'''
    cards: list[str] = []
    category_labels = {
        "Built": ui_text(locale, "构建", "Built"),
        "Fixed": ui_text(locale, "修复", "Fixed"),
        "Learned": ui_text(locale, "学到", "Learned"),
        "Decided": ui_text(locale, "决策", "Decided"),
        "Shipped": ui_text(locale, "交付", "Shipped"),
        "Failed / corrected": ui_text(locale, "失败 / 纠正", "Failed / corrected"),
    }
    for candidate in scan.candidates:
        evidence = "".join(
            f"<li>{_escape(item)}</li>" for item in candidate.supporting_evidence
        )
        selected = candidate.candidate_id == selected_candidate_id
        action = (
            f'<span class="selected-candidate">{_escape(ui_text(locale, "已选择", "Selected"))}</span>'
            if selected
            else f'<a class="secondary-button" href="{ui_url("/content", locale, scan_range=scan.scan_range.value, candidate_id=candidate.candidate_id)}#content-topic">{_escape(ui_text(locale, "用这个故事", "Use this story"))}</a>'
        )
        cards.append(
            f'''<article class="candidate-card{' selected' if selected else ''}"><div class="candidate-head"><span>{_escape(category_labels[candidate.category.value])}</span><small>{_escape(candidate.confidence.upper())}</small></div>
            <h3>{_escape(candidate.what_happened)}</h3><p>{_escape(candidate.why_share)}</p>
            <details><summary>{_escape(ui_text(locale, '支持证据', 'Supporting evidence'))}</summary><ul>{evidence}</ul></details>
            <p class="audience-value"><strong>{_escape(ui_text(locale, '对受众的价值：', 'Audience value:'))}</strong> {_escape(candidate.audience_value)}</p>{action}</article>'''
        )
    if not cards:
        cards.append(
            f'<article class="candidate-card empty"><h3>{_escape(ui_text(locale, "这个时间范围还没有足够明确的候选", "No sufficiently grounded candidate in this range"))}</h3><p>{_escape(ui_text(locale, "先完成一次真实工作或选择最近 7 天；不会为了填满列表而生成内容。", "Complete real work first or try the last seven days. SoloScale will not invent content to fill the list."))}</p></article>'
        )
    source_text = ", ".join(scan.sources_used) or ui_text(locale, "无", "None")
    return f'''<section class="scan-results"><div class="result-head"><div><span class="kicker">{_escape(ui_text(locale, '工作扫描', 'Work scan'))}</span>
      <h2>{_escape(ui_text(locale, '今天', 'Today') if scan.scan_range is ScanRange.TODAY else ui_text(locale, '最近 7 天', 'Last 7 days'))} · {len(scan.candidates)} {_escape(ui_text(locale, '个候选', 'candidates'))}</h2>
      <p>{_escape(ui_text(locale, '已检查元数据记录', 'Metadata records checked'))}: {scan.items_scanned} · {_escape(ui_text(locale, '来源', 'Sources'))}: {_escape(source_text)}</p></div>
      <div class="scan-actions"><a href="{ui_url('/content', locale, scan_range='today')}">{_escape(ui_text(locale, '今天', 'Today'))}</a><a href="{ui_url('/content', locale, scan_range='7d')}">{_escape(ui_text(locale, '7 天', '7 days'))}</a></div></div>
      <div class="candidate-grid">{''.join(cards)}</div></section>'''


def _story_mining_html(
    data_root: Path, locale: UILocale, notice: str | None = None
) -> str:
    """Live Story Bank action: mine a small batch from approved evidence."""

    try:
        candidates = load_story_candidates(data_root)
    except StoryMiningError:
        candidates = ()
    candidate_cards = "".join(
        f'''<article class="mined-candidate"><strong>{_escape(candidate.working_title_cn if locale == 'zh-CN' else candidate.working_title_en)}</strong><p>{_escape(candidate.one_sentence_thesis)}</p><small>{_escape(candidate.generated_by)} · {len(candidate.source_evidence_ids)} {_escape(ui_text(locale, '条证据', 'evidence items'))} · {_escape(ui_text(locale, '确定性草稿', 'Deterministic draft'))}</small><form method="post" action="/creator/production/start" class="canon-direct-form"><input type="hidden" name="ui_locale" value="{locale}" /><input type="hidden" name="source_kind" value="STORY" /><input type="hidden" name="source_story_id" value="{candidate.candidate_id}" /><label>{_escape(ui_text(locale, '语言', 'Language'))}<select name="language"><option value="中文">中文</option><option value="English">English</option></select></label><button type="submit" name="production_action" value="article">{_escape(ui_text(locale, '生成文章', 'Generate article'))}</button><button class="secondary-button" type="submit" name="production_action" value="video">{_escape(ui_text(locale, '生成视频', 'Generate video'))}</button><button class="secondary-button" type="submit" name="production_action" value="queue">{_escape(ui_text(locale, '生成并加入队列', 'Generate and queue'))}</button></form></article>'''
        for candidate in candidates
    ) or f'''<p class="hint">{_escape(ui_text(locale, '还没有挖掘出的候选故事。', 'No mined story candidates yet.'))}</p>'''
    notice_html = (
        f'<p class="notice" role="status">{_escape(notice)}</p>'
        if notice
        else ""
    )
    form = f'''<form method="post" action="/creator/stories/mine" data-loading-label="{_escape(ui_text(locale, '正在扫描…', 'Scanning…'))}"><input type="hidden" name="ui_locale" value="{locale}" /><button class="secondary-button" type="submit" data-loading-label="{_escape(ui_text(locale, '正在扫描…', 'Scanning…'))}">{_escape(ui_text(locale, '从最新工作生成更多故事', 'Generate More Stories'))}</button></form>'''
    note = _escape(
        ui_text(
            locale,
            "候选来自已批准证据的确定性草稿，未调用模型（model_calls=0）；真实语义挖掘需要单独授权的模型运行。",
            "Candidates are deterministic drafts from approved evidence with no model call (model_calls=0); real semantic mining requires a separately authorized model run.",
        )
    )
    return f'''<section class="story-mining"><div class="result-head"><div><span class="kicker">{_escape(ui_text(locale, '活的故事库', 'Live Story Bank'))}</span><h2>{_escape(ui_text(locale, '从最新工作生成更多故事', 'Generate More Stories'))}</h2><p>{note}</p></div>{form}</div>{notice_html}<div class="mined-grid">{candidate_cards}</div></section>'''


def _creator_production_jobs_html(data_root: Path, locale: UILocale) -> str:
    """Persist one production lifecycle strip across Story Bank and Create views."""

    try:
        jobs = load_creator_production_jobs(data_root, limit=10)
    except CreatorProductionError:
        jobs = ()
    if not jobs:
        return ""
    phase_copy = {
        "QUEUED": ui_text(locale, "已加入后台队列", "Queued in background"),
        "GENERATING_CONTENT": ui_text(locale, "正在生成内容", "Generating content"),
        "RENDERING_VIDEO": ui_text(locale, "正在后台渲染视频", "Rendering video in background"),
        "READY": ui_text(locale, "成品已就绪", "Artifacts ready"),
        "AI_NOT_EXECUTED": "AI_NOT_EXECUTED",
        "FAILED": ui_text(locale, "制作失败", "Production failed"),
    }
    output_copy = {
        "ARTICLE": ui_text(locale, "文章", "Article"),
        "VIDEO": ui_text(locale, "视频", "Video"),
    }
    active = False
    rows: list[str] = []
    for job in jobs:
        phase = job.phase
        if phase in {"QUEUED", "GENERATING_CONTENT", "RENDERING_VIDEO"}:
            active = True
            action = f'<a class="text-link" href="{ui_url("/creator/create", locale, creator_job=job.job_id)}">{_escape(ui_text(locale, "查看进度", "View progress"))} →</a>'
        elif phase == "READY":
            action = f'<a class="text-link" href="{ui_url("/creator/publish", locale, run_id=job.content_run_id or "")}">{_escape(ui_text(locale, "打开发布队列", "Open Publish Queue"))} →</a>'
        elif phase == "AI_NOT_EXECUTED":
            action = f'<a class="text-link" href="{ui_url("/settings/ai", locale)}">{_escape(ui_text(locale, "配置 AI 服务", "Configure AI service"))} →</a>'
        else:
            action = f'<a class="text-link" href="{ui_url("/creator/stories", locale)}">{_escape(ui_text(locale, "重新生成", "Retry"))} →</a>'
        source = job.request.source_story_id or ui_text(locale, "自由创作", "Free create")
        outputs = " + ".join(output_copy.get(item, item) for item in job.request.outputs)
        meta = source
        if job.content_run_id:
            meta += f' · {_escape(job.content_run_id)}'
        if job.error_code:
            meta += f' · {_escape(job.error_code)}'
        state_detail = _creator_job_state_detail(job, locale)
        rows.append(
            f'''<article class="creator-production-job" data-phase="{phase}"><span class="status-badge">{_escape(phase_copy.get(phase, phase))}</span><div><strong>{_escape(outputs)}</strong><small>{_escape(meta)}</small><small class="job-state">{state_detail}</small></div>{action}</article>'''
        )
    refresh = (
        "<script>setTimeout(()=>location.reload(),1500)</script>" if active else ""
    )
    heading = ui_text(locale, "内容生产进度", "Content production progress")
    note = ui_text(
        locale,
        "任务在后台持续运行；刷新或离开页面后仍会保留进度、失败原因和下一步。",
        "Jobs keep running in the background; progress, failure reasons, and next steps survive refresh and navigation.",
    )
    return f'''<section class="creator-production-lifecycle" aria-label="{_escape(heading)}"><div class="result-head"><div><span class="kicker">ContentProject</span><h2>{_escape(heading)}</h2><p>{_escape(note)}</p></div></div><div class="creator-production-jobs">{''.join(rows)}</div></section>{refresh}'''


def _month_one_canon_html(
    locale: UILocale, *, creator_story_bank: bool = False, data_root: Path
) -> tuple[str, str]:
    canon = load_month_one_canon()
    status_labels = {
        StoryReadiness.READY_FOR_PRODUCTION: ui_text(
            locale, "证据已就绪", "Evidence ready"
        ),
        StoryReadiness.NEEDS_EVIDENCE: ui_text(
            locale, "需要补证", "Needs evidence"
        ),
        StoryReadiness.NEEDS_USER_INPUT: ui_text(
            locale, "需要你确认", "Needs your input"
        ),
        StoryReadiness.DRAFT: ui_text(locale, "草稿", "Draft"),
    }
    week_labels = {
        1: ui_text(locale, "为什么 SoloScale 必须存在", "Why SoloScale had to exist"),
        2: ui_text(locale, "事实、学习与面试防守", "Truth, learning, and interview defense"),
        3: ui_text(locale, "性能、模型与架构取舍", "Performance, models, and architecture"),
        4: ui_text(locale, "失败隔离、求职与内容复利", "Failure isolation and compounding"),
    }
    layer_labels = (
        ("fact", "Fact", "事实"),
        ("architecture", "Architecture", "架构"),
        ("decision", "Decision", "决策"),
        ("implementation", "Implementation", "实现"),
        ("failure", "Failure", "失败"),
        ("evolution", "Evolution", "演进"),
    )
    week_sections: list[str] = []
    story_payload: dict[str, dict[str, str]] = {}
    for week in range(1, 5):
        cards: list[str] = []
        for story in (item for item in canon.stories if item.week == week):
            layers = "".join(
                f'''<div><span>{_escape(ui_text(locale, label_zh, label_en))}</span><p>{_escape(getattr(story, field))}</p></div>'''
                for field, label_en, label_zh in layer_labels
            )
            evidence = "".join(
                f"<li>{_escape(item)}</li>" for item in story.evidence_candidates
            )
            metrics = "".join(
                f"<li>{_escape(item)}</li>" for item in story.verified_metrics
            ) or f"<li>{_escape(ui_text(locale, '尚无可发布的精确指标。', 'No publication-ready exact metric yet.'))}</li>"
            guardrails = "".join(
                f"<li>{_escape(item)}</li>" for item in story.overclaim_guardrails
            )
            secondary = " · ".join(story.secondary_formats)
            production_actions = (
                f'''<form method="post" action="/creator/production/start" class="canon-direct-form">
                <input type="hidden" name="ui_locale" value="{locale}" />
                <input type="hidden" name="source_kind" value="STORY" />
                <input type="hidden" name="source_story_id" value="{story.story_id}" />
                <label>{_escape(ui_text(locale, '语言', 'Language'))}<select name="language"><option value="中文">中文</option><option value="English">English</option></select></label>
                <button type="submit" name="production_action" value="article">{_escape(ui_text(locale, '生成文章', 'Generate article'))}</button>
                <button class="secondary-button" type="submit" name="production_action" value="video">{_escape(ui_text(locale, '生成视频', 'Generate video'))}</button>
                <button class="secondary-button" type="submit" name="production_action" value="queue">{_escape(ui_text(locale, '生成并加入队列', 'Generate and queue'))}</button>
                <small>{_escape(ui_text(locale, '后台复用同一个 ContentProject；成品进入统一发布队列，未审核内容不会发布。', 'One background ContentProject produces the artifacts; unreviewed content can never publish.'))}</small>
                </form>'''
                if story.status is StoryReadiness.READY_FOR_PRODUCTION
                else f'''<button type="button" data-canon-select="{story.story_id}" data-canon-format="video">{_escape(ui_text(locale, '补充证据 / 确认', 'Add evidence / confirm'))}</button>'''
            )
            cards.append(
                f'''<details class="canon-story" data-canon-status="{story.status.value}" id="canon-{story.story_id.lower()}">
                <summary><span class="canon-sequence">{story.sequence:02d}</span><span><strong>{_escape(story.title_cn if locale == 'zh-CN' else story.working_title_en)}</strong><small>{_escape(story.working_title_en if locale == 'zh-CN' else story.title_cn)}</small></span><em class="canon-status {story.status.value.lower()}">{_escape(status_labels[story.status])}</em></summary>
                <div class="canon-story-body"><p class="canon-thesis">{_escape(story.one_sentence_thesis)}</p>
                <div class="six-layers">{layers}</div>
                <div class="canon-meta"><div><h4>{_escape(ui_text(locale, '证据候选', 'Evidence candidates'))}</h4><ul>{evidence}</ul></div><div><h4>{_escape(ui_text(locale, '已核验指标', 'Verified metrics'))}</h4><ul>{metrics}</ul></div><div><h4>{_escape(ui_text(locale, '防止过度表达', 'Overclaim guardrails'))}</h4><ul>{guardrails}</ul></div></div>
                <div class="canon-production"><span>{_escape(ui_text(locale, '主格式', 'Primary'))}: {_escape(story.primary_format)}</span><span>{_escape(ui_text(locale, '可复用', 'Repurpose'))}: {_escape(secondary)}</span></div>
                <div class="canon-actions">{production_actions}<button class="secondary-button" type="button" data-canon-select="{story.story_id}" data-canon-format="blog">{_escape(ui_text(locale, '打开并编辑输入', 'Open and edit input'))}</button></div></div></details>'''
            )
            story_payload[story.story_id] = {
                "title": story.title_cn if locale == "zh-CN" else story.working_title_en,
                "thesis": story.one_sentence_thesis,
            }
        week_sections.append(
            f'''<section class="canon-week" id="canon-week-{week}"><div class="canon-week-title"><span>{_escape(ui_text(locale, f'第 {week} 周', f'WEEK {week}'))}</span><h3>{_escape(week_labels[week])}</h3></div><div class="canon-stories">{''.join(cards)}</div></section>'''
        )
    options = "".join(
        f'<option value="{status.value}">{_escape(status_labels[status])}</option>'
        for status in StoryReadiness
    )
    payload_json = json.dumps(story_payload, ensure_ascii=False).replace("</", "<\\/")
    html_section = f'''<section class="month-one-canon" aria-labelledby="month-one-title"><div class="result-head"><div><span class="kicker">{_escape(ui_text(locale, '第一个月 · 工程故事库', 'Month 1 · Engineering Story Library'))}</span><h2 id="month-one-title">{_escape(ui_text(locale, '第一个月：从自动化幻想到可用结果', 'Month 1: From automation ambition to useful outcomes'))}</h2><p>{_escape(ui_text(locale, '24 个真实工程故事，按四周组织。先选故事，再补证、做视频或写博客；这里不会调用模型或发布。', 'Twenty-four real engineering stories across four weeks. Select one, then add evidence and produce a video or blog later; nothing here calls a model or publishes.'))}</p></div><label class="canon-filter">{_escape(ui_text(locale, '按就绪状态筛选', 'Filter by readiness'))}<select id="canon-status-filter"><option value="ALL">{_escape(ui_text(locale, '全部 24 个故事', 'All 24 stories'))}</option>{options}</select></label></div><nav class="canon-week-nav" aria-label="{_escape(ui_text(locale, '第一个月周次', 'Month 1 weeks'))}">{''.join(f'<a href="#canon-week-{week}">{_escape(ui_text(locale, f"第 {week} 周", f"Week {week}"))}</a>' for week in range(1, 5))}</nav>{''.join(week_sections)}</section>'''
    script = f'''
const monthOneStories = {payload_json};
const canonFilter = document.getElementById('canon-status-filter');
canonFilter?.addEventListener('change', () => {{
  document.querySelectorAll('.canon-story').forEach(story => {{
    story.hidden = canonFilter.value !== 'ALL' && story.dataset.canonStatus !== canonFilter.value;
  }});
  document.querySelectorAll('.canon-week').forEach(week => {{
    week.hidden = !week.querySelector('.canon-story:not([hidden])');
  }});
}});
document.querySelectorAll('[data-canon-select]').forEach(button => {{
  button.addEventListener('click', () => {{
    if ({json.dumps(creator_story_bank)}) {{
      const target = new URL({json.dumps(ui_url('/creator/create', locale))}, window.location.origin);
      target.searchParams.set('canon_story_id', button.dataset.canonSelect);
      target.searchParams.set('canon_format', button.dataset.canonFormat);
      window.location.href = target.toString();
      return;
    }}
    const story = monthOneStories[button.dataset.canonSelect];
    if (!story) return;
    const form = document.getElementById('content-form');
    form.elements.topic.value = story.title;
    form.elements.audience.value = {json.dumps(ui_text(locale, '正在用 AI 构建真实产品的工程师和独立开发者', 'AI engineers and solo builders shipping real products'))};
    form.elements.source_label.value = 'month-one-canon:' + button.dataset.canonSelect;
    form.elements.hypotheses.value = story.thesis;
    form.elements.planned.value = button.dataset.canonFormat === 'video'
      ? {json.dumps(ui_text(locale, '补齐证据后制作 60–90 秒技术视频。', 'Produce a 60–90 second technical video after evidence review.'))}
      : {json.dumps(ui_text(locale, '补齐证据后写成技术博客。', 'Write a technical blog after evidence review.'))};
    form.elements.call_to_action.value = {json.dumps(ui_text(locale, '分享你遇到过的类似工程取舍。', 'Share a similar engineering trade-off you have faced.'))};
    document.getElementById('content-topic').scrollIntoView({{behavior: 'smooth', block: 'center'}});
  }});
}});
'''
    return html_section, script


def content_page(
    *,
    data_root: Path,
    form: dict[str, str] | None = None,
    run_id: str | None = None,
    error: str | None = None,
    notice: str | None = None,
    locale: UILocale = DEFAULT_UI_LOCALE,
    creator_video_available: bool = True,
    repository_root: Path | None = None,
    scan_range: str | None = None,
    candidate_id: str | None = None,
    creator_video_phase: str | None = None,
    creator_video_error: str | None = None,
    workspace_view: Literal["all", "stories", "create"] = "all",
    canon_story_id: str | None = None,
    canon_format: str | None = None,
    creator_job: CreatorProductionJob | None = None,
) -> str:
    values = dict(form or {})
    if canon_story_id:
        selected_story = next(
            (
                story
                for story in load_month_one_canon().stories
                if story.story_id == canon_story_id
            ),
            None,
        )
        if selected_story is not None:
            values.update(
                {
                    "topic": selected_story.title_cn
                    if locale == "zh-CN"
                    else selected_story.working_title_en,
                    "audience": ui_text(
                        locale,
                        "正在用 AI 构建真实产品的工程师和独立开发者",
                        "AI engineers and solo builders shipping real products",
                    ),
                    "source_label": f"month-one-canon:{selected_story.story_id}",
                    "hypotheses": selected_story.one_sentence_thesis,
                    "planned": ui_text(
                        locale,
                        "补齐证据后制作 60–90 秒技术视频。",
                        "Produce a 60–90 second technical video after evidence review.",
                    )
                    if canon_format == "video"
                    else ui_text(
                        locale,
                        "补齐证据后写成技术博客。",
                        "Write a technical blog after evidence review.",
                    ),
                    "call_to_action": ui_text(
                        locale,
                        "分享你遇到过的类似工程取舍。",
                        "Share a similar engineering trade-off you have faced.",
                    ),
                }
            )
    scan: RecentWorkScan | None = None
    if scan_range in {ScanRange.TODAY.value, ScanRange.LAST_7_DAYS.value}:
        scan = scan_recent_work(
            data_root,
            scan_range,
            repository_root=repository_root,
        )
        selected = next(
            (
                candidate
                for candidate in scan.candidates
                if candidate.candidate_id == candidate_id
            ),
            None,
        )
        if selected is not None:
            values = selected.content_form(
                language="中文" if locale == "zh-CN" else "English"
            )
    run: ContentRun | None = None
    if run_id:
        try:
            run = load_content_run(data_root, run_id)
        except ContentWorkspaceError:
            error = "这份内容记录不可用，请重新生成。"
    if run is not None and not values:
        values = _form_from_run(run)
    recent = _recent_runs(data_root)
    recent_html = "".join(
        f'<a href="{ui_url("/content", locale, run_id=item)}">{_escape(item)}</a>'
        for item in recent
    )
    if not recent_html:
        recent_html = f"<span>{_escape(ui_text(locale, '生成后会显示在这里。', 'Generated drafts will appear here.'))}</span>"
    error_html = f'<div class="error" role="alert">{_escape(error)}</div>' if error else ""
    job_notice = ""
    if creator_job is not None:
        phase_copy = {
            "QUEUED": ui_text(locale, "已加入后台队列", "Queued in background"),
            "GENERATING_CONTENT": ui_text(locale, "正在生成内容", "Generating content"),
            "RENDERING_VIDEO": ui_text(locale, "正在后台渲染视频", "Rendering video in background"),
            "READY": ui_text(locale, "成品已就绪", "Artifacts ready"),
            "AI_NOT_EXECUTED": "AI_NOT_EXECUTED",
            "FAILED": ui_text(locale, "制作失败，请检查具体错误", "Production failed; inspect the concrete error"),
        }
        target = (
            ui_url("/creator/publish", locale, run_id=creator_job.content_run_id or "")
            if creator_job.phase == "READY"
            else ui_url("/creator/create", locale, creator_job=creator_job.job_id)
        )
        refresh = (
            "<script>setTimeout(()=>location.reload(),1200)</script>"
            if creator_job.phase in {"QUEUED", "GENERATING_CONTENT", "RENDERING_VIDEO"}
            else ""
        )
        job_detail = _creator_job_state_detail(creator_job, locale)
        job_notice = f'''<div class="notice creator-production-status" role="status" data-phase="{creator_job.phase}"><strong>{_escape(phase_copy[creator_job.phase])}</strong><span>{_escape(creator_job.job_id)}</span><span class="job-state">{job_detail}</span>{f'<a href="{target}">{_escape(ui_text(locale, "打开发布队列", "Open Publish Queue"))} →</a>' if creator_job.phase == 'READY' else ''}</div>{refresh}'''
    notice_html = (
        f'<div class="notice" role="status">{_escape(notice)}</div>' if notice else ""
    )
    result_html = (
        _result_html(
            run,
            data_root=data_root,
            video_ready=creator_video_ready(data_root, run.run_id),
            creator_video_available=creator_video_available,
            creator_video_phase=creator_video_phase,
            creator_video_error=creator_video_error,
            locale=locale,
        )
        if run is not None
        else f"""<section class="empty">
      <span class="kicker">{_escape(ui_text(locale, '从这里开始', 'Start here'))}</span>
      <h2>{_escape(ui_text(locale, '从一条你能确认的事实开始', 'Start with one fact you can stand behind'))}</h2>
      <p>{_escape(ui_text(locale, 'SoloScale 会把它整理成不同渠道的草稿；不会连接账号，也不会自动发布。', 'SoloScale turns it into channel-ready drafts. No account is connected and nothing is published automatically.'))}</p>
      <div class="channel-pills"><span>LinkedIn</span><span>X</span><span>{_escape(ui_text(locale, '短视频', 'Short video'))}</span></div>
      <ol class="empty-steps"><li><span class="step-number">1</span>{_escape(ui_text(locale, '添加已验证事实', 'Add verified facts'))}</li><li><span class="step-number">2</span>{_escape(ui_text(locale, '生成多渠道草稿', 'Generate channel drafts'))}</li><li><span class="step-number">3</span>{_escape(ui_text(locale, '发布前由你复核', 'Review before publishing'))}</li></ol>
      <a class="secondary-button" href="#content-topic">{_escape(ui_text(locale, '填写第一个主题', 'Add your first topic'))}</a>
    </section>"""
    )
    language = values.get("language", "中文" if locale == "zh-CN" else "English")
    generation_mode = values.get(
        "generation_mode", ModelProviderId.SOLOSCALE_HOSTED.value
    )
    if generation_mode not in {
        ModelProviderId.SOLOSCALE_HOSTED.value,
        ModelProviderId.OLLAMA.value,
        ModelProviderId.OPENAI_COMPATIBLE.value,
        "template",
    }:
        generation_mode = ModelProviderId.SOLOSCALE_HOSTED.value
    provider_model = values.get("provider_model", "qwen3:8b")
    provider_copy = {
        ModelProviderId.SOLOSCALE_HOSTED.value: (
            ui_text(locale, "SoloScale 托管 AI · 推荐", "SoloScale Hosted AI · Recommended"),
            ui_text(
                locale,
                "无需安装本地模型。服务不可用时会明确停止，不会静默改用其他引擎。",
                "No local model setup is required. If the service is unavailable, generation stops clearly and never silently switches engines.",
            ),
        ),
        ModelProviderId.OLLAMA.value: (
            ui_text(locale, "本地 / 自定义模型 · 高级", "Local / custom model · Advanced"),
            ui_text(
                locale,
                "本次草稿会使用你在高级设置中选择的本机模型。连接失败不会影响其他功能。",
                "This draft will use the local model selected in Advanced settings. A connection failure does not affect other features.",
            ),
        ),
        ModelProviderId.OPENAI_COMPATIBLE.value: (
            "OpenAI API",
            ui_text(
                locale,
                "使用你在 AI 服务设置中选择的 OpenAI 模型；连接失败时会停止，不会静默回退。",
                "Uses the OpenAI model selected in AI Service settings. A connection failure stops the run without a silent fallback.",
            ),
        ),
        "template": (
            ui_text(locale, "安全离线草稿 · 不使用 AI", "Safe offline draft · No AI"),
            ui_text(
                locale,
                "使用确定性模板生成基础草稿；不调用模型或网络。",
                "Creates a basic draft with a deterministic template and no model or network call.",
            ),
        ),
    }
    provider_label, provider_note = provider_copy[generation_mode]
    work_summary = render_use_my_work(
        load_work_context(data_root, workspace_root=repository_root),
        locale,
        boundary=ui_text(
            locale,
            "已有资料可以减少重复说明；本次公开内容仍只会使用你在此页明确确认的事实和来源。",
            "Existing work can reduce repeated setup, but this public draft still uses only the facts and sources you explicitly confirm on this page.",
        ),
    )
    scan_section = _scan_html(
        scan, selected_candidate_id=candidate_id, locale=locale
    )
    canon_section, canon_script = _month_one_canon_html(
        locale, creator_story_bank=workspace_view == "stories", data_root=data_root
    )
    story_mining_section = (
        _story_mining_html(data_root, locale, notice=notice)
        if workspace_view == "stories"
        else ""
    )
    production_lifecycle = (
        _creator_production_jobs_html(data_root, locale)
        if workspace_view in {"stories", "create"}
        else ""
    )
    reference_videos = recent_reference_videos(data_root)
    selected_reference_id = values.get("reference_id", "")
    reference_options = "".join(
        f'<option value="{_escape(item.asset.reference_id)}" {"selected" if item.asset.reference_id == selected_reference_id else ""}>{_escape(item.asset.title or item.asset.source_filename or item.asset.reference_id)} · {item.asset.duration_seconds:.1f}s · {_escape(item.pattern.video.shot_cadence)}</option>'
        for item in reference_videos
    )
    reference_upload = f'''<section class="reference-video-upload"><div class="result-head"><div><span class="kicker">Reference Video Intelligence · Local</span>
      <h3>{_escape(ui_text(locale, '分析本地参考视频', 'Analyze a local reference video'))}</h3>
      <p class="hint">{_escape(ui_text(locale, '只读取你主动选择的 MP4；本地提取转录、关键帧、镜头节奏和画面结构。原视频与转录不会进入公开内容。', 'Only the MP4 you choose is read. Transcript, keyframes, shot timing, and visual structure are analyzed locally. Raw media and transcript never enter public output.'))}</p></div><span class="reference-badge">{len(reference_videos)} {_escape(ui_text(locale, '个本地参考', 'local references'))}</span></div>
      <form method="post" action="/content/reference-video" enctype="multipart/form-data" class="reference-video-form">
        <input type="hidden" name="ui_locale" value="{locale}" />
        <div class="two"><label>{_escape(ui_text(locale, '参考标题（可选）', 'Reference title (optional)'))}<input name="reference_title" maxlength="180" /></label>
        <label>{_escape(ui_text(locale, '作者（可选）', 'Author (optional)'))}<input name="reference_author" maxlength="120" /></label></div>
        <label>{_escape(ui_text(locale, '选择 MP4（最多 200 MB）', 'Choose an MP4 (up to 200 MB)'))}<input type="file" name="reference_video" accept="video/mp4,.mp4" required /></label>
        <button class="secondary" type="submit">{_escape(ui_text(locale, '本地分析并加入参考库', 'Analyze locally and add to library'))}</button>
      </form></section>'''
    body = f"""{production_lifecycle}{job_notice}{work_summary}{story_mining_section}{canon_section}{scan_section}{reference_upload}<div class="grid">
<section class="form-card">
<span class="kicker">{_escape(ui_text(locale, '输入', 'Input'))}</span><h2>{_escape(ui_text(locale, '证据 + 受众 + CTA', 'Evidence + audience + CTA'))}</h2>
<p class="hint">{_escape(ui_text(locale, '第一条已验证事实会成为开头。数字、结果和结论都应附证据。', 'The first verified fact becomes the opening. Numbers, outcomes, and conclusions should include evidence.'))}</p>
{error_html}
{notice_html}
<form id="content-form" method="post" action="{'/creator/production/start' if workspace_view == 'create' else '/content/generate'}">
<input type="hidden" name="ui_locale" value="{locale}" />
{'<input type="hidden" name="source_kind" value="CREATE" />' if workspace_view == 'create' else ''}
{f'<input type="hidden" name="creator_view" value="{workspace_view}" />' if workspace_view != 'all' else ''}
<input type="hidden" name="provider_model" value="{_escape(provider_model)}" />
<label>{_escape(ui_text(locale, '主题', 'Topic'))}
  <input id="content-topic" name="topic" maxlength="180" required
    value="{_escape(values.get("topic", ""))}"
    placeholder="例如：Why green tests were not enough to publish" />
</label>
<div class="two">
  <label>{_escape(ui_text(locale, '受众', 'Audience'))}
    <input name="audience" maxlength="500" required
      value="{_escape(values.get("audience", "AI engineers and solo builders"))}" />
  </label>
  <label>{_escape(ui_text(locale, '内容输出语言', 'Draft language'))}
    <select name="language">
      <option {"selected" if language == "English" else ""}>English</option>
      <option {"selected" if language == "中文" else ""}>中文</option>
    </select>
  </label>
</div>
<section class="provider-summary" aria-labelledby="generation-engine-label">
  <span class="kicker" id="generation-engine-label">{_escape(ui_text(locale, 'AI 服务', 'AI service'))}</span>
  <strong>{_escape(provider_label)}</strong>
  <p class="hint">{_escape(provider_note)}</p>
  <a href="{ui_url('/settings/ai', locale)}">{_escape(ui_text(locale, '更换 AI 服务', 'Change AI service'))}</a>
</section>
<section class="reference-intake">
  <div class="result-head"><div><span class="kicker">Reference Intelligence · {_escape(ui_text(locale, '可选', 'Optional'))}</span>
    <h3>{_escape(ui_text(locale, '把参考内容蒸馏成表达模式', 'Distill a reference into a presentation pattern'))}</h3>
    <p class="hint">{_escape(ui_text(locale, '选择已分析视频，或粘贴文案 / transcript。SoloScale 只学习高层结构、节奏和视觉提示；事实、例子和独特措辞不会进入你的内容。', 'Choose an analyzed video, or paste copy / a transcript. SoloScale learns only high-level structure, pacing, and visual cues; reference facts, examples, and distinctive wording are excluded.'))}</p></div>
    <span class="reference-badge">{_escape(ui_text(locale, '本地分析', 'Local analysis'))}</span></div>
  <label>{_escape(ui_text(locale, '已分析的本地参考视频（可选）', 'Analyzed local reference video (optional)'))}
    <select name="reference_id"><option value="">{_escape(ui_text(locale, '不使用视频参考', 'No video reference'))}</option>{reference_options}</select>
  </label>
  <div class="two">
    <label>{_escape(ui_text(locale, '参考标题（可选）', 'Reference title (optional)'))}
      <input name="reference_title" maxlength="180" value="{_escape(values.get('reference_title', ''))}" />
    </label>
    <label>{_escape(ui_text(locale, '作者（可选）', 'Author (optional)'))}
      <input name="reference_author" maxlength="120" value="{_escape(values.get('reference_author', ''))}" />
    </label>
  </div>
  <label>{_escape(ui_text(locale, '粘贴 Reference 文本 / Transcript', 'Paste reference text / transcript'))}
    <textarea class="large" name="reference_text" maxlength="20000"
      placeholder="{_escape(ui_text(locale, '粘贴外部文章、帖子或视频转录稿；也可以在上方选择本地 MP4。', 'Paste an external article, post, or transcript; or select a local MP4 above.'))}"
    >{_escape(values.get('reference_text', ''))}</textarea>
  </label>
  <label>{_escape(ui_text(locale, '视觉备注（可选）', 'Visual notes (optional)'))}
    <input name="reference_visual_notes" maxlength="500"
      value="{_escape(values.get('reference_visual_notes', ''))}"
      placeholder="talking head, screen recording, large captions" />
  </label>
  <div class="boundary">{_escape(ui_text(locale, '原始 Reference 只私有保存，不会进入模型提示、公开草稿或事实证据链。', 'The raw reference stays private and is never placed in the model prompt, public drafts, or factual evidence chain.'))}</div>
</section>
<label>{_escape(ui_text(locale, '来源 / 项目链接', 'Source / project link'))}
  <input name="source_label" maxlength="500" required
    value="{_escape(values.get("source_label", ""))}"
    placeholder="{_escape(ui_text(locale, 'GitHub PR、公开文档或证据包标识', 'GitHub PR, public document, or evidence-bundle ID'))}" />
</label>
<label>{_escape(ui_text(locale, '已验证事实', 'Verified facts'))}
  <span class="hint">{_escape(ui_text(locale, '每行：事实 | 证据链接 | 这条证据不能证明什么（可选）', 'Each line: fact | evidence link | what it does not prove (optional)'))}</span>
  <textarea class="large" name="verified_claims" required
    placeholder="{_escape(ui_text(locale, 'Python CI 检查通过。| https://github.com/... | 仅代表本地运行。', 'Python CI checks passed. | https://github.com/... | Local run only.'))}"
  >{_escape(values.get("verified_claims", ""))}</textarea>
</label>
<label>{_escape(ui_text(locale, '个人观察（可选）', 'Personal observations (optional)'))}
  <span class="hint">{_escape(ui_text(locale, '每行：观察 | 对应记录或链接 | 边界（可选）', 'Each line: observation | record or link | boundary (optional)'))}</span>
  <textarea name="observed_claims"
  >{_escape(values.get("observed_claims", ""))}</textarea>
</label>
<label>{_escape(ui_text(locale, '待验证假设（可选）', 'Hypotheses (optional)'))}
  <span class="hint">{_escape(ui_text(locale, '每行一条，不会被写成已验证结论。', 'One per line. These will not be presented as verified conclusions.'))}</span>
  <textarea name="hypotheses">{_escape(values.get("hypotheses", ""))}</textarea>
</label>
<label>{_escape(ui_text(locale, '下一步（可选）', 'Next steps (optional)'))}
  <span class="hint">{_escape(ui_text(locale, '每行一条，会使用未来时态和 PLANNED 标签。', 'One per line, written in future tense and labeled PLANNED.'))}</span>
  <textarea name="planned">{_escape(values.get("planned", ""))}</textarea>
</label>
<label>{_escape(ui_text(locale, '行动引导', 'CTA'))}
  <input name="call_to_action" maxlength="220" required
    value="{_escape(values.get("call_to_action", ui_text(locale, "关注下一次经过测量的迭代。", "Follow the next measured iteration.")))}" />
</label>
<div class="boundary">{_escape(ui_text(locale, '草稿会先私有保存并等待你复核；SoloScale 不会自动发布。', 'Drafts stay private until you review them; SoloScale never publishes automatically.'))}</div>
<div class="generate-actions">
  <input type="hidden" name="generation_mode" value="{_escape(generation_mode)}" />
  {f'''<button id="generate-content" class="primary" type="submit" name="production_action" value="article">{_escape(ui_text(locale, '生成文章', 'Generate article'))}</button>
  <button class="secondary" type="submit" name="production_action" value="video">{_escape(ui_text(locale, '生成视频', 'Generate video'))}</button>
  <button class="secondary" type="submit" name="production_action" value="queue">{_escape(ui_text(locale, '生成并加入队列', 'Generate and queue'))}</button>''' if workspace_view == 'create' else f'''<button id="generate-content" class="primary" type="submit">{_escape(ui_text(locale, '使用当前 AI 服务生成', 'Generate with the selected AI service') if generation_mode != 'template' else ui_text(locale, '生成安全离线草稿', 'Generate safe offline draft'))}</button>'''}
</div>
</form>
<div class="recent"><strong>{_escape(ui_text(locale, '最近内容：', 'Recent drafts:'))}</strong>{recent_html}</div>
</section>
{result_html}
</div>"""
    view_css = ""
    current_url = "/content"
    heading = ui_text(
        locale,
        "把已验证的工作，整理成可以安心发布的内容。",
        "Turn verified work into content you can publish with confidence.",
    )
    description = ui_text(
        locale,
        "一次输入得到 LinkedIn、X 和短视频草稿；先私有保存、先预览，再由你决定是否发布。",
        "Create LinkedIn, X, and short-video drafts from one input. Save and preview privately before you decide whether to publish.",
    )
    if workspace_view == "stories":
        current_url = "/creator/stories"
        heading = ui_text(locale, "从真实工程故事里，选择下一条内容。", "Choose the next story from real engineering work.")
        description = ui_text(locale, "首月的 24 条工程故事现在有自己的故事库。", "The 24 Month 1 Engineering Stories now live in their own Story Bank.")
        view_css = ".use-my-work,.scan-work,.scan-results,.reference-video-upload,.grid{display:none!important}.story-mining{margin-top:18px;padding:20px;border:1px solid var(--border);border-radius:18px;background:linear-gradient(145deg,#fff,#f8fbff)}.story-mining .result-head{align-items:start}.story-mining h2{margin:6px 0}.story-mining form{display:flex;gap:8px}.mined-grid{display:grid;gap:10px;margin-top:14px}.mined-candidate{display:grid;gap:7px;padding:14px;border:1px solid var(--border);border-radius:12px;background:#fff}.mined-candidate p{margin:0;color:var(--text-muted);font-size:13px}.mined-candidate small{color:var(--brand);font-weight:800}.mined-candidate form{display:flex;gap:8px;flex-wrap:wrap;align-items:end}"
    elif workspace_view == "create":
        current_url = "/creator/create"
        heading = ui_text(locale, "把一个真实故事，做成完整内容包。", "Turn one real story into a complete content package.")
        description = ui_text(locale, "生成、审核、视频与发布准备继续使用现有流程。", "Generation, review, video, and publishing preparation keep using the existing workflow.")
        view_css = ".month-one-canon{display:none!important}"
    current_url = ui_url(
        current_url,
        locale,
        run_id=run_id or "",
        canon_story_id=canon_story_id or "",
        canon_format=canon_format or "",
    )
    if workspace_view != "all":
        body = render_creator_nav(active=workspace_view, locale=locale) + body
    script = f"""
{canon_script}
document.querySelectorAll('.tab').forEach(button => button.addEventListener('click', () => {{
  document.querySelectorAll('.tab,.tab-panel').forEach(item => {{
    item.classList.remove('active');
  }});
  button.classList.add('active');
  const panel = document.querySelector('[data-panel="' + button.dataset.tab + '"]');
  panel.classList.add('active');
}}));
document.querySelectorAll('.copy').forEach(button => {{
  button.addEventListener('click', async () => {{
    const target = document.getElementById(button.dataset.copy);
    try {{
      await navigator.clipboard.writeText(target.innerText);
      button.textContent = {json.dumps(ui_text(locale, '已复制', 'Copied'))};
    }} catch {{
      button.textContent = {json.dumps(ui_text(locale, '请手动复制', 'Copy manually'))};
    }}
  }});
}});
if(document.querySelector('[data-video-job-active="true"]')){{
  window.setTimeout(() => window.location.reload(), 1800);
}}
"""
    return render_app_shell(
        active="content",
        locale=locale,
        current_url=current_url,
        title=f"SoloScale · {ui_text(locale, '建立专业影响力', 'Build visibility')}",
        eyebrow=ui_text(locale, "建立影响力", "Build visibility"),
        heading=heading,
        description=description,
        body=body,
        script=script,
        extra_css=view_css
        + """
.use-my-work{display:grid;grid-template-columns:minmax(260px,.75fr) 1fr auto;gap:16px;align-items:center;margin-bottom:18px;padding:15px 18px;border:1px solid #d9e2dc;border-radius:15px;background:linear-gradient(110deg,#fff,#f1f8f5)}.use-my-work>div{display:grid;gap:3px}.use-my-work strong{font-size:13px}.use-my-work p{margin:0;color:var(--text-muted);font-size:13px}.use-my-work a{font-weight:800;text-decoration:none;white-space:nowrap}
.scan-work,.scan-results{margin-bottom:22px;padding:22px;border:1px solid var(--border);border-radius:20px;background:linear-gradient(135deg,#fff,var(--brand-soft))}.scan-work{display:flex;justify-content:space-between;align-items:center;gap:20px}.scan-work h2,.scan-results h2{margin:7px 0}.scan-work p,.scan-results p{color:var(--text-muted)}.scan-actions{display:flex;gap:9px;align-items:center;flex-wrap:wrap}.scan-actions a{text-decoration:none;font-weight:800}.candidate-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:13px;margin-top:16px}.candidate-card{display:grid;gap:10px;padding:17px;border:1px solid var(--border);border-radius:16px;background:#fff}.candidate-card.selected{border-color:var(--brand);box-shadow:0 0 0 3px var(--brand-soft)}.candidate-card h3,.candidate-card p{margin:0}.candidate-card details{font-size:12px}.candidate-head{display:flex;justify-content:space-between;color:var(--brand);font-size:11px;font-weight:850;letter-spacing:.06em}.candidate-card .secondary-button{justify-self:start}.selected-candidate{justify-self:start;padding:7px 10px;border-radius:999px;background:var(--success-soft);color:var(--success);font-size:12px;font-weight:850}.audience-value{font-size:12px}
.grid{display:grid;grid-template-columns:minmax(340px,.8fr) minmax(0,1.2fr);gap:22px;align-items:start}.form-card h2,.result-panel h2,.empty h2{margin:7px 0 8px;letter-spacing:-.025em}.form-card form{gap:16px;margin-top:22px}.form-card textarea{min-height:92px}.form-card textarea.large{min-height:140px}.two{display:grid;grid-template-columns:1fr 1fr;gap:10px}.primary:disabled{opacity:.65}.boundary{font-size:12px}.provider-summary{border:1px solid var(--border);background:linear-gradient(135deg,var(--brand-soft),var(--success-soft));border-radius:15px;padding:15px;display:grid;gap:5px}.provider-summary strong{font-size:16px}.provider-summary p{margin:0}.provider-summary a{font-size:12px;font-weight:800;justify-self:start}.generate-actions{display:grid;gap:9px}.generate-actions .secondary{background:var(--surface-subtle);color:var(--brand);border:1px solid var(--border)}.engine-badge{display:inline-flex;margin-left:8px;padding:4px 9px;border-radius:999px;background:var(--success-soft);color:var(--success);font-size:12px;font-weight:800}
.reference-intake,.reference-card{padding:18px;border:1px solid #cfe1d8;border-radius:18px;background:linear-gradient(145deg,#fbfffd,#eef8f3)}.reference-intake{display:grid;gap:14px}.reference-intake h3,.reference-card h3{margin:5px 0}.reference-intake p,.reference-card p{margin:4px 0}.reference-badge{display:inline-flex;align-self:flex-start;padding:6px 10px;border-radius:999px;background:#e4f3eb;color:#286b4d;font-size:11px;font-weight:850;white-space:nowrap}.reference-card{margin:18px 0}.reference-pattern-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:15px 0}.reference-pattern-grid>div{padding:12px;border:1px solid #d8e8df;border-radius:13px;background:#fff}.reference-pattern-grid strong{font-size:12px;color:var(--brand)}.reference-card details{font-size:13px}.reference-card summary{cursor:pointer;font-weight:800}.reference-boundary{margin-top:12px!important;padding:10px;border-radius:10px;background:var(--brand-soft);font-size:12px}
.channel-pills{display:flex;justify-content:center;gap:8px;flex-wrap:wrap;margin:18px 0 4px}.channel-pills span{padding:5px 10px;border-radius:999px;background:var(--brand-soft);color:var(--brand);font-size:12px;font-weight:800}.empty .secondary-button{align-self:center}.result-head{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}.downloads{display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end}.downloads a,.text-link{font-size:12px;font-weight:750;text-decoration:none;padding:8px 10px;border:1px solid var(--border);border-radius:9px}.tabs{display:flex;flex-wrap:wrap;gap:7px;margin:22px 0 14px;padding:5px;background:var(--surface-subtle);border-radius:12px}.tab{flex:1 1 110px;background:transparent;color:var(--text-muted)}.tab.active{background:white;color:var(--brand);box-shadow:0 1px 4px #17203314}.tab-panel{display:none}.tab-panel.active{display:block}.panel-title{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}.panel-title h3{margin:0}.copy{background:var(--brand-soft);color:var(--brand)}
.result-panel pre{max-height:650px;overflow:auto;font:13px/1.65 ui-monospace,SFMono-Regular,Menlo,monospace}.x-thread,.storyboard{display:grid;gap:10px}.x-post,.scene{border:1px solid var(--border);border-radius:14px;padding:14px}.x-post span,.scene span{color:var(--brand);font-size:10px;font-weight:850;letter-spacing:.1em}.x-post p,.scene p{white-space:pre-wrap;line-height:1.5;margin:8px 0}.scene{display:grid;gap:6px}.scene small{color:var(--text-muted)}.review-note{margin:18px 0 0;background:var(--warning-soft);color:var(--warning)}.editorial-trace{margin-top:12px;padding:12px;border:1px solid var(--border);border-radius:12px;color:var(--text-muted);font-size:12px}.editorial-trace summary{color:var(--text);font-weight:800;cursor:pointer}.recent{margin-top:18px;display:flex;gap:9px;flex-wrap:wrap;color:var(--text-muted);font-size:12px}.recent a{text-decoration:none}
.unified-review{margin-top:24px;padding:18px;border:1px solid var(--border);border-radius:18px;background:var(--surface-subtle)}.unified-review form{display:grid;gap:14px}.review-editor{padding:13px;border:1px solid var(--border);border-radius:14px;background:white}.review-editor textarea{width:100%;min-height:180px}.review-status{padding:6px 10px;border-radius:999px;background:var(--warning-soft);color:var(--warning);font-size:12px;font-weight:850}.review-actions{display:flex;gap:9px;flex-wrap:wrap}.danger{background:#fff1f2;color:#a11b35;border:1px solid #fecdd3}.buildlog-handoff.locked{opacity:.8}.video-job{display:grid;gap:12px;padding:14px;border:1px solid var(--border);border-radius:14px;background:var(--brand-soft)}.video-job p{margin:0;color:var(--text-muted)}.progress-track{height:8px;overflow:hidden;border-radius:999px;background:#fff}.progress-track span{display:block;width:45%;height:100%;border-radius:999px;background:var(--brand);animation:video-progress 1.4s ease-in-out infinite alternate}@keyframes video-progress{from{transform:translateX(-20%)}to{transform:translateX(140%)}}
.media-cost-panel,.media-quality-review,.distribution-package{margin-top:18px;padding:18px;border:1px solid var(--border);border-radius:18px;background:linear-gradient(145deg,#fff,var(--brand-soft))}.media-cost-panel h3,.media-quality-review h2{margin:6px 0}.media-quality-form{display:grid;gap:14px}.quality-checklist{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.quality-checklist label{display:flex;align-items:flex-start;gap:8px;padding:11px;border:1px solid var(--border);border-radius:12px;background:#fff}.quality-checklist input{width:auto;margin-top:2px}.media-quality-form textarea{min-height:82px}.media-quality-form button{width:auto;justify-self:start}.distribution-package.locked,.media-cost-panel.locked{opacity:.82}
.month-one-canon{margin-bottom:22px;padding:22px;border:1px solid var(--border);border-radius:20px;background:linear-gradient(145deg,#fff,#f4f8ff)}.month-one-canon h2{margin:7px 0}.month-one-canon p{color:var(--text-muted)}.canon-filter{min-width:220px}.canon-week-nav{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0}.canon-week-nav a{padding:7px 11px;border-radius:999px;background:var(--brand-soft);font-size:12px;font-weight:800;text-decoration:none}.canon-week{margin-top:22px}.canon-week-title{display:flex;align-items:baseline;gap:10px}.canon-week-title span{color:var(--brand);font-size:11px;font-weight:900;letter-spacing:.1em}.canon-week-title h3{margin:0}.canon-stories{display:grid;gap:10px;margin-top:11px}.canon-story{border:1px solid var(--border);border-radius:14px;background:#fff}.canon-story>summary{display:grid;grid-template-columns:auto 1fr auto;gap:12px;align-items:center;padding:14px;cursor:pointer;list-style:none}.canon-story>summary::-webkit-details-marker{display:none}.canon-story summary strong,.canon-story summary small{display:block}.canon-story summary small{margin-top:3px;color:var(--text-muted);font-size:11px}.canon-sequence{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;background:var(--brand-soft);color:var(--brand);font-weight:900}.canon-status{padding:5px 8px;border-radius:999px;background:var(--surface-subtle);font-size:10px;font-style:normal;font-weight:850}.canon-status.ready_for_production{background:var(--success-soft);color:var(--success)}.canon-status.needs_evidence,.canon-status.needs_user_input{background:var(--warning-soft);color:var(--warning)}.canon-story-body{padding:0 16px 16px}.canon-thesis{font-weight:700;color:var(--text)!important}.six-layers{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.six-layers>div,.canon-meta>div{padding:11px;border:1px solid var(--border);border-radius:11px;background:var(--surface-subtle)}.six-layers span{color:var(--brand);font-size:11px;font-weight:900}.six-layers p{margin:5px 0 0;font-size:12px}.canon-meta{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px;margin-top:9px}.canon-meta h4{margin:0 0 6px}.canon-meta ul{margin:0;padding-left:18px;color:var(--text-muted);font-size:12px}.canon-production{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px;color:var(--text-muted);font-size:12px}.canon-actions{display:flex;gap:8px;margin-top:12px}.canon-actions button{width:auto}.canon-actions .secondary-button{background:var(--surface-subtle);color:var(--brand)}
@media(max-width:900px){.use-my-work,.grid{grid-template-columns:1fr}.scan-work{align-items:flex-start;flex-direction:column}.result-head{display:block}.downloads{justify-content:flex-start;margin-top:12px}}@media(max-width:580px){.two,.reference-pattern-grid,.quality-checklist{grid-template-columns:1fr}}
.creator-production-lifecycle{margin:0 0 18px;padding:16px 18px;border:1px solid var(--border);border-radius:18px;background:linear-gradient(145deg,#fff,var(--brand-soft))}.creator-production-lifecycle .result-head{align-items:start}.creator-production-lifecycle h2{margin:6px 0}.creator-production-lifecycle p{margin:0;color:var(--text-muted)}.creator-production-jobs{display:grid;gap:9px;margin-top:14px}.creator-production-job{display:grid;grid-template-columns:auto 1fr auto;gap:14px;align-items:center;padding:12px 14px;border:1px solid var(--border);border-radius:13px;background:#fff}.creator-production-job .status-badge{white-space:nowrap}.creator-production-job strong{display:block;font-size:14px}.creator-production-job small{display:block;color:var(--text-muted);font-size:12px}.creator-production-job .text-link{white-space:nowrap}
""",
    )
