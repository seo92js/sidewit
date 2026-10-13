import { execFileSync } from "node:child_process";
import { expect, test } from "vitest";
import { generateRequestCurl } from "@/lib/apiRequest";

test("curl preserves request fields and treats shell syntax as literal text", () => {
  const request = {
    context_id: "c",
    question: "서울 O'Reilly $(printf injected) `printf injected`\n매장 \\\"개수",
    reference_time: "2026-10-13T09:00:00+09:00",
    metric_id: "count",
    date_field: "Store.createdAt",
    require_metric: true,
  };
  for (const authenticated of [false, true]) {
    const command = generateRequestCurl(request, "https://sidewit.example:8443", authenticated);
    const output = execFileSync("/bin/sh", ["-c", 'curl() { printf "%s\\0" "$@"; };\n' + command], {
      env: { SIDEWIT_API_KEY: "synthetic-key" },
      encoding: "utf8",
    }).split("\0");
    expect(output[0]).toBe("https://sidewit.example:8443/api/v1/generate");
    expect(JSON.parse(output[output.indexOf("--data-raw") + 1])).toEqual(request);
    expect(output.includes("Authorization: Bearer synthetic-key")).toBe(authenticated);
    expect(command).not.toContain("synthetic-key");
  }
});
