"""One-shot or polling worker. It intentionally has no retry loop for model calls."""

from __future__ import annotations

import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from soloscale.resume_cloud_observability import Telemetry
from soloscale.resume_cloud_repository import ResumeCloudRepository
from soloscale.resume_cloud_service import CloudSettings, ResumeCloudService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    settings = CloudSettings.from_environment(require_bearer=False)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    telemetry = Telemetry.from_environment()
    service = ResumeCloudService(
        ResumeCloudRepository(settings.database_url), settings, telemetry=telemetry
    )
    try:
        with ThreadPoolExecutor(max_workers=settings.max_model_concurrency) as pool:
            while True:
                service.repository.recover_and_cleanup(
                    lambda task_id, traceparent: _trace_recovery(telemetry, task_id, traceparent)
                )
                tasks = list(
                    pool.map(lambda _: service.process_one(), range(settings.max_model_concurrency))
                )
                for task in tasks:
                    if task is not None and task.state.value == "SUCCEEDED":
                        logging.getLogger(__name__).info(
                            "resume task completed id=%s state=%s", task.id, task.state
                        )
                if args.once:
                    return
                if not any(tasks):
                    time.sleep(max(0.1, args.poll_seconds))
    finally:
        telemetry.shutdown()


def _trace_recovery(telemetry: Telemetry, task_id: str, traceparent: str | None) -> None:
    with telemetry.span(
        "worker.recovery.needs_review",
        {"task.id": task_id, "terminal.state": "NEEDS_REVIEW"},
        traceparent,
    ):
        pass


if __name__ == "__main__":
    main()
