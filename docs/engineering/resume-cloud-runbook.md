# Resume Cloud runbook

This service accepts a base64 resume and JD only through an authenticated API. It stores normalized DOCX/JD inputs in the new dedicated PostgreSQL database, processes an explicit queued task, and exposes a short-lived DOCX download. Template mode is deterministic and returns `model_calls: 0`; AI mode requires both `approve_external_model_call: true` and a configured explicit cloud provider.

Set `RESUME_CLOUD_DATABASE_URL` and either `RESUME_CLOUD_BEARER_TOKEN` or `RESUME_CLOUD_BEARER_TOKEN_FILE`. The bearer token must have at least 32 characters. Run `python scripts/migrate_resume_cloud.py` against a new empty database. The migration refuses unrelated user relations in any non-system schema or an unknown schema version.

Run the API with `python -m soloscale.resume_cloud_api` and a worker with `python -m soloscale.resume_cloud_worker`. `GET /health/live` is public and empty (204); `GET /health/ready`, task routes, and downloads require `Authorization: Bearer <token>`. Compose exposes only `127.0.0.1:8080`. Set `RESUME_CLOUD_SECRET_DIR` to an external directory owned by the container UID, mode `0700`, containing `postgres_password` and `api_bearer_token` files mode `0600`; never place those files in the repository. Set `RESUME_CLOUD_UID` and `RESUME_CLOUD_GID` when local host ownership differs from the image's default `10001:10001`.

After the API and worker are running, `RESUME_CLOUD_BEARER_TOKEN=... python scripts/demo_resume_cloud.py` exercises the full flow with a synthetic template-only resume and checks that the returned file is DOCX data.

The worker claims with `FOR UPDATE SKIP LOCKED`, permits one attempt, and writes a token before generation. A lease-expired RUNNING task becomes `NEEDS_REVIEW` and is never requeued, because a paid provider may have received the call. Terminal cleanup clears inputs; output also expires. Logs carry task IDs/state and exception types only.
