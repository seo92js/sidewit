// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState, type ReactNode } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, expect, test, vi } from "vitest";
import type { SqlEditorProps } from "@/components/app/SqlEditor";
import { createLazySqlEditor } from "@/components/app/LazySqlEditor";
import { routes } from "@/routes";

// Rendering the real routes must not pull in Monaco or the session check.
vi.mock("@/components/app/AdminAccess", () => ({
  AdminAccess: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("@/App", () => ({
  default: () => {
    throw new Error("Render failed");
  },
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function FakeMonaco({ value }: SqlEditorProps) {
  return <output aria-label="Monaco">{value}</output>;
}

function Host({
  Editor,
  readOnly,
}: {
  Editor: ReturnType<typeof createLazySqlEditor>;
  readOnly?: boolean;
}) {
  const [sql, setSql] = useState("SELECT 1");
  return (
    <>
      <Editor value={sql} onChange={readOnly ? undefined : setSql} readOnly={readOnly} />
      <p>Saved draft: {sql}</p>
    </>
  );
}

test("a failed editor chunk falls back to plain text, keeps edits, and retries a fresh import", async () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  const load = vi
    .fn<() => Promise<{ default: typeof FakeMonaco }>>()
    .mockRejectedValueOnce(new Error("Failed to fetch dynamically imported module"))
    .mockResolvedValue({ default: FakeMonaco });
  const Editor = createLazySqlEditor(load);
  render(<Host Editor={Editor} />);

  const fallback = await screen.findByLabelText("SQL (plain text)");
  expect(fallback).toHaveProperty("value", "SELECT 1");
  fireEvent.change(fallback, { target: { value: "SELECT 2" } });
  expect(screen.getByText("Saved draft: SELECT 2")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Retry loading editor" }));
  expect((await screen.findByLabelText("Monaco")).textContent).toBe("SELECT 2");
  expect(load).toHaveBeenCalledTimes(2);
  expect(screen.queryByLabelText("SQL (plain text)")).toBeNull();
});

test("the plain-text fallback keeps read-only SQL read-only", async () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  const Editor = createLazySqlEditor(() => Promise.reject(new Error("chunk failed")));
  render(<Host Editor={Editor} readOnly />);
  expect(await screen.findByLabelText("SQL (plain text)")).toHaveProperty("readOnly", true);
  expect(screen.getByText(/Showing the SQL as plain text/)).toBeTruthy();
});

test("a render error outside any local boundary shows a recoverable app error", async () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
  const router = createMemoryRouter(routes, { initialEntries: ["/playground"] });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  expect(await screen.findByText("Sidewit stopped because of an unexpected error")).toBeTruthy();
  expect(screen.getByText("Render failed")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Reload Sidewit" })).toBeTruthy();
});
