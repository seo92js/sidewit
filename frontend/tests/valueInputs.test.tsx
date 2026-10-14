// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, test, vi } from "vitest";

import type { ScalarValue, Tool } from "@/api/types";
import { ScalarInput } from "@/components/app/ScalarInput";
import { ValueListInput } from "@/components/app/ValueListInput";
import { InvokePanel } from "@/pages/tool/InvokePanel";

afterEach(cleanup);

describe("ScalarInput", () => {
  test("focusing and leaving never changes a value", () => {
    for (const [value, type] of [
      ["X\rY", "string"],
      ["A\nB", "string"],
      [" padded ", "string"],
      ["1.0000000000000001", "integer"],
      [5, "integer"],
    ] as const) {
      const onChange = vi.fn();
      render(<ScalarInput aria-label="v" value={value} type={type} onChange={onChange} />);
      const input = screen.getByLabelText("v");
      fireEvent.focus(input);
      fireEvent.blur(input);
      expect(onChange).not.toHaveBeenCalled();
      cleanup();
    }
  });

  test("a value with a line break is read-only", () => {
    render(<ScalarInput aria-label="v" value={"X\rY"} type="string" onChange={vi.fn()} />);
    expect(screen.getByLabelText("v")).toHaveProperty("readOnly", true);
  });

  test("typing keeps the text; blur converts it exactly", () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <ScalarInput aria-label="v" value={1} type="number" onChange={onChange} />,
    );
    const input = screen.getByLabelText("v");
    fireEvent.change(input, { target: { value: "0." } });
    expect(onChange).toHaveBeenLastCalledWith("0.");
    rerender(<ScalarInput aria-label="v" value="0." type="number" onChange={onChange} />);
    fireEvent.blur(input);
    expect(onChange).toHaveBeenLastCalledWith(0);
  });
});

describe("ValueListInput", () => {
  test("empty strings and line breaks survive focus, blur and unrelated edits", () => {
    let values: ScalarValue[] = ["", "A\nB", "C"];
    const onChange = vi.fn((next: ScalarValue[]) => (values = next));
    render(<ValueListInput label="Value" type="string" values={values} onChange={onChange} />);
    for (const input of screen.getAllByRole("textbox")) {
      fireEvent.focus(input);
      fireEvent.blur(input);
    }
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Value 3"), { target: { value: "D" } });
    expect(values).toEqual(["", "A\nB", "D"]);
  });
});

vi.mock("@/api/queries", () => ({
  useInvokeTool: () => ({ mutate: mutateSpy, reset: vi.fn(), isPending: false }),
}));
const mutateSpy = vi.fn();

function tool(enumValues: string[]): Tool {
  return {
    id: "t",
    context_id: "c",
    name: "t",
    description: "t",
    input_schema: {
      type: "object",
      properties: { s: { type: "string", enum: enumValues } },
      required: ["s"],
      additionalProperties: false,
    },
    operation: { type: "sql", statement: "SELECT 1 WHERE :s = :s" },
    version: 1,
    context_version: 1,
    status: "valid",
    enabled: true,
    validation_errors: [],
    validation: null,
    source_generation_id: null,
    created_at: "",
    updated_at: "",
  };
}

function renderPanel(t: Tool) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <InvokePanel tool={t} dirty={false} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("InvokePanel", () => {
  test("the empty string is a selectable enum value, distinct from not chosen", () => {
    mutateSpy.mockClear();
    renderPanel(tool(["", "A"]));
    fireEvent.click(screen.getByRole("button", { name: /Invoke Tool/ }));
    expect(screen.getByRole("alert").textContent).toBe("This input is required.");
    expect(mutateSpy).not.toHaveBeenCalled();

    const select = screen.getByRole("combobox");
    fireEvent.change(select, { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: /Invoke Tool/ }));
    expect(mutateSpy).toHaveBeenCalledWith({ s: "" });
  });
});
