import { RefreshCw } from "lucide-react";
import { useRefreshContract } from "@/api/queries";
import type { Tool } from "@/api/types";
import { ErrorAlert, IssueList, ValidationPanel } from "@/components/app/common";
import { ToolStatusBadge } from "@/components/app/ToolStatusBadge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

export function SavedToolStatus({
  tool,
  currentContextVersion,
  dirty,
  onRebuilt,
}: {
  tool: Tool;
  currentContextVersion?: number;
  /** Rebuilding replaces the form, so it waits until unsaved edits are saved or discarded. */
  dirty: boolean;
  onRebuilt: (tool: Tool) => void;
}) {
  const refresh = useRefreshContract(tool.name);
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <ToolStatusBadge status={tool.status} />
        <Badge variant="secondary" className="font-mono">
          v{tool.version}
        </Badge>
        <span className="text-xs text-muted-foreground">
          {tool.status === "valid" ? "validated against" : "last valid against"} context v
          {tool.context_version}
        </span>
        {tool.metric_binding && (
          <span className="ml-auto flex items-center gap-2">
            {dirty && (
              <span className="text-xs text-muted-foreground">
                Save or discard your edits first
              </span>
            )}
            <Button
              variant="outline"
              size="xs"
              disabled={dirty || refresh.isPending}
              onClick={() => refresh.mutate(tool.version, { onSuccess: onRebuilt })}
            >
              <RefreshCw /> Rebuild from contract
            </Button>
          </span>
        )}
      </div>
      {tool.validation && <ValidationPanel result={tool.validation} label="Saved Tool checks" />}
      <ErrorAlert error={refresh.error} title="Could not rebuild from the contract" />
      {tool.status === "invalid" && (
        <Alert variant="destructive">
          <AlertTitle>This tool is invalid and cannot be invoked</AlertTitle>
          <AlertDescription className="space-y-2">
            <p>
              It no longer matches the current context
              {currentContextVersion ? ` (v${currentContextVersion})` : ""}.{" "}
              {tool.metric_binding
                ? "Rebuild it from its metric contract, or fix the context."
                : "Fix the SQL or inputs and save, or fix the context."}
            </p>
            <IssueList issues={tool.validation_errors} />
          </AlertDescription>
        </Alert>
      )}
    </div>
  );
}
