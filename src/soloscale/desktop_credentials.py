"""Process-memory credential handoff for the packaged macOS desktop app."""

from __future__ import annotations

import io
import json
import sys
from typing import BinaryIO

_MAX_CREDENTIAL_BYTES = 512
_MAX_CREDENTIAL_ENVELOPE_BYTES = 4096
_openai_api_key: str | None = None
_github_access_token: str | None = None
_heygen_api_key: str | None = None


class DesktopCredentialError(RuntimeError):
    """A desktop credential frame is missing or malformed."""


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise DesktopCredentialError("desktop credential handoff is incomplete")
        data.extend(chunk)
    return bytes(data)


def _read_frame(stream: BinaryIO, *, maximum: int) -> bytes:
    header = _read_exact(stream, 4)
    payload_length = int.from_bytes(header, byteorder="big", signed=False)
    if payload_length > maximum:
        raise DesktopCredentialError("desktop credential handoff exceeds its limit")
    if payload_length == 0:
        if stream.read(1):
            raise DesktopCredentialError("desktop credential handoff has trailing data")
        return b""
    payload = _read_exact(stream, payload_length)
    if stream.read(1):
        raise DesktopCredentialError("desktop credential handoff has trailing data")
    return payload


def _credential(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise DesktopCredentialError("desktop credential handoff is invalid")
    if len(value.encode("utf-8")) > _MAX_CREDENTIAL_BYTES:
        raise DesktopCredentialError("desktop credential handoff exceeds its limit")
    return value


def read_openai_credential_frame(stream: BinaryIO) -> str | None:
    """Read the legacy one-key frame retained for source/test compatibility."""

    payload = _read_frame(stream, maximum=_MAX_CREDENTIAL_BYTES)
    if not payload:
        return None
    try:
        credential = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise DesktopCredentialError("desktop credential handoff is invalid") from None
    return _credential(credential)


def read_desktop_credential_envelope(
    stream: BinaryIO,
) -> tuple[str | None, str | None, str | None]:
    """Read one versioned multi-credential frame without persisting either secret."""

    payload = _read_frame(stream, maximum=_MAX_CREDENTIAL_ENVELOPE_BYTES)
    if not payload:
        return None, None, None
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise DesktopCredentialError("desktop credential handoff is invalid") from None
    if not decoded.startswith("{"):
        return _credential(decoded), None, None
    try:
        envelope = json.loads(decoded)
    except json.JSONDecodeError:
        raise DesktopCredentialError("desktop credential handoff is invalid") from None
    if (
        not isinstance(envelope, dict)
        or set(envelope)
        - {
            "schema_version",
            "openai_api_key",
            "github_access_token",
            "heygen_api_key",
        }
        or envelope.get("schema_version") != "1.0"
    ):
        raise DesktopCredentialError("desktop credential handoff is invalid")
    return (
        _credential(envelope.get("openai_api_key")),
        _credential(envelope.get("github_access_token")),
        _credential(envelope.get("heygen_api_key")),
    )


def configure_openai_credential_from_stdin(
    stream: BinaryIO | None = None,
) -> None:
    """Load the one-shot desktop credential into process memory only."""

    global _openai_api_key
    selected_stream = stream or sys.stdin.buffer
    _openai_api_key = read_openai_credential_frame(selected_stream)


def configure_desktop_credentials_from_stdin(
    stream: BinaryIO | None = None,
) -> None:
    """Load the one-shot Desktop credential envelope into process memory only."""

    global _github_access_token, _heygen_api_key, _openai_api_key
    selected_stream = stream or sys.stdin.buffer
    (
        _openai_api_key,
        _github_access_token,
        _heygen_api_key,
    ) = read_desktop_credential_envelope(selected_stream)


def openai_api_key() -> str | None:
    """Return the in-memory credential for immediate gateway construction."""

    return _openai_api_key


def openai_api_key_is_configured() -> bool:
    return _openai_api_key is not None


def github_access_token() -> str | None:
    """Return the in-memory GitHub App user token for read-only API calls."""

    return _github_access_token


def github_access_token_is_configured() -> bool:
    return _github_access_token is not None


def heygen_api_key() -> str | None:
    """Return the in-memory HeyGen API key for an explicitly approved request."""

    return _heygen_api_key


def heygen_api_key_is_configured() -> bool:
    return _heygen_api_key is not None


def _clear_for_tests() -> None:
    global _github_access_token, _heygen_api_key, _openai_api_key
    _openai_api_key = None
    _github_access_token = None
    _heygen_api_key = None


def _frame_for_tests(value: bytes) -> io.BytesIO:
    return io.BytesIO(len(value).to_bytes(4, byteorder="big") + value)
