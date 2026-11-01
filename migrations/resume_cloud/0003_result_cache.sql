SET search_path TO public;

ALTER TABLE resume_cloud_schema_version
    DROP CONSTRAINT resume_cloud_schema_version_version_check;
ALTER TABLE resume_cloud_schema_version
    ADD CONSTRAINT resume_cloud_schema_version_version_check CHECK (version IN (1, 2, 3));

ALTER TABLE resume_cloud_tasks
    ADD COLUMN client_key CHAR(64),
    ADD COLUMN raw_resume_sha256 CHAR(64),
    ADD COLUMN job_description_sha256 CHAR(64);

CREATE TABLE resume_cloud_result_cache (
    client_key CHAR(64) NOT NULL,
    raw_resume_sha256 CHAR(64) NOT NULL,
    job_description_sha256 CHAR(64) NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('template', 'ai')),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    base_url_sha256 CHAR(64) NOT NULL,
    pipeline_sha256 CHAR(64) NOT NULL,
    output_docx BYTEA NOT NULL,
    output_sha256 CHAR(64) NOT NULL,
    output_expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (client_key, raw_resume_sha256, job_description_sha256, mode, provider,
        model, base_url_sha256, pipeline_sha256)
);
CREATE INDEX resume_cloud_result_cache_expiry_idx
    ON resume_cloud_result_cache (output_expires_at);
