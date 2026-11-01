"""Stable, non-secret cache identities for Resume cloud generation results."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from soloscale.model_gateway import ModelGateway


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def gateway_cache_identity(gateway: ModelGateway | None) -> tuple[str, str, str]:
    """Return provider/model/endpoint identities without retaining credentials."""
    if gateway is None:
        return ("template", "", sha256_text(""))
    descriptor = gateway.descriptor
    return (
        descriptor.provider.value,
        descriptor.model or "",
        sha256_text(descriptor.base_url or ""),
    )


def generation_pipeline_fingerprint(paths: Iterable[Path] | None = None) -> str:
    """Hash generation-relevant source so prompt or pipeline changes invalidate cache."""
    if paths is None:
        package = Path(__file__).parent
        names = {path.name for path in package.glob("resume_*.py")}
        names.update(
            {
                "content_canon.py",
                "evidence_hub.py",
                "github_connect.py",
                "knowledge_models.py",
                "model_gateway.py",
                "models.py",
            }
        )
        paths = (package / name for name in sorted(names))
    digest = hashlib.sha256()
    for path in sorted((Path(path) for path in paths), key=lambda item: str(item)):
        digest.update(str(path).encode("utf-8"))
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


@dataclass(frozen=True)
class ResultCacheKey:
    client_key: str
    raw_resume_sha256: str
    job_description_sha256: str
    mode: str
    provider: str
    model: str
    base_url_sha256: str
    pipeline_sha256: str
