SET search_path TO public;

ALTER TABLE resume_cloud_schema_version
    DROP CONSTRAINT resume_cloud_schema_version_version_check;
ALTER TABLE resume_cloud_schema_version
    ADD CONSTRAINT resume_cloud_schema_version_version_check CHECK (version IN (1, 2));

CREATE TABLE resume_cloud_rate_limits (
    client_id TEXT PRIMARY KEY,
    tokens DOUBLE PRECISION NOT NULL CHECK (tokens >= 0),
    updated_at TIMESTAMPTZ NOT NULL,
    last_allowed BOOLEAN NOT NULL
);
