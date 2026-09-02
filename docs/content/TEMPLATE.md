# Evidence-to-Multichannel Content Package — {Run or decision}

Use this template after a run or decision has inspectable evidence. Delete instructional text and unresolved placeholders before publication.

## 1. Source

- Run / task ID:
- Date:
- Repository / PR / issue:
- Owner:
- Audience:
- Publication gate owner:

## 2. Evidence inventory

Use `VERIFIED` only when the linked receipt supports the exact claim. Use `OBSERVED` for a dated personal observation, `HYPOTHESIS` for an unmeasured prediction, and `PLANNED` for future work.

| Evidence ID | Fact or observation | Receipt | Status | Limits |
| --- | --- | --- | --- | --- |
| E-01 | {What happened} | [PROOF: command output, diff, test, screenshot, or event] | VERIFIED | {What this does not prove} |
| E-02 | {What was noticed} | [PROOF: dated note or owner confirmation] | OBSERVED | {Subjective or incomplete context} |

## 3. Claim ledger

| Claim ID | Proposed claim | Evidence IDs | Classification | Publication wording |
| --- | --- | --- | --- | --- |
| C-01 | {Exact claim} | E-01 | VERIFIED | {Direct factual wording} |
| C-02 | {Expected benefit} | — | HYPOTHESIS | “My hypothesis is…” |
| C-03 | {Next step} | — | PLANNED | “The next experiment will…” |

## 4. Story spine

- Trigger: {The specific friction or surprising origin}
- Observation: {What was seen, without invented causality}
- Decision: {What changed}
- Alternative rejected: {What was not chosen and why}
- Current proof: {Evidence IDs}
- Limitation: {What v0.1 cannot do or what remains uncertain}
- Next experiment: {What will be measured}

## 5. X draft

1/ {Origin and hook}

2/ {New model or decision}

3/ {Verified implementation fact + proof placeholder}

4/ {Limitation}

5/ {Hypothesis and measurement plan}

Editorial proof to attach: [PROOF: public URLs for the cited receipts]

## 6. LinkedIn draft

{Origin in first person.}

{Decision and mental model.}

{Verified evidence, with no stronger conclusion than the receipts support.}

{Explicit v0.1 limitation.}

{Hypothesis and next measured experiment.}

Editorial proof to attach: [PROOF: public URLs for the cited receipts]

## 7. Visual brief

- Format and dimensions:
- One-sentence takeaway:
- Source diagram / editable file: [PROOF: editable source URL]
- Evidence labels shown in artwork: `OBSERVED`, `VERIFIED`, `HYPOTHESIS`, `PLANNED`
- Data callouts: [PROOF: receipt for every number]
- Visual exclusions: {Private data, raw prompts, unsupported comparisons}

## 8. Alt text

- Short alt text (<160 characters):
- Long description: {Reading order, nodes, connections, labels, and takeaway}
- Decorative elements intentionally omitted:

## 9. Publication gate

- [ ] Every factual claim maps to a receipt.
- [ ] Every number has a public proof link or has been removed.
- [ ] Unmeasured outcomes are labeled as hypotheses.
- [ ] Planned capabilities are written in future tense.
- [ ] The limitation is visible, not buried.
- [ ] Raw conversations, secrets, customer data, and private prompts are absent.
- [ ] Alt text conveys the same conclusion as the visual.
- [ ] A human approved the final public action.
