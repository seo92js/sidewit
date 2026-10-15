import type { ApiErrorBody, Issue } from "./types";
import { JsonPrecisionError, parseExactJson } from "../lib/json";

const BASE = "/api/web/v1";
let sessionRevision = 0;
export const SESSION_EXPIRED_EVENT = "sidewit:session-expired";

export interface AdminSession {
  username: string;
  api_key_required: boolean;
}

export class ApiError extends Error {
  readonly status: number;
  readonly error: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.status = status;
    this.error = body.error;
    this.details = body.details ?? {};
  }

  /** Issue lists the API puts in `details` under various keys. */
  issues(key: "validation_errors" | "errors" = "validation_errors"): Issue[] {
    const value = this.details[key];
    return Array.isArray(value) ? (value as Issue[]) : [];
  }
}

export async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const revision = sessionRevision;
  try {
    return await send<T>(method, BASE + path, body);
  } catch (error) {
    // An old request's delayed 401 must not hide an editor after a new successful login.
    if (error instanceof ApiError && error.status === 401 && revision === sessionRevision) {
      window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
    }
    throw error;
  }
}

export function adminSession(
  method: "GET" | "POST",
  body?: { username: string; password: string },
): Promise<AdminSession>;
export function adminSession(method: "DELETE"): Promise<void>;
export async function adminSession(
  method: "GET" | "POST" | "DELETE",
  body?: { username: string; password: string },
): Promise<AdminSession | void> {
  const result = await send<AdminSession | void>(method, "/api/web/session", body);
  if (method !== "GET") sessionRevision++;
  return result;
}

async function send<T>(method: string, url: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "X-Sidewit-Web": "1" };
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let res: Response;
  let text: string;
  try {
    res = await fetch(url, {
      credentials: "same-origin",
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(110_000),
    });
    text = res.status === 204 ? "" : await res.text();
  } catch (error) {
    if (error instanceof DOMException && error.name === "TimeoutError") {
      throw new ApiError(0, {
        error: "request_timeout",
        message: "The request exceeded the client deadline.",
        details: {},
      });
    }
    throw new ApiError(0, {
      error: "network_error",
      message: "Could not reach the Sidewit API.",
      details: {},
    });
  }

  if (res.status === 204) return undefined as T;
  let data: unknown;
  try {
    data = text ? parseExactJson(text) : undefined;
  } catch (error) {
    if (error instanceof JsonPrecisionError) {
      throw new ApiError(res.status, {
        error: "numeric_precision_loss",
        message: error.message,
        details: {},
      });
    }
    throw new ApiError(res.status, {
      error: "invalid_response",
      message: `The server returned a non-JSON response (HTTP ${res.status}).`,
      details: {},
    });
  }
  if (!res.ok) {
    const envelope =
      typeof data === "object" && data !== null ? (data as Partial<ApiErrorBody>) : undefined;
    throw new ApiError(res.status, {
      error: envelope?.error ?? "http_error",
      message: envelope?.message ?? `Request failed with status ${res.status}.`,
      details: envelope?.details ?? {},
    });
  }
  return data as T;
}
