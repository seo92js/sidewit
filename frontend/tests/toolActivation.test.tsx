// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, MemoryRouter, RouterProvider } from "react-router";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError, request } from "@/api/client";
import type { Context, Tool } from "@/api/types";
import ToolsPage from "@/pages/ToolsPage";
import ToolEditorPage from "@/pages/ToolEditorPage";
import { InvokePanel } from "@/pages/tool/InvokePanel";

vi.mock("@/api/client", async (original) => ({
  ...(await original<typeof import("@/api/client")>()),
  request: vi.fn(),
}));
vi.mock("@/components/app/LazySqlEditor", () => ({ LazySqlEditor: () => null }));
afterEach(() => {
  cleanup();
  vi.mocked(request).mockReset();
});
const validation = { valid: true, errors: [], warnings: [], checks: [] };
const context: Context = {
  id: "c",
  name: "commerce",
  dialect: "postgresql",
  timezone: "UTC",
  allow_generated_sql: true,
  definition: { resources: [], relations: [], rules: [] },
  version: 1,
  created_at: "",
  updated_at: "",
};
const tool: Tool = {
  id: "t",
  name: "report",
  context_id: "c",
  description: "Original",
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
function client() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  qc.setQueryData(["contexts"], [context]);
  qc.setQueryData(["tools"], [tool]);
  qc.setQueryData(["tools", tool.id], tool);
  return qc;
}
function list(qc: QueryClient) {
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ToolsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
function editor(qc: QueryClient) {
  const router = createMemoryRouter([{ path: "/tools/:id", element: <ToolEditorPage /> }], {
    initialEntries: ["/tools/t"],
  });
  render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

test("Tool list waits for activation to be saved and updates its version", async () => {
  let current = tool;
  let resolve!: (value: Tool) => void;
  vi.mocked(request).mockImplementation(async (method) =>
    method === "PATCH"
      ? await new Promise<Tool>((done) => {
          resolve = done;
        })
      : { items: [current] },
  );
  const qc = client();
  list(qc);
  const button = screen.getByRole("button", { name: "Disable report" });
  fireEvent.click(button);
  await waitFor(() => expect(button).toHaveProperty("disabled", true));
  expect(screen.getByText("Enabled")).toBeTruthy();
  expect(request).toHaveBeenCalledWith("PATCH", "/tools/t", {
    expected_version: 1,
    enabled: false,
  });
  current = { ...tool, enabled: false, version: 2 };
  await act(async () => resolve(current));
  expect(await screen.findByRole("button", { name: "Enable report" })).toBeTruthy();
  expect(screen.getByText("Disabled")).toBeTruthy();
  expect(qc.getQueryData(["tools", "t"])).toEqual(current);
  qc.clear();
});

test("activation conflict refreshes the row before another change", async () => {
  const current = { ...tool, enabled: false, version: 2 };
  vi.mocked(request).mockImplementation(async (method) => {
    if (method === "PATCH")
      throw new ApiError(409, {
        error: "version_conflict",
        message: "Changed on server",
        details: {},
      });
    return { items: [current] };
  });
  const qc = client();
  list(qc);
  fireEvent.click(screen.getByRole("button", { name: "Disable report" }));
  const enable = await screen.findByRole("button", { name: "Enable report" });
  await waitFor(() => expect(enable).toHaveProperty("disabled", false));
  expect(screen.getByText("Changed on server")).toBeTruthy();
  fireEvent.click(enable);
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith("PATCH", "/tools/t", {
      expected_version: 2,
      enabled: true,
    }),
  );
  qc.clear();
});

test.each([
  [503, "storage_unavailable"],
  [0, "network_error"],
  [0, "request_timeout"],
] as const)("activation %s/%s reads back committed state before retry", async (status, error) => {
  const current = { ...tool, enabled: false, version: 2 };
  let resolve!: (value: { items: Tool[] }) => void;
  let attempts = 0;
  vi.mocked(request).mockImplementation(async (method) => {
    if (method === "PATCH") {
      if (++attempts === 1)
        throw new ApiError(status, { error, message: "Response lost", details: {} });
      return { ...current, enabled: true, version: 3 };
    }
    return await new Promise<{ items: Tool[] }>((done) => {
      resolve = done;
    });
  });
  const qc = client();
  list(qc);
  fireEvent.click(screen.getByRole("button", { name: "Disable report" }));
  await waitFor(() => expect(request).toHaveBeenCalledWith("GET", "/tools"));
  expect(screen.getByRole("button", { name: "Disable report" })).toHaveProperty("disabled", true);
  expect(attempts).toBe(1);
  await act(async () => resolve({ items: [current] }));
  const enable = await screen.findByRole("button", { name: "Enable report" });
  await waitFor(() => expect(enable).toHaveProperty("disabled", false));
  expect(screen.getByText("Response lost")).toBeTruthy();
  expect(screen.getByText("Disabled")).toBeTruthy();
  fireEvent.click(enable);
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith("PATCH", "/tools/t", {
      expected_version: 2,
      enabled: true,
    }),
  );
  await waitFor(() =>
    expect(vi.mocked(request).mock.calls.filter(([method]) => method === "GET")).toHaveLength(2),
  );
  await act(async () => resolve({ items: [{ ...current, enabled: true, version: 3 }] }));
  expect(await screen.findByText("Enabled")).toBeTruthy();
  qc.clear();
});

test("failed activation refreshes the editor without discarding local edits", async () => {
  const current = { ...tool, enabled: false, version: 2, description: "Concurrent server edit" };
  vi.mocked(request).mockImplementation(async (method, path) => {
    if (method === "PATCH")
      throw new ApiError(503, {
        error: "storage_unavailable",
        message: "Storage failed",
        details: {},
      });
    if (method === "GET" && path === "/tools/t") return current;
    return validation;
  });
  const qc = client();
  editor(qc);
  fireEvent.click(screen.getByRole("button", { name: "Disable Tool" }));
  await screen.findByRole("button", { name: "Enable Tool" });
  await screen.findByText("Storage failed");
  expect(screen.getByRole("button", { name: "Invoke Tool" })).toHaveProperty("disabled", true);
  expect(screen.getByLabelText("Description")).toHaveProperty("value", tool.description);
  expect(screen.getByText("Tool changed on the server")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Save as v3" })).toHaveProperty("disabled", true);
  qc.clear();
});

test("disabled Tool cannot invoke from the UI", () => {
  const qc = client();
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <InvokePanel tool={{ ...tool, enabled: false }} dirty={false} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const invoke = screen.getByRole("button", { name: "Invoke Tool" });
  expect(invoke).toHaveProperty("disabled", true);
  fireEvent.click(invoke);
  expect(request).not.toHaveBeenCalled();
  expect(screen.getByText("Enable this Tool before invoking it.")).toBeTruthy();
  qc.clear();
});

test("unsaved editor changes prevent toggling availability", () => {
  vi.mocked(request).mockResolvedValue(validation);
  const qc = client();
  editor(qc);
  fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Unsaved change" } });
  const disable = screen.getByRole("button", { name: "Disable Tool" });
  expect(disable).toHaveProperty("disabled", true);
  fireEvent.click(disable);
  expect(vi.mocked(request).mock.calls.some(([method]) => method === "PATCH")).toBe(false);
  expect(screen.getByLabelText("Description")).toHaveProperty("value", "Unsaved change");
  qc.clear();
});

test("editor uses the activation version for the next SQL save", async () => {
  const disabled = { ...tool, enabled: false, version: 2 };
  vi.mocked(request).mockImplementation(async (method) => {
    if (method === "PATCH") return disabled;
    if (method === "PUT") return { ...disabled, description: "Changed", version: 3 };
    return validation;
  });
  const qc = client();
  editor(qc);
  fireEvent.click(screen.getByRole("button", { name: "Disable Tool" }));
  const enable = await screen.findByRole("button", { name: "Enable Tool" });
  await waitFor(() => expect(enable).toHaveProperty("disabled", false));
  expect(screen.queryByText("Tool changed on the server")).toBeNull();
  expect(screen.getByRole("button", { name: "Invoke Tool" })).toHaveProperty("disabled", true);
  fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Changed" } });
  const save = await screen.findByRole("button", { name: "Save as v3" });
  await waitFor(() => expect(save).toHaveProperty("disabled", false));
  fireEvent.click(save);
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "PUT",
      "/tools/t",
      expect.objectContaining({ expected_version: 2, description: "Changed" }),
    ),
  );
  qc.clear();
});
