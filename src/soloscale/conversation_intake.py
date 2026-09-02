from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import zipfile
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from soloscale.knowledge_models import (
    ContentRole,
    NormalizedChunk,
    NormalizedDocument,
    ParsedSource,
    SourceKind,
)

_MAX_CODEX_BYTES = 256 * 1024 * 1024
_MAX_CHATGPT_JSON_BYTES = 512 * 1024 * 1024
_MAX_CHATGPT_ZIP_BYTES = 1024 * 1024 * 1024
_MAX_BUILDLOG_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_ZIP_MEMBERS = 20_000
_MAX_ZIP_COMPRESSION_RATIO = 200
_MAX_NORMALIZED_CHUNK_BYTES = 1_200
_NORMALIZED_CHUNK_OVERLAP_BYTES = 200

BUILDLOG_ARTIFACTS: tuple[str, ...] = (
    "ingestion-report.md",
    "02_plan.json",
    "03_draft.md",
    "04_evaluation.json",
    "05_final.md",
    "run_metadata.json",
    "timeline.json",
    "events.jsonl",
)
_BUILDLOG_NARRATIVE_ARTIFACTS = frozenset({"ingestion-report.md", "03_draft.md", "05_final.md"})
_BUILDLOG_JSON_FIELDS: dict[str, frozenset[str]] = {
    "02_plan.json": frozenset(
        {
            "central_idea",
            "hook",
            "technical_points",
            "decision_story",
            "reader_value",
            "ending",
        }
    ),
    "04_evaluation.json": frozenset(
        {
            "technical_accuracy",
            "specificity",
            "readability",
            "reader_value",
            "evidence_coverage",
            "unsupported_claims",
            "vague_sections",
            "revision_instructions",
            "hard_failure",
            "status",
            "finding",
        }
    ),
    "run_metadata.json": frozenset(
        {
            "run_id",
            "task_id",
            "iteration_id",
            "output_type",
            "pipeline_status",
            "observability_status",
            "reproducibility_status",
            "status",
            "started_at",
            "ended_at",
            "duration_ms",
            "provider",
            "model",
            "model_digest",
            "temperature",
            "max_tokens",
            "git_commit",
            "git_branch",
            "working_tree_dirty",
            "llm_call_count",
            "slowest_step",
            "highest_token_step",
            "revision_performed",
            "revision_output_changed",
            "revision_improvement_status",
            "observability_issues",
        }
    ),
    "timeline.json": frozenset(
        {
            "run_id",
            "pipeline_status",
            "observability_status",
            "total_duration_ms",
            "slowest_step",
            "highest_token_step",
            "llm_call_count",
            "revision_performed",
            "steps",
        }
    ),
}
_BUILDLOG_TIMELINE_STEP_FIELDS = frozenset(
    {
        "id",
        "run_id",
        "sequence",
        "step_name",
        "status",
        "started_at",
        "ended_at",
        "duration_ms",
        "attempt_count",
        "skip_reason",
        "timing_mode",
    }
)


class ConversationIntakeError(ValueError):
    """Base error whose message is safe to place in a private failure receipt."""

    code = "conversation_intake_error"


class SourceChangedError(ConversationIntakeError):
    code = "source_changed"

    def __init__(self) -> None:
        super().__init__("source changed while it was being read")


class SourceFormatError(ConversationIntakeError):
    code = "source_format_invalid"

    def __init__(self, message: str = "source format is invalid") -> None:
        super().__init__(message)


class SourceSafetyError(ConversationIntakeError):
    code = "source_safety_rejected"

    def __init__(self, message: str = "source failed a safety check") -> None:
        super().__init__(message)


_AUTO_CONTEXT_PATTERNS = tuple(
    re.compile(pattern, flags=re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"<environment_context(?:\s[^>]*)?>.*?</environment_context\s*>",
        r"<skills_instructions(?:\s[^>]*)?>.*?</skills_instructions\s*>",
        r"<apps_instructions(?:\s[^>]*)?>.*?</apps_instructions\s*>",
        r"<plugins_instructions(?:\s[^>]*)?>.*?</plugins_instructions\s*>",
        r"<recommended_plugins(?:\s[^>]*)?>.*?</recommended_plugins\s*>",
        r"<developer_instructions(?:\s[^>]*)?>.*?</developer_instructions\s*>",
        r"<system_instructions(?:\s[^>]*)?>.*?</system_instructions\s*>",
        r"<collaboration_mode(?:\s[^>]*)?>.*?</collaboration_mode\s*>",
        (
            r"<permissions(?:_instructions|-instructions|\s+instructions)(?:\s[^>]*)?>"
            r".*?</permissions(?:_instructions|-instructions|\s+instructions)\s*>"
        ),
        r"<in-app-browser-context(?:\s[^>]*)?>.*?</in-app-browser-context\s*>",
    )
)
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    flags=re.IGNORECASE | re.DOTALL,
)
_BEARER_PATTERN = re.compile(r"(?i)(\b(?:authorization\s*:\s*)?bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_BASIC_AUTH_PATTERN = re.compile(r"(?i)(\bauthorization\s*:\s*basic\s+)[A-Za-z0-9+/=_-]{4,}")
_KNOWN_TOKEN_PATTERN = re.compile(
    r"(?i)\b(?:sk-(?:ant-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,}|glpat-[A-Za-z0-9_-]{8,}|"
    r"xox[baprs]-[A-Za-z0-9-]{12,}|AIza[0-9A-Za-z_-]{20,})\b"
)
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)(\b[A-Za-z_][A-Za-z0-9_]*(?:API[_-]?KEY|TOKEN|PASSWORD|PASSWD|PWD|SECRET|"
    r"PRIVATE[_-]?KEY|ACCESS[_-]?KEY)[A-Za-z0-9_]*\b\s*(?:=|:)\s*)"
    r"""("(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|[^\s,;#}]+)"""
)
_NAMED_SECRET_PATTERN = re.compile(
    r"(?im)(\b(?:api[_-]?key|token|access[_-]?token|refresh[_-]?token|password|passwd|"
    r"secret|private[_-]?key|access[_-]?key)"
    r"""\b\s*(?:=|:)\s*)("(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|"""
    r"[^\s,;#}]+)"
)
_QUOTED_SECRET_PATTERN = re.compile(
    r"""(?i)(["'](?:[A-Za-z_][A-Za-z0-9_]*(?:api[_-]?key|token|password|passwd|secret|"""
    r"""private[_-]?key|access[_-]?key)[A-Za-z0-9_]*|api[_-]?key|access[_-]?token|"""
    r"""refresh[_-]?token|password|passwd|secret)["']\s*:\s*["'])(.*?)(["'])"""
)
_DATABASE_CREDENTIAL_URL_PATTERN = re.compile(
    r"(?i)\b((?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss|"
    r"amqp|amqps|mssql|cockroachdb|snowflake)://)[^\s/@:]+:[^\s/@]+@"
)

_CHATGPT_HIDDEN_FLAGS = (
    "is_visually_hidden_from_conversation",
    "is_hidden",
    "hidden",
)

_BUILDLOG_EVENT_SCALAR_FIELDS = frozenset(
    {
        "artifact_sha256",
        "completion_tokens",
        "duration_ms",
        "error_code",
        "event_id",
        "event_type",
        "input_sha256",
        "input_tokens",
        "model",
        "output_sha256",
        "output_tokens",
        "prompt_sha256",
        "prompt_tokens",
        "prompt_version",
        "response_sha256",
        "run_id",
        "stage",
        "status",
        "step",
        "timestamp",
        "total_tokens",
        "type",
    }
)


def redact_text(text: str) -> str:
    """Remove common credentials and automatically injected control-plane blocks."""

    redacted = text.replace("\r\n", "\n").replace("\r", "\n")
    for pattern in _AUTO_CONTEXT_PATTERNS:
        redacted = pattern.sub("[REDACTED AUTO-CONTEXT]", redacted)
    redacted = _PRIVATE_KEY_PATTERN.sub("[REDACTED PRIVATE KEY]", redacted)
    redacted = _DATABASE_CREDENTIAL_URL_PATTERN.sub(r"\1[REDACTED CREDENTIALS]@", redacted)
    redacted = _BEARER_PATTERN.sub(r"\1[REDACTED]", redacted)
    redacted = _BASIC_AUTH_PATTERN.sub(r"\1[REDACTED]", redacted)
    redacted = _KNOWN_TOKEN_PATTERN.sub("[REDACTED TOKEN]", redacted)
    redacted = _JWT_PATTERN.sub("[REDACTED TOKEN]", redacted)
    redacted = _QUOTED_SECRET_PATTERN.sub(r"\1[REDACTED]\3", redacted)
    redacted = _SECRET_ASSIGNMENT_PATTERN.sub(r"\1[REDACTED]", redacted)
    redacted = _NAMED_SECRET_PATTERN.sub(r"\1[REDACTED]", redacted)
    return redacted


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _stable_id(prefix: str, *parts: str) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"{prefix}-{_sha256_bytes(canonical)}"


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise SourceFormatError("source contains unsupported JSON values") from None


def _decode_utf8(value: bytes) -> str:
    try:
        return value.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SourceFormatError("source text is not valid UTF-8") from None


def _load_json_bytes(value: bytes) -> object:
    try:
        return json.loads(_decode_utf8(value))
    except json.JSONDecodeError:
        raise SourceFormatError("source does not contain valid JSON") from None


def _load_json_line(value: bytes) -> object:
    try:
        return json.loads(_decode_utf8(value))
    except json.JSONDecodeError:
        raise SourceFormatError("source contains an invalid complete JSON line") from None


def _snapshot_from_stat(source_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_size,
        source_stat.st_mtime_ns,
        source_stat.st_ctime_ns,
    )


def _file_snapshot(path: Path) -> tuple[int, int, int, int, int]:
    try:
        source_stat = path.lstat()
    except OSError:
        raise SourceSafetyError("source is not an accessible regular file") from None
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
        raise SourceSafetyError("source must be a regular non-symlink file")
    return _snapshot_from_stat(source_stat)


def _open_readonly(path: Path) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError:
        raise SourceSafetyError("source could not be opened safely") from None


def _read_stable_bytes(
    path: Path, *, max_bytes: int
) -> tuple[bytes, tuple[int, int, int, int, int]]:
    before = _file_snapshot(path)
    if before[2] > max_bytes:
        raise SourceSafetyError("source exceeds the configured size limit")

    file_descriptor = _open_readonly(path)
    try:
        opened = _snapshot_from_stat(os.fstat(file_descriptor))
        if opened != before:
            raise SourceChangedError
        with os.fdopen(file_descriptor, "rb", closefd=False) as source_file:
            value = source_file.read(max_bytes + 1)
        after_read = _snapshot_from_stat(os.fstat(file_descriptor))
    finally:
        os.close(file_descriptor)

    if len(value) > max_bytes:
        raise SourceSafetyError("source exceeds the configured size limit")
    after = _file_snapshot(path)
    if opened != after_read or before != after or len(value) != before[2]:
        raise SourceChangedError
    return value, after


def _scan_stable_lines(
    path: Path,
    *,
    max_bytes: int,
    on_complete_line: Callable[[int, bytes], None],
) -> tuple[str, int, tuple[int, int, int, int, int]]:
    before = _file_snapshot(path)
    if before[2] > max_bytes:
        raise SourceSafetyError("source exceeds the configured size limit")

    digest = hashlib.sha256()
    total = 0
    file_descriptor = _open_readonly(path)
    try:
        opened = _snapshot_from_stat(os.fstat(file_descriptor))
        if opened != before:
            raise SourceChangedError
        with os.fdopen(file_descriptor, "rb", closefd=False) as source_file:
            for line_number, raw_line in enumerate(source_file, start=1):
                total += len(raw_line)
                if total > max_bytes:
                    raise SourceSafetyError("source exceeds the configured size limit")
                digest.update(raw_line)
                if raw_line.endswith(b"\n"):
                    on_complete_line(line_number, raw_line.rstrip(b"\r\n"))
                elif raw_line.strip():
                    # JSONL writers do not have to terminate the final complete record
                    # with LF.  Keep the previous crash-recovery behavior for a truly
                    # truncated final record by validating it before handing it over.
                    try:
                        _load_json_line(raw_line)
                    except SourceFormatError:
                        continue
                    on_complete_line(line_number, raw_line)
        after_read = _snapshot_from_stat(os.fstat(file_descriptor))
    finally:
        os.close(file_descriptor)

    after = _file_snapshot(path)
    if opened != after_read or before != after or total != before[2]:
        raise SourceChangedError
    return digest.hexdigest(), total, after


def _datetime_from_value(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 1024:
        return None
    return cleaned


def _redacted_nonblank(parts: Iterable[str]) -> str | None:
    joined = "\n".join(part for part in parts if part.strip())
    if not joined.strip():
        return None
    redacted = redact_text(joined).strip()
    return redacted if redacted else None


def _text_segments(text: str) -> list[str]:
    """Split long normalized content into deterministic overlapping retrieval units."""

    normalized = text.strip()
    if not normalized:
        return []
    encoded = normalized.encode("utf-8")
    if len(encoded) <= _MAX_NORMALIZED_CHUNK_BYTES:
        return [normalized]
    segments: list[str] = []
    start = 0
    while start < len(encoded):
        end = min(start + _MAX_NORMALIZED_CHUNK_BYTES, len(encoded))
        while end < len(encoded) and end > start and encoded[end] & 0b11000000 == 0b10000000:
            end -= 1
        if end <= start:
            raise SourceFormatError("normalized content could not be segmented safely")
        segment = encoded[start:end].decode("utf-8").strip()
        if segment:
            segments.append(segment)
        if end >= len(encoded):
            break
        next_start = max(start, end - _NORMALIZED_CHUNK_OVERLAP_BYTES)
        while next_start < end and encoded[next_start] & 0b11000000 == 0b10000000:
            next_start += 1
        start = end if next_start <= start else next_start
    return segments


def _codex_message_text(payload: Mapping[str, object]) -> str | None:
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for part in content:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") not in {"input_text", "output_text", "text"}:
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return _redacted_nonblank(parts)


def _codex_event_id(event: Mapping[str, object], payload: Mapping[str, object]) -> str | None:
    for candidate in (
        payload.get("id"),
        payload.get("message_id"),
        event.get("id"),
        event.get("event_id"),
    ):
        identifier = _safe_identifier(candidate)
        if identifier is not None:
            return identifier
    return None


def parse_codex_session(path: Path) -> ParsedSource:
    source_path = Path(path)
    session_payload: Mapping[str, object] | None = None
    session_timestamp: datetime | None = None
    candidates: list[
        tuple[int, Mapping[str, object], Mapping[str, object], str, datetime | None]
    ] = []

    def consume_line(line_number: int, raw_line: bytes) -> None:
        nonlocal session_payload, session_timestamp
        if not raw_line.strip():
            return
        value = _load_json_line(raw_line)
        if not isinstance(value, Mapping):
            return
        event_type = value.get("type")
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            return
        if event_type == "session_meta" and session_payload is None:
            if _safe_identifier(payload.get("id")) is not None:
                session_payload = payload
                session_timestamp = _datetime_from_value(
                    value.get("timestamp") or payload.get("timestamp")
                )
            return
        if event_type != "response_item" or payload.get("type") != "message":
            return
        role = payload.get("role")
        if role not in {ContentRole.USER.value, ContentRole.ASSISTANT.value}:
            return
        text = _codex_message_text(payload)
        if text is None:
            return
        timestamp = _datetime_from_value(value.get("timestamp") or payload.get("timestamp"))
        candidates.append((line_number, value, payload, text, timestamp))

    content_sha256, byte_size, snapshot = _scan_stable_lines(
        source_path,
        max_bytes=_MAX_CODEX_BYTES,
        on_complete_line=consume_line,
    )
    if session_payload is None:
        raise SourceFormatError("Codex source has no valid session metadata")

    external_id = _safe_identifier(session_payload.get("id"))
    if external_id is None:  # Kept explicit for type narrowing and defensive evolution.
        raise SourceFormatError("Codex source has no valid session identifier")
    document_id = _stable_id("doc", SourceKind.CODEX_SESSION.value, external_id)

    metadata: dict[str, str] = {}
    nested_source = session_payload.get("source")
    source_mapping = nested_source if isinstance(nested_source, Mapping) else {}
    for key in ("parent_thread_id", "forked_from_id"):
        identifier = _safe_identifier(session_payload.get(key)) or _safe_identifier(
            source_mapping.get(key)
        )
        if identifier is not None:
            metadata[key] = identifier
    parent_external_id = metadata.get("parent_thread_id") or metadata.get("forked_from_id")

    raw_title = session_payload.get("title")
    title = redact_text(raw_title).strip() if isinstance(raw_title, str) else None
    if title == "":
        title = None

    chunks: list[NormalizedChunk] = []
    seen_messages: dict[str, str] = {}
    for line_number, event, payload, text, timestamp in candidates:
        role = ContentRole(str(payload["role"]))
        explicit_id = _codex_event_id(event, payload)
        canonical_hash = _sha256_bytes(_canonical_json_bytes(payload))
        message_id = explicit_id or f"line-{line_number}-{canonical_hash}"
        message_hash = _sha256_text(text)
        previous_hash = seen_messages.get(message_id)
        if previous_hash is not None:
            if previous_hash != message_hash:
                raise SourceFormatError("source contains conflicting message identifiers")
            continue
        seen_messages[message_id] = message_hash
        segments = _text_segments(text)
        for segment_index, segment in enumerate(segments):
            if len(segments) == 1 and explicit_id is not None:
                chunk_id = _stable_id("chunk", document_id, "message", explicit_id)
            elif len(segments) == 1:
                chunk_id = _stable_id("chunk", external_id, str(line_number), canonical_hash)
            else:
                chunk_id = _stable_id(
                    "chunk", document_id, "message", message_id, str(segment_index)
                )
            chunks.append(
                NormalizedChunk(
                    id=chunk_id,
                    document_id=document_id,
                    ordinal=len(chunks),
                    role=role,
                    timestamp=timestamp,
                    text=segment,
                    text_sha256=_sha256_text(segment),
                    metadata={
                        "source_line": str(line_number),
                        "message_id": message_id,
                        "segment": str(segment_index),
                    },
                )
            )

    return ParsedSource(
        document=NormalizedDocument(
            id=document_id,
            source_kind=SourceKind.CODEX_SESSION,
            external_id=external_id,
            locator=str(source_path),
            title=title,
            content_sha256=content_sha256,
            byte_size=byte_size,
            observed_at=session_timestamp
            or datetime.fromtimestamp(snapshot[3] / 1_000_000_000, tz=UTC),
            parent_external_id=parent_external_id,
            metadata=metadata,
        ),
        chunks=chunks,
    )


def _peek_codex_external_id(path: Path) -> str | None:
    found: str | None = None

    def consume_line(_line_number: int, raw_line: bytes) -> None:
        nonlocal found
        if found is not None or not raw_line.strip():
            return
        value = _load_json_line(raw_line)
        if not isinstance(value, Mapping) or value.get("type") != "session_meta":
            return
        payload = value.get("payload")
        if isinstance(payload, Mapping):
            found = _safe_identifier(payload.get("id"))

    try:
        _scan_stable_lines(path, max_bytes=_MAX_CODEX_BYTES, on_complete_line=consume_line)
    except ConversationIntakeError:
        return None
    return found


def discover_codex_sources(codex_home: Path) -> list[Path]:
    home = Path(codex_home)
    candidates: list[Path] = []
    for directory_name in ("sessions", "archived_sessions"):
        directory = home / directory_name
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in directory.rglob("*.jsonl"):
            if path.is_file() and not path.is_symlink():
                candidates.append(path)

    selected: dict[str, tuple[tuple[int, int, str], Path]] = {}
    for path in sorted(set(candidates), key=lambda item: item.as_posix()):
        external_id = _peek_codex_external_id(path)
        identity = f"session:{external_id}" if external_id else f"path:{path.resolve()}"
        try:
            snapshot = _file_snapshot(path)
        except ConversationIntakeError:
            continue
        projection_key = (snapshot[2], snapshot[3], path.as_posix())
        current = selected.get(identity)
        if current is None or projection_key > current[0]:
            selected[identity] = (projection_key, path)
    return sorted((entry[1] for entry in selected.values()), key=lambda item: item.as_posix())


def _chatgpt_node_sort_key(node_id: str, node: object) -> tuple[float, str]:
    timestamp = 0.0
    if isinstance(node, Mapping):
        message = node.get("message")
        if isinstance(message, Mapping):
            value = message.get("create_time") or message.get("update_time")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                timestamp = float(value)
    return timestamp, node_id


def _ordered_chatgpt_nodes(mapping: Mapping[str, object]) -> list[tuple[str, object]]:
    ordered_ids: list[str] = []
    visited: set[str] = set()

    def visit(start_node_id: str) -> None:
        stack = [start_node_id]
        while stack:
            node_id = stack.pop()
            if node_id in visited or node_id not in mapping:
                continue
            visited.add(node_id)
            ordered_ids.append(node_id)
            node = mapping[node_id]
            children: list[str] = []
            if isinstance(node, Mapping) and isinstance(node.get("children"), list):
                children = [child for child in node["children"] if isinstance(child, str)]
            ordered_children = sorted(
                set(children),
                key=lambda item: _chatgpt_node_sort_key(item, mapping.get(item)),
            )
            stack.extend(reversed(ordered_children))

    roots = [
        node_id
        for node_id, node in mapping.items()
        if not isinstance(node, Mapping)
        or not isinstance(node.get("parent"), str)
        or node.get("parent") not in mapping
    ]
    for root in sorted(roots, key=lambda item: _chatgpt_node_sort_key(item, mapping[item])):
        visit(root)
    for node_id in sorted(mapping, key=lambda item: _chatgpt_node_sort_key(item, mapping[item])):
        visit(node_id)
    return [(node_id, mapping[node_id]) for node_id in ordered_ids]


def _chatgpt_message_text(message: Mapping[str, object]) -> str | None:
    content = message.get("content")
    if not isinstance(content, Mapping):
        return None
    if content.get("content_type") not in {None, "text", "multimodal_text"}:
        return None
    parts = content.get("parts")
    if not isinstance(parts, list):
        return None
    # Non-string parts are attachment or multimodal payloads and stay outside this boundary.
    return _redacted_nonblank(part for part in parts if isinstance(part, str))


def _truthy_visibility_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    return isinstance(value, str) and value.strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _chatgpt_object_is_hidden(value: Mapping[str, object]) -> bool:
    metadata = value.get("metadata")
    scopes = (value, metadata) if isinstance(metadata, Mapping) else (value,)
    return any(
        _truthy_visibility_flag(scope.get(flag))
        for scope in scopes
        for flag in _CHATGPT_HIDDEN_FLAGS
    )


def _chatgpt_message_is_visible(node: Mapping[str, object], message: Mapping[str, object]) -> bool:
    if _chatgpt_object_is_hidden(node) or _chatgpt_object_is_hidden(message):
        return False
    recipient = message.get("recipient")
    return recipient is None or (
        isinstance(recipient, str) and recipient.strip().casefold() == "all"
    )


def _chatgpt_current_path(
    mapping: Mapping[str, object],
    current_node: object,
) -> set[str] | None:
    node_id = _safe_identifier(current_node)
    if node_id is None or node_id not in mapping:
        return None
    path: set[str] = set()
    while node_id not in path:
        path.add(node_id)
        node = mapping.get(node_id)
        if not isinstance(node, Mapping):
            break
        parent = _safe_identifier(node.get("parent"))
        if parent is None or parent not in mapping:
            break
        node_id = parent
    return path


def _read_chatgpt_zip(path: Path) -> tuple[bytes, str]:
    before = _file_snapshot(path)
    if before[2] > _MAX_CHATGPT_ZIP_BYTES:
        raise SourceSafetyError("ChatGPT archive exceeds the configured size limit")
    file_descriptor = _open_readonly(path)
    try:
        opened = _snapshot_from_stat(os.fstat(file_descriptor))
        if opened != before:
            raise SourceChangedError
        with os.fdopen(file_descriptor, "rb", closefd=False) as source_file:
            with zipfile.ZipFile(source_file) as archive:
                members = archive.infolist()
                if len(members) > _MAX_ZIP_MEMBERS:
                    raise SourceSafetyError("ChatGPT archive contains too many members")
                candidates = [
                    member
                    for member in members
                    if not member.is_dir()
                    and PurePosixPath(member.filename).name.casefold() == "conversations.json"
                ]
                if len(candidates) != 1:
                    raise SourceSafetyError(
                        "ChatGPT archive must contain exactly one conversations.json member"
                    )
                member = candidates[0]
                if member.flag_bits & 0x1:
                    raise SourceSafetyError("encrypted ChatGPT archives are not supported")
                if member.file_size > _MAX_CHATGPT_JSON_BYTES:
                    raise SourceSafetyError("ChatGPT conversations member exceeds the size limit")
                compression_ratio_unsafe = member.compress_size == 0 or (
                    member.file_size / member.compress_size > _MAX_ZIP_COMPRESSION_RATIO
                )
                if member.file_size > 1024 * 1024 and compression_ratio_unsafe:
                    raise SourceSafetyError("ChatGPT archive compression ratio is unsafe")
                with archive.open(member, "r") as member_file:
                    value = member_file.read(_MAX_CHATGPT_JSON_BYTES + 1)
                if len(value) > _MAX_CHATGPT_JSON_BYTES or len(value) != member.file_size:
                    raise SourceSafetyError("ChatGPT conversations member failed its size check")
            after_read = _snapshot_from_stat(os.fstat(file_descriptor))
    except zipfile.BadZipFile:
        raise SourceFormatError("ChatGPT archive is not a valid ZIP file") from None
    finally:
        os.close(file_descriptor)
    after = _file_snapshot(path)
    if opened != after_read or before != after:
        raise SourceChangedError
    return value, member.filename


def _load_chatgpt_conversations(path: Path) -> tuple[list[object], str]:
    if path.name.casefold() == "conversations.json":
        raw, _snapshot = _read_stable_bytes(path, max_bytes=_MAX_CHATGPT_JSON_BYTES)
        member_name = "conversations.json"
    elif path.suffix.casefold() == ".zip":
        raw, member_name = _read_chatgpt_zip(path)
    else:
        raise SourceFormatError("ChatGPT input must be conversations.json or a ZIP export")
    value = _load_json_bytes(raw)
    if not isinstance(value, list):
        raise SourceFormatError("ChatGPT conversations root must be a JSON array")
    return value, member_name


def _parse_chatgpt_conversation(
    source_path: Path,
    member_name: str,
    conversation: Mapping[str, object],
) -> ParsedSource:
    raw_conversation = _canonical_json_bytes(conversation)
    content_sha256 = _sha256_bytes(raw_conversation)
    external_id = (
        _safe_identifier(conversation.get("conversation_id"))
        or _safe_identifier(conversation.get("id"))
        or f"anonymous-{content_sha256}"
    )
    document_id = _stable_id("doc", SourceKind.CHATGPT_EXPORT.value, external_id)
    mapping_value = conversation.get("mapping")
    mapping: Mapping[str, object] = mapping_value if isinstance(mapping_value, Mapping) else {}
    current_path = _chatgpt_current_path(mapping, conversation.get("current_node"))
    chunks: list[NormalizedChunk] = []
    seen_messages: dict[str, str] = {}
    projected_nodes: list[
        tuple[str, Mapping[str, object], Mapping[str, object], ContentRole, str, str]
    ] = []

    for node_id, node in _ordered_chatgpt_nodes(mapping):
        if current_path is not None and node_id not in current_path:
            continue
        if not isinstance(node, Mapping):
            continue
        message = node.get("message")
        if not isinstance(message, Mapping):
            continue
        if not _chatgpt_message_is_visible(node, message):
            continue
        author = message.get("author")
        if not isinstance(author, Mapping):
            continue
        role_value = author.get("role")
        if role_value not in {ContentRole.USER.value, ContentRole.ASSISTANT.value}:
            continue
        role = ContentRole(str(role_value))
        text = _chatgpt_message_text(message)
        if text is None:
            continue
        message_id = _safe_identifier(message.get("id")) or _safe_identifier(node_id)
        if message_id is None:
            message_id = _sha256_bytes(_canonical_json_bytes(message))
        message_hash = _sha256_text(text)
        previous_hash = seen_messages.get(message_id)
        if previous_hash is not None:
            if previous_hash != message_hash:
                raise SourceFormatError("source contains conflicting message identifiers")
            continue
        seen_messages[message_id] = message_hash
        projected_nodes.append((node_id, node, message, role, text, message_id))

    projected_node_ids = {node_id for node_id, *_rest in projected_nodes}
    for node_id, node, message, role, text, message_id in projected_nodes:
        timestamp = _datetime_from_value(message.get("create_time") or message.get("update_time"))
        parent_node_id = _safe_identifier(node.get("parent"))
        visited_parents: set[str] = set()
        while parent_node_id is not None and parent_node_id not in projected_node_ids:
            if parent_node_id in visited_parents:
                parent_node_id = None
                break
            visited_parents.add(parent_node_id)
            parent_node = mapping.get(parent_node_id)
            if not isinstance(parent_node, Mapping):
                parent_node_id = None
                break
            parent_node_id = _safe_identifier(parent_node.get("parent"))
        segments = _text_segments(text)
        for segment_index, segment in enumerate(segments):
            chunk_id = (
                _stable_id("chunk", document_id, "message", message_id)
                if len(segments) == 1
                else _stable_id("chunk", document_id, "message", message_id, str(segment_index))
            )
            chunk_metadata = {
                "node_id": node_id,
                "message_id": message_id,
                "segment": str(segment_index),
            }
            if parent_node_id is not None:
                chunk_metadata["parent_node_id"] = parent_node_id
            chunks.append(
                NormalizedChunk(
                    id=chunk_id,
                    document_id=document_id,
                    ordinal=len(chunks),
                    role=role,
                    timestamp=timestamp,
                    text=segment,
                    text_sha256=_sha256_text(segment),
                    metadata=chunk_metadata,
                )
            )

    raw_title = conversation.get("title")
    title = redact_text(raw_title).strip() if isinstance(raw_title, str) else None
    if title == "":
        title = None
    observed_at = _datetime_from_value(
        conversation.get("update_time") or conversation.get("create_time")
    )
    return ParsedSource(
        document=NormalizedDocument(
            id=document_id,
            source_kind=SourceKind.CHATGPT_EXPORT,
            external_id=external_id,
            locator=f"{source_path}#{member_name}:{document_id}",
            title=title,
            content_sha256=content_sha256,
            byte_size=len(raw_conversation),
            observed_at=observed_at,
            metadata={"export_member": member_name},
        ),
        chunks=chunks,
    )


def parse_chatgpt_export(path: Path) -> list[ParsedSource]:
    source_path = Path(path)
    conversations, member_name = _load_chatgpt_conversations(source_path)
    selected: dict[str, tuple[tuple[float, int, str], ParsedSource]] = {}
    for value in conversations:
        if not isinstance(value, Mapping):
            continue
        parsed = _parse_chatgpt_conversation(source_path, member_name, value)
        observed = parsed.document.observed_at
        timestamp = observed.timestamp() if observed is not None else 0.0
        choice_key = (
            timestamp,
            parsed.document.byte_size,
            parsed.document.content_sha256,
        )
        current = selected.get(parsed.document.external_id)
        if current is None or choice_key > current[0]:
            selected[parsed.document.external_id] = (choice_key, parsed)
    return [selected[key][1] for key in sorted(selected)]


def _directory_has_buildlog_artifact(path: Path) -> bool:
    return any((path / name).is_file() for name in BUILDLOG_ARTIFACTS)


def discover_buildlog_runs(root: Path) -> list[Path]:
    root_path = Path(root)
    if not root_path.is_dir() or root_path.is_symlink():
        return []
    discovered: set[Path] = set()
    frontier = [(root_path, 0)]
    while frontier:
        directory, depth = frontier.pop(0)
        if _directory_has_buildlog_artifact(directory):
            discovered.add(directory)
            continue
        if depth >= 2:
            continue
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        frontier.extend(
            (child, depth + 1) for child in children if child.is_dir() and not child.is_symlink()
        )
    return sorted(discovered, key=lambda item: item.as_posix())


def _existing_buildlog_files(run_dir: Path) -> list[Path]:
    files: list[Path] = []
    for name in BUILDLOG_ARTIFACTS:
        path = run_dir / name
        try:
            exists = path.exists() or path.is_symlink()
        except OSError:
            exists = False
        if not exists:
            continue
        _file_snapshot(path)
        files.append(path)
    return files


def _combined_artifact_hash(artifacts: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in BUILDLOG_ARTIFACTS:
        value = artifacts.get(name)
        if value is None:
            continue
        name_bytes = name.encode("utf-8")
        digest.update(len(name_bytes).to_bytes(4, "big"))
        digest.update(name_bytes)
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def _project_buildlog_event(raw_line: bytes) -> bytes | None:
    """Return only explicitly safe scalar event metadata.

    BuildLog event payloads can contain prompts, responses, tool arguments, or
    stdout.  The ingestion boundary intentionally discards every field that is
    not named in the scalar allowlist instead of trying to redact arbitrary
    nested structures after the fact.
    """

    try:
        event = _load_json_line(raw_line)
    except SourceFormatError:
        return None
    if not isinstance(event, Mapping):
        return None
    projected: dict[str, str | int | float | bool | None] = {}
    for key in sorted(_BUILDLOG_EVENT_SCALAR_FIELDS):
        value = event.get(key)
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            projected[key] = value
    if not projected:
        return None
    return _canonical_json_bytes(projected)


def _project_buildlog_events(raw: bytes) -> list[bytes]:
    projected: list[bytes] = []
    for raw_line in raw.splitlines(keepends=True):
        if not raw_line.strip():
            continue
        complete_line = raw_line.rstrip(b"\r\n")
        event = _project_buildlog_event(complete_line)
        if event is not None:
            projected.append(event)
    return projected


def _safe_json_value(value: object) -> object | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        projected = [
            item for item in (_safe_json_value(item) for item in value) if item is not None
        ]
        return projected
    return None


def _project_buildlog_json(artifact_name: str, raw: bytes) -> bytes | None:
    allowed_fields = _BUILDLOG_JSON_FIELDS.get(artifact_name)
    if allowed_fields is None:
        return None
    try:
        loaded = _load_json_bytes(raw)
    except SourceFormatError:
        return None
    if not isinstance(loaded, Mapping):
        return None
    projected: dict[str, object] = {}
    for key in sorted(allowed_fields):
        value = loaded.get(key)
        if artifact_name == "timeline.json" and key == "steps":
            if not isinstance(value, list):
                continue
            safe_steps: list[dict[str, object]] = []
            for step in value:
                if not isinstance(step, Mapping):
                    continue
                safe_step = {
                    field: safe_value
                    for field in sorted(_BUILDLOG_TIMELINE_STEP_FIELDS)
                    if (safe_value := _safe_json_value(step.get(field))) is not None
                }
                if safe_step:
                    safe_steps.append(safe_step)
            projected[key] = safe_steps
            continue
        safe_value = _safe_json_value(value)
        if safe_value is not None:
            projected[key] = safe_value
    return _canonical_json_bytes(projected) if projected else None


def parse_buildlog_run(run_dir: Path) -> ParsedSource:
    directory = Path(run_dir)
    if not directory.is_dir() or directory.is_symlink():
        raise SourceSafetyError("BuildLog run must be a regular directory")
    before_files = _existing_buildlog_files(directory)
    if not before_files:
        raise SourceFormatError("BuildLog run contains no allowlisted artifacts")

    artifacts: dict[str, bytes] = {}
    snapshots: dict[str, tuple[int, int, int, int, int]] = {}
    for path in before_files:
        value, snapshot = _read_stable_bytes(path, max_bytes=_MAX_BUILDLOG_ARTIFACT_BYTES)
        artifacts[path.name] = value
        snapshots[path.name] = snapshot

    after_files = _existing_buildlog_files(directory)
    if [path.name for path in before_files] != [path.name for path in after_files]:
        raise SourceChangedError
    if any(_file_snapshot(path) != snapshots[path.name] for path in after_files):
        raise SourceChangedError

    run_metadata: Mapping[str, object] = {}
    metadata_raw = artifacts.get("run_metadata.json")
    if metadata_raw is not None:
        try:
            loaded_metadata = _load_json_bytes(metadata_raw)
        except SourceFormatError:
            loaded_metadata = None
        if isinstance(loaded_metadata, Mapping):
            run_metadata = loaded_metadata

    path_identity = _sha256_text(str(directory.absolute()))[:16]
    fallback_name = directory.name.strip() or "buildlog-run"
    external_id = _safe_identifier(run_metadata.get("run_id")) or (
        f"{fallback_name}-{path_identity}"
    )
    document_id = _stable_id("doc", SourceKind.BUILDLOG_RUN.value, external_id)

    document_metadata: dict[str, str] = {}
    for key in ("model", "prompt_version", "status"):
        metadata_value = run_metadata.get(key)
        if isinstance(metadata_value, (str, int, float, bool)):
            document_metadata[key] = redact_text(str(metadata_value))
    task_id = _safe_identifier(run_metadata.get("task_id"))
    if task_id is not None:
        document_metadata["task_id"] = task_id

    raw_title = run_metadata.get("title")
    title: str | None = (
        redact_text(raw_title).strip() if isinstance(raw_title, str) else directory.name
    )
    if not title:
        title = None

    chunks: list[NormalizedChunk] = []
    for artifact_name in BUILDLOG_ARTIFACTS:
        raw = artifacts.get(artifact_name)
        if raw is None:
            continue
        if artifact_name == "events.jsonl":
            logical_segments = _project_buildlog_events(raw)
        elif artifact_name in _BUILDLOG_NARRATIVE_ARTIFACTS:
            logical_segments = [raw] if raw.strip() else []
        elif (projected := _project_buildlog_json(artifact_name, raw)) is not None:
            logical_segments = [projected]
        else:
            # Structured control artifacts can embed raw prompts, tool payloads,
            # or responses. Their safe scalar metadata is already projected into
            # the document contract; their arbitrary bodies are not searchable.
            logical_segments = []
        for segment_index, segment in enumerate(logical_segments):
            text = redact_text(_decode_utf8(segment)).strip()
            if not text:
                continue
            text_parts = _text_segments(text)
            for text_part_index, text_part in enumerate(text_parts):
                chunk_id = _stable_id(
                    "chunk",
                    document_id,
                    artifact_name,
                    str(segment_index),
                    str(text_part_index),
                    _sha256_text(text_part),
                )
                chunks.append(
                    NormalizedChunk(
                        id=chunk_id,
                        document_id=document_id,
                        ordinal=len(chunks),
                        role=ContentRole.ARTIFACT,
                        text=text_part,
                        text_sha256=_sha256_text(text_part),
                        metadata={
                            "artifact": artifact_name,
                            "record": str(segment_index),
                            "segment": str(text_part_index),
                        },
                    )
                )

    observed_ns = max(snapshot[3] for snapshot in snapshots.values())
    return ParsedSource(
        document=NormalizedDocument(
            id=document_id,
            source_kind=SourceKind.BUILDLOG_RUN,
            external_id=external_id,
            locator=str(directory),
            title=title,
            content_sha256=_combined_artifact_hash(artifacts),
            byte_size=sum(len(value) for value in artifacts.values()),
            observed_at=datetime.fromtimestamp(observed_ns / 1_000_000_000, tz=UTC),
            parent_external_id=task_id,
            metadata=document_metadata,
        ),
        chunks=chunks,
    )
