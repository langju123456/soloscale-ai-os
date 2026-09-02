import json
import stat
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from soloscale.platform_accounts import (
    ConnectedIdentity,
    PlatformAccountError,
    complete_authorization_response,
    consume_authorization_callback,
    disconnect_identity,
    eligible_publish_identities,
    load_authorization_attempt,
    load_connected_identities,
    load_developer_config,
    parse_linkedin_identity,
    parse_x_identity,
    platform_snapshot,
    provider_label,
    rednote_device_authorization_request,
    save_connected_identity,
    save_developer_config,
    start_authorization_attempt,
)
from soloscale.youtube_publishing import save_authorized_channel


def _identity(platform: str, scopes: tuple[str, ...]) -> ConnectedIdentity:
    return ConnectedIdentity(  # type: ignore[arg-type]
        platform=platform,
        external_account_id="12345",
        display_name="Synthetic Account",
        handle="synthetic",
        avatar_url=None,
        scopes=scopes,
        token_reference="pending",
        connected_at="2026-08-28T00:00:00+00:00",
    )


def test_youtube_channel_is_projected_from_canonical_store_and_publish_eligible(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    save_authorized_channel(
        data_root,
        channel_id="UC1234567890123456789012",
        channel_title="AI Vision",
        credential_json=json.dumps(
            {
                "access_token": "synthetic",
                "refresh_token": "synthetic-refresh",
                "scopes": [
                    "https://www.googleapis.com/auth/youtube.readonly",
                    "https://www.googleapis.com/auth/youtube.upload",
                ],
            }
        ),
    )

    snapshot = platform_snapshot(data_root, "youtube")
    eligible = eligible_publish_identities(data_root)

    assert snapshot.connection_state == "CONNECTED"
    assert snapshot.identities[0].display_name == "AI Vision"
    assert eligible["youtube"][0].external_account_id == "UC1234567890123456789012"


def test_x_pkce_attempt_uses_exact_scopes_and_state_failure_saves_nothing(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    save_developer_config(
        data_root,
        "x",
        {"client_id": "synthetic-client", "redirect_uri": "http://127.0.0.1:18765/callback"},
    )
    attempt = start_authorization_attempt(data_root, "x")
    query = parse_qs(urlsplit(attempt.authorization_url or "").query)

    assert set(query["scope"][0].split()) == {
        "tweet.read",
        "users.read",
        "tweet.write",
        "offline.access",
    }
    assert query["code_challenge_method"] == ["S256"]
    assert "code_challenge" in query
    with pytest.raises(PlatformAccountError, match="state"):
        consume_authorization_callback(
            data_root,
            "x",
            attempt.attempt_id,
            returned_state="wrong-state",
            code="synthetic-code",
            exchange=lambda *_: {"access_token": "never"},
            resolve_identity=lambda _: _identity("x", ()),
        )
    assert load_connected_identities(data_root, "x") == ()


def test_verified_identity_is_saved_separately_and_reconnect_upserts(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    identity = _identity(
        "x", ("tweet.read", "users.read", "tweet.write", "offline.access")
    )
    first = save_connected_identity(
        data_root, identity, token_payload={"access_token": "secret-one"}
    )
    save_connected_identity(
        data_root, identity, token_payload={"access_token": "secret-two"}
    )

    accounts_path = data_root / "integrations" / "x" / "accounts.json"
    metadata = accounts_path.read_text(encoding="utf-8")
    token_path = data_root / "integrations" / "x" / first.token_reference

    assert len(load_connected_identities(data_root, "x")) == 1
    assert "secret-one" not in metadata
    assert "secret-two" not in metadata
    assert stat.S_IMODE(accounts_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert eligible_publish_identities(data_root)["x"][0].handle == "synthetic"
    assert disconnect_identity(data_root, "x", "12345") is True
    assert not token_path.exists()


def test_publish_eligibility_is_evaluated_per_identity(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    read_only = _identity("x", ("tweet.read", "users.read"))
    writable = ConnectedIdentity(
        **{
            **read_only.__dict__,
            "external_account_id": "writable",
            "handle": "writable",
            "scopes": ("tweet.read", "users.read", "tweet.write", "offline.access"),
        }
    )
    save_connected_identity(
        data_root, read_only, token_payload={"access_token": "read-only"}
    )
    save_connected_identity(
        data_root, writable, token_payload={"access_token": "writable"}
    )

    eligible = eligible_publish_identities(data_root)["x"]
    assert [item.external_account_id for item in eligible] == ["writable"]


def test_expired_identity_cannot_unlock_publishing(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    identity = _identity(
        "x", ("tweet.read", "users.read", "tweet.write", "offline.access")
    )
    save_connected_identity(
        data_root,
        identity,
        token_payload={
            "access_token": "expired",
            "expires_at": "2026-08-27T00:00:00+00:00",
        },
    )

    assert platform_snapshot(data_root, "x").connection_state == "REAUTH_REQUIRED"
    assert "x" not in eligible_publish_identities(data_root)


def test_expired_access_token_with_refresh_token_remains_connected(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    identity = _identity(
        "x", ("tweet.read", "users.read", "tweet.write", "offline.access")
    )
    save_connected_identity(
        data_root,
        identity,
        token_payload={
            "access_token": "expired",
            "refresh_token": "synthetic-refresh",
            "expires_at": "2026-08-27T00:00:00+00:00",
        },
    )

    assert platform_snapshot(data_root, "x").connection_state == "CONNECTED"
    assert eligible_publish_identities(data_root)["x"][0].handle == "synthetic"


def test_douyin_authorization_requests_identity_and_video_capabilities(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    save_developer_config(
        data_root,
        "douyin",
        {
            "client_key": "synthetic-key",
            "client_secret": "synthetic-secret",
            "redirect_uri": "https://solo-scale-ai-os.vercel.app/oauth/douyin/callback",
        },
    )

    attempt = start_authorization_attempt(data_root, "douyin")
    query = parse_qs(urlsplit(attempt.authorization_url or "").query)
    assert set(query["scope"][0].split(",")) == {"user_info", "video.create"}


def test_platform_specific_readiness_and_rednote_capability(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    assert provider_label("xiaohongshu", "zh-CN") == "小红书"
    assert provider_label("xiaohongshu", "en") == "rednote"
    assert platform_snapshot(data_root, "linkedin").connection_state == "DEVELOPER_NOT_CONFIGURED"
    save_developer_config(
        data_root,
        "linkedin",
        {
            "client_id": "synthetic",
            "client_secret": "synthetic-secret",
            "redirect_uri": "https://example.com/oauth/linkedin/callback",
        },
    )
    assert platform_snapshot(data_root, "linkedin").connection_state == "REQUIRED_SETUP"

    save_developer_config(
        data_root,
        "xiaohongshu",
        {"app_id": "synthetic", "app_secret": "synthetic-secret"},
    )
    config = load_developer_config(data_root, "xiaohongshu")
    endpoint, body = rednote_device_authorization_request(config)
    assert endpoint.endswith("/oauth2/device/code")
    assert body["scopes"] == ["basic_info"]
    snapshot = platform_snapshot(data_root, "xiaohongshu")
    publish = next(item for item in snapshot.capabilities if item.key == "publish_text")
    assert publish.state == "UNAVAILABLE"


def test_pending_attempt_times_out_and_is_immediately_retryable(tmp_path: Path) -> None:
    data_root = tmp_path / ".soloscale"
    save_developer_config(
        data_root,
        "x",
        {"client_id": "synthetic-client", "redirect_uri": "http://localhost:18765/callback"},
    )
    old = datetime.now(UTC) - timedelta(minutes=6)
    attempt = start_authorization_attempt(data_root, "x", now=old)
    assert load_authorization_attempt(data_root, "x", attempt.attempt_id).phase == "TIMED_OUT"
    assert start_authorization_attempt(data_root, "x").phase == "WAITING_FOR_AUTHORIZATION"


def test_new_attempt_invalidates_old_callback_and_identity_parsers_are_bounded(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".soloscale"
    save_developer_config(
        data_root,
        "x",
        {"client_id": "synthetic-client", "redirect_uri": "http://localhost:18765/callback"},
    )
    first = start_authorization_attempt(data_root, "x")
    start_authorization_attempt(data_root, "x")
    with pytest.raises(PlatformAccountError, match="newer attempt"):
        consume_authorization_callback(
            data_root,
            "x",
            first.attempt_id,
            returned_state="irrelevant",
            code="irrelevant",
            exchange=lambda *_: {"access_token": "never"},
            resolve_identity=lambda _: _identity("x", ()),
        )

    x_identity = parse_x_identity(
        {"scope": "tweet.read users.read tweet.write offline.access"},
        {"data": {"id": "42", "name": "Synthetic X", "username": "synthetic_x"}},
    )
    linkedin_identity = parse_linkedin_identity(
        {"scope": "openid profile w_member_social"},
        {"sub": "member-42", "name": "Synthetic LinkedIn"},
    )
    assert x_identity.handle == "synthetic_x"
    assert "tweet.write" in x_identity.scopes
    assert linkedin_identity.external_account_id == "member-42"
    assert "w_member_social" in linkedin_identity.scopes


class _JSONResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.raw = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_JSONResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _: int) -> bytes:
        return self.raw


@pytest.mark.parametrize(
    ("platform", "config", "token_payload", "identity_payload", "expected_id"),
    (
        (
            "x",
            {
                "client_id": "synthetic-client",
                "redirect_uri": "http://127.0.0.1:18765/callback",
            },
            {
                "access_token": "x-access",
                "refresh_token": "x-refresh",
                "expires_in": 7200,
                "scope": "tweet.read users.read tweet.write offline.access",
            },
            {"data": {"id": "x-42", "name": "Synthetic X", "username": "x_user"}},
            "x-42",
        ),
        (
            "linkedin",
            {
                "client_id": "synthetic-client",
                "client_secret": "synthetic-secret",
                "redirect_uri": "https://solo-scale-ai-os.vercel.app/oauth/linkedin/callback",
            },
            {
                "access_token": "linkedin-access",
                "expires_in": 7200,
                "scope": "openid profile w_member_social",
            },
            {"sub": "linkedin-42", "name": "Synthetic LinkedIn"},
            "linkedin-42",
        ),
        (
            "douyin",
            {
                "client_key": "synthetic-key",
                "client_secret": "synthetic-secret",
                "redirect_uri": "https://solo-scale-ai-os.vercel.app/oauth/douyin/callback",
            },
            {
                "data": {
                    "access_token": "douyin-access",
                    "refresh_token": "douyin-refresh",
                    "expires_in": 7200,
                    "open_id": "douyin-42",
                    "scope": "user_info video.create",
                }
            },
            {
                "data": {
                    "open_id": "douyin-42",
                    "nickname": "Synthetic Douyin",
                }
            },
            "douyin-42",
        ),
    ),
)
def test_real_provider_callback_contract_is_verified_offline_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    config: dict[str, str],
    token_payload: dict[str, object],
    identity_payload: dict[str, object],
    expected_id: str,
) -> None:
    data_root = tmp_path / ".soloscale"
    save_developer_config(data_root, platform, config)  # type: ignore[arg-type]
    attempt = start_authorization_attempt(data_root, platform)  # type: ignore[arg-type]
    pending_path = (
        data_root
        / "integrations"
        / platform
        / "pending"
        / f"{attempt.attempt_id}.json"
    )
    state = json.loads(pending_path.read_text(encoding="utf-8"))["state"]
    responses = [_JSONResponse(token_payload), _JSONResponse(identity_payload)]
    requested_urls: list[str] = []

    def fake_urlopen(
        request: urllib.request.Request, *, timeout: int
    ) -> _JSONResponse:
        assert timeout == 30
        requested_urls.append(request.full_url)
        return responses.pop(0)

    monkeypatch.setattr("soloscale.platform_accounts.urllib.request.urlopen", fake_urlopen)
    separator = "&" if "?" in config["redirect_uri"] else "?"
    identity = complete_authorization_response(
        data_root,
        platform,  # type: ignore[arg-type]
        attempt.attempt_id,
        f'{config["redirect_uri"]}{separator}code=synthetic-code&state={state}',
    )

    assert identity.external_account_id == expected_id
    assert len(requested_urls) == 2
    assert requested_urls[0].startswith("https://")
    assert requested_urls[1].startswith("https://")
    accounts_path = data_root / "integrations" / platform / "accounts.json"
    metadata = accounts_path.read_text(encoding="utf-8")
    assert "-access" not in metadata
    token_path = data_root / "integrations" / platform / identity.token_reference
    token = json.loads(token_path.read_text(encoding="utf-8"))
    assert token["access_token"].endswith("-access")
    assert "expires_at" in token
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert not pending_path.exists()


def test_provider_network_failure_is_sanitized_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / ".soloscale"
    redirect = "http://127.0.0.1:18765/callback"
    save_developer_config(
        data_root, "x", {"client_id": "synthetic", "redirect_uri": redirect}
    )
    attempt = start_authorization_attempt(data_root, "x")
    pending_path = data_root / "integrations" / "x" / "pending" / f"{attempt.attempt_id}.json"
    state = json.loads(pending_path.read_text(encoding="utf-8"))["state"]

    def fail_urlopen(*_: object, **__: object) -> object:
        raise urllib.error.URLError("synthetic private provider detail")

    monkeypatch.setattr("soloscale.platform_accounts.urllib.request.urlopen", fail_urlopen)
    with pytest.raises(PlatformAccountError, match="request failed") as failure:
        complete_authorization_response(
            data_root,
            "x",
            attempt.attempt_id,
            f"{redirect}?code=synthetic&state={state}",
        )
    assert "private provider detail" not in str(failure.value)
    assert load_authorization_attempt(data_root, "x", attempt.attempt_id).phase == "FAILED"
    assert start_authorization_attempt(data_root, "x").phase == "WAITING_FOR_AUTHORIZATION"
