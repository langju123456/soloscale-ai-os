"""Authenticated JSON/base64 API for the dedicated Resume cloud database."""

from __future__ import annotations

import hmac
import logging
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from soloscale.resume_api import BodyLimitMiddleware
from soloscale.resume_cloud_models import MAX_IDEMPOTENCY_KEY_CHARS, CreateTaskRequest, TaskView
from soloscale.resume_cloud_repository import CloudTask, ResumeCloudRepository
from soloscale.resume_cloud_service import CloudSettings, ResumeCloudService


def _view(task: CloudTask) -> TaskView:
    return TaskView(
        id=task.id,
        state=task.state,
        mode=task.mode,
        model_calls=1 if task.model_call_attempted else 0,
        output_ready=task.output_docx is not None,
        failure_code=task.failure_code,
    )


def _error(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


def create_app(settings: CloudSettings | None = None) -> FastAPI:
    selected = settings or CloudSettings.from_environment()
    repository = ResumeCloudRepository(selected.database_url)
    service = ResumeCloudService(repository, selected)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimitMiddleware)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": {"code": "INVALID_REQUEST"}})

    @app.exception_handler(Exception)
    async def unexpected(_: Request, error: Exception) -> JSONResponse:
        logging.getLogger(__name__).warning("resume cloud failure type=%s", type(error).__name__)
        return JSONResponse(status_code=503, content={"error": {"code": "SERVICE_UNAVAILABLE"}})

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        supplied = (
            authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        )
        if not hmac.compare_digest(supplied.encode(), selected.bearer_token.encode()):
            raise _error(401, "UNAUTHORIZED")

    @app.get("/health/live", status_code=204)
    def live() -> Response:
        return Response(status_code=204)

    @app.get("/health/ready", dependencies=[Depends(authorize)])
    def ready() -> dict[str, bool]:
        if not repository.ready():
            raise _error(503, "NOT_READY")
        return {"ready": True}

    @app.post("/tasks", status_code=201, dependencies=[Depends(authorize)])
    def create_task(
        payload: CreateTaskRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> TaskView:
        if (
            idempotency_key is None
            or not idempotency_key.strip()
            or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_CHARS
        ):
            raise _error(400, "IDEMPOTENCY_KEY_REQUIRED")
        try:
            task, _ = service.create(payload, idempotency_key.strip())
        except ValueError as exc:
            if str(exc) == "IDEMPOTENCY_CONFLICT":
                raise _error(409, "IDEMPOTENCY_CONFLICT") from exc
            raise _error(503, "SERVICE_UNAVAILABLE") from exc
        except HTTPException:
            raise
        except Exception as exc:
            logging.getLogger(__name__).info("resume create rejected type=%s", type(exc).__name__)
            raise _error(422, "INVALID_RESUME") from exc
        return _view(task)

    @app.get("/tasks/{task_id}", dependencies=[Depends(authorize)])
    def get_task(task_id: UUID) -> TaskView:
        task = repository.get(str(task_id))
        if task is None:
            raise _error(404, "NOT_FOUND")
        return _view(task)

    @app.post("/tasks/{task_id}/process", dependencies=[Depends(authorize)])
    def process(task_id: UUID) -> TaskView:
        task = repository.queue(str(task_id))
        if task is None:
            raise _error(404, "NOT_FOUND")
        return _view(task)

    @app.get("/tasks/{task_id}/download", dependencies=[Depends(authorize)])
    def download(task_id: UUID) -> Response:
        task = repository.get(str(task_id))
        if task is None:
            raise _error(404, "NOT_FOUND")
        if task.output_docx is None:
            raise _error(409, "OUTPUT_NOT_READY")
        return Response(
            content=task.output_docx,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": 'attachment; filename="tailored-resume.docx"',
                "X-SoloScale-Mode": "template-only" if task.mode.value == "template" else "ai",
                "X-SoloScale-Output-SHA256": task.output_sha256 or "",
            },
        )

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8080, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
