import type { ComponentProps } from "react";

import type { ParamType, ScalarValue } from "@/api/types";
import { Input } from "@/components/ui/input";
import { parseScalarExact, parseScalarInput } from "@/lib/scalars";

/**
 * The one editor for a single Context value (allowed value, rule constant). It never changes
 * a value the user did not edit:
 * - typing converts only when lossless (parseScalarExact), so the text shown is the text
 *   typed ("0.", "1e5" stay as typed until blur);
 * - blur normalizes an edited value (parseScalarInput), never rounding a number;
 * - a value with CR or LF, which an <input> would silently drop, is shown read-only.
 */
export function ScalarInput({
  value,
  type,
  onChange,
  ...props
}: {
  value: ScalarValue;
  type: ParamType;
  onChange: (value: ScalarValue) => void;
} & Omit<ComponentProps<typeof Input>, "value" | "type" | "onChange" | "onBlur">) {
  const text = String(value);
  const lineBreak = /[\r\n]/.test(text);
  const commit = (next: ScalarValue) => {
    if (!Object.is(next, value)) onChange(next);
  };
  return (
    <Input
      {...props}
      value={lineBreak ? text.replace(/\r\n|\r|\n/g, "⏎") : text}
      readOnly={lineBreak || props.readOnly}
      title={lineBreak ? "Contains a line break; edit it through the API." : props.title}
      onChange={(e) => commit(parseScalarExact(e.target.value, type))}
      onBlur={(e) => {
        if (!lineBreak) commit(parseScalarInput(e.target.value, type).value);
      }}
    />
  );
}
