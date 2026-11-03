import { EVIDENCE_LABEL, STATE_LABEL } from "../lib/labels";
import type { EvidenceStatus, ReviewState } from "../lib/types";

const EVIDENCE_TONE: Record<EvidenceStatus, "ok" | "warn" | "muted" | "bad"> = {
  FULLY_VERIFIED: "ok",
  PARTIALLY_VERIFIED: "warn",
  UNVERIFIED: "muted",
  HASH_MISMATCH: "bad",
};

/** Shape and text both carry the status, so it never relies on color alone. */
function Glyph({ status }: { status: EvidenceStatus }) {
  const common = { width: 12, height: 12, viewBox: "0 0 12 12", "aria-hidden": true } as const;
  switch (status) {
    case "FULLY_VERIFIED":
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="5" fill="currentColor" />
        </svg>
      );
    case "PARTIALLY_VERIFIED":
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
          <path d="M6 1.5 A4.5 4.5 0 0 1 6 10.5 Z" fill="currentColor" />
        </svg>
      );
    case "UNVERIFIED":
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
        </svg>
      );
    case "HASH_MISMATCH":
      return (
        <svg {...common}>
          <path d="M6 0.8 L11.2 6 L6 11.2 L0.8 6 Z" fill="currentColor" />
        </svg>
      );
  }
}

export function EvidenceMark({ status }: { status: EvidenceStatus }) {
  return (
    <span className="status-mark" data-tone={EVIDENCE_TONE[status]}>
      <Glyph status={status} />
      {EVIDENCE_LABEL[status]}
    </span>
  );
}

export function StatePill({ state }: { state: ReviewState }) {
  return (
    <span className="state-pill" data-state={state}>
      {STATE_LABEL[state]}
    </span>
  );
}
