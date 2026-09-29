# Ephemeral AWS resume demo

This package is a local draft. It has not authenticated to AWS, created resources, pushed an image, or run the workflow.

The only region accepted by the scripts is `us-east-1`. The stack creates a new tagged VPC and one public subnet, a zero-inbound security group, one `t3.small` EC2 instance with standard CPU credits, a 20 GiB encrypted `gp3` root volume deleted with the instance, an immutable ECR repository, an SSM command document, and a CloudWatch group retained for one day. IMDSv2 is required with hop limit one. The API binds only to `127.0.0.1:8080`; use Session Manager port forwarding for local access. PostgreSQL uses a user-defined bridge with no PostgreSQL host port. Containers may initiate outbound connections; only the API has a loopback host binding.

The instance shuts itself down after four hours and uses EC2 termination on shutdown. CloudFormation deletion is still required to remove the VPC, ECR repository, role, and log group. `teardown.py` only deletes the exact stack ID and parameter names recorded in its `0600` manifest after verifying the active account and task tag.

The estimated four-hour operation is below USD 5 using the stated `t3.small`, public IPv4, and 20 GiB `gp3` assumptions. This is an estimate, not a billing cap. Before any creation, confirm the AWS account's Free plan state. The deployment script calls the read-only Free Tier API and stops on unavailable permissions; it never calls an account upgrade API.

## Review and execution gate

Use temporary credentials or SSO only:

```bash
aws login --profile soloscale-demo --region us-east-1
python3 scripts/aws/deploy.py plan --profile soloscale-demo --task-prefix soloscale-resume-20260929
```

The second command only validates the local template through AWS and emits a plan. After a human confirms the account state, exact resource list, costs, task prefix, and cleanup owner, creation requires the separate flag:

```bash
python3 scripts/aws/deploy.py create --profile soloscale-demo --task-prefix soloscale-resume-20260929 --execute
```

It refuses a non-`FREE` or unreadable account plan. Before the first mutation it atomically writes a `0600` manifest, then updates it after each created parameter and immediately after `create-stack` returns its stack ID. It never runs automatic cleanup after a failed create or wait. Secret values never enter CloudFormation, UserData, output, a manifest, arguments, or logs. The runtime document writes distinct `0400` secret files for PostgreSQL UID 70 and the non-root API/worker UID 10001. The PostgreSQL digest must report UID 70. A networkless, short-lived container with only `CHOWN` initializes the volume root owner; the database runs as `70:70` with all capabilities dropped. API and worker logs alone go to CloudWatch; database logging is deliberately excluded because database error text may include input values.

The pre-mutation journal contains the deterministic parameter names, a unique operation ID, intended stack name, and CloudFormation client token. Parameters and the stack carry matching ownership tags. If creation is interrupted, do not rerun `create`; inspect the journal and run the teardown plan. Teardown discovers an unrecorded stack by name only when its task tag, operation tag, account, and client token match. It deletes only the two exact task parameter names after their ownership tags are proven, and treats absent stacks or parameters as completed cleanup steps.

## Image rollout

After the app contract is integrated, the rollout command builds `linux/amd64`, pushes only a content-addressed immutable ECR tag, resolves its ECR digest, and waits up to 20 minutes for the constrained SSM document to report success:

```bash
python3 scripts/aws/deploy.py rollout --profile soloscale-demo --task-prefix soloscale-resume-20260929 --manifest .soloscale/resume-cloud-aws-manifest.json --postgres-image-uri docker.io/library/postgres@sha256:REVIEWED_16_ALPINE_DIGEST --execute
```

The PostgreSQL value must be a reviewed `docker.io/library/postgres` digest for PostgreSQL 16; it is never a mutable tag. The SSM document rejects other image forms, initializes the task-owned database volume on first rollout and reuses it on subsequent rollouts, runs `python scripts/migrate_resume_cloud.py` once, then starts API and worker. It sets no model-provider variables, so the default remains deterministic template mode.

The reusable deployment workflow runs only after the complete Python CI matrix succeeds on a push to `codex/resume-intelligence-public-sync` and the repository variable `RESUME_CLOUD_DEPLOY_ENABLED` equals `true`. It uses repository variables (including the non-secret role ARN), reuses a content-addressed image, and waits for SSM `Success`. It has no manual-dispatch or environment subject override. The IAM trust matches the observed immutable GitHub subject exactly: `repo:langju123456@178436820/soloscale-ai-os@1354608385:ref:refs/heads/codex/resume-intelligence-public-sync`. The optional `infra/aws/resume-demo-oidc.json` creates the exact-repository/ref role after a human supplies an existing GitHub OIDC provider ARN plus this stack's ECR, instance, and document ARNs. It does not create, adopt, or delete an OIDC provider and grants no CloudFormation, IAM, parameter-read, or arbitrary-command permission. `ssm:GetCommandInvocation` remains account-scoped read-only because AWS command receipts are addressed by generated command ID; command output must stay body-free.

## Cleanup gate

```bash
python3 scripts/aws/teardown.py --profile soloscale-demo --manifest .soloscale/resume-cloud-aws-manifest.json
python3 scripts/aws/teardown.py --profile soloscale-demo --manifest .soloscale/resume-cloud-aws-manifest.json --execute
```

The first command only prints the exact deletion plan. The second requires separate human approval because it is destructive.

## Local verification boundary

The application, PostgreSQL persistence, restart, and DOCX flow have passed local container checks. Template lint and workflow checks are local evidence only. AWS account login, CloudFormation creation, remote image execution, and GitHub deployment receipts remain pending.
