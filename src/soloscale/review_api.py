"""Loopback-only FastAPI service for the human Review Workbench.

The API turns saved Evidence Agent drafts into review items and records human decisions.
It makes zero model calls.  Every rule (state transitions, notes, optimistic revisions,
evidence re-checks) is enforced by ``ReviewStore``/``review_workflow``; the HTTP layer only
validates request shapes and maps rule violations to stable error envelopes.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from soloscale.resume_api import BodyLimitMiddleware
from soloscale.review_intake import (
    list_agent_drafts,
    load_verified_draft,
    validate_run_id,
)
from soloscale.review_models import (
    BatchConfirmRequest,
    ConfirmRequest,
    CorrectRequest,
    DraftCandidate,
    DraftListing,
    EvidenceStatus,
    ImportResponse,
    ItemList,
    RejectRequest,
    ReopenRequest,
    ReviewAction,
    ReviewItemDetail,
    ReviewRunSummary,
    ReviewState,
    RunList,
    VersionDiff,
)
from soloscale.review_store import ReviewStore
from soloscale.review_workflow import ReviewError

DEFAULT_PORT = 8767
DEFAULT_ALLOWED_ORIGINS = ("http://127.0.0.1:3000", "http://localhost:3000")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
REVIEW_API_BODY_LIMIT = 64 * 1024


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=128)


def create_app(
    data_root: Path,
    *,
    allowed_origins: Sequence[str] = DEFAULT_ALLOWED_ORIGINS,
    allowed_hosts: Sequence[str] = LOOPBACK_HOSTS,
) -> FastAPI:
    store = ReviewStore(Path(data_root))
    app = FastAPI(
        title="SoloScale Review Workbench API", docs_url=None, redoc_url=None, openapi_url=None
    )
    # Streamed body limit reused from the Resume API; it does not trust Content-Length.
    app.add_middleware(BodyLimitMiddleware, limit=REVIEW_API_BODY_LIMIT)
    # Reject non-loopback Host headers so a DNS-rebinding page cannot read private excerpts.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
        allow_credentials=False,
    )

    @app.exception_handler(ReviewError)
    async def review_error(_: Request, exc: ReviewError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, **exc.details}},
        )

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = sorted(
            {".".join(str(part) for part in error.get("loc", ())[1:]) for error in exc.errors()}
        )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "Request fields are invalid or incomplete",
                    "fields": fields,
                }
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "Request failed"}},
        )

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "loopback_only": True, "model_calls": 0}

    @app.get("/review/drafts")
    def drafts() -> DraftListing:
        results, skipped = list_agent_drafts(store.data_root)
        imported = store.imported_run_ids()
        return DraftListing(
            drafts=[
                DraftCandidate(
                    run_id=result.run_id,
                    question=result.question,
                    model=result.model,
                    created_at=result.created_at,
                    claim_count=len(result.claims),
                    imported=result.run_id in imported,
                )
                for result in results
            ],
            skipped_invalid=skipped,
        )

    @app.post("/review/imports")
    def import_draft(payload: ImportRequest, response: Response) -> ImportResponse:
        run_id = validate_run_id(payload.run_id)
        draft = load_verified_draft(store.data_root, run_id)
        created, run, items = store.import_draft(draft)
        response.status_code = 201 if created else 200
        return ImportResponse(created=created, run=run, items=items)

    @app.get("/review/runs")
    def runs() -> RunList:
        return RunList(runs=store.list_runs())

    @app.get("/review/runs/{run_id}")
    def run(run_id: str) -> ReviewRunSummary:
        return store.get_run(run_id)

    @app.get("/review/items")
    def items(
        state: Annotated[ReviewState | None, Query()] = None,
        evidence_status: Annotated[EvidenceStatus | None, Query()] = None,
        run_id: Annotated[str | None, Query(max_length=128)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    ) -> ItemList:
        return ItemList(
            items=store.list_items(
                state=state, evidence_status=evidence_status, run_id=run_id, limit=limit
            )
        )

    @app.get("/review/items/{item_id}")
    def item(item_id: str) -> ReviewItemDetail:
        return store.get_item(item_id)

    @app.get("/review/items/{item_id}/diff")
    def diff(
        item_id: str,
        from_version: Annotated[int | None, Query(alias="from", ge=1)] = None,
        to_version: Annotated[int | None, Query(alias="to", ge=1)] = None,
    ) -> VersionDiff:
        return store.diff(item_id, from_version=from_version, to_version=to_version)

    @app.post("/review/items/{item_id}/confirm")
    def confirm(item_id: str, payload: ConfirmRequest) -> ReviewItemDetail:
        return store.apply_action(
            item_id,
            ReviewAction.CONFIRM,
            expected_revision=payload.expected_revision,
            reviewer=payload.reviewer,
            note=payload.note,
        )

    @app.post("/review/items/{item_id}/correct")
    def correct(item_id: str, payload: CorrectRequest) -> ReviewItemDetail:
        return store.apply_action(
            item_id,
            ReviewAction.CORRECT,
            expected_revision=payload.expected_revision,
            reviewer=payload.reviewer,
            note=payload.note,
            text=payload.text,
        )

    @app.post("/review/items/{item_id}/reject")
    def reject(item_id: str, payload: RejectRequest) -> ReviewItemDetail:
        return store.apply_action(
            item_id,
            ReviewAction.REJECT,
            expected_revision=payload.expected_revision,
            reviewer=payload.reviewer,
            note=payload.note,
        )

    @app.post("/review/items/{item_id}/reopen")
    def reopen(item_id: str, payload: ReopenRequest) -> ReviewItemDetail:
        return store.apply_action(
            item_id,
            ReviewAction.REOPEN,
            expected_revision=payload.expected_revision,
            reviewer=payload.reviewer,
            note=payload.note,
        )

    @app.post("/review/batch/confirm")
    def confirm_batch(payload: BatchConfirmRequest) -> ItemList:
        return ItemList(
            items=store.confirm_batch(payload.items, reviewer=payload.reviewer, note=payload.note)
        )

    return app


def main(argv: Sequence[str] | None = None) -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the loopback Review Workbench API.")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.environ.get("SOLOSCALE_REVIEW_DATA_ROOT", ".soloscale")),
        help="Private SoloScale data root holding knowledge/agent-runs (default .soloscale)",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("SOLOSCALE_REVIEW_API_PORT", DEFAULT_PORT))
    )
    args = parser.parse_args(argv)
    origins_env = os.environ.get("SOLOSCALE_REVIEW_ALLOWED_ORIGINS")
    origins = (
        [origin.strip() for origin in origins_env.split(",") if origin.strip()]
        if origins_env
        else list(DEFAULT_ALLOWED_ORIGINS)
    )
    from soloscale.cli import _validate_private_data_root

    try:
        _validate_private_data_root(args.data_root)
    except Exception as exc:  # typer.BadParameter carries the operator-facing message.
        print(f"refusing data root: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    uvicorn.run(
        create_app(args.data_root, allowed_origins=origins),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
