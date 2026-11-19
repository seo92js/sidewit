// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError, request } from "@/api/client";
import type { Tool } from "@/api/types";
import { ErrorAlert } from "@/components/app/common";
import ToolsPage from "@/pages/ToolsPage";

vi.mock("@/api/client", async (original) => ({
  ...(await original<typeof import("@/api/client")>()),
  request: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.mocked(request).mockReset();
});

const conflict = () =>
  new ApiError(409, { error: "version_conflict", message: "Changed", details: {} });

test.each([
  ["Context", "This Context has a newer saved version"],
  ["Tool", "This Tool has a newer saved version"],
  [undefined, "The server has a newer saved version"],
] as const)("version conflict hint names the resource (%s)", (resource, hint) => {
  render(<ErrorAlert error={conflict()} resource={resource} />);
  expect(screen.getByText(new RegExp(hint))).toBeTruthy();
});

test("a Tool availability conflict is not described as a Context conflict", async () => {
  const tool: Tool = {
    id: "t",
    name: "report",
    context_id: "c",
    description: "",
    input_schema: { type: "object", properties: {}, required: [], additionalProperties: false },
    operation: { type: "sql", statement: "SELECT 1" },
    version: 1,
    context_version: 1,
    status: "valid",
    enabled: true,
    validation_errors: [],
    validation: { valid: true, errors: [], warnings: [], checks: [] },
    source_generation_id: null,
    created_at: "",
    updated_at: "",
  };
  vi.mocked(request).mockImplementation(async (method) => {
    if (method === "PATCH") throw conflict();
    return { items: [tool] };
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(["tools"], [tool]);
  qc.setQueryData(["contexts"], []);
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ToolsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Disable report" }));
  expect(await screen.findByText(/This Tool has a newer saved version/)).toBeTruthy();
  expect(screen.queryByText(/saved this context/i)).toBeNull();
  qc.clear();
});
