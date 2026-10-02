"""Loopback-only, in-memory Resume preparation API.

The endpoint deliberately runs synchronous canonical parsers in the request handler:
their bounded work is local and keeping the path synchronous makes the no-provider,
no-persistence contract explicit. Deploying this module is outside its scope.
"""

from __future__ import annotations

import base64
import binascii
import os
from typing import Annotated

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from soloscale.resume_docx import (
    ResumeTemplateError,
    extract_candidate_profile,
    tailor_resume_docx,
)
from soloscale.resume_gateway_boundary import (
    MAX_RESUME_FILE_BYTES,
    ExtractedResumeUpload,
    ResumeSourceParseError,
    ResumeUploadError,
    ResumeUploadRole,
    SelectedResumeFile,
    extract_selected_resume_files,
    normalize_text_resume_to_docx,
)
from soloscale.resume_models import (
    CandidateProfile,
    ResumeProfileLimitError,
    validate_resume_profile_entry_count,
)

MAX_RESUME_BASE64_CHARS = (MAX_RESUME_FILE_BYTES + 2) // 3 * 4
MAX_JOB_DESCRIPTION_CHARS = 50_000
# Non-BMP characters may use two six-byte Unicode escapes in JSON.
_MAX_JSON_ESCAPED_FILENAME_BYTES = 255 * 12
_MAX_JSON_ESCAPED_JD_BYTES = MAX_JOB_DESCRIPTION_CHARS * 12
API_BODY_LIMIT = (
    MAX_RESUME_BASE64_CHARS + _MAX_JSON_ESCAPED_FILENAME_BYTES + _MAX_JSON_ESCAPED_JD_BYTES + 4_096
)


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResumeInput(_RequestModel):
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    content_base64: Annotated[str, Field(min_length=1, max_length=MAX_RESUME_BASE64_CHARS)]


class ResumeRequest(_RequestModel):
    resume: ResumeInput
    job_description: Annotated[str, Field(min_length=1, max_length=MAX_JOB_DESCRIPTION_CHARS)]


class SourceResponse(_RequestModel):
    format: str
    sha256: str
    parse_status: str


class ProfileResponse(_RequestModel):
    full_name: str | None
    headline: str | None
    summary_present: bool
    skills_count: int
    experience_bullet_count: int
    project_bullet_count: int
    education_count: int


class PreviewResponse(_RequestModel):
    mode: str
    model_calls: int
    source: SourceResponse
    profile: ProfileResponse
    diagnostics: dict[str, object]


class BodyLimitMiddleware:
    """Reject an oversized ASGI body before FastAPI buffers or parses JSON."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        received = 0
        more_body = True
        while more_body:
            message = await receive()
            body = message.get("body", b"")
            if isinstance(body, bytes):
                received += len(body)
                if received > API_BODY_LIMIT:
                    response = JSONResponse(
                        status_code=413,
                        content={
                            "error": {
                                "code": "REQUEST_TOO_LARGE",
                                "message": "Request body exceeds the API limit",
                            }
                        },
                    )
                    await response(scope, receive, send)
                    return
                chunks.append(body)
            more_body = bool(message.get("more_body", False))
        body = b"".join(chunks)
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _decode_resume(value: ResumeInput) -> bytes:
    try:
        content = base64.b64decode(value.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _error(400, "INVALID_BASE64", "resume.content_base64 must be valid base64") from exc
    if not content:
        raise _error(400, "EMPTY_RESUME", "The selected resume must not be empty")
    if len(content) > MAX_RESUME_FILE_BYTES:
        raise _error(413, "RESUME_TOO_LARGE", "The selected resume exceeds the 5 MB limit")
    return content


def _prepare(payload: ResumeRequest) -> tuple[ExtractedResumeUpload, bytes]:
    if not payload.job_description.strip():
        raise _error(400, "EMPTY_JOB_DESCRIPTION", "job_description must not be blank")
    content = _decode_resume(payload.resume)
    try:
        extracted = extract_selected_resume_files(
            [
                SelectedResumeFile(
                    role=ResumeUploadRole.RESUME,
                    filename=payload.resume.filename,
                    content_type="application/octet-stream",
                    content=content,
                )
            ]
        )[ResumeUploadRole.RESUME]
        template = (
            content
            if extracted.source_format == "docx"
            else normalize_text_resume_to_docx(extracted.text)
        )
        profile = extract_candidate_profile(template)
        validate_resume_profile_entry_count(profile)
    except ResumeSourceParseError as exc:
        raise _error(422, exc.code, "The selected resume text could not be read reliably") from exc
    except ResumeProfileLimitError as exc:
        raise _error(422, "PROFILE_ENTRY_LIMIT", str(exc)) from exc
    except ResumeUploadError as exc:
        raise _error(422, "INVALID_RESUME", str(exc)) from exc
    except ResumeTemplateError as exc:
        raise _error(422, "UNPARSEABLE_RESUME", str(exc)) from exc
    return (extracted, template)


def _profile_summary(profile: CandidateProfile) -> ProfileResponse:
    # CandidateProfile fields are intentionally projected, never invented from repository data.
    return ProfileResponse.model_validate(
        {
            "full_name": profile.full_name,
            "headline": profile.headline,
            "summary_present": bool(profile.summary),
            "skills_count": len(profile.skills),
            "experience_bullet_count": len(profile.experience_bullets),
            "project_bullet_count": len(profile.project_bullets),
            "education_count": len(profile.education),
        }
    )


def create_app() -> FastAPI:
    app = FastAPI(title="SoloScale Resume API", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimitMiddleware)

    @app.exception_handler(HTTPException)
    async def handled_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "Request fields are invalid or incomplete",
                }
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, __: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Request could not be processed",
                }
            },
        )

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "loopback_only": True, "model_calls": 0}

    @app.post("/resume/preview")
    def preview(payload: ResumeRequest) -> PreviewResponse:
        extracted, template = _prepare(payload)
        profile = extract_candidate_profile(template)
        return PreviewResponse.model_validate(
            {
                "mode": "template-only",
                "model_calls": 0,
                "source": {
                    "format": extracted.source_format,
                    "sha256": extracted.content_sha256,
                    "parse_status": (
                        extracted.source_parse_trace.source_parse_status
                        if extracted.source_parse_trace
                        else "USABLE"
                    ),
                },
                "profile": _profile_summary(profile),
                "diagnostics": {
                    "claims_preserved": None,
                    "message": "Ready for deterministic template-only reordering",
                },
            }
        )

    @app.post("/resume/tailor")
    def tailor(payload: ResumeRequest) -> Response:
        _, template = _prepare(payload)
        try:
            tailored = tailor_resume_docx(template, payload.job_description)
        except ResumeTemplateError as exc:
            raise _error(422, "TAILORING_REJECTED", str(exc)) from exc
        return Response(
            content=tailored.content,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": 'attachment; filename="tailored-resume.docx"',
                "X-SoloScale-Mode": "template-only",
                "X-SoloScale-Model-Calls": "0",
                "X-SoloScale-Template-SHA256": tailored.template_sha256,
                "X-SoloScale-Output-SHA256": tailored.output_sha256,
                "X-SoloScale-Claims-Preserved": str(tailored.claims_preserved).lower(),
                "X-SoloScale-Project-Blocks-Reordered": str(tailored.project_blocks_reordered),
                "X-SoloScale-Skill-Bullets-Reordered": str(tailored.skill_bullets_reordered),
            },
        )

    return app


def main() -> None:
    import uvicorn

    inherited_fd = os.environ.get("SOLOSCALE_RESUME_API_FD")
    if inherited_fd is not None:
        uvicorn.run(create_app(), fd=int(inherited_fd), log_level="warning")
        return
    port = int(os.environ.get("SOLOSCALE_RESUME_API_PORT", "8766"))
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
