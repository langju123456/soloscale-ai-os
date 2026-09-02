# Plugin / App Action Prompts

只有 Plugin 已连接且支持对应动作时使用。所有写入、部署和公开动作仍需确认。

---

## PLUGIN-GH-001 — Create Structured Dogfood Issue

```text
@GitHub

Create a new Issue in:
langju123456/solo-scale-ai-os

Title:
Dogfood #1: Add source-grounded citations to AI Research Assistant

Use the supplied Task Envelope and contract.
Include:

- hypothesis
- target repository
- business goal
- current problem
- frozen decisions
- required analysis
- acceptance criteria
- non-goals
- stop conditions
- evidence required
- experiment metrics
- definition of done

Do not create a branch, PR, commit, deployment, or code change.
Return the Issue URL and exact final body.
```

---

## PLUGIN-GH-002 — Create PR Metadata

```text
@GitHub

Create a pull request for the already-pushed approved feature branch.

Repository:
[REPO]

Base:
[BASE]

Head:
[HEAD]

Use only the supplied:
- Issue
- Approved Plan
- verification report
- commit history

PR body must include:
- what changed
- why
- non-goals
- test evidence
- compatibility impact
- known limitations
- rollback
- SoloScale run ID
- linked Issue

Do not merge.
```

---

## PLUGIN-FIGMA-001 — SoloScale Architecture Board

```text
@Figma

Create an editable architecture board titled:
SoloScale AI OS — One Brain, Bounded Execution Surfaces

Visualize:

Lang / Human
→ ChatGPT Control Plane
→ Plugins / Apps
→ Codex Local Executor
→ Deterministic Verification
→ Fresh Review
→ GitHub Evidence
→ BuildLog
→ Creator Video Factory
→ Distribution / Revenue
→ Feedback Backlog

Use reusable components:
- system boundary
- decision node
- execution surface
- approval gate
- evidence artifact
- metric loop

Do not add unsupported product claims or performance metrics.
Return the editable Figma link.
```

---

## PLUGIN-FIGMA-002 — Evidence-to-Narrative Carousel

```text
@Figma

Create an editable 6-page 4:5 carousel from this verified evidence brief:
[BRIEF]

Pages:

1. contrarian hook
2. old workflow/problem
3. new architecture
4. concrete evidence
5. limitation or failure
6. CTA

Use the existing SoloScale visual language.
Keep text concise.
Add source labels for quantitative claims.
Do not publish.
```

---

## PLUGIN-VERCEL-001 — Preview Deployment Only

```text
@Vercel

Create or update a Preview deployment for the approved branch.

Project:
[PROJECT]

Branch:
[BRANCH]

Requirements:
- preview only, not production
- do not change billing
- do not add domains
- do not expose secrets
- report build status, preview URL, logs, and detected configuration issues

Stop before any production promotion.
```

---

## PLUGIN-AVATAR-001 — Avatar Segment Generation

```text
@HeyGen

Generate only the approved avatar segments for this video:

[APPROVED SCRIPT SEGMENTS]

Use:
- my authorized personal avatar
- my authorized voice
- approved brand background
- exact requested language

Return separate A-roll clips for:
- hook
- transition
- CTA

Do not create the entire technical video as a continuous talking-head avatar.
Do not publish.
```

---

## PLUGIN-CLIP-001 — Long-to-Short Candidates

```text
@OpusClip

Create short-form candidates from this approved master video:
[VIDEO]

Goals:
- identify 10 coherent clips
- preserve technical meaning
- generate vertical framing and captions
- do not add unsupported B-roll or claims
- do not publish automatically

Return:
- clip title
- timestamps
- duration
- suggested hook
- caption
- platform fit
- export links

Human selection is required before publishing.
```

---

## PLUGIN-PUBLISH-001 — Explicitly Approved Publishing

```text
Publish the attached final, human-approved asset to:
[PLATFORM]

Use:
- exact final caption
- exact final media
- approved disclosure
- approved CTA and URL

Before publishing, show:
- platform
- account
- caption
- media
- disclosure
- final URL target

Wait for my explicit confirmation:
PUBLISH

Do not cross-post to any other platform.
```
