"""Exercise the cloud API with a synthetic resume; it never sends a real resume."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path


def _request(
    url: str,
    method: str,
    token: str,
    payload: object | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    if idempotency_key is not None:
        request.add_header("Idempotency-Key", idempotency_key)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, response.read(), dict(response.headers.items())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path(".soloscale/resume-cloud-demo"))
    parser.add_argument("--task-id")
    args = parser.parse_args()
    base_url = os.environ.get("RESUME_CLOUD_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
    token = os.environ["RESUME_CLOUD_BEARER_TOKEN"]
    resume = (
        b"Synthetic Candidate\nPROJECT HIGHLIGHTS\n- Built a Python service.\n"
        b"TECHNICAL SKILLS\n- Python\n"
    )
    payload = {
        "resume": {
            "filename": "synthetic.txt",
            "content_base64": base64.b64encode(resume).decode(),
        },
        "job_description": "Python engineering role.",
        "mode": "template",
        "approve_external_model_call": False,
    }
    task_id = args.task_id
    if task_id is None:
        status, body, _ = _request(f"{base_url}/tasks", "POST", token, payload, str(uuid.uuid4()))
        if status != 201:
            raise SystemExit("synthetic task creation failed")
        task_id = json.loads(body)["id"]
        _request(f"{base_url}/tasks/{task_id}/process", "POST", token)
    for _ in range(30):
        _, status_body, _ = _request(f"{base_url}/tasks/{task_id}", "GET", token)
        observed = json.loads(status_body)
        state = observed["state"]
        if state == "SUCCEEDED":
            if observed["mode"] != "template" or observed["model_calls"] != 0:
                raise SystemExit("unexpected mode or model-call record")
            status, output, response_headers = _request(
                f"{base_url}/tasks/{task_id}/download", "GET", token
            )
            if status == 200 and output.startswith(b"PK"):
                digest = hashlib.sha256(output).hexdigest()
                headers = {name.lower(): value for name, value in response_headers.items()}
                if headers.get("x-soloscale-output-sha256") != digest:
                    raise SystemExit("download does not match the persisted output hash")
                with zipfile.ZipFile(io.BytesIO(output)) as docx:
                    xml = docx.read("word/document.xml")
                if b"Synthetic Candidate" not in xml:
                    raise SystemExit("synthetic text missing from DOCX")
                args.output_dir.mkdir(parents=True, exist_ok=True)
                output_path = args.output_dir / f"{task_id}.docx"
                output_path.write_bytes(output)
                (args.output_dir / f"{task_id}.json").write_text(
                    json.dumps(
                        {
                            "task_id": task_id,
                            "output_sha256": digest,
                            "mode": observed["mode"],
                            "model_calls": observed["model_calls"],
                            "state": observed["state"],
                            "http_status": status,
                            "bytes": len(output),
                            "persisted_hash_matches": True,
                            "synthetic_text_verified": True,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
                print(f"synthetic template task succeeded: {task_id}")
                return
            break
        if state in {"FAILED", "NEEDS_REVIEW"}:
            break
        time.sleep(1)
    raise SystemExit("synthetic task did not produce a DOCX")


if __name__ == "__main__":
    main()
