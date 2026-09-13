"""Read-only full-Mac evidence discovery with a durable incremental catalog.

This module is the upstream half of the Resume Intelligence "high recall" boundary. It
walks user-owned directories, classifies candidates before expensive indexing, records
Git repository identity where present, and persists a private catalog that can be
incrementally rescanned without re-hashing unchanged files.

Discovery is deliberately read-only: it never writes to, renames, or deletes a discovered
user file. The only side effect is the private catalog under the SoloScale data root.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

_CATALOG_SCHEMA_VERSION = "1.0"
_CATALOG_RELATIVE_PATH = Path("work") / "mac-discovery" / "catalog.json"
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_GIT_TIMEOUT_SECONDS = 10
_HASH_CHUNK_BYTES = 64 * 1024
_SECRET_SNIFF_BYTES = 64 * 1024
_DEFAULT_MAX_FILES = 100_000

_INDEX_EXTENSION_KINDS = {
    ".py": "code",
    ".pyw": "code",
    ".ts": "code",
    ".tsx": "code",
    ".js": "code",
    ".jsx": "code",
    ".mjs": "code",
    ".cjs": "code",
    ".swift": "code",
    ".m": "code",
    ".mm": "code",
    ".rs": "code",
    ".go": "code",
    ".java": "code",
    ".kt": "code",
    ".kts": "code",
    ".rb": "code",
    ".php": "code",
    ".c": "code",
    ".h": "code",
    ".cpp": "code",
    ".hpp": "code",
    ".cc": "code",
    ".cs": "code",
    ".scala": "code",
    ".clj": "code",
    ".cljs": "code",
    ".ex": "code",
    ".exs": "code",
    ".erl": "code",
    ".hrl": "code",
    ".hs": "code",
    ".lua": "code",
    ".pl": "code",
    ".r": "code",
    ".sql": "code",
    ".sh": "code",
    ".bash": "code",
    ".zsh": "code",
    ".fish": "code",
    ".ps1": "code",
    ".vue": "code",
    ".svelte": "code",
    ".dart": "code",
    ".groovy": "code",
    ".proto": "code",
    ".graphql": "code",
    ".md": "project_document",
    ".markdown": "project_document",
    ".rst": "project_document",
    ".adoc": "project_document",
    ".asciidoc": "project_document",
    ".org": "note",
    ".txt": "note",
    ".text": "note",
    ".ipynb": "note",
    ".json": "project_document",
    ".yaml": "project_document",
    ".yml": "project_document",
    ".toml": "project_document",
    ".ini": "project_document",
    ".cfg": "project_document",
    ".conf": "project_document",
    ".xml": "project_document",
    ".html": "project_document",
    ".htm": "project_document",
    ".csv": "project_document",
    ".tsv": "project_document",
    ".pdf": "project_document",
    ".docx": "project_document",
    ".pptx": "project_document",
    ".xlsx": "project_document",
}

_NOISE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".o",
    ".a",
    ".lib",
    ".class",
    ".jar",
    ".war",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".dmg",
    ".iso",
    ".pkg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".tiff",
    ".ico",
    ".icns",
    ".webp",
    ".heic",
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
    ".webm",
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".map",
    ".lock",
}

_NOISE_DIRECTORIES = {
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".gradle",
    ".tox",
    ".nox",
    ".cache",
    "Cache",
    "Caches",
    "DerivedData",
    ".next",
    ".nuxt",
    ".output",
    ".turbo",
    "coverage",
    ".eggs",
    "site-packages",
    "dist",
    "build",
    "target",
    ".cargo",
    ".npm",
    ".yarn",
    "bower_components",
    ".bundle",
    "vendor",
    "Pods",
    "Frameworks",
    ".dart_tool",
    ".pio",
    ".build",
    "Carthage",
    ".swiftpm",
    ".m2",
    ".ivy2",
    ".pub-cache",
    ".pnpm-store",
    ".rustup",
    ".nvm",
    ".pyenv",
    ".sdkman",
    ".jenv",
    ".rbenv",
    ".rvm",
    "SDKs",
    "Library",
    "Applications",
    ".Trash",
    ".soloscale",
    "mac-discovery",
    "Movies",
    "Music",
    "Pictures",
    "Public",
    "Sites",
    "Safari",
    "Google",
    "Mozilla",
    "Firefox",
    "Chrome",
    "Chromium",
    "Edge",
    "Brave",
    "Arc",
    "Containers",
    "Group Containers",
    "WebKit",
    "Metadata",
    "Mobile Documents",
    "Keychains",
    "Cookies",
    "Application Scripts",
    "Saved Application State",
}

_NOISE_FILE_NAMES = {
    ".DS_Store",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "poetry.lock",
    "Pipfile.lock",
}

_SECRET_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".npmrc",
    ".pypirc",
    ".htpasswd",
    "credentials.json",
    "client_secret.json",
}

_SECRET_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".keychain"}

_SECRET_DIRECTORY_NAMES = {"credentials", "client_secrets", "secrets"}

_SECRET_ID_FILE = re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)(\..*)?$")
_PRIVATE_KEY_MARKER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(api_?key|client_secret|access_token|refresh_token|auth_token"
    r"|aws_secret_access_key|openai_api_key|anthropic_api_key|deepseek_api_key)"
    r"\s*[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_\-./+]{8,}"
)

_BUILD_CI_FILE_NAMES = {
    "dockerfile",
    "makefile",
    "jenkinsfile",
    "rakefile",
    "cmakelists.txt",
    "azure-pipelines.yml",
    "gitlab-ci.yml",
}

_APP_BUNDLE_DIRECTORY = re.compile(r"\.app(\..*)?$")
_SYSTEM_ROOT_PARTS = {"/System", "/usr", "/bin", "/sbin", "/Library", "/Applications"}


class MacDiscoveryError(Exception):
    """A safe error at the Mac discovery boundary."""


@dataclass(frozen=True)
class Classification:
    kind: str
    disposition: Literal["index", "ignore"]
    reason: str


@dataclass(frozen=True)
class DiscoveryEntry:
    entry_id: str
    record_type: Literal["repository", "file", "source"]
    path: str
    kind: str
    fingerprint: str
    size: int
    mtime_ns: int
    ctime_ns: int
    git_root: str | None
    git_head: str | None
    git_branch: str | None
    git_remote: str | None
    indexed_at: str


@dataclass(frozen=True)
class DiscoveryReport:
    mode: Literal["incremental", "full"]
    roots: tuple[str, ...]
    excluded_roots: tuple[str, ...]
    started_at: str
    completed_at: str
    repositories_found: int
    sources_found: int
    sources_by_kind: dict[str, int]
    files_scanned: int
    files_indexed: int
    files_new: int
    files_changed: int
    files_unchanged: int
    entries_removed: int
    noise_excluded: int
    system_excluded: int
    secrets_excluded: int
    unknown_ignored: int
    pruned_directories: int
    by_kind: dict[str, int]
    catalog_path: str


@dataclass(frozen=True)
class DiscoveryStatus:
    catalog_exists: bool
    updated_at: str | None
    repositories_found: int
    sources_found: int
    sources_by_kind: dict[str, int]
    files_indexed: int
    noise_excluded: int
    secrets_excluded: int
    by_kind: dict[str, int]


def discovery_catalog_path(data_root: Path) -> Path:
    return Path(data_root) / _CATALOG_RELATIVE_PATH


def default_discovery_roots(*, home: Path | None = None) -> tuple[Path, ...]:
    selected_home = (home or Path.home()).expanduser()
    return (selected_home,)


def _now_text() -> str:
    return datetime.now(UTC).isoformat()


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _dedupe_descendants(roots: tuple[Path, ...]) -> tuple[Path, ...]:
    resolved = sorted({root.resolve(strict=False) for root in roots}, key=lambda p: len(p.parts))
    kept: list[Path] = []
    for root in resolved:
        if any(root.is_relative_to(parent) and root != parent for parent in kept):
            continue
        kept.append(root)
    return tuple(kept)


def _normalized(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    normalized_path = _normalized(path)
    return any(normalized_path == root or normalized_path.is_relative_to(root) for root in roots)


def _is_excluded_path(path: Path, excluded: tuple[Path, ...]) -> bool:
    normalized_path = _normalized(path)
    return any(
        normalized_path == root or normalized_path.is_relative_to(root) for root in excluded
    )


def _git_text(root: Path, *args: str) -> str | None:
    command = ["git", "-C", str(root), *args]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _is_system_path(path: Path) -> bool:
    parts = {f"/{part}" for part in path.parts[1:]}
    return bool(parts & _SYSTEM_ROOT_PARTS)


def _is_secret_name(path: Path) -> bool:
    name = path.name
    lower = name.lower()
    if name in _SECRET_FILE_NAMES:
        return True
    if path.suffix.lower() in _SECRET_EXTENSIONS:
        return True
    if _SECRET_ID_FILE.fullmatch(name) is not None:
        return True
    if any(part.lower() in _SECRET_DIRECTORY_NAMES for part in path.parts):
        return True
    return "secret" in lower


def _is_noise_name(path: Path) -> bool:
    if path.name in _NOISE_FILE_NAMES:
        return True
    if path.name.endswith(".min.js") or path.name.endswith(".min.css"):
        return True
    return path.suffix.lower() in _NOISE_EXTENSIONS


def _is_noise_directory(path: Path) -> bool:
    if path.name in _NOISE_DIRECTORIES:
        return True
    lower = path.name.lower()
    if _APP_BUNDLE_DIRECTORY.search(lower) is not None:
        return True
    return any(
        lower.endswith(suffix)
        for suffix in (".app", ".localized", ".framework", ".bundle", ".dsym")
    )


def _is_hidden_directory(path: Path) -> bool:
    return path.name.startswith(".") and path.name not in {".", ".."}


def _allowlisted_sources(home: Path) -> tuple[tuple[Path, str], ...]:
    selected_home = (home or Path.home()).expanduser()
    candidates: list[tuple[Path, str]] = []
    codex_home = selected_home / ".codex"
    if codex_home.is_dir():
        candidates.append((codex_home, "codex_local"))
    for candidate in (
        selected_home / "Documents" / "SoloScaleData",
        selected_home / "Library" / "Application Support" / "SoloScale AI OS",
    ):
        if candidate.is_dir():
            candidates.append((candidate, "soloscale_local"))
    return tuple(candidates)


def _source_kind_for_directory(path: Path, home: Path) -> str | None:
    selected_home = (home or Path.home()).expanduser()
    if _normalized(path) == _normalized(selected_home / ".codex"):
        return "codex_local"
    if path.name in {"SoloScaleData", "SoloScale AI OS"}:
        return "soloscale_local"
    return None


def _shallow_counts(root: Path) -> tuple[int, int]:
    files = 0
    directories = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    directories += 1
                elif entry.is_file(follow_symlinks=False):
                    files += 1
    except OSError:
        pass
    return files, directories


def _classify_file(path: Path) -> Classification:
    name = path.name
    lower = name.lower()
    suffix = path.suffix.lower()

    if _is_secret_name(path):
        return Classification("secret_sensitive", "ignore", "secret filename")
    if _is_noise_name(path):
        return Classification("noise", "ignore", "noise extension or generated file")
    if _is_system_path(path):
        return Classification("system", "ignore", "system path")

    path_text = path.as_posix().lower()
    is_test = (
        "/tests/" in path_text
        or "/test/" in path_text
        or lower.startswith("test_")
        or lower.endswith("_test")
        or lower.endswith(".test")
        or lower.endswith(".spec")
    )
    if is_test:
        return Classification("test", "index", "test code")

    parts = {part.lower() for part in path.parts}
    adr_like = (
        lower.startswith("adr-")
        or lower.startswith("adr_")
        or "/adr/" in path_text
        or "/decisions/" in path_text
    )
    if adr_like:
        return Classification("design_adr", "index", "architecture decision record")

    if lower in _BUILD_CI_FILE_NAMES or ".github/workflows" in path.as_posix().lower():
        return Classification("build_ci", "index", "build or CI definition")

    if (
        "resume" in lower
        or "cv" in lower.split(".")
        or lower.startswith("cv.")
        or "cover letter" in lower
        or "application" in lower
    ) and suffix in {".pdf", ".docx", ".md", ".txt", ".json", ".yaml", ".yml"}:
        return Classification("resume_application", "index", "resume or application document")

    if "career" in parts and suffix in {
        ".md",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".pdf",
        ".docx",
    }:
        return Classification("career", "index", "career document")

    kind = _INDEX_EXTENSION_KINDS.get(suffix)
    if kind is not None:
        return Classification(kind, "index", "known document or code extension")

    return Classification("unknown", "ignore", "unclassified extension")


def _sniff_secret_sample(sample: bytes) -> bool:
    text = sample.decode("utf-8", errors="ignore")
    if _PRIVATE_KEY_MARKER.search(text) is not None:
        return True
    return _SECRET_ASSIGNMENT.search(text) is not None


def _hash_file(path: Path) -> tuple[str, bool]:
    digest = hashlib.sha256()
    secret = False
    first_chunk = True
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            if first_chunk:
                secret = _sniff_secret_sample(chunk[: _SECRET_SNIFF_BYTES])
                first_chunk = False
            digest.update(chunk)
    return digest.hexdigest(), secret


def _record_repository(root: Path, indexed_at: str) -> DiscoveryEntry:
    head = _git_text(root, "rev-parse", "--verify", "HEAD")
    branch = _git_text(root, "rev-parse", "--abbrev-ref", "HEAD")
    remote = _git_text(root, "remote", "get-url", "origin")
    path_stat = root.stat()
    return DiscoveryEntry(
        entry_id=_stable_id(str(_normalized(root))),
        record_type="repository",
        path=str(root),
        kind="engineering",
        fingerprint=head or "unborn",
        size=0,
        mtime_ns=path_stat.st_mtime_ns,
        ctime_ns=path_stat.st_ctime_ns,
        git_root=str(root),
        git_head=head,
        git_branch=branch,
        git_remote=remote,
        indexed_at=indexed_at,
    )


def _record_source(root: Path, kind: str, indexed_at: str) -> DiscoveryEntry:
    files_count, directories_count = _shallow_counts(root)
    path_stat = root.stat()
    fingerprint = _stable_id(
        f"{kind}:{files_count}:{directories_count}:{path_stat.st_mtime_ns}:"
        f"{path_stat.st_ctime_ns}"
    )
    return DiscoveryEntry(
        entry_id=_stable_id(str(_normalized(root))),
        record_type="source",
        path=str(root),
        kind=kind,
        fingerprint=fingerprint,
        size=0,
        mtime_ns=path_stat.st_mtime_ns,
        ctime_ns=path_stat.st_ctime_ns,
        git_root=None,
        git_head=None,
        git_branch=None,
        git_remote=None,
        indexed_at=indexed_at,
    )


def _record_file(
    path: Path,
    *,
    indexed_at: str,
    fingerprint: str,
    classification: Classification,
    path_stat: os.stat_result,
) -> DiscoveryEntry:
    return DiscoveryEntry(
        entry_id=_stable_id(str(_normalized(path))),
        record_type="file",
        path=str(path),
        kind=classification.kind,
        fingerprint=fingerprint,
        size=path_stat.st_size,
        mtime_ns=path_stat.st_mtime_ns,
        ctime_ns=path_stat.st_ctime_ns,
        git_root=None,
        git_head=None,
        git_branch=None,
        git_remote=None,
        indexed_at=indexed_at,
    )


def _entry_to_dict(entry: DiscoveryEntry) -> dict[str, object]:
    return {
        "entry_id": entry.entry_id,
        "record_type": entry.record_type,
        "path": entry.path,
        "kind": entry.kind,
        "fingerprint": entry.fingerprint,
        "size": entry.size,
        "mtime_ns": entry.mtime_ns,
        "ctime_ns": entry.ctime_ns,
        "git_root": entry.git_root,
        "git_head": entry.git_head,
        "git_branch": entry.git_branch,
        "git_remote": entry.git_remote,
        "indexed_at": entry.indexed_at,
    }


def _entry_from_dict(value: dict[str, object]) -> DiscoveryEntry:
    def text(key: str) -> str:
        item = value.get(key)
        if not isinstance(item, str):
            raise MacDiscoveryError(f"catalog entry field is invalid: {key}")
        return item

    def optional_text(key: str) -> str | None:
        item = value.get(key)
        return item if isinstance(item, str) else None

    def integer(key: str) -> int:
        item = value.get(key)
        if not isinstance(item, int):
            raise MacDiscoveryError(f"catalog entry field is invalid: {key}")
        return item

    record_type = text("record_type")
    if record_type not in {"repository", "file", "source"}:
        raise MacDiscoveryError("catalog entry record type is invalid")
    return DiscoveryEntry(
        entry_id=text("entry_id"),
        record_type=cast(Literal["repository", "file", "source"], record_type),
        path=text("path"),
        kind=text("kind"),
        fingerprint=text("fingerprint"),
        size=integer("size"),
        mtime_ns=integer("mtime_ns"),
        ctime_ns=integer("ctime_ns"),
        git_root=optional_text("git_root"),
        git_head=optional_text("git_head"),
        git_branch=optional_text("git_branch"),
        git_remote=optional_text("git_remote"),
        indexed_at=text("indexed_at"),
    )


def _load_catalog(data_root: Path) -> dict[str, DiscoveryEntry]:
    path = discovery_catalog_path(data_root)
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise MacDiscoveryError("Mac discovery catalog storage is unsafe.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MacDiscoveryError("Mac discovery catalog is unavailable.") from error
    if not isinstance(payload, dict):
        raise MacDiscoveryError("Mac discovery catalog is invalid.")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise MacDiscoveryError("Mac discovery catalog is invalid.")
    entries: dict[str, DiscoveryEntry] = {}
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise MacDiscoveryError("Mac discovery catalog is invalid.")
        entry = _entry_from_dict(raw)
        entries[entry.entry_id] = entry
    return entries


def _write_catalog(
    data_root: Path,
    *,
    roots: tuple[Path, ...],
    excluded_roots: tuple[Path, ...],
    updated_at: str,
    entries: dict[str, DiscoveryEntry],
    report: DiscoveryReport,
) -> Path:
    path = discovery_catalog_path(data_root)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=_DIRECTORY_MODE)
    if directory.is_symlink():
        raise MacDiscoveryError("Mac discovery catalog storage is unsafe.")
    os.chmod(directory, _DIRECTORY_MODE)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix="mac-discovery-", suffix=".tmp", dir=directory
    )
    temporary = Path(raw_temporary)
    try:
        os.chmod(temporary, _FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            summary = {
                "repositories_found": report.repositories_found,
                "sources_found": report.sources_found,
                "sources_by_kind": report.sources_by_kind,
                "files_indexed": report.files_indexed,
                "noise_excluded": report.noise_excluded,
                "system_excluded": report.system_excluded,
                "secrets_excluded": report.secrets_excluded,
                "unknown_ignored": report.unknown_ignored,
                "by_kind": report.by_kind,
            }
            payload = {
                "schema_version": _CATALOG_SCHEMA_VERSION,
                "roots": [str(root) for root in roots],
                "excluded_roots": [str(root) for root in excluded_roots],
                "updated_at": updated_at,
                "entries": [
                    _entry_to_dict(entry)
                    for entry in sorted(entries.values(), key=lambda item: item.entry_id)
                ],
                "last_summary": summary,
            }
            json.dump(payload, stream, ensure_ascii=True, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, _FILE_MODE)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def discover_mac_evidence(
    data_root: Path,
    *,
    roots: tuple[Path, ...] | list[Path] | None = None,
    extra_roots: tuple[Path, ...] | list[Path] | None = None,
    excluded_roots: tuple[Path, ...] | list[Path] | None = None,
    home: Path | None = None,
    full_rescan: bool = False,
    max_files: int = _DEFAULT_MAX_FILES,
) -> DiscoveryReport:
    started_at = _now_text()
    resolved_data_root = Path(data_root)
    selected_home = (home or Path.home()).expanduser()
    base_roots = tuple(roots) if roots else default_discovery_roots(home=selected_home)
    added_roots = tuple(extra_roots or ())
    selected_roots = _dedupe_descendants(base_roots + added_roots)
    excluded = tuple(
        Path(root).expanduser().resolve(strict=False) for root in (excluded_roots or ())
    )
    previous = _load_catalog(resolved_data_root) if not full_rescan else {}
    mode: Literal["incremental", "full"] = "full" if full_rescan else "incremental"

    entries: dict[str, DiscoveryEntry] = {}
    current_ids: set[str] = set()
    repositories_found = 0
    sources_by_kind: dict[str, int] = {}
    files_scanned = 0
    files_new = 0
    files_changed = 0
    files_unchanged = 0
    noise_excluded = 0
    system_excluded = 0
    secrets_excluded = 0
    unknown_ignored = 0
    pruned_directories = 0
    by_kind: dict[str, int] = {}
    stopped = False

    recorded_source_paths: set[Path] = set()
    for source_path, kind in _allowlisted_sources(selected_home):
        normalized_source = _normalized(source_path)
        if not _is_within(normalized_source, selected_roots):
            continue
        if _is_excluded_path(normalized_source, excluded):
            continue
        source_entry = _record_source(source_path, kind, started_at)
        entries[source_entry.entry_id] = source_entry
        current_ids.add(source_entry.entry_id)
        recorded_source_paths.add(normalized_source)
        sources_by_kind[kind] = sources_by_kind.get(kind, 0) + 1

    stack: list[tuple[Path, bool]] = [
        (root, False) for root in selected_roots if not _is_excluded_path(root, excluded)
    ]
    while stack:
        directory, in_repository = stack.pop()
        normalized_directory = _normalized(directory)
        if _is_excluded_path(normalized_directory, excluded):
            continue
        if normalized_directory in recorded_source_paths:
            continue
        source_kind = _source_kind_for_directory(directory, selected_home)
        if source_kind is not None:
            source_entry = _record_source(directory, source_kind, started_at)
            entries[source_entry.entry_id] = source_entry
            current_ids.add(source_entry.entry_id)
            recorded_source_paths.add(normalized_directory)
            sources_by_kind[source_kind] = sources_by_kind.get(source_kind, 0) + 1
            continue
        if (directory / ".git").exists():
            repository = _record_repository(directory, started_at)
            entries[repository.entry_id] = repository
            current_ids.add(repository.entry_id)
            repositories_found += 1
            in_repository = True
        elif _is_noise_directory(directory) or _is_hidden_directory(directory):
            pruned_directories += 1
            continue

        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            continue
        for child in children:
            if stopped:
                break
            if child.is_symlink():
                continue
            if child.is_dir(follow_symlinks=False):
                stack.append((Path(child.path), in_repository))
                continue
            if not child.is_file(follow_symlinks=False):
                continue
            if in_repository:
                continue
            files_scanned += 1
            if files_scanned > max_files:
                stopped = True
                break
            path = Path(child.path)
            classification = _classify_file(path)
            if classification.disposition == "ignore":
                if classification.kind == "noise":
                    noise_excluded += 1
                elif classification.kind == "system":
                    system_excluded += 1
                elif classification.kind == "secret_sensitive":
                    secrets_excluded += 1
                else:
                    unknown_ignored += 1
                continue

            entry_id = _stable_id(str(_normalized(path)))
            path_stat = path.stat()
            prior = previous.get(entry_id)
            should_hash = (
                full_rescan
                or prior is None
                or prior.size != path_stat.st_size
                or prior.mtime_ns != path_stat.st_mtime_ns
                or prior.ctime_ns != path_stat.st_ctime_ns
            )
            if prior is not None and not should_hash:
                entries[entry_id] = prior
                current_ids.add(entry_id)
                files_unchanged += 1
                by_kind[classification.kind] = by_kind.get(classification.kind, 0) + 1
                continue

            fingerprint, is_secret = _hash_file(path)
            if is_secret:
                secrets_excluded += 1
                continue
            entry = _record_file(
                path,
                indexed_at=started_at,
                fingerprint=fingerprint,
                classification=classification,
                path_stat=path_stat,
            )
            entries[entry_id] = entry
            current_ids.add(entry_id)
            if prior is None:
                files_new += 1
            elif prior.fingerprint != fingerprint:
                files_changed += 1
            else:
                files_unchanged += 1
            by_kind[entry.kind] = by_kind.get(entry.kind, 0) + 1

    entries_removed = sum(1 for entry_id in previous if entry_id not in current_ids)
    files_indexed = sum(1 for entry in entries.values() if entry.record_type == "file")
    sources_found = sum(1 for entry in entries.values() if entry.record_type == "source")
    completed_at = _now_text()
    report = DiscoveryReport(
        mode=mode,
        roots=tuple(str(root) for root in selected_roots),
        excluded_roots=tuple(str(root) for root in excluded),
        started_at=started_at,
        completed_at=completed_at,
        repositories_found=repositories_found,
        sources_found=sources_found,
        sources_by_kind=dict(sorted(sources_by_kind.items())),
        files_scanned=files_scanned,
        files_indexed=files_indexed,
        files_new=files_new,
        files_changed=files_changed,
        files_unchanged=files_unchanged,
        entries_removed=entries_removed,
        noise_excluded=noise_excluded,
        system_excluded=system_excluded,
        secrets_excluded=secrets_excluded,
        unknown_ignored=unknown_ignored,
        pruned_directories=pruned_directories,
        by_kind=dict(sorted(by_kind.items())),
        catalog_path=str(discovery_catalog_path(resolved_data_root)),
    )
    _write_catalog(
        resolved_data_root,
        roots=selected_roots,
        excluded_roots=excluded,
        updated_at=completed_at,
        entries=entries,
        report=report,
    )
    return report


def load_discovery_catalog(data_root: Path) -> dict[str, DiscoveryEntry]:
    return _load_catalog(data_root)


def discovery_status(data_root: Path) -> DiscoveryStatus:
    def empty(exists: bool = False, updated_at: str | None = None) -> DiscoveryStatus:
        return DiscoveryStatus(exists, updated_at, 0, 0, {}, 0, 0, 0, {})

    path = discovery_catalog_path(data_root)
    if not path.exists() or path.is_symlink() or not path.is_file():
        return empty()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty()
    if not isinstance(payload, dict):
        return empty()
    updated = payload.get("updated_at")
    summary = payload.get("last_summary")
    updated_at = updated if isinstance(updated, str) else None
    if not isinstance(summary, dict):
        return empty(exists=True, updated_at=updated_at)

    def count(key: str) -> int:
        item = summary.get(key)
        return item if isinstance(item, int) else 0

    raw_kind = summary.get("by_kind")
    by_kind: dict[str, int] = {}
    if isinstance(raw_kind, dict):
        for kind, value in raw_kind.items():
            if isinstance(kind, str) and isinstance(value, int):
                by_kind[kind] = value

    raw_sources = summary.get("sources_by_kind")
    sources_by_kind: dict[str, int] = {}
    if isinstance(raw_sources, dict):
        for kind, value in raw_sources.items():
            if isinstance(kind, str) and isinstance(value, int):
                sources_by_kind[kind] = value
    return DiscoveryStatus(
        catalog_exists=True,
        updated_at=updated_at,
        repositories_found=count("repositories_found"),
        sources_found=count("sources_found"),
        sources_by_kind=dict(sorted(sources_by_kind.items())),
        files_indexed=count("files_indexed"),
        noise_excluded=count("noise_excluded"),
        secrets_excluded=count("secrets_excluded"),
        by_kind=dict(sorted(by_kind.items())),
    )
