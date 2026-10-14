// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError, request } from "@/api/client";
import type { Context, Tool, ToolUpdate } from "@/api/types";
import ToolEditorPage from "@/pages/ToolEditorPage";

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
function editor() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  qc.setQueryData(["contexts"], [context]);
  qc.setQueryData(["tools"], [tool]);
  qc.setQueryData(["tools", tool.id], tool);
  const router = createMemoryRouter(
    [
      { path: "/tools/new", element: <ToolEditorPage /> },
      { path: "/tools/:id", element: <ToolEditorPage /> },
    ],
    {
      initialEntries: ["/tools/t"],
    },
  );
  render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { qc, router };
}
async function save() {
  const button = screen.getByRole("button", { name: "Save as v2" });
  await waitFor(() => expect(button).toHaveProperty("disabled", false));
  fireEvent.click(button);
}

test("renaming preserves the ID route and uses the updated version", async () => {
  let current = tool;
  vi.mocked(request).mockImplementation(async (method, path, body) => {
    if (method === "PUT") {
      const change = body as ToolUpdate;
      current = {
        ...current,
        name: change.name ?? current.name,
        description: change.description,
        input_schema: change.input_schema,
        operation: change.operation,
        version: current.version + 1,
      };
      return current;
    }
    if (method === "GET" && path === "/tools") return { items: [current] };
    if (method === "GET") return current;
    return validation;
  });
  const { qc, router } = editor();
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "new_report" } });
  fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Changed" } });
  await save();
  await waitFor(() => expect(screen.getByRole("button", { name: "No changes" })).toBeTruthy());
  await waitFor(() => expect(router.state.location.pathname).toBe("/tools/t"));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByLabelText("Name")).toHaveProperty("value", "new_report");
  expect(screen.getByLabelText("Description")).toHaveProperty("value", "Changed");
  expect(qc.getQueryData(["tools", tool.id])).toEqual(current);
  fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Next edit" } });
  const next = screen.getByRole("button", { name: "Save as v3" });
  await waitFor(() => expect(next).toHaveProperty("disabled", false));
  fireEvent.click(next);
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "PUT",
      "/tools/t",
      expect.objectContaining({
        name: "new_report",
        expected_version: 2,
        description: "Next edit",
      }),
    ),
  );
  qc.clear();
});

test("name conflicts preserve edits and allow choosing a different name", async () => {
  vi.mocked(request).mockImplementation(async (method, path) => {
    if (method === "PUT")
      throw new ApiError(409, {
        error: "tool_name_conflict",
        message: "Name already exists",
        details: {},
      });
    if (method === "GET" && path === "/tools") return { items: [tool] };
    if (method === "GET") return tool;
    return validation;
  });
  const { qc, router } = editor();
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "taken" } });
  fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Unsaved edit" } });
  await save();
  await screen.findByText("Name already exists");
  expect(router.state.location.pathname).toBe("/tools/t");
  expect(screen.getByLabelText("Name")).toHaveProperty("value", "taken");
  expect(screen.getByLabelText("Description")).toHaveProperty("value", "Unsaved edit");
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "available" } });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Save as v2" })).toHaveProperty("disabled", false),
  );
  qc.clear();
});

test("invalid edited names cannot save and Discard restores the saved name", () => {
  vi.mocked(request).mockResolvedValue(validation);
  const { qc } = editor();
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Invalid Name" } });
  expect(screen.getByRole("button", { name: "Save as v2" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "Disable Tool" })).toHaveProperty("disabled", true);
  expect(screen.getByText(/snake_case/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Discard" }));
  expect(screen.getByLabelText("Name")).toHaveProperty("value", "report");
  expect(screen.getByRole("button", { name: "No changes" })).toBeTruthy();
  qc.clear();
});

test("a committed rename with a lost response offers explicit recovery and keeps edits", async () => {
  const current = { ...tool, name: "new_report", version: 2 };
  vi.mocked(request).mockImplementation(async (method, path) => {
    if (method === "PUT")
      throw new ApiError(503, {
        error: "storage_unavailable",
        message: "Response lost",
        details: {},
      });
    if (method === "GET" && path === "/tools") return { items: [current] };
    if (method === "GET") return current;
    return validation;
  });
  const { qc, router } = editor();
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "new_report" } });
  fireEvent.change(screen.getByLabelText("Description"), { target: { value: "My edit" } });
  await save();
  await screen.findByText("Tool changed on the server");
  expect(screen.getByLabelText("Description")).toHaveProperty("value", "My edit");
  expect(screen.getByRole("button", { name: "Save as v3" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "Reload latest (discards your edits)" }));
  await waitFor(() => expect(router.state.location.pathname).toBe("/tools/t"));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(await screen.findByLabelText("Description")).toHaveProperty("value", tool.description);
  expect(screen.getByRole("button", { name: "No changes" })).toBeTruthy();
  qc.clear();
});

test("concurrent rename preserves the editor until the user reloads", async () => {
  vi.mocked(request).mockResolvedValue(validation);
  const { qc, router } = editor();
  fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Local edit" } });
  await act(async () =>
    qc.setQueryData(["tools", tool.id], { ...tool, name: "remote_report", version: 2 }),
  );
  expect(await screen.findByText("Tool changed on the server")).toBeTruthy();
  expect(screen.getByLabelText("Description")).toHaveProperty("value", "Local edit");
  expect(router.state.location.pathname).toBe("/tools/t");
  expect(screen.getByRole("button", { name: "Save as v3" })).toHaveProperty("disabled", true);
  qc.clear();
});

test("an older list snapshot cannot undo a successful rename in the editor", async () => {
  const current = { ...tool, name: "new_report", version: 2 };
  vi.mocked(request).mockImplementation(async (method, path) => {
    if (method === "PUT") return current;
    if (method === "GET" && path === "/tools") return { items: [tool] };
    if (method === "GET") return current;
    return validation;
  });
  const { qc, router } = editor();
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "new_report" } });
  await save();
  await waitFor(() => expect(qc.getQueryData(["tools", tool.id])).toEqual(current));
  await waitFor(() => expect(router.state.location.pathname).toBe("/tools/t"));
  expect(screen.queryByText("Tool was renamed to report")).toBeNull();
  fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Next edit" } });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Save as v3" })).toHaveProperty("disabled", false),
  );
  qc.clear();
});

test("a stale editor still refreshes its own ID when another Tool reuses the name", async () => {
  const current = { ...tool, name: "renamed_report", version: 2 };
  const replacement = { ...tool, id: "replacement", description: "Another Tool" };
  vi.mocked(request).mockImplementation(async (method, path) => {
    if (method === "PUT")
      throw new ApiError(409, {
        error: "version_conflict",
        message: "Changed on server",
        details: {},
      });
    if (method === "GET" && path === "/tools") return { items: [current, replacement] };
    if (method === "GET" && path === "/tools/t") return current;
    return validation;
  });
  const { qc, router } = editor();
  fireEvent.change(screen.getByLabelText("Description"), { target: { value: "My unsaved edit" } });
  await save();
  await screen.findByText("Tool changed on the server");
  expect(screen.getByLabelText("Description")).toHaveProperty("value", "My unsaved edit");
  expect(screen.getByLabelText("Name")).toHaveProperty("value", tool.name);
  expect(qc.getQueryData(["tools", tool.id])).toEqual(current);
  expect(router.state.location.pathname).toBe("/tools/t");
  expect(screen.getByRole("button", { name: "Save as v3" })).toHaveProperty("disabled", true);
  expect(request).toHaveBeenCalledWith(
    "PUT",
    "/tools/t",
    expect.objectContaining({ expected_version: 1 }),
  );
  expect(request).not.toHaveBeenCalledWith("GET", "/tools/replacement");
  qc.clear();
});

test("renaming to new keeps the saved editor separate from the create screen", async () => {
  const current = { ...tool, name: "new", version: 2 };
  vi.mocked(request).mockImplementation(async (method, path) => {
    if (method === "PUT") return current;
    if (method === "GET" && path === "/tools") return { items: [current] };
    if (method === "GET") return current;
    return validation;
  });
  const { qc, router } = editor();
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "new" } });
  await save();
  await waitFor(() => expect(qc.getQueryData(["tools", tool.id])).toEqual(current));
  expect(router.state.location.pathname).toBe("/tools/t");
  expect(screen.getByLabelText("Name")).toHaveProperty("value", "new");
  expect(screen.getByRole("button", { name: "Disable Tool" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Save tool" })).toBeNull();
  qc.clear();
});
