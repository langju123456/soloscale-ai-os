#!/usr/bin/env python3
"""Bounded AWS CLI v2 deployment for the ephemeral Resume demo."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import string
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "infra/aws/resume-demo.json"
REGION, TIMEOUT = "us-east-1", 20 * 60
AMI = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
APP = re.compile(
    r"^[0-9]{12}\.dkr\.ecr\.us-east-1\.amazonaws\.com/[a-z0-9/-]+@sha256:[a-f0-9]{64}$"
)
POSTGRES = re.compile(r"^docker\.io/library/postgres@sha256:[a-f0-9]{64}$")


def cmd(argv, stdin=None):
    r = subprocess.run(argv, input=stdin, text=True, capture_output=True)
    if r.returncode:
        raise RuntimeError(
            f"command failed ({r.returncode}): {' '.join(argv[:4])}: {r.stderr.strip()}"
        )
    return r.stdout


def aws(a, *x):
    return cmd(["aws", "--profile", a.profile, "--region", REGION, *x])


def account(a):
    return json.loads(aws(a, "sts", "get-caller-identity", "--output", "json"))["Account"]


def estimate():
    return {
        "duration_hours": 4,
        "assumptions": [
            "t3.small about USD 0.0209/hour",
            "public IPv4 about USD 0.005/hour",
            "20 GiB gp3 about USD 0.08/GiB-month",
        ],
        "billing_cap": False,
    }


def free_plan(a):
    try:
        data = json.loads(aws(a, "freetier", "get-account-plan-state", "--output", "json"))
    except RuntimeError as e:
        return {"status": "UNKNOWN_REQUIRES_USER_CHECK", "reason": str(e)}
    return {
        "status": "FREE" if data.get("accountPlanType") == "FREE" else "NOT_FREE",
        "accountPlanType": data.get("accountPlanType"),
        "response": data,
    }


def manifest(path, data, new=False):
    if new and path.exists():
        raise RuntimeError(f"refusing to overwrite existing manifest: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".resume-cloud-", dir=path.parent, text=True)
    try:
        os.fchmod(fd, 0o600)
        receipt = {k: v for k, v in data.items() if k not in {"args", "manifest_path"}}
        with os.fdopen(fd, "w") as h:
            json.dump(receipt, h, indent=2, sort_keys=True)
            h.write("\n")
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def update(a, **changes):
    a.update(changes)
    manifest(a["manifest_path"], a)
    return a


def put_secret(a, name):
    fd, tmp = tempfile.mkstemp(prefix="soloscale-secret-", text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as h:
            h.write(
                "".join(
                    secrets.choice(string.ascii_letters + string.digits + "_-") for _ in range(48)
                )
            )
        aws(
            a["args"],
            "ssm",
            "put-parameter",
            "--name",
            name,
            "--type",
            "SecureString",
            "--value",
            f"file://{tmp}",
            "--description",
            "Ephemeral SoloScale resume demo secret",
            "--tags",
            f"Key=soloscale:task,Value={a['task_prefix']}",
            f"Key=soloscale:operation,Value={a['operation_id']}",
        )
    finally:
        os.unlink(tmp)


def poll_stack(a, stack_id):
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        data = json.loads(
            aws(
                a, "cloudformation", "describe-stacks", "--stack-name", stack_id, "--output", "json"
            )
        )["Stacks"][0]
        status = data["StackStatus"]
        if status == "CREATE_COMPLETE":
            return data
        if status.endswith("FAILED") or "ROLLBACK" in status:
            raise RuntimeError(f"stack terminal status: {status}")
        time.sleep(10)
    raise RuntimeError("stack creation exceeded 20 minutes")


def absent(a, name):
    r = subprocess.run(
        [
            "aws",
            "--profile",
            a.profile,
            "--region",
            REGION,
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            name,
        ],
        text=True,
        capture_output=True,
    )
    if r.returncode == 0:
        return False
    if "does not exist" in r.stderr:
        return True
    raise RuntimeError(f"could not establish stack absence: {r.stderr.strip()}")


def plan(a):
    aws(a, "cloudformation", "validate-template", "--template-body", f"file://{TEMPLATE}")
    print(
        json.dumps(
            {
                "mutates_aws": False,
                "region": REGION,
                "task_prefix": a.task_prefix,
                "account": account(a),
                "free_plan": free_plan(a),
                "resource_count": 14,
                "estimate": estimate(),
            },
            sort_keys=True,
        )
    )


def create(a):
    if not absent(a, a.task_prefix):
        raise RuntimeError("refusing existing stack name")
    state = free_plan(a)
    if state["status"] != "FREE":
        raise RuntimeError(f"account plan is not confirmed FREE: {state['status']}")
    api, pg = f"/{a.task_prefix}/api-bearer-token", f"/{a.task_prefix}/postgres-password"
    operation_id = uuid.uuid4().hex
    data = {
        "version": 3,
        "status": "CREATING",
        "account_id": account(a),
        "region": REGION,
        "task_prefix": a.task_prefix,
        "operation_id": operation_id,
        "manifest_path": a.manifest,
        "secret_parameter_names": [],
        "intended_secret_parameter_names": [api, pg],
        "intended_stack_name": a.task_prefix,
        "client_request_token": operation_id,
        "estimate": estimate(),
        "free_plan": state,
        "cleanup_command": (
            f"python3 scripts/aws/teardown.py --profile {a.profile} "
            f"--manifest {a.manifest} --execute"
        ),
        "args": a,
    }
    # `args` is stripped before each durable receipt.
    data.pop("args")
    manifest(a.manifest, data, new=True)
    data["args"] = a
    data["manifest_path"] = a.manifest
    try:
        update(data, status="CREATING_API_SECRET", secret_attempted=[api])
        put_secret(data, api)
        update(data, secret_parameter_names=[api])
        update(data, status="CREATING_POSTGRES_SECRET", secret_attempted=[api, pg])
        put_secret(data, pg)
        update(data, secret_parameter_names=[api, pg])
        ami = aws(
            a,
            "ssm",
            "get-parameter",
            "--name",
            AMI,
            "--query",
            "Parameter.Value",
            "--output",
            "text",
        ).strip()
        update(data, status="STACK_REQUESTING")
        response = json.loads(
            aws(
                a,
                "cloudformation",
                "create-stack",
                "--stack-name",
                a.task_prefix,
                "--client-request-token",
                operation_id,
                "--template-body",
                f"file://{TEMPLATE}",
                "--capabilities",
                "CAPABILITY_NAMED_IAM",
                "--parameters",
                f"ParameterKey=TaskPrefix,ParameterValue={a.task_prefix}",
                f"ParameterKey=ApiTokenParameterName,ParameterValue={api}",
                f"ParameterKey=PostgresPasswordParameterName,ParameterValue={pg}",
                f"ParameterKey=AmiId,ParameterValue={ami}",
                "--tags",
                f"Key=soloscale:task,Value={a.task_prefix}",
                f"Key=soloscale:operation,Value={operation_id}",
            )
        )
        update(data, status="STACK_CREATING", stack_id=response["StackId"])
        stack = poll_stack(a, response["StackId"])
        outputs = {x["OutputKey"]: x["OutputValue"] for x in stack.get("Outputs", [])}
        resources = json.loads(
            aws(
                a,
                "cloudformation",
                "describe-stack-resources",
                "--stack-name",
                response["StackId"],
                "--output",
                "json",
            )
        )["StackResources"]
        ids = {x["LogicalResourceId"]: x["PhysicalResourceId"] for x in resources}
        instance = ids.get("Instance")
        volume = (
            aws(
                a,
                "ec2",
                "describe-volumes",
                "--filters",
                f"Name=attachment.instance-id,Values={instance}",
                "--query",
                "Volumes[0].VolumeId",
                "--output",
                "text",
            ).strip()
            if instance
            else "UNKNOWN"
        )
        update(
            data,
            status="STACK_READY",
            creation_time=stack.get("CreationTime"),
            outputs=outputs,
            resources=ids,
            instance_id=instance,
            volume_id=volume,
        )
        print(
            json.dumps(
                {"status": "STACK_READY", "manifest": str(a.manifest), "outputs": outputs},
                sort_keys=True,
            )
        )
    except Exception as e:
        update(data, status="FAILED", failure=str(e))
        raise RuntimeError(
            "creation stopped; no cleanup was run. Review manifest then use: "
            f"{data['cleanup_command']}"
        ) from e


def source_tag(path):
    d = hashlib.sha256()
    files = [
        path / name
        for name in (
            "Dockerfile.resume-cloud",
            "Dockerfile.resume-cloud.dockerignore",
            "pyproject.toml",
            "README.md",
            "scripts/migrate_resume_cloud.py",
        )
    ]
    for directory in ("src", ".agents", "migrations/resume_cloud"):
        files.extend(
            f
            for f in (path / directory).rglob("*")
            if f.is_file() and "__pycache__" not in f.parts and not f.is_symlink()
        )
    for f in sorted(files):
        d.update(str(f.relative_to(path)).encode() + b"\0")
        d.update(f.read_bytes())
    return "content-" + d.hexdigest()[:20]


def ecr_digest(a, repository, tag):
    r = subprocess.run(
        [
            "aws",
            "--profile",
            a.profile,
            "--region",
            REGION,
            "ecr",
            "describe-images",
            "--repository-name",
            repository,
            "--image-ids",
            f"imageTag={tag}",
            "--query",
            "imageDetails[0].imageDigest",
            "--output",
            "text",
        ],
        text=True,
        capture_output=True,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def poll_command(a, cid, iid):
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        x = json.loads(
            aws(
                a,
                "ssm",
                "get-command-invocation",
                "--command-id",
                cid,
                "--instance-id",
                iid,
                "--output",
                "json",
            )
        )
        status = x["Status"]
        if status == "Success":
            return
        if status in {"Cancelled", "TimedOut", "Failed", "Cancelling"}:
            raise RuntimeError(f"SSM rollout {status}; inspect command ID {cid}")
        time.sleep(10)
    raise RuntimeError("SSM rollout exceeded 20 minutes")


def rollout(a):
    d = json.loads(a.manifest.read_text())
    if d.get("status") not in {"STACK_READY", "ROLLOUT_READY"} or d.get("account_id") != account(a):
        raise RuntimeError("manifest is not a ready receipt for this account")
    if not a.postgres_image_uri or not POSTGRES.fullmatch(a.postgres_image_uri):
        raise RuntimeError("postgres image must be docker.io/library/postgres@sha256:<digest>")
    uri = d["outputs"]["RepositoryUri"]
    registry, repository = uri.split("/", 1)
    tag = source_tag(a.source_dir)
    digest = ecr_digest(a, repository, tag)
    password = aws(a, "ecr", "get-login-password")
    cmd(["docker", "login", "--username", "AWS", "--password-stdin", registry], password)
    if digest is None:
        cmd(
            [
                "docker",
                "buildx",
                "build",
                "--platform",
                "linux/amd64",
                "--file",
                str(a.source_dir / "Dockerfile.resume-cloud"),
                "--tag",
                f"{uri}:{tag}",
                "--push",
                str(a.source_dir),
            ]
        )
        digest = ecr_digest(a, repository, tag)
    app = f"{uri}@{digest}" if digest else ""
    if not APP.fullmatch(app):
        raise RuntimeError("could not resolve approved immutable ECR digest")
    result = json.loads(
        aws(
            a,
            "ssm",
            "send-command",
            "--instance-ids",
            d["instance_id"],
            "--document-name",
            d["outputs"]["RunDocumentName"],
            "--parameters",
            f"AppImageUri={app},PostgresImageUri={a.postgres_image_uri}",
            "--comment",
            tag,
            "--output",
            "json",
        )
    )
    cid = result["Command"]["CommandId"]
    poll_command(a, cid, d["instance_id"])
    d.update(
        status="ROLLOUT_READY",
        app_image_uri=app,
        postgres_image_uri=a.postgres_image_uri,
        ssm_command_id=cid,
    )
    manifest(a.manifest, d)
    print(
        json.dumps(
            {"status": "ROLLOUT_READY", "app_image_uri": app, "ssm_command_id": cid}, sort_keys=True
        )
    )


def resolve_postgres():
    cmd(["docker", "pull", "docker.io/library/postgres:16-alpine"])
    digests = json.loads(
        cmd(
            [
                "docker",
                "image",
                "inspect",
                "docker.io/library/postgres:16-alpine",
                "--format",
                "{{json .RepoDigests}}",
            ]
        )
    )
    normalized = [
        "docker.io/library/" + value if value.startswith("postgres@sha256:") else value
        for value in digests
    ]
    selected = next((x for x in normalized if POSTGRES.fullmatch(x)), None)
    if not selected:
        raise RuntimeError("Docker did not return a docker.io/library/postgres immutable digest")
    print(
        json.dumps(
            {
                "postgres_image_uri": selected,
                "mutable_source_tag": "docker.io/library/postgres:16-alpine",
                "review_required": True,
            },
            sort_keys=True,
        )
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["plan", "create", "rollout", "resolve-postgres"])
    p.add_argument("--profile")
    p.add_argument("--task-prefix")
    p.add_argument(
        "--manifest", type=Path, default=Path(".soloscale/resume-cloud-aws-manifest.json")
    )
    p.add_argument("--source-dir", type=Path, default=ROOT)
    p.add_argument("--postgres-image-uri")
    p.add_argument("--execute", action="store_true")
    a = p.parse_args()
    if a.action == "resolve-postgres":
        resolve_postgres()
        return
    if not a.profile or not a.task_prefix:
        raise SystemExit("--profile and --task-prefix are required")
    if not re.fullmatch(r"[a-z][a-z0-9-]{5,35}", a.task_prefix):
        raise SystemExit("invalid task prefix")
    if a.action == "plan":
        plan(a)
    elif not a.execute:
        raise SystemExit("--execute is required for AWS mutations")
    elif a.action == "create":
        create(a)
    else:
        rollout(a)


if __name__ == "__main__":
    main()
