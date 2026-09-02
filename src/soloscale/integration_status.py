"""Body-free readiness summaries for external SoloScale product services."""

from __future__ import annotations

import os
from dataclasses import dataclass

from soloscale.desktop_credentials import heygen_api_key_is_configured


@dataclass(frozen=True)
class IntegrationStatus:
    service: str
    state: str
    detail: str
    ready: bool


def _linkedin_status() -> IntegrationStatus:
    try:
        from buildlog.linkedin_token_store import FileTokenStore

        token = FileTokenStore().load()
        if token is None:
            return IntegrationStatus(
                "LinkedIn", "Not connected", "Authorize through BuildLog publishing.", False
            )
        if token.is_expired():
            return IntegrationStatus(
                "LinkedIn", "Reconnect", "The saved BuildLog token is expired.", False
            )
        return IntegrationStatus(
            "LinkedIn", "Ready", "BuildLog token is present and not expired.", True
        )
    except Exception:
        return IntegrationStatus(
            "LinkedIn", "Needs attention", "BuildLog credential status is unavailable.", False
        )


def _x_status() -> IntegrationStatus:
    try:
        from buildlog.x_token_store import FileXTokenStore

        token = FileXTokenStore().load()
        if token is None:
            return IntegrationStatus(
                "X", "Not connected", "Authorize through BuildLog publishing.", False
            )
        if token.is_expired():
            return IntegrationStatus(
                "X", "Reconnect", "The saved BuildLog token is expired.", False
            )
        return IntegrationStatus(
            "X", "Ready", "BuildLog token is present and not expired.", True
        )
    except Exception:
        return IntegrationStatus(
            "X", "Needs attention", "BuildLog credential status is unavailable.", False
        )


def connected_service_statuses() -> tuple[IntegrationStatus, ...]:
    """Return non-secret provider states without contacting any external service."""

    heygen_configured = heygen_api_key_is_configured()
    youtube_configured = bool(
        os.environ.get("YOUTUBE_REFRESH_TOKEN", "").strip()
        and os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
    )
    return (
        IntegrationStatus(
            "HeyGen",
            "Connected" if heygen_configured else "Not configured",
            (
                "Keychain credential is ready for explicitly approved avatar segments."
                if heygen_configured
                else "Manual segment handoff remains available without an API key."
            ),
            heygen_configured,
        ),
        _linkedin_status(),
        _x_status(),
        IntegrationStatus(
            "YouTube",
            "Credential detected" if youtube_configured else "Export package ready",
            "SoloScale prepares the package; direct upload is not enabled yet.",
            False,
        ),
    )
