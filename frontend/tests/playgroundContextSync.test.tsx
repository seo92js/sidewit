// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError, request } from "@/api/client";
import type { Context, GenerateRequest, GenerateResponse } from "@/api/types";
import PlaygroundPage from "@/pages/PlaygroundPage";

vi.mock("@/api/client", async (original) => ({
  ...(await original<typeof import("@/api/client")>()),
  request: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.mocked(request).mockReset();
  localStorage.clear();
});

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

/** A server whose Context can be saved "elsewhere" while the Playground is open. */
function server() {
  const state = { version: 1 };
  const generate: GenerateRequest[] = [];
  vi.mocked(request).mockImplementation(async (method, path, raw) => {
    if (method === "GET" && path === "/contexts") {
      return { items: [{ ...context, version: state.version }] };
    }
    if (method === "POST" && path === "/generate") {
      const body = raw as GenerateRequest;
      generate.push(body);
      if (body.expected_context_version && body.expected_context_version !== state.version) {
        throw new ApiError(409, {
          error: "context_changed",
          message: "Context changed. Start a new request.",
          details: {},
        });
      }
      return {
        status: "ambiguous",
        generation_id: `generation-${generate.length}`,
        context_version: state.version,
        reason: "Which region?",
        reference_time: "2026-10-13T00:00:00Z",
        clarification_questions: [{ id: "region", question: "Region?", options: [] }],
        continuation_request: {
          context_id: "c",
          question: body.question,
          expected_context_version: state.version,
        },
      } satisfies GenerateResponse;
    }
    throw new Error(`Unexpected request ${method} ${path}`);
  });
  return { state, generate };
}

function renderPlayground() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <PlaygroundPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return qc;
}

async function ask(question: string) {
  fireEvent.change(await screen.findByLabelText("Question"), { target: { value: question } });
  fireEvent.click(screen.getByRole("button", { name: "Generate operation" }));
  return screen.findByLabelText("Region?");
}

async function expectClarificationUsable() {
  await waitFor(() =>
    expect(screen.getByLabelText("Region?").closest("fieldset")).toHaveProperty("disabled", false),
  );
  expect(screen.queryAllByText(/Inputs or Context changed/)).toHaveLength(0);
}

test("a result from a Context version saved elsewhere refreshes the cached Context", async () => {
  const { state, generate } = server();
  renderPlayground();
  await screen.findByRole("option", { name: "commerce (v1)" });
  state.version = 2; // Saved in another tab after the Playground loaded.

  await ask("revenue");
  await expectClarificationUsable();
  expect(screen.getByRole("option", { name: "commerce (v2)" })).toBeTruthy();

  fireEvent.change(screen.getByLabelText("Region?"), { target: { value: "Seoul" } });
  fireEvent.click(screen.getByRole("button", { name: "Continue with answers" }));
  await waitFor(() => expect(generate).toHaveLength(2));
  expect(generate[1]).toMatchObject({
    expected_context_version: 2,
    clarification_answers: [{ id: "region", answer: "Seoul" }],
  });
});

test("context_changed refreshes the cached Context so a new request can continue", async () => {
  const { state, generate } = server();
  renderPlayground();
  await ask("revenue");
  await expectClarificationUsable();

  state.version = 2;
  fireEvent.change(screen.getByLabelText("Region?"), { target: { value: "Seoul" } });
  fireEvent.click(screen.getByRole("button", { name: "Continue with answers" }));
  // The continuation pinned to v1 is rejected; the cache must learn about v2.
  await screen.findByRole("option", { name: "commerce (v2)" });
  expect(screen.getByText(/Inputs or Context changed\. Generate a new request/)).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Generate operation" }));
  await waitFor(() => expect(generate).toHaveLength(3));
  await expectClarificationUsable();
  fireEvent.change(screen.getByLabelText("Region?"), { target: { value: "Busan" } });
  fireEvent.click(screen.getByRole("button", { name: "Continue with answers" }));
  await waitFor(() => expect(generate).toHaveLength(4));
  expect(generate[3]).toMatchObject({ expected_context_version: 2 });
});

test("a failed background refresh keeps results and clarification answers", async () => {
  server();
  const qc = renderPlayground();
  await ask("revenue");
  fireEvent.change(screen.getByLabelText("Region?"), { target: { value: "half typed" } });

  vi.mocked(request).mockRejectedValue(
    new ApiError(0, { error: "network_error", message: "Could not reach the API.", details: {} }),
  );
  await act(() => qc.refetchQueries({ queryKey: ["contexts"] }));

  expect(await screen.findByText(/Could not refresh contexts/)).toBeTruthy();
  expect(screen.getByLabelText("Region?")).toHaveProperty("value", "half typed");
  expect(screen.getByLabelText("Question")).toHaveProperty("value", "revenue");
});

test("contexts that never loaded still replace the page with the error", async () => {
  vi.mocked(request).mockRejectedValue(
    new ApiError(0, { error: "network_error", message: "Could not reach the API.", details: {} }),
  );
  renderPlayground();
  expect(await screen.findByText("Could not load contexts")).toBeTruthy();
  expect(screen.queryByLabelText("Question")).toBeNull();
});
