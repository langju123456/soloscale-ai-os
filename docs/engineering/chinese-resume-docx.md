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
