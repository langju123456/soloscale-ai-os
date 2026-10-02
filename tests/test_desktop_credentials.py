from __future__ import annotations

import io
import json

import pytest

from soloscale.desktop_credentials import (
    DesktopCredentialError,
    _clear_for_tests,
    _frame_for_tests,
    configure_desktop_credentials_from_stdin,
    configure_openai_credential_from_stdin,
    deepseek_api_key,
    deepseek_api_key_is_configured,
    github_access_token,
    github_access_token_is_configured,
    heygen_api_key,
    heygen_api_key_is_configured,
    openai_api_key,
    openai_api_key_is_configured,
    read_openai_credential_frame,
)


def test_desktop_credential_frame_supports_empty_and_process_memory_values() -> None:
    _clear_for_tests()
    assert read_openai_credential_frame(_frame_for_tests(b"")) is None

    configure_openai_credential_from_stdin(_frame_for_tests(b"synthetic-test-key"))
    assert openai_api_key_is_configured() is True
    assert openai_api_key() == "synthetic-test-key"
    _clear_for_tests()

    envelope = json.dumps(
        {
            "schema_version": "1.0",
            "openai_api_key": "synthetic-openai-key",
            "github_access_token": "synthetic-github-token",
            "heygen_api_key": "synthetic-heygen-key",
            "deepseek_api_key": "synthetic-deepseek-key",
        },
        sort_keys=True,
    ).encode()
    configure_desktop_credentials_from_stdin(_frame_for_tests(envelope))
    assert openai_api_key() == "synthetic-openai-key"
    assert github_access_token_is_configured() is True
    assert github_access_token() == "synthetic-github-token"
    assert heygen_api_key_is_configured() is True
    assert heygen_api_key() == "synthetic-heygen-key"
    assert deepseek_api_key_is_configured() is True
    assert deepseek_api_key() == "synthetic-deepseek-key"
    _clear_for_tests()

    envelope_without_deepseek = json.dumps(
        {
            "schema_version": "1.0",
            "openai_api_key": "synthetic-openai-key",
        },
        sort_keys=True,
    ).encode()
    configure_desktop_credentials_from_stdin(
        _frame_for_tests(envelope_without_deepseek)
    )
    assert openai_api_key_is_configured() is True
    assert deepseek_api_key_is_configured() is False
    assert deepseek_api_key() is None
    _clear_for_tests()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x00\x00\x00",
        b"\x00\x00\x00\x04abc",
        (513).to_bytes(4, byteorder="big"),
        b"\x00\x00\x00\x02\xff\xfe",
        b"\x00\x00\x00\x05 key ",
    ],
)
def test_desktop_credential_frame_fails_closed_without_secret_echo(raw: bytes) -> None:
    sentinel = "synthetic-test-key"
    with pytest.raises(DesktopCredentialError) as failure:
        read_openai_credential_frame(io.BytesIO(raw + sentinel.encode()))
    assert sentinel not in str(failure.value)
