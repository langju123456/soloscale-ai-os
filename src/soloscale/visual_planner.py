"""Deterministic, public-safe visual planning for editorial packages."""

from __future__ import annotations

import hashlib
import html
import io
import json
import re
import shutil
import subprocess
import tempfile
from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator

from soloscale.editorial_pipeline import sha256_text, write_private_once
from soloscale.models import ContractModel


class VisualType(StrEnum):
    EVIDENCE_SCREENSHOT = "EVIDENCE_SCREENSHOT"
    PROCESS_FLOW = "PROCESS_FLOW"
    ARCHITECTURE_DIAGRAM = "ARCHITECTURE_DIAGRAM"
    DECISION_COMPARISON = "DECISION_COMPARISON"
    TIMELINE = "TIMELINE"
    INSIGHT_CARD = "INSIGHT_CARD"
    CAROUSEL_PLAN = "CAROUSEL_PLAN"
    GENERATED_ILLUSTRATION = "GENERATED_ILLUSTRATION"
    NO_VISUAL_NEEDED = "NO_VISUAL_NEEDED"


class VisualClassification(StrEnum):
    PUBLIC_SAFE = "PUBLIC_SAFE"
    PRIVATE_INTERNAL = "PRIVATE_INTERNAL"


class VisualPath(ContractModel):
    heading: str = Field(min_length=1, max_length=80)
    steps: list[str] = Field(min_length=1, max_length=12)


_PRIVATE_PATH = re.compile(
    r"(?:file://|(?:^|[\s'\"])/(?:Users|home|private|var|tmp)/|[A-Za-z]:\\)", re.I
)
_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{12,}|AKIA[A-Z0-9]{12,}|"
    r"Bearer\s+[A-Za-z0-9._~+/=-]{12,})"
)


class VisualBrief(ContractModel):
    visual_type: VisualType
    single_idea: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=140)
    source_evidence_ids: list[str] = Field(default_factory=list, max_length=30)
    exact_labels: list[str] = Field(default_factory=list, max_length=32)
    paths: list[VisualPath] = Field(default_factory=list, max_length=3)
    layout: str = Field(min_length=1, max_length=300)
    visual_hierarchy: list[str] = Field(min_length=1, max_length=10)
    classification: VisualClassification = VisualClassification.PUBLIC_SAFE
    platform_variants: dict[str, str] = Field(default_factory=dict, max_length=8)
    alt_text: str = Field(min_length=1, max_length=800)
    unsupported_information_check: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def reject_private_content(self) -> VisualBrief:
        values = [
            self.single_idea,
            self.title,
            self.layout,
            self.alt_text,
            self.unsupported_information_check,
            *self.source_evidence_ids,
            *self.exact_labels,
            *self.visual_hierarchy,
            *self.platform_variants.keys(),
            *self.platform_variants.values(),
            *(path.heading for path in self.paths),
            *(step for path in self.paths for step in path.steps),
        ]
        if any(_PRIVATE_PATH.search(value) or _SECRET.search(value) for value in values):
            raise ValueError("visual brief contains a private path or credential-like value")
        if self.visual_type is VisualType.NO_VISUAL_NEEDED and (
            self.exact_labels or self.paths
        ):
            raise ValueError("NO_VISUAL_NEEDED cannot include labels or diagram paths")
        if self.paths:
            path_labels = [label for path in self.paths for label in (path.heading, *path.steps)]
            if path_labels != self.exact_labels:
                raise ValueError("exact_labels must preserve path headings and steps in order")
        return self


class VisualPlan(ContractModel):
    brief: VisualBrief
    editable_format: str = "json+svg"
    editable_source: dict[str, object] = Field(default_factory=dict, max_length=30)
    png_renderer: str | None = None
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def editable_source_must_be_public_safe(self) -> VisualPlan:
        serialized = json.dumps(self.editable_source, ensure_ascii=False)
        if _PRIVATE_PATH.search(serialized) or _SECRET.search(serialized):
            raise ValueError("editable visual source contains private or credential-like data")
        return self


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def plan_visual(
    brief: VisualBrief,
    *,
    editable_format: str = "json+svg",
    editable_source: dict[str, object] | None = None,
    png_renderer: str | None = None,
) -> VisualPlan:
    source = editable_source or {}
    digest = sha256_text(
        _canonical_json(
            {
                "brief": brief.model_dump(mode="json"),
                "editable_format": editable_format,
                "editable_source": source,
            }
        )
    )
    return VisualPlan(
        brief=brief,
        editable_format=editable_format,
        editable_source=source,
        png_renderer=png_renderer,
        plan_sha256=digest,
    )


def _wrapped_svg_text(value: str, *, x: int, y: int, size: int, weight: int = 500) -> str:
    escaped = html.escape(value)
    return (
        f'<text x="{x}" y="{y}" font-family="Inter,Arial,sans-serif" '
        f'font-size="{size}" font-weight="{weight}" fill="#182230">{escaped}</text>'
    )


def render_editable_svg(plan: VisualPlan) -> str:
    """Render an editable SVG; path diagrams become clear comparison columns."""

    brief = plan.brief
    if brief.visual_type is VisualType.NO_VISUAL_NEEDED:
        return ""
    title = _wrapped_svg_text(brief.title, x=56, y=72, size=34, weight=750)
    subtitle = _wrapped_svg_text(brief.single_idea, x=56, y=110, size=18, weight=450)
    if brief.paths:
        column_width = 510 if len(brief.paths) == 2 else 1050 // len(brief.paths)
        columns: list[str] = []
        for column_index, path in enumerate(brief.paths):
            x = 56 + column_index * (column_width + 38)
            accent = (
                "#E24A4A"
                if column_index == 0 and len(brief.paths) == 2
                else "#138A61" if len(brief.paths) == 2 else "#3157D5"
            )
            columns.append(
                f'<rect x="{x}" y="146" width="{column_width}" height="420" rx="22" '
                f'fill="#F8FAFC" stroke="{accent}" stroke-width="3"/>'
            )
            columns.append(_wrapped_svg_text(path.heading, x=x + 26, y=190, size=22, weight=800))
            for step_index, step in enumerate(path.steps):
                step_y = 235 + step_index * 43
                if step_index:
                    columns.append(
                        f'<path d="M {x + 37} {step_y - 30} L {x + 37} {step_y - 15}" '
                        f'stroke="{accent}" stroke-width="3"/>'
                    )
                    columns.append(
                        f'<path d="M {x + 31} {step_y - 20} L {x + 37} {step_y - 14} '
                        f'L {x + 43} {step_y - 20}" fill="none" stroke="{accent}" '
                        'stroke-width="3"/>'
                    )
                columns.append(f'<circle cx="{x + 37}" cy="{step_y - 6}" r="7" fill="{accent}"/>')
                columns.append(_wrapped_svg_text(step, x=x + 58, y=step_y, size=17, weight=600))
        body = "".join(columns)
    else:
        labels = brief.exact_labels or [brief.visual_type.replace("_", " ").title()]
        if len(labels) > 7:
            fragments: list[str] = []
            for index, label in enumerate(labels[:14]):
                x = 56 + (index % 2) * 548
                y = 146 + (index // 2) * 56
                fragments.append(
                    f'<rect x="{x}" y="{y}" width="520" height="44" '
                    'rx="12" fill="#F2F4F7"/>'
                )
                fragments.append(
                    _wrapped_svg_text(label, x=x + 16, y=y + 28, size=15, weight=650)
                )
            body = "".join(fragments)
        else:
            body = "".join(
                f'<rect x="56" y="{146 + index * 64}" width="1088" height="52" '
                'rx="14" fill="#F2F4F7"/>'
                f'{_wrapped_svg_text(label, x=78, y=179 + index * 64, size=19, weight=650)}'
                for index, label in enumerate(labels)
            )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" '
        'viewBox="0 0 1200 630" role="img">'
        f"<title>{html.escape(brief.alt_text)}</title>"
        '<rect width="1200" height="630" fill="#EEF2FF"/>'
        '<rect x="24" y="24" width="1152" height="582" rx="28" fill="#FFFFFF"/>'
        f"{title}{subtitle}{body}</svg>\n"
    )


def render_svg_to_png(svg: str) -> tuple[bytes | None, str | None]:
    """Use an available macOS renderer; failure remains an optional-renderer result."""

    if not svg:
        return None, None
    with tempfile.TemporaryDirectory(prefix="soloscale-visual-") as directory:
        source = Path(directory) / "diagram.svg"
        output = Path(directory) / "diagram.png"
        source.write_text(svg, encoding="utf-8")
        sips = shutil.which("sips")
        if sips is not None:
            completed = subprocess.run(
                [sips, "-s", "format", "png", str(source), "--out", str(output)],
                check=False,
                capture_output=True,
                timeout=30,
            )
            if completed.returncode == 0 and output.is_file():
                return output.read_bytes(), "sips"
        quicklook = shutil.which("qlmanage")
        if quicklook is not None:
            preview_root = Path(directory) / "preview"
            preview_root.mkdir()
            completed = subprocess.run(
                [quicklook, "-t", "-s", "1200", "-o", str(preview_root), str(source)],
                check=False,
                capture_output=True,
                timeout=30,
            )
            preview = preview_root / "diagram.svg.png"
            if completed.returncode == 0 and preview.is_file():
                return preview.read_bytes(), "qlmanage"
        return None, None


def render_plan_to_png(plan: VisualPlan) -> tuple[bytes | None, str | None]:
    """Render a matched PNG with optional Pillow, without a model or network call."""

    if plan.brief.visual_type is VisualType.NO_VISUAL_NEEDED:
        return None, None
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None, None

    regular_path = "/System/Library/Fonts/Supplemental/Arial.ttf"
    bold_path = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

    def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(bold_path if bold else regular_path, size=size)

    image = Image.new("RGB", (1200, 630), "#EEF2FF")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, 1176, 606), radius=28, fill="#FFFFFF")
    draw.text((56, 47), plan.brief.title, font=font(34, bold=True), fill="#182230")
    draw.text((56, 92), plan.brief.single_idea, font=font(18), fill="#344054")
    if plan.brief.paths:
        column_width = 510 if len(plan.brief.paths) == 2 else 1050 // len(plan.brief.paths)
        for column_index, path in enumerate(plan.brief.paths):
            x = 56 + column_index * (column_width + 38)
            accent = (
                "#E24A4A"
                if column_index == 0 and len(plan.brief.paths) == 2
                else "#138A61" if len(plan.brief.paths) == 2 else "#3157D5"
            )
            draw.rounded_rectangle(
                (x, 146, x + column_width, 566),
                radius=22,
                fill="#F8FAFC",
                outline=accent,
                width=3,
            )
            draw.text((x + 26, 166), path.heading, font=font(22, bold=True), fill="#182230")
            for step_index, step in enumerate(path.steps):
                step_y = 224 + step_index * 43
                if step_index:
                    draw.line((x + 37, step_y - 26, x + 37, step_y - 11), fill=accent, width=3)
                    draw.polygon(
                        ((x + 31, step_y - 16), (x + 37, step_y - 10), (x + 43, step_y - 16)),
                        fill=accent,
                    )
                draw.ellipse((x + 30, step_y - 6, x + 44, step_y + 8), fill=accent)
                draw.text((x + 58, step_y - 10), step, font=font(17, bold=True), fill="#182230")
    else:
        labels = plan.brief.exact_labels or [plan.brief.visual_type.replace("_", " ").title()]
        if len(labels) > 7:
            for index, label in enumerate(labels[:14]):
                x = 56 + (index % 2) * 548
                y = 146 + (index // 2) * 56
                draw.rounded_rectangle((x, y, x + 520, y + 44), radius=12, fill="#F2F4F7")
                draw.text((x + 16, y + 13), label, font=font(15, bold=True), fill="#182230")
        else:
            for index, label in enumerate(labels):
                y = 146 + index * 64
                draw.rounded_rectangle((56, y, 1144, y + 52), radius=14, fill="#F2F4F7")
                draw.text((78, y + 14), label, font=font(19, bold=True), fill="#182230")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue(), "pillow"


def _visual_brief_markdown(brief: VisualBrief) -> str:
    return "\n".join(
        [
            f"# {brief.title}",
            "",
            f"- Visual type: {brief.visual_type.value}",
            f"- Single idea: {brief.single_idea}",
            f"- Layout: {brief.layout}",
            f"- Classification: {brief.classification.value}",
            f"- Evidence IDs: {', '.join(brief.source_evidence_ids) or 'none'}",
            f"- Unsupported-information check: {brief.unsupported_information_check}",
            "",
            "## Exact labels",
            *[f"- {label}" for label in brief.exact_labels],
            "",
        ]
    )


def write_visual_package(
    root: Path,
    plan: VisualPlan,
    *,
    try_png: bool = True,
    status: str = "READY_FOR_HUMAN_REVIEW",
) -> dict[str, str]:
    """Write one complete, non-overwriting visual package and hash receipt."""

    svg = render_editable_svg(plan)
    png_bytes, png_renderer = render_plan_to_png(plan) if try_png else (None, None)
    if try_png and png_bytes is None:
        png_bytes, png_renderer = render_svg_to_png(svg)
    artifacts: dict[str, str | bytes] = {
        "visual-plan.json": _canonical_json(plan.model_dump(mode="json")),
        "visual-brief.md": _visual_brief_markdown(plan.brief),
        "diagram-source.json": _canonical_json(
            plan.editable_source
            or {
                "format": "soloscale-path-diagram-v1",
                "paths": [path.model_dump(mode="json") for path in plan.brief.paths],
                "labels": plan.brief.exact_labels,
            }
        ),
        "diagram.svg": svg,
        "alt-text.md": plan.brief.alt_text.strip() + "\n",
    }
    if png_bytes is not None:
        artifacts["diagram.png"] = png_bytes
    hashes = {name: write_private_once(root / name, body) for name, body in artifacts.items()}
    receipt = {
        "schema_version": "0.1",
        "status": status,
        "visual_type": plan.brief.visual_type.value,
        "classification": plan.brief.classification.value,
        "editable_format": plan.editable_format,
        "plan_sha256": plan.plan_sha256,
        "png_renderer": png_renderer,
        "network_used": False,
        "paid_api_used": False,
        "artifacts": hashes,
    }
    receipt_text = _canonical_json(receipt)
    hashes["visual-receipt.json"] = write_private_once(root / "visual-receipt.json", receipt_text)
    return hashes


def verify_visual_package(root: Path) -> bool:
    receipt_path = root / "visual-receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        return False
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict):
        return False
    return all(
        (root / name).is_file()
        and hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
        for name, expected in artifacts.items()
    )
