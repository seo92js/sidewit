import { useState } from "react";
import { useImportDdl } from "@/api/queries";
import type { ImportDdlResult, ImportMode } from "@/api/types";
import { ErrorAlert } from "@/components/app/common";
import { LazySqlEditor } from "@/components/app/LazySqlEditor";
import { NativeSelect } from "@/components/app/NativeSelect";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

export function DdlImport({
  contextId,
  contextVersion,
  draftVersion,
  dirty,
  onApply,
}: {
  contextId: string;
  contextVersion: number;
  draftVersion: number;
  dirty: boolean;
  onApply: (preview: ImportDdlResult) => void;
}) {
  const importDdl = useImportDdl(contextId);
  const [ddl, setDdl] = useState("");
  const [mode, setMode] = useState<ImportMode>("merge");
  const preview: ImportDdlResult | undefined = importDdl.data;
  const previewCurrent =
    !!preview &&
    !importDdl.isPending &&
    !importDdl.isError &&
    preview.context_version === contextVersion &&
    preview.context_version === draftVersion &&
    importDdl.variables?.ddl === ddl &&
    importDdl.variables?.mode === mode;

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        Paste <code>CREATE TABLE</code> statements (and <code>ALTER TABLE … FOREIGN KEY</code>). The
        preview is computed from the <strong>saved</strong> version of this context and is not saved
        until you apply it and click Save.
      </p>
      {dirty && (
        <Alert>
          <AlertTitle>You have unsaved edits</AlertTitle>
          <AlertDescription>
            Applying an import replaces the current draft with the preview, which is based on the
            saved version.
          </AlertDescription>
        </Alert>
      )}
      <LazySqlEditor value={ddl} onChange={setDdl} height={260} />
      <div className="flex flex-wrap items-center gap-3">
        <NativeSelect
          aria-label="Import mode"
          value={mode}
          onChange={(e) => setMode(e.target.value as ImportMode)}
        >
          <option value="merge">merge — keep descriptions and sample values</option>
          <option value="replace">replace — schema from DDL only (rules kept)</option>
        </NativeSelect>
        <Button
          size="sm"
          disabled={!ddl.trim() || importDdl.isPending || draftVersion !== contextVersion}
          onClick={() => importDdl.mutate({ ddl, mode, expected_version: contextVersion })}
        >
          {importDdl.isPending ? "Parsing…" : "Preview import"}
        </Button>
      </div>
      <ErrorAlert error={importDdl.error} title="Could not parse DDL" />
      {preview && (
        <div className="space-y-3 rounded-md border p-3">
          <div className="text-sm font-medium">
            Preview: {preview.definition.resources.length} resources ·{" "}
            {preview.definition.resources.reduce((n, r) => n + r.fields.length, 0)} fields ·{" "}
            {preview.definition.relations.length} relations
          </div>
          {preview.warnings.length > 0 && (
            <div>
              <div className="mb-1 text-xs font-medium text-amber-900">
                Warnings ({preview.warnings.length})
              </div>
              <ul className="list-disc space-y-0.5 pl-5 text-sm">
                {preview.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </div>
          )}
          <ul className="grid gap-1 text-sm sm:grid-cols-2">
            {preview.definition.resources.map((r) => (
              <li key={r.name} className="font-mono text-xs">
                {r.name}{" "}
                <span className="text-muted-foreground">
                  ({r.fields.map((f) => f.name).join(", ")})
                </span>
              </li>
            ))}
          </ul>
          {!previewCurrent && (
            <p role="alert" className="text-sm text-destructive">
              This preview is out of date. Reconcile any Context changes and preview again before
              applying.
            </p>
          )}
          <Button
            size="sm"
            disabled={!previewCurrent}
            onClick={() => previewCurrent && onApply(preview)}
          >
            Apply to draft
          </Button>
        </div>
      )}
    </div>
  );
}
