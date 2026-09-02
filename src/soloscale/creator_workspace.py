# ruff: noqa: E501
"""Creator workspace overview and history built from existing local artifacts."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from soloscale.content_distribution import (
    ContentDistributionError,
    recent_distribution_packages,
)
from soloscale.content_models import ContentReviewDecision, ContentRun
from soloscale.content_ui import _creator_job_state_detail
from soloscale.content_workspace import (
    ContentWorkspaceError,
    load_content_review,
    load_content_run,
)
from soloscale.creator_accounts import load_creator_accounts
from soloscale.creator_production import load_creator_production_jobs
from soloscale.media_cost import MediaCostError, load_cost_receipts
from soloscale.ui_shell import (
    UILocale,
    render_app_shell,
    render_creator_nav,
    ui_display_value,
    ui_text,
    ui_url,
)
from soloscale.youtube_publishing import YouTubePublishingError, load_youtube_accounts


@dataclass(frozen=True)
class CreatorRunSummary:
    run: ContentRun
    review_status: str
    distribution_ready: bool
    video_ready: bool


def _video_ready(run_dir: Path) -> bool:
    """Detect a completed Creator Video render from the canonical output contract."""

    canonical = (
        (run_dir / "21_creator_video_youtube.mp4").is_file()
        and (run_dir / "10_creator_video.mp4").is_file()
    )
    legacy = (
        (run_dir / "youtube-video.mp4").is_file()
        and (run_dir / "creator-video.mp4").is_file()
    )
    return canonical or legacy


def _recent_runs(data_root: Path, *, limit: int = 12) -> list[CreatorRunSummary]:
    root = data_root / "content-runs"
    if root.is_symlink() or not root.is_dir():
        return []
    try:
        distributed = {
            str(item.get("run_id")) for item in recent_distribution_packages(data_root, limit=100)
        }
    except ContentDistributionError:
        distributed = set()
    summaries: list[CreatorRunSummary] = []
    for candidate in sorted(root.iterdir(), key=lambda item: item.name, reverse=True):
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        try:
            run = load_content_run(data_root, candidate.name)
            review = load_content_review(data_root, run.run_id)
        except ContentWorkspaceError:
            continue
        decision = (
            review[0].decision.value
            if review is not None
            else ContentReviewDecision.DRAFT.value
        )
        summaries.append(
            CreatorRunSummary(
                run=run,
                review_status=decision,
                distribution_ready=run.run_id in distributed,
                video_ready=_video_ready(candidate),
            )
        )
        if len(summaries) >= limit:
            break
    return summaries


def _week_metrics(data_root: Path, runs: list[CreatorRunSummary]) -> tuple[int, int, Decimal]:
    cutoff = datetime.now(UTC) - timedelta(days=7)
    content_count = 0
    video_count = 0
    for item in runs:
        try:
            created = datetime.fromisoformat(item.run.created_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if created >= cutoff:
            content_count += 1
            video_count += int(item.video_ready)
    spend = Decimal(0)
    try:
        receipts = load_cost_receipts(data_root)
    except MediaCostError:
        receipts = []
    for receipt in receipts:
        if receipt.started_at >= cutoff:
            spend += receipt.actual_cost_usd or receipt.estimated_cost_usd or Decimal(0)
    return content_count, video_count, spend


def creator_overview_page(data_root: Path, *, locale: UILocale = "zh-CN") -> str:
    """Render one status-driven entry point for existing Creator capabilities."""
    accounts = load_creator_accounts(data_root)
    active_accounts = sum(account.status == "ACTIVE" for account in accounts)
    try:
        youtube_connected = bool(load_youtube_accounts(data_root))
    except YouTubePublishingError:
        youtube_connected = False
    if youtube_connected and not any(
        account.platform == "youtube" and account.status == "ACTIVE"
        for account in accounts
    ):
        active_accounts += 1
    account_attention = sum(account.status == "NEEDS_ATTENTION" for account in accounts)
    runs = _recent_runs(data_root)
    latest = runs[0] if runs else None
    ready = sum(item.distribution_ready for item in runs)
    draft_attention = sum(
        item.review_status != ContentReviewDecision.APPROVED.value for item in runs
    )
    content_count, video_count, spend = _week_metrics(data_root, runs)
    if latest is None:
        next_title = ui_text(locale, "还没有内容任务", "No content run yet")
        next_detail = ui_text(locale, "从故事库选择一个故事，或直接开始创作。", "Choose a story or start creating.")
        next_href = ui_url("/creator/stories", locale)
    else:
        next_title = latest.run.brief.topic
        next_detail = (
            ui_text(locale, "发布包已就绪", "Distribution package ready")
            if latest.distribution_ready
            else ui_text(locale, "继续审核、视频或发布准备", "Continue review, video, or publishing preparation")
        )
        next_href = ui_url("/creator/create", locale, run_id=latest.run.run_id)
    body = f'''{render_creator_nav(active="overview", locale=locale)}
<section class="creator-overview-grid">
  <a class="creator-status-card" href="{ui_url('/creator/accounts', locale)}"><span>{ui_text(locale, '账号', 'Accounts')}</span><strong>{active_accounts} / 7</strong><p>{ui_text(locale, '个账号入口可用', 'account entries active')}</p></a>
  <a class="creator-status-card wide" href="{next_href}"><span>{ui_text(locale, '下一条内容', 'Next content')}</span><strong>{html.escape(next_title)}</strong><p>{html.escape(next_detail)}</p></a>
  <a class="creator-status-card" href="{ui_url('/creator/publish', locale)}"><span>{ui_text(locale, '可发布', 'Ready to publish')}</span><strong>{ready}</strong><p>{ui_text(locale, '个统一发布包', 'distribution packages')}</p></a>
  <a class="creator-status-card attention" href="{ui_url('/creator/history', locale)}"><span>{ui_text(locale, '需处理', 'Needs attention')}</span><strong>{account_attention + draft_attention}</strong><p>{ui_text(locale, '账号或内容等待处理', 'accounts or drafts need action')}</p></a>
</section>
<section class="week-panel"><div><span>{ui_text(locale, '本周', 'This week')}</span><strong>{content_count}</strong><small>{ui_text(locale, '内容', 'content runs')}</small></div><div><span>&nbsp;</span><strong>{video_count}</strong><small>{ui_text(locale, '视频', 'videos')}</small></div><div><span>&nbsp;</span><strong>${spend:.3f}</strong><small>{ui_text(locale, 'API 成本', 'API Cost')}</small></div></section>
<section class="creator-quick-actions"><a class="primary-button" href="{ui_url('/creator/stories', locale)}">{ui_text(locale, '打开故事库', 'Open Story Bank')}</a><a class="secondary-button" href="{ui_url('/creator/create', locale)}">{ui_text(locale, '开始创作', 'Start creating')}</a></section>'''
    return render_app_shell(
        active="content",
        locale=locale,
        current_url="/creator",
        title=f"SoloScale · {ui_text(locale, '创作者工作区', 'Creator Workspace')}",
        eyebrow=ui_text(locale, "建立影响力", "Build visibility"),
        heading=ui_text(locale, "今天要把哪一项真实工作变成影响力？", "What real work will you turn into visibility today?"),
        description=ui_text(locale, "账号、故事、创作和发布准备，现在各归其位。", "Accounts, stories, creation, and publishing preparation now have clear homes."),
        body=body,
        compact_hero=True,
        extra_css="""
.creator-overview-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.creator-status-card{display:grid;gap:7px;padding:20px;border:1px solid var(--border);border-radius:18px;background:#fff;text-decoration:none;color:var(--text)}.creator-status-card.wide{grid-column:span 2}.creator-status-card>span{color:var(--brand);font-size:11px;font-weight:900;letter-spacing:.08em}.creator-status-card>strong{font-size:28px;line-height:1.12}.creator-status-card.wide>strong{font-size:20px}.creator-status-card p{margin:0;color:var(--text-muted)}.creator-status-card.attention{background:linear-gradient(145deg,#fff,var(--warning-soft))}.week-panel{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;margin-top:16px;overflow:hidden;border:1px solid var(--border);border-radius:18px;background:var(--border)}.week-panel>div{display:grid;gap:4px;padding:18px;background:#fff}.week-panel span,.week-panel small{color:var(--text-muted)}.week-panel strong{font-size:24px}.creator-quick-actions{display:flex;gap:10px;margin-top:18px}.creator-quick-actions a{text-decoration:none}@media(max-width:850px){.creator-overview-grid{grid-template-columns:1fr 1fr}.creator-status-card.wide{grid-column:span 2}}@media(max-width:560px){.creator-overview-grid,.week-panel{grid-template-columns:1fr}.creator-status-card.wide{grid-column:auto}}
""",
    )


def creator_history_page(data_root: Path, *, locale: UILocale = "zh-CN") -> str:
    """Render recent content/video state and locally recorded cost receipts."""
    runs = _recent_runs(data_root)
    jobs = load_creator_production_jobs(data_root)
    job_phase_copy = {
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
    job_cards = "".join(
        f'''<article class="history-job" data-phase="{job.phase}"><span class="status-badge">{html.escape(job_phase_copy.get(job.phase, job.phase))}</span><div><strong>{html.escape(" + ".join(output_copy.get(item, item) for item in job.request.outputs))}</strong><p>{html.escape(job.request.source_story_id or ui_text(locale, "自由创作", "Free create"))}{(" · " + html.escape(job.content_run_id)) if job.content_run_id else ""}{(" · " + html.escape(job.error_code)) if job.error_code else ""}{(" · " + _creator_job_state_detail(job, locale))}</p></div><a href="{ui_url('/creator/create', locale, creator_job=job.job_id) if job.phase in {'QUEUED', 'GENERATING_CONTENT', 'RENDERING_VIDEO'} else (ui_url('/creator/publish', locale, run_id=job.content_run_id or '') if job.phase == 'READY' else ui_url('/settings/ai', locale) if job.phase == 'AI_NOT_EXECUTED' else ui_url('/creator/stories', locale))}">{html.escape(ui_text(locale, "查看", "Open"))} →</a></article>'''
        for job in jobs
    )
    cards = "".join(
        f'''<article class="history-card"><div><span>{html.escape(ui_display_value(locale, item.review_status))}</span><h2>{html.escape(item.run.brief.topic)}</h2><p>{html.escape(item.run.run_id)}</p></div><div class="history-flags"><strong>{ui_text(locale, '视频已就绪', 'Video ready') if item.video_ready else ui_text(locale, '暂无视频', 'No video')}</strong><strong>{ui_text(locale, '发布包已就绪', 'Package ready') if item.distribution_ready else ui_text(locale, '发布包未就绪', 'Package not ready')}</strong></div><a href="{ui_url('/creator/create', locale, run_id=item.run.run_id)}">{ui_text(locale, '打开', 'Open')} →</a></article>'''
        for item in runs
    ) or f'<section class="empty"><h2>{ui_text(locale, "还没有创作历史", "No Creator history yet")}</h2></section>'
    try:
        receipts = load_cost_receipts(data_root)
    except MediaCostError:
        receipts = []
    total = sum(
        (receipt.actual_cost_usd or receipt.estimated_cost_usd or Decimal(0) for receipt in receipts),
        Decimal(0),
    )
    production_section = (
        f'<section class="production-history"><span class="kicker">ContentProject</span><h2>{ui_text(locale, "内容生产任务", "Content production jobs")}</h2><div class="history-jobs">{job_cards or ui_text(locale, "还没有后台生产任务。", "No background production jobs yet.")}</div></section>'
        if jobs
        else ""
    )
    body = f'''{render_creator_nav(active="history", locale=locale)}<section class="history-summary"><strong>{len(runs)}</strong><span>{ui_text(locale, '最近内容任务', 'recent content runs')}</span><strong>{len(jobs)}</strong><span>{ui_text(locale, '生产任务', 'production jobs')}</span><strong>${total:.3f}</strong><span>{ui_text(locale, '记录成本', 'recorded cost')}</span></section>{production_section}<section class="history-list">{cards}</section>'''
    return render_app_shell(
        active="content",
        locale=locale,
        current_url="/creator/history",
        title=f"SoloScale · {ui_text(locale, '创作历史与成本', 'Creator History / Cost')}",
        eyebrow=ui_text(locale, "创作者工作区", "Creator workspace"),
        heading=ui_text(locale, "找到过去的产物，也看清成本。", "Find past outputs and understand their cost."),
        description=ui_text(locale, "这里只汇总现有本地运行与成本回执，不新增 Analytics。", "This summarizes existing local runs and cost receipts; it adds no Analytics."),
        body=body,
        compact_hero=True,
        extra_css="""
.history-summary{display:grid;grid-template-columns:repeat(3,auto 1fr);gap:8px 12px;align-items:baseline;margin-bottom:18px;padding:18px;border:1px solid var(--border);border-radius:16px;background:var(--surface-subtle)}.history-summary strong{font-size:24px}.history-summary span{color:var(--text-muted)}.history-list{display:grid;gap:12px}.history-card{display:grid;grid-template-columns:1fr auto auto;gap:18px;align-items:center;padding:18px;border:1px solid var(--border);border-radius:16px;background:#fff}.history-card span,.history-flags strong{color:var(--brand);font-size:10px;font-weight:900}.history-card h2{margin:5px 0;font-size:18px}.history-card p{margin:0;color:var(--text-muted);font-size:11px}.history-flags{display:grid;gap:5px}.history-card>a{font-weight:800;text-decoration:none}.production-history{margin-bottom:18px;padding:18px;border:1px solid var(--border);border-radius:16px;background:var(--surface-subtle)}.production-history h2{margin:8px 0 14px}.history-jobs{display:grid;gap:10px}.history-job{display:grid;grid-template-columns:auto 1fr auto;gap:14px;align-items:center;padding:13px 14px;border:1px solid var(--border);border-radius:13px;background:#fff}.history-job strong{display:block;font-size:14px}.history-job p{margin:3px 0 0;color:var(--text-muted);font-size:12px}.history-job a{font-weight:800;text-decoration:none;white-space:nowrap}@media(max-width:700px){.history-summary{grid-template-columns:auto 1fr}.history-card,.history-job{grid-template-columns:1fr}.history-flags{display:flex;gap:8px}.history-job a{justify-self:start}}
""",
    )
