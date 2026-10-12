// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError, request } from "@/api/client";
import PlaygroundPage from "@/pages/PlaygroundPage";

vi.mock("@/api/client", async (original) => ({
  ...(await original<typeof import("@/api/client")>()),
  request: vi.fn(),
}));
// A failed editor chunk must not prevent reading rejected SQL or erase the form.
vi.mock("@/components/app/LazySqlEditor", () => ({
  LazySqlEditor: () => {
    throw new Error("Failed to fetch dynamically imported module");
  },
}));
afterEach(() => {
  cleanup();
  vi.mocked(request).mockReset();
  localStorage.clear();
});

async function openRejected(log: unknown, missingId = false) {
  vi.mocked(request).mockImplementation(async (_method, path) => {
    if (path === "/generate")
      throw new ApiError(422, {
        error: "generation_invalid",
        message: "SQL failed validation",
        details: { ...(missingId ? {} : { generation_id: "g1" }), validation_errors: [] },
      });
    if (path === "/generations/g1") {
      if (log instanceof Error) throw log;
      return log;
    }
    throw new Error(`Unexpected request ${path}`);
  });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  client.setQueryData(
    ["contexts"],
    [
      {
        id: "c",
        name: "test",
        version: 1,
        dialect: "postgresql",
        timezone: "UTC",
        allow_generated_sql: true,
        definition: { resources: [], relations: [], rules: [] },
      },
    ],
  );
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <PlaygroundPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "keep my question" } });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Generate operation" }));
  const toggle = await screen.findByRole("button", { name: /Show rejected SQL/ });
  await user.click(toggle);
  return { client, user, toggle };
}

test("rejected SQL remains readable when the editor cannot load, and can be reopened", async () => {
  const sql = "SELECT missing_column FROM orders; -- <script>alert(1)</script>";
  const { client, user, toggle } = await openRejected({
    context_version: 1,
    operation: { statement: sql },
  });
  expect(await screen.findByText(sql)).toBeTruthy();
  expect(screen.getByLabelText("Question")).toHaveProperty("value", "keep my question");
  expect(document.querySelector("script")).toBeNull();
  await user.click(toggle);
  expect(screen.queryByText(sql)).toBeNull();
  await user.click(toggle);
  expect(await screen.findByText(sql)).toBeTruthy();
  client.clear();
});

test.each([null, { statement: null }, { statement: {} }, { statement: "" }])(
  "missing or malformed SQL is explained without crashing: %j",
  async (operation) => {
    const { client } = await openRejected({ context_version: 1, operation });
    expect(
      await screen.findByText("No rejected SQL was recorded for this generation."),
    ).toBeTruthy();
    expect(screen.getByLabelText("Question")).toHaveProperty("value", "keep my question");
    client.clear();
  },
);

test("expired generation history stays an inline error", async () => {
  const { client } = await openRejected(
    new ApiError(404, { error: "not_found", message: "Generation history expired", details: {} }),
  );
  expect(await screen.findByText("Generation history expired")).toBeTruthy();
  expect(screen.getByLabelText("Question")).toHaveProperty("value", "keep my question");
  client.clear();
});

test("missing generation ID does not issue a history request", async () => {
  const { client } = await openRejected(null, true);
  expect(await screen.findByText("No generation log is available for this error.")).toBeTruthy();
  expect(vi.mocked(request).mock.calls).toHaveLength(1);
  client.clear();
});
