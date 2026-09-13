"""Focused tests for the full-Mac incremental evidence catalog."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from soloscale import mac_discovery
from soloscale.mac_discovery import (
    Classification,
    discover_mac_evidence,
    discovery_status,
    load_discovery_catalog,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fake_git(*, head: str = "abc1234", branch: str = "main", remote: str = "origin") -> object:
    def fake(root: Path, *args: str) -> str | None:
        if args[0] == "rev-parse" and "--verify" in args:
            return head
        if args[0] == "rev-parse" and "--abbrev-ref" in args:
            return branch
        if args[0] == "remote":
            return f"https://example.com/{remote}.git"
        return None

    return fake


def test_classify_code_test_adr_build_and_resume() -> None:
    assert mac_discovery._classify_file(Path("/work/src/app.py")).kind == "code"
    assert mac_discovery._classify_file(Path("/work/tests/test_app.py")).kind == "test"
    assert mac_discovery._classify_file(Path("/work/docs/ADR-0001.md")).kind == "design_adr"
    assert mac_discovery._classify_file(Path("/work/Dockerfile")).kind == "build_ci"
    assert (
        mac_discovery._classify_file(Path("/work/career/Resume_Lang_Ju.docx")).kind
        == "resume_application"
    )


def test_classify_noise_system_secret_and_unknown() -> None:
    assert mac_discovery._classify_file(Path("/work/photo.png")) == Classification(
        "noise", "ignore", "noise extension or generated file"
    )
    assert mac_discovery._classify_file(Path("/Applications/X/memo.md")).kind == "system"
    assert mac_discovery._classify_file(Path("/work/.env")).kind == "secret_sensitive"
    assert mac_discovery._classify_file(Path("/work/random.xyz")).kind == "unknown"


def test_hash_file_detects_private_key_and_api_secret(tmp_path: Path) -> None:
    private_key = tmp_path / "id_rsa_backup.txt"
    _write(private_key, "-----BEGIN RSA PRIVATE KEY-----\nMIIBog==\n")
    _, is_secret = mac_discovery._hash_file(private_key)
    assert is_secret is True

    api_secret = tmp_path / "config.json"
    _write(api_secret, '{"api_key": "sk-1234567890abcdef"}')
    _, is_secret = mac_discovery._hash_file(api_secret)
    assert is_secret is True

    plain = tmp_path / "notes.md"
    _write(plain, "# A normal engineering note")
    _, is_secret = mac_discovery._hash_file(plain)
    assert is_secret is False


def test_discovery_discovers_repos_and_indexes_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mac_discovery, "_git_text", _fake_git())
    data_root = tmp_path / "data"
    scan_root = tmp_path / "scan"

    (scan_root / "project" / ".git").mkdir(parents=True)
    _write(scan_root / "project" / "app.py", "print('hi')\n")
    _write(scan_root / "notes" / "todo.txt", "ship the catalog")
    _write(scan_root / "notes" / ".env", "SECRET=1")
    _write(scan_root / "notes" / "photo.png", "not really a png")
    _write(scan_root / "leak.md", "-----BEGIN PRIVATE KEY-----\nMIIB\n")

    report = discover_mac_evidence(data_root, roots=(scan_root,))

    assert report.repositories_found == 1
    assert report.files_indexed == 1
    assert report.noise_excluded == 1
    assert report.secrets_excluded == 2
    assert report.by_kind.get("code", 0) == 0
    assert report.by_kind.get("note", 0) == 1

    entries = load_discovery_catalog(data_root)
    repos = [entry for entry in entries.values() if entry.record_type == "repository"]
    assert len(repos) == 1
    assert repos[0].git_head == "abc1234"
    assert repos[0].git_branch == "main"

    catalog_path = Path(report.catalog_path)
    assert stat.S_IMODE(catalog_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(catalog_path.parent.stat().st_mode) == 0o700


def test_incremental_rescan_detects_new_changed_and_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mac_discovery, "_git_text", _fake_git())
    data_root = tmp_path / "data"
    scan_root = tmp_path / "scan"

    _write(scan_root / "keep.md", "kept")
    _write(scan_root / "change.md", "before")

    first = discover_mac_evidence(data_root, roots=(scan_root,))
    assert first.files_indexed == 2
    assert first.files_new == 2

    unchanged = discover_mac_evidence(data_root, roots=(scan_root,))
    assert unchanged.files_indexed == 2
    assert unchanged.files_unchanged == 2
    assert unchanged.files_changed == 0

    _write(scan_root / "change.md", "after")
    _write(scan_root / "new.md", "new")
    (scan_root / "keep.md").unlink()

    second = discover_mac_evidence(data_root, roots=(scan_root,))
    assert second.files_indexed == 2
    assert second.files_changed == 1
    assert second.files_new == 1
    assert second.entries_removed == 1


def test_full_rescan_rehashes_without_previous_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mac_discovery, "_git_text", _fake_git())
    data_root = tmp_path / "data"
    scan_root = tmp_path / "scan"
    _write(scan_root / "doc.md", "hello")

    first = discover_mac_evidence(data_root, roots=(scan_root,))
    second = discover_mac_evidence(data_root, roots=(scan_root,), full_rescan=True)

    assert first.mode == "incremental"
    assert second.mode == "full"
    assert second.files_unchanged == 0
    assert second.files_indexed == 1


def test_discovery_status_and_dedupe_descendants(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    missing = discovery_status(data_root)
    assert missing.catalog_exists is False

    root_a = tmp_path / "a"
    root_b = tmp_path / "a" / "b"
    assert mac_discovery._dedupe_descendants((root_b, root_a)) == (root_a.resolve(),)


def test_default_root_is_broad_user_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    roots = mac_discovery.default_discovery_roots(home=home)
    assert roots == (home,)


def test_broad_home_discovery_prunes_noise_and_records_private_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mac_discovery, "_git_text", _fake_git())
    data_root = tmp_path / "data"
    home = tmp_path / "home"

    _write(home / "Documents" / "notes.md", "# notes")
    _write(home / "Desktop" / "resume.md", "# resume")
    _write(home / "Downloads" / "book.pdf", "%PDF-1.4\n")
    _write(home / "Documents" / "SoloScaleData" / "private.json", '{"secret": "x"}')
    _write(home / ".codex" / "sessions" / "session.jsonl", '{"type": "session_meta"}')
    _write(
        home / "Library" / "Application Support" / "SoloScale AI OS" / "private.json",
        '{"secret": "x"}',
    )
    _write(home / "work" / "SoloScaleData" / "private.json", '{"secret": "x"}')
    (home / "projects" / "repo" / ".git").mkdir(parents=True)
    _write(home / "projects" / "repo" / "app.py", "print('hi')\n")
    _write(home / "node_modules" / "pkg" / "index.js", "module.exports = 1;")
    _write(home / ".cache" / "tmp.txt", "cached")

    report = discover_mac_evidence(data_root, home=home)

    assert report.repositories_found == 1
    assert report.sources_found == 4
    assert report.sources_by_kind.get("codex_local", 0) == 1
    assert report.sources_by_kind.get("soloscale_local", 0) == 3
    assert report.files_indexed == 3
    assert report.pruned_directories >= 3

    entries = load_discovery_catalog(data_root)
    source_paths = [entry.path for entry in entries.values() if entry.record_type == "source"]
    assert any(path.endswith(".codex") for path in source_paths)
    assert (
        sum(
            path.endswith("SoloScaleData") or path.endswith("SoloScale AI OS")
            for path in source_paths
        )
        == 3
    )
    file_paths = [entry.path for entry in entries.values() if entry.record_type == "file"]
    assert len(file_paths) == 3
    assert not any(
        ".codex" in path or "SoloScaleData" in path or "SoloScale AI OS" in path
        for path in file_paths
    )


def test_extra_roots_are_scanned_and_excluded_roots_are_skipped(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    base = tmp_path / "base"
    extra = tmp_path / "extra"
    skipped = tmp_path / "base" / "skip"
    _write(base / "keep.md", "kept")
    _write(skipped / "hidden.md", "hidden")
    _write(extra / "added.md", "added")

    report = discover_mac_evidence(
        data_root,
        roots=(base,),
        extra_roots=(extra,),
        excluded_roots=(skipped,),
    )

    entries = load_discovery_catalog(data_root)
    paths = {entry.path for entry in entries.values() if entry.record_type == "file"}
    assert any(path.endswith("keep.md") for path in paths)
    assert any(path.endswith("added.md") for path in paths)
    assert not any("skip" in path for path in paths)
    assert report.excluded_roots == (str(skipped.resolve()),)
