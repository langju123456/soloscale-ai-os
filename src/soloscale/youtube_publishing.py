"""Local multi-channel YouTube OAuth and background upload boundary."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
import tempfile
import threading
import time
import webbrowser
import wsgiref.simple_server
import wsgiref.util
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from soloscale.content_distribution import load_distribution_package
from soloscale.content_workspace import content_run_directory

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_SCOPES = (YOUTUBE_UPLOAD_SCOPE, YOUTUBE_READONLY_SCOPE)
YOUTUBE_CLIENT_SECRET_ENV = "SOLOSCALE_YOUTUBE_CLIENT_SECRET_FILE"

YouTubePrivacy = Literal["private", "unlisted", "public"]
YouTubeJobKind = Literal["connect", "upload"]
YouTubeJobPhase = Literal[
    "WAITING",
    "AUTHENTICATING",
    "STARTING",
    "WAITING_FOR_AUTHORIZATION",
    "COMPLETING",
    "UPLOADING",
    "SUCCESS",
    "CANCELLED",
    "TIMED_OUT",
    "FAILED",
]
OAuthStatusCallback = Callable[[Literal["waiting", "completing"], str | None], None]

_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{8,96}$")
_RUN_ID_RE = re.compile(r"^content-[A-Za-z0-9._+-]{3,160}$")
_JOB_ID_RE = re.compile(r"^youtube-(?:auth|upload)-[a-f0-9]{12}$")
_CLIENT_SECRET_NAME = "client_secret.json"
_ACCOUNTS_NAME = "accounts.json"
_UPLOAD_METADATA_NAME = "27_youtube_upload.json"
_VIDEO_NAME = "21_creator_video_youtube.mp4"
_DEFAULT_OAUTH_TIMEOUT_SECONDS = 300.0
_ACTIVE_OAUTH_PHASES = {
    "STARTING",
    "AUTHENTICATING",
    "WAITING_FOR_AUTHORIZATION",
    "COMPLETING",
}


class YouTubePublishingError(ValueError):
    """Raised when YouTube configuration, authorization, or upload is invalid."""

    def __init__(self, message: str, *, code: str = "YOUTUBE_ERROR") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class YouTubeChannelAccount:
    channel_id: str
    channel_title: str
    token_file: str
    connected_at: str


@dataclass(frozen=True)
class YouTubeUploadRequest:
    run_id: str
    channel_id: str
    title: str
    description: str
    tags: tuple[str, ...]
    privacy_status: YouTubePrivacy


@dataclass(frozen=True)
class YouTubeUploadResult:
    video_id: str
    video_url: str
    uploaded_at: str


@dataclass(frozen=True)
class YouTubeJobSnapshot:
    job_id: str
    kind: YouTubeJobKind
    phase: YouTubeJobPhase
    created_at: str
    updated_at: str
    run_id: str | None = None
    channel_id: str | None = None
    progress_percent: int | None = None
    video_id: str | None = None
    video_url: str | None = None
    authorization_url: str | None = None
    channel_title: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class YouTubeProvider(Protocol):
    def authorize(
        self,
        client_secret_path: Path,
        *,
        status: OAuthStatusCallback,
        cancel_event: threading.Event,
        timeout_seconds: float,
    ) -> tuple[str, str, str]: ...

    def upload(
        self,
        *,
        credentials: object,
        video_path: Path,
        request: YouTubeUploadRequest,
        progress: Callable[[int | None], None],
    ) -> YouTubeUploadResult: ...


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _youtube_root(data_root: Path) -> Path:
    return data_root.expanduser().absolute() / "integrations" / "youtube"


def youtube_client_secret_path(data_root: Path) -> Path:
    configured = os.environ.get(YOUTUBE_CLIENT_SECRET_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().absolute()
    return _youtube_root(data_root) / _CLIENT_SECRET_NAME


def _private_directory(path: Path) -> None:
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise YouTubePublishingError(
            "YouTube local storage is unsafe", code="UNSAFE_LOCAL_STORAGE"
        )
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _atomic_private_json(path: Path, payload: dict[str, object]) -> None:
    _private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def validate_client_secret(path: Path) -> Path:
    selected = path.expanduser().absolute()
    if selected.is_symlink() or not selected.is_file():
        raise YouTubePublishingError(
            "Google OAuth Desktop credential JSON is missing",
            code="CREDENTIAL_JSON_MISSING",
        )
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise YouTubePublishingError(
            "Google OAuth Desktop credential JSON is invalid",
            code="INVALID_CREDENTIAL_JSON",
        ) from exc
    installed = payload.get("installed") if isinstance(payload, dict) else None
    required = {"client_id", "client_secret", "auth_uri", "token_uri", "redirect_uris"}
    if not isinstance(installed, dict) or not required <= set(installed):
        raise YouTubePublishingError(
            "Choose a Google OAuth credential created for a Desktop app",
            code="INVALID_CREDENTIAL_JSON",
        )
    return selected


def youtube_configuration_state(data_root: Path) -> str:
    try:
        validate_client_secret(youtube_client_secret_path(data_root))
        _google_modules()
    except YouTubePublishingError as exc:
        return exc.code
    return "CONFIGURED"


def _account_index_path(data_root: Path) -> Path:
    return _youtube_root(data_root) / _ACCOUNTS_NAME


def load_youtube_accounts(data_root: Path) -> tuple[YouTubeChannelAccount, ...]:
    path = _account_index_path(data_root)
    if not path.exists():
        return ()
    if path.is_symlink() or not path.is_file():
        raise YouTubePublishingError(
            "YouTube account index is unsafe", code="UNSAFE_LOCAL_STORAGE"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise YouTubePublishingError(
            "YouTube account index is invalid", code="ACCOUNT_INDEX_INVALID"
        ) from exc
    raw_accounts = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(raw_accounts, list):
        raise YouTubePublishingError(
            "YouTube account index is invalid", code="ACCOUNT_INDEX_INVALID"
        )
    accounts: list[YouTubeChannelAccount] = []
    for raw in raw_accounts:
        if not isinstance(raw, dict):
            raise YouTubePublishingError(
                "YouTube account index is invalid", code="ACCOUNT_INDEX_INVALID"
            )
        channel_id = str(raw.get("channel_id", ""))
        token_file = str(raw.get("token_file", ""))
        if (
            _CHANNEL_ID_RE.fullmatch(channel_id) is None
            or token_file != f"tokens/{channel_id}.json"
        ):
            raise YouTubePublishingError(
                "YouTube account index is invalid", code="ACCOUNT_INDEX_INVALID"
            )
        accounts.append(
            YouTubeChannelAccount(
                channel_id=channel_id,
                channel_title=str(raw.get("channel_title", ""))[:200],
                token_file=token_file,
                connected_at=str(raw.get("connected_at", "")),
            )
        )
    return tuple(sorted(accounts, key=lambda item: item.channel_title.casefold()))


def save_authorized_channel(
    data_root: Path,
    *,
    channel_id: str,
    channel_title: str,
    credential_json: str,
) -> YouTubeChannelAccount:
    if _CHANNEL_ID_RE.fullmatch(channel_id) is None:
        raise YouTubePublishingError(
            "Google returned an invalid YouTube channel ID", code="CHANNEL_ID_INVALID"
        )
    cleaned_title = channel_title.strip()
    if not cleaned_title or len(cleaned_title) > 200:
        raise YouTubePublishingError(
            "Google returned an invalid YouTube channel title",
            code="CHANNEL_IDENTITY_INVALID",
        )
    try:
        credential_payload = json.loads(credential_json)
    except json.JSONDecodeError as exc:
        raise YouTubePublishingError(
            "OAuth token could not be saved", code="TOKEN_STORAGE_FAILED"
        ) from exc
    token_relative = f"tokens/{channel_id}.json"
    account = YouTubeChannelAccount(
        channel_id=channel_id,
        channel_title=cleaned_title,
        token_file=token_relative,
        connected_at=_utc_now(),
    )
    root = _youtube_root(data_root)
    _atomic_private_json(root / token_relative, cast(dict[str, object], credential_payload))
    by_id = {item.channel_id: item for item in load_youtube_accounts(data_root)}
    by_id[channel_id] = account
    _atomic_private_json(
        _account_index_path(data_root),
        {
            "schema_version": "1.0",
            "accounts": [asdict(by_id[key]) for key in sorted(by_id)],
        },
    )
    return account


def _google_modules() -> tuple[Any, Any, Any, Any, type[Exception], type[Exception]]:
    try:
        refresh_error = importlib.import_module("google.auth.exceptions").RefreshError
        request_type = importlib.import_module("google.auth.transport.requests").Request
        credentials_type = importlib.import_module("google.oauth2.credentials").Credentials
        flow_type = importlib.import_module("google_auth_oauthlib.flow").InstalledAppFlow
        build_client = importlib.import_module("googleapiclient.discovery").build
        http_error = importlib.import_module("googleapiclient.errors").HttpError
        media_file_upload = importlib.import_module("googleapiclient.http").MediaFileUpload
    except ImportError as exc:
        raise YouTubePublishingError(
            "YouTube support is not installed in this App build",
            code="DEPENDENCY_MISSING",
        ) from exc
    return (
        flow_type,
        credentials_type,
        request_type,
        (build_client, media_file_upload),
        http_error,
        refresh_error,
    )


def load_channel_credentials(data_root: Path, channel_id: str) -> object:
    account = next(
        (item for item in load_youtube_accounts(data_root) if item.channel_id == channel_id),
        None,
    )
    if account is None:
        raise YouTubePublishingError(
            "Select a connected YouTube channel", code="CHANNEL_NOT_CONNECTED"
        )
    token_path = _youtube_root(data_root) / account.token_file
    if token_path.is_symlink() or not token_path.is_file():
        raise YouTubePublishingError(
            "The selected channel token is unavailable", code="TOKEN_MISSING"
        )
    _, credentials_type, request_type, _, _, refresh_error = _google_modules()
    try:
        credentials = credentials_type.from_authorized_user_file(
            str(token_path), scopes=list(YOUTUBE_SCOPES)
        )
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(request_type())
            _atomic_private_json(
                token_path, cast(dict[str, object], json.loads(credentials.to_json()))
            )
    except (OSError, ValueError, refresh_error) as exc:
        raise YouTubePublishingError(
            "YouTube authorization expired; connect this channel again",
            code="TOKEN_REFRESH_FAILED",
        ) from exc
    if not credentials.valid:
        raise YouTubePublishingError(
            "YouTube authorization is no longer valid; connect again",
            code="TOKEN_REFRESH_FAILED",
        )
    return credentials


class _OAuthCallbackApp:
    """Capture one localhost OAuth response without exposing its query to the UI."""

    def __init__(self) -> None:
        self.last_request_uri: str | None = None

    def __call__(self, environ: dict[str, Any], start_response: Any) -> list[bytes]:
        self.last_request_uri = wsgiref.util.request_uri(environ)
        body = (
            b"YouTube authorization response received. "
            b"You can return to SoloScale."
        )
        start_response(
            "200 OK",
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ],
        )
        return [body]


class _QuietOAuthRequestHandler(wsgiref.simple_server.WSGIRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def _oauth_authorization_url(flow: Any) -> str:
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="select_account",
    )
    return str(authorization_url)


class GoogleYouTubeProvider:
    """Official Google client implementation; instantiated only by a user action."""

    def authorize(
        self,
        client_secret_path: Path,
        *,
        status: OAuthStatusCallback,
        cancel_event: threading.Event,
        timeout_seconds: float,
    ) -> tuple[str, str, str]:
        selected = validate_client_secret(client_secret_path)
        flow_type, _, _, client_tools, _, _ = _google_modules()
        build_client, _ = client_tools
        callback = _OAuthCallbackApp()
        local_server = wsgiref.simple_server.make_server(
            "127.0.0.1",
            0,
            callback,
            handler_class=_QuietOAuthRequestHandler,
        )
        try:
            flow = flow_type.from_client_secrets_file(str(selected), scopes=list(YOUTUBE_SCOPES))
            flow.redirect_uri = f"http://127.0.0.1:{local_server.server_port}/"
            authorization_url = _oauth_authorization_url(flow)
            status("waiting", authorization_url)
            if not cancel_event.is_set():
                try:
                    webbrowser.open(authorization_url, new=1, autoraise=True)
                except webbrowser.Error:
                    pass
            deadline = time.monotonic() + timeout_seconds
            while callback.last_request_uri is None:
                if cancel_event.is_set():
                    raise YouTubePublishingError(
                        "Google authorization was cancelled",
                        code="OAUTH_CANCELLED",
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise YouTubePublishingError(
                        "Google authorization timed out",
                        code="OAUTH_TIMEOUT",
                    )
                local_server.timeout = min(0.5, remaining)
                local_server.handle_request()
            status("completing", None)
            assert callback.last_request_uri is not None
            authorization_response = callback.last_request_uri.replace("http", "https", 1)
            flow.fetch_token(authorization_response=authorization_response)
            credentials = flow.credentials
            service = build_client(
                "youtube", "v3", credentials=credentials, cache_discovery=False
            )
            response = service.channels().list(part="snippet", mine=True).execute()
        except YouTubePublishingError:
            raise
        except Exception as exc:
            raise _classify_google_error(exc, action="authorization") from exc
        finally:
            local_server.server_close()
        items = response.get("items", []) if isinstance(response, dict) else []
        first = items[0] if isinstance(items, list) and items else None
        snippet = first.get("snippet") if isinstance(first, dict) else None
        channel_id = first.get("id") if isinstance(first, dict) else None
        channel_title = snippet.get("title") if isinstance(snippet, dict) else None
        if not isinstance(channel_id, str) or not isinstance(channel_title, str):
            raise YouTubePublishingError(
                "This Google authorization did not expose a YouTube channel",
                code="CHANNEL_NOT_FOUND",
            )
        return channel_id, channel_title, credentials.to_json()

    def upload(
        self,
        *,
        credentials: object,
        video_path: Path,
        request: YouTubeUploadRequest,
        progress: Callable[[int | None], None],
    ) -> YouTubeUploadResult:
        _, _, _, client_tools, _, _ = _google_modules()
        build_client, media_file_upload = client_tools
        try:
            service = build_client(
                "youtube", "v3", credentials=credentials, cache_discovery=False
            )
            media = media_file_upload(
                str(video_path), chunksize=8 * 1024 * 1024, resumable=True
            )
            operation = service.videos().insert(
                part="snippet,status",
                body={
                    "snippet": {
                        "title": request.title,
                        "description": request.description,
                        **({"tags": list(request.tags)} if request.tags else {}),
                    },
                    "status": {"privacyStatus": request.privacy_status},
                },
                media_body=media,
            )
            response: dict[str, object] | None = None
            while response is None:
                status, raw_response = operation.next_chunk()
                progress(int(status.progress() * 100) if status is not None else None)
                if isinstance(raw_response, dict):
                    response = cast(dict[str, object], raw_response)
        except Exception as exc:
            raise _classify_google_error(exc, action="upload") from exc
        video_id = response.get("id")
        if not isinstance(video_id, str) or not video_id:
            raise YouTubePublishingError(
                "YouTube accepted the request without returning a video ID",
                code="UPLOAD_RESPONSE_INVALID",
            )
        return YouTubeUploadResult(
            video_id=video_id,
            video_url=f"https://youtu.be/{video_id}",
            uploaded_at=_utc_now(),
        )


def _classify_google_error(exc: Exception, *, action: str) -> YouTubePublishingError:
    try:
        _, _, _, _, http_error, refresh_error = _google_modules()
    except YouTubePublishingError:
        http_error = refresh_error = Exception
    if isinstance(exc, http_error):
        status = getattr(getattr(exc, "resp", None), "status", None)
        raw = getattr(exc, "content", b"")
        content = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else ""
        lowered = content.casefold()
        if "quotaexceeded" in lowered or status == 429:
            return YouTubePublishingError(
                "YouTube API quota is exhausted", code="QUOTA_EXCEEDED"
            )
        if "accessnotconfigured" in lowered:
            return YouTubePublishingError(
                "Enable YouTube Data API v3 for this Google project",
                code="YOUTUBE_API_DISABLED",
            )
        if "insufficient" in lowered or status == 403:
            return YouTubePublishingError(
                "YouTube authorization does not include the required permission",
                code="INSUFFICIENT_OAUTH_SCOPE",
            )
        return YouTubePublishingError(
            f"YouTube {action} failed", code="YOUTUBE_API_FAILURE"
        )
    if isinstance(exc, refresh_error):
        return YouTubePublishingError(
            "YouTube authorization expired; connect again", code="TOKEN_REFRESH_FAILED"
        )
    message = str(exc).casefold()
    if "access_denied" in message or ("authorization" in message and "denied" in message):
        return YouTubePublishingError(
            "Google authorization was cancelled or denied", code="OAUTH_CANCELLED"
        )
    return YouTubePublishingError(
        f"YouTube {action} could not reach Google", code="NETWORK_OR_OAUTH_FAILURE"
    )


def normalize_upload_request(
    *,
    run_id: str,
    channel_id: str,
    title: str,
    description: str,
    tags: str | tuple[str, ...],
    privacy_status: str,
) -> YouTubeUploadRequest:
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise YouTubePublishingError("Select a valid Content run", code="RUN_INVALID")
    if _CHANNEL_ID_RE.fullmatch(channel_id) is None:
        raise YouTubePublishingError(
            "Select a connected YouTube channel", code="CHANNEL_NOT_CONNECTED"
        )
    cleaned_title = title.strip()
    cleaned_description = description.strip()
    if not cleaned_title or len(cleaned_title) > 100:
        raise YouTubePublishingError(
            "YouTube title must contain 1–100 characters", code="TITLE_INVALID"
        )
    if len(cleaned_description) > 5_000:
        raise YouTubePublishingError(
            "YouTube description exceeds 5,000 characters", code="DESCRIPTION_INVALID"
        )
    raw_tags = tags if isinstance(tags, tuple) else tuple(tags.split(","))
    cleaned_tags = tuple(dict.fromkeys(item.strip() for item in raw_tags if item.strip()))
    if len(cleaned_tags) > 30 or any(len(item) > 80 for item in cleaned_tags):
        raise YouTubePublishingError("YouTube tags are invalid", code="TAGS_INVALID")
    if privacy_status not in {"private", "unlisted", "public"}:
        raise YouTubePublishingError(
            "Select private, unlisted, or public", code="PRIVACY_INVALID"
        )
    return YouTubeUploadRequest(
        run_id=run_id,
        channel_id=channel_id,
        title=cleaned_title,
        description=cleaned_description,
        tags=cleaned_tags,
        privacy_status=cast(YouTubePrivacy, privacy_status),
    )


def load_upload_defaults(data_root: Path, run_id: str) -> dict[str, object]:
    package = load_distribution_package(data_root, run_id)
    if package is None:
        raise YouTubePublishingError(
            "Prepare the Distribution Package first", code="PACKAGE_NOT_READY"
        )
    metadata_path = content_run_directory(data_root, run_id) / _UPLOAD_METADATA_NAME
    if metadata_path.is_symlink() or not metadata_path.is_file():
        raise YouTubePublishingError(
            "YouTube upload metadata is unavailable", code="PACKAGE_NOT_READY"
        )
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise YouTubePublishingError(
            "YouTube upload metadata is invalid", code="PACKAGE_INVALID"
        ) from exc
    if not isinstance(metadata, dict) or metadata.get("run_id") != run_id:
        raise YouTubePublishingError(
            "YouTube upload metadata is invalid", code="PACKAGE_INVALID"
        )
    return cast(dict[str, object], metadata)


def _validated_video_path(data_root: Path, run_id: str) -> tuple[Path, str, int]:
    package = load_distribution_package(data_root, run_id)
    if package is None:
        raise YouTubePublishingError(
            "Prepare the Distribution Package first", code="PACKAGE_NOT_READY"
        )
    artifacts = package.get("artifacts")
    video_record = artifacts.get("video") if isinstance(artifacts, dict) else None
    expected_hash = video_record.get("sha256") if isinstance(video_record, dict) else None
    video_path = content_run_directory(data_root, run_id) / _VIDEO_NAME
    try:
        metadata = video_path.lstat()
    except OSError as exc:
        raise YouTubePublishingError(
            "The YouTube master video is missing", code="VIDEO_INVALID"
        ) from exc
    if video_path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise YouTubePublishingError(
            "The YouTube master video is invalid", code="VIDEO_INVALID"
        )
    actual_hash = hashlib.sha256(video_path.read_bytes()).hexdigest()
    if not isinstance(expected_hash, str) or actual_hash != expected_hash:
        raise YouTubePublishingError(
            "The YouTube master no longer matches its Distribution Package",
            code="VIDEO_HASH_MISMATCH",
        )
    return video_path, actual_hash, metadata.st_size


def _receipt_path(data_root: Path, run_id: str, channel_id: str) -> Path:
    return content_run_directory(data_root, run_id) / f"youtube-receipt-{channel_id}.json"


def load_youtube_receipt(
    data_root: Path, run_id: str, channel_id: str
) -> dict[str, object] | None:
    path = _receipt_path(data_root, run_id, channel_id)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise YouTubePublishingError("YouTube receipt is unsafe", code="RECEIPT_INVALID")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise YouTubePublishingError(
            "YouTube receipt is invalid", code="RECEIPT_INVALID"
        ) from exc
    return cast(dict[str, object], payload) if isinstance(payload, dict) else None


class YouTubePublishingJobManager:
    """Bounded worker for OAuth and upload so the local HTTP server stays responsive."""

    def __init__(
        self,
        provider: YouTubeProvider | None = None,
        *,
        oauth_timeout_seconds: float = _DEFAULT_OAUTH_TIMEOUT_SECONDS,
    ) -> None:
        if oauth_timeout_seconds <= 0:
            raise ValueError("OAuth timeout must be positive")
        self._provider = provider or GoogleYouTubeProvider()
        self._oauth_timeout_seconds = oauth_timeout_seconds
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="soloscale-youtube")
        self._lock = threading.Lock()
        self._jobs: dict[str, YouTubeJobSnapshot] = {}
        self._cancel_events: dict[str, threading.Event] = {}

    def start_connect(self, *, data_root: Path) -> YouTubeJobSnapshot:
        validate_client_secret(youtube_client_secret_path(data_root))
        with self._lock:
            active = next(
                (
                    item
                    for item in self._jobs.values()
                    if item.kind == "connect" and item.phase in _ACTIVE_OAUTH_PHASES
                ),
                None,
            )
        if active is not None:
            raise YouTubePublishingError(
                "Cancel the current YouTube connection before starting another",
                code="OAUTH_IN_PROGRESS",
            )
        job = YouTubeJobSnapshot(
            job_id=f"youtube-auth-{uuid4().hex[:12]}",
            kind="connect",
            phase="STARTING",
            created_at=_utc_now(),
            updated_at=_utc_now(),
        )
        cancel_event = threading.Event()
        with self._lock:
            self._cancel_events[job.job_id] = cancel_event
        self._put(data_root, job)
        self._executor.submit(self._connect, data_root, job.job_id)
        return job

    def cancel_connect(self, *, data_root: Path, job_id: str) -> YouTubeJobSnapshot:
        current = self.get(data_root, job_id)
        if current is None or current.kind != "connect":
            raise YouTubePublishingError(
                "YouTube connection attempt was not found", code="JOB_INVALID"
            )
        if current.phase not in _ACTIVE_OAUTH_PHASES:
            return current
        with self._lock:
            cancel_event = self._cancel_events.get(job_id)
        if cancel_event is not None:
            cancel_event.set()
        return self._transition(
            data_root,
            job_id,
            "CANCELLED",
            authorization_url=None,
            error_code="OAUTH_CANCELLED",
            error_message="Google authorization was cancelled",
        )

    def start_upload(
        self, *, data_root: Path, request: YouTubeUploadRequest
    ) -> YouTubeJobSnapshot:
        if not any(
            item.channel_id == request.channel_id for item in load_youtube_accounts(data_root)
        ):
            raise YouTubePublishingError(
                "Select a connected YouTube channel", code="CHANNEL_NOT_CONNECTED"
            )
        _validated_video_path(data_root, request.run_id)
        job = YouTubeJobSnapshot(
            job_id=f"youtube-upload-{uuid4().hex[:12]}",
            kind="upload",
            phase="WAITING",
            created_at=_utc_now(),
            updated_at=_utc_now(),
            run_id=request.run_id,
            channel_id=request.channel_id,
            progress_percent=0,
        )
        self._put(data_root, job)
        self._executor.submit(self._upload, data_root, job.job_id, request)
        return job

    def get(self, data_root: Path, job_id: str) -> YouTubeJobSnapshot | None:
        if _JOB_ID_RE.fullmatch(job_id) is None:
            return None
        with self._lock:
            current = self._jobs.get(job_id)
        if current is not None:
            return current
        loaded = _load_job(data_root, job_id)
        return self._recover_interrupted_connect(data_root, loaded)

    def latest(
        self, data_root: Path, *, kind: YouTubeJobKind | None = None
    ) -> YouTubeJobSnapshot | None:
        jobs = list(_load_jobs(data_root))
        with self._lock:
            by_id = {item.job_id: item for item in jobs}
            by_id.update(self._jobs)
        selected = [item for item in by_id.values() if kind is None or item.kind == kind]
        latest = max(selected, key=lambda item: item.created_at) if selected else None
        return self._recover_interrupted_connect(data_root, latest)

    def shutdown(self) -> None:
        with self._lock:
            cancel_events = tuple(self._cancel_events.values())
        for cancel_event in cancel_events:
            cancel_event.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _recover_interrupted_connect(
        self,
        data_root: Path,
        job: YouTubeJobSnapshot | None,
    ) -> YouTubeJobSnapshot | None:
        if job is None or job.kind != "connect" or job.phase not in _ACTIVE_OAUTH_PHASES:
            return job
        with self._lock:
            still_running = job.job_id in self._jobs
        if still_running:
            return job
        recovered = replace(
            job,
            phase="FAILED",
            updated_at=_utc_now(),
            authorization_url=None,
            error_code="OAUTH_INTERRUPTED",
            error_message="Google authorization was interrupted; connect again",
        )
        self._put(data_root, recovered)
        return recovered

    def _put(self, data_root: Path, job: YouTubeJobSnapshot) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
        _atomic_private_json(_job_path(data_root, job.job_id), asdict(job))

    def _transition(
        self, data_root: Path, job_id: str, phase: YouTubeJobPhase, **changes: object
    ) -> YouTubeJobSnapshot:
        with self._lock:
            current = self._jobs[job_id]
        replace_snapshot = cast(Any, replace)
        updated = cast(
            YouTubeJobSnapshot,
            replace_snapshot(current, phase=phase, updated_at=_utc_now(), **changes),
        )
        self._put(data_root, updated)
        return updated

    def _connect(self, data_root: Path, job_id: str) -> None:
        with self._lock:
            cancel_event = self._cancel_events[job_id]

        def status(phase: Literal["waiting", "completing"], url: str | None) -> None:
            if cancel_event.is_set():
                return
            if phase == "waiting":
                self._transition(
                    data_root,
                    job_id,
                    "WAITING_FOR_AUTHORIZATION",
                    authorization_url=url,
                )
            else:
                self._transition(
                    data_root,
                    job_id,
                    "COMPLETING",
                    authorization_url=None,
                )

        try:
            channel_id, channel_title, credential_json = self._provider.authorize(
                youtube_client_secret_path(data_root),
                status=status,
                cancel_event=cancel_event,
                timeout_seconds=self._oauth_timeout_seconds,
            )
            if cancel_event.is_set():
                raise YouTubePublishingError(
                    "Google authorization was cancelled", code="OAUTH_CANCELLED"
                )
            account = save_authorized_channel(
                data_root,
                channel_id=channel_id,
                channel_title=channel_title,
                credential_json=credential_json,
            )
        except (OSError, YouTubePublishingError) as exc:
            error = exc if isinstance(exc, YouTubePublishingError) else YouTubePublishingError(
                "YouTube channel could not be saved", code="TOKEN_STORAGE_FAILED"
            )
            phase: YouTubeJobPhase
            if cancel_event.is_set() or error.code == "OAUTH_CANCELLED":
                phase = "CANCELLED"
            elif error.code == "OAUTH_TIMEOUT":
                phase = "TIMED_OUT"
            else:
                phase = "FAILED"
            self._transition(
                data_root,
                job_id,
                phase,
                authorization_url=None,
                error_code=error.code,
                error_message=str(error),
            )
        else:
            self._transition(
                data_root,
                job_id,
                "SUCCESS",
                channel_id=account.channel_id,
                channel_title=account.channel_title,
                authorization_url=None,
            )
        finally:
            with self._lock:
                self._cancel_events.pop(job_id, None)

    def _upload(
        self, data_root: Path, job_id: str, request: YouTubeUploadRequest
    ) -> None:
        self._transition(data_root, job_id, "UPLOADING", progress_percent=0)
        try:
            credentials = load_channel_credentials(data_root, request.channel_id)
            video_path, video_hash, video_bytes = _validated_video_path(
                data_root, request.run_id
            )

            def progress(value: int | None) -> None:
                self._transition(
                    data_root, job_id, "UPLOADING", progress_percent=value
                )

            result = self._provider.upload(
                credentials=credentials,
                video_path=video_path,
                request=request,
                progress=progress,
            )
            account = next(
                item for item in load_youtube_accounts(data_root)
                if item.channel_id == request.channel_id
            )
            _atomic_private_json(
                _receipt_path(data_root, request.run_id, request.channel_id),
                {
                    "schema_version": "1.0",
                    "receipt_id": f"youtube-{uuid4().hex[:12]}",
                    "platform": "youtube",
                    "status": "published",
                    "run_id": request.run_id,
                    "channel_id": account.channel_id,
                    "channel_title": account.channel_title,
                    "video_id": result.video_id,
                    "video_url": result.video_url,
                    "title": request.title,
                    "privacy_status": request.privacy_status,
                    "uploaded_at": result.uploaded_at,
                    "source_video": f"content-runs/{request.run_id}/{_VIDEO_NAME}",
                    "source_video_sha256": video_hash,
                    "source_video_bytes": video_bytes,
                    "automatic_retry_count": 0,
                },
            )
        except (OSError, ValueError, YouTubePublishingError) as exc:
            error = exc if isinstance(exc, YouTubePublishingError) else YouTubePublishingError(
                "YouTube upload failed", code="UPLOAD_FAILED"
            )
            self._transition(
                data_root,
                job_id,
                "FAILED",
                error_code=error.code,
                error_message=str(error),
            )
            return
        self._transition(
            data_root,
            job_id,
            "SUCCESS",
            progress_percent=100,
            video_id=result.video_id,
            video_url=result.video_url,
        )


def _job_path(data_root: Path, job_id: str) -> Path:
    if _JOB_ID_RE.fullmatch(job_id) is None:
        raise YouTubePublishingError("YouTube job ID is invalid", code="JOB_INVALID")
    return _youtube_root(data_root) / "jobs" / f"{job_id}.json"


def _load_job(data_root: Path, job_id: str) -> YouTubeJobSnapshot | None:
    path = _job_path(data_root, job_id)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise YouTubePublishingError("YouTube job is unsafe", code="JOB_INVALID")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return YouTubeJobSnapshot(**payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise YouTubePublishingError("YouTube job is invalid", code="JOB_INVALID") from exc


def _load_jobs(data_root: Path) -> tuple[YouTubeJobSnapshot, ...]:
    root = _youtube_root(data_root) / "jobs"
    if not root.is_dir() or root.is_symlink():
        return ()
    jobs: list[YouTubeJobSnapshot] = []
    for path in root.glob("youtube-*.json"):
        job = _load_job(data_root, path.stem)
        if job is not None:
            jobs.append(job)
    return tuple(jobs)
