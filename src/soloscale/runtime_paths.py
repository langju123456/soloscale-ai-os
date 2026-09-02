"""Resolve SoloScale runtime locations outside a source checkout."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

_LEGACY_DATA_DIRECTORY = ("Documents", "SoloScaleData")
_DESKTOP_DATA_DIRECTORY = ("Library", "Application Support", "SoloScale AI OS")


def _configured_path(value: str | Path | None, environment_name: str) -> Path | None:
    configured = value if value is not None else os.environ.get(environment_name)
    if not configured:
        return None
    return Path(configured).expanduser().resolve(strict=False)


def default_data_root(*, home: Path | None = None) -> Path:
    """Return the desktop data root while preserving an existing legacy root."""
    selected_home = (home or Path.home()).expanduser()
    legacy_root = selected_home.joinpath(*_LEGACY_DATA_DIRECTORY)
    if legacy_root.exists():
        return legacy_root.resolve(strict=False)
    return selected_home.joinpath(*_DESKTOP_DATA_DIRECTORY).resolve(strict=False)


def source_data_root(*, home: Path | None = None) -> Path:
    """Preserve the documented source-mode data root even before it exists."""
    selected_home = (home or Path.home()).expanduser()
    return selected_home.joinpath(*_LEGACY_DATA_DIRECTORY).resolve(strict=False)


def resolve_resource_root(resource_root: str | Path | None = None) -> Path:
    """Resolve resources for source and PyInstaller execution."""
    configured = _configured_path(resource_root, "SOLOSCALE_RESOURCE_ROOT")
    if configured is not None:
        return configured
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root).resolve(strict=False)
    return Path(__file__).resolve().parents[2]


def resolve_repository_root(
    repository_root: str | Path | None = None,
    *,
    resource_root: str | Path | None = None,
) -> Path:
    """Resolve a checkout root only when one is available or configured."""
    configured = _configured_path(repository_root, "SOLOSCALE_REPOSITORY_ROOT")
    if configured is not None:
        return configured
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "pyproject.toml").is_file():
        return source_root
    return resolve_resource_root(resource_root)


def resolve_workspace_root(
    workspace_root: str | Path | None = None,
    *,
    repository_root: str | Path | None = None,
) -> Path:
    """Resolve the operator workspace without depending on the process cwd."""
    configured = _configured_path(workspace_root, "SOLOSCALE_WORKSPACE_ROOT")
    if configured is not None:
        return configured
    return resolve_repository_root(repository_root)


@dataclass(frozen=True)
class RuntimePaths:
    data_root: Path
    resource_root: Path
    repository_root: Path
    workspace_root: Path


def resolve_runtime_paths(
    *,
    data_root: str | Path | None = None,
    resource_root: str | Path | None = None,
    repository_root: str | Path | None = None,
    workspace_root: str | Path | None = None,
) -> RuntimePaths:
    """Resolve all roots once for an explicit source or desktop runtime."""
    resolved_resources = resolve_resource_root(resource_root)
    resolved_repository = resolve_repository_root(
        repository_root, resource_root=resolved_resources
    )
    resolved_data = _configured_path(data_root, "SOLOSCALE_DATA_ROOT")
    if resolved_data is None:
        resolved_data = default_data_root()
    return RuntimePaths(
        data_root=resolved_data,
        resource_root=resolved_resources,
        repository_root=resolved_repository,
        workspace_root=resolve_workspace_root(
            workspace_root, repository_root=resolved_repository
        ),
    )
