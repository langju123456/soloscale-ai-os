# ADR-0003: Preserve evidence separately from derived learning views

**Status:** Accepted
**Date:** 2026-08-09

## Context

AI coding agents can make implementation much faster than the operator's ability to
absorb the code, failure modes, and trade-offs. The most valuable details are often
scattered across chats, terminal output, diffs, tests, CI, and review receipts. A
generated summary alone is not enough for later debugging or an interview deep dive.

Raw conversations can also contain credentials, personal information, private ideas,
or unsupported claims. Automatically importing account history would widen both the
privacy boundary and the product scope.

## Decision

Add a local-first Casebook boundary:

1. The operator explicitly selects evidence files for one resolved engineering case.
2. SoloScale archives those files byte-for-byte under ignored `.soloscale/` storage and
   records SHA-256 receipts.
3. A strict `LearningCase` contains the operator-confirmed facts and explicit unknowns.
4. Interview packets and Control Tower pages are deterministic projections. They never
   embed raw evidence bodies and are not the source of truth.
5. Learning progress is derived from append-only practice attempts across five gates:
   Explain, Trace, Rebuild, Debug, and Defend.
6. Completion is labelled `SELF_ASSESSED_INTERVIEW_READY`; it is not represented as an
   external certification of mastery.

This slice does not call an LLM, scrape ChatGPT/Codex/Claude history, grade semantic
quality, publish content, or sync private evidence to a cloud service.

## Consequences

- Valuable engineering context survives after delivery and remains integrity-checkable.
- The operator can distinguish finished engineering work from unfinished understanding.
- Every self-assessed passing claim can point to a practice receipt instead of relying
  on confidence alone.
- Input framing is initially manual and selective; automatic capture and extraction are
  later product hypotheses that require privacy, redaction, and consent design.
- Local JSON/JSONL remains the factual record. Markdown and HTML can always be rebuilt.
