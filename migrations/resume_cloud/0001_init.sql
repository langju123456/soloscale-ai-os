SET search_path TO public;

CREATE TABLE resume_cloud_schema_version (
    version INTEGER PRIMARY KEY CHECK (version = 1),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE resume_cloud_tasks (
    id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_sha256 CHAR(64) NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PREPARED', 'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'NEEDS_REVIEW')),
    mode TEXT NOT NULL CHECK (mode IN ('template', 'ai')),
    approve_external_model_call BOOLEAN NOT NULL DEFAULT FALSE,
    resume_docx BYTEA,
    job_description TEXT,
    input_expires_at TIMESTAMPTZ NOT NULL,
    output_docx BYTEA,
    output_sha256 CHAR(64),
    output_expires_at TIMESTAMPTZ,
    attempt_count SMALLINT NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 1),
    model_call_attempted BOOLEAN NOT NULL DEFAULT FALSE,
    worker_token UUID,
    lease_expires_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    failure_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX resume_cloud_tasks_claim_idx ON resume_cloud_tasks (state, created_at)
    WHERE state = 'QUEUED';
CREATE INDEX resume_cloud_tasks_expiry_idx ON resume_cloud_tasks (input_expires_at, output_expires_at);
INSERT INTO resume_cloud_schema_version (version) VALUES (1) ON CONFLICT DO NOTHING;
