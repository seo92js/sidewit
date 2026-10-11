// @vitest-environment jsdom
import { afterEach, expect, test, vi } from "vitest";
import { request } from "@/api/client";

afterEach(() => vi.unstubAllGlobals());

test("a proxy HTML error preserves HTTP status without exposing its body", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response("<h1>private upstream detail</h1>", { status: 504 })),
  );
  await expect(request("POST", "/generate", {})).rejects.toMatchObject({
    status: 504,
    error: "invalid_response",
    message: "The server returned a non-JSON response (HTTP 504).",
  });
});

test("a null error envelope falls back to a structured HTTP error", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("null", { status: 502 })));
  await expect(request("GET", "/tools")).rejects.toMatchObject({
    status: 502,
    error: "http_error",
  });
});

test("client deadline produces a distinct error", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new DOMException("deadline", "TimeoutError")));
  await expect(request("POST", "/generate", {})).rejects.toMatchObject({
    error: "request_timeout",
  });
});

test("a deadline while reading the body is handled like a fetch deadline", async () => {
  const response = new Response("", { status: 200 });
  vi.spyOn(response, "text").mockRejectedValue(new DOMException("deadline", "TimeoutError"));
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));
  await expect(request("POST", "/generate", {})).rejects.toMatchObject({
    error: "request_timeout",
  });
});

test.each(["9007199254740993", "1.0000000000000001", "1e309"])(
  "inexact API numbers produce an error instead of a copyable operation (%s)",
  async (number) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(`{"operation":{"parameters":{"id":${number}}},"status":"ok"}`, {
          status: 200,
        }),
      ),
    );
    await expect(request("POST", "/generate", {})).rejects.toMatchObject({
      error: "numeric_precision_loss",
      status: 200,
    });
  },
);
