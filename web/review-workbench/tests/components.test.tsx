import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DiffView } from "../components/DiffView";
import { EvidenceExhibit } from "../components/EvidenceExhibit";
import { Queue, filterItems } from "../components/Queue";
import type { ReviewCitation, ReviewItemSummary } from "../lib/types";

function citation(overrides: Partial<ReviewCitation>): ReviewCitation {
  return {
    ordinal: 0,
    chunk_id: "sample-resume-cloud-c0",
    document_id: "sample-doc-resume-cloud",
    source_kind: "buildlog_run",
    external_id: "sample-resume-cloud-lease",
    title: "Sample: Resume Cloud lease recovery",
    role: "assistant",
    source_timestamp: null,
    recorded_chunk_sha256: "a".repeat(64),
    recorded_document_sha256: "b".repeat(64),
    model_visible_excerpt: "A lease-expired RUNNING task becomes NEEDS_REVIEW.",
    check: "VERIFIED",
    current_chunk_sha256: "a".repeat(64),
    current_document_sha256: "b".repeat(64),
    document_changed: false,
    checked_at: "2026-11-03T09:00:00+00:00",
    ...overrides,
  };
}

function item(state: ReviewItemSummary["state"], id: string): ReviewItemSummary {
  return {
    item_id: id,
    run_id: "evidence-x",
    claim_index: 0,
    state,
    evidence_status: state === "NEEDS_REVIEW" ? "HASH_MISMATCH" : "FULLY_VERIFIED",
    revision: 1,
    current_version: 1,
    current_text: `Claim ${id}`,
    citation_count: 1,
    verified_citation_count: state === "NEEDS_REVIEW" ? 0 : 1,
    confirm_requirement: state === "NEEDS_REVIEW" ? "BLOCKED" : "NONE",
    allowed_actions: ["correct", "reject"],
    updated_at: "2026-11-03T09:00:00+00:00",
  };
}

describe("DiffView", () => {
  it("marks deletions and insertions for sighted and screen-reader users", () => {
    const html = renderToStaticMarkup(
      <DiffView
        ops={[
          { op: "equal", text: "The task is " },
          { op: "delete", text: "never requeued" },
          { op: "insert", text: "requeued only by an operator" },
          { op: "equal", text: "." },
        ]}
      />,
    );
    expect(html).toContain("<del>");
    expect(html).toContain("never requeued");
    expect(html).toContain("<ins>");
    expect(html).toContain("[removed: ");
    expect(html).toContain("[added: ");
  });
});

describe("EvidenceExhibit", () => {
  it("shows the model-visible excerpt and a matching fingerprint", () => {
    const html = renderToStaticMarkup(<EvidenceExhibit citation={citation({})} />);
    expect(html).toContain("A lease-expired RUNNING task becomes NEEDS_REVIEW.");
    expect(html).toContain('data-tone="ok"');
    expect(html).toContain("Matches the index");
    expect(html).not.toContain('data-diff="true"');
  });

  it("flags a passage whose hash changed after drafting", () => {
    const html = renderToStaticMarkup(
      <EvidenceExhibit
        citation={citation({ check: "HASH_MISMATCH", current_chunk_sha256: "c".repeat(64) })}
      />,
    );
    expect(html).toContain('data-tone="bad"');
    expect(html).toContain('data-diff="true"');
    expect(html).toContain("Passage changed since drafting");
    expect(html).toContain("aaaaaaaaaaaa");
    expect(html).toContain("cccccccccccc");
  });
});

describe("EvidenceExhibit document revision", () => {
  it("marks a revised document as a warning when the cited passage is unchanged", () => {
    const html = renderToStaticMarkup(
      <EvidenceExhibit
        citation={citation({
          check: "DOCUMENT_REVISED",
          current_document_sha256: "d".repeat(64),
          document_changed: true,
        })}
      />,
    );
    expect(html).toContain('data-tone="warn"');
    expect(html).toContain('data-diff="revised"');
    expect(html).not.toContain('data-diff="true"');
    expect(html).toContain("Passage unchanged, document revised");
  });
});

describe("Queue", () => {
  it("keeps the server's low-confidence-first order and filters by state", () => {
    const items = [item("NEEDS_REVIEW", "a"), item("PENDING_REVIEW", "b"), item("CONFIRMED", "c")];
    expect(filterItems(items, "needs").map((value) => value.item_id)).toEqual(["a"]);
    expect(filterItems(items, "decided").map((value) => value.item_id)).toEqual(["c"]);
    expect(filterItems(items, "all").map((value) => value.item_id)).toEqual(["a", "b", "c"]);

    const html = renderToStaticMarkup(
      <Queue items={items} filter="all" selectedId="b" onFilter={() => {}} onSelect={() => {}} />,
    );
    expect(html.indexOf("Claim a")).toBeLessThan(html.indexOf("Claim b"));
    expect(html).toContain("Needs review (1)");
    expect(html).toContain("Source changed");
    expect(html).toContain('aria-current="true"');
  });
});
