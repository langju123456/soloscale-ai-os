# Resume end-user UI

## Outcome

The default local UI route is now a resume-first workflow:

1. upload an existing DOCX resume template;
2. paste a Job Description and optional job metadata;
3. generate a local evidence/coverage run;
4. preview the locally rendered final document in the browser; and
5. download a tailored DOCX.

Developer-oriented Knowledge Store, Evidence Agent, model, source, and graph controls moved
to `/advanced`. They remain available without competing with the primary user outcome.

## Truth and privacy boundary

DOCX handling uses the Python standard library and runs locally. The uploaded template is
the candidate-fact authority. Tailoring only reorders intact project blocks and technical
skill bullets by deterministic JD term overlap. It does not paraphrase, add, or delete
candidate claims, and it makes no network call.

The generated DOCX is written byte-identically to both:

- `.soloscale/resume-runs/<run-id>/08_resume.docx`; and
- the non-overwriting application directory under
  `~/Documents/Resume Applications/applications/`.

The private run also records the source/output checksums and the exact external save path in
`09_user_ui.json`. The uploaded template body is not duplicated into tracked files.

## Inline document preview

When a local LibreOffice renderer is available, the UI converts the generated DOCX through
an isolated temporary profile and saves a private `10_resume_preview.pdf` artifact. The
success panel embeds that PDF with page scrolling and an optional new-window view, so the
operator can inspect content and layout before downloading the DOCX. Conversion is local
and makes no network call. If no renderer is available, generation still succeeds and the
UI falls back to the grounded text preview.

## Template fidelity

The source DOCX package is copied intact. Only `word/document.xml` is serialized after safe
paragraph reordering; styles, numbering, theme, relationships, custom XML, and other package
parts retain their original bytes. Final acceptance requires rendering the generated DOCX
and visually checking every page.
