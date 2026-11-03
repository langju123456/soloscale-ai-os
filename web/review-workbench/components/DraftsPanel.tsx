"use client";

import { useEffect, useState } from "react";

import { ReviewApi } from "../lib/api";
import { describeError, formatTime } from "../lib/labels";
import type { DraftListing, ImportResponse } from "../lib/types";

interface Props {
  api: ReviewApi;
  onImported: (response: ImportResponse) => void;
}

/** Completed Evidence Agent runs found under the API's data root, ready to import. */
export function DraftsPanel({ api, onImported }: Props) {
  const [listing, setListing] = useState<DraftListing | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busyRun, setBusyRun] = useState<string | null>(null);

  async function refresh() {
    try {
      setListing(await api.listDrafts());
      setError(null);
    } catch (caught) {
      setError(caught);
    }
  }

  useEffect(() => {
    void refresh();
  }, [api]); // eslint-disable-line react-hooks/exhaustive-deps

  async function importRun(runId: string) {
    setBusyRun(runId);
    setError(null);
    try {
      const response = await api.importDraft(runId);
      onImported(response);
      await refresh();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusyRun(null);
    }
  }

  return (
    <section className="drafts-panel" aria-labelledby="drafts-heading">
      <div className="section-heading">
        <h2 id="drafts-heading">Evidence Agent drafts</h2>
        <p>Importing re-checks every citation against the knowledge index.</p>
      </div>
      {error ? (
        <div className="server-error" role="alert">
          <strong>{describeError(error).title}</strong>
          <span>{describeError(error).detail}</span>
        </div>
      ) : null}
      {listing && listing.drafts.length === 0 ? (
        <p className="empty-note">
          No completed drafts were found. Run <code>soloscale evidence-agent</code>, or create
          the sample run with <code>python -m soloscale.review_sample</code>.
        </p>
      ) : null}
      {listing && listing.skipped_invalid > 0 ? (
        <p className="empty-note">
          {listing.skipped_invalid} run(s) were skipped because their receipt is invalid.
        </p>
      ) : null}
      {listing ? (
        <ul className="drafts-list">
          {listing.drafts.map((draft) => (
            <li key={draft.run_id} className="draft-row">
              <span className="draft-question">{draft.question}</span>
              <span className="draft-meta">
                {draft.claim_count} claims, {draft.model}, {formatTime(draft.created_at)}
              </span>
              <button
                type="button"
                className="button"
                data-variant={draft.imported ? undefined : "primary"}
                disabled={busyRun !== null}
                onClick={() => void importRun(draft.run_id)}
              >
                {busyRun === draft.run_id
                  ? "Importing"
                  : draft.imported
                    ? "Show claims"
                    : "Import claims"}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
