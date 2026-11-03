import { describe, expect, it } from "vitest";

import { ReviewApi, ReviewApiError } from "../lib/api";
import { describeError } from "../lib/labels";

interface Call {
  url: string;
  init: RequestInit | undefined;
}

function fakeFetch(status: number, body: unknown, calls: Call[]) {
  return async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  };
}

describe("ReviewApi", () => {
  it("sends the loaded revision with every action", async () => {
    const calls: Call[] = [];
    const api = new ReviewApi("http://127.0.0.1:8767/", fakeFetch(200, { item: {} }, calls));

    await api.correct("evidence-x.c3", {
      expectedRevision: 4,
      note: "source says otherwise",
      text: "Corrected claim.",
      reviewer: "lang",
    });

    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe("http://127.0.0.1:8767/review/items/evidence-x.c3/correct");
    expect(calls[0]?.init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({
      expected_revision: 4,
      note: "source says otherwise",
      text: "Corrected claim.",
      reviewer: "lang",
    });
  });

  it("surfaces a stale-revision rejection with its server details", async () => {
    const calls: Call[] = [];
    const api = new ReviewApi(
      "http://127.0.0.1:8767",
      fakeFetch(
        409,
        {
          error: {
            code: "STALE_REVISION",
            message: "this item changed since it was loaded; reload before acting",
            item_id: "evidence-x.c0",
            current_revision: 3,
            current_state: "CORRECTED",
          },
        },
        calls,
      ),
    );

    const failure = await api
      .confirm("evidence-x.c0", { expectedRevision: 2, note: "" })
      .catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ReviewApiError);
    const error = failure as ReviewApiError;
    expect(error.status).toBe(409);
    expect(error.code).toBe("STALE_REVISION");
    expect(error.details.current_revision).toBe(3);
    const described = describeError(error);
    expect(described.title).toBe("Someone changed this item first");
    expect(described.detail).toContain("revision 3");
  });

  it("reports an unreachable API as a network error instead of throwing raw", async () => {
    const api = new ReviewApi("http://127.0.0.1:1", async () => {
      throw new TypeError("fetch failed");
    });

    const failure = await api.listItems().catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ReviewApiError);
    expect((failure as ReviewApiError).code).toBe("NETWORK_ERROR");
  });

  it("builds queue filters as query parameters", async () => {
    const calls: Call[] = [];
    const api = new ReviewApi("http://127.0.0.1:8767", fakeFetch(200, { items: [] }, calls));

    await api.listItems({ state: "NEEDS_REVIEW", evidenceStatus: "HASH_MISMATCH" });

    expect(calls[0]?.url).toBe(
      "http://127.0.0.1:8767/review/items?state=NEEDS_REVIEW&evidence_status=HASH_MISMATCH",
    );
  });
});
