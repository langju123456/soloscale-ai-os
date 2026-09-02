from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from typing import Any

import pytest

import soloscale.conversation_intake as intake
from soloscale.conversation_intake import (
    SourceChangedError,
    discover_buildlog_runs,
    discover_codex_sources,
    parse_buildlog_run,
    parse_chatgpt_export,
    parse_codex_session,
    redact_text,
)
from soloscale.knowledge_models import ContentRole, SourceKind
from soloscale.knowledge_store import KnowledgeStore


def _json_line(value: object) -> str:
    return json.dumps(value, ensure_ascii=False) + "\n"


def _codex_session(session_id: str, *, secret: str = "sk-example123456789") -> str:
    return "".join(
        (
            _json_line(
                {
                    "timestamp": "2026-08-09T01:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": session_id,
                        "parent_thread_id": "parent-1",
                    },
                }
            ),
            _json_line(
                {
                    "id": "event-user-1",
                    "timestamp": "2026-08-09T01:01:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    f"debug this token={secret}\n"
                                    "<environment_context>private cwd</environment_context>"
                                ),
                            },
                            {"type": "input_image", "image_url": "private-attachment"},
                        ],
                    },
                }
            ),
            _json_line(
                {
                    "type": "response_item",
                    "payload": {
                        "id": "message-assistant-1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Use a bounded parser."}],
                    },
                }
            ),
            _json_line(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "shell",
                        "arguments": f"echo {secret}",
                    },
                }
            ),
            _json_line({"type": "future_event", "payload": {"private": secret}}),
        )
    )


def test_parse_codex_session_filters_and_redacts_with_stable_ids(tmp_path: Path) -> None:
    session_path = tmp_path / "session.jsonl"
    session_path.write_text(_codex_session("thread-1") + '{"unfinished":', encoding="utf-8")

    first = parse_codex_session(session_path)
    second = parse_codex_session(session_path)

    assert first == second
    assert first.document.source_kind is SourceKind.CODEX_SESSION
    assert first.document.external_id == "thread-1"
    assert first.document.parent_external_id == "parent-1"
    assert [chunk.role for chunk in first.chunks] == [
        ContentRole.USER,
        ContentRole.ASSISTANT,
    ]
    rendered = "\n".join(chunk.text for chunk in first.chunks)
    assert "sk-example123456789" not in rendered
    assert "private cwd" not in rendered
    assert "private-attachment" not in rendered
    assert "Use a bounded parser." in rendered


def test_parse_codex_session_accepts_valid_final_record_without_lf(tmp_path: Path) -> None:
    session_path = tmp_path / "session.jsonl"
    final_record = {
        "type": "response_item",
        "payload": {
            "id": "message-assistant-final",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Final record is complete."}],
        },
    }
    session_path.write_text(
        _codex_session("thread-final") + json.dumps(final_record),
        encoding="utf-8",
    )

    parsed = parse_codex_session(session_path)

    assert parsed.chunks[-1].text == "Final record is complete."


def test_codex_long_message_is_split_into_stable_overlapping_chunks(tmp_path: Path) -> None:
    session_path = tmp_path / "session.jsonl"
    long_answer = (
        "frobnicator outage "
        + ("filler " * 1_100)
        + "fixed with a durable queue and idempotency key"
    )
    final_record = {
        "type": "response_item",
        "payload": {
            "id": "message-long",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": long_answer}],
        },
    }
    session_path.write_text(
        _codex_session("thread-long") + json.dumps(final_record),
        encoding="utf-8",
    )

    parsed = parse_codex_session(session_path)
    long_chunks = [
        chunk for chunk in parsed.chunks if chunk.metadata.get("message_id") == "message-long"
    ]

    assert len(long_chunks) > 2
    assert "frobnicator outage" in long_chunks[0].text
    assert "durable queue" in long_chunks[-1].text
    assert all(len(chunk.text.encode("utf-8")) <= 1_200 for chunk in long_chunks)
    assert [chunk.metadata["segment"] for chunk in long_chunks] == [
        str(index) for index in range(len(long_chunks))
    ]


def test_discover_codex_sources_deduplicates_active_and_archived_projection(
    tmp_path: Path,
) -> None:
    active = tmp_path / "sessions" / "2026" / "session.jsonl"
    archived = tmp_path / "archived_sessions" / "session.jsonl"
    active.parent.mkdir(parents=True)
    archived.parent.mkdir(parents=True)
    active.write_text(_codex_session("same-thread"), encoding="utf-8")
    archived.write_text(
        _codex_session("same-thread") + _json_line({"type": "unknown", "payload": {}}),
        encoding="utf-8",
    )

    discovered = discover_codex_sources(tmp_path)

    assert discovered == [archived]
    assert parse_codex_session(discovered[0]).document.external_id == "same-thread"


def test_codex_source_change_is_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(_codex_session("thread-change"), encoding="utf-8")
    original_snapshot = intake._file_snapshot
    calls = 0

    def changing_snapshot(source_path: Path) -> tuple[int, int, int, int, int]:
        nonlocal calls
        calls += 1
        snapshot = original_snapshot(source_path)
        if calls > 1:
            return snapshot[:3] + (snapshot[3] + 1, snapshot[4])
        return snapshot

    monkeypatch.setattr(intake, "_file_snapshot", changing_snapshot)
    with pytest.raises(SourceChangedError, match="source changed") as error:
        parse_codex_session(path)
    assert "thread-change" not in str(error.value)


def test_codex_same_inode_same_size_swap_is_detected_by_ctime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(_codex_session("thread-change"), encoding="utf-8")
    original_open = intake._open_readonly

    def swapping_open(source_path: Path) -> int:
        before = source_path.stat()
        original = source_path.read_bytes()
        changed = original.replace(b"thread-change", b"thread-mutate")
        assert len(changed) == len(original) and changed != original
        source_path.write_bytes(changed)
        os.utime(source_path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return original_open(source_path)

    monkeypatch.setattr(intake, "_open_readonly", swapping_open)

    with pytest.raises(SourceChangedError, match="source changed"):
        parse_codex_session(path)


def _chatgpt_export() -> list[object]:
    return [
        {
            "id": "conversation-1",
            "title": "RAG API_KEY=private-value",
            "create_time": 1_786_240_000,
            "current_node": "assistant",
            "mapping": {
                "root": {
                    "id": "root",
                    "parent": None,
                    "children": [
                        "user",
                        "system",
                        "hidden-node",
                        "hidden-message",
                        "internal-recipient",
                    ],
                    "message": None,
                },
                "system": {
                    "id": "system",
                    "parent": "root",
                    "children": [],
                    "message": {
                        "id": "system-message",
                        "author": {"role": "system"},
                        "content": {"content_type": "text", "parts": ["private system"]},
                    },
                },
                "user": {
                    "id": "user",
                    "parent": "root",
                    "children": ["assistant"],
                    "message": {
                        "id": "user-message",
                        "author": {"role": "user"},
                        "create_time": 1_786_240_001,
                        "content": {
                            "content_type": "multimodal_text",
                            "parts": ["Explain hybrid RAG", {"asset_pointer": "attachment"}],
                        },
                    },
                },
                "assistant": {
                    "id": "assistant",
                    "parent": "user",
                    "children": [],
                    "message": {
                        "id": "assistant-message",
                        "author": {"role": "assistant"},
                        "create_time": 1_786_240_002,
                        "content": {
                            "content_type": "text",
                            "parts": ["Use lexical and semantic channels."],
                        },
                    },
                },
                "hidden-node": {
                    "id": "hidden-node",
                    "parent": "root",
                    "children": [],
                    "metadata": {"is_visually_hidden_from_conversation": True},
                    "message": {
                        "id": "hidden-node-message",
                        "author": {"role": "assistant"},
                        "content": {
                            "content_type": "text",
                            "parts": ["PRIVATE HIDDEN NODE"],
                        },
                    },
                },
                "hidden-message": {
                    "id": "hidden-message",
                    "parent": "root",
                    "children": [],
                    "message": {
                        "id": "hidden-message-message",
                        "author": {"role": "assistant"},
                        "metadata": {"hidden": "true"},
                        "content": {
                            "content_type": "text",
                            "parts": ["PRIVATE HIDDEN MESSAGE"],
                        },
                    },
                },
                "internal-recipient": {
                    "id": "internal-recipient",
                    "parent": "root",
                    "children": [],
                    "message": {
                        "id": "internal-recipient-message",
                        "author": {"role": "assistant"},
                        "recipient": "python",
                        "content": {
                            "content_type": "text",
                            "parts": ["PRIVATE INTERNAL RECIPIENT"],
                        },
                    },
                },
            },
        }
    ]


def test_chatgpt_json_and_zip_normalize_to_same_documents(tmp_path: Path) -> None:
    export_value = _chatgpt_export()
    raw = json.dumps(export_value, ensure_ascii=False)
    json_path = tmp_path / "conversations.json"
    json_path.write_text(raw, encoding="utf-8")
    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nested/conversations.json", raw)
        archive.writestr("user.json", '{"email":"private@example.com"}')

    from_json = parse_chatgpt_export(json_path)
    from_zip = parse_chatgpt_export(zip_path)

    assert len(from_json) == len(from_zip) == 1
    assert from_json[0].document.id == from_zip[0].document.id
    assert from_json[0].document.content_sha256 == from_zip[0].document.content_sha256
    assert [chunk.id for chunk in from_json[0].chunks] == [chunk.id for chunk in from_zip[0].chunks]
    assert [chunk.role for chunk in from_json[0].chunks] == [
        ContentRole.USER,
        ContentRole.ASSISTANT,
    ]
    combined = "\n".join(chunk.text for chunk in from_json[0].chunks)
    assert "private system" not in combined
    assert "attachment" not in combined
    assert "PRIVATE HIDDEN NODE" not in combined
    assert "PRIVATE HIDDEN MESSAGE" not in combined
    assert "PRIVATE INTERNAL RECIPIENT" not in combined
    assert "private-value" not in (from_json[0].document.title or "")


def test_chatgpt_current_node_excludes_conflicting_sibling_branch(tmp_path: Path) -> None:
    export = [
        {
            "id": "branched-conversation",
            "current_node": "current",
            "mapping": {
                "root": {"id": "root", "parent": None, "children": ["u"], "message": None},
                "u": {
                    "id": "u",
                    "parent": "root",
                    "children": ["old", "current"],
                    "message": {
                        "id": "u-message",
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": ["How to frobnicate?"]},
                    },
                },
                "old": {
                    "id": "old",
                    "parent": "u",
                    "children": ["old-followup"],
                    "message": {
                        "id": "old-message",
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": ["Use obsolete branch."]},
                    },
                },
                "old-followup": {
                    "id": "old-followup",
                    "parent": "old",
                    "children": [],
                    "message": {
                        "id": "old-followup-message",
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": ["Obsolete detail."]},
                    },
                },
                "current": {
                    "id": "current",
                    "parent": "u",
                    "children": [],
                    "message": {
                        "id": "current-message",
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": ["Use current safe branch."]},
                    },
                },
            },
        }
    ]
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(export), encoding="utf-8")

    parsed = parse_chatgpt_export(path)[0]
    rendered = "\n".join(chunk.text for chunk in parsed.chunks)

    assert "How to frobnicate?" in rendered
    assert "Use current safe branch." in rendered
    assert "Use obsolete branch." not in rendered
    assert "Obsolete detail." not in rendered


def test_chatgpt_deep_linear_conversation_does_not_recurse(tmp_path: Path) -> None:
    mapping: dict[str, object] = {}
    node_count = 1_200
    for index in range(node_count):
        node_id = f"n{index}"
        parent = f"n{index - 1}" if index else None
        children = [f"n{index + 1}"] if index + 1 < node_count else []
        mapping[node_id] = {
            "id": node_id,
            "parent": parent,
            "children": children,
            "message": {
                "id": f"message-{index}",
                "author": {"role": "user" if index % 2 == 0 else "assistant"},
                "content": {"content_type": "text", "parts": [f"turn {index}"]},
            },
        }
    export = [
        {
            "id": "deep-conversation",
            "current_node": f"n{node_count - 1}",
            "mapping": mapping,
        }
    ]
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(export), encoding="utf-8")

    parsed = parse_chatgpt_export(path)

    assert len(parsed) == 1
    assert len(parsed[0].chunks) == node_count
    assert parsed[0].chunks[-1].text == f"turn {node_count - 1}"


def test_chatgpt_filtered_tool_chain_links_nearest_visible_turns(tmp_path: Path) -> None:
    export = [
        {
            "id": "tool-chain",
            "current_node": "final",
            "mapping": {
                "user": {
                    "id": "user",
                    "parent": None,
                    "children": ["call"],
                    "message": {
                        "id": "user-message",
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": ["INCIDENT_9001?"]},
                    },
                },
                "call": {
                    "id": "call",
                    "parent": "user",
                    "children": ["tool"],
                    "message": {
                        "id": "call-message",
                        "author": {"role": "assistant"},
                        "recipient": "python",
                        "content": {"content_type": "text", "parts": ["private tool call"]},
                    },
                },
                "tool": {
                    "id": "tool",
                    "parent": "call",
                    "children": ["final"],
                    "message": {
                        "id": "tool-message",
                        "author": {"role": "tool"},
                        "content": {"content_type": "text", "parts": ["private tool result"]},
                    },
                },
                "final": {
                    "id": "final",
                    "parent": "tool",
                    "children": [],
                    "message": {
                        "id": "final-message",
                        "author": {"role": "assistant"},
                        "content": {
                            "content_type": "text",
                            "parts": ["DECISIVE visible answer uses a durable queue."],
                        },
                    },
                },
            },
        }
    ]
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(export), encoding="utf-8")

    source = parse_chatgpt_export(path)[0]
    final = next(chunk for chunk in source.chunks if "DECISIVE" in chunk.text)
    store = KnowledgeStore(tmp_path / ".soloscale")
    store.sync([source])
    question = store.search("INCIDENT_9001", limit=1)[0]

    assert final.metadata["parent_node_id"] == "user"
    assert any(
        "DECISIVE visible answer" in hit.excerpt for hit in store.get_neighbors([question.chunk_id])
    )


def test_chatgpt_zip_is_opened_through_checked_file_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = json.dumps(_chatgpt_export(), ensure_ascii=False)
    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("conversations.json", raw)
    real_zip_file = zipfile.ZipFile

    def checked_zip_file(file: Any, *args: Any, **kwargs: Any) -> zipfile.ZipFile:
        assert not isinstance(file, (str, Path))
        return real_zip_file(file, *args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", checked_zip_file)

    parsed = parse_chatgpt_export(zip_path)

    assert len(parsed) == 1


def test_buildlog_only_reads_allowlisted_artifacts(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "run-1"
    run.mkdir(parents=True)
    (run / "run_metadata.json").write_text(
        json.dumps(
            {
                "run_id": "dogfood-001",
                "model": "qwen3:8b",
                "prompt_version": "v2",
            }
        ),
        encoding="utf-8",
    )
    (run / "timeline.json").write_text(
        json.dumps({"events": [{"tool_output": "RAW TIMELINE OUTPUT"}]}),
        encoding="utf-8",
    )
    (run / "03_draft.md").write_text(
        "Draft with Authorization: Bearer private-token-value", encoding="utf-8"
    )
    (run / "events.jsonl").write_text(
        _json_line(
            {
                "event_id": "event-1",
                "stage": "planner",
                "status": "pass",
                "payload": {"stdout": "PRIVATE TOOL OUTPUT"},
                "prompt": "PRIVATE PROMPT",
                "response": "PRIVATE RESPONSE",
                "input": "PRIVATE INPUT",
            }
        )
        + '{"partial":\n'
        + json.dumps(
            {
                "event_id": "event-2",
                "stage": "writer",
                "status": "pass",
                "stdout": "PRIVATE FINAL STDOUT",
            }
        ),
        encoding="utf-8",
    )
    (run / ".env").write_text("PASSWORD=must-never-appear", encoding="utf-8")

    assert discover_buildlog_runs(tmp_path / "runs") == [run]
    parsed = parse_buildlog_run(run)

    assert parsed.document.source_kind is SourceKind.BUILDLOG_RUN
    assert parsed.document.external_id == "dogfood-001"
    assert {chunk.metadata["artifact"] for chunk in parsed.chunks} == {
        "03_draft.md",
        "run_metadata.json",
        "events.jsonl",
    }
    rendered = "\n".join(chunk.text for chunk in parsed.chunks)
    assert "private-token-value" not in rendered
    assert "must-never-appear" not in rendered
    assert "partial" not in rendered
    assert "event-1" in rendered
    assert "event-2" in rendered
    assert "PRIVATE TOOL OUTPUT" not in rendered
    assert "PRIVATE PROMPT" not in rendered
    assert "PRIVATE RESPONSE" not in rendered
    assert "PRIVATE INPUT" not in rendered
    assert "PRIVATE FINAL STDOUT" not in rendered
    assert "RAW TIMELINE OUTPUT" not in rendered


def test_buildlog_projects_plan_evaluation_and_timeline_safe_fields(tmp_path: Path) -> None:
    run = tmp_path / "run-projected"
    run.mkdir()
    (run / "02_plan.json").write_text(
        json.dumps(
            {
                "central_idea": "Explain the evaluator recovery",
                "technical_points": ["Strict structured output"],
                "prompt": "PRIVATE PLAN PROMPT",
            }
        ),
        encoding="utf-8",
    )
    (run / "04_evaluation.json").write_text(
        json.dumps(
            {
                "status": "pass",
                "finding": "evaluator recovery verified",
                "technical_accuracy": 9,
                "raw_response": "PRIVATE EVALUATOR RESPONSE",
            }
        ),
        encoding="utf-8",
    )
    (run / "timeline.json").write_text(
        json.dumps(
            {
                "pipeline_status": "completed",
                "steps": [
                    {
                        "step_name": "evaluator",
                        "status": "completed",
                        "duration_ms": 20,
                        "tool_output": "PRIVATE TOOL OUTPUT",
                    }
                ],
                "payload": "PRIVATE TIMELINE PAYLOAD",
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_buildlog_run(run)
    rendered = "\n".join(chunk.text for chunk in parsed.chunks)

    assert "evaluator recovery verified" in rendered
    assert "Strict structured output" in rendered
    assert "pipeline_status" in rendered
    assert "PRIVATE PLAN PROMPT" not in rendered
    assert "PRIVATE EVALUATOR RESPONSE" not in rendered
    assert "PRIVATE TOOL OUTPUT" not in rendered
    assert "PRIVATE TIMELINE PAYLOAD" not in rendered


def test_buildlog_ignores_truncated_final_event(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "run-truncated"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text(
        _json_line({"event_id": "complete", "status": "pass"}) + '{"event_id":',
        encoding="utf-8",
    )

    parsed = parse_buildlog_run(run)

    assert len(parsed.chunks) == 1
    assert "complete" in parsed.chunks[0].text


def test_redaction_covers_secret_assignments_tokens_and_auto_context() -> None:
    github_token = "github_" + "pat_" + "abcdefghijklmnopqrstuvwxyz123456"
    gitlab_token = "gl" + "pat-" + "abcdefghijklmnopqrstuvwxyz"
    raw = (
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz\n"
        "TOKEN=standalone-token-secret\n"
        "PRIVATE_KEY=standalone-private-secret\n"
        "ACCESS_KEY=standalone-access-secret\n"
        "password: hunter2\n"
        'PASSWORD="two word secret"\n'
        "Authorization: Bearer abcdefghijklmnop\n"
        "Authorization: Basic dXNlcjpwYXNzd29yZA==\n"
        f"{github_token}\n"
        f"{gitlab_token}\n"
        "postgresql://db-user:db-password@example.com/private\n"
        '{"refresh_token":"generic-json-secret"}\n'
        "<skills_instructions>private tools</skills_instructions>\n"
        "<recommended_plugins>private plugins</recommended_plugins>\n"
        "<developer_instructions>private developer</developer_instructions>\n"
        "<system_instructions>private system prompt</system_instructions>\n"
        "<collaboration_mode>private collaboration</collaboration_mode>"
    )

    redacted = redact_text(raw)

    for secret in (
        "sk-abcdefghijklmnopqrstuvwxyz",
        "standalone-token-secret",
        "standalone-private-secret",
        "standalone-access-secret",
        "hunter2",
        "two word secret",
        "abcdefghijklmnop",
        "dXNlcjpwYXNzd29yZA==",
        github_token,
        gitlab_token,
        "db-user",
        "db-password",
        "generic-json-secret",
        "private tools",
        "private plugins",
        "private developer",
        "private system prompt",
        "private collaboration",
    ):
        assert secret not in redacted
    assert redacted.count("[REDACTED") >= 17


def test_buildlog_fallback_identity_is_qualified_by_path(tmp_path: Path) -> None:
    first = tmp_path / "one" / "same-run"
    second = tmp_path / "two" / "same-run"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "03_draft.md").write_text("first", encoding="utf-8")
    (second / "03_draft.md").write_text("second", encoding="utf-8")

    first_parsed = parse_buildlog_run(first)
    second_parsed = parse_buildlog_run(second)

    assert first_parsed.document.external_id != second_parsed.document.external_id
    assert first_parsed.document.id != second_parsed.document.id
