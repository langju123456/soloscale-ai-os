from __future__ import annotations

import re
from collections.abc import Sequence

from soloscale.casebook_models import (
    LearningCase,
    MasterySnapshot,
    PracticeStage,
)

_EXERCISES: dict[PracticeStage, tuple[str, str]] = {
    PracticeStage.EXPLAIN: (
        "Explain the incident from symptom to verified resolution without consulting the case.",
        (
            "Accurately connect expected and actual behavior, root cause, fix, verification, and "
            "remaining unknowns."
        ),
    ),
    PracticeStage.TRACE: (
        "Trace the relevant execution or data flow and identify where behavior diverged.",
        (
            "Name the important boundaries and place the failure at the correct boundary with a "
            "clear causal chain."
        ),
    ),
    PracticeStage.REBUILD: (
        "Rebuild the smallest representative version of the resolution from memory.",
        (
            "Produce a working minimal solution and verify it with evidence comparable to the "
            "original case."
        ),
    ),
    PracticeStage.DEBUG: (
        "Debug a changed version of the failure where the original fix is insufficient.",
        "Form and test falsifiable hypotheses, isolate the new cause, and verify the correction.",
    ),
    PracticeStage.DEFEND: (
        "Defend the diagnosis and solution under interview-style follow-up questions.",
        (
            "Explain alternatives and trade-offs, acknowledge unknowns, and avoid claims stronger "
            "than the evidence."
        ),
    ),
}

_PUBLIC_REPOSITORY_REFERENCE = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
)


def _render_repository_reference(repository: str | None) -> str:
    if repository is None:
        return "Not recorded"
    if _PUBLIC_REPOSITORY_REFERENCE.fullmatch(repository):
        return repository
    return "Private reference retained in the local case record"


def render_interview_packet(case: LearningCase, mastery: MasterySnapshot) -> str:
    """Render a concise, evidence-indexed practice packet without raw evidence bodies."""

    if mastery.case_id != case.id:
        raise ValueError("mastery snapshot and learning case must have the same case_id")

    lines = [
        f"# Interview Practice Packet — {case.title}",
        "",
        (
            "> **Self-assessment only:** “Interview ready” means every practice gate currently "
            "has a self-recorded pass. It is not an independent evaluation, hiring signal, or "
            "guarantee of interview performance."
        ),
        "",
        "## Case Brief",
        "",
        f"- Case: `{case.id}`",
        f"- Project: {case.project}",
        f"- Engineering state: `{case.engineering_state.value}`",
        f"- Repository: {_render_repository_reference(case.repository)}",
        "",
        case.problem,
        "",
        "## Expected vs. Actual",
        "",
        "### Expected",
        "",
        case.expected_behavior,
        "",
        "### Actual",
        "",
        case.actual_behavior,
        "",
        "## Root Cause",
        "",
        case.root_cause,
        "",
        "## Resolution",
        "",
        case.resolution,
        "",
        "## Verification",
        "",
        *_bullets(case.verification),
        "",
        "## Unknowns",
        "",
        *_bullets(case.unknowns, empty="No remaining unknowns recorded."),
        "",
    ]

    if case.alternatives_considered or case.trade_offs:
        lines.extend(
            [
                "## Engineering Judgment",
                "",
                "### Alternatives Considered",
                "",
                *_bullets(case.alternatives_considered),
                "",
                "### Trade-offs",
                "",
                *_bullets(case.trade_offs),
                "",
            ]
        )

    lines.extend(["## Evidence Index", ""])
    if case.evidence:
        for index, receipt in enumerate(case.evidence, start=1):
            lines.append(
                f"- E{index}: `{receipt.kind.value}` — receipt `{receipt.id}`; "
                f"SHA-256 `{receipt.sha256}`; {receipt.byte_size} bytes"
            )
    else:
        lines.append("_No evidence receipts recorded._")

    lines.extend(["", "## Concept Checklist", ""])
    if case.concepts:
        lines.extend(f"- [ ] {concept}" for concept in case.concepts)
    else:
        lines.append("_No concepts recorded._")

    lines.extend(["", "## Practice Exercises", ""])
    for position, stage in enumerate(PracticeStage, start=1):
        exercise, pass_criterion = _EXERCISES[stage]
        outcome = mastery.stage_results[stage]
        result = outcome.value if outcome is not None else "not-attempted"
        lines.extend(
            [
                f"### {position}. {stage.value.title()}",
                "",
                f"- Exercise: {exercise}",
                f"- Pass criterion: {pass_criterion}",
                f"- Latest self-assessed result: `{result}`",
                "",
            ]
        )

    passed = ", ".join(stage.value for stage in mastery.passed_stages) or "none"
    next_stage = mastery.next_stage.value if mastery.next_stage is not None else "none"
    lines.extend(
        [
            "## Mastery Status",
            "",
            f"- Derived status: `{mastery.status.value}`",
            f"- Passed gates: {passed}",
            f"- Next gate: `{next_stage}`",
            f"- Self-assessed interview ready: `{'yes' if mastery.interview_ready else 'no'}`",
            "",
        ]
    )
    return "\n".join(lines)


def render_interview_packet_markdown(
    case: LearningCase, mastery: MasterySnapshot
) -> str:
    """Descriptive alias for callers that render multiple packet formats."""

    return render_interview_packet(case, mastery)


def _bullets(values: Sequence[str], *, empty: str = "None recorded.") -> list[str]:
    if not values:
        return [f"_{empty}_"]
    return [f"- {value}" for value in values]
