from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from soloscale.application_record import (
    ApplicationStatus,
    applications_root,
    link_learning_case,
    list_application_records,
    update_application_status,
)
from soloscale.buildlog_adapter import export_buildlog_iteration
from soloscale.casebook_models import (
    AttemptOutcome,
    EvidenceKind,
    LearningCase,
    PracticeStage,
)
from soloscale.casebook_store import CasebookStore
from soloscale.control_tower import build_control_tower
from soloscale.conversation_intake import (
    discover_buildlog_runs,
    discover_codex_sources,
    parse_buildlog_run,
    parse_chatgpt_export,
    parse_codex_session,
)
from soloscale.evidence_agent import (
    BoundedEvidenceAgent,
    EvidenceAgentError,
    OllamaReasoner,
)
from soloscale.evidence_hub import EvidenceHub, EvidenceHubError
from soloscale.evidence_ui import refresh_evidence_catalog
from soloscale.handoff import packet_from_task, render_packet_markdown
from soloscale.knowledge_models import ParsedSource, SourceFailure, SourceKind, SyncReport
from soloscale.knowledge_store import KnowledgeStore, KnowledgeStoreError
from soloscale.learning_practice import (
    ExerciseType,
    PracticeLanguage,
    create_practice_workspace,
    generate_practice_exercise,
    ingest_practice_completion,
    load_exercise,
    open_practice_workspace,
)
from soloscale.models import (
    LatencyTolerance,
    ReasoningDepth,
    RiskLevel,
    RunSummary,
    TaskEnvelope,
)
from soloscale.router import route_task
from soloscale.skill_os import (
    SkillOSError,
    default_registry_path,
    load_skill_registry,
    persist_route_receipt,
    render_skill_route,
    route_skill_request,
)

app = typer.Typer(no_args_is_help=True)
console = Console()
_DEFAULT_CODEX_HOME = Path.home() / ".codex"
_DEFAULT_SKILL_REGISTRY = default_registry_path()


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _parse_evidence_sources(specs: list[str]) -> list[tuple[EvidenceKind, Path]]:
    sources: list[tuple[EvidenceKind, Path]] = []
    for spec in specs:
        if "=" not in spec:
            raise typer.BadParameter(
                f"invalid evidence '{spec}'; expected KIND=PATH",
                param_hint="--evidence",
            )
        raw_kind, raw_path = spec.split("=", 1)
        if not raw_kind.strip() or not raw_path.strip():
            raise typer.BadParameter(
                f"invalid evidence '{spec}'; expected non-empty KIND=PATH",
                param_hint="--evidence",
            )
        try:
            kind = EvidenceKind(raw_kind.strip().lower())
        except ValueError as exc:
            allowed = ", ".join(item.value for item in EvidenceKind)
            raise typer.BadParameter(
                f"unsupported evidence kind '{raw_kind}'; choose one of: {allowed}",
                param_hint="--evidence",
            ) from exc
        sources.append((kind, Path(raw_path)))
    if not sources:
        raise typer.BadParameter("at least one evidence file is required", param_hint="--evidence")
    return sources


def _resolve_case_target(target: str, data_root: Path) -> tuple[str, Path]:
    candidate = Path(target)
    try:
        is_file = candidate.is_file()
    except OSError as exc:
        raise typer.BadParameter("case target could not be inspected", param_hint="case") from exc

    if not is_file:
        return target, data_root

    if candidate.name != "case.json" or candidate.parent.parent.name != "cases":
        raise typer.BadParameter(
            "case file must be named case.json under <data-root>/cases/<case-id>/",
            param_hint="case",
        )

    directory_case_id = candidate.parent.name
    try:
        learning_case = LearningCase.model_validate_json(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(
            "case.json is not a valid Casebook manifest",
            param_hint="case",
        ) from exc

    if learning_case.id != directory_case_id:
        raise typer.BadParameter(
            "case.json id does not match its case directory",
            param_hint="case",
        )

    inferred_root = candidate.parent.parent.parent
    return learning_case.id, inferred_root


def _git_worktree_root(path: Path) -> Path | None:
    """Find the nearest Git worktree containing a prospective data root."""

    candidate = path.resolve(strict=False)
    if not candidate.is_dir():
        candidate = candidate.parent
    for ancestor in (candidate, *candidate.parents):
        if (ancestor / ".git").exists():
            return ancestor
    return None


def _validate_private_data_root(data_root: Path) -> None:
    """Keep in-repository private artifacts under the ignored .soloscale tree."""

    resolved_root = data_root.resolve(strict=False)
    worktree = _git_worktree_root(resolved_root)
    if worktree is None:
        return
    relative_parts = resolved_root.relative_to(worktree).parts
    if ".soloscale" not in relative_parts:
        raise typer.BadParameter(
            "a data root inside a Git worktree must stay under a .soloscale directory",
            param_hint="--data-root",
        )
    if not _git_ignores_path(worktree, resolved_root):
        raise typer.BadParameter(
            "the selected .soloscale data root is not ignored by Git; add an ignore "
            "rule before storing private evidence",
            param_hint="--data-root",
        )


def _git_ignores_path(worktree: Path, data_root: Path) -> bool:
    git_executable = shutil.which("git")
    if git_executable is None:
        return False
    probe = data_root / ".casebook-private-probe"
    try:
        result = subprocess.run(
            [
                git_executable,
                "-C",
                str(worktree),
                "check-ignore",
                "--quiet",
                "--no-index",
                "--",
                str(probe),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _combine_sync_reports(reports: Iterable[SyncReport]) -> SyncReport:
    totals = {
        "discovered": 0,
        "imported": 0,
        "updated": 0,
        "skipped": 0,
        "documents": 0,
        "chunks_written": 0,
    }
    failures: list[SourceFailure] = []
    for report in reports:
        for field in totals:
            totals[field] += int(getattr(report, field))
        failures.extend(report.failures)
    return SyncReport(
        discovered=totals["discovered"],
        imported=totals["imported"],
        updated=totals["updated"],
        skipped=totals["skipped"],
        failed=len(failures),
        documents=totals["documents"],
        chunks_written=totals["chunks_written"],
        failures=failures,
    )


def _sanitized_sync_failure(
    locator: Path,
    source_kind: SourceKind,
    error: Exception,
) -> SyncReport:
    return SyncReport(
        discovered=1,
        failed=1,
        failures=[
            SourceFailure(
                source_locator=str(locator),
                source_kind=source_kind,
                code=type(error).__name__,
            )
        ],
    )


def _sync_parsed_sources(
    store: KnowledgeStore,
    parsed_sources: Iterable[ParsedSource],
) -> list[SyncReport]:
    return [store.sync([parsed_source]) for parsed_source in parsed_sources]


def _auto_buildlog_roots(start: Path) -> list[Path]:
    """Find an enclosing BuildLog checkout without searching unrelated directories."""

    resolved = start.resolve(strict=False)
    for candidate in (resolved, *resolved.parents):
        if (candidate / "src" / "buildlog").is_dir() and (candidate / "runs").is_dir():
            return [candidate]
    return []


@app.command("task-create")
def task_create(
    title: Annotated[str, typer.Option(help="Task title")],
    goal: Annotated[str, typer.Option(help="Desired outcome")],
    repo: Annotated[str | None, typer.Option(help="Local or GitHub repository reference")] = None,
    branch: Annotated[str | None, typer.Option(help="Approved working branch")] = None,
    requested_path: Annotated[
        list[str] | None,
        typer.Option("--requested-path", help="In-scope path; repeat for multiple paths"),
    ] = None,
    constraint: Annotated[
        list[str] | None,
        typer.Option("--constraint", help="Task constraint; repeat for multiple constraints"),
    ] = None,
    frozen_decision: Annotated[
        list[str] | None,
        typer.Option(
            "--frozen-decision",
            help="Approved decision; repeat for multiple decisions",
        ),
    ] = None,
    required_change: Annotated[
        list[str] | None,
        typer.Option(
            "--required-change",
            help="Required implementation change; repeat for multiple changes",
        ),
    ] = None,
    acceptance_criterion: Annotated[
        list[str] | None,
        typer.Option(
            "--acceptance-criterion",
            help="Acceptance criterion; repeat for multiple criteria",
        ),
    ] = None,
    test_to_run: Annotated[
        list[str] | None,
        typer.Option("--test-to-run", help="Verification command; repeat for multiple tests"),
    ] = None,
    non_goal: Annotated[
        list[str] | None,
        typer.Option("--non-goal", help="Explicit non-goal; repeat for multiple non-goals"),
    ] = None,
    reasoning_depth: Annotated[ReasoningDepth, typer.Option()] = ReasoningDepth.MEDIUM,
    latency_tolerance: Annotated[LatencyTolerance, typer.Option()] = LatencyTolerance.BATCH,
    risk: Annotated[RiskLevel, typer.Option()] = RiskLevel.MEDIUM,
    requires_local: Annotated[bool, typer.Option(help="Requires local files or terminal")] = False,
    requires_realtime: Annotated[bool, typer.Option()] = False,
    requires_scheduled: Annotated[bool, typer.Option()] = False,
    plugin: Annotated[
        str | None, typer.Option(help="Connected plugin that can complete the action")
    ] = None,
    public_action: Annotated[bool, typer.Option()] = False,
) -> None:
    task = TaskEnvelope(
        title=title,
        goal=goal,
        repository=repo,
        branch=branch,
        requested_paths=requested_path or [],
        constraints=constraint or [],
        frozen_decisions=frozen_decision or [],
        required_changes=required_change or [],
        acceptance_criteria=acceptance_criterion or [],
        tests_to_run=test_to_run or [],
        non_goals=non_goal or [],
        reasoning_depth=reasoning_depth,
        latency_tolerance=latency_tolerance,
        risk=risk,
        requires_local_files=requires_local,
        requires_terminal=requires_local,
        requires_realtime=requires_realtime,
        requires_scheduled_execution=requires_scheduled,
        plugin_can_complete=plugin is not None,
        plugin_name=plugin,
        public_action=public_action,
    )
    path = Path(".soloscale") / "tasks" / task.id / "task.json"
    _write_json(path, task.model_dump(mode="json"))
    console.print(f"[green]Created[/green] {path}")
    decision = route_task(task)
    console.print(Panel(decision.model_dump_json(indent=2), title="Route decision"))


@app.command("skill-list")
def skill_list(
    registry_path: Annotated[
        Path,
        typer.Option("--registry", help="Tracked repo-scoped Skill registry"),
    ] = _DEFAULT_SKILL_REGISTRY,
) -> None:
    """List registered Skill versions and trust status without reading private runs."""

    try:
        registry = load_skill_registry(registry_path)
    except SkillOSError as exc:
        raise typer.BadParameter(str(exc), param_hint="--registry") from exc
    table = Table(title="SoloScale Skill Registry")
    table.add_column("Skill")
    table.add_column("Version")
    table.add_column("Status")
    table.add_column("Risk")
    for skill in registry.skills:
        table.add_row(skill.name, skill.current_version, skill.status.value, skill.risk_class.value)
    console.print(table)


@app.command("skill-route")
def skill_route(
    request_text: Annotated[str, typer.Argument(help="High-level operator request")],
    data_root: Annotated[
        Path,
        typer.Option("--data-root", help="Private ignored SoloScale data root"),
    ] = Path(".soloscale"),
    registry_path: Annotated[
        Path,
        typer.Option("--registry", help="Tracked repo-scoped Skill registry"),
    ] = _DEFAULT_SKILL_REGISTRY,
) -> None:
    """Normalize one high-level request, compose Skills, and save a private route receipt."""

    _validate_private_data_root(data_root)
    try:
        registry = load_skill_registry(registry_path)
        route = route_skill_request(request_text, registry=registry)
        receipt, receipt_path = persist_route_receipt(route, data_root=data_root)
    except SkillOSError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(render_skill_route(route))
    console.print(
        f"[green]Private receipt[/green] {receipt.receipt_id} — {receipt.final_status.value}"
    )
    console.print(receipt_path)


@app.command("task-route")
def task_route(task_file: Annotated[Path, typer.Argument(help="Task Envelope JSON")]) -> None:
    task = TaskEnvelope.model_validate_json(task_file.read_text(encoding="utf-8"))
    decision = route_task(task)
    table = Table(title=f"Route — {task.id}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Primary", decision.primary.value)
    table.add_row("Secondary", ", ".join(item.value for item in decision.secondary) or "None")
    table.add_row("Human gate", str(decision.human_gate_required))
    table.add_row("Rationale", "\n".join(decision.rationale))
    console.print(table)


@app.command("packet-create")
def packet_create(task_file: Annotated[Path, typer.Argument(help="Task Envelope JSON")]) -> None:
    task = TaskEnvelope.model_validate_json(task_file.read_text(encoding="utf-8"))
    packet = packet_from_task(task)
    out = task_file.parent / "execution-packet.md"
    out.write_text(render_packet_markdown(packet), encoding="utf-8")
    console.print(f"[green]Created[/green] {out}")


@app.command("buildlog-export")
def buildlog_export(
    summary_file: Annotated[Path, typer.Argument(help="Run Summary JSON")],
) -> None:
    summary = RunSummary.model_validate_json(summary_file.read_text(encoding="utf-8"))
    payload = export_buildlog_iteration(summary)
    out = summary_file.parent / "buildlog-iteration.json"
    _write_json(out, payload)
    console.print(f"[green]Created[/green] {out}")


@app.command("case-create")
def case_create(
    title: Annotated[str, typer.Option(help="Engineering case title")],
    project: Annotated[str, typer.Option(help="Project or product name")],
    problem: Annotated[str, typer.Option(help="Observed engineering problem")],
    expected: Annotated[str, typer.Option(help="Expected behavior")],
    actual: Annotated[str, typer.Option(help="Actual behavior")],
    root_cause: Annotated[
        str,
        typer.Option("--root-cause", help="Confirmed cause or an explicit UNKNOWN statement"),
    ],
    resolution: Annotated[str, typer.Option(help="Resolution or bounded recovery decision")],
    evidence: Annotated[
        list[str] | None,
        typer.Option(
            "--evidence",
            help="Selected local evidence as KIND=PATH; repeat for multiple files",
        ),
    ] = None,
    verification: Annotated[
        list[str] | None,
        typer.Option(
            "--verification",
            help="Observed verification statement; repeat for multiple statements",
        ),
    ] = None,
    concept: Annotated[
        list[str] | None,
        typer.Option("--concept", help="Concept to master; repeat for multiple concepts"),
    ] = None,
    case_id: Annotated[
        str | None,
        typer.Option("--case-id", help="Stable lowercase-and-hyphen case identifier"),
    ] = None,
    repository: Annotated[
        str | None,
        typer.Option(help="Optional repository reference"),
    ] = None,
    alternative: Annotated[
        list[str] | None,
        typer.Option("--alternative", help="Alternative considered; repeat as needed"),
    ] = None,
    trade_off: Annotated[
        list[str] | None,
        typer.Option("--trade-off", help="Trade-off; repeat as needed"),
    ] = None,
    unknown: Annotated[
        list[str] | None,
        typer.Option("--unknown", help="Explicit unknown; repeat as needed"),
    ] = None,
    data_root: Annotated[
        Path,
        typer.Option("--data-root", help="Private SoloScale data root"),
    ] = Path(".soloscale"),
) -> None:
    if not verification:
        raise typer.BadParameter(
            "at least one verification statement is required",
            param_hint="--verification",
        )
    if not concept:
        raise typer.BadParameter(
            "at least one learning concept is required",
            param_hint="--concept",
        )
    sources = _parse_evidence_sources(evidence or [])
    _validate_private_data_root(data_root)
    try:
        store = CasebookStore(data_root)
        case = store.create_case(
            case_id=case_id,
            title=title,
            project=project,
            problem=problem,
            expected_behavior=expected,
            actual_behavior=actual,
            root_cause=root_cause,
            resolution=resolution,
            verification=verification,
            concepts=concept,
            evidence_sources=sources,
            repository=repository,
            alternatives_considered=alternative or [],
            trade_offs=trade_off or [],
            unknowns=unknown or [],
        )
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    case_dir = data_root / "cases" / case.id
    console.print(f"[green]Created case[/green] {case.id}")
    console.print(f"Case record: {case_dir / 'case.json'}")
    console.print(f"Interview packet: {case_dir / 'interview-packet.md'}")
    console.print("Current status: CAPTURED | Readiness: 0/5 | Next action: EXPLAIN")
    try:
        dashboard = build_control_tower(store)
    except Exception:
        console.print(
            "[yellow]Warning:[/yellow] The case is committed, but the Control Tower "
            "refresh failed; do not retry case-create."
        )
    else:
        console.print(f"Control Tower: {dashboard}")


@app.command("case-attempt")
def case_attempt(
    case: Annotated[str, typer.Argument(help="Case ID or path to case.json")],
    stage: Annotated[PracticeStage, typer.Option(help="Practice stage")],
    outcome: Annotated[AttemptOutcome, typer.Option(help="Self-assessed outcome")],
    receipt: Annotated[
        Path | None,
        typer.Option(help="Non-empty practice artifact; required for pass"),
    ] = None,
    note: Annotated[str | None, typer.Option(help="Attempt note")] = None,
    data_root: Annotated[
        Path,
        typer.Option("--data-root", help="Private SoloScale data root"),
    ] = Path(".soloscale"),
) -> None:
    case_id, resolved_root = _resolve_case_target(case, data_root)
    _validate_private_data_root(resolved_root)
    try:
        store = CasebookStore(resolved_root)
        result = store.record_attempt(
            case_id,
            stage,
            outcome,
            receipt_path=receipt,
            note=note,
        )
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    snapshot = result.mastery
    next_action = snapshot.next_stage.value.upper() if snapshot.next_stage else "COMPLETE"
    status = snapshot.status.value.replace("-", "_").upper()
    console.print(
        f"[green]Recorded[/green] {stage.value.upper()} → {outcome.value.upper()} "
        "[green]— attempt committed[/green]"
    )
    console.print(
        f"Current status: {status} | Readiness: {len(snapshot.passed_stages)}/5 | "
        f"Next action: {next_action}"
    )
    if result.commit_warning is not None:
        console.print(
            "[yellow]Warning:[/yellow] The attempt reached durable commit, but closing "
            "the attempt log reported an error; do not retry case-attempt. Inspect "
            "case-status and evidence integrity."
        )
    if not result.packet_refreshed:
        console.print(
            "[yellow]Warning:[/yellow] The attempt is committed, but the interview "
            "packet refresh failed; do not retry case-attempt."
        )
    try:
        dashboard = build_control_tower(store)
    except Exception:
        console.print(
            "[yellow]Warning:[/yellow] The attempt is committed, but the Control Tower "
            "refresh failed; do not retry case-attempt."
        )
    else:
        console.print(f"Control Tower: {dashboard}")


@app.command("case-status")
def case_status(
    case: Annotated[str, typer.Argument(help="Case ID or path to case.json")],
    data_root: Annotated[
        Path,
        typer.Option("--data-root", help="Private SoloScale data root"),
    ] = Path(".soloscale"),
) -> None:
    case_id, resolved_root = _resolve_case_target(case, data_root)
    _validate_private_data_root(resolved_root)
    try:
        store = CasebookStore(resolved_root)
        learning_case = store.load_case(case_id)
        snapshot = store.mastery(case_id)
        integrity = store.verify_integrity(case_id)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    status = snapshot.status.value.replace("-", "_").upper()
    next_action = snapshot.next_stage.value.upper() if snapshot.next_stage else "COMPLETE"
    summary = Table(title=f"Case — {learning_case.title}")
    summary.add_column("Track")
    summary.add_column("Current state")
    summary.add_row("Engineering", learning_case.engineering_state.value.upper())
    summary.add_row("Evidence integrity", "PASS" if integrity.ok else "FAIL")
    summary.add_row("Learning", status)
    summary.add_row("Readiness", f"{len(snapshot.passed_stages)}/5")
    summary.add_row("Next action", next_action)
    console.print(summary)

    stages = Table(title="Practice gates")
    stages.add_column("Stage")
    stages.add_column("Latest result")
    for practice_stage in PracticeStage:
        result = snapshot.stage_results[practice_stage]
        label = "NOT STARTED" if result is None else result.value.replace("-", " ").upper()
        stages.add_row(practice_stage.value.upper(), label)
    console.print(stages)


@app.command("learning-exercise-create")
def learning_exercise_create(
    case: Annotated[str, typer.Argument(help="Case ID or path to case.json")],
    requirement: Annotated[str, typer.Option("--requirement", help="JD requirement to practice")],
    exercise_type: Annotated[
        ExerciseType,
        typer.Option("--type", help="Exercise type"),
    ] = ExerciseType.IMPLEMENT,
    difficulty: Annotated[
        int,
        typer.Option("--difficulty", min=1, max=5, help="Difficulty 1-5"),
    ] = 2,
    language: Annotated[
        PracticeLanguage,
        typer.Option("--language", help="Practice language"),
    ] = PracticeLanguage.PYTHON,
    claim: Annotated[
        str | None,
        typer.Option(help="Optional Resume claim this exercise trains"),
    ] = None,
    data_root: Annotated[
        Path,
        typer.Option("--data-root", help="Private SoloScale data root"),
    ] = Path(".soloscale"),
) -> None:
    case_id, resolved_root = _resolve_case_target(case, data_root)
    _validate_private_data_root(resolved_root)
    try:
        store = CasebookStore(resolved_root)
        learning_case = store.load_case(case_id)
        exercise = generate_practice_exercise(
            case=learning_case,
            jd_requirement=requirement,
            resume_claim=claim,
            exercise_type=exercise_type,
            difficulty=difficulty,
            practice_language=language,
        )
        workspace = create_practice_workspace(exercise, resolved_root)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"[green]Created exercise[/green] {exercise.id}")
    console.print(
        f"Type: {exercise.exercise_type.value} | Language: {exercise.practice_language.value}"
    )
    console.print(f"Mastery capability: {exercise.mastery_capability.value}")
    console.print(f"Practice workspace: {workspace}")
    console.print("Status: USER_PRACTICE_REQUIRED — open it in VS Code and complete the task.")


@app.command("learning-exercise-open")
def learning_exercise_open(
    exercise: Annotated[str, typer.Argument(help="Exercise ID")],
    data_root: Annotated[
        Path,
        typer.Option("--data-root", help="Private SoloScale data root"),
    ] = Path(".soloscale"),
) -> None:
    _validate_private_data_root(data_root)
    try:
        stored = load_exercise(exercise, data_root)
        if not stored.workspace_path:
            raise ValueError("exercise has no practice workspace yet")
        workspace = Path(stored.workspace_path)
        opened = open_practice_workspace(workspace)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if opened:
        console.print(f"[green]Opened[/green] {workspace} in VS Code.")
    else:
        console.print("[yellow]code CLI not found[/yellow]; open the folder manually:")
        console.print(workspace)


@app.command("learning-exercise-complete")
def learning_exercise_complete(
    exercise: Annotated[str, typer.Argument(help="Exercise ID")],
    evidence: Annotated[
        Path | None,
        typer.Option(help="Practice artifact (required for a pass)"),
    ] = None,
    tests_passed: Annotated[
        int,
        typer.Option("--tests-passed", min=0, help="Tests that passed"),
    ] = 0,
    tests_failed: Annotated[
        int,
        typer.Option("--tests-failed", min=0, help="Tests that failed"),
    ] = 0,
    attempts: Annotated[
        int,
        typer.Option("--attempts", min=1, help="Practice attempts used"),
    ] = 1,
    note: Annotated[
        str | None,
        typer.Option(help="Attempt note (required for needs-work)"),
    ] = None,
    changed_file: Annotated[
        list[str] | None,
        typer.Option("--changed-file", help="Changed file; repeat as needed"),
    ] = None,
    hint: Annotated[
        list[str] | None,
        typer.Option("--hint", help="Tutor escalation step used; repeat as needed"),
    ] = None,
    git_commit: Annotated[
        str | None,
        typer.Option("--git-commit", help="Optional git commit of the practice work"),
    ] = None,
    data_root: Annotated[
        Path,
        typer.Option("--data-root", help="Private SoloScale data root"),
    ] = Path(".soloscale"),
) -> None:
    _validate_private_data_root(data_root)
    try:
        store = CasebookStore(data_root)
        stored = load_exercise(exercise, data_root)
        receipt, result = ingest_practice_completion(
            store=store,
            exercise=stored,
            evidence_path=evidence,
            files_changed=changed_file or (),
            tests_passed=tests_passed,
            tests_failed=tests_failed,
            attempts=attempts,
            hints_used=hint or (),
            note=note,
            git_commit=git_commit,
        )
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    outcome = "PASS" if receipt.completed else "NEEDS_WORK"
    snapshot = result.mastery
    next_action = snapshot.next_stage.value.upper() if snapshot.next_stage else "COMPLETE"
    console.print(f"[green]Recorded[/green] {outcome} for {exercise}")
    console.print(
        f"Mastery: {receipt.mastery_before.value if receipt.mastery_before else 'L0 Seen'} -> "
        f"{receipt.mastery_after.value if receipt.mastery_after else 'L0 Seen'}"
    )
    console.print(
        f"Interview ready: {'yes' if receipt.interview_ready else 'no'} | "
        f"Next mastery action: {next_action}"
    )


@app.command("application-list")
def application_list(
    library_root: Annotated[
        Path,
        typer.Option("--library-root", help="Private Resume application library root"),
    ],
) -> None:
    records = list_application_records(library_root)
    table = Table(title="Career applications")
    table.add_column("Role")
    table.add_column("Company")
    table.add_column("Status")
    table.add_column("Run")
    for record in records:
        table.add_row(
            record.role or "-",
            record.company or "-",
            record.status.value,
            record.soloscale_run_id,
        )
    console.print(table)
    console.print(f"Total: {len(records)}")


@app.command("application-status")
def application_status_update(
    application: Annotated[
        str,
        typer.Argument(help="Application directory name under <library-root>/applications"),
    ],
    status: Annotated[ApplicationStatus, typer.Option("--status")],
    library_root: Annotated[
        Path,
        typer.Option("--library-root", help="Private Resume application library root"),
    ],
    note: Annotated[
        str | None,
        typer.Option("--note", help="Optional operator note"),
    ] = None,
    next_action: Annotated[
        str | None,
        typer.Option("--next-action", help="Optional next action"),
    ] = None,
) -> None:
    application_dir = applications_root(library_root) / application
    if not application_dir.is_dir() or application_dir.is_symlink():
        raise typer.BadParameter("application directory not found", param_hint="application")
    try:
        record = update_application_status(
            application_dir=application_dir,
            status=status,
            note=note,
            next_action=next_action,
        )
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(
        f"[green]Updated[/green] {record.role or application} -> {record.status.value}"
    )
    if record.next_action:
        console.print(f"Next action: {record.next_action}")


@app.command("application-link-learning")
def application_link_learning(
    application: Annotated[
        str,
        typer.Argument(help="Application directory name under <library-root>/applications"),
    ],
    case: Annotated[str, typer.Argument(help="Learning case ID")],
    library_root: Annotated[
        Path,
        typer.Option("--library-root", help="Private Resume application library root"),
    ],
    data_root: Annotated[
        Path,
        typer.Option("--data-root", help="Private SoloScale data root"),
    ] = Path(".soloscale"),
) -> None:
    application_dir = applications_root(library_root) / application
    if not application_dir.is_dir() or application_dir.is_symlink():
        raise typer.BadParameter("application directory not found", param_hint="application")
    try:
        record = link_learning_case(
            application_dir=application_dir,
            learning_case_id=case,
            data_root=data_root,
        )
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(
        f"[green]Linked[/green] {record.role or application} -> learning case {case}"
    )
    console.print(
        f"Interview ready: {'yes' if record.interview_ready else 'no'}"
    )


@app.command("control-tower-build")
def control_tower_build(
    data_root: Annotated[
        Path,
        typer.Option("--data-root", help="Private SoloScale data root"),
    ] = Path(".soloscale"),
    output: Annotated[
        Path | None,
        typer.Option(help="Optional output HTML path"),
    ] = None,
) -> None:
    _validate_private_data_root(data_root)
    try:
        store = CasebookStore(data_root)
        out = build_control_tower(store, output)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--output") from exc
    console.print(f"[green]Created[/green] {out}")


@app.command("knowledge-sync")
def knowledge_sync(
    data_root: Annotated[
        Path,
        typer.Option("--data-root", help="Private ignored SoloScale data root"),
    ] = Path(".soloscale"),
    codex_home: Annotated[
        Path,
        typer.Option(help="Local Codex home containing sessions/ and archived_sessions/"),
    ] = _DEFAULT_CODEX_HOME,
    include_codex: Annotated[
        bool,
        typer.Option("--codex/--no-codex", help="Discover local Codex sessions"),
    ] = True,
    chatgpt_export: Annotated[
        list[Path] | None,
        typer.Option(
            "--chatgpt-export",
            help="ChatGPT conversations.json or export ZIP; repeat as needed",
        ),
    ] = None,
    buildlog_root: Annotated[
        list[Path] | None,
        typer.Option(
            "--buildlog-root",
            help="BuildLog repository/run root; repeat as needed (auto-detected if omitted)",
        ),
    ] = None,
) -> None:
    """Incrementally index private, user-visible engineering conversations."""

    _validate_private_data_root(data_root)
    reports: list[SyncReport] = []
    try:
        store = KnowledgeStore(data_root)
    except (KnowledgeStoreError, OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--data-root") from exc

    if include_codex:
        try:
            codex_sources = discover_codex_sources(codex_home)
        except (OSError, ValueError) as exc:
            reports.append(_sanitized_sync_failure(codex_home, SourceKind.CODEX_SESSION, exc))
        else:
            for source_path in codex_sources:
                try:
                    reports.extend(_sync_parsed_sources(store, [parse_codex_session(source_path)]))
                except (KnowledgeStoreError, OSError, ValueError) as exc:
                    reports.append(
                        _sanitized_sync_failure(
                            source_path,
                            SourceKind.CODEX_SESSION,
                            exc,
                        )
                    )

    for export_path in chatgpt_export or []:
        try:
            parsed_exports = parse_chatgpt_export(export_path)
            reports.extend(_sync_parsed_sources(store, parsed_exports))
        except (KnowledgeStoreError, OSError, ValueError) as exc:
            reports.append(_sanitized_sync_failure(export_path, SourceKind.CHATGPT_EXPORT, exc))

    selected_buildlog_roots = (
        list(buildlog_root) if buildlog_root else _auto_buildlog_roots(Path.cwd())
    )
    for root in selected_buildlog_roots:
        try:
            run_directories = discover_buildlog_runs(root)
        except (OSError, ValueError) as exc:
            reports.append(_sanitized_sync_failure(root, SourceKind.BUILDLOG_RUN, exc))
            continue
        for run_directory in run_directories:
            try:
                reports.extend(_sync_parsed_sources(store, [parse_buildlog_run(run_directory)]))
            except (KnowledgeStoreError, OSError, ValueError) as exc:
                reports.append(
                    _sanitized_sync_failure(
                        run_directory,
                        SourceKind.BUILDLOG_RUN,
                        exc,
                    )
                )

    if not reports:
        console.print("[yellow]No supported conversation sources were discovered.[/yellow]")
        return

    report = _combine_sync_reports(reports)
    table = Table(title="Private knowledge sync")
    table.add_column("Result")
    table.add_column("Count", justify="right")
    table.add_row("Discovered", str(report.discovered))
    table.add_row("Imported", str(report.imported))
    table.add_row("Updated", str(report.updated))
    table.add_row("Unchanged", str(report.skipped))
    table.add_row("Failed", str(report.failed))
    table.add_row("Chunks written", str(report.chunks_written))
    console.print(table)
    if report.failures:
        console.print(
            "[yellow]Some sources were deferred.[/yellow] Failure receipts contain only "
            "source locators and error types; rerun sync after inspecting the local source."
        )
    console.print(f"Private index: {store.database_path}")
    successful_sources = report.imported + report.updated + report.skipped
    if report.failed and successful_sources == 0:
        console.print("[red]Knowledge sync failed for every discovered source.[/red]")
        raise typer.Exit(code=1)


@app.command("knowledge-status")
def knowledge_status(
    data_root: Annotated[
        Path,
        typer.Option("--data-root", help="Private ignored SoloScale data root"),
    ] = Path(".soloscale"),
) -> None:
    """Show metadata-only index coverage without printing conversation bodies."""

    _validate_private_data_root(data_root)
    try:
        status = KnowledgeStore(data_root).status()
    except (KnowledgeStoreError, OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--data-root") from exc
    table = Table(title="Conversation knowledge")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Documents", str(status.documents))
    table.add_row("Chunks", str(status.chunks))
    for source_kind, count in sorted(status.source_counts.items()):
        table.add_row(source_kind, str(count))
    table.add_row(
        "Last sync",
        status.last_synced_at.isoformat() if status.last_synced_at else "NEVER",
    )
    console.print(table)


@app.command("evidence-refresh")
def evidence_refresh(
    data_root: Annotated[
        Path,
        typer.Option("--data-root", help="Private ignored SoloScale data root"),
    ] = Path(".soloscale"),
    repository_root: Annotated[
        Path | None,
        typer.Option("--repository-root", help="Repository used for Git snapshot metadata"),
    ] = None,
    buildlog_root: Annotated[
        list[Path] | None,
        typer.Option("--buildlog-root", help="BuildLog root; repeat as needed"),
    ] = None,
) -> None:
    """Explicitly refresh metadata-only local evidence without models or publishing."""

    _validate_private_data_root(data_root)
    try:
        receipt = refresh_evidence_catalog(
            data_root,
            repository_root=repository_root or Path.cwd(),
            buildlog_roots=buildlog_root or (),
        )
        status = EvidenceHub(data_root).status()
    except (EvidenceHubError, OSError, ValueError) as exc:
        raise typer.BadParameter("evidence refresh could not be completed") from exc
    table = Table(title="Evidence catalog refresh")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Status", receipt.status.value)
    table.add_row("Sources", str(status.source_count))
    table.add_row("Evidence", str(status.evidence_count))
    table.add_row("Assets", str(status.asset_count))
    table.add_row("Outcomes", str(status.outcome_count))
    table.add_row("Created", str(receipt.created_count))
    table.add_row("Updated", str(receipt.updated_count))
    table.add_row("Unchanged", str(receipt.unchanged_count))
    table.add_row("Errors", str(receipt.error_count))
    console.print(table)
    if receipt.status.value == "failed":
        console.print(
            "[red]Evidence refresh failed. Review local source availability and retry.[/red]"
        )
        raise typer.Exit(code=1)


@app.command("knowledge-reset")
def knowledge_reset(
    data_root: Annotated[
        Path,
        typer.Option("--data-root", help="Private ignored SoloScale data root"),
    ] = Path(".soloscale"),
    confirmed: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Confirm deletion of the derived conversation index",
        ),
    ] = False,
) -> None:
    """Reset the derived search index while preserving Evidence Agent run receipts."""

    if not confirmed:
        raise typer.BadParameter(
            "knowledge reset requires --yes; agent-run receipts are preserved",
            param_hint="--yes",
        )
    _validate_private_data_root(data_root)
    try:
        KnowledgeStore.reset_derived_index(data_root)
    except (KnowledgeStoreError, OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--data-root") from exc
    console.print("[green]Reset[/green] derived conversation index.")
    console.print("Private Evidence Agent run receipts were preserved.")


@app.command("knowledge-search")
def knowledge_search(
    query: Annotated[str, typer.Argument(help="Private evidence search query")],
    data_root: Annotated[
        Path,
        typer.Option("--data-root", help="Private ignored SoloScale data root"),
    ] = Path(".soloscale"),
    limit: Annotated[int, typer.Option(min=1, max=50)] = 10,
    source_kind: Annotated[
        list[SourceKind] | None,
        typer.Option("--source-kind", help="Restrict source kind; repeat as needed"),
    ] = None,
) -> None:
    """Search the private index deterministically without calling an LLM."""

    _validate_private_data_root(data_root)
    try:
        hits = KnowledgeStore(data_root).search(
            query,
            limit=limit,
            source_kinds=source_kind,
        )
    except (KnowledgeStoreError, OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="query") from exc
    if not hits:
        console.print("[yellow]No evidence chunks matched.[/yellow]")
        return
    table = Table(title=f"Knowledge search — {query}")
    table.add_column("Chunk")
    table.add_column("Source")
    table.add_column("Score", justify="right")
    table.add_column("Excerpt")
    for hit in hits:
        display_excerpt = hit.excerpt
        if len(display_excerpt) > 360:
            display_excerpt = f"{display_excerpt[:359]}…"
        table.add_row(
            hit.chunk_id,
            hit.source_kind.value,
            f"{hit.score:.4f}",
            display_excerpt,
        )
    console.print(table)


@app.command("evidence-agent")
def evidence_agent(
    question: Annotated[str, typer.Argument(help="Question to investigate from local evidence")],
    data_root: Annotated[
        Path,
        typer.Option("--data-root", help="Private ignored SoloScale data root"),
    ] = Path(".soloscale"),
    model: Annotated[
        str,
        typer.Option(help="Already-installed local Ollama model"),
    ] = "qwen3:8b",
    ollama_url: Annotated[
        str,
        typer.Option(help="Loopback-only Ollama base URL"),
    ] = "http://127.0.0.1:11434",
    max_rounds: Annotated[int, typer.Option(min=1, max=3)] = 2,
    source_kind: Annotated[
        list[SourceKind] | None,
        typer.Option("--source-kind", help="Restrict source kind; repeat as needed"),
    ] = None,
) -> None:
    """Run a bounded, citation-enforced local evidence investigation."""

    _validate_private_data_root(data_root)
    try:
        store = KnowledgeStore(data_root)
        reasoner = OllamaReasoner(endpoint=ollama_url, model=model)
        agent = BoundedEvidenceAgent(
            store,
            reasoner,
            data_root,
            max_rounds=max_rounds,
        )
        result = agent.run(question, source_kinds=source_kind)
    except (EvidenceAgentError, KnowledgeStoreError, OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(Panel(result.answer, title="Evidence candidate — human confirmation required"))
    summary = Table(title="Agent run receipt")
    summary.add_column("Field")
    summary.add_column("Value")
    summary.add_row("Run", result.run_id)
    summary.add_row("Model", result.model)
    summary.add_row("Queries", str(len(result.queries)))
    summary.add_row("Retrieved chunks", str(len(result.retrieved_chunk_ids)))
    summary.add_row("Cited chunks", str(len(result.refs)))
    summary.add_row("Open/unsupported", str(len(result.open_questions) + len(result.unsupported)))
    summary.add_row("Status", result.status)
    console.print(summary)
    console.print(
        "Private result: "
        f"{data_root / 'knowledge' / 'agent-runs' / result.run_id / '04_result.json'}"
    )
    console.print("No Casebook, BuildLog, resume, or publishing record was changed.")


@app.command("demo")
def demo() -> None:
    task = TaskEnvelope(
        id="task-research-citations-001",
        title="Add source-grounded citations to the Research Agent",
        goal=(
            "Every externally verifiable claim in the generated report must link to "
            "inspectable source evidence."
        ),
        repository="../AI-Research-Assistant-LangJu-Edition",
        branch="feat/source-grounded-citations",
        requested_paths=["app/", "tests/"],
        reasoning_depth=ReasoningDepth.HIGH,
        requires_local_files=True,
        requires_terminal=True,
        frozen_decisions=["Missing source evidence must never be invented."],
        required_changes=["Trace every externally verifiable claim to a source identifier."],
        constraints=[
            "Preserve the existing user-facing workflow.",
            "Do not add a production dependency without approval.",
        ],
        acceptance_criteria=[
            "Each report claim can be traced to a source identifier.",
            "Missing sources are represented honestly rather than invented.",
            "Unit tests cover successful and missing-citation paths.",
        ],
        tests_to_run=["pytest"],
        non_goals=[
            "Redesigning the entire Research Agent UI.",
            "Adding autonomous publishing.",
        ],
    )
    decision = route_task(task)
    packet = packet_from_task(task)
    console.print(Panel(task.model_dump_json(indent=2), title="Task Envelope"))
    console.print(Panel(decision.model_dump_json(indent=2), title="Route Decision"))
    console.print(Panel(render_packet_markdown(packet), title="Execution Packet"))


if __name__ == "__main__":
    app()
