import type { InputProperty, InputSchema, ParamType } from "@/api/types";

/** Inputs in `required` order: JSONB does not keep `properties` key order. */
export function orderedInputs(schema: InputSchema): string[] {
  const extra = Object.keys(schema.properties).filter((n) => !schema.required.includes(n));
  return [...schema.required.filter((n) => n in schema.properties), ...extra];
}

export function paramType(prop: InputProperty): ParamType {
  return prop.type === "string" && prop.format ? prop.format : prop.type;
}
