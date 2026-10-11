import { parseScalarInput } from "./scalars";

export class JsonPrecisionError extends Error {
  constructor() {
    super(
      "This JSON contains a number the browser cannot preserve exactly. Use the API with an exact-number client, or enter a representable value.",
    );
    this.name = "JsonPrecisionError";
  }
}

/** Validate numeric source tokens before exposing JSON.parse's potentially rounded result.
 * JSON.parse checks syntax; this scan skips entire strings, including escaped quotes, so
 * numeric-looking keys/text never become numbers. No reviver-source browser support needed.
 */
export function parseExactJson(text: string): unknown {
  const result: unknown = JSON.parse(text);
  const tokens = /"(?:[^"\\]|\\[\s\S])*"|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/g;
  for (const [token] of text.matchAll(tokens)) {
    if (!token.startsWith('"') && parseScalarInput(token, "number").error)
      throw new JsonPrecisionError();
  }
  return result;
}
