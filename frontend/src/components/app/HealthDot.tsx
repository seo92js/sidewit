import { cn } from "cn";

import { useHealth } from "@/api/queries";
import type { Health } from "@/api/types";

type Tone = "ok" | "degraded" | "down" | "unknown";

const TONES: Record<Tone, string> = {
  ok: "bg-emerald-500",
  degraded: "bg-amber-500",
  down: "bg-red-500",
  unknown: "bg-muted-foreground/40",
};

/** Error first: a failed refetch keeps the previous report in `data`. */
function healthIndicator({ data, isError }: { data: Health | undefined; isError: boolean }): {
  tone: Tone;
  label: string;
} {
  if (isError) return { tone: "down", label: "API unreachable" };
  if (!data) return { tone: "unknown", label: "Checking API…" };
  const label = `API ${data.status} · storage ${data.storage}`;
  return { tone: data.status === "ok" ? "ok" : "degraded", label };
}

export function HealthDot() {
  const { tone, label } = healthIndicator(useHealth());
  return (
    <span className="flex items-center gap-1.5 text-xs text-muted-foreground" title={label}>
      <span aria-hidden className={cn("size-2 rounded-full", TONES[tone])} />
      API
      <span className="sr-only">: {label}</span>
    </span>
  );
}
