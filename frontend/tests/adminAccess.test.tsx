// @vitest-environment jsdom
import { StrictMode, useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider, Routes, Route } from "react-router";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError, adminSession, SESSION_EXPIRED_EVENT, type AdminSession } from "@/api/client";
import { AdminAccess, LogoutPage, useApiKeyRequired } from "@/components/app/AdminAccess";
import { UnsavedChangesGuard } from "@/components/app/UnsavedChangesGuard";

const mockSession = vi.hoisted(() =>
  vi.fn<
    (
      method: "GET" | "POST" | "DELETE",
      body?: { username: string; password: string },
    ) => Promise<AdminSession | void>
  >(),
);

vi.mock("@/api/client", async (original) => ({
  ...(await original<typeof import("@/api/client")>()),
  adminSession: mockSession,
}));
afterEach(() => {
  cleanup();
  mockSession.mockReset();
  localStorage.clear();
});
const user = { username: "admin", api_key_required: true };
const unauthorized = new ApiError(401, {
  error: "admin_session_required",
  message: "Sign in",
  details: {},
});

function Editor() {
  const [note, setNote] = useState("");
  const keyRequired = useApiKeyRequired();
  return (
    <>
      <UnsavedChangesGuard dirty={!!note} />
      <label>
        Draft
        <input aria-label="Draft" value={note} onChange={(event) => setNote(event.target.value)} />
      </label>
      <p>API key required: {String(keyRequired)}</p>
    </>
  );
}
function setup(initial = "/editor") {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const router = createMemoryRouter(
    [
      {
        path: "*",
        element: (
          <AdminAccess>
            <Routes>
              <Route path="/editor" element={<Editor />} />
              <Route path="/playground" element={<Editor />} />
              <Route path="/logout" element={<LogoutPage />} />
            </Routes>
          </AdminAccess>
        ),
      },
    ],
    { initialEntries: [initial] },
  );
  render(
    <StrictMode>
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </StrictMode>,
  );
  return { qc, router };
}
function requiresLogin() {
  mockSession.mockImplementation(async (method) => {
    if (method === "GET") throw unauthorized;
    if (method === "POST") return user;
  });
}
async function signIn() {
  await screen.findByLabelText("Password");
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "admin" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  await screen.findByRole("textbox", { name: "Draft" });
}

test("default login replaces the API-key dialog and does not force a password change", async () => {
  requiresLogin();
  localStorage.setItem("sidewit.apiKey", "obsolete-key");
  const { qc } = setup();
  await screen.findByRole("button", { name: "Sign in" });
  expect(screen.queryByLabelText("Draft")).toBeNull();
  expect(localStorage.getItem("sidewit.apiKey")).toBeNull();
  await signIn();
  expect(adminSession).toHaveBeenCalledWith("POST", { username: "admin", password: "admin" });
  expect(screen.getByText("API key required: true")).toBeTruthy();
  expect(screen.queryByLabelText("Password")).toBeNull();
  expect(localStorage.length).toBe(0);
  qc.clear();
});

test("incorrect credentials stay on the login form with the server error", async () => {
  requiresLogin();
  mockSession.mockImplementation(async (method) => {
    if (method === "GET") throw unauthorized;
    throw new ApiError(401, {
      error: "invalid_admin_credentials",
      message: "Invalid username or password.",
      details: {},
    });
  });
  const { qc } = setup();
  await screen.findByLabelText("Password");
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "wrong" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  await screen.findByText("Invalid username or password.");
  expect(screen.queryByLabelText("Draft")).toBeNull();
  qc.clear();
});

test("session expiry preserves unsaved edits while the console is hidden", async () => {
  requiresLogin();
  const { qc, router } = setup();
  await signIn();
  fireEvent.change(screen.getByLabelText("Draft"), { target: { value: "My unsaved context" } });
  await act(async () => {
    window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
  });
  await screen.findByLabelText("Password");
  expect(screen.getByLabelText("Draft").closest("[hidden]")).not.toBeNull();
  expect(router.state.location.pathname).toBe("/editor");
  await signIn();
  expect(screen.getByRole("textbox", { name: "Draft" })).toHaveProperty(
    "value",
    "My unsaved context",
  );
  expect(screen.getByLabelText("Draft").closest("[hidden]")).toBeNull();
  qc.clear();
});

test("session expiry suspends open portal dialogs so they cannot block sign-in", async () => {
  requiresLogin();
  const { qc, router } = setup();
  await signIn();
  fireEvent.change(screen.getByLabelText("Draft"), { target: { value: "Unsaved" } });
  await act(async () => {
    await router.navigate("/logout");
  });
  await screen.findByRole("dialog");
  await act(async () => {
    window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
  });
  await screen.findByLabelText("Password");
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(document.body.style.pointerEvents).not.toBe("none");
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "admin" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  await screen.findByRole("dialog");
  fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
  expect(screen.getByRole("textbox", { name: "Draft" })).toHaveProperty("value", "Unsaved");
  expect(router.state.location.pathname).toBe("/editor");
  qc.clear();
});

test("signing out respects the unsaved-navigation guard and clears cached data", async () => {
  requiresLogin();
  const { qc, router } = setup();
  await signIn();
  qc.setQueryData(["contexts"], ["private context"]);
  fireEvent.change(screen.getByLabelText("Draft"), { target: { value: "Unsaved" } });
  await act(async () => {
    await router.navigate("/logout");
  });
  expect(await screen.findByRole("dialog")).toBeTruthy();
  expect(mockSession.mock.calls.some(([method]) => method === "DELETE")).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Discard and leave" }));
  await screen.findByRole("button", { name: "Sign in" });
  expect(adminSession).toHaveBeenCalledWith("DELETE");
  expect(qc.getQueryData(["contexts"])).toBeUndefined();
  expect(screen.queryByLabelText("Draft")).toBeNull();
  expect(router.state.location.pathname).toBe("/playground");
  const logoutCalls = vi
    .mocked(adminSession)
    .mock.calls.filter(([method]) => method === "DELETE").length;
  await signIn();
  expect(screen.getByLabelText("Draft")).toHaveProperty("value", "");
  expect(mockSession.mock.calls.filter(([method]) => method === "DELETE")).toHaveLength(
    logoutCalls,
  );
  qc.clear();
});

test("failed logout retains access and provides an explicit retry", async () => {
  requiresLogin();
  mockSession.mockImplementation(async (method) => {
    if (method === "GET") return user;
    if (method === "DELETE") throw new Error("Server unavailable");
    return user;
  });
  const { qc, router } = setup();
  await screen.findByLabelText("Draft");
  await act(async () => {
    await router.navigate("/logout");
  });
  await screen.findByText("Could not sign out");
  expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
  mockSession.mockImplementation(async (method) => {
    if (method !== "DELETE") return user;
  });
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  await screen.findByRole("button", { name: "Sign in" });
  expect(router.state.location.pathname).toBe("/playground");
  expect(mockSession.mock.calls.filter(([method]) => method === "DELETE")).toHaveLength(2);
  qc.clear();
});

test("session-check network failures offer a retry without revealing console data", async () => {
  mockSession.mockRejectedValue(new Error("Server unavailable"));
  const { qc } = setup();
  await screen.findByText("Could not check your session");
  expect(screen.queryByLabelText("Draft")).toBeNull();
  mockSession.mockResolvedValue(user);
  fireEvent.click(screen.getByRole("button", { name: "Retry connection" }));
  await screen.findByLabelText("Draft");
  qc.clear();
});

test.each(["/logout", "/logout/", "/LOGOUT"])(
  "sign-in from %s opens Playground without revoking the new session",
  async (path) => {
    requiresLogin();
    const { qc, router } = setup(path);
    await screen.findByLabelText("Password");
    expect(router.state.location.pathname).toBe("/playground");
    await signIn();
    expect(mockSession.mock.calls.filter(([method]) => method === "POST")).toHaveLength(1);
    expect(mockSession.mock.calls.filter(([method]) => method === "DELETE")).toHaveLength(0);
    qc.clear();
  },
);

test("an authenticated visit to the logout URL still revokes the existing session", async () => {
  mockSession.mockImplementation(async (method) => {
    if (method === "GET") return user;
  });
  const { qc, router } = setup("/logout");
  await screen.findByRole("button", { name: "Sign in" });
  expect(router.state.location.pathname).toBe("/playground");
  expect(mockSession.mock.calls.filter(([method]) => method === "DELETE")).toHaveLength(1);
  expect(mockSession.mock.calls.filter(([method]) => method === "POST")).toHaveLength(0);
  qc.clear();
});

test("a pending logout blocks navigation until it completes and revokes only once", async () => {
  let finishLogout!: () => void;
  mockSession.mockImplementation(async (method) => {
    if (method === "GET" || method === "POST") return user;
    return new Promise<void>((resolve) => {
      finishLogout = resolve;
    });
  });
  const { qc, router } = setup();
  await screen.findByLabelText("Draft");
  await act(async () => {
    await router.navigate("/logout");
  });
  await screen.findByRole("status");
  expect(mockSession.mock.calls.filter(([method]) => method === "DELETE")).toHaveLength(1);
  await act(async () => {
    window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
  });
  expect(screen.queryByLabelText("Password")).toBeNull();
  expect(screen.getByRole("status").textContent).toBe("Signing out…");
  await act(async () => {
    await router.navigate("/editor");
  });
  expect(await screen.findByText("Request in progress")).toBeTruthy();
  expect(router.state.location.pathname).toBe("/logout");
  expect(screen.queryByLabelText("Draft")).toBeNull();
  await act(async () => {
    finishLogout();
  });
  await screen.findByRole("button", { name: "Sign in" });
  expect(router.state.location.pathname).toBe("/playground");
  expect(screen.queryByRole("dialog")).toBeNull();
  await signIn();
  expect(screen.getByLabelText("Draft")).toHaveProperty("value", "");
  expect(mockSession.mock.calls.filter(([method]) => method === "DELETE")).toHaveLength(1);
  qc.clear();
});
