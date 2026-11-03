"""Review Workbench: rule-based evidence status, server-enforced state machine,
append-only versions, optimistic concurrency, and the loopback API.

The sample run is built with the real KnowledgeStore and BoundedEvidenceAgent; only the
language model is replaced by the fixture's scripted structured outputs.
"""

from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soloscale.knowledge_store import KnowledgeStore
from soloscale.review_api import REVIEW_API_BODY_LIMIT, create_app
from soloscale.review_intake import (
    DraftInvalidError,
    ReceiptIntegrityError,
    agent_runs_root,
    load_verified_draft,
)
from soloscale.review_models import (
    BatchConfirmEntry,
    CitationCheck,
    ConfirmRequirement,
    EvidenceStatus,
    ReviewAction,
    ReviewState,
)
from soloscale.review_sample import (
    SAMPLE_FIXTURE,
    SAMPLE_LABEL,
    _parsed_source,
    build_sample_run,
    load_fixture,
)
from soloscale.review_store import DraftChangedError, ReviewStore, ReviewStoreError, item_id_for
from soloscale.review_workflow import (
    DECIDED_STATES,
    PENDING_STATES,
    TRANSITIONS,
    ConfirmBlockedError,
    EvidenceChangedError,
    InvalidTransitionError,
    LowConfidenceBatchError,
    NoChangeError,
    NoteRequiredError,
    StaleRevisionError,
    VersionNotFoundError,
    allowed_actions,
    derive_evidence_status,
    diff_text,
    initial_state,
    plan_transition,
)

# ----------------------------------------------------------------- helpers


@pytest.fixture()
def sample(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / ".soloscale"
    run = build_sample_run(root)
    return root, run.run_id


@pytest.fixture()
def imported(sample: tuple[Path, str]) -> tuple[ReviewStore, str]:
    root, run_id = sample
    store = ReviewStore(root)
    store.import_draft(load_verified_draft(root, run_id))
    return store, run_id


def _item(run_id: str, claim_index: int) -> str:
    return item_id_for(run_id, claim_index)


def _client(root: Path) -> TestClient:
    return TestClient(create_app(root), base_url="http://127.0.0.1")


# ------------------------------------------------------- pure workflow rules


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([CitationCheck.VERIFIED], EvidenceStatus.FULLY_VERIFIED),
        ([CitationCheck.VERIFIED, CitationCheck.VERIFIED], EvidenceStatus.FULLY_VERIFIED),
        ([CitationCheck.VERIFIED, CitationCheck.SOURCE_MISSING], EvidenceStatus.PARTIALLY_VERIFIED),
        ([CitationCheck.DOCUMENT_REVISED], EvidenceStatus.PARTIALLY_VERIFIED),
        ([CitationCheck.SOURCE_MISSING], EvidenceStatus.UNVERIFIED),
        ([CitationCheck.UNCHECKED], EvidenceStatus.UNVERIFIED),
        ([CitationCheck.NOT_IN_RECEIPT], EvidenceStatus.UNVERIFIED),
        ([], EvidenceStatus.UNVERIFIED),
        ([CitationCheck.VERIFIED, CitationCheck.HASH_MISMATCH], EvidenceStatus.HASH_MISMATCH),
        ([CitationCheck.INTEGRITY_FAILED], EvidenceStatus.HASH_MISMATCH),
    ],
)
def test_evidence_status_is_a_documented_rule_not_a_score(
    checks: list[CitationCheck], expected: EvidenceStatus
) -> None:
    assert derive_evidence_status(checks) is expected


def test_only_fully_verified_items_start_outside_needs_review() -> None:
    assert initial_state(EvidenceStatus.FULLY_VERIFIED) is ReviewState.PENDING_REVIEW
    for status in EvidenceStatus:
        if status is not EvidenceStatus.FULLY_VERIFIED:
            assert initial_state(status) is ReviewState.NEEDS_REVIEW


@pytest.mark.parametrize("state", list(ReviewState))
@pytest.mark.parametrize(
    "action", [ReviewAction.CONFIRM, ReviewAction.CORRECT, ReviewAction.REJECT]
)
def test_transition_table_is_the_only_authority(state: ReviewState, action: ReviewAction) -> None:
    target = TRANSITIONS.get((state, action))
    if target is None:
        with pytest.raises(InvalidTransitionError):
            plan_transition(state, action, EvidenceStatus.FULLY_VERIFIED, note="note")
        assert state in DECIDED_STATES
    else:
        assert state in PENDING_STATES
        assert plan_transition(state, action, EvidenceStatus.FULLY_VERIFIED, note="n") is target


def test_correct_reject_and_reopen_require_a_note() -> None:
    for action in (ReviewAction.CORRECT, ReviewAction.REJECT):
        with pytest.raises(NoteRequiredError):
            plan_transition(
                ReviewState.PENDING_REVIEW, action, EvidenceStatus.FULLY_VERIFIED, note=""
            )
    with pytest.raises(NoteRequiredError):
        plan_transition(
            ReviewState.CONFIRMED, ReviewAction.REOPEN, EvidenceStatus.FULLY_VERIFIED, note=""
        )
    with pytest.raises(InvalidTransitionError):
        plan_transition(
            ReviewState.NEEDS_REVIEW, ReviewAction.REOPEN, EvidenceStatus.UNVERIFIED, note="x"
        )


def test_low_confidence_confirm_needs_a_note_and_hash_mismatch_is_blocked() -> None:
    assert (
        plan_transition(
            ReviewState.PENDING_REVIEW,
            ReviewAction.CONFIRM,
            EvidenceStatus.FULLY_VERIFIED,
            note="",
        )
        is ReviewState.CONFIRMED
    )
    for status in (EvidenceStatus.PARTIALLY_VERIFIED, EvidenceStatus.UNVERIFIED):
        with pytest.raises(NoteRequiredError):
            plan_transition(ReviewState.NEEDS_REVIEW, ReviewAction.CONFIRM, status, note="")
        assert (
            plan_transition(
                ReviewState.NEEDS_REVIEW, ReviewAction.CONFIRM, status, note="checked manually"
            )
            is ReviewState.CONFIRMED
        )
    with pytest.raises(ConfirmBlockedError):
        plan_transition(
            ReviewState.NEEDS_REVIEW,
            ReviewAction.CONFIRM,
            EvidenceStatus.HASH_MISMATCH,
            note="I am sure",
        )
    assert ReviewAction.CONFIRM not in allowed_actions(
        ReviewState.NEEDS_REVIEW, EvidenceStatus.HASH_MISMATCH
    )
    assert allowed_actions(ReviewState.REJECTED, EvidenceStatus.FULLY_VERIFIED) == [
        ReviewAction.REOPEN
    ]


def test_reopen_returns_to_the_evidence_based_initial_state() -> None:
    assert (
        plan_transition(
            ReviewState.CORRECTED, ReviewAction.REOPEN, EvidenceStatus.HASH_MISMATCH, note="x"
        )
        is ReviewState.NEEDS_REVIEW
    )
    assert (
        plan_transition(
            ReviewState.CONFIRMED, ReviewAction.REOPEN, EvidenceStatus.FULLY_VERIFIED, note="x"
        )
        is ReviewState.PENDING_REVIEW
    )


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("The API used a queue.", "The API used a durable queue and idempotency key."),
        ("使用持久队列和幂等键。", "使用持久队列，但没有幂等键。"),
        ("same text", "same text"),
        ("", "new"),
    ],
)
def test_diff_reconstructs_both_versions(before: str, after: str) -> None:
    ops = diff_text(before, after)
    assert "".join(op.text for op in ops if op.op != "insert") == before
    assert "".join(op.text for op in ops if op.op != "delete") == after


# --------------------------------------------- intake over the real pipeline


def test_sample_run_uses_real_receipts_and_is_labeled(sample: tuple[Path, str]) -> None:
    root, run_id = sample
    run_dir = agent_runs_root(root) / run_id
    assert {"00_input.json", "03_retrieval_manifest.json", "04_result.json"} <= {
        path.name for path in run_dir.iterdir()
    }
    draft = load_verified_draft(root, run_id)
    assert draft.result.status == "CANDIDATE_REQUIRES_HUMAN_CONFIRMATION"
    assert SAMPLE_LABEL in draft.result.question
    assert draft.result.model.startswith("fixture/")
    assert draft.knowledge_index_checked is True
    assert all(
        reference.title.startswith("Sample:") for reference in draft.result.refs if reference.title
    )


def test_citation_drift_is_detected_against_the_current_index(sample: tuple[Path, str]) -> None:
    root, run_id = sample
    expected = load_fixture()["expected_evidence_status"]
    draft = load_verified_draft(root, run_id)

    assert {str(claim.claim_index): claim.evidence_status.value for claim in draft.claims} == (
        expected
    )
    mismatch = draft.claims[2].citations[0]
    assert mismatch.current.check is CitationCheck.HASH_MISMATCH
    assert mismatch.recorded.chunk_sha256 != mismatch.current.current_chunk_sha256
    # The excerpt the model saw is preserved from the receipt, not re-read from the index.
    assert mismatch.reference is not None
    assert "never requeued" in mismatch.reference.excerpt
    assert draft.claims[3].citations[0].current.check is CitationCheck.DOCUMENT_REVISED
    assert draft.claims[4].citations[0].current.check is CitationCheck.SOURCE_MISSING


def test_without_an_index_nothing_counts_as_verified(sample: tuple[Path, str]) -> None:
    root, run_id = sample
    (root / "knowledge" / "index.sqlite3").unlink()
    draft = load_verified_draft(root, run_id)
    assert draft.knowledge_index_checked is False
    assert {claim.evidence_status for claim in draft.claims} == {EvidenceStatus.UNVERIFIED}


def test_tampered_model_visible_excerpt_fails_closed(sample: tuple[Path, str]) -> None:
    root, run_id = sample
    result_path = agent_runs_root(root) / run_id / "04_result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["refs"][0]["excerpt"] = "An excerpt the model never saw."
    result_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReceiptIntegrityError, match="model-visible"):
        load_verified_draft(root, run_id)


def test_tampered_retrieval_manifest_fails_closed(sample: tuple[Path, str]) -> None:
    root, run_id = sample
    manifest_path = agent_runs_root(root) / run_id / "03_retrieval_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["queries"].append("injected")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReceiptIntegrityError, match="retrieval manifest hash"):
        load_verified_draft(root, run_id)


def test_failed_agent_run_cannot_be_imported(sample: tuple[Path, str]) -> None:
    root, run_id = sample
    (agent_runs_root(root) / run_id / "failure.json").write_text("{}", encoding="utf-8")
    with pytest.raises(DraftInvalidError):
        load_verified_draft(root, run_id)


# ------------------------------------------------------------- store rules


def test_import_routes_low_confidence_items_to_needs_review(
    imported: tuple[ReviewStore, str],
) -> None:
    store, run_id = imported
    items = store.list_items(run_id=run_id)
    states = {item.claim_index: item.state for item in items}
    assert states == {
        0: ReviewState.PENDING_REVIEW,
        1: ReviewState.PENDING_REVIEW,
        2: ReviewState.NEEDS_REVIEW,
        3: ReviewState.NEEDS_REVIEW,
        4: ReviewState.NEEDS_REVIEW,
    }
    # Queue order: needs review first, weakest evidence first.
    assert [item.claim_index for item in items] == [2, 4, 3, 0, 1]
    assert items[0].confirm_requirement is ConfirmRequirement.BLOCKED
    assert all(item.revision == 1 and item.current_version == 1 for item in items)


def test_reimport_is_idempotent_and_detects_a_changed_receipt(
    sample: tuple[Path, str],
) -> None:
    root, run_id = sample
    store = ReviewStore(root)
    created, _, first = store.import_draft(load_verified_draft(root, run_id))
    replayed, _, second = store.import_draft(load_verified_draft(root, run_id))
    assert (created, replayed) == (True, False)
    assert [item.item_id for item in first] == [item.item_id for item in second]

    result_path = agent_runs_root(root) / run_id / "04_result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["limitations"].append("edited after import")
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DraftChangedError):
        store.import_draft(load_verified_draft(root, run_id))


def test_corrections_append_versions_and_keep_the_original(
    imported: tuple[ReviewStore, str],
) -> None:
    store, run_id = imported
    item_id = _item(run_id, 3)
    original = store.get_item(item_id).original_text

    first = store.apply_action(
        item_id,
        ReviewAction.CORRECT,
        expected_revision=1,
        reviewer="tester",
        note="add the scope limit",
        text=f"{original} Citation membership is structural only.",
    )
    assert first.item.state is ReviewState.CORRECTED
    reopened = store.apply_action(
        item_id,
        ReviewAction.REOPEN,
        expected_revision=2,
        reviewer="tester",
        note="tighten wording",
    )
    assert reopened.item.state is ReviewState.NEEDS_REVIEW
    assert reopened.item.current_version == 2
    final = store.apply_action(
        item_id,
        ReviewAction.CORRECT,
        expected_revision=3,
        reviewer="tester",
        note="final wording",
        text="Claims must cite an in-context chunk from the same run (structural check).",
    )

    assert final.original_text == original
    assert [version.version for version in final.versions] == [1, 2, 3]
    assert final.versions[0].author_kind.value == "MODEL"
    assert final.versions[0].text == original
    assert final.item.current_version == 3 and final.item.revision == 4
    assert [event.action.value for event in final.events] == [
        "IMPORTED",
        "CORRECTED",
        "REOPENED",
        "CORRECTED",
    ]
    assert [event.to_revision for event in final.events] == [1, 2, 3, 4]

    whole = store.diff(item_id)
    assert (whole.from_version, whole.to_version) == (1, 3)
    assert "".join(op.text for op in whole.ops if op.op != "insert") == original
    step = store.diff(item_id, from_version=2, to_version=3)
    assert step.from_text == final.versions[1].text
    with pytest.raises(VersionNotFoundError):
        store.diff(item_id, from_version=9)


def test_stale_revision_is_rejected_without_writing(imported: tuple[ReviewStore, str]) -> None:
    store, run_id = imported
    item_id = _item(run_id, 0)
    loaded_by_a = store.get_item(item_id).item.revision
    loaded_by_b = store.get_item(item_id).item.revision

    store.apply_action(
        item_id,
        ReviewAction.CORRECT,
        expected_revision=loaded_by_a,
        reviewer="reviewer-a",
        note="clarify",
        text="Reviewer A wording.",
    )
    with pytest.raises(StaleRevisionError) as caught:
        store.apply_action(
            item_id,
            ReviewAction.CORRECT,
            expected_revision=loaded_by_b,
            reviewer="reviewer-b",
            note="clarify",
            text="Reviewer B wording that would overwrite A.",
        )
    assert caught.value.details["current_revision"] == 2
    detail = store.get_item(item_id)
    assert detail.item.current_text == "Reviewer A wording."
    assert len(detail.versions) == 2 and len(detail.events) == 2


def test_low_confidence_items_cannot_be_auto_confirmed(imported: tuple[ReviewStore, str]) -> None:
    store, run_id = imported
    verified, unverified, mismatch = _item(run_id, 0), _item(run_id, 4), _item(run_id, 2)

    with pytest.raises(LowConfidenceBatchError):
        store.confirm_batch(
            [
                BatchConfirmEntry(item_id=verified, expected_revision=1),
                BatchConfirmEntry(item_id=unverified, expected_revision=1),
            ],
            reviewer="tester",
            note="",
        )
    # The batch is atomic: the verified item in the same request was not confirmed.
    assert store.get_item(verified).item.state is ReviewState.PENDING_REVIEW

    with pytest.raises(NoteRequiredError):
        store.apply_action(
            unverified, ReviewAction.CONFIRM, expected_revision=1, reviewer="t", note=""
        )
    with pytest.raises(ConfirmBlockedError):
        store.apply_action(
            mismatch, ReviewAction.CONFIRM, expected_revision=1, reviewer="t", note="trust me"
        )
    confirmed = store.apply_action(
        unverified,
        ReviewAction.CONFIRM,
        expected_revision=1,
        reviewer="t",
        note="Checked the current port setting by hand.",
    )
    assert confirmed.item.state is ReviewState.CONFIRMED
    assert confirmed.events[-1].note == "Checked the current port setting by hand."

    batch = store.confirm_batch(
        [BatchConfirmEntry(item_id=verified, expected_revision=1)], reviewer="t", note=""
    )
    assert batch[0].state is ReviewState.CONFIRMED


def test_invalid_transitions_are_rejected_by_the_store(imported: tuple[ReviewStore, str]) -> None:
    store, run_id = imported
    item_id = _item(run_id, 1)
    store.apply_action(
        item_id, ReviewAction.REJECT, expected_revision=1, reviewer="t", note="not relevant"
    )
    with pytest.raises(InvalidTransitionError):
        store.apply_action(
            item_id, ReviewAction.CONFIRM, expected_revision=2, reviewer="t", note=""
        )
    with pytest.raises(InvalidTransitionError):
        store.apply_action(
            item_id,
            ReviewAction.CORRECT,
            expected_revision=2,
            reviewer="t",
            note="x",
            text="new text",
        )
    assert store.get_item(item_id).item.state is ReviewState.REJECTED
    with pytest.raises(NoChangeError):
        pending = _item(run_id, 0)
        store.apply_action(
            pending,
            ReviewAction.CORRECT,
            expected_revision=1,
            reviewer="t",
            note="x",
            text=store.get_item(pending).item.current_text,
        )


def test_confirm_rechecks_evidence_that_changed_after_import(
    imported: tuple[ReviewStore, str],
) -> None:
    store, run_id = imported
    fixture = load_fixture()
    api_doc = next(doc for doc in fixture["documents"] if doc["id"] == "sample-doc-resume-api")
    api_doc["chunks"][1]["text"] = "Rewritten after import: the limit now trusts Content-Length."
    report = KnowledgeStore(store.data_root).sync([_parsed_source(api_doc, 99)])
    assert report.failed == 0

    item_id = _item(run_id, 0)
    with pytest.raises(EvidenceChangedError) as caught:
        store.apply_action(
            item_id, ReviewAction.CONFIRM, expected_revision=1, reviewer="t", note=""
        )
    assert caught.value.details["current_check"] == "HASH_MISMATCH"
    assert store.get_item(item_id).item.state is ReviewState.PENDING_REVIEW
    # The reviewer can still correct or reject it.
    rejected = store.apply_action(
        item_id, ReviewAction.REJECT, expected_revision=1, reviewer="t", note="source changed"
    )
    assert rejected.item.state is ReviewState.REJECTED


def test_schema_triggers_protect_history_from_any_writer(
    imported: tuple[ReviewStore, str],
) -> None:
    store, run_id = imported
    item_id = _item(run_id, 2)
    connection = sqlite3.connect(store.database_path)
    try:
        for statement, message in (
            ("UPDATE review_text_versions SET text = 'x' WHERE item_id = ?", "append-only"),
            ("DELETE FROM review_events WHERE item_id = ?", "review log is append-only"),
            ("DELETE FROM review_citations WHERE item_id = ?", "append-only"),
            (
                (
                    "UPDATE review_items SET state = 'CONFIRMED', revision = revision + 1 "
                    "WHERE item_id = ?"
                ),
                "evidence_status <> 'HASH_MISMATCH'",
            ),
            (
                "UPDATE review_items SET revision = revision + 5 WHERE item_id = ?",
                "advance by exactly one",
            ),
            (
                (
                    "UPDATE review_items SET evidence_status = 'FULLY_VERIFIED', "
                    "revision = revision + 1 WHERE item_id = ?"
                ),
                "evidence status are immutable",
            ),
            (
                (
                    "UPDATE review_items SET current_version = 7, state = 'CORRECTED', "
                    "revision = revision + 1 WHERE item_id = ?"
                ),
                "must reference a stored text version",
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError, match=message):
                connection.execute(statement, (item_id,))
    finally:
        connection.close()
    assert stat.S_IMODE(store.database_path.stat().st_mode) == 0o600


def test_unsupported_schema_version_is_refused(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / ".soloscale")
    connection = sqlite3.connect(store.database_path)
    connection.execute("PRAGMA user_version = 99")
    connection.close()
    with pytest.raises(ReviewStoreError):
        ReviewStore(tmp_path / ".soloscale")


# ----------------------------------------------------------------- HTTP API


def test_api_end_to_end_with_server_side_rejections(sample: tuple[Path, str]) -> None:
    root, run_id = sample
    client = _client(root)

    health = client.get("/health").json()
    assert health["model_calls"] == 0

    drafts = client.get("/review/drafts").json()
    assert [draft["run_id"] for draft in drafts["drafts"]] == [run_id]
    assert drafts["drafts"][0]["imported"] is False

    created = client.post("/review/imports", json={"run_id": run_id})
    assert created.status_code == 201
    assert created.json()["run"]["gaps"]["unsupported"]
    replay = client.post("/review/imports", json={"run_id": run_id})
    assert replay.status_code == 200 and replay.json()["created"] is False

    queue = client.get("/review/items").json()["items"]
    assert queue[0]["evidence_status"] == "HASH_MISMATCH"
    assert "confirm" not in queue[0]["allowed_actions"]
    needs = client.get("/review/items", params={"state": "NEEDS_REVIEW"}).json()["items"]
    assert {item["state"] for item in needs} == {"NEEDS_REVIEW"} and len(needs) == 3

    verified_id = _item(run_id, 0)
    confirmed = client.post(f"/review/items/{verified_id}/confirm", json={"expected_revision": 1})
    assert confirmed.status_code == 200
    assert confirmed.json()["item"]["state"] == "CONFIRMED"

    invalid = client.post(f"/review/items/{verified_id}/confirm", json={"expected_revision": 2})
    assert invalid.status_code == 409
    assert invalid.json()["error"]["code"] == "INVALID_TRANSITION"

    revised_id = _item(run_id, 3)
    corrected = client.post(
        f"/review/items/{revised_id}/correct",
        json={"expected_revision": 1, "text": "Corrected claim text.", "note": "scope"},
    )
    assert corrected.status_code == 200
    detail = corrected.json()
    assert detail["item"]["state"] == "CORRECTED"
    assert detail["original_text"] != "Corrected claim text."
    assert [version["author_kind"] for version in detail["versions"]] == ["MODEL", "REVIEWER"]

    stale = client.post(
        f"/review/items/{revised_id}/reopen", json={"expected_revision": 1, "note": "again"}
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "STALE_REVISION"
    assert stale.json()["error"]["current_revision"] == 2

    diff = client.get(f"/review/items/{revised_id}/diff").json()
    assert diff["to_text"] == "Corrected claim text."
    assert {op["op"] for op in diff["ops"]} >= {"delete", "insert"}

    mismatch_id = _item(run_id, 2)
    blocked = client.post(
        f"/review/items/{mismatch_id}/confirm", json={"expected_revision": 1, "note": "sure"}
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "CONFIRM_BLOCKED_HASH_MISMATCH"
    no_note = client.post(f"/review/items/{mismatch_id}/reject", json={"expected_revision": 1})
    assert no_note.status_code == 422
    assert no_note.json()["error"]["code"] == "NOTE_REQUIRED"
    empty_text = client.post(
        f"/review/items/{mismatch_id}/correct",
        json={"expected_revision": 1, "text": "   ", "note": "x"},
    )
    assert empty_text.status_code == 422
    assert empty_text.json()["error"]["code"] == "INVALID_REQUEST"

    batch = client.post(
        "/review/batch/confirm",
        json={"items": [{"item_id": _item(run_id, 4), "expected_revision": 1}]},
    )
    assert batch.status_code == 409
    assert batch.json()["error"]["code"] == "LOW_CONFIDENCE_REQUIRES_INDIVIDUAL_REVIEW"

    assert client.get("/review/items/missing").status_code == 404
    unknown_run = client.post("/review/imports", json={"run_id": "../../etc"})
    assert unknown_run.status_code == 404
    assert unknown_run.json()["error"]["code"] == "DRAFT_NOT_FOUND"

    run_summary = client.get(f"/review/runs/{run_id}").json()
    assert run_summary["state_counts"]["CONFIRMED"] == 1
    assert run_summary["state_counts"]["CORRECTED"] == 1


def test_api_rejects_foreign_hosts_and_oversized_bodies(sample: tuple[Path, str]) -> None:
    root, _run_id = sample
    foreign = TestClient(create_app(root), base_url="http://attacker.example")
    assert foreign.get("/health").status_code == 400

    client = _client(root)
    oversized = client.post(
        "/review/imports",
        content=b"{" + b" " * (REVIEW_API_BODY_LIMIT + 1) + b"}",
        headers={"Content-Type": "application/json"},
    )
    assert oversized.status_code == 413

    preflight = client.options(
        "/review/items",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_sample_fixture_is_repo_owned_and_labeled() -> None:
    fixture = load_fixture(SAMPLE_FIXTURE)
    assert "SAMPLE FIXTURE" in fixture["provenance"]
    assert all(doc["title"].startswith("Sample:") for doc in fixture["documents"])
    assert all(doc["locator"].startswith("sample://") for doc in fixture["documents"])
