// @vitest-environment jsdom
import { afterEach, expect, test, vi } from "vitest";
import { request, adminSession, SESSION_EXPIRED_EVENT } from "@/api/client";

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

test("management requests use same-origin cookies and the CSRF header, not a saved API key", async () => {
  localStorage.setItem("sidewit.apiKey", "old-private-api-key");
  const fetch = vi.fn().mockResolvedValue(new Response('{"items":[]}', { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  await request("GET", "/contexts");
  expect(fetch).toHaveBeenCalledWith(
    "/api/web/v1/contexts",
    expect.objectContaining({
      credentials: "same-origin",
      headers: { "X-Sidewit-Web": "1" },
    }),
  );
  expect(JSON.stringify(fetch.mock.calls)).not.toContain("old-private-api-key");
  localStorage.clear();
});

test("a management 401 requests reauthentication without putting cookies in JS storage", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response('{"error":"admin_session_required","message":"Sign in","details":{}}', {
        status: 401,
      }),
    ),
  );
  const expired = vi.fn();
  window.addEventListener(SESSION_EXPIRED_EVENT, expired);
  try {
    await expect(request("GET", "/contexts")).rejects.toMatchObject({ status: 401 });
    expect(expired).toHaveBeenCalledTimes(1);
  } finally {
    window.removeEventListener(SESSION_EXPIRED_EVENT, expired);
  }
});

test("a delayed 401 from before sign-in cannot expire the new session", async () => {
  let finishOldRequest!: (response: Response) => void;
  const oldResponse = new Promise<Response>((resolve) => {
    finishOldRequest = resolve;
  });
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockReturnValueOnce(oldResponse)
      .mockResolvedValueOnce(
        new Response('{"username":"admin","api_key_required":false}', { status: 200 }),
      ),
  );
  const expired = vi.fn();
  window.addEventListener(SESSION_EXPIRED_EVENT, expired);
  try {
    const oldRequest = request("GET", "/contexts");
    await adminSession("POST", { username: "admin", password: "admin" });
    finishOldRequest(
      new Response('{"error":"admin_session_required","message":"Sign in"}', {
        status: 401,
      }),
    );
    await expect(oldRequest).rejects.toMatchObject({ status: 401 });
    expect(expired).not.toHaveBeenCalled();
  } finally {
    window.removeEventListener(SESSION_EXPIRED_EVENT, expired);
  }
});

test("bad login credentials do not expire a separate existing editor session", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValue(
      new Response(
        '{"error":"invalid_admin_credentials","message":"Invalid credentials","details":{}}',
        { status: 401 },
      ),
    );
  vi.stubGlobal("fetch", fetch);
  const expired = vi.fn();
  window.addEventListener(SESSION_EXPIRED_EVENT, expired);
  try {
    await expect(
      adminSession("POST", { username: "admin", password: "wrong" }),
    ).rejects.toMatchObject({ status: 401 });
    expect(fetch).toHaveBeenCalledWith(
      "/api/web/session",
      expect.objectContaining({ method: "POST", credentials: "same-origin" }),
    );
    expect(expired).not.toHaveBeenCalled();
  } finally {
    window.removeEventListener(SESSION_EXPIRED_EVENT, expired);
  }
});
