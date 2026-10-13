// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { CopyButton } from "@/components/app/common";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("successful clipboard write reports success without opening a dialog", async () => {
  const user = userEvent.setup();
  const write = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
  render(<CopyButton text="SELECT 1" label="Copy SQL" />);
  await user.click(screen.getByRole("button", { name: "Copy SQL" }));
  expect(write).toHaveBeenCalledWith("SELECT 1");
  expect(screen.getByRole("button", { name: "Copied" })).toBeTruthy();
  expect(screen.queryByRole("dialog")).toBeNull();
});

test.each(["denied", "unavailable"])(
  "%s clipboard offers selectable text and can be dismissed",
  async (mode) => {
    const user = userEvent.setup();
    if (mode === "denied")
      vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("Denied"));
    else vi.stubGlobal("navigator", { clipboard: undefined });
    render(<CopyButton text="SELECT '서울'" label="Copy SQL" />);
    await user.click(screen.getByRole("button", { name: "Copy SQL" }));
    expect(await screen.findByRole("dialog", { name: "Copy manually" })).toBeTruthy();
    const input = screen.getByRole("textbox", {
      name: "Text to copy: Copy SQL",
    }) as HTMLTextAreaElement;
    expect(input.value).toBe("SELECT '서울'");
    expect(input.selectionEnd - input.selectionStart).toBe(input.value.length);
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Copy SQL" }));
  },
);
