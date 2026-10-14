// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, MemoryRouter, RouterProvider } from "react-router";
import { afterEach, expect, test, vi } from "vitest";
import { request } from "@/api/client";
import type { GenerateOk } from "@/api/types";
import { OperationResultView } from "@/pages/OperationResultView";
import ToolEditorPage from "@/pages/ToolEditorPage";

vi.mock("@/api/client", async (original) => ({
  ...(await original<typeof import("@/api/client")>()),
  request: vi.fn(),
}));
vi.mock("@/components/app/LazySqlEditor", () => ({ LazySqlEditor: () => null }));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const validation = { valid: true, errors: [], warnings: [], checks: [] };
const operation = {
  type: "sql" as const,
  dialect: "postgresql" as const,
  statement: "SELECT 1",
  parameters: {},
  parameter_types: {},
};
const plan: GenerateOk = {
  status: "ok",
  source: "plan",
  execution: "independent",
  generation_id: "gen",
  context_version: 1,
  operations: [
    {
      id: "step_1",
      source: "tool",
      selected_tool: { id: "existing-id", name: "existing", version: 1 },
      operation,
      validation,
    },
    { id: "step_2", source: "generated_sql", task: "List customer IDs", operation, validation },
    {
      id: "step_3",
      source: "metric",
      task: "Count orders",
      operation,
      validation,
      metric_binding: { metric_id: "count_orders", time_field: null },
    },
  ],
};

test("mixed plan labels provenance and offers drafts only for new operations", () => {
  render(
    <MemoryRouter>
      <OperationResultView result={plan} inputsChanged={false} />
    </MemoryRouter>,
  );
  expect(screen.getByText("Operation plan · 3 operations")).toBeTruthy();
  expect(screen.getByText("New SQL draft · review before use")).toBeTruthy();
  expect(screen.getByRole("link", { name: "existing" }).getAttribute("href")).toBe(
    "/tools/existing-id",
  );
  expect(screen.queryByRole("link", { name: "Create Tool draft from step_1" })).toBeNull();
  expect(
    screen.getByRole("link", { name: "Create Tool draft from step_2" }).getAttribute("href"),
  ).toBe("/tools/new?generation=gen&step=step_2");
  expect(
    screen.getByRole("link", { name: "Create Tool draft from step_3" }).getAttribute("href"),
  ).toContain("step=step_3");
});

test("draft loading and saving preserve step identity within the same generation", async () => {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  qc.setQueryData(
    ["contexts"],
    [
      {
        id: "context",
        name: "demo",
        version: 1,
        timezone: "UTC",
        dialect: "postgresql",
        definition: { resources: [], relations: [], rules: [] },
      },
    ],
  );
  vi.mocked(request).mockImplementation(async (_method, path, raw) => {
    if (path === "/tools/draft") {
      const body = raw as { generation_id: string; step_id: string };
      return {
        context_id: "context",
        name: `draft_${body.step_id}`,
        description: `Draft ${body.step_id}`,
        input_schema: { type: "object", properties: {}, required: [], additionalProperties: false },
        operation: { type: "sql", statement: "SELECT 1" },
        example_arguments: {},
        source_generation_id: body.generation_id,
        source_step_id: body.step_id,
      };
    }
    if (path === "/validate") return validation;
    if (path === "/tools") return { id: "tool", name: "saved" };
    throw new Error(`Unexpected request ${path}`);
  });
  const router = createMemoryRouter(
    [
      { path: "/tools/new", element: <ToolEditorPage /> },
      { path: "/tools/tool", element: <div>Saved</div> },
    ],
    { initialEntries: ["/tools/new?generation=gen&step=step_2"] },
  );
  render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  await screen.findByDisplayValue("Draft step_2");
  await act(async () => {
    await router.navigate("/tools/new?generation=gen&step=step_3");
  });
  fireEvent.click(await screen.findByRole("button", { name: "Discard and leave" }));
  await screen.findByDisplayValue("Draft step_3");
  expect(screen.queryByDisplayValue("Draft step_2")).toBeNull();
  const save = screen.getByRole("button", { name: "Save tool" });
  await waitFor(() => expect(save).toHaveProperty("disabled", false));
  fireEvent.click(save);
  await screen.findByText("Saved");
  expect(request).toHaveBeenCalledWith("POST", "/tools/draft", {
    generation_id: "gen",
    step_id: "step_3",
  });
  expect(request).toHaveBeenCalledWith(
    "POST",
    "/tools",
    expect.objectContaining({ source_generation_id: "gen", source_step_id: "step_3" }),
  );
  qc.clear();
});
