// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, expect, test, vi } from "vitest";
import { request } from "@/api/client";
import type { Context, ImportDdlRequest, ImportDdlResult } from "@/api/types";
import { ContextEditor } from "@/pages/ContextEditor";
vi.mock("@/api/client", async (original) => ({
  ...(await original<typeof import("@/api/client")>()),
  request: vi.fn(),
}));
vi.mock("@/components/app/LazySqlEditor", () => ({
  LazySqlEditor: ({ value, onChange }: { value: string; onChange: (text: string) => void }) => (
    <textarea aria-label="DDL text" value={value} onChange={(e) => onChange(e.target.value)} />
  ),
}));
afterEach(() => {
  cleanup();
  vi.mocked(request).mockReset();
});
const context: Context = {
  id: "ctx",
  name: "sample",
  dialect: "postgresql",
  timezone: "UTC",
  allow_generated_sql: true,
  version: 1,
  created_at: "",
  updated_at: "",
  definition: {
    resources: [{ name: "orders", fields: [{ name: "id", type: "bigint" }] }],
    relations: [],
    rules: [],
    metrics: [],
  },
};
const preview: ImportDdlResult = {
  context_version: 1,
  definition: {
    ...context.definition,
    resources: [...context.definition.resources, { name: "imported", fields: [] }],
  },
  warnings: [],
};
function setup(onRouter?: (router: ReturnType<typeof createMemoryRouter>) => void) {
  const client = new QueryClient({
    defaultOptions: { queries: { staleTime: Infinity, retry: false } },
  });
  client.setQueryData(["contexts", "ctx"], context);
  const router = createMemoryRouter([{ path: "*", element: <ContextEditor id="ctx" /> }]);
  onRouter?.(router);
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return client;
}
async function startPreview() {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Import DDL" }));
  fireEvent.change(screen.getByLabelText("DDL text"), {
    target: { value: "CREATE TABLE imported (id int)" },
  });
  await user.click(screen.getByRole("button", { name: "Preview changes" }));
  expect(request).toHaveBeenCalledWith(
    "POST",
    "/contexts/ctx/import-ddl",
    expect.objectContaining({ expected_version: 1 }),
  );
  return user;
}
test.each([false, true])(
  "stale DDL cannot overwrite a newer Context (response pending: %s)",
  async (pending) => {
    let resolve!: (result: ImportDdlResult) => void;
    const response = new Promise<ImportDdlResult>((done) => {
      resolve = done;
    });
    vi.mocked(request).mockReturnValue(response);
    const client = setup();
    await startPreview();
    if (!pending) await act(async () => resolve(preview));
    await act(async () => {
      client.setQueryData(["contexts", "ctx"], {
        ...context,
        version: 2,
        definition: {
          ...context.definition,
          resources: [...context.definition.resources, { name: "remote_new_table", fields: [] }],
        },
      });
    });
    if (pending) await act(async () => resolve(preview));
    const apply = await screen.findByRole("button", { name: "Add 1 table" });
    expect(apply).toHaveProperty("disabled", true);
    expect(screen.getByRole("button", { name: "Save changes" })).toHaveProperty("disabled", true);
    expect(request).toHaveBeenCalledTimes(1);
    client.clear();
  },
);
test("fresh preview saves with its original version; changed DDL invalidates it", async () => {
  vi.mocked(request).mockImplementation(async (method) =>
    method === "POST"
      ? preview
      : {
          context: { ...context, version: 2, definition: preview.definition },
          revalidation: { checked: 0, valid: [], invalid: [] },
        },
  );
  const client = setup();
  const user = await startPreview();
  const apply = await screen.findByRole("button", { name: "Add 1 table" });
  await waitFor(() => expect(apply).toHaveProperty("disabled", false));
  fireEvent.change(screen.getByLabelText("DDL text"), {
    target: { value: "CREATE TABLE other (id int)" },
  });
  expect(apply).toHaveProperty("disabled", true);
  await user.click(screen.getByRole("button", { name: "Preview changes" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Add 1 table" })).toHaveProperty("disabled", false),
  );
  await user.click(screen.getByRole("button", { name: "Add 1 table" }));
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "PUT",
      "/contexts/ctx",
      expect.objectContaining({ expected_version: 1, definition: preview.definition }),
    ),
  );
  client.clear();
});
test.each(["9007199254740993", "1.0000000000000001"])(
  "Metrics cannot save a rounded fixed value (%s)",
  async (value) => {
    const client = setup();
    const user = userEvent.setup();
    await user.click(screen.getByRole("tab", { name: /Metrics/ }));
    const text = `[{"id":"m","description":"m","resource":"orders","aggregate":{"kind":"row_count"},"fixed_filters":[{"field":"orders.id","value":${value}}]}]`;
    fireEvent.change(screen.getByLabelText("Metric contracts (JSON array)"), {
      target: { value: text },
    });
    expect(screen.getByRole("alert").textContent).toContain("cannot preserve exactly");
    expect(screen.getByRole("button", { name: "Save changes" })).toHaveProperty("disabled", true);
    expect(screen.getByLabelText("Metric contracts (JSON array)")).toHaveProperty("value", text);
    expect(request).not.toHaveBeenCalled();
    client.clear();
  },
);

test("sequential imports keep local descriptions and rules, then save once", async () => {
  const bodies: ImportDdlRequest[] = [];
  vi.mocked(request).mockImplementation(async (method, _path, raw) => {
    if (method === "POST") {
      const body = raw as ImportDdlRequest;
      bodies.push(body);
      const name = bodies.length === 1 ? "first" : "second";
      return {
        context_version: 1,
        warnings: [],
        pending_foreign_keys: [],
        definition: {
          ...body.base_definition,
          resources: [...body.base_definition.resources, { name, fields: [] }],
        },
      };
    }
    return {
      context: { ...context, version: 2, ...(raw as object) },
      revalidation: { checked: 0, valid: [], invalid: [] },
    };
  });
  const client = setup();
  const user = userEvent.setup();
  // Unsaved changes from elsewhere must be part of the preview baseline.
  const description = screen.getByPlaceholderText("Resource description");
  await user.type(description, "My local description");
  await startPreview();
  await user.click(await screen.findByRole("button", { name: "Add 1 table" }));
  expect(screen.getByRole("tab", { name: "Schema (2)" })).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "Import another DDL" }));
  fireEvent.change(screen.getByLabelText("DDL text"), {
    target: { value: "CREATE TABLE second (id int)" },
  });
  await user.click(screen.getByRole("button", { name: "Preview changes" }));
  await user.click(await screen.findByRole("button", { name: "Add 1 table" }));
  expect(bodies[1].base_definition.resources.map((r) => r.name)).toEqual(["orders", "first"]);
  expect(bodies[1].base_definition.resources[0].description).toBe("My local description");
  expect(screen.getByRole("tab", { name: "Schema (3)" })).toBeTruthy();
  expect(vi.mocked(request).mock.calls.filter(([method]) => method === "PUT")).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() =>
    expect(vi.mocked(request).mock.calls.filter(([method]) => method === "PUT")).toHaveLength(1),
  );
  expect(vi.mocked(request).mock.calls.at(-1)?.[2]).toEqual(
    expect.objectContaining({
      definition: expect.objectContaining({
        resources: expect.arrayContaining([
          expect.objectContaining({ name: "orders", description: "My local description" }),
          expect.objectContaining({ name: "first" }),
          expect.objectContaining({ name: "second" }),
        ]),
      }),
    }),
  );
  client.clear();
});

test("editing the schema while a preview is pending cannot discard that edit", async () => {
  let resolve!: (result: ImportDdlResult) => void;
  vi.mocked(request).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const client = setup();
  const user = await startPreview();
  await user.type(screen.getByPlaceholderText("Resource description"), "changed during preview");
  await act(async () => resolve(preview));
  expect(await screen.findByRole("button", { name: "Add 1 table" })).toHaveProperty(
    "disabled",
    true,
  );
  client.clear();
});

test("DDL input survives tab changes and navigation requires an explicit discard", async () => {
  let router!: ReturnType<typeof createMemoryRouter>;
  const client = setup((value) => {
    router = value;
  });
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Import DDL" }));
  fireEvent.change(screen.getByLabelText("DDL text"), {
    target: { value: "CREATE TABLE pending (id int)" },
  });
  await user.click(screen.getByRole("tab", { name: /Business rules/ }));
  await user.click(screen.getByRole("tab", { name: /Schema/ }));
  expect(screen.getByLabelText("DDL text")).toHaveProperty(
    "value",
    "CREATE TABLE pending (id int)",
  );
  await act(async () => {
    await router.navigate("/other");
  });
  expect(screen.getByRole("dialog")).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "Keep editing" }));
  expect(router.state.location.pathname).toBe("/");
  expect(screen.getByLabelText("DDL text")).toHaveProperty(
    "value",
    "CREATE TABLE pending (id int)",
  );
  client.clear();
});

test("pending foreign keys block save until explicitly excluded", async () => {
  vi.mocked(request).mockResolvedValue({
    ...preview,
    pending_foreign_keys: [
      { table: "imported", column: "user_id", ref_table: "users", ref_column: "id" },
    ],
  });
  const client = setup();
  const user = await startPreview();
  await user.click(await screen.findByRole("button", { name: "Add 1 table" }));
  expect(screen.getByRole("button", { name: "Save changes" })).toHaveProperty("disabled", true);
  await user.click(screen.getByRole("button", { name: "Exclude relation" }));
  expect(screen.getByRole("button", { name: "Save changes" })).toHaveProperty("disabled", false);
  client.clear();
});

test("replacement requires acknowledgment; failed save retains the applied draft", async () => {
  vi.mocked(request).mockImplementation(async (method) => {
    if (method === "POST")
      return { ...preview, definition: { ...preview.definition, resources: [] } };
    throw new Error("Save failed");
  });
  const client = setup();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Import DDL" }));
  fireEvent.change(screen.getByLabelText("DDL text"), {
    target: { value: "-- intentionally empty schema" },
  });
  await user.click(screen.getByText("Advanced"));
  await user.click(screen.getByRole("button", { name: "Replace entire schema…" }));
  await user.click(screen.getByRole("button", { name: "Preview changes" }));
  const replace = await screen.findByRole("button", { name: "Replace schema" });
  expect(replace).toHaveProperty("disabled", true);
  expect(screen.getByText("Remove tables: orders")).toBeTruthy();
  await user.click(
    screen.getByLabelText("Replace the current tables and relations with this preview."),
  );
  await user.click(replace);
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await screen.findByText("Could not save context");
  expect(screen.getByRole("tab", { name: "Schema (0)" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Save changes" })).toHaveProperty("disabled", false);
  client.clear();
});

test("reimporting equivalent normalized fields is a no-op", async () => {
  vi.mocked(request).mockResolvedValue({
    context_version: 1,
    warnings: [],
    pending_foreign_keys: [],
    definition: {
      ...context.definition,
      resources: [
        {
          name: "orders",
          description: null,
          fields: [
            {
              name: "id",
              type: "bigint",
              description: null,
              nullable: true,
              primary_key: false,
              allowed_values: [],
            },
          ],
        },
      ],
    },
  });
  const client = setup();
  await startPreview();
  await screen.findByText("No changes to apply.");
  expect(screen.getByRole("button", { name: "Apply changes" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "Save changes" })).toHaveProperty("disabled", true);
  client.clear();
});

test("discard clears both imported edits and unapplied DDL", async () => {
  vi.mocked(request).mockResolvedValue(preview);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  const client = setup();
  const user = await startPreview();
  await user.click(await screen.findByRole("button", { name: "Add 1 table" }));
  await user.click(screen.getByRole("button", { name: "Import another DDL" }));
  fireEvent.change(screen.getByLabelText("DDL text"), {
    target: { value: "CREATE TABLE unsaved (id int)" },
  });
  await user.click(screen.getByRole("button", { name: "Discard" }));
  expect(screen.getByRole("tab", { name: "Schema (1)" })).toBeTruthy();
  expect(screen.getByLabelText("DDL text")).toHaveProperty("value", "");
  expect(screen.getByRole("button", { name: "Discard" })).toHaveProperty("disabled", true);
  confirm.mockRestore();
  client.clear();
});
