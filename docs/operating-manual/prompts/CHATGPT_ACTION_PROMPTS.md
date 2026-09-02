# ChatGPT Action Prompts

这些 Prompt 用于普通 ChatGPT，优先处理所有非本地推理工作。

---

## CHAT-ROUTE-001 — Master Triage & Routing

```text
You are my AI work control plane.

Classify this request into one or more surfaces:

- CHAT: research, reasoning, product, business, architecture, content, review
- PLUGIN: an online action supported by an available plugin/app
- CODEX: requires local files, uncommitted state, terminal, tests, build, or Git
- RUNTIME: requires real-time, scheduled, event-driven, or unattended execution
- HUMAN: money, production, permissions, public publishing, secrets, or irreversible actions

Request:
[PASTE REQUEST]

Output:

1. Outcome
2. Primary surface
3. Secondary surfaces
4. What can be completed in this chat now
5. Plugin actions
6. Codex Execution Packet needed? yes/no
7. Runtime/API backlog
8. Human approval points
9. Smallest next action

Do not delegate to Codex unless local state is genuinely required.
Do not create a multi-agent team unless tasks are independent or require different tools/permissions.
```

---

## CHAT-RESEARCH-001 — Research & Opportunity Analysis

```text
Act as a senior research and business analyst.

Topic:
[TOPIC]

Target user:
[USER]

Decision I need to make:
[DECISION]

Constraints:
[CONSTRAINTS]

Research:
- user pains and jobs-to-be-done
- current alternatives
- willingness-to-pay signals
- distribution channels
- operational constraints
- legal/platform risks
- evidence that would falsify the opportunity

Separate:
- verified facts
- reasonable inferences
- hypotheses requiring interviews or experiments

End with:
1. opportunity thesis
2. smallest validation experiment
3. success metric
4. stop condition
5. what must not be built yet
```

---

## CHAT-PLAN-001 — Repository-Grounded Product & Architecture Plan

```text
You are the planning and architecture stage.

Perform read-only repository analysis through the available GitHub capability.
Do not write code, create branches, commit, push, deploy, or change dependencies.

Source of truth:
- GitHub Issue: [ISSUE]
- Target repository: [REPO]
- Default branch: [BRANCH]

Tasks:

1. Trace the current implementation and data flow.
2. Identify exact relevant paths, symbols, contracts, and verification commands.
3. State the evidence-supported root problem.
4. Compare at least two implementation options.
5. Recommend the smallest safe option.
6. Define:
   - files/modules likely to change
   - public/data/type contracts
   - failure behavior
   - unit/integration/regression tests
   - security, compatibility, performance, concurrency, and rollback risks
7. Identify every conflict between the Issue and the real repository.
8. List stop conditions.
9. Produce an implementation plan, not complete code.

Final deliverables:

- Approved Plan draft
- Risk Register
- Test Plan
- Definition of Done
- Inputs required for a Codex Execution Packet
```

---

## CHAT-PACKET-001 — Generate Bounded Codex Execution Packet

```text
Convert the approved plan below into a bounded Codex Execution Packet.

Approved plan:
[PASTE OR LINK]

Requirements:

- include only facts and decisions needed for implementation
- mark product and architecture decisions as Frozen Decisions
- identify target repo, branch, paths, symbols, and commands
- specify required changes
- specify non-goals and forbidden changes
- specify acceptance criteria
- specify tests and exact verification commands
- specify stop conditions
- specify the required return report
- do not include hidden reasoning or the entire planning conversation
- do not write full implementation code

Use the CODEX_EXECUTION_PACKET template.
```

---

## CHAT-REVIEW-001 — Fresh Independent PR Review

```text
Act as an independent senior software reviewer.

You did not participate in planning or implementation.
Do not trust implementation claims unless supported by code or test evidence.

Inputs:
- Original Issue: [LINK/TEXT]
- Approved Plan: [LINK/TEXT]
- PR Diff: [LINK/TEXT]
- Changed-file context: [LINK/TEXT]
- Local verification: [TEXT]
- CI result: [LINK/TEXT]

Review:

1. requirement coverage
2. deviation from frozen decisions
3. logic and failure semantics
4. security and information disclosure
5. backward compatibility
6. schema/type integrity
7. concurrency and performance where relevant
8. test blind spots
9. unrelated scope expansion
10. rollback and operability

Classify:

- P0: blocks merge
- P1: must fix before merge
- P2: valid follow-up

For each finding:
- severity
- file and symbol
- evidence
- impact
- minimum safe remediation

End with:
APPROVE
APPROVE WITH P2 FOLLOW-UP
or
REQUEST CHANGES
```

---

## CHAT-NARRATIVE-001 — Evidence to Canonical Narrative

```text
Transform the supplied engineering evidence into a factual canonical narrative.

Evidence:
[PASTE RUN EVIDENCE]

Output:

1. user or engineering problem
2. why it mattered
3. constraints
4. options considered
5. decision and trade-off
6. implementation summary
7. verification evidence
8. failure or limitation
9. lesson
10. unsupported claims that must not be published
11. evidence map linking every quantitative or technical claim to a source

Do not optimize for a platform yet.
Do not invent metrics, results, users, or revenue.
```

---

## CHAT-CREATOR-001 — Creator Skill Distillation

```text
Analyze the selected creator samples as an abstract skill system.

Do not copy:
- exact wording
- face, voice, identity, or persona
- distinctive catchphrases
- copyrighted visual sequences

Extract:

- positioning
- audience
- hook archetypes
- narrative structures
- proof style
- pacing
- visual grammar
- emotional tone
- CTA patterns
- funnel
- strengths
- weaknesses
- attributes that fit my brand
- attributes that conflict with my brand

Then create a composite playbook combining transferable strengths with my own:
- real AI engineering evidence
- one-person company thesis
- direct, evidence-first voice
- honest limitations
- business conversion path
```

---

## CHAT-SHORTS-001 — Batch Short Video Scripts

```text
Create 10 distinct short-video scripts from one canonical evidence package.

Evidence:
[PASTE]

Audience:
[AUDIENCE]

Offer / CTA:
[CTA]

For each script provide:

- title
- hook in first 1–2 seconds
- one core claim
- evidence shown on screen
- 30–60 second voiceover
- shot list
- caption text
- platform variants for X, LinkedIn, YouTube Shorts, Instagram/Facebook Reels
- CTA
- disclosure requirement
- claim-to-evidence references

Each script must cover a different angle.
Do not create generic AI advice.
Do not invent metrics.
```

---

## CHAT-LONGVIDEO-001 — Long Video Script & Storyboard

```text
Create an 8–15 minute evidence-grounded YouTube video.

Topic:
[TOPIC]

Evidence:
[EVIDENCE]

Audience:
[AUDIENCE]

Output:

1. five title options
2. thumbnail concepts
3. first 30-second hook
4. chapter outline
5. full script
6. screen-demo moments
7. architecture visual moments
8. B-roll or motion-graphics requirements
9. honest limitations/failures
10. CTA
11. 5 candidate short clips
12. claim-to-evidence map

The video must teach something useful even if the viewer never buys anything.
```

---

## CHAT-OFFER-001 — Offer & Landing Page

```text
Design a simple offer ladder for my company.

Inputs:
- audience
- painful repeated workflow
- evidence and projects
- delivery capacity
- existing LLC/domain/email/Stripe
- desired price range

Create:

1. free lead magnet
2. low-ticket digital product
3. productized audit/service
4. higher-ticket implementation sprint
5. future recurring product

For each:
- promise
- target buyer
- scope
- non-goals
- deliverables
- proof
- qualification
- CTA
- refund/risk notes
- validation experiment

Then write landing-page copy without exaggerated claims.
```

---

## CHAT-ANALYTICS-001 — Content & Revenue Review

```text
Analyze this content and revenue dataset.

Data:
[PASTE]

Separate:
- reach
- attention quality
- trust signals
- traffic
- lead quality
- sales
- revenue

Identify:

1. which topic/format/hook worked
2. which platform role is validated
3. which CTA converted
4. which content had vanity metrics only
5. next 3 experiments
6. one thing to stop
7. one production bottleneck worth automating
8. whether any paid tool is now justified
```

---

## CHAT-WEEKLY-001 — Weekly Outcome Planning

```text
Act as my weekly operating system.

Inputs:
- current active work
- backlog
- last week's evidence
- available hours
- budget
- blockers
- current SoloScale and Creator milestones

Select only three outcomes:

1. one engineering outcome
2. one distribution/revenue outcome
3. one system-improvement outcome

For each provide:
- why now
- owner: Chat / Plugin / Codex / Human / Runtime
- exact first action
- definition of done
- stop condition
- what is explicitly deferred

Do not create a long wish list.
```
