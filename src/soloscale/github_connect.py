"""Read-only GitHub App connection, selection, and metadata Evidence projection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator

from soloscale.evidence_hub_models import (
    EvidenceItem,
    SourceRecord,
    TruthClass,
)
from soloscale.models import ContractModel

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_API_ROOT = "https://api.github.com"
_API_VERSION = "2022-11-28"
_MAX_REPOSITORIES = 500
_MAX_SELECTED_REPOSITORIES = 20
_COMMIT_FILE_RE = re.compile(
    r"(?i)(?:^|\s)[^\s/\\]+\.(?:c|cc|cpp|css|go|h|hpp|html|java|js|jsx|json|md|mjs|py|rs|sh|sql|swift|toml|ts|tsx|xml|ya?ml)(?:$|[\s,:;])"
)
_COMMIT_CODE_MARKERS = ("`", "@@", "diff --git", "+++", "---", "=>", "::")
_MAX_COMMITS = 12
_MAX_PULL_REQUESTS = 10
_MAX_ISSUES = 10
_MAX_WORKFLOW_RUNS = 10
_TEXT_LIMIT = 300
_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubConnectError(ValueError):
    """A sanitized failure at the read-only GitHub boundary."""


class GitHubRepository(ContractModel):
    repository_id: int = Field(ge=1)
    full_name: str = Field(min_length=3, max_length=201)
    private: bool
    default_branch: str = Field(min_length=1, max_length=255)
    html_url: str = Field(min_length=19, max_length=500)
    updated_at: datetime | None = None

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, value: str) -> str:
        if _FULL_NAME_RE.fullmatch(value) is None:
            raise ValueError("GitHub repository name is invalid")
        return value

    @field_validator("html_url")
    @classmethod
    def validate_html_url(cls, value: str) -> str:
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("GitHub repository URL is invalid")
        return value


class GitHubConnectionState(ContractModel):
    account_id: int = Field(ge=1)
    account_login: str = Field(min_length=1, max_length=100)
    repositories: list[GitHubRepository] = Field(default_factory=list)
    selected_repository_ids: list[int] = Field(default_factory=list)
    inventory_refreshed_at: datetime
    evidence_refreshed_at: datetime | None = None
    evidence_receipt_id: str | None = None
    last_error_code: str | None = None

    @model_validator(mode="after")
    def validate_selection(self) -> GitHubConnectionState:
        repository_ids = [item.repository_id for item in self.repositories]
        if len(repository_ids) != len(set(repository_ids)):
            raise ValueError("GitHub repository inventory contains duplicates")
        if len(self.selected_repository_ids) != len(set(self.selected_repository_ids)):
            raise ValueError("GitHub repository selection contains duplicates")
        if len(self.selected_repository_ids) > _MAX_SELECTED_REPOSITORIES:
            raise ValueError("Too many GitHub repositories were selected")
        if not set(self.selected_repository_ids).issubset(repository_ids):
            raise ValueError("GitHub repository selection is outside the inventory")
        return self

    @property
    def selected_repositories(self) -> list[GitHubRepository]:
        selected = set(self.selected_repository_ids)
        return [item for item in self.repositories if item.repository_id in selected]


class GitHubConnectionStore:
    """Persist non-secret account and selection metadata under the private data root."""

    def __init__(self, data_root: Path) -> None:
        self.root = Path(data_root) / "github"
        self.path = self.root / "connection.json"

    def load(self) -> GitHubConnectionState | None:
        if not self.path.exists():
            return None
        self._validate_paths()
        try:
            return GitHubConnectionState.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise GitHubConnectError("Saved GitHub connection state is invalid") from exc

    def save_inventory(
        self,
        *,
        account_id: int,
        account_login: str,
        repositories: Sequence[GitHubRepository],
    ) -> GitHubConnectionState:
        existing = self.load()
        repository_list = list(repositories)
        available_ids = {item.repository_id for item in repository_list}
        selected_ids = (
            [
                repository_id
                for repository_id in existing.selected_repository_ids
                if repository_id in available_ids
            ]
            if existing is not None and existing.account_id == account_id
            else []
        )
        state = GitHubConnectionState(
            account_id=account_id,
            account_login=account_login,
            repositories=repository_list,
            selected_repository_ids=selected_ids,
            inventory_refreshed_at=_now(),
            evidence_refreshed_at=(
                existing.evidence_refreshed_at
                if existing is not None and existing.account_id == account_id
                else None
            ),
            evidence_receipt_id=(
                existing.evidence_receipt_id
                if existing is not None and existing.account_id == account_id
                else None
            ),
        )
        self._write(state)
        return state

    def save_selection(self, repository_ids: Sequence[int]) -> GitHubConnectionState:
        state = self.load()
        if state is None:
            raise GitHubConnectError("Refresh the GitHub repository list first")
        updated = state.model_copy(
            update={
                "selected_repository_ids": list(dict.fromkeys(repository_ids)),
                "evidence_refreshed_at": None,
                "evidence_receipt_id": None,
                "last_error_code": None,
            }
        )
        updated = GitHubConnectionState.model_validate(updated.model_dump())
        self._write(updated)
        return updated

    def mark_evidence_refresh(
        self, *, receipt_id: str | None, error_code: str | None = None
    ) -> GitHubConnectionState:
        state = self.load()
        if state is None:
            raise GitHubConnectError("GitHub is not connected")
        updated = state.model_copy(
            update={
                "evidence_refreshed_at": _now() if error_code is None else None,
                "evidence_receipt_id": receipt_id if error_code is None else None,
                "last_error_code": error_code,
            }
        )
        updated = GitHubConnectionState.model_validate(updated.model_dump())
        self._write(updated)
        return updated

    def clear(self) -> None:
        self._validate_paths(allow_missing=True)
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise GitHubConnectError("GitHub connection state could not be removed") from exc

    def _validate_paths(self, *, allow_missing: bool = False) -> None:
        absolute_root = self.root.expanduser().absolute()
        if any(candidate.is_symlink() for candidate in (absolute_root, *absolute_root.parents)):
            raise GitHubConnectError("GitHub storage paths must not contain symlinks")
        if not allow_missing and not self.path.is_file():
            raise GitHubConnectError("Saved GitHub connection state is unavailable")
        if self.path.exists() and self.path.is_symlink():
            raise GitHubConnectError("Saved GitHub connection state is invalid")

    def _write(self, state: GitHubConnectionState) -> None:
        self._validate_paths(allow_missing=True)
        self.root.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
        if stat.S_IMODE(self.root.stat().st_mode) != _DIRECTORY_MODE:
            self.root.chmod(_DIRECTORY_MODE)
        temporary = self.root / f".connection-{os.getpid()}-{os.urandom(4).hex()}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                _FILE_MODE,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(state.model_dump_json(indent=2))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            if stat.S_IMODE(self.path.stat().st_mode) != _FILE_MODE:
                self.path.chmod(_FILE_MODE)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise GitHubConnectError("GitHub connection state could not be saved") from exc


GitHubTransport = Callable[[str], tuple[int, Mapping[str, str], bytes]]


class GitHubReadOnlyClient:
    """A bounded GET-only client for one GitHub App user access token."""

    def __init__(
        self,
        access_token: str,
        *,
        transport: GitHubTransport | None = None,
    ) -> None:
        if not access_token or access_token != access_token.strip():
            raise GitHubConnectError("GitHub access token is invalid")
        self._access_token = access_token
        self._transport = transport or self._get

    def discover(self) -> tuple[int, str, list[GitHubRepository]]:
        account_payload, _headers = self._json("/user")
        if not isinstance(account_payload, dict):
            raise GitHubConnectError("GitHub account response is invalid")
        account_id = _positive_int(account_payload.get("id"), "account")
        account_login = _text(account_payload.get("login"), "account login", 100)
        repositories: list[GitHubRepository] = []
        page = 1
        while len(repositories) < _MAX_REPOSITORIES:
            payload, headers = self._json(
                "/user/repos",
                query={
                    "affiliation": "owner,collaborator,organization_member",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": "100",
                    "page": str(page),
                },
            )
            if not isinstance(payload, list):
                raise GitHubConnectError("GitHub repository response is invalid")
            repositories.extend(_repository(item) for item in payload)
            if 'rel="next"' not in headers.get("link", "").casefold() or not payload:
                break
            page += 1
            if page > 5:
                break
        unique = {item.repository_id: item for item in repositories[:_MAX_REPOSITORIES]}
        return account_id, account_login, list(unique.values())

    def evidence_snapshot(
        self,
        *,
        account_id: int,
        account_login: str,
        repositories: Sequence[GitHubRepository],
    ) -> tuple[SourceRecord, list[EvidenceItem]]:
        selected = list(repositories)
        if not selected:
            raise GitHubConnectError("Select at least one GitHub repository")
        if len(selected) > _MAX_SELECTED_REPOSITORIES:
            raise GitHubConnectError("Too many GitHub repositories were selected")
        source_id = _stable_id("source", "github", str(account_id))
        captured_at = _now()
        item_specs: list[dict[str, Any]] = []
        latest_source_at: datetime | None = None
        for repository in selected:
            owner, name = repository.full_name.split("/", 1)
            encoded_repo = "/".join(
                (urllib.parse.quote(owner, safe=""), urllib.parse.quote(name, safe=""))
            )
            repo_payload, _headers = self._json(f"/repos/{encoded_repo}")
            if not isinstance(repo_payload, dict):
                raise GitHubConnectError("GitHub repository metadata is invalid")
            repo_updated = _optional_datetime(repo_payload.get("updated_at"))
            latest_source_at = _latest(latest_source_at, repo_updated)
            item_specs.append(
                _item_spec(
                    kind="github_repository",
                    native_id=str(repository.repository_id),
                    project=repository.full_name,
                    source_at=repo_updated,
                    summary=f"GitHub repository metadata: {repository.full_name}",
                    relationships=[f"github_repository:{repository.repository_id}"],
                    verification={
                        "repository_id": str(repository.repository_id),
                        "default_branch": repository.default_branch,
                        "visibility": "private" if repository.private else "public",
                    },
                )
            )
            commits, _headers = self._json(
                f"/repos/{encoded_repo}/commits",
                query={"per_page": str(_MAX_COMMITS)},
                accepted_statuses=(200, 409),
            )
            if isinstance(commits, list):
                for commit in commits[:_MAX_COMMITS]:
                    if not isinstance(commit, dict):
                        continue
                    sha = _text(commit.get("sha"), "commit SHA", 64)
                    commit_block = commit.get("commit")
                    if not isinstance(commit_block, dict):
                        continue
                    committed_at = _nested_datetime(commit_block, "committer", "date")
                    message = _first_line(commit_block.get("message"))
                    if not is_resume_safe_commit_summary(message):
                        continue
                    latest_source_at = _latest(latest_source_at, committed_at)
                    item_specs.append(
                        _item_spec(
                            kind="github_commit",
                            native_id=sha,
                            project=repository.full_name,
                            source_at=committed_at,
                            summary=f"GitHub commit: {message}",
                            relationships=[
                                f"github_repository:{repository.repository_id}",
                                f"commit:{sha}",
                            ],
                            verification={"commit_sha": sha},
                        )
                    )
            pulls, _headers = self._json(
                f"/repos/{encoded_repo}/pulls",
                query={
                    "state": "all",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": str(_MAX_PULL_REQUESTS),
                },
            )
            if isinstance(pulls, list):
                for pull in pulls[:_MAX_PULL_REQUESTS]:
                    spec = _numbered_item_spec(
                        pull,
                        kind="github_pull_request",
                        label="GitHub pull request",
                        repository=repository,
                    )
                    if spec is not None:
                        item_specs.append(spec)
                        latest_source_at = _latest(latest_source_at, spec["source_at"])
            issues, _headers = self._json(
                f"/repos/{encoded_repo}/issues",
                query={
                    "state": "all",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": str(_MAX_ISSUES),
                },
            )
            if isinstance(issues, list):
                for issue in issues[:_MAX_ISSUES]:
                    if isinstance(issue, dict) and "pull_request" in issue:
                        continue
                    spec = _numbered_item_spec(
                        issue,
                        kind="github_issue",
                        label="GitHub issue",
                        repository=repository,
                    )
                    if spec is not None:
                        item_specs.append(spec)
                        latest_source_at = _latest(latest_source_at, spec["source_at"])
            workflows, _headers = self._json(
                f"/repos/{encoded_repo}/actions/runs",
                query={"per_page": str(_MAX_WORKFLOW_RUNS)},
            )
            workflow_runs = (
                workflows.get("workflow_runs", [])
                if isinstance(workflows, dict)
                else []
            )
            if isinstance(workflow_runs, list):
                for run in workflow_runs[:_MAX_WORKFLOW_RUNS]:
                    if not isinstance(run, dict):
                        continue
                    run_id = _positive_int(run.get("id"), "workflow run")
                    updated_at = _optional_datetime(run.get("updated_at"))
                    name_value = _text(run.get("name") or "Workflow", "workflow name", _TEXT_LIMIT)
                    conclusion = _optional_text(run.get("conclusion"), 40) or "unknown"
                    latest_source_at = _latest(latest_source_at, updated_at)
                    item_specs.append(
                        _item_spec(
                            kind="github_workflow_run",
                            native_id=str(run_id),
                            project=repository.full_name,
                            source_at=updated_at,
                            summary=f"GitHub workflow: {name_value} · {conclusion}",
                            relationships=[
                                f"github_repository:{repository.repository_id}",
                                f"workflow_run:{run_id}",
                            ],
                            verification={
                                "workflow_run_id": str(run_id),
                                "conclusion": conclusion,
                            },
                        )
                    )
        selection_hash = _sha256(
            {
                "account_id": account_id,
                "repositories": sorted(item.repository_id for item in selected),
                "items": sorted(spec["content_sha256"] for spec in item_specs),
            }
        )
        source = SourceRecord(
            source_id=source_id,
            native_id=f"github-account:{account_id}",
            source_system="github",
            source_type="selected_repository_snapshot",
            project=None,
            original_locator=f"https://github.com/{account_login}",
            captured_at=captured_at,
            source_at=latest_source_at,
            content_sha256=selection_hash,
            sensitivity="private",
            truth_class=TruthClass.PERSONAL_ARTIFACT,
            raw_available=False,
            adapter="github_rest_metadata_v0_1",
            metadata={
                "account_id": str(account_id),
                "account_login": account_login,
                "selected_repository_count": str(len(selected)),
                "selected_repositories_sha256": _sha256(
                    sorted(item.full_name for item in selected)
                ),
                "permission_boundary": "read_only_github_app_selected_repositories",
            },
        )
        items = [
            EvidenceItem(
                evidence_id=_stable_id(
                    "evidence", source_id, spec["kind"], spec["native_id"]
                ),
                source_id=source_id,
                native_id=spec["native_id"],
                evidence_type=spec["kind"],
                project=spec["project"],
                captured_at=captured_at,
                source_at=spec["source_at"],
                time_start=spec["source_at"],
                time_end=spec["source_at"],
                provenance_locator=(
                    f"https://github.com/{spec['project']}"
                    if spec["project"]
                    else source.original_locator
                ),
                truth_class=TruthClass.PERSONAL_ARTIFACT,
                trust_state="github_api_observed",
                public_safe_summary=spec["summary"],
                relationships=spec["relationships"],
                verification=spec["verification"],
                verification_status="github_api_observed_not_role_verified",
                content_sha256=spec["content_sha256"],
            )
            for spec in item_specs
        ]
        return source, items

    def _json(
        self,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        accepted_statuses: Sequence[int] = (200,),
    ) -> tuple[Any, Mapping[str, str]]:
        if not path.startswith("/") or path.startswith("//"):
            raise GitHubConnectError("GitHub API path is invalid")
        url = f"{_API_ROOT}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        status, headers, body = self._transport(url)
        normalized_headers = {str(key).casefold(): str(value) for key, value in headers.items()}
        if status not in accepted_statuses:
            if status == 401:
                raise GitHubConnectError("GitHub authorization is no longer valid")
            if status == 403:
                raise GitHubConnectError("GitHub denied the requested read-only metadata")
            if status == 404:
                raise GitHubConnectError("A selected GitHub repository is unavailable")
            raise GitHubConnectError(f"GitHub read failed with HTTP {status}")
        if status == 409:
            return [], normalized_headers
        try:
            return json.loads(body.decode("utf-8")), normalized_headers
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GitHubConnectError("GitHub returned an invalid response") from exc

    def _get(self, url: str) -> tuple[int, Mapping[str, str], bytes]:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.github.com"
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise GitHubConnectError("GitHub API destination is invalid")
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._access_token}",
                "User-Agent": "SoloScale-AI-OS",
                "X-GitHub-Api-Version": _API_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return int(response.status), dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as exc:
            return int(exc.code), dict(exc.headers.items()), exc.read()
        except (OSError, urllib.error.URLError) as exc:
            raise GitHubConnectError("GitHub could not be reached") from exc


def _repository(payload: object) -> GitHubRepository:
    if not isinstance(payload, dict):
        raise GitHubConnectError("GitHub repository response is invalid")
    return GitHubRepository(
        repository_id=_positive_int(payload.get("id"), "repository"),
        full_name=_text(payload.get("full_name"), "repository name", 201),
        private=bool(payload.get("private", False)),
        default_branch=_text(payload.get("default_branch") or "main", "default branch", 255),
        html_url=_text(payload.get("html_url"), "repository URL", 500),
        updated_at=_optional_datetime(payload.get("updated_at")),
    )


def _numbered_item_spec(
    payload: object,
    *,
    kind: str,
    label: str,
    repository: GitHubRepository,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    number = _positive_int(payload.get("number"), label)
    title = _text(payload.get("title"), f"{label} title", _TEXT_LIMIT)
    updated_at = _optional_datetime(payload.get("updated_at"))
    return _item_spec(
        kind=kind,
        native_id=f"{repository.repository_id}:{number}",
        project=repository.full_name,
        source_at=updated_at,
        summary=f"{label} #{number}: {title}",
        relationships=[
            f"github_repository:{repository.repository_id}",
            f"number:{number}",
        ],
        verification={"number": str(number)},
    )


def _item_spec(
    *,
    kind: str,
    native_id: str,
    project: str,
    source_at: datetime | None,
    summary: str,
    relationships: list[str],
    verification: dict[str, str],
) -> dict[str, Any]:
    normalized_summary = " ".join(summary.split())[:_TEXT_LIMIT]
    return {
        "kind": kind,
        "native_id": native_id,
        "project": project,
        "source_at": source_at,
        "summary": normalized_summary,
        "relationships": relationships,
        "verification": verification,
        "content_sha256": _sha256(
            {
                "kind": kind,
                "native_id": native_id,
                "project": project,
                "source_at": source_at.isoformat() if source_at else None,
                "summary": normalized_summary,
                "verification": verification,
            }
        ),
    }


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise GitHubConnectError(f"GitHub {label} response is invalid")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isdigit():
        parsed = int(value)
    else:
        raise GitHubConnectError(f"GitHub {label} response is invalid")
    if parsed <= 0:
        raise GitHubConnectError(f"GitHub {label} response is invalid")
    return parsed


def _text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise GitHubConnectError(f"GitHub {label} response is invalid")
    normalized = " ".join(value.split())[:limit]
    if not normalized:
        raise GitHubConnectError(f"GitHub {label} response is invalid")
    return normalized


def _optional_text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())[:limit]
    return normalized or None


def _first_line(value: object) -> str:
    if not isinstance(value, str):
        raise GitHubConnectError("GitHub commit message is invalid")
    return _text(value.splitlines()[0] if value.splitlines() else "", "commit message", _TEXT_LIMIT)


def is_resume_safe_commit_summary(value: str) -> bool:
    """Reject commit subjects that expose paths, filenames, code, or diff markers."""

    normalized = " ".join(value.split())
    lowered = normalized.casefold()
    return bool(normalized) and not (
        "/" in normalized
        or "\\" in normalized
        or _COMMIT_FILE_RE.search(normalized)
        or any(marker in lowered for marker in _COMMIT_CODE_MARKERS)
    )


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GitHubConnectError("GitHub timestamp is invalid") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _nested_datetime(payload: Mapping[str, object], *keys: str) -> datetime | None:
    current: object = payload
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return _optional_datetime(current)


def _latest(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _now() -> datetime:
    return datetime.now(UTC)


def _sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:24]
    return f"{parts[0]}-{digest}"
