"use client";

import { useEffect, useState } from "react";

import { ReviewApi } from "../lib/api";
import {
  CONFIRM_HINT,
  EVENT_LABEL,
  EVIDENCE_HELP,
  EVIDENCE_LABEL,
  STATE_LABEL,
  describeError,
  formatTime,
  shortHash,
} from "../lib/labels";
import type { ReviewAction, ReviewItemDetail, VersionDiff } from "../lib/types";
import { DiffView } from "./DiffView";
import { EvidenceExhibit } from "./EvidenceExhibit";
import { EvidenceMark, StatePill } from "./marks";

type Mode = ReviewAction | null;

const ACTION_LABEL: Record<ReviewAction, string> = {
  confirm: "Confirm",
  correct: "Correct",
  reject: "Reject",
  reopen: "Reopen",
};

interface Props {
  api: ReviewApi;
  detail: ReviewItemDetail;
  reviewer: string;
  onChanged: (detail: ReviewItemDetail) => void;
  onReload: () => Promise<void>;
}

export function ItemView({ api, detail, reviewer, onChanged, onReload }: Props) {
  const { item } = detail;
  const [mode, setMode] = useState<Mode>(null);
  const [note, setNote] = useState("");
  const [text, setText] = useState(item.current_text);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [flash, setFlash] = useState(false);

  // A different item resets the form; a reload of the same item keeps the reviewer's draft.
  useEffect(() => {
    setMode(null);
    setNote("");
    setText(item.current_text);
    setError(null);
  }, [item.item_id]); // eslint-disable-line react-hooks/exhaustive-deps

  const noteRequired =
    mode === "correct" ||
    mode === "reject" ||
    mode === "reopen" ||
    (mode === "confirm" && item.confirm_requirement === "NOTE_REQUIRED");

  function open(action: ReviewAction) {
    setError(null);
    setMode(action);
    if (action === "correct" && mode !== "correct") setText(item.current_text);
  }

  async function submit() {
    if (!mode) return;
    setBusy(true);
    setError(null);
    // The revision the reviewer is looking at; the server rejects the write if it moved.
    const input = { expectedRevision: item.revision, note: note.trim(), reviewer };
    try {
      let next: ReviewItemDetail;
      if (mode === "confirm") next = await api.confirm(item.item_id, input);
      else if (mode === "correct") next = await api.correct(item.item_id, { ...input, text });
      else if (mode === "reject") next = await api.reject(item.item_id, input);
      else next = await api.reopen(item.item_id, input);
      setMode(null);
      setNote("");
      setText(next.item.current_text);
      setFlash(true);
      window.setTimeout(() => setFlash(false), 1000);
      onChanged(next);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  const described = error ? describeError(error) : null;
  const errorCode =
    error && typeof error === "object" && "code" in error ? String(error.code) : null;
  const edited = item.current_version > 1;

  return (
    <>
      <section className="claim-panel" aria-labelledby="claim-heading">
        <div className="claim-status">
          <StatePill state={item.state} />
          <EvidenceMark status={item.evidence_status} />
          <span className="claim-provenance">
            Claim {item.claim_index + 1} of {detail.run.item_count}, revision {item.revision}
            {edited ? `, text version ${item.current_version}` : ""}
          </span>
        </div>
        <h2 id="claim-heading" className="visually-hidden">
          Claim under review
        </h2>
        <p className="claim-text" data-flash={flash}>
          {item.current_text}
        </p>
        {edited ? (
          <div className="original-text">
            <strong>Original model text (version 1, kept unchanged)</strong>
            {detail.original_text}
          </div>
        ) : null}
        {item.allowed_actions.includes("reopen") ? null : (
          <p className="evidence-help">{EVIDENCE_HELP[item.evidence_status]}</p>
        )}
        <p className="claim-provenance">
          From the question “{detail.run.question}”, drafted by {detail.run.model} on{" "}
          {formatTime(detail.run.draft_created_at)}.
        </p>

        <div className="actions" role="toolbar" aria-label="Review actions">
          {item.allowed_actions.map((action) => (
            <button
              key={action}
              type="button"
              className="button"
              data-variant={action === "reject" ? "danger" : undefined}
              aria-pressed={mode === action}
              onClick={() => open(action)}
            >
              {ACTION_LABEL[action]}
            </button>
          ))}
          {item.confirm_requirement === "BLOCKED" && item.allowed_actions.includes("correct") ? (
            <span className="evidence-help">{CONFIRM_HINT.BLOCKED}</span>
          ) : null}
        </div>

        {mode ? (
          <form
            className="action-form"
            onSubmit={(event) => {
              event.preventDefault();
              void submit();
            }}
          >
            {mode === "correct" ? (
              <label>
                Corrected text
                <span className="hint">
                  Saved as a new version. The model&apos;s original stays as version 1.
                </span>
                <textarea
                  className="correction"
                  value={text}
                  onChange={(event) => setText(event.target.value)}
                  rows={4}
                />
              </label>
            ) : null}
            <label>
              {noteRequired ? "Reviewer note (required)" : "Reviewer note (optional)"}
              {mode === "confirm" && item.confirm_requirement === "NOTE_REQUIRED" ? (
                <span className="hint">{CONFIRM_HINT.NOTE_REQUIRED}</span>
              ) : null}
              <textarea
                value={note}
                onChange={(event) => setNote(event.target.value)}
                rows={2}
                placeholder={
                  mode === "reject"
                    ? "Why this claim should not be used"
                    : mode === "correct"
                      ? "What was wrong and what the source supports"
                      : "What you checked"
                }
              />
            </label>
            {described ? (
              <div className="server-error" role="alert">
                <strong>{described.title}</strong>
                <span>{described.detail}</span>
                {errorCode ? <code>Server response: {errorCode}</code> : null}
                {errorCode === "STALE_REVISION" || errorCode === "INVALID_TRANSITION" ? (
                  <div className="action-row">
                    <button type="button" className="button" onClick={() => void onReload()}>
                      Reload item
                    </button>
                  </div>
                ) : null}
              </div>
            ) : null}
            <div className="action-row">
              <button
                type="submit"
                className="button"
                data-variant="primary"
                disabled={busy || (noteRequired && note.trim() === "")}
              >
                {busy ? "Saving" : saveLabel(mode)}
              </button>
              <button type="button" className="button" data-variant="quiet" onClick={() => setMode(null)}>
                Cancel
              </button>
              <span className="hint evidence-help">
                Sends revision {item.revision}; the server rejects it if the item changed.
              </span>
            </div>
          </form>
        ) : null}
      </section>

      <section aria-labelledby="evidence-heading">
        <div className="section-heading">
          <h2 id="evidence-heading">Cited evidence</h2>
          <p>
            {item.verified_citation_count} of {item.citation_count} verified at import
          </p>
        </div>
        <div className="exhibits">
          {detail.citations.map((citation) => (
            <EvidenceExhibit key={citation.ordinal} citation={citation} />
          ))}
        </div>
      </section>

      <ChangesPanel api={api} detail={detail} />
    </>
  );
}

function saveLabel(mode: ReviewAction): string {
  switch (mode) {
    case "confirm":
      return "Confirm claim";
    case "correct":
      return "Save correction";
    case "reject":
      return "Reject claim";
    case "reopen":
      return "Reopen for review";
  }
}

function ChangesPanel({ api, detail }: { api: ReviewApi; detail: ReviewItemDetail }) {
  const latest = detail.item.current_version;
  const [from, setFrom] = useState(1);
  const [to, setTo] = useState(latest);
  const [diff, setDiff] = useState<VersionDiff | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    setFrom(1);
    setTo(latest);
  }, [detail.item.item_id, latest]);

  useEffect(() => {
    if (latest < 2) {
      setDiff(null);
      return;
    }
    let cancelled = false;
    api
      .getDiff(detail.item.item_id, from, to)
      .then((value) => {
        if (!cancelled) {
          setDiff(value);
          setError(null);
        }
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught);
      });
    return () => {
      cancelled = true;
    };
  }, [api, detail.item.item_id, from, to, latest]);

  return (
    <section className="diff-panel" aria-labelledby="changes-heading">
      <div className="section-heading">
        <h2 id="changes-heading">Changes to the model text</h2>
        {latest > 1 ? (
          <div className="diff-legend" aria-hidden="true">
            <del>removed</del>
            <ins>added</ins>
          </div>
        ) : null}
      </div>
      {latest < 2 ? (
        <p className="evidence-help">No edits yet. The text above is the model&apos;s original.</p>
      ) : (
        <>
          <div className="diff-controls">
            <label>
              Compare version{" "}
              <select value={from} onChange={(event) => setFrom(Number(event.target.value))}>
                {detail.versions.map((version) => (
                  <option key={version.version} value={version.version}>
                    {version.version}
                    {version.author_kind === "MODEL" ? " (model)" : ""}
                  </option>
                ))}
              </select>
            </label>
            <label>
              with{" "}
              <select value={to} onChange={(event) => setTo(Number(event.target.value))}>
                {detail.versions.map((version) => (
                  <option key={version.version} value={version.version}>
                    {version.version}
                    {version.version === latest ? " (current)" : ""}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {error ? (
            <div className="server-error" role="alert">
              <strong>{describeError(error).title}</strong>
              <span>{describeError(error).detail}</span>
            </div>
          ) : diff ? (
            <DiffView ops={diff.ops} />
          ) : null}
        </>
      )}
    </section>
  );
}

export function HistoryPanel({ detail }: { detail: ReviewItemDetail }) {
  const { run } = detail;
  const gapCount =
    run.gaps.unsupported.length + run.gaps.open_questions.length + run.gaps.limitations.length;
  return (
    <>
      <section aria-labelledby="log-heading">
        <div className="section-heading">
          <h2 id="log-heading">Review log</h2>
          <p>Append-only</p>
        </div>
        <ol className="timeline">
          {detail.events.map((event) => (
            <li key={event.event_id} data-action={event.action}>
              <div className="timeline-title">{EVENT_LABEL[event.action]}</div>
              <div className="timeline-meta">
                {event.from_state ? `${STATE_LABEL[event.from_state]} to ` : ""}
                {STATE_LABEL[event.to_state]}, revision {event.to_revision}
                {event.action === "CORRECTED" ? `, saved version ${event.text_version}` : ""}
              </div>
              <div className="timeline-meta">
                {event.reviewer ? `${event.reviewer}, ` : ""}
                {formatTime(event.created_at)}
              </div>
              {event.action === "IMPORTED" ? (
                <p className="timeline-meta">
                  Evidence at import: {EVIDENCE_LABEL[detail.item.evidence_status].toLowerCase()}
                </p>
              ) : event.note ? (
                <p className="timeline-note">{event.note}</p>
              ) : null}
            </li>
          ))}
        </ol>
      </section>

      <section aria-labelledby="versions-heading">
        <div className="section-heading">
          <h2 id="versions-heading">Text versions</h2>
          <p>{detail.versions.length} stored</p>
        </div>
        <div className="versions">
          {[...detail.versions].reverse().map((version) => (
            <article
              key={version.version}
              className="version"
              data-current={version.version === detail.item.current_version}
            >
              <div className="version-head">
                <span>
                  <strong>Version {version.version}</strong>{" "}
                  {version.author_kind === "MODEL" ? "model draft" : `by ${version.reviewer ?? "reviewer"}`}
                </span>
                <span className="ident" title={version.text_sha256}>
                  {shortHash(version.text_sha256, 8)}
                </span>
              </div>
              <p className="version-text">{version.text}</p>
              {version.note ? <p className="timeline-meta">Note: {version.note}</p> : null}
              <p className="timeline-meta">{formatTime(version.created_at)}</p>
            </article>
          ))}
        </div>
      </section>

      {gapCount > 0 ? (
        <details className="gaps">
          <summary>What the draft could not support ({gapCount})</summary>
          <ul>
            {run.gaps.unsupported.map((gap) => (
              <li key={`u-${gap}`}>Unsupported: {gap}</li>
            ))}
            {run.gaps.open_questions.map((gap) => (
              <li key={`q-${gap}`}>Open question: {gap}</li>
            ))}
            {run.gaps.limitations.map((gap) => (
              <li key={`l-${gap}`}>Limitation: {gap}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </>
  );
}
