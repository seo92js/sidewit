import type { SqlOperation } from "@/api/types";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

import { CopyButton } from "./common";
import { LazySqlEditor } from "./LazySqlEditor";

/** A resolved SQL Operation. Sidewit never executes it; the application does. */
export function OperationView({ operation, order }: { operation: SqlOperation; order?: string[] }) {
  const names = order ?? Object.keys(operation.parameter_types);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs text-muted-foreground">
          <code className="font-mono">{operation.type}</code> · dialect{" "}
          <code className="font-mono">{operation.dialect}</code> · not executed by Sidewit
        </div>
        <div className="flex gap-2">
          <CopyButton text={operation.statement} label="Copy SQL" />
          <CopyButton text={JSON.stringify(operation, null, 2)} label="Copy JSON" />
        </div>
      </div>
      <LazySqlEditor
        value={operation.statement}
        readOnly
        height={Math.min(60 + operation.statement.split("\n").length * 20, 360)}
      />
      <div>
        <div className="mb-1 text-xs font-medium">Parameters (bind by name)</div>
        {names.length === 0 ? (
          <p className="text-xs text-muted-foreground">No parameters.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Value</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {names.map((name) => (
                <TableRow key={name}>
                  <TableCell className="font-mono text-xs">:{name}</TableCell>
                  <TableCell className="font-mono text-xs">
                    {operation.parameter_types[name]}
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {JSON.stringify(operation.parameters[name])}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
    </div>
  );
}
