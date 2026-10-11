import { describe, expect, test } from "vitest";

import type { ContextDefinition, FixedCondition, ScalarValue } from "@/api/types";
import {
  coerceScalar,
  contextValueErrors,
  fieldInputType,
  parseScalarExact,
  parseScalarInput,
  setFieldType,
} from "@/lib/scalars";

function definitionWith(
  type: string,
  allowed: ScalarValue[] | undefined,
  constant: FixedCondition,
): ContextDefinition {
  return {
    resources: [{ name: "orders", fields: [{ name: "status", type, allowed_values: allowed }] }],
    relations: [],
    rules: [{ id: "rule", text: "", condition: constant }],
  };
}

// An allowed value is parsed one at a time (ValueListInput has one input per value).
const allowed = (text: string, fieldType: string) =>
  parseScalarInput(text, fieldInputType(fieldType)).value;

describe("allowed values", () => {
  test("text and custom enum labels preserve leading zeros and boolean-looking strings", () => {
    for (const type of ["text", "varchar(20)", "order_status"]) {
      for (const text of ["001", "true", "", "Seoul, KR", " padded "]) {
        expect(allowed(text, type)).toBe(text);
      }
    }
  });

  test("numeric and boolean fields convert exact values", () => {
    expect(allowed("-2", "integer")).toBe(-2);
    expect(allowed("1.25", "numeric(8,2)")).toBe(1.25);
    expect(allowed("1e2", "numeric")).toBe(100);
    expect(allowed("false", "boolean")).toBe(false);
  });

  test("invalid, unsafe or inexact numbers stay text for rejection, never rounded", () => {
    for (const text of ["9007199254740993", "1.5", "1e999", "0x10"]) {
      expect(allowed(text, "bigint")).toBe(text);
    }
    expect(allowed("yes", "boolean")).toBe("yes");
  });
});

describe("scalar input", () => {
  test("invoke scalar parsing rejects unsafe integers before a request", () => {
    for (const text of ["9007199254740993", "-9007199254740993", "1e999", "1.5", "0x10"]) {
      const parsed = parseScalarInput(text, "integer");
      expect(parsed.error).toBeTruthy();
      expect(parsed.value).toBe(text);
    }
    expect(parseScalarInput("9007199254740991", "integer")).toEqual({
      value: 9007199254740991,
      error: null,
    });
    expect(parseScalarInput("001", "string")).toEqual({ value: "001", error: null });
  });

  test("numbers that Number() would round are rejected, not rounded", () => {
    for (const [text, type] of [
      ["1.0000000000000001", "integer"],
      ["9007199254740991.1", "integer"],
      ["0.1000000000000000055511151231257827", "number"],
    ] as const) {
      const parsed = parseScalarInput(text, type);
      expect(parsed.error).toBeTruthy();
      expect(parsed.value).toBe(text);
    }
    for (const [text, value] of [
      ["1.50", 1.5],
      ["15e-1", 1.5],
      ["-0", -0],
      ["100000", 100000],
    ] as const) {
      expect(parseScalarInput(text, "number")).toEqual({ value, error: null });
    }
  });

  test("exact parsing keeps partially typed or reformattable numbers as text", () => {
    for (const text of ["0.", "0.0", "1e5", "001", "-", "", " 1"]) {
      expect(parseScalarExact(text, "number")).toBe(text);
    }
    expect(parseScalarExact("0.05", "number")).toBe(0.05);
    expect(parseScalarExact("-3", "integer")).toBe(-3);
    expect(parseScalarExact("true", "boolean")).toBe(true);
    expect(parseScalarExact("1.5", "integer")).toBe("1.5");
  });
});

describe("type coercion", () => {
  test("coerces only when lossless", () => {
    expect(coerceScalar("1", "integer")).toBe(1);
    expect(coerceScalar(1, "string")).toBe("1");
    expect(coerceScalar(true, "string")).toBe("true");
    expect(coerceScalar("false", "boolean")).toBe(false);
    expect(coerceScalar("001", "integer")).toBe("001");
    expect(coerceScalar("1.5", "integer")).toBe("1.5");
    expect(coerceScalar(1.5, "integer")).toBe(1.5);
    expect(coerceScalar(1, "boolean")).toBe(1);
    expect(coerceScalar("Seoul, KR", "number")).toBe("Seoul, KR");
  });

  test("changing a field type re-types its allowed values and rule constants", () => {
    const before = definitionWith("text", ["1", "2", "x"], { field: "orders.status", value: "2" });
    const after = setFieldType(before, 0, 0, "integer");
    expect(after.resources[0].fields[0]).toEqual({
      name: "status",
      type: "integer",
      allowed_values: [1, 2, "x"],
    });
    expect(after.rules[0].condition?.value).toBe(2);
    expect(contextValueErrors(after)).toHaveLength(1); // only "x" is left for the user

    const back = setFieldType(after, 0, 0, "text");
    expect(back.resources[0].fields[0].allowed_values).toEqual(["1", "2", "x"]);
    expect(back.rules[0].condition?.value).toBe("2");
    expect(contextValueErrors(back)).toEqual([]);
  });

  test("a type change within the same input type leaves values and rules untouched", () => {
    const before = definitionWith("int4", [1], { field: "orders.status", value: 1 });
    const after = setFieldType(before, 0, 0, "bigint");
    expect(after.resources[0].fields[0].allowed_values).toBe(
      before.resources[0].fields[0].allowed_values,
    );
    expect(after.rules).toBe(before.rules);
  });

  test("constants on other fields are not touched", () => {
    const before = definitionWith("text", undefined, { field: "orders.other", value: "1" });
    expect(setFieldType(before, 0, 0, "integer").rules[0].condition?.value).toBe("1");
  });
});

test("Context save rejects unsafe values loaded from JSON as well as editor text", () => {
  const definition = definitionWith("bigint", [Number("9007199254740993")], {
    field: "orders.status",
    value: "9007199254740993",
  });
  expect(contextValueErrors(definition)).toHaveLength(2);
});

describe("context value errors", () => {
  test("an inexact number left as text explains why", () => {
    const definition = definitionWith("integer", ["1.0000000000000001"], {
      field: "orders.status",
      value: 1,
    });
    expect(contextValueErrors(definition)).toEqual([
      "orders.status: This number cannot be represented exactly; use fewer digits.",
    ]);
  });
});
