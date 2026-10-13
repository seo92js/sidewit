import type { GenerateRequest } from "@/api/types";

// POSIX shell quoting also protects JSON containing apostrophes or shell syntax.
function shellQuote(value: string): string {
  return "'" + value.replaceAll("'", "'\"'\"'") + "'";
}

export function generateRequestCurl(
  request: GenerateRequest,
  origin: string,
  authenticated: boolean,
): string {
  return [
    `curl ${shellQuote(new URL("/api/v1/generate", origin).href)}`,
    `  -H 'Content-Type: application/json'`,
    ...(authenticated
      ? ['  -H "Authorization: Bearer ${SIDEWIT_API_KEY:?Set SIDEWIT_API_KEY in your shell}"']
      : []),
    `  --data-raw ${shellQuote(JSON.stringify(request, null, 2))}`,
  ].join(" \\\n");
}
