import json
import stat
from pathlib import Path

import pytest

from soloscale.creator_accounts import (
    CreatorAccountError,
    creator_accounts_page,
    load_creator_accounts,
    normalize_account,
    save_creator_account,
)
from soloscale.platform_accounts import save_developer_config, start_authorization_attempt
from soloscale.youtube_publishing import YouTubeJobSnapshot


def test_account_center_exposes_all_platforms_and_only_configured_links(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    save_creator_account(
        data_root,
        normalize_account(
            platform="linkedin",
            display_name="Lang Ju",
            handle="lang-ju",
            profile_url="https://www.linkedin.com/in/lang-ju",
            admin_url="https://www.linkedin.com/in/lang-ju/edit/",
            status="ACTIVE",
        ),
    )

    page = creator_accounts_page(data_root)

    for platform in (
        "douyin",
        "xiaohongshu",
        "youtube",
        "x",
        "linkedin",
        "github",
        "independent_site",
    ):
        assert f'data-platform="{platform}"' in page
    assert page.count('class="account-card"') == 6
    assert page.count('action="/creator/accounts/save"') == 1
    assert 'data-platform="linkedin"' in page
    assert "已连接 0 个账号" in page
    assert "平台认证中心" in page
    assert "手动状态不再代表已连接" in page
    assert "配置开发者应用" in page
    assert 'href="/creator/accounts?lang=zh-CN"' in page


def test_account_center_persists_only_directory_fields_with_private_permissions(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    account = normalize_account(
        platform="youtube",
        display_name="SoloScale",
        handle="@soloscale",
        profile_url="https://youtube.com/@soloscale",
        admin_url="https://studio.youtube.com/channel/example",
        status="ACTIVE",
    )

    path = save_creator_account(data_root, account)
    payload = json.loads(path.read_text(encoding="utf-8"))
    youtube = next(
        item for item in payload["accounts"] if item["platform"] == "youtube"
    )

    assert set(youtube) == {
        "platform",
        "display_name",
        "handle",
        "profile_url",
        "admin_url",
        "status",
    }
    assert len(load_creator_accounts(data_root)) == 7
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("profile_url", "javascript:alert(1)"),
        ("admin_url", "file:///tmp/admin"),
        ("profile_url", "https://user:password@example.com/profile"),
    ),
)
def test_account_center_rejects_unsafe_external_urls(field: str, value: str) -> None:
    values = {"platform": "x", field: value}
    with pytest.raises(CreatorAccountError):
        normalize_account(**values)


def test_account_center_renders_english_copy(tmp_path: Path) -> None:
    page = creator_accounts_page(tmp_path / ".soloscale", locale="en")
    assert "Connect real platform identities and see exact capabilities." in page
    assert "Open profile" not in page
    assert "Open admin" not in page
    assert "Platform Authentication Hub" in page
    assert "rednote" in page
    assert "Xiaohongshu" not in page
    assert "Developer integration" in page


def test_account_center_exposes_manual_oauth_controls_and_cancel(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    job = YouTubeJobSnapshot(
        job_id="youtube-auth-012345abcdef",
        kind="connect",
        phase="WAITING_FOR_AUTHORIZATION",
        created_at="2026-08-28T12:00:00+00:00",
        updated_at="2026-08-28T12:00:01+00:00",
        authorization_url=(
            "https://accounts.google.com/o/oauth2/auth?prompt=select_account&state=opaque"
        ),
    )

    page = creator_accounts_page(data_root, youtube_job=job)

    assert "请选择 Google 账号并完成授权" in page
    assert "在浏览器中打开" in page
    assert "复制授权链接" in page
    assert 'action="/creator/accounts/youtube/cancel"' in page
    assert 'name="job_id" value="youtube-auth-012345abcdef"' in page
    assert "prompt=select_account&amp;state=opaque" in page
    assert "setTimeout(()=>location.reload(),1500)" in page
    assert "连接另一个 YouTube 频道</button>" in page
    assert "连接另一个 YouTube 频道</button></form>" in page
    assert "<button type=\"submit\" disabled>连接另一个 YouTube 频道" in page


def test_account_center_exposes_callback_completion_without_echoing_secrets(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    save_developer_config(
        data_root,
        "x",
        {
            "client_id": "synthetic-client",
            "redirect_uri": "http://127.0.0.1:18765/callback",
        },
    )
    attempt = start_authorization_attempt(data_root, "x")

    page = creator_accounts_page(data_root, auth_attempt=attempt)

    assert 'action="/creator/accounts/auth/complete"' in page
    assert 'name="authorization_response"' in page
    assert f'name="attempt_id" value="{attempt.attempt_id}"' in page
    assert "access_token" not in page


@pytest.mark.parametrize(
    ("phase", "message"),
    (
        ("CANCELLED", "授权已取消，可以重新连接。"),
        ("TIMED_OUT", "授权已超时，可以重新连接。"),
        ("FAILED", "synthetic failure"),
    ),
)
def test_account_center_terminal_oauth_states_offer_retry(
    tmp_path: Path,
    phase: str,
    message: str,
) -> None:
    job = YouTubeJobSnapshot(
        job_id="youtube-auth-012345abcdef",
        kind="connect",
        phase=phase,  # type: ignore[arg-type]
        created_at="2026-08-28T12:00:00+00:00",
        updated_at="2026-08-28T12:00:01+00:00",
        error_message="synthetic failure" if phase == "FAILED" else None,
    )

    page = creator_accounts_page(tmp_path / ".soloscale", youtube_job=job)

    assert message in page
    assert "重试连接 YouTube" in page
    assert "setTimeout(()=>location.reload(),1500)" not in page
