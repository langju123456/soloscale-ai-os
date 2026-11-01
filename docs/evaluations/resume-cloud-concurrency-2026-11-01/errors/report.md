# Resume Cloud concurrency evaluation

Status: **PASSED**

Measured local synthetic workload; no paid model, provider HTTP/TLS, or production capacity claim.

Source: `651112bad4b73828fc8b6cd1d38bf115085fdc21`; dirty: `False`.

| Measurement | Observed value |
|---|---|
| config.tasks | `24` |
| config.clients | `4` |
| config.workers | `2` |
| config.threads | `2` |
| config.delay | `0.15` |
| config.error_rate | `0.25` |
| config.seed | `7` |
| config.capacity | `8` |
| config.refill | `0.1` |
| requests.create_requests | `91` |
| requests.statuses | `{"201": 82, "409": 1, "429": 8}` |
| requests.replays | `48` |
| requests.burst_duration_seconds | `0.08373137516900897` |
| requests.create_latency_seconds | `{"count": 91, "max": 0.08224529167637229, "p50": 0.06054366705939174, "p95": 0.0741769999731332}` |
| tasks.total | `34` |
| tasks.states | `{"FAILED": 9, "SUCCEEDED": 25}` |
| tasks.terminal_audit_events | `34` |
| tasks.duplicate_terminal_events | `0` |
| tasks.end_to_end_seconds | `{"count": 34, "max": 2.184294, "p50": 1.493878, "p95": 2.17652455}` |
| tasks.queue_wait_seconds | `{"count": 34, "max": 1.910175, "p50": 1.056311, "p95": 1.8588783}` |
| model.calls | `33` |
| model.batch_calls | `32` |
| model.batch_errors | `9` |
| model.duration_seconds | `{"count": 33, "max": 0.159014375, "p50": 0.15261675, "p95": 0.1555001164}` |
| model.per_worker_peak | `{"88209": 2, "88210": 2}` |
| model.aggregate_peak | `4` |
| model.aggregate_limit | `4` |
| cache.same_client_call_delta | `0` |
| cache.other_client_call_delta | `1` |
| metrics.queue_samples | `34` |
| metrics.model_samples | `33` |
| metrics.cache_lookups | `34` |
| metrics.cache_hits | `1` |

| Assertion | Result |
|---|---|
| concurrent_replays_one_task_per_key | True |
| different_payload_conflicts | True |
| rate_limit_and_retry_after | True |
| shared_api_bucket | True |
| only_expected_http_statuses | True |
| all_accepted_tasks_queued | True |
| cache_hit_zero_model_calls | True |
| cache_preserves_output_and_expiry | True |
| cross_client_cache_miss | True |
| workers_exit_cleanly | True |
| each_key_one_task | True |
| rate_denials_not_persisted | True |
| terminal_transition_exactly_once | True |
| one_attempt_per_task | True |
| actual_model_calls_positive | True |
| actual_calls_match_attempt_flags | True |
| batch_errors_no_retry | True |
| no_fake_contract_errors | True |
| requested_failure_scenario_observed | True |
| all_workers_called_model | True |
| per_worker_model_peak_bounded | True |
| aggregate_model_peak_bounded | True |
| both_apis_still_ready | True |
| metrics_match_independent_audit | True |
| audit_detects_rolled_back_duplicate_probe | True |
| evidence_contains_no_private_content | True |

Full source hashes, runtime, timings and process evidence are in report.json.
Batch failures are seeded by JD hash. Cache control calls use error rate 0.
The terminal audit trigger exists only in this disposable evaluation database.
