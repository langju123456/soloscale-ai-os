# ADR-0001: One strong reasoning core, many bounded executors

**Status:** Accepted  
**Date:** 2026-08-06

## Context

A common multi-agent design creates many role-playing agents that read the same context, generate overlapping opinions, and require a final model to synthesize them. This increases cost and latency without guaranteeing independent errors.

The personal workflow already has access to a strong ChatGPT reasoning surface, cloud plugins, local Codex execution, and human judgment.

## Decision

Use one default reasoning core. Add a specialist only when at least one of these is true:

- it has a distinct required tool;
- it has a distinct data source;
- it has a distinct permission boundary;
- it can execute an independent parallel subtask;
- it provides a genuinely independent review;
- it operates under a different latency or cost profile.

## Consequences

### Expected positive effects — not yet measured

- less repeated context
- simpler observability
- lower token cost
- clearer responsibility
- easier evaluation

These are design hypotheses until comparative dogfood runs produce inspectable measurements.

### Negative

- the central reasoner can become a bottleneck
- correlated reasoning failures remain possible
- high-risk work may still justify an independent model or human review
