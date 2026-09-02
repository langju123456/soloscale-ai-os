"""Evidence-driven learning practice loop.

Turn a real LearningCase gap into a bounded practice exercise the operator can
open in VS Code, complete locally, and feed back into mastery without ever
mutating Resume truth.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from soloscale.casebook_models import (
    AttemptOutcome,
    LearningCase,
    MasterySnapshot,
    PracticeStage,
)
from soloscale.casebook_store import AttemptRecordResult, CasebookStore
from soloscale.learning_models import (
    MasteryAction,
    MasteryLevel,
    NonBlankStr,
    Sha256Digest,
    StableId,
    TruthStage,
)
from soloscale.models import ContractModel, utc_now

_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


class ExerciseType(StrEnum):
    EXPLAIN = "EXPLAIN"
    TRACE = "TRACE"
    DEBUG = "DEBUG"
    IMPLEMENT = "IMPLEMENT"
    DSA = "DSA"
    SYSTEM_DESIGN = "SYSTEM_DESIGN"
    DEFEND = "DEFEND"


class PracticeLanguage(StrEnum):
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    SQL = "sql"
    BASH = "bash"
    YAML = "yaml"
    GENERIC = "generic"


class ExerciseStatus(StrEnum):
    GENERATED = "GENERATED"
    OPENED = "OPENED"
    USER_PRACTICE_REQUIRED = "USER_PRACTICE_REQUIRED"
    COMPLETED = "COMPLETED"


class TutorEscalation(StrEnum):
    GUIDING_QUESTION = "guiding_question"
    CONCEPTUAL_HINT = "conceptual_hint"
    PSEUDOCODE = "pseudocode"
    MINIMAL_SYNTAX_EXAMPLE = "minimal_syntax_example"
    FULL_SOLUTION = "full_solution"


TUTOR_MODE_CONTRACT = (
    "You are a tutor for one SoloScale practice exercise, not an autonomous executor.\n"
    "Escalate in this exact order and never jump ahead:\n"
    "1. guiding_question: ask one question that helps the learner locate the next step.\n"
    "2. conceptual_hint: name the concept without giving the implementation.\n"
    "3. pseudocode: give structure only, no runnable code.\n"
    "4. minimal_syntax_example: show the smallest syntax example needed.\n"
    "5. full_solution: only when the learner explicitly asks for the full solution.\n"
    "Do not silently edit the exercise, starter code, or tests. Do not claim mastery for "
    "the learner. Keep answers bounded to the exercise objective and acceptance criteria."
)


class LearningExercise(ContractModel):
    id: StableId
    case_id: StableId
    project_source_id: StableId
    jd_requirement: NonBlankStr
    resume_claim: str | None = None
    evidence_ids: list[StableId] = Field(default_factory=list)
    exercise_type: ExerciseType
    objective: NonBlankStr
    bounded_task: NonBlankStr
    acceptance_criteria: list[NonBlankStr] = Field(min_length=1)
    difficulty: int = Field(ge=1, le=5)
    practice_language: PracticeLanguage = PracticeLanguage.PYTHON
    mastery_capability: MasteryAction
    capability_domain: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    status: ExerciseStatus = ExerciseStatus.GENERATED
    workspace_path: str | None = None

    @model_validator(mode="after")
    def validate_workspace_status(self) -> LearningExercise:
        if self.status in {ExerciseStatus.OPENED, ExerciseStatus.USER_PRACTICE_REQUIRED}:
            if not self.workspace_path:
                raise ValueError("active practice exercises require a workspace path")
        if self.status is ExerciseStatus.COMPLETED and not self.workspace_path:
            raise ValueError("completed practice exercises require a workspace path")
        return self


class PracticeCompletionReceipt(ContractModel):
    id: StableId
    exercise_id: StableId
    case_id: StableId
    files_changed: list[str] = Field(default_factory=list)
    tests_passed: int = Field(ge=0)
    tests_failed: int = Field(ge=0)
    user_code_sha256: Sha256Digest | None = None
    git_commit: str | None = None
    attempts: int = Field(ge=1)
    hints_used: list[TutorEscalation] = Field(default_factory=list)
    completed: bool
    mastery_before: MasteryLevel | None = None
    mastery_after: MasteryLevel | None = None
    interview_ready: bool
    submitted_at: datetime = Field(default_factory=utc_now)
    truth_stage: Literal[TruthStage.RAW_STATEMENT] = TruthStage.RAW_STATEMENT


_EXERCISE_CAPABILITY: dict[ExerciseType, MasteryAction] = {
    ExerciseType.EXPLAIN: MasteryAction.EXPLAIN,
    ExerciseType.TRACE: MasteryAction.TRACE,
    ExerciseType.DEBUG: MasteryAction.DEBUG,
    ExerciseType.IMPLEMENT: MasteryAction.REBUILD,
    ExerciseType.DSA: MasteryAction.REBUILD,
    ExerciseType.SYSTEM_DESIGN: MasteryAction.DEFEND,
    ExerciseType.DEFEND: MasteryAction.DEFEND,
}

_EXERCISE_STAGE: dict[ExerciseType, PracticeStage] = {
    ExerciseType.EXPLAIN: PracticeStage.EXPLAIN,
    ExerciseType.TRACE: PracticeStage.TRACE,
    ExerciseType.DEBUG: PracticeStage.DEBUG,
    ExerciseType.IMPLEMENT: PracticeStage.REBUILD,
    ExerciseType.DSA: PracticeStage.REBUILD,
    ExerciseType.SYSTEM_DESIGN: PracticeStage.DEFEND,
    ExerciseType.DEFEND: PracticeStage.DEFEND,
}


def _capability_domain(case: LearningCase) -> str:
    """Detect one practice capability domain from the case's durable facts."""

    text = " ".join([case.title, case.problem, *case.concepts]).lower()
    if any(
        token in text
        for token in (
            "ci/cd",
            "ci-cd",
            "github actions",
            "workflow",
            "pipeline",
            "build/release",
            "deployment",
            "automated testing",
            "verification gates",
        )
    ):
        return "CI_CD"
    if any(
        token in text
        for token in ("rest api", "endpoint", "http", "validation", "serializer")
    ):
        return "REST_API"
    if any(
        token in text
        for token in ("async", "job system", "queue", "retry", "idempotency")
    ):
        return "ASYNC_JOB"
    if any(
        token in text
        for token in ("observability", "trace", "span", "instrumentation", "logging")
    ):
        return "OBSERVABILITY"
    if any(
        token in text
        for token in ("security", "allowlist", "denial", "audit", "authentication")
    ):
        return "SECURITY"
    if any(
        token in text
        for token in ("rag", "retrieval", "chunking", "embedding", "vector")
    ):
        return "RAG"
    if any(
        token in text
        for token in ("dsa", "algorithm", "data structure")
    ):
        return "DSA"
    if any(
        token in text
        for token in ("system design", "architecture", "trade-off")
    ):
        return "SYSTEM_DESIGN"
    return "GENERAL"


def capability_requirement(case: LearningCase) -> str:
    """Return a capability-specific JD requirement for the case."""

    requirements = {
        "CI_CD": (
            "Automate software verification with CI/CD: run lint, type-check, tests, "
            "and build gates on push and pull requests."
        ),
        "REST_API": (
            "Design and test a bounded REST API endpoint with validation and error "
            "handling."
        ),
        "ASYNC_JOB": (
            "Implement a bounded background job system with explicit state, retry, "
            "and idempotency."
        ),
        "OBSERVABILITY": (
            "Add trace, log, and span instrumentation to make one failure path "
            "observable."
        ),
        "SECURITY": (
            "Implement an allowlist/denial and audit-log test for one sensitive "
            "boundary."
        ),
        "RAG": "Implement a bounded retrieval, chunking, and evaluation task.",
        "DSA": "Solve a bounded algorithmic exercise with correctness tests.",
        "SYSTEM_DESIGN": (
            "Produce a structured architecture and trade-off artifact for one system "
            "boundary."
        ),
    }
    return requirements.get(
        _capability_domain(case),
        "Design and build context, memory, tooling, and retrieval systems for AI "
        "products.",
    )


def _ci_cd_workspace_files(exercise: LearningExercise) -> dict[str, str]:
    """Return a real CI/CD practice task, not a solve()->True toy."""

    workflow = (
        "name: SoloScale verification\n\n"
        "on:\n"
        "  push:\n"
        "  pull_request:\n\n"
        "jobs:\n"
        "  verify:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      # TODO: set up the pinned Python runtime the repository expects.\n"
        "      # TODO: install development dependencies in a reproducible way.\n"
        "      # TODO: run the canonical lint, type-check, tests, and build gates.\n"
        "      # TODO: add one intentional-failure check that fails without weakening the gate.\n"
    )
    validator = (
        "#!/usr/bin/env python3\n"
        "\"\"\"Local CI/CD acceptance checks for the practice workflow.\n\n"
        "This validator does not grade your code; it verifies that the workflow\n"
        "actually encodes the expected capability instead of merely existing.\n"
        "\"\"\"\n"
        "import pathlib\n"
        "import sys\n\n\n"
        "def main() -> int:\n"
        "    path = pathlib.Path(\".github/workflows/ci.yml\")\n"
        "    if not path.is_file():\n"
        "        print(\"MISSING .github/workflows/ci.yml\")\n"
        "        return 1\n"
        "    text = path.read_text(encoding=\"utf-8\")\n"
        "    required = [\"push\", \"pull_request\", \"runs-on\", \"pytest\", \"ruff\", \"mypy\"]\n"
        "    missing = [token for token in required if token not in text]\n"
        "    if missing:\n"
        "        print(\"MISSING workflow tokens:\", \", \".join(missing))\n"
        "        return 1\n"
        "    print(\"CI/CD workflow encodes the expected verification gates.\")\n"
        "    return 0\n\n\n"
        "if __name__ == \"__main__\":\n"
        "    raise SystemExit(main())\n"
    )
    task = (
        "# CI/CD practice task\n\n"
        "Complete `.github/workflows/ci.yml` so it automates SoloScale verification.\n\n"
        "Required capabilities:\n"
        "- triggers: push and pull_request\n"
        "- a pinned Python runtime and reproducible dependency install\n"
        "- the canonical gates: pytest, ruff, mypy, and package build\n"
        "- one intentional failure/recovery scenario that does not weaken the gate\n\n"
        "Verify locally with `python validate.py` and record your evidence.\n"
    )
    return {
        ".github/workflows/ci.yml": workflow,
        "validate.py": validator,
        "task.md": task,
    }


def generate_practice_exercise(
    *,
    case: LearningCase,
    jd_requirement: str,
    resume_claim: str | None = None,
    exercise_type: ExerciseType = ExerciseType.IMPLEMENT,
    difficulty: int = 2,
    practice_language: PracticeLanguage = PracticeLanguage.PYTHON,
    project_source_id: str = "local-casebook",
) -> LearningExercise:
    """Derive one bounded exercise from a real case and a JD requirement."""

    requirement = jd_requirement.strip()
    if not requirement:
        raise ValueError("jd_requirement must be nonblank")
    if difficulty < 1 or difficulty > 5:
        raise ValueError("difficulty must be between 1 and 5")
    normalized_type = ExerciseType(exercise_type)
    normalized_language = PracticeLanguage(practice_language)
    domain = _capability_domain(case)
    if normalized_type is ExerciseType.IMPLEMENT and domain == "CI_CD":
        normalized_language = PracticeLanguage.YAML
    return LearningExercise(
        id=f"exercise-{uuid4().hex[:12]}",
        case_id=case.id,
        project_source_id=project_source_id,
        jd_requirement=requirement,
        resume_claim=resume_claim.strip() if resume_claim else None,
        evidence_ids=[receipt.id for receipt in case.evidence],
        exercise_type=normalized_type,
        objective=(
            "Automate SoloScale verification with a GitHub Actions workflow that "
            "runs lint, type-check, tests, and build gates."
            if normalized_type is ExerciseType.IMPLEMENT and domain == "CI_CD"
            else _build_objective(case, requirement, normalized_type)
        ),
        bounded_task=(
            "Complete `.github/workflows/ci.yml` so it encodes a real CI/CD "
            "verification pipeline with the expected triggers, a reproducible runtime, "
            "the canonical pytest/Ruff/mypy/build gates, and one intentional "
            "failure/recovery scenario. Verify locally with `python validate.py`."
            if normalized_type is ExerciseType.IMPLEMENT and domain == "CI_CD"
            else _build_bounded_task(case, normalized_type, normalized_language)
        ),
        acceptance_criteria=(
            [
                "The workflow file exists under .github/workflows/ci.yml.",
                "The workflow encodes push and pull_request triggers.",
                "The workflow runs pytest, ruff, mypy, and a package build.",
                "The workflow includes an intentional failure/recovery scenario.",
                "python validate.py passes against the completed workflow.",
            ]
            if normalized_type is ExerciseType.IMPLEMENT and domain == "CI_CD"
            else _build_acceptance_criteria(case, normalized_type)
        ),
        difficulty=difficulty,
        practice_language=normalized_language,
        mastery_capability=_EXERCISE_CAPABILITY[normalized_type],
        capability_domain=domain,
    )


def practice_workspace_root(data_root: Path) -> Path:
    return Path(data_root) / "practice-workspaces"


def create_practice_workspace(exercise: LearningExercise, data_root: Path) -> Path:
    """Create the private local practice workspace with README, starter, and tests."""

    root = practice_workspace_root(data_root)
    root.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    workspace = root / exercise.id
    if workspace.exists():
        raise FileExistsError(f"practice workspace already exists: {workspace}")
    workspace.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    try:
        _write_private(workspace / "README.md", _render_readme(exercise))
        if exercise.capability_domain == "CI_CD":
            for relative, content in _ci_cd_workspace_files(exercise).items():
                _write_private(workspace / relative, content)
        elif exercise.practice_language is PracticeLanguage.PYTHON:
            _write_private(workspace / "starter.py", _render_python_starter())
            _write_private(workspace / "test_task.py", _render_python_test())
        else:
            _write_private(
                workspace / f"starter{_starter_suffix(exercise.practice_language)}",
                _render_generic_starter(exercise.practice_language),
            )
            _write_private(workspace / "task.md", _render_task_markdown(exercise))
        stored = exercise.model_copy(
            update={
                "status": ExerciseStatus.USER_PRACTICE_REQUIRED,
                "workspace_path": str(workspace),
            }
        )
        _write_private(
            workspace / "exercise.json", stored.model_dump_json(indent=2) + "\n"
        )
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise
    return workspace


def open_practice_workspace(workspace_path: Path) -> bool:
    """Open the workspace in VS Code when the ``code`` CLI is available."""

    code_cli = shutil.which("code")
    if code_cli is None:
        return False
    try:
        subprocess.run(
            [code_cli, str(workspace_path)],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def save_exercise(exercise: LearningExercise, data_root: Path) -> None:
    root = practice_workspace_root(data_root)
    root.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    _write_private(root / exercise.id / "exercise.json", exercise.model_dump_json(indent=2) + "\n")


def load_exercise(exercise_id: str, data_root: Path) -> LearningExercise:
    path = practice_workspace_root(data_root) / exercise_id / "exercise.json"
    if not path.is_file():
        raise ValueError(f"practice exercise not found: {exercise_id}")
    return LearningExercise.model_validate_json(_read_private(path))


def list_exercises(data_root: Path) -> list[LearningExercise]:
    root = practice_workspace_root(data_root)
    if not root.is_dir():
        return []
    exercises: list[LearningExercise] = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        exercise_path = directory / "exercise.json"
        if exercise_path.is_file():
            exercises.append(LearningExercise.model_validate_json(_read_private(exercise_path)))
    return exercises


_CI_CD_WORKFLOW_TOKENS = (
    "push",
    "pull_request",
    "runs-on",
    "pytest",
    "ruff",
    "mypy",
    "build",
)


def _evaluate_exercise_acceptance(
    exercise: LearningExercise,
    workspace: Path,
    evidence: Path | None,
) -> tuple[AttemptOutcome, str | None]:
    """Evaluate the exercise-specific acceptance contract.

    Capability-specific exercises use their own contract; generic exercises
    preserve the original evidence-presence semantics.
    """

    if exercise.capability_domain == "CI_CD":
        return _evaluate_ci_cd_acceptance(workspace)
    outcome = AttemptOutcome.PASS if evidence is not None else AttemptOutcome.NEEDS_WORK
    return outcome, None


def _evaluate_ci_cd_acceptance(workspace: Path) -> tuple[AttemptOutcome, str | None]:
    """Evaluate the CI/CD exercise acceptance contract from its workspace."""

    workflow = workspace / ".github" / "workflows" / "ci.yml"
    if not workflow.is_file():
        return (
            AttemptOutcome.NEEDS_WORK,
            "CI workflow validation failed — .github/workflows/ci.yml is missing",
        )
    try:
        text = workflow.read_text(encoding="utf-8")
    except OSError:
        return (
            AttemptOutcome.NEEDS_WORK,
            "CI workflow validation failed — could not read the workflow",
        )
    missing = [token for token in _CI_CD_WORKFLOW_TOKENS if token not in text]
    if missing:
        return (
            AttemptOutcome.NEEDS_WORK,
            f"CI workflow validation failed — missing gates: {', '.join(missing)}",
        )
    validator = workspace / "validate.py"
    if not validator.is_file():
        return (
            AttemptOutcome.NEEDS_WORK,
            "CI workflow validation failed — validate.py is missing",
        )
    interpreter = _python_interpreter()
    if interpreter is None:
        return (
            AttemptOutcome.NEEDS_WORK,
            "CI workflow validation failed — no Python interpreter is available",
        )
    try:
        completed = subprocess.run(
            [interpreter, str(validator)],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return (
            AttemptOutcome.NEEDS_WORK,
            "CI workflow validation failed — validate.py could not run",
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        reason = detail[0] if detail else f"exit code {completed.returncode}"
        return AttemptOutcome.NEEDS_WORK, f"CI workflow validation failed — {reason}"
    return AttemptOutcome.PASS, None


def _python_interpreter() -> str | None:
    """Resolve a CLI Python interpreter for running the generated validator.

    Under a PyInstaller bundle ``sys.executable`` is the frozen application
    binary, not Python. Source runs keep the active interpreter; packaged runs
    fall back to ``python3`` on PATH (the validator only uses stdlib).
    """

    if not getattr(sys, "frozen", False):
        return sys.executable
    discovered = shutil.which("python3")
    return discovered


def ingest_practice_completion(
    *,
    store: CasebookStore,
    exercise: LearningExercise,
    evidence_path: Path | None = None,
    files_changed: Sequence[str] = (),
    tests_passed: int = 0,
    tests_failed: int = 0,
    attempts: int = 1,
    hints_used: Sequence[TutorEscalation] = (),
    note: str | None = None,
    git_commit: str | None = None,
) -> tuple[PracticeCompletionReceipt, AttemptRecordResult]:
    """Record one practice result and advance case mastery.

    Capability-specific exercises use their own acceptance contract (CI/CD
    requires a valid workflow with the expected gates); generic exercises still
    require a nonempty practice artifact. Mastery advances through the canonical
    sequential Casebook gate; Resume truth is never touched.
    """

    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if tests_passed < 0 or tests_failed < 0:
        raise ValueError("test counts cannot be negative")
    normalized_hints = [TutorEscalation(hint) for hint in hints_used]
    evidence = Path(evidence_path) if evidence_path is not None else None
    if evidence is not None and not _nonempty_regular_file(evidence):
        raise ValueError("practice evidence must be a nonempty regular file")

    workspace = (
        Path(exercise.workspace_path)
        if exercise.workspace_path
        else practice_workspace_root(store.root) / exercise.id
    )
    outcome, failure_reason = _evaluate_exercise_acceptance(exercise, workspace, evidence)
    receipt_path = evidence
    if exercise.capability_domain == "CI_CD" and outcome is AttemptOutcome.PASS:
        workflow = workspace / ".github" / "workflows" / "ci.yml"
        if workflow.is_file():
            receipt_path = workflow
    if outcome is AttemptOutcome.NEEDS_WORK:
        if failure_reason:
            trimmed_note = note.strip() if note else ""
            note = (
                f"{trimmed_note} — {failure_reason}".strip()
                if trimmed_note
                else failure_reason
            )
        elif not (note and note.strip()):
            raise ValueError("a needs-work practice completion requires a note")

    mastery_before = store.mastery(exercise.case_id)
    result = store.record_attempt(
        case_id=exercise.case_id,
        stage=_EXERCISE_STAGE[exercise.exercise_type],
        outcome=outcome,
        receipt_path=receipt_path,
        note=note,
    )
    user_code_sha256 = _sha256_file(receipt_path) if receipt_path is not None else None
    receipt = PracticeCompletionReceipt(
        id=f"receipt-{uuid4().hex[:12]}",
        exercise_id=exercise.id,
        case_id=exercise.case_id,
        files_changed=list(files_changed),
        tests_passed=tests_passed,
        tests_failed=tests_failed,
        user_code_sha256=user_code_sha256,
        git_commit=git_commit.strip() if git_commit else None,
        attempts=attempts,
        hints_used=normalized_hints,
        completed=outcome is AttemptOutcome.PASS,
        mastery_before=_mastery_level(mastery_before),
        mastery_after=_mastery_level(result.mastery),
        interview_ready=result.mastery.interview_ready,
    )
    workspace = (
        Path(exercise.workspace_path)
        if exercise.workspace_path
        else practice_workspace_root(store.root) / exercise.id
    )
    _write_private(
        workspace / "completion-receipt.json", receipt.model_dump_json(indent=2) + "\n"
    )
    updated = exercise.model_copy(
        update={
            "status": ExerciseStatus.COMPLETED
            if receipt.completed
            else ExerciseStatus.USER_PRACTICE_REQUIRED,
            "workspace_path": str(workspace),
        }
    )
    save_exercise(updated, store.root)
    return receipt, result


def _build_objective(case: LearningCase, requirement: str, exercise_type: ExerciseType) -> str:
    verb = {
        ExerciseType.EXPLAIN: "Explain",
        ExerciseType.TRACE: "Trace the flow behind",
        ExerciseType.DEBUG: "Debug a regression variant of",
        ExerciseType.IMPLEMENT: "Implement the smallest working version of the resolution behind",
        ExerciseType.DSA: "Solve a small algorithmic exercise based on",
        ExerciseType.SYSTEM_DESIGN: "Design the smallest system that would prevent the failure in",
        ExerciseType.DEFEND: "Defend the diagnosis and solution of",
    }[exercise_type]
    return (
        f"{verb} the case \u201c{case.title}\u201d to satisfy the JD requirement: {requirement}"
    )


def _build_bounded_task(
    case: LearningCase, exercise_type: ExerciseType, language: PracticeLanguage
) -> str:
    language_label = f"{language.value} " if language is not PracticeLanguage.GENERIC else ""
    if exercise_type is ExerciseType.EXPLAIN:
        return (
            f"Write a bounded explanation (up to 400 words) connecting expected behavior, actual "
            f"behavior, root cause, resolution, and verification.\n\n"
            f"Expected: {case.expected_behavior}\n"
            f"Actual: {case.actual_behavior}\n"
            f"Root cause: {case.root_cause}\n"
            f"Verification: {'; '.join(case.verification)}\n\n"
            "Include at least one trade-off and one truth boundary (what remains "
            "unknown or unproven)."
        )
    if exercise_type is ExerciseType.TRACE:
        return (
            f"Trace the execution or data flow from input to the failure and through the fix.\n\n"
            f"Resolution: {case.resolution}\n"
            f"Concepts: {', '.join(case.concepts)}\n\n"
            "Name each important boundary and place the divergence at the correct boundary "
            "with a complete causal chain."
        )
    if exercise_type is ExerciseType.DEBUG:
        return (
            f"The starter code contains a deliberately broken variant of the resolution.\n\n"
            f"Original resolution: {case.resolution}\n"
            f"Root cause to revisit: {case.root_cause}\n\n"
            "Find the new failure, form and test a falsifiable hypothesis, fix the smallest "
            "root cause, and make the provided test pass without weakening it."
        )
    if exercise_type is ExerciseType.DSA:
        return (
            f"Extract the core algorithmic/technical pattern from this case and implement it as "
            f"a small standalone {language_label}solution with a correctness test you author.\n\n"
            f"Concepts: {', '.join(case.concepts)}\n"
            f"Resolution: {case.resolution}"
        )
    if exercise_type is ExerciseType.SYSTEM_DESIGN:
        return (
            f"Write a small design document (up to 600 words) covering components and data flow, "
            f"the failure mode this design prevents, alternatives considered, trade-offs, and a "
            f"decision with a truth boundary.\n\n"
            f"Failure: {case.actual_behavior}\n"
            f"Root cause: {case.root_cause}\n"
            f"Resolution: {case.resolution}"
        )
    if exercise_type is ExerciseType.DEFEND:
        alternatives = "; ".join(case.alternatives_considered) or "none recorded"
        trade_offs = "; ".join(case.trade_offs) or "none recorded"
        unknowns = "; ".join(case.unknowns) or "none recorded"
        return (
            f"Prepare to defend the case under interview-style follow-up questions.\n\n"
            f"Alternatives considered: {alternatives}\n"
            f"Trade-offs: {trade_offs}\n"
            f"Unknowns: {unknowns}\n\n"
            "Write your answer as if asked \u201cWhy is this the right approach, and "
            "what are its limits?\u201d"
        )
    return (
        f"Reproduce the core capability from the case without copying private evidence bodies.\n\n"
        f"Case resolution to internalize: {case.resolution}\n"
        f"Concepts to apply: {', '.join(case.concepts)}\n\n"
        f"In the provided workspace, implement the starter so it produces a working minimal "
        f"{language_label}solution for that capability. Keep the solution bounded to the task "
        "and work from your own understanding; case evidence stays private and must not be "
        "copied into the workspace."
    )


def _build_acceptance_criteria(case: LearningCase, exercise_type: ExerciseType) -> list[str]:
    if exercise_type is ExerciseType.EXPLAIN:
        return [
            "The explanation covers symptom, root cause, resolution, and verification.",
            "At least one trade-off and one truth boundary are included.",
            "The explanation does not overclaim beyond the case evidence.",
        ]
    if exercise_type is ExerciseType.TRACE:
        return [
            "Every important boundary is named.",
            "The divergence is placed at the correct boundary.",
            "The causal chain is complete from symptom to resolution.",
        ]
    if exercise_type is ExerciseType.DEBUG:
        return [
            "The broken variant is fixed at the smallest root cause.",
            "The provided test passes without being weakened.",
            "The new root cause and the falsifiable hypothesis you tested are stated.",
        ]
    if exercise_type is ExerciseType.DSA:
        return [
            "A small standalone solution implements the core pattern.",
            "A correctness test you authored passes.",
            "The solution stays bounded to the case and adds no unrelated features.",
        ]
    if exercise_type is ExerciseType.SYSTEM_DESIGN:
        return [
            "The design names components and data flow.",
            "The design explicitly addresses the case failure mode.",
            "Alternatives, trade-offs, and a decision are stated with a truth boundary.",
        ]
    if exercise_type is ExerciseType.DEFEND:
        return [
            "The defense explains why the approach is right.",
            "Alternatives and trade-offs are acknowledged.",
            "Unknowns are stated without overclaiming.",
        ]
    return [
        "A working minimal solution exists in the workspace starter file.",
        "The provided test passes.",
        (
            "The code is authored by you from your own understanding, not copied "
            "from private evidence."
        ),
        f"The solution stays bounded to the case capability: {', '.join(case.concepts)}.",
    ]


def _render_readme(exercise: LearningExercise) -> str:
    if exercise.capability_domain == "CI_CD":
        language_run = "Verify your workflow locally with `python validate.py`."
    elif exercise.practice_language is PracticeLanguage.PYTHON:
        language_run = "Run the test with `python -m pytest -q`."
    else:
        language_run = "Follow task.md for the language-specific run/check instructions."
    return (
        f"# Practice Exercise — {exercise.id}\n\n"
        f"- Case: `{exercise.case_id}`\n"
        f"- JD requirement: {exercise.jd_requirement}\n"
        f"- Exercise type: {exercise.exercise_type.value}\n"
        f"- Practice language: {exercise.practice_language.value}\n"
        f"- Difficulty: {exercise.difficulty}/5\n"
        f"- Mastery capability: {exercise.mastery_capability.value}\n\n"
        f"## Objective\n\n{exercise.objective}\n\n"
        f"## Bounded task\n\n{exercise.bounded_task}\n\n"
        "## Acceptance criteria\n\n"
        + "\n".join(f"- {criterion}" for criterion in exercise.acceptance_criteria)
        + "\n\n"
        f"## Run\n\n{language_run}\n\n"
        "## Tutor mode\n\n"
        + TUTOR_MODE_CONTRACT
        + "\n\n"
        "## Submit\n\n"
        "In the SoloScale app: Learning → Coding practice → submit your evidence file, test "
        "counts, and a note. Completion is decided by this exercise's acceptance criteria; "
        "mastery only advances through the canonical casebook gate and never rewrites "
        "Resume truth.\n"
    )


def _render_python_starter() -> str:
    return (
        "def solve() -> bool:\n"
        "    \"\"\"Implement the bounded task described in README.md.\n\n"
        "    Replace this stub with your own working solution and return True\n"
        "    only when the task is complete.\n"
        "    \"\"\"\n"
        "    raise NotImplementedError(\"replace me\")\n"
    )


def _render_python_test() -> str:
    return (
        "import pytest\n\n"
        "from starter import solve\n\n\n"
        "def test_task_completes() -> None:\n"
        "    assert solve() is True\n"
    )


def _render_generic_starter(language: PracticeLanguage) -> str:
    if language is PracticeLanguage.TYPESCRIPT:
        return (
            "export function solve(): boolean {\n"
            "  throw new Error(\"replace me\");\n"
            "}\n"
        )
    if language is PracticeLanguage.SQL:
        return "-- Implement the bounded task described in README.md.\n"
    if language is PracticeLanguage.BASH:
        return "#!/usr/bin/env bash\n\necho \"TODO: implement the bounded task\" >&2\nexit 1\n"
    return "# Implement the bounded task described in README.md.\n"


def _render_task_markdown(exercise: LearningExercise) -> str:
    return (
        f"# Task\n\n{exercise.bounded_task}\n\n"
        "## Acceptance criteria\n\n"
        + "\n".join(f"- {criterion}" for criterion in exercise.acceptance_criteria)
        + "\n"
    )


def _starter_suffix(language: PracticeLanguage) -> str:
    return {
        PracticeLanguage.TYPESCRIPT: ".ts",
        PracticeLanguage.SQL: ".sql",
        PracticeLanguage.BASH: ".sh",
        PracticeLanguage.GENERIC: ".txt",
    }[language]


def _mastery_level(snapshot: MasterySnapshot) -> MasteryLevel:
    levels = list(MasteryLevel)
    return levels[min(len(snapshot.passed_stages), len(levels) - 1)]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _nonempty_regular_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink() and path.stat().st_size > 0
    except OSError:
        return False


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".tmp-", delete=False
        ) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), _PRIVATE_FILE_MODE)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _read_private(path: Path) -> str:
    if path.is_symlink():
        raise ValueError(f"refusing symlink path: {path}")
    return path.read_text(encoding="utf-8")


def tutor_mode_prompt(exercise: LearningExercise) -> str:
    """Return the reusable tutor-mode contract scoped to one exercise."""

    return (
        f"{TUTOR_MODE_CONTRACT}\n\n"
        f"Exercise: {exercise.id} ({exercise.exercise_type.value})\n"
        f"Objective: {exercise.objective}\n"
        f"Practice language: {exercise.practice_language.value}\n"
        "Acceptance criteria: "
        + "; ".join(exercise.acceptance_criteria)
    )


def exercise_to_practice_stage(exercise_type: ExerciseType) -> PracticeStage:
    return _EXERCISE_STAGE[ExerciseType(exercise_type)]
