// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { ValidationPanel } from "@/components/app/common";
import { ClarificationForm } from "@/pages/ClarificationForm";
afterEach(cleanup);

test.each([true, false])(
  "validation details collapse without hiding issues (valid=%s)",
  async (valid) => {
    render(
      <ValidationPanel
        label="SQL structurally validated"
        result={{
          valid,
          checks: ["syntax"],
          errors: valid ? [] : [{ code: "bad_sql", message: "Invalid SQL", ref: null }],
          warnings: [{ code: "cost", message: "Review query cost", ref: null }],
        }}
      />,
    );
    const summary = screen.getByText("Validation details and limits");
    expect(summary.closest("details")?.open).toBe(false);
    expect(screen.getByText("Review query cost").closest("details")).toBeNull();
    if (!valid) expect(screen.getByText("Invalid SQL").closest("details")).toBeNull();
    await userEvent.setup().click(summary);
    expect(summary.closest("details")?.open).toBe(true);
    expect(screen.getByText("Checks: syntax")).toBeTruthy();
  },
);
const result = {
  status: "ambiguous" as const,
  generation_id: "g2",
  context_version: 1,
  reason: "Choose a region",
  reference_time: "2026-10-14T00:00:00Z",
  clarification_questions: [{ id: "region", question: "Which region?", options: ["Seoul"] }],
};
const previousAnswers = [{ id: "date", question: "Which date?", answer: "Created" }];

test("previous answers are summarized, editable, and submitted with new answers", async () => {
  const submit = vi.fn();
  render(
    <ClarificationForm
      result={result}
      previousAnswers={previousAnswers}
      pending={false}
      blockedReason={null}
      onContinue={submit}
    />,
  );
  const user = userEvent.setup();
  const previous = screen.getByRole("region", { name: "Previous answers" });
  expect(within(previous).getByText("Created", { selector: "p" })).toBeTruthy();
  const edit = within(previous).getByText("Edit answer");
  expect(edit.closest("details")?.open).toBe(false);
  await user.click(edit);
  const answer = screen.getByLabelText("Which date?");
  await user.clear(answer);
  await user.type(answer, "Updated");
  await user.click(edit);
  await user.click(screen.getByRole("button", { name: "Seoul" }));
  await user.click(screen.getByRole("button", { name: "Continue with answers" }));
  expect(submit).toHaveBeenCalledWith([
    { id: "date", question: "Which date?", answer: "Updated" },
    { id: "region", question: "Which region?", answer: "Seoul" },
  ]);
});

test("collapsed previous answers still count toward the text limit", () => {
  render(
    <ClarificationForm
      result={result}
      originalQuestion={"q".repeat(8000)}
      previousAnswers={Array.from({ length: 4 }, (_, i) => ({
        id: `q${i}`,
        question: "Q".repeat(1000),
        answer: "A".repeat(1000),
      }))}
      pending={false}
      blockedReason={null}
      onContinue={vi.fn()}
    />,
  );
  fireEvent.change(screen.getByRole("textbox", { name: "Which region?" }), {
    target: { value: "Seoul" },
  });
  expect(screen.getByRole("alert").textContent).toContain("16,000");
  expect(screen.getByRole("button", { name: "Continue with answers" })).toHaveProperty(
    "disabled",
    true,
  );
});
