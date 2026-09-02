# ADR-0002: Deterministic code controls orchestration

**Status:** Accepted  
**Date:** 2026-08-06

## Context

LLM-controlled loops can expand scope, retry indefinitely, spend unpredictably, or claim success without deterministic evidence.

## Decision

Use ordinary code for:

- state transitions
- routing
- retry budgets
- cost budgets
- timeouts
- approval gates
- verification commands
- completion checks

Use LLMs for tasks that require language understanding, synthesis, planning, coding, or review.

## Consequences

The system is more predictable and replayable, but policies must be explicitly designed and maintained.
