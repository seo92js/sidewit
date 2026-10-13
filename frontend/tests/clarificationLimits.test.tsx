// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ClarificationForm } from "@/pages/ClarificationForm";
import type { GenerateResponse } from "@/api/types";

afterEach(cleanup);
const result: Extract<GenerateResponse, { status: "ambiguous" }> = {
  status: "ambiguous",
  generation_id: "generation",
  context_version: 1,
  reference_time: "2026-10-13T00:00:00Z",
  reason: "Choose a region",

  clarification_questions: [{ id: "new", question: "Region?", options: [] }],
};

test("prior answers can be corrected and keep their IDs", () => {
  const send = vi.fn();
  render(
    <ClarificationForm
      result={result}
      blockedReason={null}
      pending={false}
      previousAnswers={[{ id: "old", question: "Date?", answer: "yesterday" }]}
      onContinue={send}
    />,
  );
  fireEvent.change(screen.getByLabelText("Date?"), { target: { value: "today" } });
  fireEvent.change(screen.getByLabelText("Region?"), { target: { value: "Seoul" } });
  fireEvent.click(screen.getByRole("button", { name: "Continue with answers" }));
  expect(send).toHaveBeenCalledWith([
    { id: "old", question: "Date?", answer: "today" },
    { id: "new", question: "Region?", answer: "Seoul" },
  ]);
});

test("total text limit blocks submission and recovers when shortened", () => {
  const send = vi.fn();
  render(
    <ClarificationForm
      result={result}
      originalQuestion={"a".repeat(15990)}
      blockedReason={null}
      pending={false}
      onContinue={send}
    />,
  );
  fireEvent.change(screen.getByLabelText("Region?"), { target: { value: "long answer" } });
  expect(screen.getByRole("alert").textContent).toContain("16,000");
  const button = screen.getByRole("button", { name: "Continue with answers" });
  expect(button).toHaveProperty("disabled", true);
  fireEvent.click(button);
  expect(send).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("Region?"), { target: { value: "NY" } });
  expect(button).toHaveProperty("disabled", false);
});

test("24-answer cap blocks a further clarification", () => {
  render(
    <ClarificationForm
      result={result}
      blockedReason={null}
      pending={false}
      previousAnswers={Array.from({ length: 24 }, (_, i) => ({
        id: String(i),
        question: `Q${i}`,
        answer: "a",
      }))}
      onContinue={vi.fn()}
    />,
  );
  fireEvent.change(screen.getByLabelText("Region?"), { target: { value: "Seoul" } });
  expect(screen.getByRole("alert").textContent).toContain("24");
  expect(screen.getByRole("button", { name: "Continue with answers" })).toHaveProperty(
    "disabled",
    true,
  );
});
