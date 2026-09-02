#!/usr/bin/env python3
"""Assemble reviewed Codex outputs into private, checksum-backed editorial packages."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from soloscale.editorial_models import (
    AuthorVoiceProfile,
    EditorialProvenance,
    EditorialRole,
    ProviderIdentity,
    ProviderKind,
    ReviewResult,
    RevisionResult,
    RunStatus,
)
from soloscale.editorial_pipeline import PrivateWriteError, write_private_once
from soloscale.editorial_workspace import (
    EditorialArtifacts,
    EditorialPackage,
    EvidenceAnchor,
    verify_editorial_package,
    write_author_voice_profile,
    write_editorial_package,
    x_weighted_length,
)
from soloscale.visual_planner import (
    VisualBrief,
    VisualClassification,
    VisualPath,
    VisualType,
    plan_visual,
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return cast(dict[str, Any], value)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invocation timestamp is missing")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _provenance(
    invocation: dict[str, Any],
    *,
    role: EditorialRole,
    input_artifacts: dict[str, str],
    output_artifacts: dict[str, str],
) -> EditorialProvenance:
    if invocation.get("role") != role.value:
        raise ValueError(f"{role.value} invocation role is invalid")
    if invocation.get("network_used") is not False or invocation.get("cost_usd") != 0:
        raise ValueError(f"{role.value} invocation crossed the no-network/no-cost boundary")
    if invocation.get("status") != "completed" or invocation.get("errors") != []:
        raise ValueError(f"{role.value} invocation did not complete cleanly")
    model = str(invocation.get("model") or "UNKNOWN")
    provider = str(invocation.get("provider") or "UNKNOWN")
    return EditorialProvenance(
        role=role,
        provider=ProviderIdentity(
            kind=ProviderKind.CODEX_SESSION,
            provider=provider,
            model=model,
        ),
        exact_model=model,
        reasoning=str(invocation.get("reasoning") or "UNKNOWN"),
        prompt_version=str(invocation.get("prompt_version") or "UNKNOWN"),
        input_artifact_hashes={
            name: _sha256_text(value) for name, value in input_artifacts.items()
        },
        output_artifact_hashes={
            name: _sha256_text(value) for name, value in output_artifacts.items()
        },
        started_at=_parse_timestamp(invocation.get("started_at")),
        completed_at=_parse_timestamp(invocation.get("completed_at")),
        network_used=False,
        token_usage=cast(dict[str, int] | None, invocation.get("token_usage")),
        cost_usd=0,
        status=RunStatus.SUCCEEDED,
        errors=[],
        fresh_context=bool(invocation.get("fresh_context")),
    )


def _by_day(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    days = payload.get("days")
    if not isinstance(days, list):
        raise ValueError("editorial payload is missing days")
    result: dict[int, dict[str, Any]] = {}
    for raw in days:
        if not isinstance(raw, dict) or not isinstance(raw.get("day"), int):
            raise ValueError("editorial day is invalid")
        day = cast(int, raw["day"])
        if day in result:
            raise ValueError("editorial payload repeats a day")
        result[day] = cast(dict[str, Any], raw)
    if set(result) != set(range(1, 8)):
        raise ValueError("editorial payload must contain days 1 through 7")
    return result


def _file_bodies(day: dict[str, Any]) -> dict[str, str]:
    final = day.get("final")
    source = cast(dict[str, Any], final) if isinstance(final, dict) else day
    thread = source.get("x_thread")
    if not isinstance(thread, list) or not all(isinstance(post, str) for post in thread):
        raise ValueError("X thread is invalid")
    return {
        "canonical-story.md": str(source.get("canonical_story", "")).rstrip() + "\n",
        "linkedin.md": str(source.get("linkedin", "")).rstrip() + "\n",
        "x-thread.md": "\n\n".join(str(post).strip() for post in thread) + "\n",
        "x-post.md": str(source.get("x_post", "")).rstrip() + "\n",
    }


def _flatten_labels(value: object) -> list[str]:
    labels: list[str] = []
    if isinstance(value, str):
        labels.append(value)
    elif isinstance(value, list):
        for item in value:
            labels.extend(_flatten_labels(item))
    elif isinstance(value, dict):
        for item in value.values():
            labels.extend(_flatten_labels(item))
    return labels


def _visual_type(day_number: int, label: str) -> VisualType:
    lowered = label.lower()
    if day_number in {1, 2} or "comparison" in lowered or "matrix" in lowered:
        return VisualType.DECISION_COMPARISON
    if "architecture" in lowered or "hub" in lowered or "spoke" in lowered:
        return VisualType.ARCHITECTURE_DIAGRAM
    if "card" in lowered:
        return VisualType.INSIGHT_CARD
    return VisualType.PROCESS_FLOW


def _visual_plan(day: dict[str, Any]) -> object:
    raw = day.get("visual_plan")
    editable = day.get("editable_diagram_source")
    if not isinstance(raw, dict) or not isinstance(editable, dict):
        raise ValueError("visual plan is invalid")
    paths: list[VisualPath] = []
    editable_spec = editable.get("spec")
    if isinstance(editable_spec, dict) and isinstance(editable_spec.get("columns"), list):
        for column in editable_spec["columns"]:
            if isinstance(column, dict) and isinstance(column.get("items"), list):
                paths.append(
                    VisualPath(
                        heading=str(column.get("heading")),
                        steps=[str(item) for item in column["items"]],
                    )
                )
    elif isinstance(editable_spec, dict) and isinstance(editable_spec.get("left"), dict):
        for side in (editable_spec.get("left"), editable_spec.get("right")):
            if isinstance(side, dict) and isinstance(side.get("items"), list):
                paths.append(
                    VisualPath(
                        heading=str(side.get("heading")),
                        steps=[str(item) for item in side["items"]],
                    )
                )
    labels = (
        [label for path in paths for label in (path.heading, *path.steps)]
        if paths
        else _flatten_labels(raw.get("exact_labels"))
    )
    variants = raw.get("variants")
    platform_variants = {
        f"variant-{index}": str(value)
        for index, value in enumerate(variants, start=1)
    } if isinstance(variants, list) else {}
    unsupported = raw.get("unsupported_check")
    unsupported_text = (
        _canonical_json(unsupported).strip() if unsupported is not None else "UNKNOWN"
    )
    if len(unsupported_text) > 800:
        unsupported_text = unsupported_text[:797] + "..."
    title = str(day.get("topic"))
    if isinstance(editable_spec, dict) and editable_spec.get("title"):
        title = str(editable_spec["title"])
    allowed_type = str(raw.get("allowed_visual_type", "process flow"))
    brief = VisualBrief(
        visual_type=_visual_type(int(day["day"]), allowed_type),
        single_idea=str(raw.get("single_idea")),
        title=title[:140],
        source_evidence_ids=[str(item) for item in raw.get("source_evidence_ids", [])],
        exact_labels=labels,
        paths=paths,
        layout=str(raw.get("layout")),
        visual_hierarchy=[str(raw.get("hierarchy"))],
        classification=VisualClassification.PUBLIC_SAFE,
        platform_variants=platform_variants,
        alt_text=str(raw.get("alt_text")),
        unsupported_information_check=unsupported_text,
    )
    return plan_visual(
        brief,
        editable_format=str(editable.get("format", "json+svg")),
        editable_source=cast(dict[str, object], editable),
    )


def _author_profile(writer: dict[str, Any]) -> AuthorVoiceProfile:
    raw = writer.get("author_voice_profile")
    invocation = writer.get("invocation")
    if not isinstance(raw, dict) or not isinstance(invocation, dict):
        raise ValueError("writer payload is missing the author voice profile")
    traits = [str(item) for item in raw.get("tone", [])]
    traits.extend([str(raw.get("stance")), str(raw.get("privacy_rule"))])
    return AuthorVoiceProfile(
        profile_id="solo-builder-peer",
        version="v1",
        voice_traits=traits,
        preferred_phrases=[str(item) for item in raw.get("preferred_boundary_phrases", [])],
        prohibited_phrases=[str(item) for item in raw.get("prohibited_claims", [])],
        audience_notes=str(raw.get("identity")),
        updated_at=_parse_timestamp(invocation.get("completed_at")),
    )


def _review_result(
    review_day: dict[str, Any],
    review_invocation: dict[str, Any],
    writer_day: dict[str, Any],
) -> ReviewResult:
    payload = {key: value for key, value in review_day.items() if key != "day"}
    provenance = _provenance(
        review_invocation,
        role=EditorialRole.REVIEWER,
        input_artifacts={
            **_file_bodies(writer_day),
            "evidence-manifest.json": _canonical_json(writer_day.get("evidence_manifest")),
        },
        output_artifacts={"structured-review-output.json": _canonical_json(payload)},
    )
    return ReviewResult.model_validate({**payload, "provenance": provenance})


def _revision_result(
    reviser_day: dict[str, Any],
    reviser_invocation: dict[str, Any],
    writer_day: dict[str, Any],
    review_day: dict[str, Any],
) -> RevisionResult:
    provenance = _provenance(
        reviser_invocation,
        role=EditorialRole.REVISER,
        input_artifacts={
            **_file_bodies(writer_day),
            "evidence-manifest.json": _canonical_json(writer_day.get("evidence_manifest")),
            "structured-review.json": _canonical_json(review_day),
        },
        output_artifacts=_file_bodies(reviser_day),
    )
    return RevisionResult.model_validate(
        {"decisions": reviser_day.get("revision_decisions", []), "provenance": provenance}
    )


def _package(
    day_number: int,
    *,
    writer_day: dict[str, Any],
    review_day: dict[str, Any],
    reviser_day: dict[str, Any],
    writer_invocation: dict[str, Any],
    review_invocation: dict[str, Any],
    reviser_invocation: dict[str, Any],
) -> EditorialPackage:
    if reviser_day.get("structured_review") != review_day:
        raise ValueError(f"day {day_number} does not preserve the independent review")
    final = reviser_day.get("final")
    if not isinstance(final, dict):
        raise ValueError(f"day {day_number} is missing revised artifacts")
    artifacts = EditorialArtifacts(
        canonical_story=str(final.get("canonical_story")),
        linkedin=str(final.get("linkedin")),
        x_thread=[str(item) for item in final.get("x_thread", [])],
        x_post=str(final.get("x_post")),
    )
    writer_receipt = _provenance(
        writer_invocation,
        role=EditorialRole.WRITER,
        input_artifacts={
            "topic": str(writer_day.get("topic")),
            "evidence-manifest.json": _canonical_json(writer_day.get("evidence_manifest")),
        },
        output_artifacts=_file_bodies(writer_day),
    )
    review_result = _review_result(review_day, review_invocation, writer_day)
    revision_result = _revision_result(
        reviser_day, reviser_invocation, writer_day, review_day
    )
    evidence = [
        EvidenceAnchor(
            evidence_id=str(item.get("id")),
            source_label=str(item.get("source_label")),
            factual_boundary=str(item.get("boundary")),
        )
        for item in reviser_day.get("evidence_manifest", [])
        if isinstance(item, dict)
    ]
    return EditorialPackage(
        package_id=f"day-{day_number:02d}-{_slug(str(reviser_day.get('topic')))}",
        day=day_number,
        status=cast(Any, reviser_day.get("status")),
        topic=str(reviser_day.get("topic")),
        audience=str(reviser_day.get("audience")),
        author_voice_profile_id="solo-builder-peer-v1",
        evidence_manifest=evidence,
        factual_gaps=[str(item) for item in reviser_day.get("factual_gaps", [])],
        artifacts=artifacts,
        visual_plan=cast(Any, _visual_plan(reviser_day)),
        writer=writer_receipt,
        reviewer=review_result,
        revision=revision_result,
    )


def _slug(value: str) -> str:
    cleaned = "".join(character.lower() if character.isalnum() else "-" for character in value)
    return "-".join(part for part in cleaned.split("-") if part)[:80] or "story"


def _topic_additions(candidates: list[object]) -> str:
    lines = ["# Topic Bank Additions — 2026-08-13", ""]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        lines.extend(
            [
                f"## Rank {candidate.get('rank')} — {candidate.get('topic')}",
                "",
                f"- Audience pain: {candidate.get('audience_pain')}",
                f"- Real event: {candidate.get('real_event')}",
                f"- Old belief: {candidate.get('old_belief')}",
                f"- New belief: {candidate.get('new_belief')}",
                "- Evidence IDs: "
                + ", ".join(str(item) for item in candidate.get("evidence_ids", [])),
                f"- Hook: {candidate.get('hook')}",
                f"- Diagram: {candidate.get('diagram')}",
                f"- Factual boundary: {candidate.get('factual_boundary')}",
                f"- Status: {candidate.get('status')}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _append_topic_bank(path: Path, candidates: list[object]) -> tuple[str, str]:
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    marker = "## 2026-08-13 — Editorial sprint additions"
    current = path.read_text(encoding="utf-8")
    if marker in current:
        raise ValueError("topic bank already contains this editorial sprint")
    selected = [item for item in candidates if isinstance(item, dict)][1:]
    addition = ["", marker, ""]
    for index, candidate in enumerate(selected, start=2):
        addition.extend(
            [
                f"### TOPIC-{index:03d} — {candidate.get('topic')}",
                "",
                f"**Audience Pain:** {candidate.get('audience_pain')}",
                "",
                f"**Real Event:** {candidate.get('real_event')}",
                "",
                f"**Old Belief:** {candidate.get('old_belief')}",
                "",
                f"**New Belief:** {candidate.get('new_belief')}",
                "",
                "**Evidence IDs:** "
                + ", ".join(str(item) for item in candidate.get("evidence_ids", [])),
                "",
                f"**Possible Hook:** {candidate.get('hook')}",
                "",
                f"**Possible Diagram:** {candidate.get('diagram')}",
                "",
                f"**Factual Boundary:** {candidate.get('factual_boundary')}",
                "",
                f"**Status:** {candidate.get('status')}",
                "",
            ]
        )
    data = ("\n".join(addition).rstrip() + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise OSError("short topic-bank append")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)
    return before, hashlib.sha256(path.read_bytes()).hexdigest()


def _review_index(packages: list[EditorialPackage]) -> str:
    cards: list[str] = []
    for package in packages:
        directory = f"day-{package.day:02d}"
        cards.append(
            f'<article><span>DAY {package.day} · {html.escape(package.status)}</span>'
            f"<h2>{html.escape(package.topic)}</h2>"
            f'<img src="{directory}/visual/diagram.png" '
            f'alt="{html.escape(package.visual_plan.brief.alt_text)}" />'
            f'<p><a href="{directory}/linkedin.md">LinkedIn</a> · '
            f'<a href="{directory}/x-thread.md">X Thread</a> · '
            f'<a href="{directory}/canonical-story.md">Canonical Story</a> · '
            f'<a href="{directory}/structured-review.json">Review</a></p></article>'
        )
    head = """<!doctype html>
<html><head><meta charset="utf-8"><title>SoloScale Editorial Week</title>
<style>
body { font:16px/1.5 system-ui; max-width:1080px; margin:40px auto; padding:0 20px;
  background:#f5f7fb; color:#182230; }
h1 { font-size:44px; }
article { background:white; padding:24px; border-radius:20px; margin:20px 0; }
article span { color:#3157d5; font-weight:800; font-size:12px; }
img { width:100%; border-radius:14px; border:1px solid #ddd; }
a { color:#3157d5; }
</style></head><body>
<h1>SoloScale · First Editorial Week</h1>
<p>Private review only. Nothing here has been published.</p>
"""
    return head + "\n".join(cards) + "\n</body></html>\n"


def assemble(
    *,
    writer_path: Path,
    review_path: Path,
    reviser_path: Path,
    output_root: Path,
    topic_bank_path: Path,
) -> None:
    if output_root.exists() or output_root.is_symlink():
        raise PrivateWriteError("editorial week output already exists")
    ancestry = (output_root.parent, *output_root.parents)
    if any((candidate / ".git").exists() for candidate in ancestry):
        raise PrivateWriteError("private editorial output cannot be inside a Git repository")
    writer = _load_json(writer_path)
    review = _load_json(review_path)
    reviser = _load_json(reviser_path)
    writer_days, review_days, reviser_days = map(_by_day, (writer, review, reviser))
    invocations = (writer.get("invocation"), review.get("invocation"), reviser.get("invocation"))
    if not all(isinstance(item, dict) for item in invocations):
        raise ValueError("one or more invocation receipts are missing")
    writer_invocation, review_invocation, reviser_invocation = cast(
        tuple[dict[str, Any], dict[str, Any], dict[str, Any]], invocations
    )
    output_root.mkdir(mode=0o700)
    os.chmod(output_root, 0o700)
    profile = _author_profile(writer)
    voice_path = write_author_voice_profile(output_root / "author-voice", profile)
    packages: list[EditorialPackage] = []
    day_receipts: dict[str, str] = {}
    for day_number in range(1, 8):
        package = _package(
            day_number,
            writer_day=writer_days[day_number],
            review_day=review_days[day_number],
            reviser_day=reviser_days[day_number],
            writer_invocation=writer_invocation,
            review_invocation=review_invocation,
            reviser_invocation=reviser_invocation,
        )
        day_root = output_root / f"day-{day_number:02d}"
        write_editorial_package(day_root, package, try_png=True)
        if not verify_editorial_package(day_root):
            raise ValueError(f"day {day_number} package failed checksum verification")
        receipt_path = day_root / "receipt.json"
        day_receipts[f"day-{day_number:02d}/receipt.json"] = hashlib.sha256(
            receipt_path.read_bytes()
        ).hexdigest()
        packages.append(package)
    candidates = writer.get("ranked_topic_candidates")
    if not isinstance(candidates, list):
        raise ValueError("writer payload is missing ranked topic candidates")
    additions_path = output_root / "topic-bank-additions.md"
    write_private_once(additions_path, _topic_additions(candidates))
    write_private_once(output_root / "review-index.html", _review_index(packages))
    topic_before, topic_after = _append_topic_bank(topic_bank_path, candidates)
    receipt = {
        "schema_version": "0.1",
        "status": "EDITORIAL_AND_VISUAL_WEEK_READY_FOR_HUMAN_REVIEW",
        "writer_input_sha256": hashlib.sha256(writer_path.read_bytes()).hexdigest(),
        "review_input_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
        "reviser_input_sha256": hashlib.sha256(reviser_path.read_bytes()).hexdigest(),
        "author_voice_sha256": hashlib.sha256(voice_path.read_bytes()).hexdigest(),
        "day_receipts": day_receipts,
        "topic_bank_additions_sha256": hashlib.sha256(additions_path.read_bytes()).hexdigest(),
        "topic_bank_before_sha256": topic_before,
        "topic_bank_after_sha256": topic_after,
        "models": {
            "writer": writer_invocation.get("model"),
            "reviewer": review_invocation.get("model"),
            "reviser": reviser_invocation.get("model"),
        },
        "network_used": False,
        "paid_api_used": False,
        "publication_performed": False,
        "human_gate_required": True,
        "x_weighted_lengths": {
            f"day-{package.day:02d}": {
                "thread": [x_weighted_length(post) for post in package.artifacts.x_thread],
                "standalone": x_weighted_length(package.artifacts.x_post),
            }
            for package in packages
        },
    }
    write_private_once(output_root / "week-receipt.json", _canonical_json(receipt))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--writer", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--reviser", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--topic-bank", type=Path, required=True)
    args = parser.parse_args()
    assemble(
        writer_path=args.writer.absolute(),
        review_path=args.review.absolute(),
        reviser_path=args.reviser.absolute(),
        output_root=args.output_root.absolute(),
        topic_bank_path=args.topic_bank.absolute(),
    )


if __name__ == "__main__":
    main()
