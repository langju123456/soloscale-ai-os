-- Review Workbench schema v1 (local single-operator SQLite).
-- Applied atomically by ReviewStore to an empty database, then PRAGMA user_version = 1.
-- Invariants that must hold for every writer are enforced here, not only in Python:
--   * runs, citation snapshots, text versions and events are append-only;
--   * an item's revision advances by exactly one per update (optimistic concurrency);
--   * an item's identity and evidence status never change after import;
--   * version 1 is always the model's original text.

CREATE TABLE review_runs (
    run_id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    draft_created_at TEXT NOT NULL,
    draft_sha256 TEXT NOT NULL CHECK (length(draft_sha256) = 64),
    input_manifest_sha256 TEXT NOT NULL,
    retrieval_manifest_sha256 TEXT NOT NULL,
    unsupported_json TEXT NOT NULL,
    open_questions_json TEXT NOT NULL,
    limitations_json TEXT NOT NULL,
    knowledge_index_checked INTEGER NOT NULL CHECK (knowledge_index_checked IN (0, 1)),
    imported_at TEXT NOT NULL
);

CREATE TABLE review_items (
    item_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES review_runs(run_id),
    claim_index INTEGER NOT NULL CHECK (claim_index >= 0),
    state TEXT NOT NULL CHECK (
        state IN ('PENDING_REVIEW', 'NEEDS_REVIEW', 'CONFIRMED', 'CORRECTED', 'REJECTED')
    ),
    evidence_status TEXT NOT NULL CHECK (
        evidence_status IN (
            'FULLY_VERIFIED', 'PARTIALLY_VERIFIED', 'UNVERIFIED', 'HASH_MISMATCH'
        )
    ),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    current_version INTEGER NOT NULL CHECK (current_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, claim_index),
    -- Only a fully verified claim may start (or be reopened) as plain PENDING_REVIEW.
    CHECK (state <> 'PENDING_REVIEW' OR evidence_status = 'FULLY_VERIFIED'),
    -- A model claim whose cited source changed can never be accepted verbatim.
    CHECK (state <> 'CONFIRMED' OR evidence_status <> 'HASH_MISMATCH')
);

CREATE TABLE review_text_versions (
    item_id TEXT NOT NULL REFERENCES review_items(item_id),
    version INTEGER NOT NULL CHECK (version >= 1),
    text TEXT NOT NULL,
    text_sha256 TEXT NOT NULL CHECK (length(text_sha256) = 64),
    author_kind TEXT NOT NULL CHECK (author_kind IN ('MODEL', 'REVIEWER')),
    reviewer TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (item_id, version),
    CHECK ((version = 1) = (author_kind = 'MODEL')),
    CHECK (author_kind = 'MODEL' OR (reviewer IS NOT NULL AND note IS NOT NULL AND note <> ''))
);

CREATE TABLE review_citations (
    item_id TEXT NOT NULL REFERENCES review_items(item_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    chunk_id TEXT NOT NULL,
    document_id TEXT,
    source_kind TEXT,
    external_id TEXT,
    title TEXT,
    role TEXT,
    source_timestamp TEXT,
    recorded_chunk_sha256 TEXT,
    recorded_document_sha256 TEXT,
    model_visible_excerpt TEXT,
    check_result TEXT NOT NULL CHECK (
        check_result IN (
            'VERIFIED', 'DOCUMENT_REVISED', 'HASH_MISMATCH', 'INTEGRITY_FAILED',
            'SOURCE_MISSING', 'NOT_IN_RECEIPT', 'UNCHECKED'
        )
    ),
    current_chunk_sha256 TEXT,
    current_document_sha256 TEXT,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (item_id, ordinal)
);

CREATE TABLE review_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL REFERENCES review_items(item_id),
    action TEXT NOT NULL CHECK (
        action IN ('IMPORTED', 'CONFIRMED', 'CORRECTED', 'REJECTED', 'REOPENED')
    ),
    from_state TEXT,
    to_state TEXT NOT NULL,
    from_revision INTEGER,
    to_revision INTEGER NOT NULL CHECK (to_revision >= 1),
    text_version INTEGER NOT NULL CHECK (text_version >= 1),
    reviewer TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    CHECK ((action = 'IMPORTED') = (from_state IS NULL)),
    CHECK (from_revision IS NULL OR to_revision = from_revision + 1)
);

CREATE INDEX idx_review_items_state ON review_items(state, evidence_status);
CREATE INDEX idx_review_items_run ON review_items(run_id, claim_index);
CREATE INDEX idx_review_events_item ON review_events(item_id, event_id);

CREATE TRIGGER review_runs_append_only_update BEFORE UPDATE ON review_runs
BEGIN
    SELECT RAISE(ABORT, 'review runs are append-only');
END;

CREATE TRIGGER review_runs_append_only_delete BEFORE DELETE ON review_runs
BEGIN
    SELECT RAISE(ABORT, 'review runs are append-only');
END;

CREATE TRIGGER review_items_no_delete BEFORE DELETE ON review_items
BEGIN
    SELECT RAISE(ABORT, 'review items cannot be deleted');
END;

-- Import can only create an undecided item at revision 1 showing the model's text.
CREATE TRIGGER review_items_insert_undecided BEFORE INSERT ON review_items
WHEN NEW.state NOT IN ('PENDING_REVIEW', 'NEEDS_REVIEW')
    OR NEW.revision <> 1
    OR NEW.current_version <> 1
BEGIN
    SELECT RAISE(ABORT, 'review items must be imported undecided at revision 1');
END;

-- Coarse backstop for the Python state machine: every update decides an undecided
-- item or reopens a decided one.  There is no decided-to-decided edge.
CREATE TRIGGER review_items_state_edges BEFORE UPDATE ON review_items
WHEN NOT (
    (OLD.state IN ('PENDING_REVIEW', 'NEEDS_REVIEW')
        AND NEW.state IN ('CONFIRMED', 'CORRECTED', 'REJECTED'))
    OR (OLD.state IN ('CONFIRMED', 'CORRECTED', 'REJECTED')
        AND NEW.state IN ('PENDING_REVIEW', 'NEEDS_REVIEW'))
)
BEGIN
    SELECT RAISE(ABORT, 'review item state change is not an allowed edge');
END;

CREATE TRIGGER review_items_identity_immutable BEFORE UPDATE ON review_items
WHEN NEW.item_id <> OLD.item_id
    OR NEW.run_id <> OLD.run_id
    OR NEW.claim_index <> OLD.claim_index
    OR NEW.evidence_status <> OLD.evidence_status
    OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'review item identity and evidence status are immutable');
END;

CREATE TRIGGER review_items_revision_step BEFORE UPDATE ON review_items
WHEN NEW.revision <> OLD.revision + 1
BEGIN
    SELECT RAISE(ABORT, 'review item revision must advance by exactly one');
END;

CREATE TRIGGER review_items_current_version_exists BEFORE UPDATE ON review_items
WHEN NOT EXISTS (
    SELECT 1 FROM review_text_versions
    WHERE item_id = NEW.item_id AND version = NEW.current_version
)
BEGIN
    SELECT RAISE(ABORT, 'review item current version must reference a stored text version');
END;

CREATE TRIGGER review_text_versions_append_only_update BEFORE UPDATE ON review_text_versions
BEGIN
    SELECT RAISE(ABORT, 'review text versions are append-only');
END;

CREATE TRIGGER review_text_versions_append_only_delete BEFORE DELETE ON review_text_versions
BEGIN
    SELECT RAISE(ABORT, 'review text versions are append-only');
END;

CREATE TRIGGER review_text_versions_contiguous BEFORE INSERT ON review_text_versions
WHEN NEW.version <> 1 + COALESCE(
    (SELECT MAX(version) FROM review_text_versions WHERE item_id = NEW.item_id), 0
)
BEGIN
    SELECT RAISE(ABORT, 'review text versions must be contiguous');
END;

CREATE TRIGGER review_citations_append_only_update BEFORE UPDATE ON review_citations
BEGIN
    SELECT RAISE(ABORT, 'review citation snapshots are append-only');
END;

CREATE TRIGGER review_citations_append_only_delete BEFORE DELETE ON review_citations
BEGIN
    SELECT RAISE(ABORT, 'review citation snapshots are append-only');
END;

CREATE TRIGGER review_events_append_only_update BEFORE UPDATE ON review_events
BEGIN
    SELECT RAISE(ABORT, 'review log is append-only');
END;

CREATE TRIGGER review_events_append_only_delete BEFORE DELETE ON review_events
BEGIN
    SELECT RAISE(ABORT, 'review log is append-only');
END;
