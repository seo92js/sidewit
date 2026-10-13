import { Link } from "react-router";
import type { GeneratePlan } from "@/api/types";
import { CopyButton, ValidationPanel } from "@/components/app/common";
import { OperationView } from "@/components/app/OperationView";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function ToolPlanView({ result }: { result: GeneratePlan }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Saved Tool plan · {result.operations.length} operations
        </CardTitle>
        <p className="font-mono text-xs text-muted-foreground">
          generation {result.generation_id.slice(0, 8)} · context v{result.context_version}
        </p>
        <p className="text-sm text-muted-foreground">
          Every operation passed validation. Review that the selected Tools and arguments answer the
          whole question. These operations are independent; Sidewit has not executed them or
          combined their results. Your application controls execution and presentation.
        </p>
        <CopyButton label="Copy plan JSON" text={JSON.stringify(result, null, 2)} />
      </CardHeader>
      <CardContent className="space-y-5">
        {result.operations.map((step, index) => (
          <section key={step.id} aria-label={step.id} className="space-y-3 rounded-md border p-3">
            <h3 className="text-sm font-medium">
              {index + 1}.{" "}
              <Link
                className="underline"
                to={`/tools/${encodeURIComponent(step.selected_tool.name)}`}
              >
                {step.selected_tool.name}
              </Link>{" "}
              · v{step.selected_tool.version}
            </h3>
            <p className="text-xs text-muted-foreground">{step.id} · saved Tool</p>
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
          </section>
        ))}
      </CardContent>
    </Card>
  );
}
