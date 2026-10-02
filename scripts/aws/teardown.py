#!/usr/bin/env python3
"""Idempotently remove only resources proven to belong to one deployment journal."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path


def call(profile, region, *x, allow_absent=False):
    r = subprocess.run(
        ["aws", "--profile", profile, "--region", region, *x],
        text=True,
        capture_output=True,
    )
    if r.returncode and not (
        allow_absent and ("does not exist" in r.stderr or "ParameterNotFound" in r.stderr)
    ):
        raise RuntimeError(r.stderr.strip())
    return r.stdout if r.returncode == 0 else None


def allowed(prefix):
    return {f"/{prefix}/api-bearer-token", f"/{prefix}/postgres-password"}


def stack(profile, region, name):
    raw = call(
        profile,
        region,
        "cloudformation",
        "describe-stacks",
        "--stack-name",
        name,
        "--output",
        "json",
        allow_absent=True,
    )
    return json.loads(raw)["Stacks"][0] if raw else None


def owned_stack(s, data):
    if not s:
        return False
    tags = {x["Key"]: x["Value"] for x in s.get("Tags", [])}
    return (
        tags.get("soloscale:task") == data["task_prefix"]
        and tags.get("soloscale:operation") == data["operation_id"]
        and s.get("ClientRequestToken") == data["client_request_token"]
    )


def parameter_owned(profile, region, name, data):
    raw = call(
        profile,
        region,
        "ssm",
        "get-parameter",
        "--name",
        name,
        "--output",
        "json",
        allow_absent=True,
    )
    if not raw:
        return False
    arn = json.loads(raw)["Parameter"]["ARN"]
    tags = json.loads(
        call(
            profile,
            region,
            "ssm",
            "list-tags-for-resource",
            "--resource-type",
            "Parameter",
            "--resource-id",
            arn,
            "--output",
            "json",
        )
    )["TagList"]
    got = {x["Key"]: x["Value"] for x in tags}
    return (
        got.get("soloscale:task") == data["task_prefix"]
        and got.get("soloscale:operation") == data["operation_id"]
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--execute", action="store_true")
    a = p.parse_args()
    if not a.manifest.is_file() or os.stat(a.manifest).st_mode & 0o077:
        raise SystemExit("manifest must be mode 0600")
    d = json.loads(a.manifest.read_text())
    prefix = d.get("task_prefix", "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{5,35}", prefix):
        raise SystemExit("invalid journal task prefix")
    if set(d.get("intended_secret_parameter_names", [])) != allowed(prefix):
        raise SystemExit("journal secret names are not exact task names")
    actual = json.loads(
        call(a.profile, d["region"], "sts", "get-caller-identity", "--output", "json")
    )["Account"]
    if actual != d.get("account_id"):
        raise SystemExit("journal account does not match active profile")
    candidate = d.get("stack_id") or d.get("intended_stack_name")
    s = stack(a.profile, d["region"], candidate) if candidate else None
    if s and not owned_stack(s, d):
        raise SystemExit("stack ownership cannot be proven")
    owned = [name for name in allowed(prefix) if parameter_owned(a.profile, d["region"], name, d)]
    if not a.execute:
        print(
            json.dumps(
                {
                    "mutates_aws": False,
                    "stack_id": s.get("StackId") if s else None,
                    "owned_parameters": sorted(owned),
                    "unproven_or_absent_parameters": sorted(allowed(prefix) - set(owned)),
                }
            )
        )
        return
    if s:
        call(a.profile, d["region"], "cloudformation", "delete-stack", "--stack-name", s["StackId"])
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            if not stack(a.profile, d["region"], s["StackId"]):
                break
            time.sleep(10)
        else:
            raise SystemExit("stack deletion exceeded 20 minutes; parameters retained")
    for name in owned:
        call(a.profile, d["region"], "ssm", "delete-parameter", "--name", name, allow_absent=True)
    print(
        json.dumps(
            {
                "deleted_stack": s.get("StackId") if s else None,
                "deleted_parameters": sorted(owned),
                "retained_unproven": sorted(allowed(prefix) - set(owned)),
            }
        )
    )


if __name__ == "__main__":
    main()
