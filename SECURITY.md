# Security Policy

## Supported versions

SoloScale AI OS is pre-1.0. Security fixes are applied to the latest commit on `main`.

## Reporting a vulnerability

Do not open a public Issue for a vulnerability that could expose secrets, user data, repository access, or unsafe command execution.

Use GitHub's private vulnerability reporting for the repository once it is enabled. Until the public repository exists, contact the repository owner privately and include:

- the affected component and version or commit;
- reproducible steps;
- the expected and observed behavior;
- the likely impact;
- any safe mitigation you have tested.

Do not include real credentials or personal data in a report. A response target will be published after the repository's security channel is configured.

## Security boundaries

The current v0.1 is a local, human-controlled workflow tool. It must not be treated as a safe multi-tenant executor. Production deployment, arbitrary shell execution, secret access, permission changes, destructive actions, and public publishing require explicit human approval.
