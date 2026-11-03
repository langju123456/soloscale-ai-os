import type {
  ApiErrorBody,
  DraftListing,
  EvidenceStatus,
  ImportResponse,
  ReviewItemDetail,
  ReviewItemSummary,
  ReviewState,
  VersionDiff,
} from "./types";

export const DEFAULT_API_BASE_URL = "http://127.0.0.1:8767";

/** A rejection reported by the review API (or a transport failure, code NETWORK_ERROR). */
export class ReviewApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message ?? body.code);
    this.name = "ReviewApiError";
    this.status = status;
    this.code = body.code;
    const { code: _code, message: _message, ...details } = body;
    this.details = details;
  }
}

type Fetch = (input: string, init?: RequestInit) => Promise<Response>;

export interface ActionInput {
  expectedRevision: number;
  note: string;
  reviewer?: string;
}

export class ReviewApi {
  readonly baseUrl: string;
  private readonly fetchImpl: Fetch;

  constructor(baseUrl: string = DEFAULT_API_BASE_URL, fetchImpl?: Fetch) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.fetchImpl = fetchImpl ?? ((input, init) => fetch(input, init));
  }

  health(): Promise<{ status: string; model_calls: number }> {
    return this.request("GET", "/health");
  }

  listDrafts(): Promise<DraftListing> {
    return this.request("GET", "/review/drafts");
  }

  importDraft(runId: string): Promise<ImportResponse> {
    return this.request("POST", "/review/imports", { run_id: runId });
  }

  async listItems(filter: {
    state?: ReviewState;
    evidenceStatus?: EvidenceStatus;
  } = {}): Promise<ReviewItemSummary[]> {
    const params = new URLSearchParams();
    if (filter.state) params.set("state", filter.state);
    if (filter.evidenceStatus) params.set("evidence_status", filter.evidenceStatus);
    const query = params.toString();
    const body = await this.request<{ items: ReviewItemSummary[] }>(
      "GET",
      `/review/items${query ? `?${query}` : ""}`,
    );
    return body.items;
  }

  getItem(itemId: string): Promise<ReviewItemDetail> {
    return this.request("GET", `/review/items/${encodeURIComponent(itemId)}`);
  }

  getDiff(itemId: string, fromVersion?: number, toVersion?: number): Promise<VersionDiff> {
    const params = new URLSearchParams();
    if (fromVersion !== undefined) params.set("from", String(fromVersion));
    if (toVersion !== undefined) params.set("to", String(toVersion));
    const query = params.toString();
    return this.request(
      "GET",
      `/review/items/${encodeURIComponent(itemId)}/diff${query ? `?${query}` : ""}`,
    );
  }

  confirm(itemId: string, input: ActionInput): Promise<ReviewItemDetail> {
    return this.action(itemId, "confirm", input);
  }

  correct(itemId: string, input: ActionInput & { text: string }): Promise<ReviewItemDetail> {
    return this.action(itemId, "correct", input, { text: input.text });
  }

  reject(itemId: string, input: ActionInput): Promise<ReviewItemDetail> {
    return this.action(itemId, "reject", input);
  }

  reopen(itemId: string, input: ActionInput): Promise<ReviewItemDetail> {
    return this.action(itemId, "reopen", input);
  }

  private action(
    itemId: string,
    action: "confirm" | "correct" | "reject" | "reopen",
    input: ActionInput,
    extra: Record<string, unknown> = {},
  ): Promise<ReviewItemDetail> {
    const payload: Record<string, unknown> = {
      expected_revision: input.expectedRevision,
      note: input.note,
      ...extra,
    };
    if (input.reviewer) payload.reviewer = input.reviewer;
    return this.request("POST", `/review/items/${encodeURIComponent(itemId)}/${action}`, payload);
  }

  private async request<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method,
        headers: body === undefined ? undefined : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store",
      });
    } catch {
      throw new ReviewApiError(0, {
        code: "NETWORK_ERROR",
        message: `The review API at ${this.baseUrl} did not respond. Start it with python -m soloscale.review_api.`,
      });
    }
    const parsed: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      const error = (parsed as { error?: ApiErrorBody } | null)?.error;
      throw new ReviewApiError(
        response.status,
        error && typeof error.code === "string"
          ? error
          : { code: `HTTP_${response.status}`, message: response.statusText },
      );
    }
    return parsed as T;
  }
}
