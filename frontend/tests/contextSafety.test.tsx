// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, expect, test, vi } from "vitest";
import { request } from "@/api/client";
import type { Context, ImportDdlResult } from "@/api/types";
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
function setup() {
  const client = new QueryClient({
    defaultOptions: { queries: { staleTime: Infinity, retry: false } },
  });
  client.setQueryData(["contexts", "ctx"], context);
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ContextEditor id="ctx" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return client;
}
async function startPreview() {
  const user = userEvent.setup();
  await user.click(screen.getByRole("tab", { name: /Import DDL/ }));
  fireEvent.change(screen.getByLabelText("DDL text"), {
    target: { value: "CREATE TABLE imported (id int)" },
  });
  await user.click(screen.getByRole("button", { name: "Preview import" }));
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
    const apply = await screen.findByRole("button", { name: "Apply to draft" });
    expect(apply).toHaveProperty("disabled", true);
    expect(screen.getByRole("button", { name: "Save as v3" })).toHaveProperty("disabled", true);
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
  const apply = await screen.findByRole("button", { name: "Apply to draft" });
  await waitFor(() => expect(apply).toHaveProperty("disabled", false));
  fireEvent.change(screen.getByLabelText("DDL text"), {
    target: { value: "CREATE TABLE other (id int)" },
  });
  expect(apply).toHaveProperty("disabled", true);
  await user.click(screen.getByRole("button", { name: "Preview import" }));
  await waitFor(() => expect(apply).toHaveProperty("disabled", false));
  await user.click(apply);
  await user.click(screen.getByRole("button", { name: "Save as v2" }));
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
    expect(screen.getByRole("button", { name: "Save as v2" })).toHaveProperty("disabled", true);
    expect(screen.getByLabelText("Metric contracts (JSON array)")).toHaveProperty("value", text);
    expect(request).not.toHaveBeenCalled();
    client.clear();
  },
);
