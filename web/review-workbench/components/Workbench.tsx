"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { DEFAULT_API_BASE_URL, ReviewApi } from "../lib/api";
import { describeError } from "../lib/labels";
import type { ImportResponse, ReviewItemDetail, ReviewItemSummary } from "../lib/types";
import { DraftsPanel } from "./DraftsPanel";
import { HistoryPanel, ItemView } from "./ItemView";
import { Queue, type QueueFilter, filterItems } from "./Queue";

const REVIEWER_KEY = "soloscale-review-reviewer";

function readReviewer(): string {
  try {
    return window.localStorage.getItem(REVIEWER_KEY) || "local-operator";
  } catch {
    return "local-operator";
  }
}

export function Workbench() {
  const api = useMemo(
    () => new ReviewApi(process.env.NEXT_PUBLIC_REVIEW_API_BASE_URL || DEFAULT_API_BASE_URL),
    [],
  );
  const [apiState, setApiState] = useState<"checking" | "online" | "offline">("checking");
  const [items, setItems] = useState<ReviewItemSummary[]>([]);
  const [filter, setFilter] = useState<QueueFilter>("needs");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ReviewItemDetail | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [showDrafts, setShowDrafts] = useState(false);
  const [reviewer, setReviewer] = useState("local-operator");

  useEffect(() => setReviewer(readReviewer()), []);

  const refreshQueue = useCallback(async () => {
    try {
      const next = await api.listItems();
      setItems(next);
      setApiState("online");
      setLoadError(null);
      return next;
    } catch (caught) {
      setApiState("offline");
      setLoadError(caught);
      return null;
    }
  }, [api]);

  const loadDetail = useCallback(
    async (itemId: string) => {
      try {
        setDetail(await api.getItem(itemId));
        setLoadError(null);
      } catch (caught) {
        setLoadError(caught);
      }
    },
    [api],
  );

  useEffect(() => {
    void (async () => {
      const loaded = await refreshQueue();
      if (loaded === null) return;
      if (loaded.length === 0) {
        setShowDrafts(true);
        return;
      }
      const firstOpen = filterItems(loaded, "needs")[0] ?? loaded[0];
      if (firstOpen) {
        setFilter(firstOpen.state === "NEEDS_REVIEW" ? "needs" : "all");
        setSelectedId(firstOpen.item_id);
      }
    })();
  }, [refreshQueue]);

  useEffect(() => {
    if (selectedId) void loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  function onImported(response: ImportResponse) {
    void refreshQueue();
    const first = response.items[0];
    if (first) {
      setFilter("all");
      setSelectedId(first.item_id);
    }
  }

  function onChanged(next: ReviewItemDetail) {
    setDetail(next);
    void refreshQueue();
  }

  function updateReviewer(value: string) {
    setReviewer(value);
    try {
      window.localStorage.setItem(REVIEWER_KEY, value);
    } catch {
      // Per-browser convenience only; the server records whatever name is sent.
    }
  }

  const failure = loadError ? describeError(loadError) : null;

  return (
    <div className="frame">
      <header className="topbar">
        <div className="topbar-title">
          <h1>Review workbench</h1>
          <p>Human confirmation for Evidence Agent claims</p>
        </div>
        <span className="api-status" data-state={apiState}>
          {apiState === "online"
            ? `API ${api.baseUrl.replace(/^https?:\/\//, "")}`
            : apiState === "offline"
              ? "API unreachable"
              : "Connecting"}
        </span>
        <label className="reviewer-field">
          Reviewer
          <input
            value={reviewer}
            maxLength={80}
            onChange={(event) => updateReviewer(event.target.value)}
          />
        </label>
        <button
          type="button"
          className="button"
          aria-expanded={showDrafts}
          onClick={() => setShowDrafts((open) => !open)}
        >
          {showDrafts ? "Hide drafts" : "Import drafts"}
        </button>
      </header>

      <div className="workspace">
        {showDrafts ? <DraftsPanel api={api} onImported={onImported} /> : null}
        <Queue
          items={items}
          filter={filter}
          selectedId={selectedId}
          onFilter={setFilter}
          onSelect={setSelectedId}
        />
        <main className="item-column">
          {failure ? (
            <div className="server-error" role="alert">
              <strong>{failure.title}</strong>
              <span>{failure.detail}</span>
            </div>
          ) : null}
          {detail ? (
            <ItemView
              api={api}
              detail={detail}
              reviewer={reviewer.trim() || "local-operator"}
              onChanged={onChanged}
              onReload={async () => {
                await loadDetail(detail.item.item_id);
                await refreshQueue();
              }}
            />
          ) : (
            <div className="welcome">
              <h2>Check each model claim against the passage it cites.</h2>
              <p>
                Claims whose sources changed, went missing, or could not be checked are listed
                first. Confirming, correcting, or rejecting is recorded with your name and note,
                and the model&apos;s original wording is always kept.
              </p>
              <p>
                Start the API with <code>python -m soloscale.review_api</code>, then import a
                draft.
              </p>
            </div>
          )}
        </main>
        <aside className="history-column" aria-label="Item history">
          {detail ? <HistoryPanel detail={detail} /> : null}
        </aside>
      </div>
    </div>
  );
}
