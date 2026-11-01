import json
from pathlib import Path

from pydantic import BaseModel

from scripts.evaluate_resume_concurrency import FakeGateway, safe_loopback_dsn


def test_safe_dsn_refuses_non_loopback_and_wrong_database() -> None:
    assert safe_loopback_dsn("postgresql://u@127.0.0.1/resume_service_eval_x")
    assert not safe_loopback_dsn("postgresql://u@db/resume_service_eval")
    assert not safe_loopback_dsn("postgresql://u@127.0.0.1/postgres")


def test_fake_gateway_records_safe_deterministic_audit(tmp_path: Path) -> None:
    class Output(BaseModel):
        pass

    audit = tmp_path / "audit.jsonl"
    fake = FakeGateway(audit, 0, 0, 7)
    payload = {
        "candidate_profile": [{"profile_entry_id": "P-1", "text": "safe"}],
        "atomic_facts": [{"profile_entry_id": "P-1", "fact_id": "F-1", "text": "safe"}],
    }
    result = fake.complete(Output, system="x", user=json.dumps(payload))
    assert result is not None
    record = json.loads(audit.read_text())
    assert set(record) == {
        "ordinal",
        "pid",
        "start_monotonic_ns",
        "end_monotonic_ns",
        "walltime_ns",
        "errorcategory",
    }
