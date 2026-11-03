import type {
  CitationCheck,
  ConfirmRequirement,
  EvidenceStatus,
  EventAction,
  ReviewState,
} from "./types";
import { ReviewApiError } from "./api";

export const STATE_LABEL: Record<ReviewState, string> = {
  NEEDS_REVIEW: "Needs review",
  PENDING_REVIEW: "Ready to review",
  CONFIRMED: "Confirmed",
  CORRECTED: "Corrected",
  REJECTED: "Rejected",
};

export const EVIDENCE_LABEL: Record<EvidenceStatus, string> = {
  FULLY_VERIFIED: "Sources verified",
  PARTIALLY_VERIFIED: "Partly verified",
  UNVERIFIED: "Not verified",
  HASH_MISMATCH: "Source changed",
};

export const EVIDENCE_HELP: Record<EvidenceStatus, string> = {
  FULLY_VERIFIED: "Every cited passage and its document still match what the model saw.",
  PARTIALLY_VERIFIED:
    "Some cited text still matches, but not every citation verifies. Confirming needs a note.",
  UNVERIFIED:
    "No cited passage could be matched in the current index. Confirming needs a note.",
  HASH_MISMATCH:
    "A cited passage changed after the model read it. The model text cannot be confirmed; correct or reject it.",
};

export const CHECK_LABEL: Record<CitationCheck, string> = {
  VERIFIED: "Matches the index",
  DOCUMENT_REVISED: "Passage unchanged, document revised",
  HASH_MISMATCH: "Passage changed since drafting",
  INTEGRITY_FAILED: "Index failed its integrity check",
  SOURCE_MISSING: "Passage no longer in the index",
  NOT_IN_RECEIPT: "Not recorded in the run receipt",
  UNCHECKED: "No index available to check",
};

export const CHECK_TONE: Record<CitationCheck, "ok" | "warn" | "muted" | "bad"> = {
  VERIFIED: "ok",
  DOCUMENT_REVISED: "warn",
  HASH_MISMATCH: "bad",
  INTEGRITY_FAILED: "bad",
  SOURCE_MISSING: "muted",
  NOT_IN_RECEIPT: "muted",
  UNCHECKED: "muted",
};

export const EVENT_LABEL: Record<EventAction, string> = {
  IMPORTED: "Imported from draft",
  CONFIRMED: "Confirmed",
  CORRECTED: "Corrected",
  REJECTED: "Rejected",
  REOPENED: "Reopened",
};

export const CONFIRM_HINT: Record<ConfirmRequirement, string> = {
  NONE: "",
  NOTE_REQUIRED: "Evidence is not fully verified, so confirming requires a note explaining why.",
  BLOCKED: "Confirm is unavailable: a cited source changed after drafting.",
};

/** Turn a server rejection into a sentence that says what happened and what to do next. */
export function describeError(error: unknown): { title: string; detail: string } {
  if (!(error instanceof ReviewApiError)) {
    return { title: "Request failed", detail: String(error) };
  }
  const d = error.details;
  switch (error.code) {
    case "STALE_REVISION":
      return {
        title: "Someone changed this item first",
        detail: `It is now at revision ${String(d.current_revision)} (${String(
          d.current_state,
        ).toLowerCase().replace("_", " ")}). Reload to see the latest version; your text is kept below.`,
      };
    case "INVALID_TRANSITION":
      return {
        title: "That action is not allowed now",
        detail: `${error.message}. Reload the item to see which actions are available.`,
      };
    case "NOTE_REQUIRED":
      return { title: "Add a note", detail: error.message };
    case "CONFIRM_BLOCKED_HASH_MISMATCH":
      return { title: "Confirm blocked", detail: error.message };
    case "EVIDENCE_CHANGED":
      return {
        title: "A source changed after import",
        detail: `${error.message} (chunk ${String(d.chunk_id)} is now ${String(
          d.current_check,
        )}).`,
      };
    case "NO_CHANGE":
      return { title: "Nothing to save", detail: error.message };
    case "NETWORK_ERROR":
      return { title: "Review API unreachable", detail: error.message };
    default:
      return { title: `Rejected by the server (${error.code})`, detail: error.message };
  }
}

export function shortHash(hash: string | null | undefined, length = 12): string {
  return hash ? hash.slice(0, length) : "none";
}

export function formatTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
