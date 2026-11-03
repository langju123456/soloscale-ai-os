import { EvidenceMark, StatePill } from "./marks";
import type { ReviewItemSummary, ReviewState } from "../lib/types";

export type QueueFilter = "needs" | "pending" | "decided" | "all";

const FILTER_STATES: Record<QueueFilter, ReadonlySet<ReviewState> | null> = {
  needs: new Set(["NEEDS_REVIEW"]),
  pending: new Set(["PENDING_REVIEW"]),
  decided: new Set(["CONFIRMED", "CORRECTED", "REJECTED"]),
  all: null,
};

const FILTER_LABEL: Record<QueueFilter, string> = {
  needs: "Needs review",
  pending: "Ready",
  decided: "Decided",
  all: "All",
};

export function filterItems(items: ReviewItemSummary[], filter: QueueFilter): ReviewItemSummary[] {
  const states = FILTER_STATES[filter];
  // The server already orders the queue (low confidence first); filtering keeps that order.
  return states ? items.filter((item) => states.has(item.state)) : items;
}

interface Props {
  items: ReviewItemSummary[];
  filter: QueueFilter;
  selectedId: string | null;
  onFilter: (filter: QueueFilter) => void;
  onSelect: (itemId: string) => void;
}

export function Queue({ items, filter, selectedId, onFilter, onSelect }: Props) {
  const visible = filterItems(items, filter);
  return (
    <nav className="queue" aria-label="Review queue">
      <div className="queue-heading">
        <h2>Queue</h2>
        <span className="evidence-help">{items.length} claims</span>
      </div>
      <div className="filters" role="group" aria-label="Filter by review state">
        {(Object.keys(FILTER_LABEL) as QueueFilter[]).map((key) => (
          <button
            key={key}
            type="button"
            aria-pressed={filter === key}
            onClick={() => onFilter(key)}
          >
            {FILTER_LABEL[key]} ({filterItems(items, key).length})
          </button>
        ))}
      </div>
      {visible.length === 0 ? (
        <p className="empty-note">
          {items.length === 0
            ? "No claims imported yet. Import a draft to start reviewing."
            : `Nothing in ${FILTER_LABEL[filter].toLowerCase()}.`}
        </p>
      ) : (
        <ul className="queue-list">
          {visible.map((item) => (
            <li key={item.item_id}>
              <button
                type="button"
                className="queue-row"
                aria-current={item.item_id === selectedId}
                onClick={() => onSelect(item.item_id)}
              >
                <span className="queue-row-text">{item.current_text}</span>
                <span className="queue-row-meta">
                  <StatePill state={item.state} />
                  <EvidenceMark status={item.evidence_status} />
                  {item.current_version > 1 ? <span>edited</span> : null}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </nav>
  );
}
