import { expect, test } from "vitest";
import { JsonPrecisionError, parseExactJson } from "@/lib/json";

test.each(["9007199254740993", "-9007199254740993", "1.0000000000000001", "1e309", "1e-400"])(
  "rejects inexact numeric JSON source %s",
  (number) => {
    expect(() => parseExactJson(`{"nested":[{"value":${number}}]}`)).toThrow(JsonPrecisionError);
  },
);
test("preserves supported numbers, numeric-looking strings and escaped quotes", () => {
  const text = JSON.stringify({
    "9007199254740993": '"9007199254740993"',
    values: [0.1, 1.5, 9007199254740991],
    escaped: "\\",
    n: null,
    b: true,
  });
  expect(parseExactJson(text)).toEqual(JSON.parse(text));
  expect(parseExactJson("[1.50,15e-1,-0]")).toEqual([1.5, 1.5, -0]);
});
test("syntax errors stay syntax errors", () => {
  expect(() => parseExactJson('{"value":01}')).toThrow(SyntaxError);
});
