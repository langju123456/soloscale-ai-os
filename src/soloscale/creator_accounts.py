# ruff: noqa: E501
"""Local account directory for SoloScale Creator workflows."""

from __future__ import annotations

import html
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from soloscale.platform_accounts import (
    AuthorizationAttempt,
    Capability,
    ConnectedIdentity,
    PlatformAccountSnapshot,
    all_platform_snapshots,
    provider_label,
)
from soloscale.ui_shell import (
    UILocale,
    render_app_shell,
    render_creator_nav,
    ui_display_value,
    ui_text,
)
from soloscale.youtube_publishing import (
    YouTubeJobSnapshot,
)

CreatorPlatform = Literal[
    "douyin", "xiaohongshu", "youtube", "x", "linkedin", "github", "independent_site"
]
CreatorAccountStatus = Literal["NOT_CONFIGURED", "ACTIVE", "NEEDS_ATTENTION"]

PLATFORMS: tuple[CreatorPlatform, ...] = (
    "douyin", "xiaohongshu", "youtube", "x", "linkedin", "github", "independent_site"
)
STATUSES: tuple[CreatorAccountStatus, ...] = (
    "NOT_CONFIGURED", "ACTIVE", "NEEDS_ATTENTION"
)
LABELS: dict[CreatorPlatform, str] = {
    "douyin": "Douyin", "xiaohongshu": "Xiaohongshu", "youtube": "YouTube",
    "x": "X", "linkedin": "LinkedIn", "github": "GitHub",
    "independent_site": "Independent Site",
}


class CreatorAccountError(ValueError):
    """Raised when an account entry is invalid."""


@dataclass(frozen=True)
class CreatorAccount:
    platform: CreatorPlatform
    display_name: str = ""
    handle: str = ""
    profile_url: str = ""
    admin_url: str = ""
    status: CreatorAccountStatus = "NOT_CONFIGURED"


def _settings_path(data_root: Path) -> Path:
    return data_root.expanduser().absolute() / "settings" / "creator-accounts.json"


def _text(value: str, field: str, limit: int = 160) -> str:
    cleaned = value.strip()
    if len(cleaned) > limit or any(ord(character) < 32 for character in cleaned):
        raise CreatorAccountError(f"Invalid {field}")
    return cleaned


def _url(value: str, field: str) -> str:
    cleaned = _text(value, field, 2048)
    if not cleaned:
        return ""
    parsed = urlsplit(cleaned)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise CreatorAccountError(f"Invalid {field}")
    return cleaned


def normalize_account(
    *, platform: str, display_name: str = "", handle: str = "",
    profile_url: str = "", admin_url: str = "", status: str = "NOT_CONFIGURED",
) -> CreatorAccount:
    """Validate exactly the six fields accepted by Account Center v0.1."""
    if platform not in PLATFORMS:
        raise CreatorAccountError("Unsupported platform")
    if status not in STATUSES:
        raise CreatorAccountError("Unsupported status")
    return CreatorAccount(
        platform=platform,
        display_name=_text(display_name, "display_name"),
        handle=_text(handle, "handle"),
        profile_url=_url(profile_url, "profile_url"),
        admin_url=_url(admin_url, "admin_url"),
        status=status,
    )


def load_creator_accounts(data_root: Path) -> tuple[CreatorAccount, ...]:
    """Load the seven stable platform slots, filling any missing entries."""
    stored: dict[CreatorPlatform, CreatorAccount] = {}
    path = _settings_path(data_root)
    if path.is_file() and not path.is_symlink():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            entries = payload.get("accounts", []) if isinstance(payload, dict) else []
            for raw in entries:
                if isinstance(raw, dict):
                    account = normalize_account(**{key: str(raw.get(key, "")) for key in (
                        "platform", "display_name", "handle", "profile_url", "admin_url", "status"
                    )})
                    stored[account.platform] = account
        except (OSError, json.JSONDecodeError, CreatorAccountError, TypeError):
            stored = {}
    return tuple(stored.get(platform, CreatorAccount(platform)) for platform in PLATFORMS)


def save_creator_account(data_root: Path, account: CreatorAccount) -> Path:
    """Atomically update one local account entry with private permissions."""
    root = data_root.expanduser().absolute()
    settings = root / "settings"
    if root.is_symlink() or settings.is_symlink():
        raise CreatorAccountError("Account settings cannot use symlinked directories")
    settings.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    os.chmod(settings, 0o700)
    accounts = {item.platform: item for item in load_creator_accounts(root)}
    accounts[account.platform] = account
    payload = {"schema_version": "1.0", "accounts": [
        asdict(accounts[platform]) for platform in PLATFORMS
    ]}
    descriptor, temporary_name = tempfile.mkstemp(prefix=".creator-accounts-", dir=settings)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, _settings_path(root))
        os.chmod(_settings_path(root), 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return _settings_path(root)


def creator_accounts_page(
    data_root: Path,
    *,
    locale: UILocale = "zh-CN",
    notice: str | None = None,
    youtube_job: YouTubeJobSnapshot | None = None,
    auth_attempt: AuthorizationAttempt | None = None,
) -> str:
    """Render six provider-specific cards from one account/capability truth source."""
    notice_html = f'<p class="notice">{html.escape(notice)}</p>' if notice else ""
    snapshots = all_platform_snapshots(data_root)
    cards = [
        _provider_card(
            snapshot,
            locale=locale,
            youtube_job=youtube_job,
            auth_attempt=(
                auth_attempt if auth_attempt is not None and auth_attempt.platform == snapshot.platform else None
            ),
        )
        for snapshot in snapshots
    ]
    active_youtube = youtube_job is not None and youtube_job.phase in {
        "STARTING", "AUTHENTICATING", "WAITING_FOR_AUTHORIZATION", "COMPLETING"
    }
    refresh_script = (
        "<script>setTimeout(()=>location.reload(),1500)</script>" if active_youtube else ""
    )
    legacy_site = next(
        (item for item in load_creator_accounts(data_root) if item.platform == "independent_site"),
        CreatorAccount("independent_site"),
    )
    site_editor = _independent_site_editor(legacy_site, locale)
    body = f'''{render_creator_nav(active="accounts", locale=locale)}{notice_html}<section class="boundary"><strong>{ui_text(locale, "平台认证中心", "Platform Authentication Hub")}</strong><p>{ui_text(locale, "账号状态只来自真实凭证、平台身份和已授予能力；手动状态不再代表已连接。", "Connection status comes only from real credentials, verified identities, and granted capabilities; manual status is not connection truth.")}</p></section><section class="account-grid">{"".join(cards)}</section>{site_editor}{refresh_script}<script>function copyAuthorizationUrl(id,button){{const input=document.getElementById(id);if(!input)return;const copied=()=>{{button.textContent={json.dumps(ui_text(locale, "已复制", "Copied"))};}};if(navigator.clipboard&&window.isSecureContext){{navigator.clipboard.writeText(input.value).then(copied).catch(()=>{{input.select();document.execCommand('copy');copied();}});}}else{{input.select();document.execCommand('copy');copied();}}}}</script>'''
    return render_app_shell(
        active="content", locale=locale, current_url="/creator/accounts",
        title=f"SoloScale · {ui_text(locale, '账号中心', 'Account Center')}",
        eyebrow=ui_text(locale, "建立影响力", "Build visibility"),
        heading=ui_text(locale, "连接真实平台身份，能力状态一眼可见。", "Connect real platform identities and see exact capabilities."),
        description=ui_text(locale, "认证、账号身份与发布资格共用一个真值来源。", "Authentication, identity, and publish eligibility share one source of truth."),
        body=body, compact_hero=True,
        extra_css="""
.boundary{margin-bottom:18px;padding:16px 18px;border:1px solid var(--border);border-radius:16px;background:var(--brand-soft)}.boundary p{margin:5px 0 0;color:var(--text-muted)}.account-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px}.account-card{display:grid;gap:13px;padding:19px;border:1px solid var(--border);border-radius:18px;background:#fff}.account-head{display:flex;justify-content:space-between;gap:16px}.account-head span{font-size:11px;font-weight:850;color:var(--brand)}.account-head h2{margin:5px 0}.account-head p{margin:0;color:var(--text-muted)}.account-head>strong{align-self:flex-start;padding:6px 9px;border-radius:999px;background:var(--surface-subtle);font-size:11px}.account-head>strong[data-status="CONNECTED"]{background:var(--success-soft);color:var(--success)}.account-head>strong[data-status="REAUTH_REQUIRED"],.account-head>strong[data-status="REQUIRED_SETUP"]{background:var(--warning-soft);color:var(--warning)}.developer-status,.capabilities,.identity-list{display:grid;gap:7px}.developer-status p,.capabilities p{margin:0;color:var(--text-muted)}.capability-chips{display:flex;gap:6px;flex-wrap:wrap}.capability-chips span{padding:5px 8px;border-radius:999px;background:var(--surface-subtle);font-size:11px;font-weight:800}.capability-chips span[data-state="AVAILABLE"]{background:var(--success-soft);color:var(--success)}.identity{display:flex;justify-content:space-between;gap:12px;padding:10px;border-radius:11px;background:var(--surface-subtle)}.identity small{display:block;color:var(--text-muted)}.identity-actions,.account-actions,.oauth-actions{display:flex;align-items:center;flex-wrap:wrap;gap:8px}.account-actions details{width:100%}.account-actions summary{cursor:pointer;font-weight:800;color:var(--brand)}.account-actions form,.developer-form{display:grid;gap:10px;margin-top:10px}.account-actions label,.developer-form label{display:grid;gap:5px}.oauth-job{display:grid;gap:8px;padding:12px;border-radius:10px;background:var(--brand-soft);color:var(--brand);font-weight:800}.oauth-url{width:100%;font:12px ui-monospace,SFMono-Regular,Menlo,monospace}.danger-button{background:#fff;border-color:#d66;color:#a22}.legacy-site{margin-top:18px;padding:16px;border:1px dashed var(--border);border-radius:14px}.notice{padding:12px 14px;border-radius:12px;background:var(--success-soft);color:var(--success);font-weight:750}
""",
    )


def _capability_copy(capability: Capability, locale: UILocale) -> str:
    labels = {
        "authenticate": ui_text(locale, "账号认证", "Authentication"),
        "read_identity": ui_text(locale, "身份读取", "Identity"),
        "publish_text": ui_text(locale, "发布文字", "Publish text"),
        "publish_image": ui_text(locale, "发布图片", "Publish image"),
        "publish_video": ui_text(locale, "发布视频", "Publish video"),
        "repo_read": ui_text(locale, "仓库读取", "Repository read"),
        "repo_write": ui_text(locale, "仓库写入", "Repository write"),
        "refresh_token": ui_text(locale, "长期连接", "Refresh access"),
    }
    if capability.state == "AVAILABLE":
        return f"{labels[capability.key]} ✓"
    if capability.state == "MISSING_SCOPE":
        return f"{labels[capability.key]} · {ui_text(locale, '当前账号未授权', 'Current account not authorized')}"
    if capability.state == "UNAVAILABLE":
        return f"{labels[capability.key]} · {ui_text(locale, '平台不支持', 'Platform unavailable')}"
    return f"{labels[capability.key]} · {ui_text(locale, '需要设置', 'Setup required')}"


def _identity_row(identity: ConnectedIdentity, locale: UILocale) -> str:
    handle = f"@{identity.handle.lstrip('@')}" if identity.handle else identity.external_account_id
    if identity.platform == "youtube":
        profile = f"https://www.youtube.com/channel/{html.escape(identity.external_account_id, quote=True)}"
        external = f'<a href="{profile}" target="_blank" rel="noopener noreferrer">{ui_text(locale, "打开频道", "Open channel")}</a>'
        remove = ""
    elif identity.platform == "github":
        external = f'<a href="https://github.com/{html.escape(identity.handle, quote=True)}" target="_blank" rel="noopener noreferrer">{ui_text(locale, "打开主页", "Open profile")}</a>'
        remove = '<a class="secondary-button" href="soloscale://disconnect-github">' + ui_text(locale, "断开连接", "Disconnect") + "</a>"
    else:
        external = ""
        remove = f'''<form method="post" action="/creator/accounts/disconnect"><input type="hidden" name="ui_locale" value="{locale}" /><input type="hidden" name="platform" value="{identity.platform}" /><input type="hidden" name="external_account_id" value="{html.escape(identity.external_account_id, quote=True)}" /><button type="submit" class="danger-button">{ui_text(locale, "移除连接", "Disconnect")}</button></form>'''
    return f'''<div class="identity"><div><strong>{html.escape(identity.display_name)}</strong><small>{html.escape(handle)}</small></div><div class="identity-actions">{external}{remove}</div></div>'''


def _youtube_job_html(job: YouTubeJobSnapshot | None, locale: UILocale) -> tuple[str, bool]:
    if job is None:
        return "", False
    copies = {
        "WAITING": ui_text(locale, "等待开始", "Waiting"),
        "STARTING": ui_text(locale, "正在准备 Google 授权…", "Preparing Google authorization…"),
        "AUTHENTICATING": ui_text(locale, "请在浏览器完成 Google 授权", "Complete Google authorization in your browser"),
        "WAITING_FOR_AUTHORIZATION": ui_text(locale, "请选择 Google 账号并完成授权", "Choose a Google account and complete authorization"),
        "COMPLETING": ui_text(locale, "正在验证频道身份…", "Verifying channel identity…"),
        "SUCCESS": ui_text(locale, "频道已连接", "Channel connected"),
        "CANCELLED": ui_text(locale, "授权已取消，可以重新连接。", "Authorization cancelled. You can retry."),
        "TIMED_OUT": ui_text(locale, "授权已超时，可以重新连接。", "Authorization timed out. You can retry."),
        "FAILED": job.error_message or ui_text(locale, "连接失败", "Connection failed"),
    }
    active = job.phase in {"STARTING", "AUTHENTICATING", "WAITING_FOR_AUTHORIZATION", "COMPLETING"}
    actions = ""
    if job.phase == "WAITING_FOR_AUTHORIZATION" and job.authorization_url:
        url = html.escape(job.authorization_url, quote=True)
        input_id = f"auth-url-{job.job_id}"
        actions = f'''<div class="oauth-actions"><a class="secondary-button" href="{url}" target="_blank" rel="noopener noreferrer">{ui_text(locale, "在浏览器中打开", "Open in browser")}</a><button type="button" class="secondary-button" onclick="copyAuthorizationUrl('{input_id}',this)">{ui_text(locale, "复制授权链接", "Copy authorization link")}</button><form method="post" action="/creator/accounts/youtube/cancel"><input type="hidden" name="ui_locale" value="{locale}" /><input type="hidden" name="job_id" value="{job.job_id}" /><button type="submit" class="danger-button">{ui_text(locale, "取消连接", "Cancel")}</button></form></div><input class="oauth-url" id="{input_id}" value="{url}" readonly />'''
    elif active:
        actions = f'''<form method="post" action="/creator/accounts/youtube/cancel"><input type="hidden" name="ui_locale" value="{locale}" /><input type="hidden" name="job_id" value="{job.job_id}" /><button type="submit" class="danger-button">{ui_text(locale, "取消连接", "Cancel")}</button></form>'''
    return f'<div class="oauth-job" data-phase="{job.phase}">{html.escape(copies.get(job.phase, ui_display_value(locale, job.phase)))}{actions}</div>', active


def _provider_card(
    snapshot: PlatformAccountSnapshot,
    *,
    locale: UILocale,
    youtube_job: YouTubeJobSnapshot | None,
    auth_attempt: AuthorizationAttempt | None,
) -> str:
    label = provider_label(snapshot.platform, locale)
    state_copy = {
        "DEVELOPER_NOT_CONFIGURED": ui_text(locale, "需要配置", "Setup required"),
        "REQUIRED_SETUP": ui_text(locale, "需要外部设置", "External setup required"),
        "READY_TO_CONNECT": ui_text(locale, "可以连接", "Ready to connect"),
        "CONNECTED": ui_text(locale, "已连接", "Connected"),
        "REAUTH_REQUIRED": ui_text(locale, "需要重新授权", "Reauthorization required"),
    }.get(snapshot.connection_state, ui_display_value(locale, snapshot.connection_state))
    dev = snapshot.developer_config
    if dev.configured:
        developer_copy = ui_text(locale, "开发者凭证已配置", "Developer integration configured")
    elif dev.required_setup:
        developer_copy = ui_text(
            locale,
            "需要在对应平台的开发者后台完成设置。",
            dev.required_setup,
        )
    else:
        missing = ", ".join(field.replace("_", " ") for field in dev.missing_fields)
        developer_copy = ui_text(locale, "缺少：", "Missing: ") + missing
    identities = "".join(_identity_row(item, locale) for item in snapshot.identities)
    if not identities:
        identities = f'<p>{ui_text(locale, "尚未连接账号。", "No account connected.")}</p>'
    chips = "".join(
        f'<span data-state="{item.state}" title="{html.escape(ui_display_value(locale, item.state), quote=True)}">{html.escape(_capability_copy(item, locale))}</span>'
        for item in snapshot.capabilities
    )
    actions = ""
    job_html = ""
    active = False
    if snapshot.platform == "youtube":
        job_html, active = _youtube_job_html(youtube_job, locale)
        label_copy = (
            ui_text(locale, "重试连接 YouTube", "Retry YouTube connection")
            if youtube_job is not None and youtube_job.phase in {"CANCELLED", "TIMED_OUT", "FAILED"}
            else ui_text(locale, "连接另一个 YouTube 频道", "Connect another YouTube channel")
        )
        disabled = "" if dev.configured and not active else " disabled"
        actions = f'''<form method="post" action="/creator/accounts/youtube/connect"><input type="hidden" name="ui_locale" value="{locale}" /><button type="submit"{disabled}>{label_copy}</button></form>'''
    elif snapshot.platform == "github":
        actions = f'<a class="secondary-button" href="soloscale://connect-github">{ui_text(locale, "连接 / 重新授权 GitHub", "Connect / reauthorize GitHub")}</a>'
    else:
        if auth_attempt is not None:
            state_text = {
                "WAITING_FOR_AUTHORIZATION": ui_text(locale, "等待你在浏览器完成授权", "Waiting for browser authorization"),
                "CANCELLED": ui_text(locale, "连接已取消，可以立即重试", "Connection cancelled; you can retry now"),
                "TIMED_OUT": ui_text(locale, "连接已超时，可以立即重试", "Connection timed out; you can retry now"),
                "FAILED": ui_text(locale, "连接失败，可以立即重试", "Connection failed; you can retry now"),
            }.get(auth_attempt.phase, ui_display_value(locale, auth_attempt.phase))
            attempt_actions = ""
            if auth_attempt.phase == "WAITING_FOR_AUTHORIZATION" and auth_attempt.authorization_url:
                url = html.escape(auth_attempt.authorization_url, quote=True)
                input_id = f"auth-url-{auth_attempt.attempt_id}"
                attempt_actions = f'''<div class="oauth-actions"><a class="secondary-button" href="{url}" target="_blank" rel="noopener noreferrer">{ui_text(locale, "在浏览器中打开", "Open in browser")}</a><button type="button" class="secondary-button" onclick="copyAuthorizationUrl('{input_id}',this)">{ui_text(locale, "复制授权链接", "Copy authorization link")}</button><form method="post" action="/creator/accounts/auth/cancel"><input type="hidden" name="ui_locale" value="{locale}" /><input type="hidden" name="platform" value="{snapshot.platform}" /><input type="hidden" name="attempt_id" value="{auth_attempt.attempt_id}" /><button type="submit" class="danger-button">{ui_text(locale, "取消连接", "Cancel")}</button></form></div><input class="oauth-url" id="{input_id}" value="{url}" readonly /><form class="developer-form" method="post" action="/creator/accounts/auth/complete"><input type="hidden" name="ui_locale" value="{locale}" /><input type="hidden" name="platform" value="{snapshot.platform}" /><input type="hidden" name="attempt_id" value="{auth_attempt.attempt_id}" /><label>{ui_text(locale, "完成授权后，粘贴浏览器中的完整回调网址", "After authorization, paste the complete callback URL from the browser")}<input type="url" name="authorization_response" required /></label><button type="submit">{ui_text(locale, "验证并连接", "Verify and connect")}</button></form>'''
            job_html = f'<div class="oauth-job" data-phase="{auth_attempt.phase}">{html.escape(state_text)}{attempt_actions}</div>'
        if dev.configured and snapshot.platform in {"x", "linkedin", "douyin"}:
            actions += f'''<form method="post" action="/creator/accounts/{snapshot.platform}/connect"><input type="hidden" name="ui_locale" value="{locale}" /><button type="submit">{ui_text(locale, "连接 / 重新授权", "Connect / reauthorize")}</button></form>'''
        elif dev.configured and snapshot.platform == "xiaohongshu":
            actions += f'<p>{ui_text(locale, "设备授权初始化已就绪；自动发布仍不可用。", "Device authorization setup is ready; automated publishing remains unavailable.")}</p>'
        fields = "".join(_developer_field(snapshot.platform, field, dev.configured, locale) for field in dev.missing_fields or dev.values.keys())
        if not fields:
            fields = "".join(_developer_field(snapshot.platform, field, dev.configured, locale) for field in ("client_id",))
        actions += f'''<details><summary>{ui_text(locale, "配置开发者应用", "Configure developer app")}</summary><form class="developer-form" method="post" action="/creator/accounts/configure"><input type="hidden" name="ui_locale" value="{locale}" /><input type="hidden" name="platform" value="{snapshot.platform}" />{fields}<button type="submit">{ui_text(locale, "保存配置", "Save configuration")}</button></form></details>'''
    return f'''<article class="account-card" data-platform="{snapshot.platform}"><div class="account-head"><div><span>{html.escape(label)}</span><h2>{html.escape(label)}</h2><p>{ui_text(locale, f"已连接 {len(snapshot.identities)} 个账号", f"{len(snapshot.identities)} connected")}</p></div><strong data-status="{snapshot.connection_state}">{html.escape(state_copy)}</strong></div><div class="developer-status"><strong>{ui_text(locale, "开发者应用", "Developer integration")}</strong><p>{html.escape(developer_copy)}</p></div><div class="capabilities"><strong>{ui_text(locale, "当前能力", "Capabilities")}</strong><div class="capability-chips">{chips}</div></div><div class="identity-list">{identities}</div>{job_html}<div class="account-actions">{actions}</div></article>'''


def _developer_field(platform: str, field: str, configured: bool, locale: UILocale) -> str:
    secret = field in {"client_secret", "app_secret"}
    placeholder = ui_text(locale, "已保存；留空则保持不变", "Saved; leave blank to keep") if configured else ""
    labels = {
        "client_id": ui_text(locale, "客户端 ID", "Client ID"),
        "client_secret": ui_text(locale, "客户端密钥", "Client secret"),
        "app_id": ui_text(locale, "应用 ID", "App ID"),
        "app_secret": ui_text(locale, "应用密钥", "App secret"),
        "redirect_uri": ui_text(locale, "回调网址", "Redirect URI"),
    }
    label = labels.get(field, field.replace("_", " ").title())
    return f'''<label>{html.escape(label)}<input name="{html.escape(field, quote=True)}" type="{"password" if secret else "text"}" autocomplete="{"new-password" if secret else "off"}" placeholder="{html.escape(placeholder, quote=True)}" /></label>'''


def _independent_site_editor(account: CreatorAccount, locale: UILocale) -> str:
    return f'''<section class="legacy-site" data-platform="independent_site"><strong>{ui_text(locale, "独立站快捷入口", "Independent site shortcut")}</strong><p>{ui_text(locale, "独立站没有统一 OAuth；这里只保存本地快捷链接，不代表已认证。", "Independent sites have no common OAuth; this saves local shortcuts only and does not imply authentication.")}</p><form method="post" action="/creator/accounts/save"><input type="hidden" name="ui_locale" value="{locale}" /><input type="hidden" name="platform" value="independent_site" /><input type="hidden" name="status" value="NOT_CONFIGURED" /><label>{ui_text(locale, "显示名称", "Display name")}<input name="display_name" maxlength="160" value="{html.escape(account.display_name, quote=True)}" /></label><label>{ui_text(locale, "主页 URL", "Profile URL")}<input name="profile_url" maxlength="2048" value="{html.escape(account.profile_url, quote=True)}" placeholder="https://" /></label><label>{ui_text(locale, "管理后台 URL", "Admin URL")}<input name="admin_url" maxlength="2048" value="{html.escape(account.admin_url, quote=True)}" placeholder="https://" /></label><input type="hidden" name="handle" value="" /><button type="submit">{ui_text(locale, "保存快捷入口", "Save shortcut")}</button></form></section>'''
