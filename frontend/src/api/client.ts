import type { ApiErrorBody, Issue } from "./types";
import { JsonPrecisionError, parseExactJson } from "../lib/json";

const BASE = "/api/v1";
const API_KEY_STORAGE = "sidewit.apiKey";

export function getApiKey(): string {
  return localStorage.getItem(API_KEY_STORAGE) ?? "";
}

export function setApiKey(key: string): void {
  if (key) localStorage.setItem(API_KEY_STORAGE, key);
  else localStorage.removeItem(API_KEY_STORAGE);
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
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const key = getApiKey();
  if (key) headers.Authorization = `Bearer ${key}`;

  let res: Response;
  let text: string;
  try {
    res = await fetch(BASE + path, {
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
