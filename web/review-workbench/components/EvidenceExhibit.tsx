import { CHECK_LABEL, CHECK_TONE, formatTime, shortHash } from "../lib/labels";
import type { ReviewCitation } from "../lib/types";

const SOURCE_KIND_LABEL: Record<string, string> = {
  codex_session: "Codex session",
  chatgpt_export: "ChatGPT export",
  buildlog_run: "BuildLog run",
};

/**
 * One cited source: the exact excerpt the model saw (from the run receipt), where it came
 * from, and its content fingerprint when drafted compared with the knowledge index now.
 */
export function EvidenceExhibit({ citation }: { citation: ReviewCitation }) {
  const tone = CHECK_TONE[citation.check];
  const chunkDiffers =
    citation.current_chunk_sha256 !== null &&
    citation.current_chunk_sha256 !== citation.recorded_chunk_sha256;
  const documentDiffers = citation.document_changed;
  return (
    <article className="exhibit" data-tone={tone} aria-label={`Cited source ${citation.ordinal + 1}`}>
      <div className="exhibit-rule" aria-hidden="true" />
      <div className="exhibit-body">
        {citation.model_visible_excerpt ? (
          <blockquote className="exhibit-quote">{citation.model_visible_excerpt}</blockquote>
        ) : (
          <p className="exhibit-quote exhibit-quote-missing">
            The run receipt has no excerpt for this citation.
          </p>
        )}

        <div className="exhibit-source">
          <strong>{citation.title ?? citation.external_id ?? "Untitled source"}</strong>
          {citation.source_kind ? (
            <span>{SOURCE_KIND_LABEL[citation.source_kind] ?? citation.source_kind}</span>
          ) : null}
          {citation.role ? <span>{citation.role} turn</span> : null}
          {citation.source_timestamp ? <span>{formatTime(citation.source_timestamp)}</span> : null}
        </div>

        <dl className="facts">
          <dt>Chunk</dt>
          <dd className="ident">{citation.chunk_id}</dd>
          {citation.document_id ? (
            <>
              <dt>Document</dt>
              <dd className="ident">{citation.document_id}</dd>
            </>
          ) : null}
        </dl>

        <div className="fingerprint" role="group" aria-label="Passage fingerprint">
          <span className="fingerprint-label">Passage when drafted</span>
          <span className="fingerprint-hash" title={citation.recorded_chunk_sha256 ?? undefined}>
            {shortHash(citation.recorded_chunk_sha256)}
          </span>
          <span />
          <span className="fingerprint-label">Passage in index now</span>
          <span
            className="fingerprint-hash"
            data-diff={chunkDiffers}
            title={citation.current_chunk_sha256 ?? undefined}
          >
            {shortHash(citation.current_chunk_sha256)}
          </span>
          <span />
          {documentDiffers ? (
            <>
              <span className="fingerprint-label">Document now</span>
              <span
                className="fingerprint-hash"
                data-diff={chunkDiffers ? "true" : "revised"}
                title={citation.current_document_sha256 ?? undefined}
              >
                {shortHash(citation.current_document_sha256)}
              </span>
              <span />
            </>
          ) : null}
          <span className="fingerprint-verdict" data-tone={tone}>
            {CHECK_LABEL[citation.check]}
            <span className="fingerprint-label"> (checked at import, {formatTime(citation.checked_at)})</span>
          </span>
        </div>
      </div>
    </article>
  );
}
