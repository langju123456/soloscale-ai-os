# ruff: noqa: E501
"""Metadata-only Evidence Center rendering and explicit local refresh helpers."""

from __future__ import annotations

import html
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from soloscale.evidence_hub import EvidenceHub, EvidenceHubError, inspect_git_repository
from soloscale.evidence_hub_models import EvidenceHubStatus, ReceiptStatus, SyncReceipt
from soloscale.knowledge_store import KnowledgeStore
from soloscale.ui_shell import (
    DEFAULT_UI_LOCALE,
    UILocale,
    render_app_shell,
    ui_display_value,
    ui_text,
)


def refresh_evidence_catalog(
    data_root: Path,
    *,
    repository_root: Path | None,
    buildlog_roots: Sequence[Path] = (),
) -> SyncReceipt:
    """Explicitly build the evidence catalog without models, network, or publishing."""

    root = Path(data_root)
    knowledge_path = root / "knowledge" / "index.sqlite3"
    knowledge_store = KnowledgeStore(root) if knowledge_path.is_file() else None
    selected_buildlog_roots = list(buildlog_roots)
    discovered_buildlog = root / "publishing"
    if discovered_buildlog.is_dir() and discovered_buildlog not in selected_buildlog_roots:
        selected_buildlog_roots.append(discovered_buildlog)
    return EvidenceHub(root).refresh(
        knowledge_store=knowledge_store,
        buildlog_roots=selected_buildlog_roots,
        git_root=repository_root,
    )


def refresh_local_project_evidence(
    data_root: Path,
    *,
    repository_root: Path,
) -> SyncReceipt:
    """Incrementally refresh only the Git project explicitly selected by the user."""

    return EvidenceHub(Path(data_root)).sync_git_repository(repository_root)


def ensure_local_project_evidence(
    data_root: Path,
    *,
    repository_root: Path,
    timing: Callable[[str, int], None] | None = None,
) -> bool:
    """Refresh a selected project only when its deterministic fingerprint changed."""

    root = Path(data_root)
    freshness_started = time.perf_counter()
    current_source, current_items = inspect_git_repository(repository_root)
    if timing is not None:
        timing(
            "freshness_check_ms",
            int((time.perf_counter() - freshness_started) * 1000),
        )
    if EvidenceHub.catalog_exists(root):
        stored = EvidenceHub(root).git_repository_snapshot(repository_root)
        if stored is not None and stored[0].content_sha256 == current_source.content_sha256:
            if timing is not None:
                timing("local_git_refresh_ms", 0)
            return False
    refresh_started = time.perf_counter()
    receipt = EvidenceHub(root).sync_source(current_source, items=current_items)
    if timing is not None:
        timing(
            "local_git_refresh_ms",
            int((time.perf_counter() - refresh_started) * 1000),
        )
    if receipt.status is not ReceiptStatus.SUCCEEDED:
        raise EvidenceHubError("local project evidence refresh failed")
    return True


def evidence_page(
    data_root: Path, locale: UILocale = DEFAULT_UI_LOCALE
) -> str:
    """Render a catalog view without creating or exposing private catalog contents."""

    root = Path(data_root)
    exists = EvidenceHub.catalog_exists(root)
    hub = EvidenceHub(root) if exists else None
    status = hub.status() if hub else None
    receipts = hub.recent_receipts(limit=5) if hub else []
    assets = hub.recent_assets(limit=5) if hub else []
    outcomes = hub.recent_outcomes(limit=5) if hub else []
    receipt_items = [
        f"{receipt.receipt_id} · {receipt.status} · {receipt.adapter} · "
        f"created {receipt.created_count} · updated {receipt.updated_count} · "
        f"unchanged {receipt.unchanged_count} · errors {receipt.error_count}"
        for receipt in receipts
    ]
    asset_items = [f"{asset.asset_id} · {asset.asset_type} · {asset.approval}" for asset in assets]
    outcome_items = [
        f"{outcome.outcome_id} · {outcome.outcome_type} · {outcome.platform} · {outcome.status}"
        for outcome in outcomes
    ]
    empty_note = (
        f'<div class="notice">{html.escape(ui_text(locale, "你的私有证据库还没有建立。点击刷新后，只会显示安全的元数据统计，不会显示原文或路径。", "Your private evidence library has not been initialized. Refreshing exposes only safe metadata counts—not source bodies or paths."))}</div>'
        if not exists
        else ""
    )
    body = f"""<section class="panel evidence-intro">
<span class="status-badge">{html.escape(ui_text(locale, '仅本机元数据', 'Local metadata only'))}</span>
<h2>{html.escape(ui_text(locale, '刷新你的证据目录', 'Refresh your evidence catalog'))}</h2>
<p>{html.escape(ui_text(locale, '此页面只展示私有目录的计数与状态，不显示来源正文、locator、绝对路径或凭据。', 'This page displays private catalog counts and status only. It never renders source bodies, locators, absolute paths, or credentials.'))}</p>
{empty_note}
<form method="post" action="/evidence/refresh"><input type="hidden" name="ui_locale" value="{locale}" />
<button type="submit">{html.escape(ui_text(locale, '刷新证据目录', 'Refresh evidence catalog'))}</button></form>
</section>
{_status_html(exists, status, locale=locale)}
<div class="evidence-grid">
<section class="panel"><h2>{html.escape(ui_text(locale, '最近刷新回执', 'Recent refresh receipts'))}</h2>{_list_html(receipt_items, locale=locale)}</section>
<section class="panel"><h2>{html.escape(ui_text(locale, '最近资产', 'Recent assets'))}</h2>{_list_html(asset_items, locale=locale)}</section>
<section class="panel"><h2>{html.escape(ui_text(locale, '最近结果', 'Recent outcomes'))}</h2>{_list_html(outcome_items, locale=locale)}</section>
</div>"""
    return render_app_shell(
        active="evidence",
        locale=locale,
        current_url="/evidence",
        title=f"SoloScale · {ui_text(locale, '证据库', 'Evidence Center')}",
        eyebrow=ui_text(locale, "证据库", "Evidence center"),
        heading=ui_text(locale, "让真实工作留下可以复用的证据。", "Keep real work available as reusable evidence."),
        description=ui_text(locale, "按需刷新，不常驻扫描；原始私密内容不会出现在这里。", "Refresh when you need it—no background watcher, and no private source body appears here."),
        body=body,
        compact_hero=True,
        extra_css="""
#main-content{display:grid;gap:18px}.evidence-intro h2{margin:12px 0 8px}.evidence-intro form{margin-top:18px}.evidence-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}.evidence-grid .panel{min-width:0}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.metric{padding:14px;border:1px solid var(--border);border-radius:12px;background:var(--surface-subtle)}.panel p,.panel li{color:var(--text-muted)}
@media(max-width:900px){.evidence-grid{grid-template-columns:1fr}}@media(max-width:700px){.grid{grid-template-columns:1fr}}
""",
    )


def _status_html(
    exists: bool,
    status: EvidenceHubStatus | None,
    *,
    locale: UILocale = DEFAULT_UI_LOCALE,
) -> str:
    if not exists or status is None:
        return (
            f'<section class="panel"><h2>{html.escape(ui_text(locale, "目录状态", "Catalog status"))}</h2>'
            f'<p>{html.escape(ui_text(locale, "尚未初始化。", "Not initialized."))}</p>'
            f'<p><strong>{html.escape(ui_text(locale, "下一步：", "Next action:"))}</strong> '
            f'{html.escape(ui_text(locale, "刷新证据目录。", "Refresh evidence catalog."))}</p></section>'
        )
    counts = [
        (ui_text(locale, "来源", "Sources"), status.source_count),
        (ui_text(locale, "证据", "Evidence"), status.evidence_count),
        (ui_text(locale, "证据包", "Bundles"), status.bundle_count),
        (ui_text(locale, "案例", "Cases"), status.case_count),
        (ui_text(locale, "资产", "Assets"), status.asset_count),
        (ui_text(locale, "结果", "Outcomes"), status.outcome_count),
    ]
    truth_counts = "".join(
        f"<li>{html.escape(ui_display_value(locale, key))}: {value}</li>"
        for key, value in sorted(status.truth_class_counts.items())
    ) or f"<li>{html.escape(ui_text(locale, '无', 'None'))}</li>"
    source_counts = "".join(
        f"<li>{html.escape(ui_display_value(locale, key))}: {value}</li>"
        for key, value in sorted(status.source_counts.items())
    ) or f"<li>{html.escape(ui_text(locale, '无', 'None'))}</li>"
    last_refresh = (
        status.last_receipt.completed_at.isoformat()
        if status.last_receipt
        else ui_text(locale, "从未", "Never")
    )
    next_action = (
        ui_text(locale, "查看刷新失败原因，再次刷新证据目录。", "Review the failed refresh, then refresh evidence catalog.")
        if status.last_receipt and status.last_receipt.status.value == "failed"
        else ui_text(locale, "添加来源后刷新证据目录。", "Refresh evidence catalog after adding sources.")
        if status.source_count == 0
        else ui_text(locale, "创建证据包前先检查证据元数据。", "Review evidence metadata before creating a bundle.")
    )
    metrics = "".join(
        f"<div class=\"metric\"><small>{label}</small><br><strong>{value}</strong></div>"
        for label, value in counts
    )
    return f"""<section class=\"panel\"><h2>{html.escape(ui_text(locale, '目录状态', 'Catalog status'))}</h2>
<p>{html.escape(ui_text(locale, '上次刷新：', 'Last refresh:'))} {html.escape(last_refresh)}</p><div class=\"grid\">{metrics}</div>
<h3>{html.escape(ui_text(locale, '来源类型', 'Source types'))}</h3><ul>{source_counts}</ul>
<h3>{html.escape(ui_text(locale, '事实分类', 'Truth classes'))}</h3><ul>{truth_counts}</ul>
<p><strong>{html.escape(ui_text(locale, '下一步：', 'Next action:'))}</strong> {html.escape(next_action)}</p></section>"""


def _list_html(
    items: list[str], *, locale: UILocale = DEFAULT_UI_LOCALE
) -> str:
    if not items:
        return f"<p>{html.escape(ui_text(locale, '还没有记录。', 'Nothing recorded yet.'))}</p>"
    return "<ul>" + "".join(f"<li>{html.escape(item)}</li>" for item in items) + "</ul>"
