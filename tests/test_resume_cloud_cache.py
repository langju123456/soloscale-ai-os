from pathlib import Path

from soloscale.resume_cloud_cache import generation_pipeline_fingerprint, sha256_text


def test_generation_pipeline_fingerprint_changes_with_source_bytes(tmp_path: Path) -> None:
    source = tmp_path / "resume_docx.py"
    source.write_text("PROMPT = 'one'\n", encoding="utf-8")
    before = generation_pipeline_fingerprint([source])
    source.write_text("PROMPT = 'two'\n", encoding="utf-8")
    assert generation_pipeline_fingerprint([source]) != before


def test_client_partition_hash_does_not_store_label() -> None:
    assert sha256_text("alpha") != sha256_text("beta")
