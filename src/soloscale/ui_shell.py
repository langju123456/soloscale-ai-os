# ruff: noqa: E501
"""Shared, bilingual presentation shell for the local SoloScale product."""

from __future__ import annotations

import html
import urllib.parse
from typing import Literal

UILocale = Literal["zh-CN", "en"]
DEFAULT_UI_LOCALE: UILocale = "zh-CN"
SourceState = Literal[
    "READY",
    "STALE",
    "PROCESSING",
    "AVAILABLE",
    "NOT_CONNECTED",
    "UNAVAILABLE",
    "NEEDS_ATTENTION",
]

_SOURCE_STATE_PRESENTATION: dict[SourceState, tuple[str, str, str]] = {
    "READY": ("✓", "已就绪", "Ready"),
    "STALE": ("!", "需刷新", "Stale"),
    "PROCESSING": ("●", "处理中", "Processing"),
    "AVAILABLE": ("＋", "可添加", "Available"),
    "NOT_CONNECTED": ("○", "未连接", "Not connected"),
    "UNAVAILABLE": ("—", "暂不可用", "Unavailable"),
    "NEEDS_ATTENTION": ("!", "需处理", "Needs attention"),
}

_UI_VALUE_PRESENTATION: dict[str, tuple[str, str]] = {
    "UNKNOWN": ("未知", "Unknown"),
    "ENGINEERING_VERIFIED": ("工程能力已验证", "Engineering verified"),
    "VERIFIED_EVIDENCE": ("已验证证据", "Verified evidence"),
    "APPROVED_CLAIM": ("已批准陈述", "Approved claim"),
    "RAW_STATEMENT": ("原始陈述", "Raw statement"),
    "DISTILLED_INSIGHT": ("提炼洞察", "Distilled insight"),
    "DECISION": ("决策", "Decision"),
    "IMPLEMENTED_CAPABILITY": ("已实现能力", "Implemented capability"),
    "MASTERY_RECEIPT": ("掌握回执", "Mastery receipt"),
    "personal_artifact": ("个人产物", "Personal artifact"),
    "personal_context": ("个人背景", "Personal context"),
    "external_knowledge": ("外部知识", "External knowledge"),
    "outcome_receipt": ("结果回执", "Outcome receipt"),
    "codex_session": ("Codex 对话", "Codex session"),
    "chatgpt_export": ("ChatGPT 导出", "ChatGPT export"),
    "buildlog_run": ("BuildLog 记录", "BuildLog run"),
    "local_git": ("本地 Git", "Local Git"),
    "succeeded": ("成功", "Succeeded"),
    "failed": ("失败", "Failed"),
    "L0 Seen": ("L0 · 已见过", "L0 · Seen"),
    "L1 Explain": ("L1 · 能讲解", "L1 · Explain"),
    "L2 Trace": ("L2 · 能追踪", "L2 · Trace"),
    "L3 Rebuild": ("L3 · 能重建", "L3 · Rebuild"),
    "L4 Debug": ("L4 · 能调试", "L4 · Debug"),
    "L5 Defend": ("L5 · 能答辩", "L5 · Defend"),
    "Explain": ("讲解", "Explain"),
    "Trace": ("追踪", "Trace"),
    "Rebuild": ("重建", "Rebuild"),
    "Debug": ("调试", "Debug"),
    "Defend": ("答辩", "Defend"),
    "DRAFT": ("草稿", "Draft"),
    "APPROVED": ("已批准", "Approved"),
    "REJECTED": ("已拒绝", "Rejected"),
    "PENDING": ("待处理", "Pending"),
    "SUCCESS": ("已成功", "Success"),
    "FAILED": ("失败", "Failed"),
    "CANCELLED": ("已取消", "Cancelled"),
    "TIMED_OUT": ("已超时", "Timed out"),
    "STARTING": ("正在启动", "Starting"),
    "WAITING": ("等待中", "Waiting"),
    "WAITING_FOR_AUTHORIZATION": ("等待授权", "Waiting for authorization"),
    "COMPLETING": ("正在完成", "Completing"),
    "CONNECTED": ("已连接", "Connected"),
    "ACTIVE": ("可用", "Active"),
    "NOT_CONFIGURED": ("未配置", "Not configured"),
    "REAUTH_REQUIRED": ("需要重新授权", "Reauthorization required"),
    "REQUIRED_SETUP": ("需要设置", "Setup required"),
    "MAPPED": ("已关联", "Mapped"),
    "NEEDS_MAPPING": ("需要关联", "Needs mapping"),
}

_NAV_ITEMS: tuple[tuple[str, str, str, str], ...] = (
    ("home", "/", "首页", "Home"),
    ("resume", "/resume", "找到机会", "Get the job"),
    ("learning", "/learning", "面试准备", "Defend the job"),
    ("content", "/creator", "建立影响力", "Build visibility"),
)
_MORE_ITEMS: tuple[tuple[str, str, str, str], ...] = (
    ("work", "/work", "我的工作资料", "Your work"),
    ("video", "/video", "创建视频", "Create video"),
    ("publishing", "/creator/publish", "发布内容", "Publish content"),
    ("advanced", "/advanced", "设置与高级工具", "Settings & advanced"),
)

_CREATOR_ITEMS: tuple[tuple[str, str, str, str], ...] = (
    ("overview", "/creator", "总览", "Overview"),
    ("accounts", "/creator/accounts", "账号", "Accounts"),
    ("stories", "/creator/stories", "故事库", "Story Bank"),
    ("create", "/creator/create", "创作", "Create"),
    ("publish", "/creator/publish", "发布队列", "Publish Queue"),
    ("history", "/creator/history", "历史与成本", "History / Cost"),
)


def normalize_ui_locale(value: str | None) -> UILocale:
    """Return one supported application locale."""
    return "en" if value == "en" else DEFAULT_UI_LOCALE


def ui_text(locale: UILocale, chinese: str, english: str) -> str:
    """Select already-authored interface copy without translating domain artifacts."""
    return chinese if locale == "zh-CN" else english


def ui_bool(locale: UILocale, value: bool) -> str:
    """Render a boolean as interface copy without changing its stored value."""

    return ui_text(locale, "是" if value else "否", "Yes" if value else "No")


def ui_display_value(locale: UILocale, value: object) -> str:
    """Localize a known internal value while preserving unknown user/source content."""

    if isinstance(value, bool):
        return ui_bool(locale, value)
    raw_value = getattr(value, "value", value)
    key = str(raw_value)
    copies = _UI_VALUE_PRESENTATION.get(key)
    return ui_text(locale, *copies) if copies is not None else key


def ui_url(path: str, locale: UILocale, **query: str) -> str:
    """Build an internal product URL that retains the selected interface locale."""
    split = urllib.parse.urlsplit(path)
    values = dict(urllib.parse.parse_qsl(split.query, keep_blank_values=True))
    values.update({key: value for key, value in query.items() if value})
    values["lang"] = locale
    return urllib.parse.urlunsplit(
        ("", "", split.path or "/", urllib.parse.urlencode(values), split.fragment)
    )


def render_source_state(state: SourceState, locale: UILocale) -> str:
    """Render one reusable, semantic data-source state indicator."""

    symbol, chinese, english = _SOURCE_STATE_PRESENTATION[state]
    label = ui_text(locale, chinese, english)
    css_state = state.casefold().replace("_", "-")
    return (
        f'<span class="source-state source-state-{css_state}" '
        f'data-source-state="{state}" title="{html.escape(label, quote=True)}" '
        f'aria-label="{html.escape(label)}">'
        f'<span class="source-state-symbol" aria-hidden="true">{symbol}</span>'
        f"<span>{html.escape(label)}</span></span>"
    )


def render_creator_nav(*, active: str, locale: UILocale) -> str:
    """Render the stable Creator workspace information architecture."""
    links = []
    for key, path, chinese, english in _CREATOR_ITEMS:
        current = key == active
        aria_current = ' aria-current="page"' if current else ""
        links.append(
            f'<a class="creator-link{" active" if current else ""}" '
            f'href="{ui_url(path, locale)}"'
            f"{aria_current}>"
            f'{html.escape(ui_text(locale, chinese, english))}</a>'
        )
    return (
        f'<nav class="creator-nav" aria-label="{html.escape(ui_text(locale, "创作者工作区", "Creator workspace"))}">'
        + "".join(links)
        + "</nav>"
    )


def render_app_nav(
    *, active: str, locale: UILocale, current_url: str, product_note: str | None = None
) -> str:
    link_parts: list[str] = []
    for key, path, zh, en in _NAV_ITEMS:
        current = key == active
        aria_current = ' aria-current="page"' if current else ""
        link_parts.append(
            f'<a class="nav-link{" active" if current else ""}" '
            f'href="{ui_url(path, locale)}"{aria_current}>'
            f"{html.escape(ui_text(locale, zh, en))}</a>"
        )
    links = "".join(link_parts)
    more_parts: list[str] = []
    for key, path, zh, en in _MORE_ITEMS:
        current = key == active
        aria_current = ' aria-current="page"' if current else ""
        more_parts.append(
            f'<a class="more-link{" active" if current else ""}" '
            f'href="{ui_url(path, locale)}"{aria_current}>'
            f"{html.escape(ui_text(locale, zh, en))}</a>"
        )
    more_links = "".join(more_parts)
    alternate: UILocale = "en" if locale == "zh-CN" else "zh-CN"
    product_copy = product_note or ui_text(
        locale, "把真实工作变成结果", "Turn real work into outcomes"
    )
    return f"""<nav class="app-nav" aria-label="{html.escape(ui_text(locale, '主导航', 'Main navigation'))}">
  <a class="brand" href="{ui_url('/', locale)}" aria-label="SoloScale">
    <span class="brand-mark" aria-hidden="true">S</span>
    <span class="brand-copy"><strong>SoloScale</strong><small>{html.escape(product_copy)}</small></span>
  </a>
  <div class="nav-scroll">{links}</div>
  <details class="nav-more">
    <summary>{html.escape(ui_text(locale, '更多', 'More'))}</summary>
    <div class="more-menu">{more_links}</div>
  </details>
  <a class="locale-switch" href="{html.escape(ui_url(current_url, alternate))}"
    hreflang="{alternate}" aria-label="{html.escape(ui_text(locale, 'Switch to English', '切换到中文'))}">
    {"EN" if locale == "zh-CN" else "中文"}
  </a>
</nav>"""


_BASE_CSS = r"""
:root {
  color-scheme: light;
  --canvas:#f7f8fc; --canvas-tint:#eef2ff; --surface:#fff; --surface-subtle:#f5f7fb;
  --surface-warm:#faf8f5; --text:#182034; --text-muted:#5f697c; --text-soft:#667085;
  --border:#dce2ec; --control-border:#8490a3; --brand:#4056b4; --brand-hover:#30449a;
  --brand-soft:#ecefff; --brand-secondary:#7257ad; --on-brand:#fff; --focus:#4056b4;
  --success:#18775c; --success-soft:#e9f6f0; --warning:#795607; --warning-soft:#fff5da;
  --danger:#a33a35; --danger-soft:#fff0ef; --info:#4056b4; --info-soft:#ecefff;
  --radius-sm:10px; --radius-md:14px; --radius-lg:20px; --radius-xl:24px;
  --shadow-card:0 16px 44px rgb(35 45 70 / 8%),0 1px 2px rgb(35 45 70 / 6%);
}
* { box-sizing:border-box; }
html { scroll-behavior:smooth; }
body {
  margin:0; color:var(--text); background:
    radial-gradient(circle at 8% 2%,#e8edff 0,transparent 28%),
    radial-gradient(circle at 92% 14%,#edf7f2 0,transparent 24%),
    linear-gradient(180deg,#fbfbfd 0,var(--canvas) 55%,#f4f6fa 100%);
  font:16px/1.6 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
}
.page-progress { position:fixed; inset:0 0 auto; z-index:200; height:4px; overflow:hidden;
  background:transparent; opacity:0; pointer-events:none; transition:opacity .12s ease; }
.page-progress.active { opacity:1; }
.page-progress::after { content:""; display:block; width:36%; height:100%; border-radius:999px;
  background:linear-gradient(90deg,var(--brand),#7b63c8,var(--success));
  animation:page-progress 1s ease-in-out infinite; }
@keyframes page-progress { from { transform:translateX(-120%); } to { transform:translateX(390%); } }
a { color:var(--brand); }
.skip-link { position:fixed; left:16px; top:-80px; z-index:100; padding:10px 14px;
  border-radius:10px; background:var(--text); color:#fff; }
.skip-link:focus { top:14px; }
.app-shell { max-width:1220px; margin:0 auto; padding:26px 24px 70px; }
.app-nav { min-height:52px; display:flex; align-items:center; gap:18px; margin-bottom:48px; }
.brand { display:flex; align-items:center; gap:11px; min-width:max-content; color:var(--text);
  text-decoration:none; }
.brand-mark { width:38px; height:38px; border-radius:13px; display:grid; place-items:center;
  color:#fff; font-weight:850; background:linear-gradient(135deg,var(--brand),#7656c8);
  box-shadow:0 8px 24px rgb(64 86 180 / 22%); }
.brand-copy { display:grid; line-height:1.15; }
.brand-copy strong { font-size:17px; letter-spacing:-.02em; }
.brand-copy small { margin-top:3px; color:var(--text-muted); font-size:10px; font-weight:600; }
.nav-scroll { flex:1; display:flex; justify-content:flex-end; gap:4px; }
.nav-link,.locale-switch,.nav-more summary,.more-link { min-height:44px; display:flex;
  align-items:center; border-radius:12px; padding:0 11px; color:var(--text-muted);
  text-decoration:none; font-size:14px; font-weight:700; cursor:pointer; }
.nav-link:hover,.nav-link.active,.locale-switch:hover,.nav-more summary:hover,.more-link:hover,
.more-link.active { color:var(--brand); background:var(--brand-soft); }
.nav-more { position:relative; }
.nav-more summary { list-style:none; }
.nav-more summary::-webkit-details-marker { display:none; }
.nav-more summary::after { content:"⌄"; margin-left:6px; font-size:12px; }
.more-menu { position:absolute; right:0; top:48px; min-width:190px; z-index:20; padding:7px;
  border:1px solid var(--border); border-radius:14px; background:var(--surface);
  box-shadow:var(--shadow-card); }
.more-link { width:100%; }
.locale-switch { border:1px solid var(--border); min-width:48px; justify-content:center; }
.creator-nav { display:flex; gap:6px; margin:-18px 0 28px; padding:6px; overflow-x:auto;
  border:1px solid var(--border); border-radius:15px; background:var(--surface-subtle); }
.creator-link { min-height:40px; display:flex; align-items:center; flex:0 0 auto; padding:0 12px;
  border-radius:10px; color:var(--text-muted); text-decoration:none; font-size:13px; font-weight:800; }
.creator-link:hover,.creator-link.active { color:var(--brand); background:var(--surface);
  box-shadow:0 1px 4px rgb(34 44 75 / 9%); }
.app-hero { max-width:800px; margin:0 auto 34px; text-align:center; }
.app-hero.compact { max-width:none; margin:0 0 24px; text-align:left; }
.eyebrow,.kicker,.result-kicker { color:var(--brand); font-size:12px; font-weight:850;
  letter-spacing:.13em; text-transform:uppercase; }
.app-hero h1 { margin:11px 0 14px; font-size:clamp(38px,5.7vw,64px); line-height:1.04;
  letter-spacing:-.052em; }
.app-hero.compact h1 { font-size:clamp(30px,4vw,46px); }
.app-hero p { max-width:700px; margin:0 auto; color:var(--text-muted); font-size:18px; }
.app-hero.compact p { margin:0; font-size:16px; }
.card,.panel,.input-card,.result-card,.form-card,.result-panel,.channel,.empty {
  background:color-mix(in srgb,var(--surface) 96%,transparent); border:1px solid #fff;
  border-radius:var(--radius-xl); box-shadow:var(--shadow-card); padding:28px;
}
.empty-state,.empty { min-height:360px; display:flex; flex-direction:column;
  justify-content:center; text-align:center; }
.empty-state p,.empty p { max-width:520px; margin:6px auto; color:var(--text-muted); }
.empty-steps { display:grid; gap:10px; max-width:520px; margin:20px auto; padding:0; list-style:none;
  text-align:left; }
.empty-steps li { display:flex; gap:10px; align-items:flex-start; padding:11px 13px;
  border:1px solid var(--border); border-radius:12px; background:var(--surface-subtle); }
.step-number { flex:0 0 24px; height:24px; display:grid; place-items:center; border-radius:50%;
  background:var(--brand-soft); color:var(--brand); font-size:12px; font-weight:850; }
form { display:grid; gap:16px; }
label { display:grid; gap:7px; color:var(--text); font-size:14px; font-weight:750; }
.hint,.muted,.small { color:var(--text-muted); font-size:12px; font-weight:400; }
input:not([type="checkbox"]):not([type="radio"]):not([type="hidden"]),textarea,select { width:100%; min-height:44px; border:1px solid var(--control-border);
  border-radius:13px; padding:12px 13px; background:#fff; color:var(--text); font:inherit; }
input[type="checkbox"],input[type="radio"] { width:20px; height:20px; min-height:20px;
  margin:1px 0 0; padding:0; accent-color:var(--brand); flex:0 0 20px; }
textarea { resize:vertical; line-height:1.55; }
button,.primary,.primary-button,.button-link,.secondary-button { min-height:44px; border:0;
  border-radius:13px; padding:11px 16px; background:var(--brand); color:var(--on-brand);
  font:inherit; font-weight:800; text-align:center; text-decoration:none; cursor:pointer; }
button:hover,.primary:hover,.primary-button:hover,.button-link:hover { background:var(--brand-hover); }
.secondary,.secondary-button { background:var(--brand-soft); color:var(--brand); }
.notice,.boundary,.save-note,.privacy-note,.review-note { padding:12px 14px;
  border-radius:12px; background:var(--surface-subtle); color:var(--text-muted); }
.success { background:var(--success-soft); color:var(--success); border-color:#b9dfcf; }
.warning { background:var(--warning-soft); color:var(--warning); border-color:#ead69b; }
.error,.error-state { background:var(--danger-soft); color:var(--danger); border-color:#efc7c4; }
.status-badge { display:inline-flex; align-items:center; min-height:28px; padding:3px 9px;
  border-radius:999px; background:var(--brand-soft); color:var(--brand); font-size:12px;
  font-weight:800; }
.source-state { display:inline-flex; align-items:center; gap:6px; width:max-content; min-height:27px;
  padding:3px 9px; border-radius:999px; background:var(--surface-subtle); color:var(--text-muted);
  font-size:11px; font-weight:850; letter-spacing:.02em; white-space:nowrap; }
.source-state-symbol { min-width:12px; text-align:center; font-size:13px; line-height:1; }
.source-state-ready { background:var(--success-soft); color:var(--success); }
.source-state-processing,.source-state-available { background:var(--brand-soft); color:var(--brand); }
.source-state-stale,.source-state-needs-attention { background:var(--warning-soft); color:var(--warning); }
pre,code { overflow-wrap:anywhere; }
pre { white-space:pre-wrap; padding:16px; border:1px solid var(--border); border-radius:14px;
  background:#fafbfc; color:#2c3548; }
:focus-visible { outline:3px solid var(--focus); outline-offset:3px; }
@media(max-width:900px) {
  .app-nav { align-items:flex-start; flex-wrap:wrap; margin-bottom:34px; }
  .nav-scroll { order:3; width:100%; justify-content:flex-start; overflow-x:auto; padding-bottom:4px; }
  .nav-more { margin-left:auto; }
}
@media(max-width:580px) {
  .app-shell { padding:18px 14px 44px; }
  .brand-copy small { display:none; }
  .app-hero h1 { font-size:40px; }
  .card,.panel,.input-card,.result-card,.form-card,.result-panel,.channel,.empty { padding:21px;
    border-radius:19px; }
}
@media(prefers-reduced-motion:reduce) { * { scroll-behavior:auto!important; transition:none!important; } }
"""


def render_app_shell(
    *,
    active: str,
    locale: UILocale,
    current_url: str,
    title: str,
    eyebrow: str,
    heading: str,
    description: str,
    body: str,
    extra_css: str = "",
    script: str = "",
    compact_hero: bool = False,
) -> str:
    """Render one accessible product shell around a page-specific body."""
    skip = ui_text(locale, "跳到主要内容", "Skip to main content")
    nav = render_app_nav(active=active, locale=locale, current_url=current_url)
    hero_class = "app-hero compact" if compact_hero else "app-hero"
    return f"""<!doctype html>
<html lang="{locale}">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>{_BASE_CSS}\n{extra_css}</style>
</head>
<body>
  <div id="page-progress" class="page-progress" role="status" aria-live="polite" aria-label=""></div>
  <a class="skip-link" href="#main-content">{html.escape(skip)}</a>
  <div class="app-shell">
    {nav}
    <header class="{hero_class}">
      <span class="eyebrow">{html.escape(eyebrow)}</span>
      <h1>{html.escape(heading)}</h1>
      <p>{html.escape(description)}</p>
    </header>
    <main id="main-content">{body}</main>
  </div>
  <script>
  (() => {{
    const progress = document.getElementById("page-progress");
    const busyLabel = document.documentElement.lang === "en" ? "Working…" : "正在处理…";
    const showProgress = (label) => {{
      progress.classList.add("active");
      progress.setAttribute("aria-label", label || busyLabel);
    }};
    document.addEventListener("click", (event) => {{
      if (!(event.target instanceof Element)) return;
      const link = event.target.closest("a[href]");
      if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || link.target) return;
      let target;
      try {{ target = new URL(link.href, window.location.href); }} catch (_) {{ return; }}
      if (target.origin !== window.location.origin) return;
      if (target.pathname === window.location.pathname && target.search === window.location.search) return;
      showProgress(link.getAttribute("aria-label") || link.textContent.trim());
    }});
    document.addEventListener("submit", (event) => {{
      const form = event.target;
      if (!(form instanceof HTMLFormElement)) return;
      if (form.dataset.submitting === "true") {{
        event.preventDefault();
        return;
      }}
      form.dataset.submitting = "true";
      form.setAttribute("aria-busy", "true");
      const button = event.submitter instanceof HTMLButtonElement
        ? event.submitter
        : form.querySelector('button[type="submit"]');
      if (button) {{
        // Keep the successful submitter enabled until the browser serializes
        // its name/value pair. Disabling it here drops actions such as
        // generation_mode=template and review_action=approve.
        button.setAttribute("aria-disabled", "true");
        button.textContent = button.dataset.loadingLabel || busyLabel;
      }}
      showProgress(form.dataset.progressLabel || busyLabel);
    }});
    window.addEventListener("pageshow", () => {{
      progress.classList.remove("active");
      progress.setAttribute("aria-label", "");
    }});
  }})();
  {script}
  </script>
</body>
</html>"""
