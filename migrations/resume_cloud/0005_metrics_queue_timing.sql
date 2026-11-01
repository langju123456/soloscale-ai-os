SET search_path TO public;
ALTER TABLE resume_cloud_schema_version DROP CONSTRAINT resume_cloud_schema_version_version_check;
ALTER TABLE resume_cloud_schema_version ADD CONSTRAINT resume_cloud_schema_version_version_check CHECK (version IN (1, 2, 3, 4, 5));
ALTER TABLE resume_cloud_tasks ADD COLUMN IF NOT EXISTS queued_at TIMESTAMPTZ;
