# Chinese Resume DOCX Parsing

The Resume DOCX parser recognizes the established Chinese headings `教育背景` and
`专业技能`. A paragraph with Word numbering or a leading literal `•` is a profile
bullet. Its visible text remains the source fact, including the marker.

Technical-skills extraction retains every nonblank line within the skills section so
templates that mix plain text, numbered paragraphs, and literal bullets can be ranked.
Both template-only and model-backed priority reorder paths use that same set of skill
lines. When a model rewrite omits the literal marker from a source bullet, rendering
restores the source paragraph's original marker once; Candidate Profile text and
provenance identities remain unchanged.

This behavior is covered with synthetic Chinese fixtures only. Real resumes and local
dogfood artifacts remain outside the repository.

For Chinese output, an exact cited `PROFILE_ENTRY` fact remains valid when the model
preserves its text after whitespace normalization and omits the source's leading literal
`•` marker. This is only an anchor check: source identity, protected facts, numbers,
technology, ownership, inflation, and multi-fact representation checks still apply.
Resume-only local Ollama requests use a validated `num_ctx=16384` option; other Ollama
callers retain their existing default request options.

Project reordering moves each heading, any following plain descriptive paragraph, and its
bullets together. A following non-bullet begins another project only after the current
project contains a bullet.
