// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider, MemoryRouter } from "react-router";
import { afterEach, expect, test, vi } from "vitest";
import { request } from "@/api/client";
import type { Context, Tool } from "@/api/types";
import ToolEditorPage from "@/pages/ToolEditorPage";
import PlaygroundPage from "@/pages/PlaygroundPage";
import ContextsPage from "@/pages/ContextsPage";
import { InvokePanel } from "@/pages/tool/InvokePanel";

vi.mock("@/api/client", async (original) => ({
  ...(await original<typeof import("@/api/client")>()),
  request: vi.fn(),
}));
vi.mock("@/components/app/LazySqlEditor", () => ({
  LazySqlEditor: ({
    value,
    onChange,
    readOnly,
  }: {
    value: string;
    onChange?: (v: string) => void;
    readOnly?: boolean;
  }) => (
    <textarea
      aria-label={readOnly ? "SQL result" : "SQL editor"}
      value={value}
      readOnly={readOnly}
      onChange={(e) => onChange?.(e.target.value)}
    />
  ),
}));
afterEach(() => {
  cleanup();
  vi.mocked(request).mockReset();
  localStorage.clear();
});
const valid = { valid: true, errors: [], warnings: [], checks: [] };
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
  description: "original description",
  input_schema: { type: "object", properties: {}, required: [], additionalProperties: false },
  operation: { type: "sql", statement: "SELECT 1" },
  version: 1,
  context_version: 1,
  status: "valid",
  validation_errors: [],
  validation: valid,
  source_generation_id: null,
  created_at: "",
  updated_at: "",
};
function client() {
  const qc = new QueryClient({
    defaultOptions: { queries: { staleTime: Infinity, retry: false } },
  });
  qc.setQueryData(["contexts"], [context]);
  qc.setQueryData(["tools", "report"], tool);
  return qc;
}
function editor(qc: QueryClient, path = "/tools/report") {
  const router = createMemoryRouter(
    [
      { path: "/tools/new", element: <ToolEditorPage /> },
      { path: "/tools/:name", element: <ToolEditorPage /> },
      { path: "/tools", element: <div>Tool listing</div> },
    ],
    { initialEntries: [path] },
  );
  render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

test("Tool navigation protects unsaved edits until explicitly discarded", async () => {
  vi.mocked(request).mockResolvedValue(valid);
  const qc = client();
  const router = editor(qc);
  const user = userEvent.setup();
  fireEvent.change(screen.getByLabelText("Description"), { target: { value: "unsaved work" } });
  await user.click(screen.getByRole("link", { name: "Tools" }));
  expect(screen.getByRole("dialog")).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "Keep editing" }));
  expect(screen.getByLabelText("Description")).toHaveProperty("value", "unsaved work");
  await user.click(screen.getByRole("link", { name: "Tools" }));
  await user.click(screen.getByRole("button", { name: "Discard and leave" }));
  expect(screen.getByText("Tool listing")).toBeTruthy();
  await act(async () => {
    await router.navigate(-1);
  });
  expect(screen.getByLabelText("Description")).toHaveProperty("value", "original description");
  qc.clear();
});

test("First save locks edits and navigates without a discard prompt", async () => {
  let resolve!: (v: Tool) => void;
  const pending = new Promise<Tool>((r) => (resolve = r));
  vi.mocked(request).mockImplementation(async (_method, path) =>
    path === "/tools" ? pending : valid,
  );
  const qc = client();
  editor(qc, "/tools/new");
  const user = userEvent.setup();
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "report" } });
  fireEvent.change(screen.getByLabelText("Description"), {
    target: { value: "submitted description" },
  });
  fireEvent.change(screen.getByLabelText("SQL editor"), { target: { value: "SELECT 1" } });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Save tool" })).toHaveProperty("disabled", false),
  );
  await user.click(screen.getByRole("button", { name: "Save tool" }));
  expect(screen.getByLabelText("Description").matches(":disabled")).toBe(true);
  await user.type(screen.getByLabelText("Description"), "new work typed while saving");
  expect(screen.getByLabelText("Description")).toHaveProperty("value", "submitted description");
  await act(async () => resolve({ ...tool, description: "submitted description" }));
  await waitFor(() =>
    expect(screen.getByLabelText("Description")).toHaveProperty("value", "submitted description"),
  );
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByLabelText("Name")).toHaveProperty("disabled", true);
  qc.clear();
});

test("Rebuild locks edits until the response is applied", async () => {
  let resolve!: (v: Tool) => void;
  const pending = new Promise<Tool>((r) => (resolve = r));
  vi.mocked(request).mockImplementation(async (_method, path) =>
    path.endsWith("refresh-contract") ? pending : valid,
  );
  const qc = client();
  const bound = { ...tool, metric_binding: { metric_id: "count", time_field: null } };
  qc.setQueryData(["tools", "report"], bound);
  editor(qc);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Rebuild from contract" }));
  expect(screen.getByLabelText("Description").matches(":disabled")).toBe(true);
  await user.type(screen.getByLabelText("Description"), "description edited during rebuild");
  await act(async () => resolve({ ...bound, version: 2 }));
  await waitFor(() =>
    expect(screen.getByLabelText("Description")).toHaveProperty("value", "original description"),
  );
  qc.clear();
});

test("Playground binds an earlier result to its submitted question", async () => {
  vi.mocked(request).mockResolvedValue({
    status: "ok",
    generation_id: "g1",
    context_version: 1,
    source: "generated_sql",
    operation: {
      type: "sql",
      dialect: "postgresql",
      statement: "SELECT 1 AS old_answer",
      parameters: {},
      parameter_types: {},
    },
    validation: valid,
    tool_suggestion: null,
  });
  const qc = client();
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <PlaygroundPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const user = userEvent.setup();
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "first question" } });
  await user.click(screen.getByRole("button", { name: "Generate operation" }));
  await screen.findByRole("button", { name: "Create Tool draft" });
  fireEvent.change(screen.getByLabelText("Question"), {
    target: { value: "a different question" },
  });
  expect(screen.getByLabelText("SQL result")).toHaveProperty("value", "SELECT 1 AS old_answer");
  expect(screen.getByRole("button", { name: "Create draft from previous result" })).toBeTruthy();
  expect(screen.getByText("first question")).toBeTruthy();
  expect(screen.getByText(/Inputs or Context changed/)).toBeTruthy();
  qc.clear();
});

test("Invoke parameter rows belong to the result even after Tool schema changes", async () => {
  const first = {
    ...tool,
    input_schema: {
      ...tool.input_schema,
      properties: { old_arg: { type: "string" as const } },
      required: ["old_arg"],
    },
  };
  const response = {
    tool_version: 1,
    context_version: 1,
    validation: valid,
    operation: {
      type: "sql",
      dialect: "postgresql",
      statement: "SELECT :old_arg",
      parameters: { old_arg: "review-value" },
      parameter_types: { old_arg: "string" },
    },
  };
  vi.mocked(request).mockResolvedValue(response);
  const qc = client();
  const wrap = (t: Tool) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <InvokePanel tool={t} dirty={false} />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(wrap(first));
  const user = userEvent.setup();
  fireEvent.change(screen.getByLabelText(/old_arg/), { target: { value: "review-value" } });
  await user.click(screen.getByRole("button", { name: "Invoke Tool" }));
  await screen.findByText('"review-value"');
  view.rerender(
    wrap({
      ...tool,
      version: 2,
      input_schema: {
        ...tool.input_schema,
        properties: { new_arg: { type: "string" } },
        required: ["new_arg"],
      },
    }),
  );
  expect(screen.getByLabelText("SQL result")).toHaveProperty("value", "SELECT :old_arg");
  expect(screen.getByText(":old_arg")).toBeTruthy();
  expect(screen.getByText('"review-value"')).toBeTruthy();
  expect(screen.queryByText(":new_arg")).toBeNull();
  expect(screen.getByText(/earlier Tool or Context version/)).toBeTruthy();
  qc.clear();
});

test.each(["create", "rebuild"])(
  "%s failure unlocks the editor and keeps entered values",
  async (action) => {
    let reject!: (error: Error) => void;
    const pending = new Promise<Tool>((_, fail) => {
      reject = fail;
    });
    vi.mocked(request).mockImplementation(async (_method, path) =>
      path === "/tools" || path.endsWith("refresh-contract") ? pending : valid,
    );
    const qc = client();
    if (action === "rebuild")
      qc.setQueryData(["tools", "report"], {
        ...tool,
        metric_binding: { metric_id: "count", time_field: null },
      });
    editor(qc, action === "create" ? "/tools/new" : "/tools/report");
    const user = userEvent.setup();
    if (action === "create") {
      fireEvent.change(screen.getByLabelText("Name"), { target: { value: "report" } });
      fireEvent.change(screen.getByLabelText("Description"), {
        target: { value: "keep after failure" },
      });
      fireEvent.change(screen.getByLabelText("SQL editor"), { target: { value: "SELECT 1" } });
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Save tool" })).toHaveProperty("disabled", false),
      );
      await user.click(screen.getByRole("button", { name: "Save tool" }));
    } else await user.click(screen.getByRole("button", { name: "Rebuild from contract" }));
    expect(screen.getByLabelText("Description").matches(":disabled")).toBe(true);
    await user.click(screen.getByRole("link", { name: "Tools" }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Discard and leave" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    await act(async () => reject(new Error("Offline")));
    await waitFor(() =>
      expect(screen.getByLabelText("Description").matches(":disabled")).toBe(false),
    );
    expect(screen.getByLabelText("Description")).toHaveProperty(
      "value",
      action === "create" ? "keep after failure" : "original description",
    );
    await user.type(screen.getByLabelText("Description"), " more work");
    expect((screen.getByLabelText("Description") as HTMLTextAreaElement).value).toContain(
      "more work",
    );
    qc.clear();
  },
);

test("an untouched blank Tool can be left without confirmation", async () => {
  const qc = client();
  editor(qc, "/tools/new");
  await userEvent.setup().click(screen.getByRole("link", { name: "Tools" }));
  expect(screen.getByText("Tool listing")).toBeTruthy();
  expect(screen.queryByRole("dialog")).toBeNull();
  qc.clear();
});

test("deleting a Tool navigates normally even with unsaved edits", async () => {
  vi.mocked(request).mockResolvedValue(valid);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  const qc = client();
  editor(qc);
  fireEvent.change(screen.getByLabelText("Description"), { target: { value: "unsaved" } });
  await userEvent.setup().click(screen.getByRole("button", { name: "Delete" }));
  await screen.findByText("Tool listing");
  expect(screen.queryByRole("dialog")).toBeNull();
  confirm.mockRestore();
  qc.clear();
});

test("Playground retains the submitted request when inputs change during generation", async () => {
  let resolve!: (value: unknown) => void;
  vi.mocked(request).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const qc = client();
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <PlaygroundPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const user = userEvent.setup();
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "submitted question" } });
  fireEvent.change(screen.getByLabelText("Reference time (optional)"), {
    target: { value: "2026-10-01T00:00:00Z" },
  });
  await user.click(screen.getByRole("button", { name: "Generate operation" }));
  fireEvent.change(screen.getByLabelText("Question"), {
    target: { value: "edited while waiting" },
  });
  fireEvent.change(screen.getByLabelText("Reference time (optional)"), {
    target: { value: "2026-10-02T00:00:00Z" },
  });
  await act(async () =>
    resolve({
      status: "ok",
      generation_id: "g1",
      context_version: 1,
      source: "generated_sql",
      operation: {
        type: "sql",
        dialect: "postgresql",
        statement: "SELECT 1",
        parameters: {},
        parameter_types: {},
      },
      validation: valid,
      tool_suggestion: null,
    }),
  );
  expect(
    await screen.findByRole("button", { name: "Create draft from previous result" }),
  ).toBeTruthy();
  expect(screen.getByText("submitted question")).toBeTruthy();
  expect(screen.getByText("2026-10-01T00:00:00Z")).toBeTruthy();
  expect(screen.getByLabelText("Question")).toHaveProperty("value", "edited while waiting");
  qc.clear();
});

test.each(["tool", "contexts"])(
  "background %s refresh failure preserves unsaved Tool edits",
  async (source) => {
    vi.mocked(request).mockResolvedValue(valid);
    const qc = client();
    editor(qc);
    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "valuable unsaved edits" },
    });
    await act(async () => {
      await qc
        .fetchQuery({
          queryKey: source === "tool" ? ["tools", "report"] : ["contexts"],
          queryFn: async () => {
            throw new Error("temporary connection failure");
          },
          staleTime: 0,
          retry: false,
        })
        .catch(() => {});
    });
    await screen.findByText(`Could not refresh ${source}; your edits are preserved`);
    expect(screen.getByLabelText("Description")).toHaveProperty("value", "valuable unsaved edits");
    await act(async () => {
      qc.setQueryData(
        source === "tool" ? ["tools", "report"] : ["contexts"],
        source === "tool" ? { ...tool } : [{ ...context }],
      );
    });
    expect(await screen.findByLabelText("Description")).toHaveProperty(
      "value",
      "valuable unsaved edits",
    );
    qc.clear();
  },
);

test("a changed boolean input displays and submits the same reset value", async () => {
  const qc = client();
  const inputTool: Tool = {
    ...tool,
    input_schema: {
      type: "object",
      properties: { active: { type: "string" } },
      required: ["active"],
      additionalProperties: false,
    },
  };
  const ui = (value: Tool) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <InvokePanel tool={value} dirty={false} />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(ui(inputTool));
  fireEvent.change(screen.getByLabelText(/active/), { target: { value: "true" } });
  view.rerender(
    ui({
      ...inputTool,
      version: 2,
      input_schema: { ...inputTool.input_schema, properties: { active: { type: "boolean" } } },
    }),
  );
  expect(screen.getByRole("checkbox").getAttribute("aria-checked")).toBe("false");
  vi.mocked(request).mockRejectedValue(new Error("stop after capturing request"));
  await userEvent.setup().click(screen.getByRole("button", { name: "Invoke Tool" }));
  expect(request).toHaveBeenCalledWith("POST", "/tools/report/invoke", {
    arguments: { active: false },
    expected_version: 2,
  });
  qc.clear();
});

test.each([true, false])(
  "Context creation locks inputs and preserves them on failure (success: %s)",
  async (success) => {
    const qc = client();
    let resolve!: (value: Context) => void;
    let reject!: (error: Error) => void;
    const pending = new Promise<Context>((done, fail) => {
      resolve = done;
      reject = fail;
    });
    vi.mocked(request).mockImplementation(async (_method, path) =>
      path === "/contexts" ? pending : context,
    );
    const router = createMemoryRouter(
      [
        { path: "/contexts", element: <ContextsPage /> },
        { path: "/contexts/:id", element: <div>Created context</div> },
      ],
      { initialEntries: ["/contexts"] },
    );
    render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "first" } });
    const originalTimezone = (screen.getByLabelText("Timezone") as HTMLInputElement).value;
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(screen.getByLabelText("Name").matches(":disabled")).toBe(true);
    expect(screen.getByLabelText("Timezone").matches(":disabled")).toBe(true);
    await user.type(screen.getByLabelText("Name"), "second name typed during save");
    expect(screen.getByLabelText("Name")).toHaveProperty("value", "first");
    if (success) {
      await act(async () => resolve({ ...context, name: "first" }));
      expect(await screen.findByText("Created context")).toBeTruthy();
    } else {
      await act(async () => reject(new Error("Create failed")));
      expect(await screen.findByText("Create failed")).toBeTruthy();
      expect(screen.getByLabelText("Name")).toHaveProperty("value", "first");
      expect(screen.getByLabelText("Timezone")).toHaveProperty("value", originalTimezone);
      expect(screen.getByLabelText("Name").matches(":disabled")).toBe(false);
    }
    qc.clear();
  },
);

test("schema changes preserve compatible values and reset removed enum choices", async () => {
  const qc = client();
  const inputTool: Tool = {
    ...tool,
    input_schema: {
      type: "object",
      properties: {
        region: { type: "string" },
        status: { type: "string", enum: ["old", "new"] },
      },
      required: ["region", "status"],
      additionalProperties: false,
    },
  };
  const ui = (value: Tool) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <InvokePanel tool={value} dirty={false} />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(ui(inputTool));
  fireEvent.change(screen.getByLabelText(/region/), { target: { value: "Seoul" } });
  fireEvent.change(screen.getByLabelText(/status/), { target: { value: "0" } });
  view.rerender(
    ui({
      ...inputTool,
      version: 2,
      input_schema: {
        ...inputTool.input_schema,
        properties: {
          region: { type: "string" },
          status: { type: "string", enum: ["new"] },
        },
      },
    }),
  );
  expect(screen.getByLabelText(/region/)).toHaveProperty("value", "Seoul");
  expect(screen.getByLabelText(/status/)).toHaveProperty("value", "");
  await userEvent.setup().click(screen.getByRole("button", { name: "Invoke Tool" }));
  expect(screen.getByText("This input is required.")).toBeTruthy();
  expect(request).not.toHaveBeenCalled();
  qc.clear();
});
