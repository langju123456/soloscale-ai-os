"""Private, non-publishing Writer → Reviewer → Reviser editorial packages."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field, model_validator

from soloscale.editorial_models import (
    AuthorVoiceProfile,
    EditorialProvenance,
    EditorialRole,
    ReviewResult,
    RevisionResult,
)
from soloscale.editorial_pipeline import PrivateWriteError, write_private_once
from soloscale.evidence_capture import capture_assets
from soloscale.evidence_hub import EvidenceHub
from soloscale.models import ContractModel
from soloscale.visual_planner import VisualPlan, write_visual_package

EditorialPackageStatus = Literal["READY_FOR_HUMAN_PUBLICATION", "READY_FOR_HUMAN_REVIEW"]

_PACKAGE_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_URL = re.compile(r"https?://[^\s]+", re.I)
_PRIVATE = re.compile(
    r"(?:file://|(?:^|[\s'\"])/(?:Users|home|private|var|tmp)/|[A-Za-z]:\\)", re.I
)
_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{12,}|AKIA[A-Z0-9]{12,}|"
    r"Bearer\s+[A-Za-z0-9._~+/=-]{12,})"
)


def validate_public_editorial_text(value: str) -> None:
    """Reject provider-bound text containing local paths or credential shapes."""

    if _PRIVATE.search(value) or _SECRET.search(value):
        raise ValueError("editorial text contains a private path or credential-like value")


class EvidenceAnchor(ContractModel):
    evidence_id: str = Field(min_length=1, max_length=180)
    source_label: str = Field(min_length=1, max_length=240)
    factual_boundary: str = Field(min_length=1, max_length=1000)


class EditorialArtifacts(ContractModel):
    canonical_story: str = Field(min_length=1, max_length=30_000)
    linkedin: str = Field(min_length=1, max_length=10_000)
    x_thread: list[str] = Field(min_length=1, max_length=12)
    x_post: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def public_artifacts_must_be_safe_and_platform_fit(self) -> EditorialArtifacts:
        public_values = [self.canonical_story, self.linkedin, self.x_post, *self.x_thread]
        for value in public_values:
            validate_public_editorial_text(value)
        validate_x_artifacts(self.x_thread, self.x_post)
        return self


class EditorialPackage(ContractModel):
    package_id: str
    day: int = Field(ge=1, le=31)
    status: EditorialPackageStatus
    topic: str = Field(min_length=1, max_length=300)
    audience: str = Field(min_length=1, max_length=600)
    author_voice_profile_id: str = Field(min_length=1, max_length=160)
    evidence_manifest: list[EvidenceAnchor] = Field(min_length=1, max_length=40)
    factual_gaps: list[str] = Field(default_factory=list, max_length=30)
    artifacts: EditorialArtifacts
    visual_plan: VisualPlan
    writer: EditorialProvenance
    reviewer: ReviewResult
    revision: RevisionResult
    publication_performed: Literal[False] = False

    @model_validator(mode="after")
    def validate_pipeline_roles(self) -> EditorialPackage:
        if _PACKAGE_ID.fullmatch(self.package_id) is None:
            raise ValueError("package_id must be a lowercase slug")
        if self.writer.role is not EditorialRole.WRITER:
            raise ValueError("writer receipt must have writer role")
        if self.reviewer.provenance.role is not EditorialRole.REVIEWER:
            raise ValueError("review receipt must have reviewer role")
        if self.revision.provenance.role is not EditorialRole.REVISER:
            raise ValueError("revision receipt must have reviser role")
        if not self.reviewer.provenance.fresh_context:
            raise ValueError("reviewer must use a fresh context")
        finding_ids = {finding.finding_id for finding in self.reviewer.findings}
        decision_ids = {decision.finding_id for decision in self.revision.decisions}
        if finding_ids != decision_ids:
            raise ValueError("revision decisions must cover every review finding exactly once")
        if self.status == "READY_FOR_HUMAN_PUBLICATION" and any(
            finding.severity.value == "blocker" for finding in self.reviewer.findings
        ):
            raise ValueError("a package with blocking findings cannot be publication-ready")
        expected_final_hashes = {
            "canonical-story.md": hashlib.sha256(
                (self.artifacts.canonical_story.rstrip() + "\n").encode()
            ).hexdigest(),
            "linkedin.md": hashlib.sha256(
                (self.artifacts.linkedin.rstrip() + "\n").encode()
            ).hexdigest(),
            "x-thread.md": hashlib.sha256(
                ("\n\n".join(post.strip() for post in self.artifacts.x_thread) + "\n").encode()
            ).hexdigest(),
            "x-post.md": hashlib.sha256(
                (self.artifacts.x_post.rstrip() + "\n").encode()
            ).hexdigest(),
        }
        if any(
            self.revision.provenance.output_artifact_hashes.get(name) != digest
            for name, digest in expected_final_hashes.items()
        ):
            raise ValueError("reviser receipt must hash every final editorial artifact")
        return self


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _weighted_character(value: str) -> int:
    codepoint = ord(value)
    if codepoint <= 0x10FF or 0x2000 <= codepoint <= 0x200D:
        return 1
    if 0x2010 <= codepoint <= 0x201F or 0x2032 <= codepoint <= 0x2037:
        return 1
    return 2


def x_weighted_length(value: str) -> int:
    """Approximate twitter-text v3 weighting, including the fixed 23-char URL length."""

    length = 0
    cursor = 0
    for match in _URL.finditer(value):
        length += sum(_weighted_character(character) for character in value[cursor : match.start()])
        length += 23
        cursor = match.end()
    return length + sum(_weighted_character(character) for character in value[cursor:])


def validate_x_artifacts(thread: list[str], x_post: str) -> None:
    for index, post in enumerate(thread, start=1):
        if x_weighted_length(post) > 280:
            raise ValueError(f"X thread post {index} exceeds 280 weighted characters")
    if x_weighted_length(x_post) > 280:
        raise ValueError("standalone X post exceeds 280 weighted characters")


def write_author_voice_profile(root: Path, profile: AuthorVoiceProfile) -> Path:
    path = root / f"{profile.profile_id}-{profile.version}.json"
    write_private_once(path, _canonical_json(profile.model_dump(mode="json")))
    return path


def write_editorial_package(
    root: Path,
    package: EditorialPackage,
    *,
    try_png: bool = True,
    data_root: Path | None = None,
    evidence_hub: EvidenceHub | None = None,
    evidence_bundle_id: str | None = None,
    evidence_item_ids: list[str] | None = None,
) -> dict[str, str]:
    """Persist one complete package exactly once; never publish or call a provider."""

    if root.exists() or root.is_symlink():
        raise PrivateWriteError("editorial package already exists")
    artifacts: dict[str, str] = {
        "canonical-story.md": package.artifacts.canonical_story.rstrip() + "\n",
        "linkedin.md": package.artifacts.linkedin.rstrip() + "\n",
        "x-thread.md": "\n\n".join(post.strip() for post in package.artifacts.x_thread) + "\n",
        "x-post.md": package.artifacts.x_post.rstrip() + "\n",
        "evidence-manifest.json": _canonical_json(
            {"evidence": [item.model_dump(mode="json") for item in package.evidence_manifest]}
        ),
        "factual-gaps.md": "\n".join(
            ["# Factual gaps", "", *[f"- {item}" for item in package.factual_gaps], ""]
        ),
        "structured-review.json": _canonical_json(package.reviewer.model_dump(mode="json")),
        "revision-decisions.json": _canonical_json(package.revision.model_dump(mode="json")),
        "writer-receipt.json": _canonical_json(package.writer.model_dump(mode="json")),
        "reviewer-receipt.json": _canonical_json(
            package.reviewer.provenance.model_dump(mode="json")
        ),
        "reviser-receipt.json": _canonical_json(
            package.revision.provenance.model_dump(mode="json")
        ),
    }
    hashes = {name: write_private_once(root / name, body) for name, body in artifacts.items()}
    write_visual_package(
        root / "visual",
        package.visual_plan,
        try_png=try_png,
        status=package.status,
    )
    for path in sorted((root / "visual").iterdir()):
        if path.is_file() and not path.is_symlink():
            hashes[f"visual/{path.name}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt = {
        "schema_version": "0.1",
        "package_id": package.package_id,
        "day": package.day,
        "status": package.status,
        "topic": package.topic,
        "author_voice_profile_id": package.author_voice_profile_id,
        "models": {
            "writer": package.writer.exact_model,
            "reviewer": package.reviewer.provenance.exact_model,
            "reviser": package.revision.provenance.exact_model,
        },
        "provider_runs": {
            "writer": package.writer.provider.kind.value,
            "reviewer": package.reviewer.provenance.provider.kind.value,
            "reviser": package.revision.provenance.provider.kind.value,
        },
        "network_used": any(
            receipt.network_used
            for receipt in (
                package.writer,
                package.reviewer.provenance,
                package.revision.provenance,
            )
        ),
        "paid_api_used": False,
        "publication_performed": False,
        "human_gate_required": True,
        "artifacts": hashes,
    }
    hashes["receipt.json"] = write_private_once(root / "receipt.json", _canonical_json(receipt))
    selected_data_root = data_root or _inferred_data_root(root)
    if evidence_hub is not None:
        selected_data_root = evidence_hub.data_root
    if selected_data_root is not None:
        capture_assets(
            data_root=selected_data_root,
            run_dir=root,
            owner="editorial",
            run_id=package.package_id,
            artifact_names=list(hashes),
            evidence_bundle_id=evidence_bundle_id,
            evidence_item_ids=evidence_item_ids,
            evidence_hub=evidence_hub,
        )
    return hashes


def _inferred_data_root(package_root: Path) -> Path | None:
    for parent in package_root.absolute().parents:
        if parent.name == "content":
            return parent.parent
    return None


def verify_editorial_package(root: Path) -> bool:
    receipt_path = root / "receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        return False
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict) or receipt.get("publication_performed") is not False:
        return False
    root_absolute = root.absolute()
    for name, expected in artifacts.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            return False
        relative = Path(name)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            return False
        path = root / relative
        if not path.absolute().is_relative_to(root_absolute):
            return False
        if path.is_symlink() or not path.is_file():
            return False
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            return False
    return True


def finalize_post_revision_review(*, batch_root: Path, review_path: Path) -> None:
    """Attach one fresh Day 1 review without mutating the sealed week receipt."""

    week_receipt = batch_root / "week-receipt.json"
    if not week_receipt.is_file() or week_receipt.is_symlink():
        raise ValueError("verified editorial week receipt is unavailable")
    value = json.loads(review_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("post-revision review must be a JSON object")
    review = cast(dict[str, Any], value)
    invocation = review.get("invocation")
    if not isinstance(invocation, dict):
        raise ValueError("post-revision reviewer provenance is missing")
    if (
        invocation.get("role") != "reviewer"
        or invocation.get("fresh_context") is not True
        or invocation.get("network_used") is not False
        or invocation.get("cost_usd") != 0
        or invocation.get("status") != "completed"
        or invocation.get("errors") != []
    ):
        raise ValueError("post-revision reviewer provenance is invalid")
    if review.get("overall_verdict") != "READY_FOR_HUMAN_PUBLICATION":
        raise ValueError("Day 1 is not ready for human publication")
    if review.get("material_findings") != []:
        raise ValueError("Day 1 still has material findings")
    target_hash = write_private_once(
        batch_root / "day-01" / "post-revision-review.json",
        _canonical_json(review),
    )
    receipt = {
        "schema_version": "0.1",
        "status": "READY_FOR_HUMAN_PUBLICATION",
        "week_receipt_sha256": hashlib.sha256(week_receipt.read_bytes()).hexdigest(),
        "post_revision_review_sha256": target_hash,
        "reviewer": {
            "provider": invocation.get("provider"),
            "model": invocation.get("model"),
            "reasoning": invocation.get("reasoning"),
            "prompt_version": invocation.get("prompt_version"),
            "fresh_context": True,
        },
        "network_used": False,
        "paid_api_used": False,
        "publication_performed": False,
        "human_gate_required": True,
    }
    write_private_once(
        batch_root / "final-validation-receipt.json",
        _canonical_json(receipt),
    )
