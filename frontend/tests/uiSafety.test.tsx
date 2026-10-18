// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider, MemoryRouter } from "react-router";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError, request } from "@/api/client";
import type { Context, Tool } from "@/api/types";
import ToolEditorPage from "@/pages/ToolEditorPage";
import PlaygroundPage from "@/pages/PlaygroundPage";
import { ContextEditor } from "@/pages/ContextEditor";
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
  enabled: true,
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
  qc.setQueryData(["tools", "t"], tool);
  qc.setQueryData(["tools"], [tool]);
  return qc;
}
function editor(qc: QueryClient, path = "/tools/t") {
  const router = createMemoryRouter(
    [
      { path: "/tools/new", element: <ToolEditorPage /> },
      { path: "/tools/:id", element: <ToolEditorPage /> },
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

test("blocked browser storage does not prevent Playground rendering", () => {
  const read = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new DOMException("Storage access is denied", "SecurityError");
  });
  try {
    render(
      <QueryClientProvider client={client()}>
        <MemoryRouter>
          <PlaygroundPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect((screen.getByLabelText("Context") as HTMLSelectElement).value).toBe("c");
    expect(screen.getByLabelText("Question")).toBeTruthy();
  } finally {
    read.mockRestore();
  }
});

test("full browser storage does not prevent switching Playground Contexts", () => {
  const qc = client();
  qc.setQueryData(["contexts"], [context, { ...context, id: "other", name: "other" }]);
  const write = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new DOMException("Storage quota exceeded", "QuotaExceededError");
  });
  try {
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <PlaygroundPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    fireEvent.change(screen.getByLabelText("Context"), { target: { value: "other" } });
    expect((screen.getByLabelText("Context") as HTMLSelectElement).value).toBe("other");
  } finally {
    write.mockRestore();
  }
});

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
  vi.mocked(request).mockImplementation(async (method, path) =>
    path === "/tools" ? (method === "POST" ? pending : { items: [tool] }) : valid,
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
  expect(screen.getByLabelText("Name")).toHaveProperty("disabled", false);
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
  qc.setQueryData(["tools", "t"], bound);
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
    execution: "independent",
    operations: [
      {
        id: "step_1",
        source: "generated_sql",
        task: "Generated task",
        operation: {
          type: "sql",
          dialect: "postgresql",
          statement: "SELECT 1 AS old_answer",
          parameters: {},
          parameter_types: {},
        },
        validation: valid,
        tool_suggestion: null,
      },
    ],
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
  await screen.findByRole("link", { name: "Create Tool draft from step_1" });
  fireEvent.change(screen.getByLabelText("Question"), {
    target: { value: "a different question" },
  });
  expect(screen.getByLabelText("SQL result")).toHaveProperty("value", "SELECT 1 AS old_answer");
  expect(screen.getByRole("link", { name: "Create draft from previous result" })).toBeTruthy();
  expect(screen.getByText("first question")).toBeTruthy();
  expect(screen.getByText(/Inputs or Context changed/)).toBeTruthy();
  const write = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
  await user.click(screen.getByRole("button", { name: "Copy API request" }));
  const command = write.mock.calls[0][0];
  expect(command).toContain('"question": "first question"');
  expect(command).toContain('"context_id": "c"');
  expect(command).not.toContain("a different question");
  write.mockRestore();
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
    tool_id: "t",
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
      qc.setQueryData(["tools", "t"], {
        ...tool,
        metric_binding: { metric_id: "count", time_field: null },
      });
    editor(qc, action === "create" ? "/tools/new" : "/tools/t");
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
      execution: "independent",
      operations: [
        {
          id: "step_1",
          source: "generated_sql",
          task: "Generated task",
          operation: {
            type: "sql",
            dialect: "postgresql",
            statement: "SELECT 1",
            parameters: {},
            parameter_types: {},
          },
          validation: valid,
          tool_suggestion: null,
        },
      ],
    }),
  );
  expect(
    await screen.findByRole("link", { name: "Create draft from previous result" }),
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
          queryKey: source === "tool" ? ["tools", "t"] : ["contexts"],
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
        source === "tool" ? ["tools", "t"] : ["contexts"],
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
  expect(request).toHaveBeenCalledWith("POST", "/tools/t/invoke", {
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

test("Playground identifies a reused Tool and links to it instead of creating another draft", async () => {
  vi.mocked(request).mockResolvedValue({
    status: "ok",
    source: "plan",
    execution: "independent",
    generation_id: "g-tool",
    context_version: 1,
    operations: [
      {
        id: "step_1",
        source: "tool",
        selected_tool: { id: "t", name: "report", version: 3 },
        operation: {
          type: "sql",
          dialect: "postgresql",
          statement: "SELECT 1",
          parameters: {},
          parameter_types: {},
        },
        validation: valid,
      },
    ],
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
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "Run my report" } });
  await user.click(screen.getByRole("button", { name: "Generate operation" }));
  const link = await screen.findByRole("link", { name: "report" });
  expect(link.getAttribute("href")).toBe("/tools/t");
  expect(screen.getByText(/step_1 · saved Tool/)).toBeTruthy();
  expect(screen.getByText(/v3/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Create Tool draft" })).toBeNull();
  expect(screen.queryByText(/Create and save a Tool before/)).toBeNull();
  expect(screen.getByRole("button", { name: "Copy API request" })).toBeTruthy();
  qc.clear();
});

const clarification = {
  continuation_request: {
    context_id: "c",
    question: "This month reviews",
    reference_time: "2026-10-13T01:00:00Z",
    expected_context_version: 1,
  },
  status: "ambiguous",
  generation_id: "g1",
  context_version: 1,
  reference_time: "2026-10-13T01:00:00Z",
  reason: "Choose a date basis.",

  clarification_questions: [
    { id: "g1:0", question: "Which date?", options: ["Created", "Updated"] },
  ],
};
function playground(qc: QueryClient) {
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <PlaygroundPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

test("Clarification preserves the original request and accumulates answers across rounds", async () => {
  const second = {
    ...clarification,
    generation_id: "g2",
    continuation_request: {
      ...clarification.continuation_request,
      clarification_answers: [{ id: "g1:0", question: "Which date?", answer: "Created" }],
    },

    clarification_questions: [{ id: "g2:0", question: "Which region?", options: [] }],
  };
  vi.mocked(request)
    .mockResolvedValueOnce(clarification)
    .mockResolvedValueOnce(second)
    .mockResolvedValueOnce({
      status: "unanswerable",
      generation_id: "g3",
      context_version: 1,
      reason: "No matching data.",
    });
  const qc = client();
  playground(qc);
  const user = userEvent.setup();
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "This month reviews" } });
  await user.click(screen.getByRole("button", { name: "Generate operation" }));
  await screen.findByText("Which date?");
  expect(screen.getByRole("button", { name: "Continue with answers" }).matches(":disabled")).toBe(
    true,
  );
  await user.click(screen.getByRole("button", { name: "Created" }));
  await user.click(screen.getByRole("button", { name: "Continue with answers" }));
  await screen.findByText("Which region?");
  expect(request).toHaveBeenNthCalledWith(2, "POST", "/generate", {
    context_id: "c",
    question: "This month reviews",
    reference_time: clarification.reference_time,
    expected_context_version: 1,
    clarification_answers: [{ id: "g1:0", question: "Which date?", answer: "Created" }],
  });
  expect(screen.queryByText(/Inputs or Context changed since/)).toBeNull();
  await user.type(screen.getByLabelText("Which region?"), "Seoul");
  await user.click(screen.getByRole("button", { name: "Continue with answers" }));
  await screen.findByText("No matching data.");
  expect(request).toHaveBeenLastCalledWith("POST", "/generate", {
    context_id: "c",
    question: "This month reviews",
    reference_time: clarification.reference_time,
    expected_context_version: 1,
    clarification_answers: [
      { id: "g1:0", question: "Which date?", answer: "Created" },
      { id: "g2:0", question: "Which region?", answer: "Seoul" },
    ],
  });
  expect(screen.queryByRole("button", { name: "Continue with answers" })).toBeNull();
  qc.clear();
});

test("Clarification keeps answers on failure and blocks stale inputs", async () => {
  vi.mocked(request)
    .mockResolvedValueOnce(clarification)
    .mockRejectedValueOnce(new Error("Offline"));
  const qc = client();
  playground(qc);
  const user = userEvent.setup();
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "This month reviews" } });
  await user.click(screen.getByRole("button", { name: "Generate operation" }));
  await user.click(await screen.findByRole("button", { name: "Created" }));
  await user.click(screen.getByRole("button", { name: "Continue with answers" }));
  await screen.findByText("Offline");
  expect(screen.getByRole("textbox", { name: "Which date?" })).toHaveProperty("value", "Created");
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "Another request" } });
  expect(screen.getByRole("button", { name: "Continue with answers" }).matches(":disabled")).toBe(
    true,
  );
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "This month reviews" } });
  expect(screen.getByRole("button", { name: "Continue with answers" }).matches(":disabled")).toBe(
    false,
  );
  await act(async () => {
    qc.setQueryData(["contexts"], [{ ...context, version: 2 }]);
  });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Continue with answers" }).matches(":disabled")).toBe(
      true,
    ),
  );
  qc.clear();
});

test("Starting a new request drops prior clarification answers", async () => {
  vi.mocked(request).mockResolvedValue(clarification);
  const qc = client();
  playground(qc);
  const user = userEvent.setup();
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "First request" } });
  await user.click(screen.getByRole("button", { name: "Generate operation" }));
  await user.click(await screen.findByRole("button", { name: "Created" }));
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "New request" } });
  await user.click(screen.getByRole("button", { name: "Generate operation" }));
  await screen.findByRole("textbox", { name: "Which date?" });
  expect(request).toHaveBeenLastCalledWith("POST", "/generate", {
    context_id: "c",
    question: "New request",
  });
  expect(screen.getByRole("textbox", { name: "Which date?" })).toHaveProperty("value", "");
  qc.clear();
});

test("Clarification retry uses the response Context version after the cache catches up", async () => {
  vi.mocked(request)
    .mockResolvedValueOnce({
      ...clarification,
      context_version: 2,
      continuation_request: {
        ...clarification.continuation_request,
        expected_context_version: 2,
      },
    })
    .mockRejectedValueOnce(new Error("Offline"));
  const qc = client();
  playground(qc);
  const user = userEvent.setup();
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "This month reviews" } });
  await user.click(screen.getByRole("button", { name: "Generate operation" }));
  await screen.findByRole("button", { name: "Created" });
  await act(async () => {
    qc.setQueryData(["contexts"], [{ ...context, version: 2 }]);
  });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Created" }).matches(":disabled")).toBe(false),
  );
  await user.click(screen.getByRole("button", { name: "Created" }));
  await user.click(screen.getByRole("button", { name: "Continue with answers" }));
  await screen.findByText("Offline");
  expect(screen.getByRole("textbox", { name: "Which date?" })).toHaveProperty("value", "Created");
  expect(screen.getByRole("button", { name: "Continue with answers" }).matches(":disabled")).toBe(
    false,
  );
  vi.mocked(request).mockResolvedValueOnce({
    status: "unanswerable",
    generation_id: "g2",
    context_version: 2,
    reason: "No matching data",
  });
  await user.click(screen.getByRole("button", { name: "Continue with answers" }));
  await screen.findByText("No matching data");
  expect(request).toHaveBeenLastCalledWith(
    "POST",
    "/generate",
    expect.objectContaining({ expected_context_version: 2 }),
  );
  qc.clear();
});

test("Tool conflict preserves edits, reloads explicitly, and saves with the new version", async () => {
  const remote = { ...tool, version: 2, description: "remote description" };
  let conflict = true;
  vi.mocked(request).mockImplementation(async (method, path, body) => {
    if (method === "PUT") {
      if (conflict) {
        conflict = false;
        throw new ApiError(409, { error: "version_conflict", message: "Changed", details: {} });
      }
      return { ...remote, ...(body as object), version: 3 };
    }
    if (method === "GET" && path === "/tools/t") return remote;
    return valid;
  });
  const qc = client();
  editor(qc);
  fireEvent.change(screen.getByLabelText("Description"), { target: { value: "my draft" } });
  const save = screen.getByRole("button", { name: "Save as v2" });
  await waitFor(() => expect(save).toHaveProperty("disabled", false));
  fireEvent.click(save);
  expect(await screen.findByText("Changed")).toBeTruthy();
  expect(screen.getByLabelText("Description")).toHaveProperty("value", "my draft");
  const reload = screen.getByRole("button", { name: "Reload latest (discards your edits)" });
  await waitFor(() => expect(reload).toHaveProperty("disabled", false));
  fireEvent.click(reload);
  await waitFor(() =>
    expect(screen.getByLabelText("Description")).toHaveProperty("value", "remote description"),
  );
  fireEvent.change(screen.getByLabelText("Description"), { target: { value: "revised" } });
  const retry = screen.getByRole("button", { name: "Save as v3" });
  await waitFor(() => expect(retry).toHaveProperty("disabled", false));
  fireEvent.click(retry);
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "PUT",
      "/tools/t",
      expect.objectContaining({ expected_version: 2 }),
    ),
  );
  qc.clear();
});

test("Context authentication failures preserve edits and unapplied DDL across refetch", async () => {
  vi.mocked(request).mockRejectedValue(
    new ApiError(401, {
      error: "admin_session_required",
      message: "Sign in to continue.",
      details: {},
    }),
  );
  const qc = client();
  qc.setQueryData(["contexts", "c"], context);
  const router = createMemoryRouter([{ path: "/contexts/c", element: <ContextEditor id="c" /> }], {
    initialEntries: ["/contexts/c"],
  });
  render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "unsaved context" } });
  fireEvent.click(screen.getByRole("button", { name: "Import DDL" }));
  const ddl = "CREATE TABLE pending_import (id bigint);";
  fireEvent.change(screen.getByLabelText("SQL editor"), { target: { value: ddl } });
  await act(async () => {
    await qc.refetchQueries({ queryKey: ["contexts", "c"], exact: true });
  });
  expect(await screen.findByText("Could not refresh context")).toBeTruthy();
  expect(screen.getByLabelText("Name")).toHaveProperty("value", "unsaved context");
  expect(screen.getByLabelText("SQL editor")).toHaveProperty("value", ddl);
  vi.mocked(request).mockResolvedValue(context);
  await act(async () => {
    await qc.refetchQueries({ queryKey: ["contexts", "c"], exact: true });
  });
  await waitFor(() => expect(screen.queryByText("Could not refresh context")).toBeNull());
  expect(screen.getByLabelText("Name")).toHaveProperty("value", "unsaved context");
  expect(screen.getByLabelText("SQL editor")).toHaveProperty("value", ddl);
  qc.clear();
});

test("Deleting a dirty Context navigates without a discard dialog", async () => {
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.mocked(request).mockImplementation(async (method, path) => {
    if (method === "DELETE") return undefined;
    if (path === "/contexts") return { items: [] };
    return context;
  });
  const qc = client();
  qc.setQueryData(["contexts", "c"], context);
  const router = createMemoryRouter(
    [
      { path: "/contexts/c", element: <ContextEditor id="c" /> },
      { path: "/contexts", element: <div>Context listing</div> },
    ],
    { initialEntries: ["/contexts/c"] },
  );
  render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "unsaved" } });
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  expect(await screen.findByText("Context listing")).toBeTruthy();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(request).toHaveBeenCalledWith("DELETE", "/contexts/c");
  vi.mocked(window.confirm).mockRestore();
  qc.clear();
});

test.each(["success", "failure"])(
  "Tool reload locks edits and navigation until %s",
  async (outcome) => {
    const remote = { ...tool, version: 2, description: "remote description" };
    let resolve!: (value: Tool) => void;
    let reject!: (reason: Error) => void;
    const pending = new Promise<Tool>((done, fail) => {
      resolve = done;
      reject = fail;
    });
    vi.mocked(request).mockImplementation(async (method, path) => {
      if (method === "GET" && path === "/tools/t") return pending;
      return valid;
    });
    const qc = client();
    editor(qc);
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "old draft" } });
    await act(async () => {
      qc.setQueryData(["tools", "t"], remote);
    });
    fireEvent.click(
      await screen.findByRole("button", { name: "Reload latest (discards your edits)" }),
    );
    await waitFor(() => expect(request).toHaveBeenCalledWith("GET", "/tools/t"));
    const field = screen.getByLabelText("Description");
    expect(field.matches(":disabled")).toBe(true);
    fireEvent.change(field, { target: { value: "new edit during reload" } });
    expect(field).toHaveProperty("value", "old draft");
    await userEvent.setup().click(screen.getByRole("link", { name: "Tools" }));
    expect(screen.getByRole("dialog")).toBeTruthy();
    await userEvent.setup().click(screen.getByRole("button", { name: "Keep editing" }));
    await act(async () => {
      if (outcome === "success") resolve(remote);
      else reject(new Error("Reload unavailable"));
    });
    await waitFor(() => expect(field.matches(":disabled")).toBe(false));
    expect(field).toHaveProperty(
      "value",
      outcome === "success" ? "remote description" : "old draft",
    );
    qc.clear();
  },
);

const toolPlanResult = {
  status: "ok",
  source: "plan",
  execution: "independent",
  generation_id: "plan-id",
  context_version: 1,
  operations: ["서울", "부산"].map((region, i) => ({
    id: `step_${i + 1}`,
    source: "tool",
    selected_tool: { id: "t", name: "report", version: 1 },
    operation: {
      type: "sql",
      dialect: "postgresql",
      statement: "SELECT COUNT(*) FROM stores WHERE region = :region",
      parameters: { region },
      parameter_types: { region: "string" },
    },
    validation: valid,
  })),
};

test("Default generation and displays all operations without a draft action", async () => {
  vi.mocked(request).mockResolvedValue(toolPlanResult);
  const qc = client();
  playground(qc);
  const user = userEvent.setup();
  expect(screen.queryByRole("checkbox", { name: "Plan with saved Tools" })).toBeNull();
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "서울 부산 매장 수" } });
  await user.click(screen.getByRole("button", { name: "Generate operation" }));
  expect(await screen.findByText("Saved Tool plan · 2 operations")).toBeTruthy();
  expect(screen.getByRole("region", { name: "step_1" })).toBeTruthy();
  expect(screen.getByRole("region", { name: "step_2" })).toBeTruthy();
  expect(screen.getAllByLabelText("SQL result")).toHaveLength(2);
  expect(screen.getByRole("button", { name: "Copy plan JSON" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Create Tool draft" })).toBeNull();
  expect(request).toHaveBeenCalledWith(
    "POST",
    "/generate",
    expect.objectContaining({ question: "서울 부산 매장 수" }),
  );
  qc.clear();
});

test("Default plan clarification preserves request and prevents continuation after question change", async () => {
  vi.mocked(request)
    .mockResolvedValueOnce({
      status: "ambiguous",
      continuation_request: {
        context_id: "c",
        question: "Compare regions",
        reference_time: "2026-10-13T00:00:00Z",
        expected_context_version: 1,
      },
      generation_id: "plan-question",
      context_version: 1,
      reason: "Which regions?",

      clarification_questions: [{ id: "q", question: "Regions?", options: [] }],
      reference_time: "2026-10-13T00:00:00Z",
    })
    .mockResolvedValue(toolPlanResult);
  const qc = client();
  playground(qc);
  const user = userEvent.setup();
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "Compare regions" } });
  await user.click(screen.getByRole("button", { name: "Generate operation" }));
  const answer = await screen.findByLabelText("Regions?");
  fireEvent.change(answer, { target: { value: "서울 부산" } });
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "Changed question" } });
  expect(answer.matches(":disabled")).toBe(true);
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "Compare regions" } });
  await user.click(screen.getByRole("button", { name: "Continue with answers" }));
  expect(await screen.findByText("Saved Tool plan · 2 operations")).toBeTruthy();
  expect(request).toHaveBeenLastCalledWith(
    "POST",
    "/generate",
    expect.objectContaining({
      expected_context_version: 1,
      reference_time: "2026-10-13T00:00:00Z",
      clarification_answers: [{ id: "q", question: "Regions?", answer: "서울 부산" }],
    }),
  );
  qc.clear();
});

test("additional options default closed, show active values, and preserve the API request", async () => {
  vi.mocked(request).mockResolvedValue(toolPlanResult);
  const qc = client();
  playground(qc);
  const user = userEvent.setup();
  const summary = screen.getByText("Additional options");
  const details = summary.closest("details")!;
  expect(details.open).toBe(false);
  await user.click(summary);
  fireEvent.change(screen.getByLabelText("Reference time (optional)"), {
    target: { value: "2026-10-14T09:00:00+09:00" },
  });
  await user.click(summary);
  expect(details.open).toBe(false);
  expect(summary.textContent).toContain("2026-10-14T09:00:00+09:00");
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "Count stores" } });
  await user.click(screen.getByRole("button", { name: "Generate operation" }));
  await screen.findByText("Saved Tool plan · 2 operations");
  expect(request).toHaveBeenCalledWith(
    "POST",
    "/generate",
    expect.objectContaining({
      reference_time: "2026-10-14T09:00:00+09:00",
    }),
  );
  qc.clear();
});
