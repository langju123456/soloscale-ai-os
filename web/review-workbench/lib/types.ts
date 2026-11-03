// Mirrors src/soloscale/review_models.py. The server is the source of truth for every
// rule; the UI only renders what these responses say is allowed.

export type ReviewState =
  | "PENDING_REVIEW"
  | "NEEDS_REVIEW"
  | "CONFIRMED"
  | "CORRECTED"
  | "REJECTED";

export type EvidenceStatus =
  | "FULLY_VERIFIED"
  | "PARTIALLY_VERIFIED"
  | "UNVERIFIED"
  | "HASH_MISMATCH";

export type CitationCheck =
  | "VERIFIED"
  | "DOCUMENT_REVISED"
  | "HASH_MISMATCH"
  | "INTEGRITY_FAILED"
  | "SOURCE_MISSING"
  | "NOT_IN_RECEIPT"
  | "UNCHECKED";

export type ReviewAction = "confirm" | "correct" | "reject" | "reopen";
export type ConfirmRequirement = "NONE" | "NOTE_REQUIRED" | "BLOCKED";
export type EventAction = "IMPORTED" | "CONFIRMED" | "CORRECTED" | "REJECTED" | "REOPENED";

export interface ReviewItemSummary {
  item_id: string;
  run_id: string;
  claim_index: number;
  state: ReviewState;
  evidence_status: EvidenceStatus;
  revision: number;
  current_version: number;
  current_text: string;
  citation_count: number;
  verified_citation_count: number;
  confirm_requirement: ConfirmRequirement;
  allowed_actions: ReviewAction[];
  updated_at: string;
}

export interface ReviewCitation {
  ordinal: number;
  chunk_id: string;
  document_id: string | null;
  source_kind: string | null;
  external_id: string | null;
  title: string | null;
  role: string | null;
  source_timestamp: string | null;
  recorded_chunk_sha256: string | null;
  recorded_document_sha256: string | null;
  model_visible_excerpt: string | null;
  check: CitationCheck;
  current_chunk_sha256: string | null;
  current_document_sha256: string | null;
  document_changed: boolean;
  checked_at: string;
}

export interface TextVersion {
  version: number;
  text: string;
  text_sha256: string;
  author_kind: "MODEL" | "REVIEWER";
  reviewer: string | null;
  note: string | null;
  created_at: string;
}

export interface ReviewEvent {
  event_id: number;
  action: EventAction;
  from_state: ReviewState | null;
  to_state: ReviewState;
  from_revision: number | null;
  to_revision: number;
  text_version: number;
  reviewer: string | null;
  note: string | null;
  created_at: string;
}

export interface ReviewRunSummary {
  run_id: string;
  question: string;
  model: string;
  prompt_version: string;
  draft_created_at: string;
  draft_sha256: string;
  input_manifest_sha256: string;
  retrieval_manifest_sha256: string;
  knowledge_index_checked: boolean;
  imported_at: string;
  item_count: number;
  state_counts: Record<ReviewState, number>;
  gaps: { unsupported: string[]; open_questions: string[]; limitations: string[] };
}

export interface ReviewItemDetail {
  item: ReviewItemSummary;
  run: ReviewRunSummary;
  original_text: string;
  citations: ReviewCitation[];
  versions: TextVersion[];
  events: ReviewEvent[];
}

export interface DraftCandidate {
  run_id: string;
  question: string;
  model: string;
  created_at: string;
  claim_count: number;
  imported: boolean;
}

export interface DraftListing {
  drafts: DraftCandidate[];
  skipped_invalid: number;
}

export interface ImportResponse {
  created: boolean;
  run: ReviewRunSummary;
  items: ReviewItemSummary[];
}

export interface DiffOp {
  op: "equal" | "insert" | "delete";
  text: string;
}

export interface VersionDiff {
  item_id: string;
  from_version: number;
  to_version: number;
  from_text: string;
  to_text: string;
  ops: DiffOp[];
}

export interface ApiErrorBody {
  code: string;
  message?: string;
  [detail: string]: unknown;
}
