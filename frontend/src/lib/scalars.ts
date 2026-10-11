import type { ContextDefinition, ContextField, ParamType, ScalarValue } from "@/api/types";

const INTEGER_TYPES = new Set([
  "int",
  "integer",
  "smallint",
  "bigint",
  "int2",
  "int4",
  "int8",
  "serial",
  "bigserial",
]);
const NUMBER_TYPES = new Set([
  "numeric",
  "decimal",
  "real",
  "float",
  "float4",
  "float8",
  "double precision",
]);

export function fieldInputType(fieldType: string): ParamType {
  const type = fieldType
    .toLowerCase()
    .replace(/\(.*?\)/g, "")
    .trim();
  if (INTEGER_TYPES.has(type)) return "integer";
  if (NUMBER_TYPES.has(type)) return "number";
  if (type === "bool" || type === "boolean") return "boolean";
  return "string";
}

export function scalarValueError(value: ScalarValue, type: ParamType): string | null {
  if (type === "integer" || type === "number") {
    if (typeof value !== "number" || !Number.isFinite(value))
      return "Enter a finite decimal number.";
    if (type === "integer" && !Number.isInteger(value)) return "Enter a whole number.";
    if (Number.isInteger(value) && !Number.isSafeInteger(value))
      return "This integer exceeds the browser's exact range (±9007199254740991). Use the API with an exact integer.";
  } else if (type === "boolean") {
    if (typeof value !== "boolean") return "Enter true or false.";
  } else if (typeof value !== "string") return "Enter a text value.";
  return null;
}

const DECIMAL = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;

/** Canonical form of a decimal literal, so "1.50", "15e-1" and "1.5" compare equal. */
function decimalKey(text: string): string {
  const match = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(text);
  if (!match) return text;
  const [, sign, whole, fraction = "", exponent = "0"] = match;
  let digits = (whole + fraction).replace(/^0+/, "");
  let scale = Number(exponent) - fraction.length;
  if (!digits) return "0";
  const trailing = digits.length - digits.replace(/0+$/, "").length;
  digits = digits.slice(0, digits.length - trailing);
  scale += trailing;
  return `${sign === "-" ? "-" : ""}${digits}e${scale}`;
}

export function parseScalarInput(
  text: string,
  type: ParamType,
): { value: ScalarValue; error: string | null } {
  let value: ScalarValue = text;
  if (type === "integer" || type === "number") {
    if (DECIMAL.test(text.trim())) value = Number(text);
  } else if (type === "boolean") {
    value = text === "true" ? true : text === "false" ? false : text;
  }
  let error = scalarValueError(value, type);
  // Number() rounds silently ("1.0000000000000001" → 1); the value must equal the text.
  if (!error && typeof value === "number" && decimalKey(text.trim()) !== decimalKey(String(value)))
    error = "This number cannot be represented exactly; use fewer digits.";
  // Keep invalid text intact for correction. Never commit a rounded integer to editor state.
  return { value: error ? text : value, error };
}

/**
 * Parses `text` only if printing the result gives the same text back, so "0.", "1e5" or "001"
 * stay text. Used while the user is typing and for coercion that must never change a value.
 */
export function parseScalarExact(text: string, type: ParamType): ScalarValue {
  const { value, error } = parseScalarInput(text, type);
  return !error && String(value) === text ? value : text;
}

/** Converts a value to `type` when that is lossless ("1" → 1, 1 → "1"); otherwise keeps it. */
export function coerceScalar(value: ScalarValue, type: ParamType): ScalarValue {
  const coerced = parseScalarExact(String(value), type);
  return scalarValueError(coerced, type) ? value : coerced;
}

/**
 * Changes a field's type and re-types its allowed values and rule constants where lossless,
 * so values entered for the old type do not block Save.
 */
export function setFieldType(
  definition: ContextDefinition,
  resourceIndex: number,
  fieldIndex: number,
  fieldType: string,
): ContextDefinition {
  const resource = definition.resources[resourceIndex];
  const field = resource.fields[fieldIndex];
  const type = fieldInputType(fieldType);
  const retype = type !== fieldInputType(field.type);
  const ref = `${resource.name}.${field.name}`;
  const retyped: ContextField = { ...field, type: fieldType };
  if (retype && field.allowed_values)
    retyped.allowed_values = field.allowed_values.map((v) => coerceScalar(v, type));
  const fields = resource.fields.map((f, i) => (i === fieldIndex ? retyped : f));
  return {
    ...definition,
    resources: definition.resources.map((r, i) => (i === resourceIndex ? { ...r, fields } : r)),
    rules: !retype
      ? definition.rules
      : definition.rules.map((rule) =>
          rule.condition?.field === ref
            ? {
                ...rule,
                condition: { ...rule.condition, value: coerceScalar(rule.condition.value, type) },
              }
            : rule,
        ),
  };
}

/** Like scalarValueError, but says why text left in a typed field did not convert. */
function storedValueError(value: ScalarValue, type: ParamType): string | null {
  const parsed =
    typeof value === "string" && type !== "string" ? parseScalarInput(value, type) : null;
  return parsed?.error ?? scalarValueError(value, type);
}

/** Used before Context save, including values loaded from JSON rather than typed in the UI. */
export function contextValueErrors(definition: ContextDefinition): string[] {
  const errors: string[] = [];
  for (const resource of definition.resources) {
    for (const field of resource.fields) {
      for (const value of field.allowed_values ?? []) {
        const error = storedValueError(value, fieldInputType(field.type));
        if (error) errors.push(`${resource.name}.${field.name}: ${error}`);
      }
    }
  }
  for (const rule of definition.rules) {
    if (!rule.condition) continue;
    const [resource, name] = rule.condition.field.split(".");
    const field = definition.resources
      .find((r) => r.name === resource)
      ?.fields.find((f) => f.name === name);
    if (field) {
      const error = storedValueError(rule.condition.value, fieldInputType(field.type));
      if (error) errors.push(`Rule ${rule.id}: ${error}`);
    }
  }
  return [...new Set(errors)];
}
