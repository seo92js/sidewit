import type { ToolStatus } from "@/api/types";
import { Badge } from "@/components/ui/badge";

export function ToolStatusBadge({ status }: { status: ToolStatus }) {
  if (status === "valid") return <Badge className="bg-emerald-600 text-white">valid</Badge>;
  if (status === "invalid") return <Badge variant="destructive">invalid</Badge>;
  return <Badge variant="secondary">stale</Badge>;
}
