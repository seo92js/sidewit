// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError, request } from "@/api/client";
import type { Context, Tool, ValidationResult } from "@/api/types";
import ToolEditorPage from "@/pages/ToolEditorPage";

vi.mock("@/api/client", async (original) => ({
  ...(await original<typeof import("@/api/client")>()),
  request: vi.fn(),
}));
vi.mock("@/components/app/LazySqlEditor", () => ({ LazySqlEditor: () => null }));
afterEach(cleanup);

const context: Context = {
  id: "context-id",
  name: "commerce",
  dialect: "postgresql",
  timezone: "UTC",
  allow_generated_sql: true,
  definition: { resources: [], relations: [], rules: [] },
  version: 1,
  created_at: "",
  updated_at: "",
};

test("server refetch preserves edits and the editor's original version", async () => {
  const validation: ValidationResult = {
    valid: true,
    errors: [],
    warnings: [],
    checks: [],
  };
  const tool: Tool = {
    id: "tool-id",
    context_id: "context-id",
    name: "report",
    description: "original",
    input_schema: { type: "object", properties: {}, required: [], additionalProperties: false },
    operation: { type: "sql", statement: "SELECT 1" },
    version: 1,
    context_version: 1,
    status: "valid",
    enabled: true,
    validation_errors: [],
    validation,
    source_generation_id: null,
    created_at: "",
    updated_at: "",
  };
  vi.mocked(request).mockImplementation(async (method) => {
    if (method === "PUT")
      throw new ApiError(409, {
        error: "version_conflict",
        message: "Changed on server",
        details: {},
      });
    return validation;
  });
  const client = new QueryClient({
    defaultOptions: { queries: { staleTime: Infinity, retry: false } },
  });
  client.setQueryData(["contexts"], [context]);
  client.setQueryData(["tools", tool.id], tool);
  client.setQueryData(["tools"], [tool]);
  render(
    <QueryClientProvider client={client}>
      <RouterProvider
        router={createMemoryRouter([{ path: "/tools/:id", element: <ToolEditorPage /> }], {
          initialEntries: ["/tools/tool-id"],
        })}
      />
    </QueryClientProvider>,
  );
  fireEvent.change(screen.getByLabelText("Description"), { target: { value: "my unsaved edits" } });
  await act(async () => {
    client.setQueryData(["tools", tool.id], { ...tool, version: 2, description: "remote edit" });
  });
  expect(screen.getByLabelText("Description")).toHaveProperty("value", "my unsaved edits");
  expect(await screen.findByText("Tool changed on the server")).toBeTruthy();
  const save = screen.getByRole("button", { name: "Save as v3" });
  expect(save).toHaveProperty("disabled", true);
  vi.mocked(request).mockResolvedValueOnce({ ...tool, version: 2, description: "remote edit" });
  fireEvent.click(screen.getByRole("button", { name: "Reload latest (discards your edits)" }));
  await waitFor(() =>
    expect(screen.getByLabelText("Description")).toHaveProperty("value", "remote edit"),
  );
  client.clear();
});

test.each(["valid", "invalid", "error"] as const)(
  "Context changes require a new validation before saving (%s)",
  async (outcome) => {
    const valid: ValidationResult = { valid: true, errors: [], warnings: [], checks: [] };
    const invalid: ValidationResult = {
      valid: false,
      errors: [{ code: "unknown_field", message: "Field was removed", ref: "id" }],
      warnings: [],
      checks: [],
    };
    const tool: Tool = {
      id: "tool-id",
      context_id: context.id,
      name: "report",
      description: "original",
      input_schema: { type: "object", properties: {}, required: [], additionalProperties: false },
      operation: { type: "sql", statement: "SELECT 1" },
      version: 1,
      context_version: 1,
      status: "valid",
      enabled: true,
      validation_errors: [],
      validation: valid,
      source_generation_id: null,
      created_at: "",
      updated_at: "",
    };
    let resolve!: (result: ValidationResult) => void;
    let reject!: (reason: Error) => void;
    const pending = new Promise<ValidationResult>((done, fail) => {
      resolve = done;
      reject = fail;
    });
    vi.mocked(request).mockReset();
    vi.mocked(request).mockResolvedValueOnce(valid).mockReturnValueOnce(pending);
    const client = new QueryClient({
      defaultOptions: { queries: { staleTime: Infinity, retry: false } },
    });
    client.setQueryData(["contexts"], [context]);
    client.setQueryData(["tools", tool.id], tool);
    client.setQueryData(["tools"], [tool]);
    render(
      <QueryClientProvider client={client}>
        <RouterProvider
          router={createMemoryRouter([{ path: "/tools/:id", element: <ToolEditorPage /> }], {
            initialEntries: ["/tools/tool-id"],
          })}
        />
      </QueryClientProvider>,
    );
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "my edits" } });
    const save = screen.getByRole("button", { name: "Save as v2" });
    await waitFor(() => expect(save).toHaveProperty("disabled", false));
    await act(async () => {
      client.setQueryData(["contexts"], [{ ...context, version: 2 }]);
    });
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
    expect(save).toHaveProperty("disabled", true);
    expect(screen.queryByText(/Tool validated/)).toBeNull();
    await act(async () => {
      if (outcome === "error") reject(new Error("Validation unavailable"));
      else resolve(outcome === "valid" ? valid : invalid);
    });
    if (outcome === "error") {
      expect(await screen.findByText("Validation request failed")).toBeTruthy();
    } else {
      expect(
        await screen.findByText(outcome === "valid" ? /Tool validated/ : /Tool validation failed/),
      ).toBeTruthy();
    }
    await waitFor(() => expect(save).toHaveProperty("disabled", outcome !== "valid"));
    expect(screen.getByLabelText("Description")).toHaveProperty("value", "my edits");
    client.clear();
  },
);
