// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { HealthDot } from "@/components/app/HealthDot";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const json = (body: unknown, status: number) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function renderDot() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <HealthDot />
    </QueryClientProvider>,
  );
  return qc;
}

test("a 503 health report shows that storage is unavailable", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(json({ status: "unavailable", storage: "unavailable" }, 503)),
  );
  renderDot();
  expect(await screen.findByTitle("API unavailable · storage unavailable")).toBeTruthy();
});

test("an API that stops answering is no longer shown as healthy", async () => {
  const fetch = vi.fn().mockResolvedValue(json({ status: "ok", storage: "ok" }, 200));
  vi.stubGlobal("fetch", fetch);
  const qc = renderDot();
  expect(await screen.findByTitle("API ok · storage ok")).toBeTruthy();

  fetch.mockRejectedValue(new TypeError("Failed to fetch"));
  await act(() => qc.refetchQueries({ queryKey: ["health"] }));
  expect(await screen.findByTitle("API unreachable")).toBeTruthy();

  fetch.mockResolvedValue(json({ status: "ok", storage: "ok" }, 200));
  await act(() => qc.refetchQueries({ queryKey: ["health"] }));
  expect(await screen.findByTitle("API ok · storage ok")).toBeTruthy();
});

test("a 503 without a health report (e.g. from a proxy) means the API is unreachable", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response("<h1>Service Unavailable</h1>", { status: 503 })),
  );
  renderDot();
  expect(await screen.findByTitle("API unreachable")).toBeTruthy();
});
