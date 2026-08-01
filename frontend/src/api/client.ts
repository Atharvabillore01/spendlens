import { readToken, writeToken, type Identity } from "../auth/session";
import type { Health, QueryRequest, QueryResult, UsersResponse } from "../types";

/** Thrown for any non-2xx. Carries the status so callers can distinguish a
 *  404 (unknown user) from a 401 (session gone) from a transport failure. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** `hadToken` distinguishes the two very different 401s:
 *   - true  -> a token was presented and rejected. The session really expired.
 *   - false -> nobody was signed in. Entirely normal on a first visit, and
 *              telling that person their session expired is a lie. */
type Unauthorized = (hadToken: boolean) => void;
let onUnauthorized: Unauthorized = () => {};

/** The app registers a handler so a dead token drops you to the login screen
 *  once, centrally, instead of every caller checking for 401. */
export function setUnauthorizedHandler(handler: Unauthorized): void {
  onUnauthorized = handler;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = readToken();
  // FormData must set its own `multipart/form-data; boundary=…`. Declaring any
  // Content-Type here produces a boundary-less header the server cannot parse,
  // so the header is omitted entirely for uploads.
  const isUpload = init.body instanceof FormData;
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: {
        ...(isUpload ? {} : { "Content-Type": "application/json" }),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(init.headers ?? {}),
      },
    });
  } catch (cause) {
    // An aborted request is a deliberate user action, not a failure.
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError("Could not reach the server.", 0);
  }

  if (response.status === 401) {
    // Clear any token before handing control back, so the next render cannot
    // re-use one the server has already rejected.
    const hadToken = Boolean(token);
    if (hadToken) writeToken(null);
    onUnauthorized(hadToken);
    throw new ApiError(
      hadToken ? "Your session has expired. Please sign in again." : "Not signed in.",
      401,
    );
  }

  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    const inner = detail?.detail ?? detail;
    throw new ApiError(
      inner?.message ?? `${response.status} ${response.statusText}`,
      response.status,
      inner?.error,
    );
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/* ── wire types for the new surfaces ─────────────────────────────── */

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user_id: string;
  email: string;
  role: "user" | "manager" | "admin";
  scopes: string[];
}

export interface LoginHints {
  accounts: { email: string; role: "user" | "manager" | "admin"; user_id: string }[];
  password: string;
}

export interface PasteParse {
  rows_parsed: number;
  columns_detected: string[];
  header_used: boolean;
  column_mapping: Record<string, string>;
  unmapped_columns: string[];
  missing_required: string[];
  coercion_notes: string[];
  dropped_blank_rows: number;
  ok: boolean;
}

export interface IngestSummary {
  batch_id: string;
  filename: string;
  total_rows: number;
  inserted: number;
  skipped_duplicates: number;
  rejected_rows: number;
  users_seen: number;
  rejections: string[];
}

export interface PasteResponse {
  parse: PasteParse;
  preview_rows: Record<string, unknown>[];
  committed: boolean;
  ingest?: IngestSummary;
}

export interface ManagerRequest {
  request_id: string;
  from_user_id: string;
  from_user_name: string;
  question: string;
  status: "open" | "answered" | "closed";
  created_at: string | null;
  computed_answer: string | null;
  computed_summary?: QueryResult["data_summary"] | null;
  computed_at: string | null;
  reply: string | null;
  replied_by: string | null;
  replied_at: string | null;
}

export interface RequestsResponse {
  requests: ManagerRequest[];
  can_reply: boolean;
  counts: { open: number; answered: number; total: number };
}

export const api = {
  /* auth */
  login: (email: string, password: string) =>
    request<LoginResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  me: () => request<Identity>("/auth/me"),

  /** 404s when SHOW_LOGIN_HINTS is off — absence is the normal case. */
  hints: () => request<LoginHints>("/auth/hints"),

  /* data */
  users: () => request<UsersResponse>("/users"),
  health: () => request<Health>("/readyz"),

  query: (body: QueryRequest, signal?: AbortSignal) =>
    request<QueryResult>("/query", {
      method: "POST",
      body: JSON.stringify(body),
      ...(signal ? { signal } : {}),
    }),

  cacheSnapshot: (userId: string) =>
    request<Record<string, unknown>>(`/users/${encodeURIComponent(userId)}/cache`),

  invalidateCache: (userId: string) =>
    request<void>(`/users/${encodeURIComponent(userId)}/cache`, { method: "DELETE" }),

  /* ingest */
  paste: (body: {
    text: string;
    commit: boolean;
    has_header?: boolean | null;
    delimiter?: string | null;
    column_overrides?: Record<string, string>;
  }) => request<PasteResponse>("/ingest/paste", { method: "POST", body: JSON.stringify(body) }),

  uploadFile: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    // FormData sets its own multipart boundary; forcing a Content-Type here
    // would produce a boundary-less header the server can't parse.
    return request<IngestSummary>("/ingest", { method: "POST", body: form });
  },

  /* ask your manager */
  submitRequest: (question: string) =>
    request<ManagerRequest>("/requests", { method: "POST", body: JSON.stringify({ question }) }),

  listRequests: (status?: string) =>
    request<RequestsResponse>(`/requests${status ? `?status=${encodeURIComponent(status)}` : ""}`),

  runRequest: (requestId: string, theme: "light" | "dark") =>
    request<{ request: ManagerRequest; result: QueryResult }>(
      `/requests/${encodeURIComponent(requestId)}/run?theme=${theme}`,
      { method: "POST" },
    ),

  replyToRequest: (requestId: string, reply: string) =>
    request<ManagerRequest>(`/requests/${encodeURIComponent(requestId)}/reply`, {
      method: "POST",
      body: JSON.stringify({ reply }),
    }),
};
