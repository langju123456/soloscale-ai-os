from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from soloscale.casebook_models import (
    AttemptOutcome,
    DerivedCaseStatus,
    LearningCase,
    MasterySnapshot,
    PracticeStage,
)
from soloscale.casebook_store import CasebookStore, IntegrityReport, derive_mastery
from soloscale.knowledge_store import KnowledgeStore, KnowledgeStoreError
from soloscale.models import SCHEMA_VERSION


@dataclass(frozen=True)
class _CaseView:
    case: LearningCase
    mastery: MasterySnapshot
    integrity: IntegrityReport
    latest_recorded_at: datetime


@dataclass(frozen=True)
class _Summary:
    active_cases: int
    evidence_gaps: int
    practices_waiting: int
    interview_ready: int


@dataclass(frozen=True)
class _KnowledgeView:
    state: str
    documents: int
    chunks: int
    source_counts: dict[str, int]
    last_synced_at: datetime | None
    completed_runs: int
    failed_runs: int
    pending_runs: int
    next_action: str


_STAGE_FOCUS = {
    PracticeStage.EXPLAIN: "Explain the incident clearly from memory",
    PracticeStage.TRACE: "Trace the failure from symptom to root cause",
    PracticeStage.REBUILD: "Rebuild the resolution without copying the archive",
    PracticeStage.DEBUG: "Debug a variation of the original failure",
    PracticeStage.DEFEND: "Defend the decision, alternatives, and trade-offs",
}

_STAGE_ACTION = {
    PracticeStage.EXPLAIN: (
        "Explain the problem, root cause, resolution, and verification from memory; "
        "then record a pass or needs-work attempt with its receipt."
    ),
    PracticeStage.TRACE: (
        "Trace the failing path from the visible symptom to the root cause; then record "
        "a pass or needs-work attempt with its receipt."
    ),
    PracticeStage.REBUILD: (
        "Rebuild the fix without copying archived evidence; then record a pass or "
        "needs-work attempt with its receipt."
    ),
    PracticeStage.DEBUG: (
        "Reproduce and diagnose a meaningful variation of the failure; then record a "
        "pass or needs-work attempt with its receipt."
    ),
    PracticeStage.DEFEND: (
        "Defend the chosen resolution against its alternatives and trade-offs; then "
        "record a pass or needs-work attempt with its receipt."
    ),
}

_MASTERY_LABEL = {
    DerivedCaseStatus.CAPTURED: "Captured",
    DerivedCaseStatus.IN_PRACTICE: "In practice",
    DerivedCaseStatus.SELF_ASSESSED_INTERVIEW_READY: "Self-assessed interview-ready",
}

_DASHBOARD_DIRECTORY_MODE = 0o700
_DASHBOARD_FILE_MODE = 0o600


def _e(value: object) -> str:
    """Escape every value before it crosses the HTML boundary."""

    return escape(str(value), quote=True)


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or f"{singular}s")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _summarize(views: list[_CaseView]) -> _Summary:
    return _Summary(
        active_cases=len(views),
        evidence_gaps=sum(view.integrity.evidence_gap for view in views),
        practices_waiting=sum(
            len(PracticeStage) - len(view.mastery.passed_stages) for view in views
        ),
        interview_ready=sum(view.mastery.interview_ready for view in views),
    )


def _render_summary(summary: _Summary) -> str:
    cards = (
        (
            "Active cases",
            summary.active_cases,
            "Local learning cases in this Control Tower.",
        ),
        (
            "Evidence gaps",
            summary.evidence_gaps,
            "Cases with absent or invalid archived evidence receipts.",
        ),
        (
            "Practices waiting",
            summary.practices_waiting,
            "Stage gates without a current self-assessed pass.",
        ),
        (
            "Interview-ready",
            summary.interview_ready,
            "Cases with five current self-assessed passes.",
        ),
    )
    rendered = []
    for label, value, description in cards:
        rendered.append(
            "".join(
                (
                    '<div class="metric">',
                    f"<dt>{_e(label)}</dt>",
                    f"<dd><strong>{_e(value)}</strong><span>{_e(description)}</span></dd>",
                    "</div>",
                )
            )
        )
    return "\n".join(rendered)


def _knowledge_view(root: Path) -> _KnowledgeView:
    knowledge_root = root / "knowledge"
    database = knowledge_root / "index.sqlite3"
    if database.is_symlink() or knowledge_root.is_symlink():
        return _KnowledgeView(
            state="Attention required",
            documents=0,
            chunks=0,
            source_counts={},
            last_synced_at=None,
            completed_runs=0,
            failed_runs=0,
            pending_runs=0,
            next_action="Repair the private knowledge storage path before syncing again.",
        )
    if not database.is_file():
        return _KnowledgeView(
            state="Not synced",
            documents=0,
            chunks=0,
            source_counts={},
            last_synced_at=None,
            completed_runs=0,
            failed_runs=0,
            pending_runs=0,
            next_action="Run soloscale knowledge-sync to build the private evidence index.",
        )
    try:
        status = KnowledgeStore(root).status()
    except (KnowledgeStoreError, OSError, ValueError):
        return _KnowledgeView(
            state="Attention required",
            documents=0,
            chunks=0,
            source_counts={},
            last_synced_at=None,
            completed_runs=0,
            failed_runs=0,
            pending_runs=0,
            next_action="Inspect or reset the derived knowledge index before running an agent.",
        )

    completed = failed = pending = 0
    agent_runs = knowledge_root / "agent-runs"
    if agent_runs.is_dir() and not agent_runs.is_symlink():
        for run in agent_runs.iterdir():
            if not run.is_dir() or run.is_symlink():
                continue
            result = run / "04_result.json"
            failure = run / "failure.json"
            if failure.is_file() and not failure.is_symlink():
                failed += 1
            elif result.is_file() and not result.is_symlink():
                completed += 1
            else:
                pending += 1

    if failed:
        state = "Recovery review"
        next_action = "Review failed private agent receipts, then rerun only after remediation."
    elif completed:
        state = "Human confirmation"
        next_action = (
            "Review the latest candidate and its cited excerpts before promoting a Casebook record."
        )
    elif status.documents:
        state = "Ready for question"
        next_action = "Run soloscale evidence-agent with one concrete engineering question."
    else:
        state = "Empty index"
        next_action = "Select sources and run soloscale knowledge-sync."
    return _KnowledgeView(
        state=state,
        documents=status.documents,
        chunks=status.chunks,
        source_counts=status.source_counts,
        last_synced_at=status.last_synced_at,
        completed_runs=completed,
        failed_runs=failed,
        pending_runs=pending,
        next_action=next_action,
    )


def _render_knowledge(view: _KnowledgeView) -> str:
    source_summary = (
        ", ".join(f"{kind}: {count}" for kind, count in sorted(view.source_counts.items()))
        or "No indexed sources"
    )
    last_sync = (
        view.last_synced_at.replace(microsecond=0).isoformat()
        if view.last_synced_at is not None
        else "Never"
    )
    return f"""
<section class="knowledge-plane shell" aria-labelledby="knowledge-title">
  <div class="knowledge-heading">
    <div>
      <p class="section-label">Private retrieval plane</p>
      <h2 id="knowledge-title">Conversation RAG</h2>
    </div>
    <span class="knowledge-state">{_e(view.state)}</span>
  </div>
  <div class="knowledge-flow" aria-label="Conversation evidence workflow">
    <span>Selected conversations</span><b aria-hidden="true">→</b>
    <span>Private index</span><b aria-hidden="true">→</b>
    <span>Bounded Evidence Agent</span><b aria-hidden="true">→</b>
    <span>Human confirmation</span><b aria-hidden="true">→</b>
    <span>Casebook</span>
  </div>
  <dl class="knowledge-metrics">
    <div><dt>Documents</dt><dd>{_e(view.documents)}</dd></div>
    <div><dt>Chunks</dt><dd>{_e(view.chunks)}</dd></div>
    <div><dt>Completed runs</dt><dd>{_e(view.completed_runs)}</dd></div>
    <div><dt>Failed runs</dt><dd>{_e(view.failed_runs)}</dd></div>
  </dl>
  <p class="knowledge-meta">
    {_e(source_summary)} · Last sync: {_e(last_sync)} · Pending: {_e(view.pending_runs)}
  </p>
  <div class="knowledge-next"><strong>Exact next action</strong><p>{_e(view.next_action)}</p></div>
</section>
""".strip()


def _integrity_copy(report: IntegrityReport) -> tuple[str, str, str]:
    gap_count = len(report.failures)
    if gap_count:
        label = "Attention required"
        detail = (
            f"Integrity check found {gap_count} missing or changed archived "
            f"{_plural(gap_count, 'receipt')}."
        )
        return "attention", label, detail

    evidence_count = report.checked_evidence
    practice_count = report.checked_practice_receipts
    detail = (
        f"Verified {evidence_count} evidence {_plural(evidence_count, 'receipt')} and "
        f"{practice_count} practice {_plural(practice_count, 'receipt')}."
    )
    return "verified", "Verified", detail


def _focus_and_action(view: _CaseView) -> tuple[str, str]:
    gap_count = len(view.integrity.failures)
    if gap_count:
        focus = "Repair archived evidence integrity before the next practice gate"
        action = (
            f"Restore or deliberately recapture the {gap_count} missing or changed "
            f"archived {_plural(gap_count, 'receipt')}, then rebuild this Control Tower."
        )
        return focus, action

    next_stage = view.mastery.next_stage
    if next_stage is None:
        return (
            "Retain the case through interview rehearsal",
            (
                "Choose one stage to rehearse before the next interview and record the new "
                "self-assessment so readiness reflects the latest attempt."
            ),
        )
    return _STAGE_FOCUS[next_stage], _STAGE_ACTION[next_stage]


def _stage_state(
    stage: PracticeStage,
    mastery: MasterySnapshot,
) -> tuple[str, str, str, bool]:
    result = mastery.stage_results[stage]
    is_next = mastery.next_stage is stage
    if result is AttemptOutcome.PASS:
        return "passed", "✓", "Self-assessed pass", is_next
    if result is AttemptOutcome.NEEDS_WORK:
        label = "Self-assessed needs work — next" if is_next else "Self-assessed needs work"
        return "needs-work", "!", label, is_next
    if is_next:
        return "next", "→", "Next practice", True
    return "waiting", "•", "Waiting", False


def _render_stages(mastery: MasterySnapshot) -> str:
    items = []
    for stage in PracticeStage:
        state_class, marker, label, is_next = _stage_state(stage, mastery)
        current = ' aria-current="step"' if is_next else ""
        items.append(
            "".join(
                (
                    f'<li class="stage stage--{state_class}"{current}>',
                    f'<span class="stage-marker" aria-hidden="true">{_e(marker)}</span>',
                    '<span class="stage-copy">',
                    f'<strong class="stage-name">{_e(stage.value.title())}</strong>',
                    f'<span class="stage-state">{_e(label)}</span>',
                    "</span>",
                    "</li>",
                )
            )
        )
    return "\n".join(items)


def _render_concepts(case: LearningCase) -> str:
    return "\n".join(f"<li>{_e(concept)}</li>" for concept in case.concepts)


def _render_evidence(case: LearningCase) -> str:
    rows = []
    for receipt in case.evidence:
        rows.append(
            "".join(
                (
                    "<tr>",
                    f'<th scope="row">{_e(receipt.kind.value.title())}</th>',
                    f"<td>{_e(receipt.id)}</td>",
                    f'<td><code class="digest">{_e(receipt.sha256)}</code></td>',
                    f'<td class="numeric">{_e(receipt.byte_size)}</td>',
                    "</tr>",
                )
            )
        )
    return "\n".join(rows)


def _render_case(view: _CaseView, index: int) -> str:
    case = view.case
    mastery = view.mastery
    integrity_class, integrity_label, integrity_detail = _integrity_copy(view.integrity)
    focus, action = _focus_and_action(view)
    passed_count = len(mastery.passed_stages)
    total_stages = len(PracticeStage)
    ready_label = _MASTERY_LABEL[mastery.status]
    evidence_caption = (
        f"{len(case.evidence)} archived evidence "
        f"{_plural(len(case.evidence), 'receipt')} for {case.title}"
    )
    unknown_copy = (
        f"{len(case.unknowns)} explicit {_plural(len(case.unknowns), 'unknown')} recorded."
    )

    return f"""
<article class="case-card" aria-labelledby="case-title-{_e(index)}">
  <header class="case-header">
    <div>
      <p class="eyebrow">{_e(case.project)} · {_e(case.id)}</p>
      <h2 id="case-title-{_e(index)}">{_e(case.title)}</h2>
    </div>
    <span class="case-date">Captured {_e(case.created_at.date().isoformat())}</span>
  </header>

  <section class="focus-panel" aria-labelledby="focus-{_e(index)}">
    <div>
      <p class="section-label">Current focus</p>
      <h3 id="focus-{_e(index)}">{_e(focus)}</h3>
    </div>
    <div class="next-action">
      <strong>Exact next action</strong>
      <p>{_e(action)}</p>
    </div>
  </section>

  <div class="tracks">
    <section class="track" aria-labelledby="engineering-track-{_e(index)}">
      <p class="track-kicker">Track 1</p>
      <h3 id="engineering-track-{_e(index)}">Engineering evidence</h3>
      <p class="status-line">
        <span class="status status--resolved">{_e(case.engineering_state.value.title())}</span>
        <span>Operator-confirmed delivery state</span>
      </p>
      <p class="track-note">
        A resolved case is captured for practice. Resolution does not imply learning mastery.
      </p>
      <div class="integrity integrity--{_e(integrity_class)}">
        <strong>Evidence integrity: {_e(integrity_label)}</strong>
        <span>{_e(integrity_detail)}</span>
      </div>
    </section>

    <section class="track" aria-labelledby="learning-track-{_e(index)}">
      <p class="track-kicker">Track 2</p>
      <h3 id="learning-track-{_e(index)}">Learning mastery</h3>
      <p class="status-line">
        <span class="status status--learning">{_e(ready_label)}</span>
        <span>{_e(passed_count)}/{_e(total_stages)} self-assessed stages passed</span>
      </p>
      <p class="track-note">
        Derived from the latest append-only self-assessment for each practice stage.
        It is not an external certification.
      </p>
      <p class="unknowns">{_e(unknown_copy)}</p>
    </section>
  </div>

  <section class="practice" aria-labelledby="practice-{_e(index)}">
    <div class="section-heading">
      <div>
        <p class="section-label">Self-assessed practice</p>
        <h3 id="practice-{_e(index)}">Explain → Trace → Rebuild → Debug → Defend</h3>
      </div>
      <strong>{_e(passed_count)} of {_e(total_stages)} current passes</strong>
    </div>
    <ol class="stages">
      {_render_stages(mastery)}
    </ol>
  </section>

  <section class="concepts" aria-labelledby="concepts-{_e(index)}">
    <h3 id="concepts-{_e(index)}">Concepts to defend</h3>
    <ul>
      {_render_concepts(case)}
    </ul>
  </section>

  <section class="evidence" aria-labelledby="evidence-{_e(index)}">
    <div class="section-heading">
      <div>
        <p class="section-label">Metadata only</p>
        <h3 id="evidence-{_e(index)}">Evidence index</h3>
      </div>
      <span>Raw evidence and original source paths are intentionally omitted.</span>
    </div>
    <div class="table-wrap" role="region" aria-label="Archived evidence receipts" tabindex="0">
      <table>
        <caption>{_e(evidence_caption)}</caption>
        <thead>
          <tr>
            <th scope="col">Type</th>
            <th scope="col">Receipt</th>
            <th scope="col">SHA-256</th>
            <th scope="col">Bytes</th>
          </tr>
        </thead>
        <tbody>
          {_render_evidence(case)}
        </tbody>
      </table>
    </div>
  </section>
</article>
""".strip()


def _render_empty_state() -> str:
    return """
<section class="empty-state" aria-labelledby="empty-title">
  <p class="eyebrow">No active records</p>
  <h2 id="empty-title">Capture a learning case to begin</h2>
  <p>The Control Tower is derived from local case JSON and append-only practice attempts.</p>
</section>
""".strip()


def _render_document(views: list[_CaseView], knowledge: _KnowledgeView) -> str:
    summary = _summarize(views)
    cases = "\n".join(_render_case(view, index) for index, view in enumerate(views, start=1))
    if not cases:
        cases = _render_empty_state()
    source_snapshot_at = max(
        (
            *(_as_utc(view.latest_recorded_at) for view in views),
            *([_as_utc(knowledge.last_synced_at)] if knowledge.last_synced_at is not None else []),
        ),
        default=None,
    )
    if source_snapshot_at is None:
        snapshot_metadata = f"schema {_e(SCHEMA_VERSION)} · local derived artifact"
    else:
        snapshot_metadata = (
            "Source snapshot "
            f"{_e(source_snapshot_at.replace(microsecond=0).isoformat())} · "
            f"schema {_e(SCHEMA_VERSION)} · local derived artifact"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>SoloScale Control Tower</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #172033;
      --muted: #59657a;
      --line: #d8dfeb;
      --panel: #ffffff;
      --canvas: #f2f5fa;
      --navy: #13284a;
      --blue: #2457d6;
      --blue-soft: #e9efff;
      --green: #176b4d;
      --green-soft: #e6f5ee;
      --amber: #8a4b0f;
      --amber-soft: #fff0d7;
      --shadow: 0 18px 50px rgb(26 42 74 / 10%);
    }}

    * {{ box-sizing: border-box; }}

    html {{ scroll-behavior: smooth; }}

    body {{
      margin: 0;
      background: var(--canvas);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
      font-size: 1rem;
      line-height: 1.55;
    }}

    a:focus-visible,
    [tabindex]:focus-visible {{
      outline: 3px solid #ffbf47;
      outline-offset: 3px;
    }}

    .skip-link {{
      position: fixed;
      z-index: 20;
      top: 0.75rem;
      left: 0.75rem;
      padding: 0.7rem 1rem;
      border-radius: 0.4rem;
      background: #ffffff;
      color: var(--navy);
      font-weight: 800;
      transform: translateY(-180%);
    }}

    .skip-link:focus {{ transform: translateY(0); }}

    .shell {{ width: min(1180px, calc(100% - 2rem)); margin-inline: auto; }}

    .site-header {{
      overflow: hidden;
      padding: 4.5rem 0 5.75rem;
      background: var(--navy);
      color: #ffffff;
    }}

    .site-header .shell {{ position: relative; }}

    .site-header .shell::after {{
      position: absolute;
      right: -6rem;
      bottom: -12rem;
      width: 28rem;
      height: 28rem;
      border: 1px solid rgb(255 255 255 / 18%);
      border-radius: 50%;
      box-shadow: 0 0 0 5rem rgb(255 255 255 / 3%);
      content: "";
    }}

    .brand {{
      position: relative;
      z-index: 1;
      margin: 0 0 1.1rem;
      color: #b9cbff;
      font-size: 0.8rem;
      font-weight: 800;
      letter-spacing: 0.16em;
      text-transform: uppercase;
    }}

    .site-header h1 {{
      position: relative;
      z-index: 1;
      max-width: 780px;
      margin: 0;
      font-size: clamp(2.35rem, 7vw, 5rem);
      letter-spacing: -0.055em;
      line-height: 0.98;
    }}

    .lede {{
      position: relative;
      z-index: 1;
      max-width: 720px;
      margin: 1.5rem 0 0;
      color: #dce5fa;
      font-size: clamp(1rem, 2.4vw, 1.25rem);
    }}

    main {{ padding-bottom: 5rem; }}

    .summary {{ position: relative; z-index: 2; margin-top: -2.5rem; }}

    .summary h2 {{
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
      white-space: nowrap;
    }}

    .metrics {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      margin: 0;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 1rem;
      background: var(--panel);
      box-shadow: var(--shadow);
    }}

    .metric {{ padding: 1.35rem 1.4rem; }}
    .metric + .metric {{ border-left: 1px solid var(--line); }}
    .metric dt {{ color: var(--muted); font-size: 0.8rem; font-weight: 750; }}
    .metric dd {{ margin: 0; }}
    .metric strong {{ display: block; font-size: 2rem; line-height: 1.2; }}
    .metric span {{ display: block; margin-top: 0.3rem; color: var(--muted); font-size: 0.82rem; }}

    .case-list {{ display: grid; gap: 1.5rem; margin-top: 2rem; }}

    .case-card {{
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 1rem;
      background: var(--panel);
      box-shadow: var(--shadow);
    }}

    .case-header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 1rem;
      padding: 1.75rem;
      border-bottom: 1px solid var(--line);
    }}

    .eyebrow,
    .section-label,
    .track-kicker {{
      margin: 0 0 0.35rem;
      color: var(--blue);
      font-size: 0.72rem;
      font-weight: 850;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }}

    .case-header h2 {{ margin: 0; font-size: clamp(1.45rem, 3vw, 2rem); line-height: 1.15; }}
    .case-date {{ flex: 0 0 auto; color: var(--muted); font-size: 0.83rem; }}

    .tracks {{ display: grid; grid-template-columns: 1fr 1fr; }}
    .track {{ padding: 1.6rem 1.75rem; }}
    .track + .track {{ border-left: 1px solid var(--line); }}
    .track h3 {{ margin: 0; font-size: 1.1rem; }}
    .track-note {{ margin-bottom: 0; color: var(--muted); font-size: 0.9rem; }}

    .status-line {{ display: flex; flex-wrap: wrap; align-items: center; gap: 0.55rem; }}
    .status-line > span:last-child {{ color: var(--muted); font-size: 0.82rem; }}

    .status {{
      display: inline-flex;
      align-items: center;
      min-height: 1.75rem;
      padding: 0.25rem 0.65rem;
      border: 1px solid currentcolor;
      border-radius: 999px;
      font-size: 0.74rem;
      font-weight: 850;
    }}

    .status--resolved {{ background: var(--green-soft); color: var(--green); }}
    .status--learning {{ background: var(--blue-soft); color: #173f9d; }}

    .integrity {{
      display: grid;
      gap: 0.15rem;
      margin-top: 1rem;
      padding: 0.8rem 0.9rem;
      border-left: 4px solid currentcolor;
      border-radius: 0.25rem;
      font-size: 0.84rem;
    }}

    .integrity span {{ color: var(--ink); }}
    .integrity--verified {{ background: var(--green-soft); color: var(--green); }}
    .integrity--attention {{ background: var(--amber-soft); color: var(--amber); }}
    .unknowns {{ margin-bottom: 0; color: var(--muted); font-size: 0.82rem; font-weight: 700; }}

    .focus-panel {{
      display: grid;
      grid-template-columns: minmax(0, 0.8fr) minmax(0, 1.2fr);
      gap: 1.5rem;
      padding: 1.5rem 1.75rem;
      border-block: 1px solid var(--line);
      background: #f8faff;
    }}

    .focus-panel h3 {{ margin: 0; font-size: 1.25rem; }}
    .next-action {{ padding-left: 1.5rem; border-left: 3px solid var(--blue); }}
    .next-action strong {{ color: var(--blue); font-size: 0.78rem; text-transform: uppercase; }}
    .next-action p {{ margin: 0.25rem 0 0; font-weight: 700; }}

    .practice,
    .concepts,
    .evidence {{ padding: 1.6rem 1.75rem; }}

    .practice,
    .concepts {{ border-bottom: 1px solid var(--line); }}

    .section-heading {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 1rem;
      margin-bottom: 1rem;
    }}

    .section-heading h3,
    .concepts h3 {{ margin: 0; font-size: 1.05rem; }}
    .section-heading > strong,
    .section-heading > span {{ color: var(--muted); font-size: 0.82rem; text-align: right; }}

    .stages {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 0.6rem;
      margin: 0;
      padding: 0;
      list-style: none;
    }}

    .stage {{
      display: flex;
      min-width: 0;
      gap: 0.6rem;
      padding: 0.85rem;
      border: 1px solid var(--line);
      border-radius: 0.65rem;
      background: #ffffff;
    }}

    .stage-marker {{
      display: grid;
      flex: 0 0 1.5rem;
      width: 1.5rem;
      height: 1.5rem;
      place-items: center;
      border: 1px solid currentcolor;
      border-radius: 50%;
      font-weight: 900;
    }}

    .stage-copy {{ min-width: 0; }}
    .stage-name,
    .stage-state {{ display: block; }}
    .stage-name {{ font-size: 0.83rem; }}
    .stage-state {{ color: var(--muted); font-size: 0.7rem; }}
    .stage--passed {{ background: var(--green-soft); color: var(--green); }}
    .stage--needs-work {{ background: var(--amber-soft); color: var(--amber); }}
    .stage--next {{ border-color: var(--blue); background: var(--blue-soft); color: var(--blue); }}
    .stage--waiting {{ color: var(--muted); }}

    .concepts ul {{ display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0.8rem 0 0; padding: 0; }}
    .concepts li {{
      padding: 0.35rem 0.65rem;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #f8faff;
      font-size: 0.78rem;
      font-weight: 700;
      list-style: none;
    }}

    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
    caption {{ padding-bottom: 0.65rem; color: var(--muted); text-align: left; }}
    th,
    td {{
      padding: 0.7rem;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    thead th {{
      color: var(--muted);
      font-size: 0.7rem;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}
    tbody tr:last-child th,
    tbody tr:last-child td {{ border-bottom: 0; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 0.74rem; }}
    .digest {{ overflow-wrap: anywhere; }}
    .numeric {{ font-variant-numeric: tabular-nums; text-align: right; }}

    .empty-state {{
      padding: 4rem 2rem;
      border: 1px dashed #aab5c7;
      border-radius: 1rem;
      background: var(--panel);
      text-align: center;
    }}

    .knowledge-plane {{
      margin-top: 2rem;
      padding: 1.5rem;
      border: 1px solid var(--line);
      border-radius: 1rem;
      background: var(--panel);
      box-shadow: var(--shadow);
    }}
    .knowledge-heading {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
    }}
    .knowledge-heading h2 {{ margin: 0; }}
    .knowledge-state {{
      padding: 0.35rem 0.65rem;
      border-radius: 999px;
      background: var(--blue-soft);
      color: var(--blue);
      font-weight: 800;
    }}
    .knowledge-flow {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.55rem;
      align-items: center;
      margin-top: 1.25rem;
      color: var(--muted);
    }}
    .knowledge-flow span {{
      padding: 0.4rem 0.65rem;
      border: 1px solid var(--line);
      border-radius: 0.5rem;
    }}
    .knowledge-metrics {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 1px;
      margin: 1.25rem 0 0;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 0.75rem;
      background: var(--line);
    }}
    .knowledge-metrics div {{ padding: 0.8rem; background: var(--panel); }}
    .knowledge-metrics dt {{ color: var(--muted); font-size: 0.75rem; font-weight: 750; }}
    .knowledge-metrics dd {{ margin: 0.1rem 0 0; font-size: 1.35rem; font-weight: 800; }}
    .knowledge-meta {{ color: var(--muted); font-size: 0.82rem; }}
    .knowledge-next {{
      padding: 1rem;
      border-left: 3px solid var(--blue);
      background: var(--blue-soft);
    }}
    .knowledge-next p {{ margin: 0.25rem 0 0; }}

    .empty-state h2 {{ margin: 0; }}
    .empty-state p:last-child {{ color: var(--muted); }}

    .site-footer {{ padding: 1.5rem 0 3rem; color: var(--muted); font-size: 0.82rem; }}
    .site-footer .shell {{ display: grid; gap: 0.25rem; }}
    .site-footer p {{ margin: 0; }}

    @media (max-width: 860px) {{
      .metrics {{ grid-template-columns: 1fr 1fr; }}
      .metric + .metric {{ border-left: 0; }}
      .metric:nth-child(even) {{ border-left: 1px solid var(--line); }}
      .metric:nth-child(n + 3) {{ border-top: 1px solid var(--line); }}
      .stages {{ grid-template-columns: 1fr 1fr; }}
      .knowledge-metrics {{ grid-template-columns: 1fr 1fr; }}
    }}

    @media (max-width: 640px) {{
      .site-header {{ padding-block: 3.2rem 4.75rem; }}
      .tracks,
      .focus-panel {{ grid-template-columns: 1fr; }}
      .track + .track {{ border-top: 1px solid var(--line); border-left: 0; }}
      .next-action {{
        padding-top: 1rem;
        padding-left: 0;
        border-top: 3px solid var(--blue);
        border-left: 0;
      }}
      .case-header,
      .section-heading {{ align-items: flex-start; flex-direction: column; }}
      .section-heading > strong,
      .section-heading > span {{ text-align: left; }}
      .stages {{ grid-template-columns: 1fr; }}
    }}

    @media print {{
      @page {{ margin: 14mm; }}
      body {{ background: #ffffff; color: #000000; font-size: 10pt; }}
      .skip-link {{ display: none; }}
      .site-header {{ padding: 0 0 1.5rem; background: #ffffff; color: #000000; }}
      .site-header h1 {{ font-size: 28pt; }}
      .brand,
      .lede {{ color: #000000; }}
      .summary {{ margin-top: 0; }}
      .metrics,
      .case-card {{ box-shadow: none; }}
      .case-card {{ break-inside: avoid; }}
      .focus-panel {{ background: #ffffff; }}
      .table-wrap {{ overflow: visible; }}
      code {{ overflow-wrap: anywhere; }}
    }}
  </style>
</head>
<body>
  <a class="skip-link" href="#main-content">Skip to main content</a>
  <header class="site-header">
    <div class="shell">
      <p class="brand">SoloScale · Local learning plane</p>
      <h1>Control Tower</h1>
      <p class="lede">
        Engineering evidence and learning mastery stay separate, inspectable, and actionable.
      </p>
    </div>
  </header>

  <main id="main-content" tabindex="-1">
    <section class="summary shell" aria-labelledby="summary-title">
      <h2 id="summary-title">Control Tower summary</h2>
      <dl class="metrics">
        {_render_summary(summary)}
      </dl>
    </section>

    {_render_knowledge(knowledge)}

    <div class="case-list shell">
      {cases}
    </div>
  </main>

  <footer class="site-footer">
    <div class="shell">
      <p>
        {snapshot_metadata}
      </p>
      <p>
        Source of truth: local case JSON and append-only practice JSONL, plus private knowledge
        index metadata. Raw conversation/evidence bodies and original source paths are not
        embedded in this page.
      </p>
      <p>
        All learning outcomes shown here are self-assessed, not externally verified mastery.
      </p>
    </div>
  </footer>
</body>
</html>
"""


def _resolve_output_target(
    store: CasebookStore,
    output_path: Path | None,
) -> tuple[Path, Path, Path]:
    store_root = store.root.resolve()
    requested_dashboard_root = store.root / "control-tower"
    dashboard_root = store_root / "control-tower"

    if requested_dashboard_root.resolve(strict=False) != dashboard_root:
        raise ValueError("Control Tower directory must not be a symlink")

    requested_target = (
        Path(output_path) if output_path is not None else requested_dashboard_root / "index.html"
    )
    resolved_target = requested_target.resolve(strict=False)
    if resolved_target == dashboard_root or not resolved_target.is_relative_to(dashboard_root):
        raise ValueError(f"Control Tower output must stay within {requested_dashboard_root}")

    cases_root = store.cases_root.resolve(strict=False)
    if resolved_target.is_relative_to(cases_root):
        raise ValueError("Control Tower output must not target Casebook source records")

    return requested_target, dashboard_root, resolved_target


def _prepare_private_output_parent(dashboard_root: Path, target_parent: Path) -> None:
    dashboard_root.mkdir(
        mode=_DASHBOARD_DIRECTORY_MODE,
        parents=True,
        exist_ok=True,
    )
    if not dashboard_root.is_dir() or dashboard_root.resolve() != dashboard_root:
        raise ValueError("Control Tower directory must be a real directory")
    dashboard_root.chmod(_DASHBOARD_DIRECTORY_MODE)

    current = dashboard_root
    for part in target_parent.relative_to(dashboard_root).parts:
        current /= part
        if current.exists():
            if not current.is_dir():
                raise NotADirectoryError(f"Control Tower parent is not a directory: {current}")
            continue
        current.mkdir(mode=_DASHBOARD_DIRECTORY_MODE)
        current.chmod(_DASHBOARD_DIRECTORY_MODE)


def _atomic_write_private_html(target: Path, document: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    descriptor_open = True
    try:
        os.fchmod(descriptor, _DASHBOARD_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor_open = False
            handle.write(document.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if descriptor_open:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def build_control_tower(store: CasebookStore, output_path: Path | None = None) -> Path:
    """Build the local, zero-JavaScript Control Tower as a derived HTML artifact."""

    requested_target, dashboard_root, target = _resolve_output_target(store, output_path)
    views = []
    for case in store.list_cases():
        attempts = store.read_attempts(case.id)
        recorded_times = [_as_utc(case.created_at)]
        recorded_times.extend(_as_utc(attempt.created_at) for attempt in attempts)
        latest_recorded_at = max(recorded_times)
        views.append(
            _CaseView(
                case=case,
                mastery=derive_mastery(case, attempts),
                integrity=store.verify_integrity(case.id),
                latest_recorded_at=latest_recorded_at,
            )
        )

    knowledge = _knowledge_view(store.root)
    _prepare_private_output_parent(dashboard_root, target.parent)
    _atomic_write_private_html(target, _render_document(views, knowledge))
    return requested_target
