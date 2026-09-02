# SoloScale Casebook v0.1

Casebook is the local learning and interview-preparation slice of SoloScale. It
converts one resolved AI-assisted engineering problem into an integrity-checkable
case and a five-stage practice loop.

## Why it exists

Coding agents can finish work faster than a person can absorb it. The code may be
merged while the operator still cannot independently explain the failure boundary,
trace the implementation, rebuild a minimal version, debug an unseen variant, or
defend the trade-offs.

Casebook keeps these as separate truths:

```text
Engineering delivery: complete or incomplete
Evidence integrity: verified or failing
Human practice: Explain → Trace → Rebuild → Debug → Defend
```

It never converts “the agent implemented it” into “the operator mastered it.”

## Privacy and evidence boundary

Only evidence explicitly passed to `case-create` and practice artifacts explicitly
passed to `case-attempt` are archived. They are copied into ignored
`.soloscale/cases/<case-id>/` storage and recorded with SHA-256 and byte-size receipts.

- Raw evidence bodies are not embedded in the interview packet or Control Tower.
- Case facts are supplied explicitly; SoloScale does not infer a root cause from a
  transcript.
- Generated Markdown and HTML are projections. `case.json` and `attempts.jsonl`
  remain the source of truth.
- `.soloscale/` is intentionally ignored by Git. Do not move private evidence into
  tracked documentation without a separate redaction and review step.
- The CLI refuses an in-repository `--data-root` unless it is beneath a directory
  named `.soloscale` and `git check-ignore` confirms the selected root is ignored.
  This starter ignores that name at every depth. A custom root outside a Git worktree
  remains available for deliberately managed private storage.

Automatic account-history ingestion, secret redaction, cloud sync, LLM extraction,
semantic grading, resume generation, and publishing are not part of v0.1.

## Practice contract

Every passing practice attempt requires a non-empty archived receipt. Examples:

| Stage | Suitable receipt |
|---|---|
| Explain | an unaided written or recorded 60-second explanation |
| Trace | a code/data-flow diagram tied to real paths |
| Rebuild | a minimal implementation and test output |
| Debug | a diagnosis of an unseen failure variant |
| Defend | written answers to architecture and trade-off follow-ups |

Attempts are append-only. The latest attempt determines each stage, so recording a
later `needs-work` result correctly removes readiness. Passing all five stages yields
`SELF_ASSESSED_INTERVIEW_READY`, not an external certification.

## Dogfood example

The files in `examples/casebook/` describe the completed source-grounded citations
feature. They contain no raw chat transcript, local path, credential, or private prompt.
The README quick start shows how to turn them into the first local case.

## Product validation gate

Do not infer commercial demand from the founder's own need. Extend toward SaaS only
after observing at least five other heavy coding-agent users create real cases. The
immediate dogfood signal is simpler: one case created in under ten minutes and at
least three practice stages completed with genuine receipts.
