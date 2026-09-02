# ruff: noqa: E501
"""Shared account and capability truth for Creator platform integrations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from urllib.parse import parse_qs, urlencode, urlsplit

from soloscale.desktop_credentials import github_access_token_is_configured
from soloscale.github_connect import GitHubConnectError, GitHubConnectionStore
from soloscale.youtube_publishing import (
    YouTubePublishingError,
    load_youtube_accounts,
)

PlatformKey = Literal["youtube", "x", "linkedin", "douyin", "xiaohongshu", "github"]
ConnectionState = Literal[
    "DEVELOPER_NOT_CONFIGURED",
    "REQUIRED_SETUP",
    "READY_TO_CONNECT",
    "AUTH_STARTING",
    "WAITING_FOR_AUTHORIZATION",
    "VERIFYING_IDENTITY",
    "CONNECTED",
    "REAUTH_REQUIRED",
    "CANCELLED",
    "TIMED_OUT",
    "FAILED",
    "DISCONNECTED",
]
CapabilityKey = Literal[
    "authenticate",
    "read_identity",
    "publish_text",
    "publish_image",
    "publish_video",
    "repo_read",
    "repo_write",
    "refresh_token",
]
CapabilityState = Literal["AVAILABLE", "MISSING_SCOPE", "UNAVAILABLE", "REQUIRED_SETUP"]

_PLATFORMS: tuple[PlatformKey, ...] = (
    "youtube",
    "x",
    "linkedin",
    "douyin",
    "xiaohongshu",
    "github",
)
_TOKEN_FIELDS = frozenset(
    {"access_token", "refresh_token", "id_token", "client_secret", "app_secret"}
)
_TEXT_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,500}$")
_EXTERNAL_ID_RE = re.compile(r"^[^\s/\\]{1,200}$")
_X_SCOPES = frozenset({"tweet.read", "users.read", "tweet.write", "offline.access"})
_LINKEDIN_SCOPES = frozenset({"openid", "profile", "w_member_social"})
_YOUTUBE_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
    }
)


class PlatformAccountError(ValueError):
    """A sanitized platform-account boundary failure."""


@dataclass(frozen=True)
class ProviderDefinition:
    platform: PlatformKey
    auth_type: str
    required_fields: tuple[str, ...]
    scopes: tuple[str, ...]
    publish_capability: CapabilityKey | None


PROVIDERS: dict[PlatformKey, ProviderDefinition] = {
    "youtube": ProviderDefinition(
        "youtube", "GOOGLE_DESKTOP_OAUTH", ("credential_file",),
        tuple(sorted(_YOUTUBE_SCOPES)), "publish_video"
    ),
    "x": ProviderDefinition(
        "x", "OAUTH2_PKCE", ("client_id", "redirect_uri"),
        tuple(sorted(_X_SCOPES)), "publish_text"
    ),
    "linkedin": ProviderDefinition(
        "linkedin", "OAUTH2_AUTHORIZATION_CODE",
        ("client_id", "client_secret", "redirect_uri"),
        tuple(sorted(_LINKEDIN_SCOPES)), "publish_text"
    ),
    "douyin": ProviderDefinition(
        "douyin", "OAUTH2_AUTHORIZATION_CODE",
        ("client_key", "client_secret", "redirect_uri"),
        ("user_info", "video.create"), "publish_video"
    ),
    "xiaohongshu": ProviderDefinition(
        "xiaohongshu", "OAUTH2_DEVICE", ("app_id", "app_secret"),
        ("basic_info",), None
    ),
    "github": ProviderDefinition(
        "github", "GITHUB_DEVICE_FLOW", ("native_client_id",), (), None
    ),
}


@dataclass(frozen=True)
class DeveloperConfig:
    platform: PlatformKey
    values: Mapping[str, str]
    source: Literal["environment", "local", "native", "missing"]
    configured: bool
    missing_fields: tuple[str, ...]
    required_setup: str | None = None


@dataclass(frozen=True)
class ConnectedIdentity:
    platform: PlatformKey
    external_account_id: str
    display_name: str
    handle: str
    avatar_url: str | None
    scopes: tuple[str, ...]
    token_reference: str
    connected_at: str


@dataclass(frozen=True)
class Capability:
    key: CapabilityKey
    state: CapabilityState
    reason: str = ""


@dataclass(frozen=True)
class PlatformAccountSnapshot:
    platform: PlatformKey
    developer_config: DeveloperConfig
    connection_state: ConnectionState
    identities: tuple[ConnectedIdentity, ...]
    capabilities: tuple[Capability, ...]


@dataclass(frozen=True)
class AuthorizationAttempt:
    attempt_id: str
    platform: PlatformKey
    state_hash: str
    code_verifier: str | None
    authorization_url: str | None
    phase: ConnectionState
    created_at: str
    updated_at: str


def platform_keys() -> tuple[PlatformKey, ...]:
    return _PLATFORMS


def provider_label(platform: PlatformKey, locale: str) -> str:
    if platform == "douyin":
        return "抖音" if locale == "zh-CN" else "Douyin"
    if platform == "xiaohongshu":
        return "小红书" if locale == "zh-CN" else "rednote"
    return {
        "youtube": "YouTube",
        "x": "X",
        "linkedin": "LinkedIn",
        "github": "GitHub",
    }[platform]


def _integration_root(data_root: Path, platform: PlatformKey) -> Path:
    return data_root.expanduser().absolute() / "integrations" / platform


def _config_path(data_root: Path, platform: PlatformKey) -> Path:
    return _integration_root(data_root, platform) / "config.json"


def _accounts_path(data_root: Path, platform: PlatformKey) -> Path:
    return _integration_root(data_root, platform) / "accounts.json"


def _attempt_path(data_root: Path, platform: PlatformKey, attempt_id: str) -> Path:
    return _integration_root(data_root, platform) / "pending" / f"{attempt_id}.json"


def _current_attempt_path(data_root: Path, platform: PlatformKey) -> Path:
    return _integration_root(data_root, platform) / "pending" / "current.json"


def _safe_text(value: object, field: str, *, optional: bool = False) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, str) or value != value.strip() or not value:
        raise PlatformAccountError(f"Invalid {field}")
    if _TEXT_RE.fullmatch(value) is None:
        raise PlatformAccountError(f"Invalid {field}")
    return value


def _safe_url(value: str, field: str, *, https_only: bool = True) -> str:
    parsed = urlsplit(_safe_text(value, field))
    allowed_schemes = {"https"} if https_only else {"http", "https"}
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise PlatformAccountError(f"Invalid {field}")
    return value


def _private_write(path: Path, payload: Mapping[str, object]) -> None:
    root = path.parent
    if path.is_symlink() or root.is_symlink():
        raise PlatformAccountError("Integration storage path is unsafe")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=root)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise PlatformAccountError("Integration state could not be saved") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _load_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise PlatformAccountError("Integration state is unsafe")
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise PlatformAccountError("Integration state permissions are unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlatformAccountError("Integration state is invalid") from exc
    if not isinstance(payload, dict):
        raise PlatformAccountError("Integration state is invalid")
    return cast(dict[str, object], payload)


def _env_mapping(platform: PlatformKey, environ: Mapping[str, str]) -> dict[str, str]:
    names: dict[PlatformKey, dict[str, str]] = {
        "youtube": {},
        "x": {"client_id": "X_CLIENT_ID", "redirect_uri": "X_REDIRECT_URI"},
        "linkedin": {
            "client_id": "LINKEDIN_CLIENT_ID",
            "client_secret": "LINKEDIN_CLIENT_SECRET",
            "redirect_uri": "LINKEDIN_REDIRECT_URI",
        },
        "douyin": {
            "client_key": "DOUYIN_CLIENT_KEY",
            "client_secret": "DOUYIN_CLIENT_SECRET",
            "redirect_uri": "DOUYIN_REDIRECT_URI",
        },
        "xiaohongshu": {
            "app_id": "XIAOHONGSHU_APP_ID",
            "app_secret": "XIAOHONGSHU_APP_SECRET",
        },
        "github": {},
    }
    return {
        field: value
        for field, env_name in names[platform].items()
        if (value := environ.get(env_name, "").strip())
    }


def load_developer_config(
    data_root: Path,
    platform: PlatformKey,
    *,
    environ: Mapping[str, str] | None = None,
    github_native_configured: bool | None = None,
) -> DeveloperConfig:
    """Load environment-first readiness without returning secrets to UI callers."""
    if platform == "youtube":
        from soloscale.youtube_publishing import youtube_configuration_state

        state = youtube_configuration_state(data_root)
        return DeveloperConfig(
            platform, {}, "local", state == "CONFIGURED",
            () if state == "CONFIGURED" else ("credential_file",),
            None if state == "CONFIGURED" else state,
        )
    if platform == "github":
        source = environ if environ is not None else os.environ
        ready = bool(github_native_configured) or source.get(
            "SOLOSCALE_GITHUB_NATIVE_AVAILABLE"
        ) == "1"
        return DeveloperConfig(
            platform, {}, "native" if ready else "missing", ready,
            () if ready else ("native_client_id",),
        )

    source = environ if environ is not None else os.environ
    env_values = _env_mapping(platform, source)
    local_payload = _load_object(_config_path(data_root, platform))
    local_values = local_payload.get("values", {})
    if not isinstance(local_values, dict):
        local_values = {}
    values = {
        key: str(value)
        for key, value in cast(dict[object, object], local_values).items()
        if isinstance(key, str) and isinstance(value, str) and value
    }
    config_source: Literal["environment", "local", "missing"] = "local" if values else "missing"
    if env_values:
        values.update(env_values)
        config_source = "environment"
    required = PROVIDERS[platform].required_fields
    missing = tuple(field for field in required if not values.get(field, "").strip())
    required_setup: str | None = None
    if not missing and platform == "x":
        redirect = _safe_url(values["redirect_uri"], "redirect_uri", https_only=False)
        parsed = urlsplit(redirect)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port is None:
            required_setup = "X redirect must be an exact loopback callback"
    elif not missing and platform in {"linkedin", "douyin"}:
        redirect = _safe_url(values["redirect_uri"], "redirect_uri")
        if redirect.startswith("https://solo-scale-ai-os.vercel.app/") is False:
            required_setup = "Register a supported HTTPS OAuth callback/bridge"
    return DeveloperConfig(
        platform, values, config_source, not missing and required_setup is None, missing,
        required_setup,
    )


def save_developer_config(
    data_root: Path,
    platform: PlatformKey,
    values: Mapping[str, str],
) -> Path:
    """Persist only provider-specific developer configuration with private permissions."""
    if platform in {"youtube", "github"}:
        raise PlatformAccountError("This provider uses its existing native configuration")
    allowed = set(PROVIDERS[platform].required_fields)
    if set(values) - allowed:
        raise PlatformAccountError("Unsupported developer configuration field")
    normalized = {field: _safe_text(values.get(field), field) for field in allowed}
    if "redirect_uri" in normalized:
        _safe_url(
            normalized["redirect_uri"], "redirect_uri",
            https_only=platform in {"linkedin", "douyin"},
        )
    path = _config_path(data_root, platform)
    _private_write(path, {"schema_version": "1.0", "platform": platform, "values": normalized})
    return path


def _identity_from_payload(platform: PlatformKey, raw: Mapping[str, object]) -> ConnectedIdentity:
    external_id = _safe_text(raw.get("external_account_id"), "external_account_id")
    if _EXTERNAL_ID_RE.fullmatch(external_id) is None:
        raise PlatformAccountError("Invalid external_account_id")
    avatar = raw.get("avatar_url")
    avatar_url = _safe_url(avatar, "avatar_url") if isinstance(avatar, str) and avatar else None
    scopes = raw.get("scopes", [])
    if not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes):
        raise PlatformAccountError("Invalid scopes")
    return ConnectedIdentity(
        platform=platform,
        external_account_id=external_id,
        display_name=_safe_text(raw.get("display_name"), "display_name"),
        handle=_safe_text(raw.get("handle", ""), "handle", optional=True),
        avatar_url=avatar_url,
        scopes=tuple(sorted(set(cast(list[str], scopes)))),
        token_reference=_safe_text(raw.get("token_reference"), "token_reference"),
        connected_at=_safe_text(raw.get("connected_at"), "connected_at"),
    )


def load_connected_identities(data_root: Path, platform: PlatformKey) -> tuple[ConnectedIdentity, ...]:
    payload = _load_object(_accounts_path(data_root, platform))
    entries = payload.get("accounts", [])
    if entries in (None, []):
        return ()
    if not isinstance(entries, list):
        raise PlatformAccountError("Account metadata is invalid")
    return tuple(
        _identity_from_payload(platform, cast(Mapping[str, object], raw))
        for raw in entries
        if isinstance(raw, dict)
    )


def save_connected_identity(
    data_root: Path,
    identity: ConnectedIdentity,
    *,
    token_payload: Mapping[str, object],
) -> ConnectedIdentity:
    """Write credentials separately, then upsert non-secret verified identity metadata."""
    if not token_payload or _TOKEN_FIELDS.isdisjoint(token_payload):
        raise PlatformAccountError("Provider token response is incomplete")
    reference_name = hashlib.sha256(
        f"{identity.platform}:{identity.external_account_id}".encode()
    ).hexdigest()[:24]
    token_reference = f"tokens/{reference_name}.json"
    token_path = _integration_root(data_root, identity.platform) / token_reference
    _private_write(token_path, dict(token_payload))
    stored = replace(identity, token_reference=token_reference)
    accounts = {
        item.external_account_id: item
        for item in load_connected_identities(data_root, identity.platform)
    }
    accounts[stored.external_account_id] = stored
    metadata = {
        "schema_version": "1.0",
        "accounts": [asdict(accounts[key]) for key in sorted(accounts)],
    }
    if any(_TOKEN_FIELDS.intersection(item) for item in metadata["accounts"]):
        raise PlatformAccountError("Credential data cannot be written to account metadata")
    _private_write(_accounts_path(data_root, identity.platform), metadata)
    return stored


def disconnect_identity(data_root: Path, platform: PlatformKey, external_account_id: str) -> bool:
    accounts = list(load_connected_identities(data_root, platform))
    selected = next((item for item in accounts if item.external_account_id == external_account_id), None)
    if selected is None:
        return False
    token_path = _integration_root(data_root, platform) / selected.token_reference
    root = _integration_root(data_root, platform).resolve()
    try:
        if token_path.resolve().is_relative_to(root):
            token_path.unlink(missing_ok=True)
    except OSError as exc:
        raise PlatformAccountError("Provider token could not be removed") from exc
    remaining = [item for item in accounts if item.external_account_id != external_account_id]
    _private_write(
        _accounts_path(data_root, platform),
        {"schema_version": "1.0", "accounts": [asdict(item) for item in remaining]},
    )
    return True


def _youtube_identities(data_root: Path) -> tuple[ConnectedIdentity, ...]:
    try:
        accounts = load_youtube_accounts(data_root)
    except YouTubePublishingError:
        return ()
    identities: list[ConnectedIdentity] = []
    for account in accounts:
        scopes: tuple[str, ...] = ()
        try:
            token = _load_object(
                _integration_root(data_root, "youtube") / account.token_file
            )
            raw_scopes = token.get("scopes", [])
            if isinstance(raw_scopes, list) and all(isinstance(value, str) for value in raw_scopes):
                scopes = tuple(sorted(set(cast(list[str], raw_scopes))))
        except PlatformAccountError:
            pass
        identities.append(
            ConnectedIdentity(
                platform="youtube",
                external_account_id=account.channel_id,
                display_name=account.channel_title,
                handle="",
                avatar_url=None,
                scopes=scopes,
                token_reference=account.token_file,
                connected_at=account.connected_at,
            )
        )
    return tuple(identities)


def _github_identities(data_root: Path) -> tuple[ConnectedIdentity, ...]:
    try:
        state = GitHubConnectionStore(data_root).load()
    except GitHubConnectError:
        state = None
    if state is None:
        return ()
    return (
        ConnectedIdentity(
            platform="github",
            external_account_id=str(state.account_id),
            display_name=state.account_login,
            handle=state.account_login,
            avatar_url=None,
            scopes=(),
            token_reference="keychain:github/default",
            connected_at=state.inventory_refreshed_at.isoformat(),
        ),
    )


def _capabilities(platform: PlatformKey, identities: Sequence[ConnectedIdentity]) -> tuple[Capability, ...]:
    scopes = set().union(*(set(item.scopes) for item in identities)) if identities else set()
    base = [
        Capability("authenticate", "AVAILABLE" if identities else "REQUIRED_SETUP"),
        Capability("read_identity", "AVAILABLE" if identities else "REQUIRED_SETUP"),
    ]
    if platform == "youtube":
        base.append(Capability("publish_video", "AVAILABLE" if _YOUTUBE_SCOPES.issubset(scopes) else "MISSING_SCOPE", "youtube.upload + youtube.readonly"))
        base.append(Capability("refresh_token", "AVAILABLE" if identities else "REQUIRED_SETUP"))
    elif platform == "x":
        base.append(Capability("publish_text", "AVAILABLE" if "tweet.write" in scopes else "MISSING_SCOPE", "tweet.write"))
        base.append(Capability("publish_image", "UNAVAILABLE", "media.write is outside this connection scope"))
        base.append(Capability("refresh_token", "AVAILABLE" if "offline.access" in scopes else "MISSING_SCOPE", "offline.access"))
    elif platform == "linkedin":
        base.append(Capability("publish_text", "AVAILABLE" if "w_member_social" in scopes else "MISSING_SCOPE", "w_member_social"))
    elif platform == "douyin":
        base.append(Capability("publish_video", "AVAILABLE" if "video.create" in scopes else "MISSING_SCOPE", "video.create requires platform approval and user authorization"))
    elif platform == "xiaohongshu":
        base.append(Capability("publish_text", "UNAVAILABLE", "Official write_notes capability is not generally available"))
        base.append(Capability("refresh_token", "AVAILABLE" if identities else "REQUIRED_SETUP"))
    elif platform == "github":
        base.extend((
            Capability("repo_read", "AVAILABLE" if identities else "REQUIRED_SETUP"),
            Capability("repo_write", "UNAVAILABLE", "SoloScale GitHub integration is read-only"),
        ))
    return tuple(base)


def _token_requires_reauthorization(
    data_root: Path, identity: ConnectedIdentity
) -> bool:
    if identity.platform == "github":
        return not github_access_token_is_configured()
    token_path = _integration_root(data_root, identity.platform) / identity.token_reference
    try:
        token = _load_object(token_path)
    except PlatformAccountError:
        return True
    raw_expiry = token.get("expires_at", token.get("expiry"))
    if not isinstance(raw_expiry, str) or not raw_expiry:
        return False
    try:
        expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expiry.tzinfo is None:
        return True
    if datetime.now(UTC) < expiry.astimezone(UTC):
        return False
    refresh_token = token.get("refresh_token")
    return not isinstance(refresh_token, str) or not refresh_token.strip()


def platform_snapshot(
    data_root: Path,
    platform: PlatformKey,
    *,
    environ: Mapping[str, str] | None = None,
    github_native_configured: bool | None = None,
) -> PlatformAccountSnapshot:
    if platform == "youtube":
        identities = _youtube_identities(data_root)
    elif platform == "github":
        identities = _github_identities(data_root)
        github_native_configured = (
            github_access_token_is_configured()
            if github_native_configured is None
            else github_native_configured
        )
    else:
        identities = load_connected_identities(data_root, platform)
    config = load_developer_config(
        data_root, platform, environ=environ,
        github_native_configured=github_native_configured,
    )
    state: ConnectionState
    requires_reauthorization = bool(identities) and (
        not bool(github_native_configured)
        if platform == "github"
        else any(
            _token_requires_reauthorization(data_root, identity)
            for identity in identities
        )
    )
    if requires_reauthorization:
        state = "REAUTH_REQUIRED"
    elif identities:
        state = "CONNECTED"
    elif config.required_setup:
        state = "REQUIRED_SETUP"
    elif config.configured:
        state = "READY_TO_CONNECT"
    else:
        state = "DEVELOPER_NOT_CONFIGURED"
    if platform == "github" and identities and not (
        github_native_configured
        or (environ if environ is not None else os.environ).get(
            "SOLOSCALE_GITHUB_NATIVE_AVAILABLE"
        )
        == "1"
    ):
        state = "REAUTH_REQUIRED"
    return PlatformAccountSnapshot(platform, config, state, identities, _capabilities(platform, identities))


def all_platform_snapshots(
    data_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
    github_native_configured: bool | None = None,
) -> tuple[PlatformAccountSnapshot, ...]:
    return tuple(
        platform_snapshot(
            data_root, platform, environ=environ,
            github_native_configured=github_native_configured,
        )
        for platform in _PLATFORMS
    )


def _identity_has_capability(
    platform: PlatformKey,
    identity: ConnectedIdentity,
    capability: CapabilityKey,
) -> bool:
    scopes = set(identity.scopes)
    if platform == "youtube" and capability == "publish_video":
        return _YOUTUBE_SCOPES.issubset(scopes)
    if platform == "x" and capability == "publish_text":
        return "tweet.write" in scopes
    if platform == "linkedin" and capability == "publish_text":
        return "w_member_social" in scopes
    if platform == "douyin" and capability == "publish_video":
        return "video.create" in scopes
    return False


def eligible_publish_identities(data_root: Path) -> dict[PlatformKey, tuple[ConnectedIdentity, ...]]:
    eligible: dict[PlatformKey, tuple[ConnectedIdentity, ...]] = {}
    for snapshot in all_platform_snapshots(data_root):
        required = PROVIDERS[snapshot.platform].publish_capability
        if required is None:
            continue
        identities = tuple(
            identity
            for identity in snapshot.identities
            if _identity_has_capability(snapshot.platform, identity, required)
            and not _token_requires_reauthorization(data_root, identity)
        )
        if identities:
            eligible[snapshot.platform] = identities
    return eligible


def _attempt_id(platform: PlatformKey) -> str:
    return f"{platform}-auth-{secrets.token_hex(6)}"


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def start_authorization_attempt(
    data_root: Path,
    platform: Literal["x", "linkedin", "douyin"],
    *,
    now: datetime | None = None,
) -> AuthorizationAttempt:
    config = load_developer_config(data_root, platform)
    if not config.configured:
        raise PlatformAccountError(config.required_setup or "Developer integration is not configured")
    created = now or datetime.now(UTC)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64) if platform == "x" else None
    values = config.values
    if platform == "x":
        query = {
            "response_type": "code",
            "client_id": values["client_id"],
            "redirect_uri": values["redirect_uri"],
            "scope": " ".join(sorted(_X_SCOPES)),
            "state": state,
            "code_challenge": _pkce_challenge(cast(str, verifier)),
            "code_challenge_method": "S256",
        }
        endpoint = "https://x.com/i/oauth2/authorize"
    elif platform == "linkedin":
        query = {
            "response_type": "code",
            "client_id": values["client_id"],
            "redirect_uri": values["redirect_uri"],
            "scope": "openid profile w_member_social",
            "state": state,
        }
        endpoint = "https://www.linkedin.com/oauth/v2/authorization"
    else:
        query = {
            "client_key": values["client_key"],
            "response_type": "code",
            "scope": ",".join(PROVIDERS["douyin"].scopes),
            "redirect_uri": values["redirect_uri"],
            "state": state,
        }
        endpoint = "https://open.douyin.com/platform/oauth/connect/"
    attempt = AuthorizationAttempt(
        attempt_id=_attempt_id(platform),
        platform=platform,
        state_hash=hashlib.sha256(state.encode()).hexdigest(),
        code_verifier=verifier,
        authorization_url=f"{endpoint}?{urlencode(query)}",
        phase="WAITING_FOR_AUTHORIZATION",
        created_at=created.isoformat(),
        updated_at=created.isoformat(),
    )
    _private_write(
        _attempt_path(data_root, platform, attempt.attempt_id),
        {**asdict(attempt), "state": state},
    )
    _private_write(
        _current_attempt_path(data_root, platform),
        {"schema_version": "1.0", "attempt_id": attempt.attempt_id},
    )
    return replace(attempt, code_verifier=None)


def cancel_authorization_attempt(
    data_root: Path, platform: PlatformKey, attempt_id: str
) -> AuthorizationAttempt:
    _require_current_attempt(data_root, platform, attempt_id)
    attempt = _load_attempt(data_root, platform, attempt_id)
    updated = replace(
        attempt, phase="CANCELLED", authorization_url=None,
        code_verifier=None, updated_at=datetime.now(UTC).isoformat(),
    )
    _private_write(_attempt_path(data_root, platform, attempt_id), asdict(updated))
    return updated


def load_authorization_attempt(
    data_root: Path, platform: PlatformKey, attempt_id: str
) -> AuthorizationAttempt:
    """Return one non-secret pending/terminal attempt for the Accounts UI."""
    attempt = _load_attempt(data_root, platform, attempt_id)
    created = datetime.fromisoformat(attempt.created_at)
    if (
        attempt.phase == "WAITING_FOR_AUTHORIZATION"
        and datetime.now(UTC) - created > timedelta(minutes=5)
    ):
        attempt = replace(
            attempt,
            phase="TIMED_OUT",
            authorization_url=None,
            code_verifier=None,
            updated_at=datetime.now(UTC).isoformat(),
        )
        _private_write(_attempt_path(data_root, platform, attempt_id), asdict(attempt))
    return replace(attempt, code_verifier=None)


def consume_authorization_callback(
    data_root: Path,
    platform: Literal["x", "linkedin", "douyin"],
    attempt_id: str,
    *,
    returned_state: str,
    code: str,
    exchange: Callable[[DeveloperConfig, str, str | None], Mapping[str, object]],
    resolve_identity: Callable[[Mapping[str, object]], ConnectedIdentity],
    now: datetime | None = None,
) -> ConnectedIdentity:
    """Consume one current attempt; save only after token and identity validation succeed."""
    _require_current_attempt(data_root, platform, attempt_id)
    attempt = _load_attempt(data_root, platform, attempt_id)
    current = now or datetime.now(UTC)
    created = datetime.fromisoformat(attempt.created_at)
    if attempt.phase != "WAITING_FOR_AUTHORIZATION" or current - created > timedelta(minutes=5):
        raise PlatformAccountError("Authorization attempt is no longer current")
    payload = _load_object(_attempt_path(data_root, platform, attempt_id))
    expected_hash = payload.get("state_hash")
    actual_hash = hashlib.sha256(_safe_text(returned_state, "state").encode()).hexdigest()
    if not isinstance(expected_hash, str) or not hmac.compare_digest(expected_hash, actual_hash):
        raise PlatformAccountError("OAuth state is invalid")
    verifier = payload.get("code_verifier")
    if verifier is not None and not isinstance(verifier, str):
        raise PlatformAccountError("PKCE verifier is invalid")
    config = load_developer_config(data_root, platform)
    verifying = replace(
        attempt,
        phase="VERIFYING_IDENTITY",
        authorization_url=None,
        updated_at=current.isoformat(),
    )
    _private_write(_attempt_path(data_root, platform, attempt_id), asdict(verifying))
    try:
        token_payload = exchange(config, _safe_text(code, "code"), verifier)
        identity = resolve_identity(token_payload)
        if identity.platform != platform:
            raise PlatformAccountError(
                "Provider identity does not match the authorization"
            )
        stored = save_connected_identity(
            data_root, identity, token_payload=token_payload
        )
    except (OSError, PlatformAccountError):
        failed = replace(
            verifying,
            phase="FAILED",
            code_verifier=None,
            updated_at=datetime.now(UTC).isoformat(),
        )
        _private_write(_attempt_path(data_root, platform, attempt_id), asdict(failed))
        raise
    _attempt_path(data_root, platform, attempt_id).unlink(missing_ok=True)
    _current_attempt_path(data_root, platform).unlink(missing_ok=True)
    return stored


def _request_json(request: urllib.request.Request) -> dict[str, object]:
    parsed = urlsplit(request.full_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise PlatformAccountError("Provider endpoint is invalid")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(1024 * 1024 + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PlatformAccountError("Provider authorization request failed") from exc
    if len(raw) > 1024 * 1024:
        raise PlatformAccountError("Provider authorization response is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PlatformAccountError("Provider authorization response is invalid") from exc
    if not isinstance(payload, dict):
        raise PlatformAccountError("Provider authorization response is invalid")
    return cast(dict[str, object], payload)


def _post_form_json(url: str, fields: Mapping[str, str]) -> dict[str, object]:
    return _request_json(
        urllib.request.Request(
            url,
            data=urlencode(fields).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
    )


def _bearer_json(url: str, access_token: str) -> dict[str, object]:
    return _request_json(
        urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
        )
    )


def _normalized_token(
    platform: Literal["x", "linkedin", "douyin"],
    payload: Mapping[str, object],
) -> dict[str, object]:
    token = dict(payload)
    _safe_text(token.get("access_token"), "access_token")
    if not isinstance(token.get("scope"), str):
        token["scope"] = ""
    expires_in = token.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        token["expires_at"] = (
            datetime.now(UTC) + timedelta(seconds=float(expires_in))
        ).isoformat()
    return token


def exchange_provider_authorization_code(
    config: DeveloperConfig, code: str, verifier: str | None
) -> Mapping[str, object]:
    values = config.values
    if config.platform == "x":
        if verifier is None:
            raise PlatformAccountError("PKCE verifier is unavailable")
        payload = _post_form_json(
            "https://api.x.com/2/oauth2/token",
            {
                "code": code,
                "grant_type": "authorization_code",
                "client_id": values["client_id"],
                "redirect_uri": values["redirect_uri"],
                "code_verifier": verifier,
            },
        )
    elif config.platform == "linkedin":
        payload = _post_form_json(
            "https://www.linkedin.com/oauth/v2/accessToken",
            {
                "code": code,
                "grant_type": "authorization_code",
                "client_id": values["client_id"],
                "client_secret": values["client_secret"],
                "redirect_uri": values["redirect_uri"],
            },
        )
    elif config.platform == "douyin":
        outer = _post_form_json(
            "https://open.douyin.com/oauth/access_token/",
            {
                "code": code,
                "grant_type": "authorization_code",
                "client_key": values["client_key"],
                "client_secret": values["client_secret"],
            },
        )
        nested = outer.get("data")
        payload = cast(dict[str, object], nested) if isinstance(nested, dict) else outer
    else:
        raise PlatformAccountError("Unsupported OAuth provider")
    return _normalized_token(config.platform, payload)


def resolve_authorized_identity(
    platform: Literal["x", "linkedin", "douyin"],
    token: Mapping[str, object],
) -> ConnectedIdentity:
    access_token = _safe_text(token.get("access_token"), "access_token")
    if platform == "x":
        return parse_x_identity(
            token,
            _bearer_json(
                "https://api.x.com/2/users/me?user.fields=id,name,username,profile_image_url",
                access_token,
            ),
        )
    if platform == "linkedin":
        return parse_linkedin_identity(
            token,
            _bearer_json("https://api.linkedin.com/v2/userinfo", access_token),
        )
    open_id = _safe_text(token.get("open_id"), "open_id")
    return parse_douyin_identity(
        token,
        _post_form_json(
            "https://open.douyin.com/oauth/userinfo/",
            {"access_token": access_token, "open_id": open_id},
        ),
    )


def complete_authorization_response(
    data_root: Path,
    platform: Literal["x", "linkedin", "douyin"],
    attempt_id: str,
    authorization_response: str,
) -> ConnectedIdentity:
    config = load_developer_config(data_root, platform)
    expected = urlsplit(config.values.get("redirect_uri", ""))
    returned = urlsplit(
        _safe_url(
            authorization_response,
            "authorization_response",
            https_only=False,
        )
    )
    if (returned.scheme, returned.hostname, returned.port, returned.path) != (
        expected.scheme,
        expected.hostname,
        expected.port,
        expected.path,
    ):
        raise PlatformAccountError(
            "OAuth callback does not match the registered redirect"
        )
    query = parse_qs(returned.query)
    code = query.get("code", [""])[0]
    state = query.get("state", [""])[0]
    if not code or not state:
        raise PlatformAccountError("OAuth callback is incomplete")
    return consume_authorization_callback(
        data_root,
        platform,
        attempt_id,
        returned_state=state,
        code=code,
        exchange=exchange_provider_authorization_code,
        resolve_identity=lambda payload: resolve_authorized_identity(
            platform, payload
        ),
    )


def _require_current_attempt(
    data_root: Path, platform: PlatformKey, attempt_id: str
) -> None:
    pointer = _load_object(_current_attempt_path(data_root, platform))
    current = pointer.get("attempt_id")
    if not isinstance(current, str) or not hmac.compare_digest(current, attempt_id):
        raise PlatformAccountError("Authorization attempt was replaced by a newer attempt")


def _load_attempt(data_root: Path, platform: PlatformKey, attempt_id: str) -> AuthorizationAttempt:
    if re.fullmatch(r"[a-z]+-auth-[0-9a-f]{12}", attempt_id) is None:
        raise PlatformAccountError("Authorization attempt is invalid")
    payload = _load_object(_attempt_path(data_root, platform, attempt_id))
    try:
        return AuthorizationAttempt(
            attempt_id=_safe_text(payload.get("attempt_id"), "attempt_id"),
            platform=cast(PlatformKey, _safe_text(payload.get("platform"), "platform")),
            state_hash=_safe_text(payload.get("state_hash"), "state_hash"),
            code_verifier=(
                cast(str, payload["code_verifier"])
                if isinstance(payload.get("code_verifier"), str)
                else None
            ),
            authorization_url=(
                cast(str, payload["authorization_url"])
                if isinstance(payload.get("authorization_url"), str)
                else None
            ),
            phase=cast(ConnectionState, _safe_text(payload.get("phase"), "phase")),
            created_at=_safe_text(payload.get("created_at"), "created_at"),
            updated_at=_safe_text(payload.get("updated_at"), "updated_at"),
        )
    except KeyError as exc:
        raise PlatformAccountError("Authorization attempt is invalid") from exc


def rednote_device_authorization_request(config: DeveloperConfig) -> tuple[str, dict[str, object]]:
    if config.platform != "xiaohongshu" or not config.configured:
        raise PlatformAccountError("rednote developer integration is not configured")
    return (
        "https://openaccount.xiaohongshu.com/api/sns/v1/oauth2/device/code",
        {
            "app_id": config.values["app_id"],
            "app_secret": config.values["app_secret"],
            "scopes": ["basic_info"],
        },
    )


def require_publish_identity(
    data_root: Path, platform: Literal["x", "linkedin", "youtube", "douyin"]
) -> ConnectedIdentity:
    identities = eligible_publish_identities(data_root).get(platform, ())
    if not identities:
        raise PlatformAccountError(
            "No connected account has the required publishing capability"
        )
    return identities[0]


def provider_account_reference(
    platform: Literal["x", "linkedin"], external_account_id: str
) -> str:
    if platform not in {"x", "linkedin"}:
        raise PlatformAccountError("Provider account reference is unsupported")
    return hashlib.sha256(
        _safe_text(external_account_id, "external_account_id").encode("utf-8")
    ).hexdigest()[:20]


def require_publish_account_reference(
    data_root: Path,
    platform: Literal["x", "linkedin"],
    account_reference: str,
) -> ConnectedIdentity:
    expected = _safe_text(account_reference, "account_reference")
    for identity in eligible_publish_identities(data_root).get(platform, ()):
        actual = provider_account_reference(platform, identity.external_account_id)
        if hmac.compare_digest(actual, expected):
            return identity
    raise PlatformAccountError(
        "The publication preview is not bound to an eligible connected account"
    )


def parse_douyin_identity(token: Mapping[str, object], payload: Mapping[str, object]) -> ConnectedIdentity:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise PlatformAccountError("Douyin identity response is invalid")
    raw = cast(Mapping[str, object], data)
    scopes = str(token.get("scope", "")).replace(",", " ").split()
    return ConnectedIdentity(
        "douyin", _safe_text(raw.get("open_id"), "open_id"),
        _safe_text(raw.get("nickname"), "nickname"), "",
        _safe_url(cast(str, raw["avatar"]), "avatar") if isinstance(raw.get("avatar"), str) and raw["avatar"] else None,
        tuple(sorted(set(scopes))), "pending", datetime.now(UTC).isoformat(),
    )


def parse_x_identity(
    token: Mapping[str, object], payload: Mapping[str, object]
) -> ConnectedIdentity:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise PlatformAccountError("X identity response is invalid")
    raw = cast(Mapping[str, object], data)
    scopes = str(token.get("scope", "")).replace(",", " ").split()
    return ConnectedIdentity(
        "x",
        _safe_text(raw.get("id"), "id"),
        _safe_text(raw.get("name"), "name"),
        _safe_text(raw.get("username"), "username"),
        None,
        tuple(sorted(set(scopes))),
        "pending",
        datetime.now(UTC).isoformat(),
    )


def parse_linkedin_identity(
    token: Mapping[str, object], payload: Mapping[str, object]
) -> ConnectedIdentity:
    scopes = str(token.get("scope", "")).replace(",", " ").split()
    subject = _safe_text(payload.get("sub"), "sub")
    avatar = payload.get("picture")
    return ConnectedIdentity(
        "linkedin",
        subject,
        _safe_text(payload.get("name"), "name"),
        "",
        _safe_url(avatar, "picture") if isinstance(avatar, str) and avatar else None,
        tuple(sorted(set(scopes))),
        "pending",
        datetime.now(UTC).isoformat(),
    )


def parse_rednote_identity(token: Mapping[str, object], payload: Mapping[str, object]) -> ConnectedIdentity:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise PlatformAccountError("rednote identity response is invalid")
    raw = cast(Mapping[str, object], data)
    scopes = token.get("scope", [])
    if not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes):
        raise PlatformAccountError("rednote token scope is invalid")
    return ConnectedIdentity(
        "xiaohongshu", _safe_text(raw.get("open_id"), "open_id"),
        _safe_text(raw.get("nickname"), "nickname"), "",
        _safe_url(cast(str, raw["avatar"]), "avatar") if isinstance(raw.get("avatar"), str) and raw["avatar"] else None,
        tuple(sorted(set(cast(list[str], scopes)))), "pending", datetime.now(UTC).isoformat(),
    )
