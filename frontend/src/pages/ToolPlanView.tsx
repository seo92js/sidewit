import { Link } from "react-router";
import type { GeneratePlan } from "@/api/types";
import { CopyButton, ValidationPanel } from "@/components/app/common";
import { OperationView } from "@/components/app/OperationView";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

export function ToolPlanView({ result }: { result: GeneratePlan }) {
  const hasDrafts = result.operations.some((step) => step.source !== "tool");
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          {hasDrafts ? "Operation plan" : "Saved Tool plan"} · {result.operations.length} operations
        </CardTitle>
        <p className="font-mono text-xs text-muted-foreground">
          generation {result.generation_id.slice(0, 8)} · context v{result.context_version}
        </p>
        <p className="text-sm text-muted-foreground">
          Review each operation before use. Operations are independent and have not been executed.
        </p>
        <CopyButton label="Copy plan JSON" text={JSON.stringify(result, null, 2)} />
      </CardHeader>
      <CardContent className="space-y-5">
        {result.operations.map((step, index) => (
          <section key={step.id} aria-label={step.id} className="space-y-3 rounded-md border p-3">
            <h3 className="text-sm font-medium">
              {index + 1}.{" "}
              {step.source === "tool" ? (
                <>
                  <Link
                    className="underline"
                    to={`/tools/${encodeURIComponent(step.selected_tool.name)}`}
                  >
                    {step.selected_tool.name}
                  </Link>{" "}
                  · v{step.selected_tool.version}
                </>
              ) : (
                step.task
              )}
            </h3>
            <p className="text-xs text-muted-foreground">
              {step.id} ·{" "}
              {step.source === "tool"
                ? "saved Tool"
                : step.source === "metric"
                  ? "metric contract"
                  : "model-written SQL"}
            </p>
            {step.source === "generated_sql" && (
              <Badge className="bg-amber-500 text-white">New SQL draft · review before use</Badge>
            )}
            {step.metric_binding && (
              <p className="text-xs">Metric contract: {step.metric_binding.metric_id}</p>
            )}
            <OperationView operation={step.operation} />
            <ValidationPanel
              result={step.validation}
              label={
                step.metric_binding
                  ? "SQL and metric contract validated"
                  : "SQL structurally validated"
              }
            />
            {step.source !== "tool" && (
              <Link
                className="inline-block rounded-md border px-3 py-2 text-sm underline"
                to={`/tools/new?generation=${result.generation_id}&step=${step.id}`}
              >
                Create Tool draft from {step.id}
              </Link>
            )}
          </section>
        ))}
        {hasDrafts && (
          <p className="text-xs text-muted-foreground">
            Generation history is temporary. Save new drafts as Tools before this record expires or
            the server restarts.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
