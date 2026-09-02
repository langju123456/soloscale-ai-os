import hashlib
import json
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import soloscale.youtube_publishing as youtube
from soloscale.youtube_publishing import (
    GoogleYouTubeProvider,
    YouTubePublishingJobManager,
    YouTubeUploadRequest,
    YouTubeUploadResult,
    load_youtube_accounts,
    load_youtube_receipt,
    normalize_upload_request,
    save_authorized_channel,
    validate_client_secret,
)

CHANNEL_ID = "UCabcdefghijk"


def _client_secret(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "test.apps.googleusercontent.com",
                    "client_secret": "not-a-real-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _distribution_package(data_root: Path, run_id: str) -> None:
    run_dir = data_root / "content-runs" / run_id
    run_dir.mkdir(parents=True)
    video = run_dir / "21_creator_video_youtube.mp4"
    video.write_bytes(b"synthetic-mp4")
    (run_dir / "26_distribution_package.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "artifacts": {
                    "video": {"sha256": hashlib.sha256(video.read_bytes()).hexdigest()}
                },
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "27_youtube_upload.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "title": "Synthetic title",
                "description": "Synthetic description",
                "visibility": "private",
            }
        ),
        encoding="utf-8",
    )


def _wait(
    manager: YouTubePublishingJobManager,
    data_root: Path,
    job_id: str,
) -> youtube.YouTubeJobSnapshot:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        snapshot = manager.get(data_root, job_id)
        assert snapshot is not None
        if snapshot.phase in {"SUCCESS", "CANCELLED", "TIMED_OUT", "FAILED"}:
            return snapshot
        time.sleep(0.01)
    raise AssertionError("YouTube job did not finish")


def _wait_phase(
    manager: YouTubePublishingJobManager,
    data_root: Path,
    job_id: str,
    phase: youtube.YouTubeJobPhase,
) -> youtube.YouTubeJobSnapshot:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        snapshot = manager.get(data_root, job_id)
        assert snapshot is not None
        if snapshot.phase == phase:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"YouTube job did not reach {phase}")


class FakeProvider:
    def authorize(
        self,
        client_secret_path: Path,
        *,
        status: youtube.OAuthStatusCallback,
        cancel_event: threading.Event,
        timeout_seconds: float,
    ) -> tuple[str, str, str]:
        assert validate_client_secret(client_secret_path)
        assert not cancel_event.is_set()
        assert timeout_seconds > 0
        status(
            "waiting",
            "https://accounts.google.com/o/oauth2/auth?prompt=select_account",
        )
        status("completing", None)
        return (
            CHANNEL_ID,
            "SoloScale",
            json.dumps({"token": "redacted", "refresh_token": "redacted"}),
        )

    def upload(
        self,
        *,
        credentials: object,
        video_path: Path,
        request: YouTubeUploadRequest,
        progress: Callable[[int | None], None],
    ) -> YouTubeUploadResult:
        assert credentials is not None
        assert video_path.read_bytes() == b"synthetic-mp4"
        assert request.channel_id == CHANNEL_ID
        progress(50)
        return YouTubeUploadResult(
            video_id="video-123",
            video_url="https://youtu.be/video-123",
            uploaded_at="2026-08-28T12:00:00+00:00",
        )


class CancelThenSucceedProvider(FakeProvider):
    def __init__(self) -> None:
        self.calls = 0

    def authorize(
        self,
        client_secret_path: Path,
        *,
        status: youtube.OAuthStatusCallback,
        cancel_event: threading.Event,
        timeout_seconds: float,
    ) -> tuple[str, str, str]:
        self.calls += 1
        if self.calls == 1:
            status(
                "waiting",
                "https://accounts.google.com/o/oauth2/auth?prompt=select_account",
            )
            cancel_event.wait(timeout_seconds)
            raise youtube.YouTubePublishingError(
                "Google authorization was cancelled", code="OAUTH_CANCELLED"
            )
        return super().authorize(
            client_secret_path,
            status=status,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
        )


class FailThenSucceedProvider(FakeProvider):
    def __init__(self, code: str) -> None:
        self.code = code
        self.calls = 0

    def authorize(
        self,
        client_secret_path: Path,
        *,
        status: youtube.OAuthStatusCallback,
        cancel_event: threading.Event,
        timeout_seconds: float,
    ) -> tuple[str, str, str]:
        self.calls += 1
        if self.calls == 1:
            status(
                "waiting",
                "https://accounts.google.com/o/oauth2/auth?prompt=select_account",
            )
            raise youtube.YouTubePublishingError("synthetic failure", code=self.code)
        return super().authorize(
            client_secret_path,
            status=status,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
        )


def test_client_configuration_and_multichannel_store_are_private(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    selected = _client_secret(data_root / "integrations/youtube/client_secret.json")

    assert validate_client_secret(selected) == selected
    first = save_authorized_channel(
        data_root,
        channel_id=CHANNEL_ID,
        channel_title="SoloScale",
        credential_json=json.dumps({"token": "one"}),
    )
    second = save_authorized_channel(
        data_root,
        channel_id="UCzyxwvutsrqp",
        channel_title="AI Vision",
        credential_json=json.dumps({"token": "two"}),
    )

    assert {item.channel_id for item in load_youtube_accounts(data_root)} == {
        first.channel_id,
        second.channel_id,
    }
    token = data_root / "integrations/youtube" / first.token_file
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert stat.S_IMODE(token.parent.stat().st_mode) == 0o700
    accounts_payload = json.loads(
        (data_root / "integrations/youtube/accounts.json").read_text(encoding="utf-8")
    )
    assert all(
        set(item) == {"channel_id", "channel_title", "token_file", "connected_at"}
        for item in accounts_payload["accounts"]
    )
    assert "redacted" not in json.dumps(accounts_payload)


def test_background_connect_and_upload_create_receipt_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    _client_secret(data_root / "integrations/youtube/client_secret.json")
    manager = YouTubePublishingJobManager(provider=FakeProvider())
    try:
        started = time.monotonic()
        connect_job = manager.start_connect(data_root=data_root)
        assert time.monotonic() - started < 0.2
        assert _wait(manager, data_root, connect_job.job_id).phase == "SUCCESS"

        run_id = "content-20260828T120000Z-deadbeef00"
        _distribution_package(data_root, run_id)
        monkeypatch.setattr(youtube, "load_channel_credentials", lambda *_: object())
        request = normalize_upload_request(
            run_id=run_id,
            channel_id=CHANNEL_ID,
            title="Synthetic title",
            description="Synthetic description",
            tags="AI, SoloScale",
            privacy_status="private",
        )
        upload_job = manager.start_upload(data_root=data_root, request=request)
        complete = _wait(manager, data_root, upload_job.job_id)

        assert complete.phase == "SUCCESS"
        assert complete.progress_percent == 100
        receipt = load_youtube_receipt(data_root, run_id, CHANNEL_ID)
        assert receipt is not None
        assert receipt["video_id"] == "video-123"
        assert receipt["privacy_status"] == "private"
        assert receipt["automatic_retry_count"] == 0
        assert "token" not in json.dumps(receipt)
    finally:
        manager.shutdown()


def test_oauth_authorization_url_forces_account_selection() -> None:
    captured: dict[str, object] = {}

    class Flow:
        def authorization_url(self, **kwargs: object) -> tuple[str, str]:
            captured.update(kwargs)
            return "https://accounts.google.com/o/oauth2/auth?prompt=select_account", "state"

    assert "prompt=select_account" in youtube._oauth_authorization_url(Flow())
    assert captured == {"access_type": "offline", "prompt": "select_account"}


def test_cancelled_oauth_saves_no_account_and_can_retry(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _client_secret(data_root / "integrations/youtube/client_secret.json")
    provider = CancelThenSucceedProvider()
    manager = YouTubePublishingJobManager(provider=provider, oauth_timeout_seconds=1)
    try:
        first = manager.start_connect(data_root=data_root)
        waiting = _wait_phase(
            manager, data_root, first.job_id, "WAITING_FOR_AUTHORIZATION"
        )
        assert waiting.authorization_url is not None
        cancelled = manager.cancel_connect(data_root=data_root, job_id=first.job_id)
        assert cancelled.phase == "CANCELLED"
        assert _wait(manager, data_root, first.job_id).phase == "CANCELLED"
        assert load_youtube_accounts(data_root) == ()
        assert not (data_root / "integrations/youtube/tokens").exists()

        second = manager.start_connect(data_root=data_root)
        assert _wait(manager, data_root, second.job_id).phase == "SUCCESS"
        assert len(load_youtube_accounts(data_root)) == 1
    finally:
        manager.shutdown()


@pytest.mark.parametrize(
    ("failure_code", "expected_phase"),
    (("OAUTH_TIMEOUT", "TIMED_OUT"), ("NETWORK_UNAVAILABLE", "FAILED")),
)
def test_timeout_or_failure_returns_to_retry_ready(
    tmp_path: Path,
    failure_code: str,
    expected_phase: youtube.YouTubeJobPhase,
) -> None:
    data_root = tmp_path / failure_code.lower()
    _client_secret(data_root / "integrations/youtube/client_secret.json")
    manager = YouTubePublishingJobManager(provider=FailThenSucceedProvider(failure_code))
    try:
        first = manager.start_connect(data_root=data_root)
        failed = _wait(manager, data_root, first.job_id)
        assert failed.phase == expected_phase
        assert failed.authorization_url is None
        assert load_youtube_accounts(data_root) == ()

        second = manager.start_connect(data_root=data_root)
        complete = _wait(manager, data_root, second.job_id)
        assert complete.phase == "SUCCESS"
        assert complete.channel_title == "SoloScale"
    finally:
        manager.shutdown()


def test_reconnecting_same_channel_updates_without_duplicate(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _client_secret(data_root / "integrations/youtube/client_secret.json")
    manager = YouTubePublishingJobManager(provider=FakeProvider())
    try:
        for _ in range(2):
            job = manager.start_connect(data_root=data_root)
            assert _wait(manager, data_root, job.job_id).phase == "SUCCESS"
        accounts = load_youtube_accounts(data_root)
        assert len(accounts) == 1
        assert accounts[0].channel_id == CHANNEL_ID
    finally:
        manager.shutdown()


def test_google_provider_builds_resumable_videos_insert_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class Operation:
        def next_chunk(self) -> tuple[None, dict[str, str]]:
            return None, {"id": "video-456"}

    class Videos:
        def insert(self, **kwargs: object) -> Operation:
            captured.update(kwargs)
            return Operation()

    class Service:
        def videos(self) -> Videos:
            return Videos()

    def build_client(*args: object, **kwargs: object) -> Service:
        captured["build_args"] = args
        captured["build_kwargs"] = kwargs
        return Service()

    def media_file_upload(*args: object, **kwargs: object) -> object:
        captured["media_args"] = args
        captured["media_kwargs"] = kwargs
        return object()

    monkeypatch.setattr(
        youtube,
        "_google_modules",
        lambda: (
            object(),
            object(),
            object(),
            (build_client, media_file_upload),
            Exception,
            Exception,
        ),
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    result = GoogleYouTubeProvider().upload(
        credentials=object(),
        video_path=video,
        request=YouTubeUploadRequest(
            run_id="content-20260828T120000Z-deadbeef00",
            channel_id=CHANNEL_ID,
            title="Title",
            description="Description",
            tags=("AI",),
            privacy_status="unlisted",
        ),
        progress=lambda _: None,
    )

    assert captured["part"] == "snippet,status"
    assert captured["body"] == {
        "snippet": {"title": "Title", "description": "Description", "tags": ["AI"]},
        "status": {"privacyStatus": "unlisted"},
    }
    assert captured["media_kwargs"] == {
        "chunksize": 8 * 1024 * 1024,
        "resumable": True,
    }
    assert result.video_id == "video-456"
