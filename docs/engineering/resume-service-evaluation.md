# Local Resume service evaluation

`scripts/evaluate_resume_service.py` exercises the existing FastAPI, PostgreSQL task repository,
and worker with synthetic template-only resumes. It makes no model calls and is opt-in; CI runs
only the offline guard regressions.

## Run

Provide `RESUME_CLOUD_DATABASE_URL` through the local environment for a new, disposable,
otherwise empty database named `resume_service_eval` or `resume_service_eval_<alphanumeric>`.
Only literal `127.0.0.1` and `::1` PostgreSQL URI hosts are accepted; remote hosts, URI query
overrides, resolver-dependent `localhost`, and existing user relations are rejected. The runner
applies the existing approved schema only after confirming that the dedicated database is empty.
It never creates or drops databases and never truncates an existing task table.

```bash
.venv/bin/python scripts/evaluate_resume_service.py --approve-disposable-database
```

The runner starts its own API on an ephemeral `127.0.0.1` port and two production worker
processes. It creates and queues 20 synthetic tasks over HTTP, replays one idempotency key,
rejects a changed payload for that key, and checks every returned DOCX against its persisted
hash and candidate identity. Database attempt counts and per-worker task logs must show one
completion per task and participation by both workers.

## Interruption experiment

A separate harness child invokes the production worker with a wrapper around the service's
module-global `tailor_resume_docx`. Its marker is emitted after the real repository claim and
before generation. Only this child gets a shortened three-second lease; ordinary batch and
replacement workers keep production settings. No production source is changed, and the
harness does not directly alter database timestamps.

The parent verifies database time and a live RUNNING lease, kills only that spawned child,
reaps it, and observes database time again to prove interruption before lease expiry. A new
production worker then observes natural lease expiry and moves the task to `NEEDS_REVIEW`.
The interrupted task is not requeued; a fresh task completes successfully after restart.
This demonstrates conservative failure handling, not automatic completion of interrupted work.

## Reports and limits

Each fresh private directory under `.soloscale/resume-service-evals/` contains JSON/Markdown
reports and process logs, with directory mode `0700` and file mode `0600`. Reports retain source
HEAD/dirty state/hashes, real process IDs, task states, attempts, model-call flags, output checks,
per-task elapsed time, and the explicit harness injections. Credentials and raw resumes are not
included. Every owned API/worker subprocess is stopped and reaped before completion.

Throughput is calculated from starting the two workers to observing every batch task succeed;
individual latency is database creation-to-completion time. These are small synthetic template
measurements, including process startup and polling, not production capacity or P95 estimates.
Semantic model quality, paid-call recovery, HA, database backup restoration, and AWS deployment
are not evaluated.

## Observed run on 2026-10-02

The final local PostgreSQL run completed 20/20 batch tasks; each worker handled 10. All DOCX
hash and identity checks passed, every task had one attempt, and model calls were zero. The
interruption was proved before lease expiry; the task became `NEEDS_REVIEW` after natural
expiry, remained at one attempt, and a new task succeeded through the replacement worker.
All five owned API/worker processes were reaped.

The batch worker-start window measured 0.696114 seconds, or 28.730909 synthetic template tasks
per second. This single run is evidence of the tested workflow only. A public sanitized receipt
is saved in [the evidence record](../evidence/2026-10-02-resume-service-evaluation.json).
