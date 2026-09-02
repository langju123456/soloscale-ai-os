from __future__ import annotations

import json
import os
import stat
import sys
import time
from http.client import HTTPConnection
from pathlib import Path
from subprocess import PIPE, Popen

import pytest

from soloscale.local_ui import (
    _desktop_bootstrap_request_proof,
    _desktop_bootstrap_response_proof,
    _desktop_readiness_proof,
    _desktop_session_cookie,
    _write_readiness_file,
)
from soloscale.runtime_paths import (
    default_data_root,
    resolve_repository_root,
    resolve_resource_root,
    resolve_runtime_paths,
    resolve_workspace_root,
    source_data_root,
)


def test_default_data_root_preserves_existing_legacy_location(tmp_path: Path) -> None:
    assert source_data_root(home=tmp_path) == tmp_path / "Documents" / "SoloScaleData"
    assert default_data_root(home=tmp_path) == (
        tmp_path / "Library" / "Application Support" / "SoloScale AI OS"
    )

    legacy = tmp_path / "Documents" / "SoloScaleData"
    legacy.mkdir(parents=True)
    assert default_data_root(home=tmp_path) == legacy


def test_resource_root_uses_pyinstaller_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = tmp_path / "_internal"
    bundle.mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)

    assert resolve_resource_root() == bundle


def test_runtime_paths_honor_explicit_roots(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(
        data_root=tmp_path / "data",
        resource_root=tmp_path / "resources",
        repository_root=tmp_path / "repository",
        workspace_root=tmp_path / "workspace",
    )

    assert paths.data_root == tmp_path / "data"
    assert paths.resource_root == tmp_path / "resources"
    assert paths.repository_root == tmp_path / "repository"
    assert paths.workspace_root == tmp_path / "workspace"
    assert resolve_resource_root(tmp_path / "resources") == paths.resource_root
    assert resolve_repository_root(tmp_path / "repository") == paths.repository_root
    assert resolve_workspace_root(tmp_path / "workspace") == paths.workspace_root


def test_readiness_file_is_private_and_atomic(tmp_path: Path) -> None:
    readiness = tmp_path / "sidecar" / "ready.json"
    payload = {"schema_version": "1.0", "url": "http://127.0.0.1:1", "pid": 1}

    _write_readiness_file(readiness, payload)

    assert readiness.read_text(encoding="utf-8") == (
        '{"schema_version":"1.0","url":"http://127.0.0.1:1","pid":1}\n'
    )
    assert stat.S_IMODE(readiness.stat().st_mode) == 0o600


def test_local_ui_port_zero_writes_readiness_and_serves_health(tmp_path: Path) -> None:
    readiness = tmp_path / "ready.json"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1] / "src"), environment.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    process = Popen(
        [
            sys.executable,
            "-m",
            "soloscale.local_ui",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--data-root",
            str(tmp_path / "data"),
            "--readiness-file",
            str(readiness),
        ],
        env=environment,
        stdout=PIPE,
        stderr=PIPE,
        text=True,
    )
    try:
        for _ in range(60):
            if readiness.is_file():
                break
            time.sleep(0.05)
        assert readiness.is_file()
        record = json.loads(readiness.read_text(encoding="utf-8"))
        assert record["schema_version"] == "1.0"
        assert record["pid"] == process.pid
        connection = HTTPConnection("127.0.0.1", int(record["url"].rsplit(":", 1)[1]))
        connection.request("GET", "/health")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == b'{"status":"ok"}'
    finally:
        process.terminate()
        process.wait(timeout=5)
    assert not readiness.exists()


def test_desktop_mode_requires_exact_host_and_bootstrap_cookie(tmp_path: Path) -> None:
    readiness = tmp_path / "ready.json"
    data_root = tmp_path / "data"
    token = "a" * 64
    nonce = "b" * 64
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1] / "src"), environment.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    environment["SOLOSCALE_DESKTOP_SESSION_TOKEN"] = token
    process = Popen(
        [
            sys.executable,
            "-m",
            "soloscale.local_ui",
            "--desktop-mode",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--data-root",
            str(data_root),
            "--readiness-file",
            str(readiness),
        ],
        env=environment,
        stdin=PIPE,
        stdout=PIPE,
        stderr=PIPE,
        text=True,
    )
    assert process.stdin is not None
    process.stdin.write("\x00\x00\x00\x00")
    process.stdin.close()
    stderr_output = ""
    try:
        for _ in range(60):
            if readiness.is_file():
                break
            time.sleep(0.05)
        assert readiness.is_file()
        record = json.loads(readiness.read_text(encoding="utf-8"))
        assert record["pid"] == process.pid
        assert record["proof"] == _desktop_readiness_proof(
            token=token,
            url=record["url"],
            pid=record["pid"],
        )
        assert record["proof"] != _desktop_readiness_proof(
            token=token,
            url="http://127.0.0.1:1",
            pid=record["pid"],
        )
        port = int(record["url"].rsplit(":", 1)[1])
        expected_host = f"127.0.0.1:{port}"

        connection = HTTPConnection("127.0.0.1", port)
        connection.request("GET", "/health")
        response = connection.getresponse()
        assert response.status == 403
        response.read()

        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.status == 403
        response.read()

        connection.request(
            "GET", f"/?desktop_token={token}", headers={"Host": f"localhost:{port}"}
        )
        response = connection.getresponse()
        assert response.status == 403
        response.read()

        connection.request("GET", f"/?desktop_token={token}", headers={"Host": expected_host})
        response = connection.getresponse()
        assert response.status == 403
        response.read()

        request_proof = _desktop_bootstrap_request_proof(
            token=token,
            url=record["url"],
            pid=record["pid"],
            nonce=nonce,
        )
        connection.request(
            "POST",
            "/__desktop/bootstrap",
            body=b"",
            headers={
                "Host": expected_host,
                "Content-Length": "0",
                "X-SoloScale-Bootstrap-Nonce": nonce,
                "X-SoloScale-Bootstrap-Proof": "0" * 64,
            },
        )
        response = connection.getresponse()
        assert response.status == 403
        assert response.getheader("Set-Cookie") is None
        response.read()

        connection.request(
            "POST",
            "/__desktop/bootstrap",
            body=b"",
            headers={
                "Host": expected_host,
                "Content-Length": "0",
                "X-SoloScale-Bootstrap-Nonce": nonce,
                "X-SoloScale-Bootstrap-Proof": request_proof,
            },
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("X-SoloScale-Bootstrap-Nonce") == nonce
        set_cookie = response.getheader("Set-Cookie") or ""
        assert "HttpOnly" in set_cookie
        assert "SameSite=Strict" in set_cookie
        session_cookie = set_cookie.split(";", 1)[0]
        expected_cookie = _desktop_session_cookie(
            token=token,
            url=record["url"],
            pid=record["pid"],
            nonce=nonce,
        )
        assert session_cookie == f"soloscale_desktop_session={expected_cookie}"
        assert token not in set_cookie
        assert response.getheader(
            "X-SoloScale-Bootstrap-Proof"
        ) == _desktop_bootstrap_response_proof(
            token=token,
            url=record["url"],
            pid=record["pid"],
            nonce=nonce,
            cookie=expected_cookie,
        )
        response.read()

        connection.request(
            "POST",
            "/__desktop/bootstrap",
            body=b"",
            headers={
                "Host": expected_host,
                "Content-Length": "0",
                "X-SoloScale-Bootstrap-Nonce": nonce,
                "X-SoloScale-Bootstrap-Proof": request_proof,
            },
        )
        response = connection.getresponse()
        assert response.status == 403
        assert response.getheader("Set-Cookie") is None
        response.read()

        second_nonce = "c" * 64
        connection.request(
            "POST",
            "/__desktop/bootstrap",
            body=b"",
            headers={
                "Host": expected_host,
                "Content-Length": "0",
                "X-SoloScale-Bootstrap-Nonce": second_nonce,
                "X-SoloScale-Bootstrap-Proof": _desktop_bootstrap_request_proof(
                    token=token,
                    url=record["url"],
                    pid=record["pid"],
                    nonce=second_nonce,
                ),
            },
        )
        response = connection.getresponse()
        assert response.status == 403
        assert response.getheader("Set-Cookie") is None
        response.read()

        connection.request(
            "GET", "/", headers={"Cookie": f"soloscale_desktop_session={token}"}
        )
        response = connection.getresponse()
        assert response.status == 403
        response.read()

        connection.request("GET", "/", headers={"Cookie": session_cookie})
        response = connection.getresponse()
        assert response.status == 200
        home = response.read()
        assert b"SoloScale" in home
        assert b'href="/resume?lang=zh-CN"' in home

        connection.request("GET", "/work", headers={"Cookie": session_cookie})
        response = connection.getresponse()
        assert response.status == 200
        work_page = response.read()
        assert b"SoloScale" in work_page
        assert str(data_root).encode() not in work_page
        assert b"EvidenceHub" not in work_page

        connection.request("GET", "/health", headers={"Cookie": session_cookie})
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == b'{"status":"ok"}'

        connection.request("POST", "/evidence/refresh", body=b"")
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        assert not (data_root / "evidence").exists()
        connection.close()
    finally:
        process.terminate()
        process.wait(timeout=5)
        assert process.stdout is not None
        stdout_output = process.stdout.read()
        assert process.stderr is not None
        stderr_output = process.stderr.read()
    assert not readiness.exists()
    assert token not in stdout_output
    assert token not in stderr_output
