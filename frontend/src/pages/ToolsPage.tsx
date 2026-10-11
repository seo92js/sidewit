import { Plus } from "lucide-react";
import { Link } from "react-router";

import { useContexts, useTools } from "@/api/queries";
import { Empty, ErrorAlert, Loading } from "@/components/app/common";
import { ToolStatusBadge } from "@/components/app/ToolStatusBadge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export default function ToolsPage() {
  const tools = useTools();
  const contexts = useContexts();
  const contextName = (id: string) =>
    contexts.data?.find((c) => c.id === id)?.name ?? id.slice(0, 8);

  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between gap-2">
        <div>
          <h1 className="text-lg font-semibold">Tools</h1>
          <p className="text-sm text-muted-foreground">
            Validated, reusable operations. Invoking a tool resolves arguments into a SQL operation
            without an LLM — deterministic, and never executed by Sidewit.
          </p>
        </div>
        <Button asChild size="sm">
          <Link to="/tools/new">
            <Plus /> New tool
          </Link>
        </Button>
      </div>
      {tools.isLoading && <Loading />}
      {tools.error && <ErrorAlert error={tools.error} title="Could not load tools" />}
      {tools.data?.length === 0 && (
        <Empty>
          No tools yet. Generate an operation in the{" "}
          <Link className="underline" to="/playground">
            Playground
          </Link>{" "}
          and choose “Create Tool draft”, or create one from scratch.
        </Empty>
      )}
      {!!tools.data?.length && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Context</TableHead>
              <TableHead>Version</TableHead>
              <TableHead>Inputs</TableHead>
              <TableHead>Description</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {tools.data.map((tool) => (
              <TableRow key={tool.id}>
                <TableCell>
                  <Link
                    className="font-mono text-sm underline-offset-2 hover:underline"
                    to={`/tools/${tool.name}`}
                  >
                    {tool.name}
                  </Link>
                  {tool.metric_binding && (
                    <Badge
                      variant="outline"
                      className="ml-2"
                      title={`Built from metric ${tool.metric_binding.metric_id}`}
                    >
                      metric
                    </Badge>
                  )}
                </TableCell>
                <TableCell>
                  <ToolStatusBadge status={tool.status} />
                  {tool.status === "invalid" && (
                    <span className="ml-2 text-xs text-destructive">
                      {tool.validation_errors[0]?.code}
                    </span>
                  )}
                </TableCell>
                <TableCell className="text-sm">
                  {contextName(tool.context_id)}{" "}
                  <span className="font-mono text-xs text-muted-foreground">
                    {tool.status === "valid"
                      ? `v${tool.context_version}`
                      : `last valid v${tool.context_version}`}
                  </span>
                </TableCell>
                <TableCell className="font-mono text-xs">v{tool.version}</TableCell>
                <TableCell className="font-mono text-xs">
                  {tool.input_schema.required.join(", ") || "—"}
                </TableCell>
                <TableCell className="max-w-md truncate text-sm text-muted-foreground">
                  {tool.description}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
