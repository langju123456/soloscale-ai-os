"""Deterministic Skill discovery, request routing, and private receipt persistence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from soloscale.editorial_pipeline import PrivateWriteError, write_private_once
from soloscale.skill_models import (
    ArtifactHash,
    DeterministicCheck,
    PhaseModelRoute,
    RepositoryRunState,
    RunOutcomeState,
    SelectedSkill,
    SkillRegistry,
    SkillRegistryEntry,
    SkillRunReceipt,
    SkillRunStatus,
    SkillStatus,
    SkillTaskEnvelope,
    SkillTaskRoute,
)

_SKILL_ORDER = (
    "task-intake-and-routing",
    "evidence-refresh-and-bundle",
    "editorial-package",
    "visual-storytelling",
    "linkedin-publishing",
    "x-thread-publishing",
    "resume-tailoring",
    "learning-gap-to-packet",
    "skill-distillation",
)
_PHASES = ("discovery", "decision", "implementation", "verification", "review")
_ROUTE_RANK = {"D0": 0, "F1": 1, "S2": 2, "D3": 3, "U4": 4}
_GENERAL_PUBLICATION_STOPS = (
    "stop before publication",
    "stop before publishing",
    "without publishing",
    "no publication",
    "发布前停止",
)
_PUBLICATION_ACTION_PATTERN = re.compile(
    r"(?:(?P<negation>do\s+not|don't|never|不要|不)\s+)?"
    r"(?P<action>publish|post(?:\s+to|\s+on)?|发布|发到)"
)
_IMMEDIATE_CHANNEL_NEGATION = re.compile(
    r"(?:\bnot|\bnever|不要|不)\s+(?:publish\s+|post\s+)?(?:to\s+|on\s+)?$"
)


class SkillOSError(ValueError):
    """Raised when Skill definitions, routing, or private persistence are unsafe."""


def default_registry_path() -> Path:
    """Resolve the canonical repo registry or its packaged read-only copy."""

    configured = os.environ.get("SOLOSCALE_SKILL_REGISTRY")
    if configured:
        return Path(configured).expanduser().absolute()

    candidates: list[Path] = []
    cwd = Path.cwd().absolute()
    candidates.extend(
        ancestor / ".agents" / "skills" / "registry.yaml"
        for ancestor in (cwd, *cwd.parents)
    )
    module_path = Path(__file__).absolute()
    candidates.extend(
        [
            module_path.parents[2] / ".agents" / "skills" / "registry.yaml",
            module_path.parents[1]
            / "share"
            / "soloscale-ai-os"
            / ".agents"
            / "skills"
            / "registry.yaml",
            Path(sys.prefix)
            / "share"
            / "soloscale-ai-os"
            / ".agents"
            / "skills"
            / "registry.yaml",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillOSError(f"invalid Skill registry artifact: {path.name}") from exc


def load_skill_registry(path: Path | None = None) -> SkillRegistry:
    """Load the tracked JSON-compatible registry and verify its Skill artifacts."""

    registry_path = (path or default_registry_path()).expanduser().absolute()
    try:
        registry = SkillRegistry.model_validate(_load_json(registry_path))
    except ValueError as exc:
        raise SkillOSError("Skill registry contract validation failed") from exc
    if tuple(skill.name for skill in registry.skills) != _SKILL_ORDER:
        raise SkillOSError("Skill registry must contain the canonical Skills in stable order")
    skills_root = registry_path.parent
    registry_bundle_root = registry_path.parents[2]
    for skill in registry.skills:
        skill_root = skills_root / skill.name
        required = (
            skill_root / "SKILL.md",
            skill_root / "contract.schema.json",
            skill_root / "CHANGELOG.md",
            skill_root / "agents" / "openai.yaml",
        )
        if any(not candidate.is_file() for candidate in required):
            raise SkillOSError(f"tracked artifacts are incomplete for Skill {skill.name}")
        instructions = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        if f"name: {skill.name}\n" not in instructions:
            raise SkillOSError(f"SKILL.md name does not match registry entry {skill.name}")
        if f"Version: `{skill.current_version}`" not in instructions:
            raise SkillOSError(f"SKILL.md version does not match registry entry {skill.name}")
        if f"Status: `{skill.status.value}`" not in instructions:
            raise SkillOSError(f"SKILL.md status does not match registry entry {skill.name}")
        contract_path = Path(skill.input_contract)
        if contract_path.is_absolute() or ".." in contract_path.parts:
            raise SkillOSError(f"input contract is unsafe for Skill {skill.name}")
        input_contract = (
            registry_bundle_root / contract_path
            if contract_path.parts and contract_path.parts[0] == ".agents"
            else skill_root / contract_path
        )
        if not input_contract.is_file():
            raise SkillOSError(f"input contract is missing for Skill {skill.name}")
        _load_json(skill_root / "contract.schema.json")
    return registry


def _contains(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def _publication_channels(text: str) -> tuple[bool, bool, bool]:
    """Return LinkedIn, X, and ambiguous intent using channel-local actions."""

    normalized = " ".join(text.casefold().split())
    if _contains(normalized, *_GENERAL_PUBLICATION_STOPS):
        return False, False, False

    action_matches = list(_PUBLICATION_ACTION_PATTERN.finditer(normalized))

    def channel_states(pattern: re.Pattern[str]) -> set[bool]:
        states: set[bool] = set()
        for mention in pattern.finditer(normalized):
            prefix = normalized[: mention.start()]
            if _IMMEDIATE_CHANNEL_NEGATION.search(prefix):
                states.add(False)
                continue
            preceding = [match for match in action_matches if match.start() < mention.start()]
            if preceding:
                states.add(preceding[-1].group("negation") is None)
        return states

    linkedin_states = channel_states(re.compile(r"\b linkedin\b|\blinkedin\b|领英"))
    x_states = channel_states(re.compile(r"\bx(?:\s+thread)?\b|\btwitter\b|推文串"))
    recognized_mentions = bool(linkedin_states or x_states)
    positive_action = any(match.group("negation") is None for match in action_matches)
    contradictory = len(linkedin_states) > 1 or len(x_states) > 1
    ambiguous = contradictory or (positive_action and not recognized_mentions)
    return True in linkedin_states, True in x_states, ambiguous


def _publication_requested(text: str) -> bool:
    linkedin, x_channel, ambiguous = _publication_channels(text)
    return linkedin or x_channel or ambiguous


def _selected_skill(entry: SkillRegistryEntry, reason: str) -> SelectedSkill:
    if entry.status in {SkillStatus.BLOCKED, SkillStatus.DEPRECATED}:
        raise SkillOSError(f"Skill {entry.name} is not selectable in status {entry.status}")
    return SelectedSkill(
        name=entry.name,
        version=entry.current_version,
        status=entry.status,
        reason=reason,
    )


def _choose_model_route(
    entries: list[SkillRegistryEntry],
) -> list[PhaseModelRoute]:
    routes: list[PhaseModelRoute] = []
    for phase in _PHASES:
        recommendations = [entry.recommended_model_route[phase] for entry in entries]
        recommendations.sort(
            key=lambda value: _ROUTE_RANK.get(value.split(maxsplit=1)[0], -1), reverse=True
        )
        recommended = recommendations[0]
        if recommended.startswith("U4"):
            recommended = "D3 deep_architect (U4 requires explicit operator approval)"
        routes.append(PhaseModelRoute(phase=phase, recommended=recommended))  # type: ignore[arg-type]
    return routes


def _dependency_order(
    registry: SkillRegistry,
    explicit_names: list[str],
) -> list[str]:
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise SkillOSError("Skill dependency graph contains a cycle")
        visiting.add(name)
        entry = registry.get(name)
        for dependency in entry.dependencies:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for name in explicit_names:
        visit(name)

    selected = set(ordered)
    for name in ordered:
        entry = registry.get(name)
        conflict = selected.intersection(entry.incompatible_skills)
        if conflict:
            raise SkillOSError(
                f"Skill {name} is incompatible with selected Skills: {sorted(conflict)}"
            )
    return ordered


def _normalize_request(request_text: str, selected_names: list[str]) -> SkillTaskEnvelope:
    request = " ".join(request_text.split())
    if not request:
        raise SkillOSError("operator request must not be blank")
    if len(request) > 4000:
        raise SkillOSError("operator request exceeds the bounded 4000-character intake limit")
    lowered = request.casefold()
    linkedin_publication, x_publication, ambiguous_publication = _publication_channels(lowered)
    publication_intent = linkedin_publication or x_publication or ambiguous_publication
    deployment_intent = _contains(lowered, "deploy", "deployment", "部署") and not _contains(
        lowered, "stop before deployment", "do not deploy", "no deployment", "不要部署"
    )
    application_intent = _contains(
        lowered, "submit application", "apply for", "application submission", "投递"
    ) and not _contains(
        lowered,
        "stop before application submission",
        "do not submit",
        "no application submission",
        "不要投递",
    )
    paid_intent = _contains(
        lowered, "paid api", "paid generation", "付费 api", "付费调用"
    ) and not _contains(lowered, "no paid api", "do not use paid", "不要付费")

    requested_artifacts: list[str] = []
    if _contains(lowered, "canonical story", "canonical narrative"):
        requested_artifacts.append("Canonical Story")
    if _contains(lowered, "linkedin"):
        requested_artifacts.append("LinkedIn artifact")
    if _contains(lowered, "x thread", "thread", "x 帖", "推文串"):
        requested_artifacts.append("X Thread")
    if _contains(lowered, "diagram", "visual", "image", "配图", "图"):
        requested_artifacts.append("Matched visual")
    if "resume-tailoring" in selected_names:
        requested_artifacts.extend(["Tailored resume", "Evidence map", "Unsupported requirements"])
    if "linkedin-publishing" in selected_names:
        requested_artifacts.append("Exact LinkedIn publication preview")
    if "x-thread-publishing" in selected_names:
        requested_artifacts.append("Exact X Thread publication preview")
    if "learning-gap-to-packet" in selected_names:
        requested_artifacts.append("Learning packet")
    if not requested_artifacts:
        requested_artifacts.append("Skill route")
    requested_artifacts = list(dict.fromkeys(requested_artifacts))

    if "resume-tailoring" in selected_names:
        workstream = "Career Studio"
        outcome = "A human-reviewable tailored resume package exists without invented claims."
    elif "linkedin-publishing" in selected_names or "x-thread-publishing" in selected_names:
        workstream = "Publishing"
        outcome = "Exact approved artifacts are ready at the required public PUBLISH gate."
    elif "editorial-package" in selected_names:
        workstream = "Content Studio"
        outcome = "A private evidence-backed editorial package is ready for human review."
    elif "evidence-refresh-and-bundle" in selected_names:
        workstream = "EvidenceHub"
        outcome = "A bounded private EvidenceBundle is available to the selected workflow."
    elif "skill-distillation" in selected_names:
        workstream = "Skill OS"
        outcome = "An auditable Skill change decision exists without automatic promotion."
    else:
        workstream = "SoloScale orchestration"
        outcome = "A concise bounded Skill route exists for the requested outcome."

    available_inputs: list[str] = ["Operator high-level request"]
    if _contains(lowered, "jd", "job description", "职位描述"):
        available_inputs.append("Job Description")
    if _contains(
        lowered,
        "approved profile",
        "approved candidate profile",
        "operator-approved candidate profile",
        "已批准候选人资料",
    ):
        available_inputs.append("Operator-approved Candidate Profile")
    elif _contains(lowered, "profile", "candidate profile", "候选人资料"):
        available_inputs.append("Candidate Profile (approval not established)")
    if _contains(lowered, "approved", "final package", "approved day"):
        available_inputs.append("Approved artifact package")
    if _contains(lowered, "latest evidence", "engineering evidence", "evidence", "证据"):
        available_inputs.append("Configured local evidence")

    evidence_requirements: list[str] = []
    if "evidence-refresh-and-bundle" in selected_names:
        evidence_requirements.extend(
            ["Use configured local sources only", "Preserve stable IDs and truth boundaries"]
        )
    if "resume-tailoring" in selected_names:
        evidence_requirements.append("Resume claims come only from the approved Candidate Profile")

    constraints = ["Use exact registered Skill versions", "Keep invocation state private"]
    non_goals = ["Do not redesign underlying product modules"]
    if _contains(lowered, *_GENERAL_PUBLICATION_STOPS):
        constraints.append("Stop before public publication")
        non_goals.append("No platform publication")
    if publication_intent:
        constraints.append("Stop at the explicit PUBLISH gate before any platform call")
    if paid_intent:
        constraints.append("Require approval before paid API usage")

    stop_match = re.search(r"(?:stop|停止)(?:\s+|在)([^.。;；]+)", lowered)
    completion = (
        f"Stop {stop_match.group(1).strip()}."
        if stop_match
        else "Produce the requested artifacts and required private Run Receipt."
    )
    return SkillTaskEnvelope(
        request_sha256=hashlib.sha256(request_text.encode("utf-8")).hexdigest(),
        objective=request,
        desired_user_or_external_outcome=outcome,
        requested_artifacts=requested_artifacts,
        project_or_workstream=workstream,
        available_inputs=available_inputs,
        evidence_requirements=evidence_requirements,
        constraints=constraints,
        publication_intent=publication_intent,
        deployment_intent=deployment_intent,
        application_submission_intent=application_intent,
        paid_api_intent=paid_intent,
        cost_boundary=(
            "Human approval before paid API usage" if paid_intent else "No paid call inferred"
        ),
        privacy_boundary=[
            "Persist under the private SoloScale data root",
            "Do not expose raw evidence bodies in tracked Skill artifacts",
        ],
        completion_condition=completion,
        known_non_goals=non_goals,
    )


def route_skill_request(
    request_text: str,
    *,
    registry: SkillRegistry | None = None,
) -> SkillTaskRoute:
    """Normalize a high-level request and compose the smallest registered Skill route."""

    available = registry or load_skill_registry()
    lowered = " ".join(request_text.casefold().split())
    explicit_names = ["task-intake-and-routing"]
    reasons = {
        "task-intake-and-routing": (
            "Normalize the operator request and preserve the selected Skill versions."
        )
    }

    publish_linkedin, publish_x, ambiguous_publication = _publication_channels(lowered)
    publication = publish_linkedin or publish_x or ambiguous_publication
    resume = _contains(lowered, "resume", "job description", "candidate profile", "简历", "jd")
    editorial = _contains(
        lowered,
        "canonical story",
        "linkedin post",
        "linkedin draft",
        "x thread",
        "editorial",
        "fresh-review",
        "revise",
        "长帖",
        "内容包",
    )
    evidence = _contains(lowered, "evidence", "latest engineering", "最新证据", "工程证据")
    visual = _contains(lowered, "diagram", "visual", "image", "配图", "架构图", "流程图")
    learning = _contains(
        lowered, "learning gap", "learning gaps", "learning packet", "学习缺口", "学习包"
    )
    distillation = _contains(
        lowered, "distill skill", "skill improvement", "skill update", "沉淀 skill"
    )

    primary_name = "task-intake-and-routing"
    if publication:
        if ambiguous_publication and not (publish_linkedin or publish_x):
            raise SkillOSError("publication channel is ambiguous; name LinkedIn or X")
        if editorial:
            explicit_names.append("editorial-package")
            reasons["editorial-package"] = (
                "The request also requires a reviewed editorial package before publication."
            )
        if visual:
            explicit_names.append("visual-storytelling")
            reasons["visual-storytelling"] = (
                "The request also requires a matched public-safe visual."
            )
        if publish_linkedin:
            explicit_names.append("linkedin-publishing")
            reasons["linkedin-publishing"] = (
                "The request asks to publish an approved LinkedIn artifact."
            )
            primary_name = "linkedin-publishing"
        if publish_x:
            explicit_names.append("x-thread-publishing")
            reasons["x-thread-publishing"] = (
                "The request asks to publish an approved X Thread."
            )
            if primary_name == "task-intake-and-routing":
                primary_name = "x-thread-publishing"
    elif resume:
        explicit_names.append("resume-tailoring")
        reasons["resume-tailoring"] = (
            "The Career request requires the Candidate Profile trust boundary."
        )
        primary_name = "resume-tailoring"
        if learning:
            explicit_names.append("learning-gap-to-packet")
            reasons["learning-gap-to-packet"] = (
                "The requested Learning Gaps require a separate mastery-safe packet."
            )
    elif editorial or visual:
        if editorial:
            explicit_names.append("editorial-package")
            reasons["editorial-package"] = (
                "The request requires Writer, fresh Reviewer, and controlled Reviser provenance."
            )
            primary_name = "editorial-package"
        if visual:
            explicit_names.append("visual-storytelling")
            reasons["visual-storytelling"] = (
                "The requested visual must be matched to the reviewed story."
            )
            if not editorial:
                primary_name = "visual-storytelling"
    elif learning:
        explicit_names.append("learning-gap-to-packet")
        reasons["learning-gap-to-packet"] = (
            "The request requires a mastery-safe staged learning packet."
        )
        primary_name = "learning-gap-to-packet"
    elif distillation:
        explicit_names.append("skill-distillation")
        reasons["skill-distillation"] = (
            "The request asks for an auditable Skill change decision."
        )
        primary_name = "skill-distillation"
    elif evidence:
        explicit_names.append("evidence-refresh-and-bundle")
        reasons["evidence-refresh-and-bundle"] = (
            "The request asks for a bounded evidence refresh or bundle."
        )
        primary_name = "evidence-refresh-and-bundle"

    selected_names = _dependency_order(available, explicit_names)
    for name in selected_names:
        reasons.setdefault(name, "Required registered dependency for the selected workflow.")
    selected_entries = [available.get(name) for name in selected_names]
    selected_by_name = {
        entry.name: _selected_skill(entry, reasons[entry.name]) for entry in selected_entries
    }
    task = _normalize_request(request_text, selected_names)

    human_gates = list(
        dict.fromkeys(gate for entry in selected_entries for gate in entry.human_gates)
    )
    if task.paid_api_intent:
        human_gates.append("Approve paid API usage and cost boundary")
    if task.deployment_intent:
        human_gates.append("Approve production deployment")
    if task.application_submission_intent:
        human_gates.append("Approve job application submission")
    human_gates = list(dict.fromkeys(human_gates))

    unmet_preconditions: list[str] = []
    if "resume-tailoring" in selected_names and (
        "Operator-approved Candidate Profile" not in task.available_inputs
    ):
        unmet_preconditions.append("Operator-approved Candidate Profile")
    if publication and not _contains(
        lowered,
        "approved",
        "human-reviewed",
        "final package",
        "已批准",
        "已审核",
    ):
        unmet_preconditions.append("Approved final platform artifact")
    if "skill-distillation" in selected_names and not (
        _contains(lowered, "approved run", "human-approved run", "已批准 run")
        and _contains(lowered, "receipt", "回执")
    ):
        unmet_preconditions.append("Completed human-approved Run Receipt")

    expected_receipts = ["Private Skill Run Receipt"]
    receipt_by_skill = {
        "evidence-refresh-and-bundle": "EvidenceBundle receipt",
        "editorial-package": "Writer, Reviewer, and Reviser provenance",
        "visual-storytelling": "Visual receipt",
        "linkedin-publishing": "LinkedIn Publication Receipt after PUBLISH",
        "x-thread-publishing": "Per-post and final X Thread Receipts after PUBLISH",
        "resume-tailoring": "Application package receipt",
        "learning-gap-to-packet": "Learning receipts",
        "skill-distillation": "Skill change proposal",
    }
    expected_receipts.extend(
        receipt_by_skill[name] for name in selected_names if name in receipt_by_skill
    )

    return SkillTaskRoute(
        task=task,
        primary_skill=selected_by_name[primary_name],
        supporting_skills=[
            selected_by_name[name] for name in selected_names if name != primary_name
        ],
        dependency_order=selected_names,
        model_route=_choose_model_route(selected_entries),
        human_gates=human_gates,
        unmet_preconditions=unmet_preconditions,
        expected_receipts=expected_receipts,
        routing_reason=[f"{name}: {reasons[name]}" for name in selected_names],
    )


def render_skill_route(route: SkillTaskRoute) -> str:
    model_lines = "\n".join(
        f"- {phase.phase}: {phase.recommended}" for phase in route.model_route
    )
    gate_items = [*route.human_gates]
    gate_items.extend(
        f"BLOCKING PRECONDITION: {precondition}"
        for precondition in route.unmet_preconditions
    )
    gate_lines = "\n".join(f"- {gate}" for gate in gate_items) or "- NONE"
    output_lines = "\n".join(
        f"- {artifact}" for artifact in route.task.requested_artifacts
    )
    selected = {
        skill.name: skill for skill in [route.primary_skill, *route.supporting_skills]
    }
    skills = "\n".join(
        f"{index}. {selected[name].name}@{selected[name].version} "
        f"[{selected[name].status}]"
        for index, name in enumerate(route.dependency_order, start=1)
    )
    return (
        f"TASK:\n{route.task.objective}\n\n"
        f"SKILL ROUTE:\n{skills}\n\n"
        f"MODEL ROUTE:\n{model_lines}\n\n"
        f"HUMAN GATES:\n{gate_lines}\n\n"
        f"EXPECTED OUTPUT:\n{output_lines}\n"
    )


def _git_worktree_root(path: Path) -> Path | None:
    candidate = path.expanduser().absolute()
    if not candidate.is_dir():
        candidate = candidate.parent
    for ancestor in (candidate, *candidate.parents):
        if (ancestor / ".git").exists():
            return ancestor
    return None


def _git_ignores_path(worktree: Path, data_root: Path) -> bool:
    git = shutil.which("git")
    if git is None:
        return False
    try:
        result = subprocess.run(
            [
                git,
                "-C",
                str(worktree),
                "check-ignore",
                "--quiet",
                "--no-index",
                "--",
                str(data_root / ".skill-os-private-probe"),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _validate_private_data_root(data_root: Path) -> None:
    root = data_root.expanduser().absolute()
    worktree = _git_worktree_root(root)
    if worktree is None:
        return
    if ".soloscale" not in root.relative_to(worktree).parts:
        raise SkillOSError(
            "Skill data inside a Git worktree must stay under an ignored .soloscale directory"
        )
    if not _git_ignores_path(worktree, root):
        raise SkillOSError("the selected .soloscale Skill data root is not ignored by Git")


def _ensure_private_directories(data_root: Path) -> dict[str, Path]:
    root = data_root.expanduser().absolute()
    if any(candidate.is_symlink() for candidate in (root, *root.parents)):
        raise SkillOSError("private Skill storage ancestry cannot contain a symlink")
    paths = {
        "root": root / "skills",
        "runs": root / "skills" / "runs",
        "receipts": root / "skills" / "receipts",
        "evaluations": root / "skills" / "evaluations",
        "candidate_updates": root / "skills" / "candidate-updates",
    }
    try:
        for path in paths.values():
            if any(candidate.is_symlink() for candidate in (path, *path.parents)):
                raise SkillOSError("private Skill storage ancestry cannot contain a symlink")
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
    except OSError as exc:
        raise SkillOSError("private Skill storage could not be prepared") from exc
    return paths


def _repository_state(start: Path) -> list[RepositoryRunState]:
    git = shutil.which("git")
    if git is None:
        return []

    def inspect(*arguments: str) -> str | None:
        try:
            result = subprocess.run(
                [git, "-C", str(start), *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        value = result.stdout.strip()
        return value if result.returncode == 0 and value else None

    root = inspect("rev-parse", "--show-toplevel")
    branch = inspect("branch", "--show-current")
    sha = inspect("rev-parse", "HEAD")
    if root is None or branch is None or sha is None:
        return []
    return [RepositoryRunState(repository=root, branch=branch, sha=sha)]


def persist_route_receipt(
    route: SkillTaskRoute,
    *,
    data_root: Path,
) -> tuple[SkillRunReceipt, Path]:
    """Persist a route-only receipt without claiming workflow or artifact completion."""

    _validate_private_data_root(data_root)
    paths = _ensure_private_directories(data_root)
    now = datetime.now(UTC)
    receipt_id = f"skill-run-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    route_json = route.model_dump_json(indent=2) + "\n"
    repositories = _repository_state(Path.cwd())
    commands = ["soloscale skill-route"]
    if repositories:
        commands.extend(
            [
                "git rev-parse --show-toplevel",
                "git branch --show-current",
                "git rev-parse HEAD",
            ]
        )
    selected = {
        skill.name: skill for skill in [route.primary_skill, *route.supporting_skills]
    }
    immediate_human_gate = bool(route.unmet_preconditions) or any(
        (
            route.task.publication_intent,
            route.task.paid_api_intent,
            route.task.deployment_intent,
            route.task.application_submission_intent,
        )
    )
    receipt = SkillRunReceipt(
        receipt_id=receipt_id,
        task_id=route.task.task_id,
        normalized_task_envelope=route.task,
        selected_skills=[selected[name] for name in route.dependency_order],
        routing_reason=route.routing_reason,
        phase_routes=route.model_route,
        tools_and_commands=commands,
        repositories=repositories,
        evidence_bundle_ids=[],
        input_hashes={"operator_request": route.task.request_sha256},
        output_artifacts=[
            ArtifactHash(
                artifact_id="skill-route",
                sha256=hashlib.sha256(route_json.encode("utf-8")).hexdigest(),
            )
        ],
        deterministic_checks=[
            DeterministicCheck(name="Skill registry contract", status="PASSED"),
            DeterministicCheck(name="Task Envelope normalization", status="PASSED"),
            DeterministicCheck(name="Skill dependency route", status="PASSED"),
        ],
        human_gates=route.human_gates,
        approvals=[],
        failures=[],
        retries=0,
        final_status=(
            SkillRunStatus.AWAITING_HUMAN_GATE
            if immediate_human_gate
            else SkillRunStatus.ROUTED
        ),
        started_at=now,
        completed_at=now,
        external_outcome_ids=[],
        outcome_state=RunOutcomeState(),
    )
    receipt_path = paths["receipts"] / f"{receipt_id}.json"
    try:
        run_dir = paths["runs"] / receipt_id
        run_dir.mkdir(mode=0o700)
        run_dir.chmod(0o700)
        write_private_once(
            run_dir / "route.json",
            route_json,
        )
        write_private_once(
            receipt_path,
            receipt.model_dump_json(indent=2) + "\n",
        )
    except (OSError, PrivateWriteError, ValueError) as exc:
        raise SkillOSError("private Skill receipt could not be persisted") from exc
    return receipt, receipt_path
