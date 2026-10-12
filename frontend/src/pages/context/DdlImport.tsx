import { useEffect, useRef, useState } from "react";
import { useImportDdl } from "@/api/queries";
import type {
  ContextDefinition,
  ImportDdlResult,
  ImportMode,
  PendingForeignKey,
} from "@/api/types";
import { ErrorAlert } from "@/components/app/common";
import { LazySqlEditor } from "@/components/app/LazySqlEditor";
import { Button } from "@/components/ui/button";
import { importChanges } from "./importChanges";

export function DdlImport({
  dirty,
  contextId,
  contextVersion,
  draftVersion,
  definition,
  pendingForeignKeys,
  blocked,
  onApply,
  onClose,
  onInputChange,
}: {
  dirty: boolean;
  contextId: string;
  contextVersion: number;
  draftVersion: number;
  definition: ContextDefinition;
  pendingForeignKeys: PendingForeignKey[];
  blocked: boolean;
  onApply: (preview: ImportDdlResult) => void;
  onClose: () => void;
  onInputChange: (hasInput: boolean) => void;
}) {
  const importDdl = useImportDdl(contextId);
  const [ddl, setDdl] = useState("");
  const [mode, setMode] = useState<ImportMode>("merge");
  const [applied, setApplied] = useState(false);
  const successRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (applied) successRef.current?.focus();
  }, [applied]);
  const [replaceConfirmed, setReplaceConfirmed] = useState(false);
  const preview = importDdl.data;
  const base = importDdl.variables?.base_definition;
  const previewCurrent =
    !!preview &&
    !importDdl.isPending &&
    !importDdl.isError &&
    !blocked &&
    preview.context_version === contextVersion &&
    preview.context_version === draftVersion &&
    importDdl.variables?.ddl === ddl &&
    importDdl.variables?.mode === mode &&
    JSON.stringify(base) === JSON.stringify(definition) &&
    JSON.stringify(importDdl.variables?.pending_foreign_keys) ===
      JSON.stringify(pendingForeignKeys);
  const changes = preview && base ? importChanges(base, preview.definition) : null;
  const pendingChanged =
    !!preview &&
    JSON.stringify(preview.pending_foreign_keys ?? []) !== JSON.stringify(pendingForeignKeys);
  const hasChanges = changes?.changed || pendingChanged;
  // Older servers provide messages only: display all of them without guessing their category.
  const details =
    preview?.warning_details ??
    preview?.warnings.map((message) => ({ code: "unknown", message })) ??
    [];
  const ignored = details.filter((w) => w.code === "ignored_statement").map((w) => w.message);
  const warnings = details
    .filter(
      (w) => !["ignored_statement", "resource_preserved", "pending_foreign_key"].includes(w.code),
    )
    .map((w) => w.message);

  function reset() {
    setDdl("");
    setApplied(false);
    setReplaceConfirmed(false);
    setMode("merge");
    importDdl.reset();
    onInputChange(false);
  }

  if (applied)
    return (
      <section aria-label="DDL import" className="space-y-3 rounded-lg border bg-muted/20 p-4">
        <p ref={successRef} tabIndex={-1} role="status" className="font-medium outline-none">
          Changes applied · {definition.resources.length}{" "}
          {definition.resources.length === 1 ? "table" : "tables"} in this Context.
        </p>
        <p className="text-sm text-muted-foreground">
          {dirty
            ? "Not saved yet. Add more tables or review your schema, then save changes."
            : "Saved. You can import more tables or review your schema."}
        </p>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={reset}>
            Import another DDL
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              reset();
              onClose();
            }}
          >
            Review tables
          </Button>
        </div>
      </section>
    );

  return (
    <section aria-label="DDL import" className="space-y-4 rounded-lg border bg-muted/20 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-medium">
            {mode === "merge" ? "Import tables from DDL" : "Replace schema from DDL"}
          </h3>
          <p className="mt-1 text-sm text-muted-foreground">
            {mode === "merge"
              ? "Add tables to your current edits. Existing tables, descriptions and business rules are kept. Matching columns can be updated."
              : "Replace all tables and relations. Existing descriptions can be lost. Business rules and metrics stay and must match the new schema."}
          </p>
        </div>
        <Button size="sm" variant="ghost" onClick={onClose}>
          Close import
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        Paste CREATE TABLE statements and ALTER TABLE … FOREIGN KEY. Your database is not modified.
      </p>
      <LazySqlEditor
        value={ddl}
        onChange={(value) => {
          setDdl(value);
          setReplaceConfirmed(false);
          onInputChange(!!value.trim());
        }}
        height={260}
      />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Button
          size="sm"
          disabled={
            !ddl.trim() || importDdl.isPending || blocked || draftVersion !== contextVersion
          }
          onClick={() => {
            setReplaceConfirmed(false);
            importDdl.mutate({
              ddl,
              mode,
              expected_version: draftVersion,
              base_definition: definition,
              pending_foreign_keys: pendingForeignKeys,
            });
          }}
        >
          {importDdl.isPending ? "Checking changes…" : "Preview changes"}
        </Button>
        {mode === "merge" ? (
          <details className="text-xs">
            <summary className="cursor-pointer text-muted-foreground">Advanced</summary>
            <Button
              size="sm"
              variant="ghost"
              className="mt-2 text-destructive"
              onClick={() => {
                setMode("replace");
                importDdl.reset();
              }}
            >
              Replace entire schema…
            </Button>
          </details>
        ) : (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setMode("merge");
              setReplaceConfirmed(false);
              importDdl.reset();
            }}
          >
            Back to adding tables
          </Button>
        )}
      </div>
      {blocked && (
        <p role="alert" className="text-sm">
          Finish saving or fix the Context input errors before previewing.
        </p>
      )}
      <ErrorAlert error={importDdl.error} title="Could not preview DDL" />
      {preview && changes && (
        <div className="space-y-3 rounded-md border bg-background p-3">
          <h4 className="font-medium">Changes to your current schema</h4>
          {!!changes.added.length && (
            <div>
              <p className="text-sm font-medium">
                Add {changes.added.length} {changes.added.length === 1 ? "table" : "tables"}
              </p>
              <ul className="list-inside list-disc text-sm">
                {changes.added.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            </div>
          )}
          {changes.updated.map((r) => (
            <details key={r.name} open className="text-sm">
              <summary className="font-medium">Update {r.name}</summary>
              <ul className="ml-4 list-disc">
                {r.details.map((d) => (
                  <li key={d}>{d}</li>
                ))}
              </ul>
            </details>
          ))}
          {!!changes.removed.length && (
            <p className="text-sm text-destructive">Remove tables: {changes.removed.join(", ")}</p>
          )}
          {!!changes.relationsAdded.length && (
            <p className="text-sm">Add relations: {changes.relationsAdded.join(", ")}</p>
          )}
          {!!changes.relationsRemoved.length && (
            <p className="text-sm text-destructive">
              Remove relations: {changes.relationsRemoved.join(", ")}
            </p>
          )}
          <p className="text-sm text-muted-foreground">
            Tables unchanged: {changes.unchanged} · Tables after applying:{" "}
            {preview.definition.resources.length}
          </p>
          {!!preview.pending_foreign_keys?.length && (
            <div className="text-sm">
              <p className="font-medium">Relations waiting for referenced columns</p>
              <ul className="list-inside list-disc">
                {preview.pending_foreign_keys.map((fk, i) => (
                  <li key={i}>
                    {fk.table}.{fk.column} → {fk.ref_table}.{fk.ref_column ?? "(primary key)"}
                  </li>
                ))}
              </ul>
              <p>
                These will be kept for the next import. Resolve or explicitly exclude them before
                saving.
              </p>
            </div>
          )}
          {!!warnings.length && (
            <div className="text-sm">
              <p className="font-medium">Review before applying</p>
              <ul className="ml-4 list-disc">
                {warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </div>
          )}
          {!!ignored.length && (
            <details className="text-xs text-muted-foreground">
              <summary className="cursor-pointer">
                {ignored.length} unsupported {ignored.length === 1 ? "statement" : "statements"}{" "}
                omitted
              </summary>
              <ul className="ml-4 list-disc">
                {ignored.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </details>
          )}
          {!previewCurrent && (
            <p role="alert" className="text-sm text-destructive">
              The DDL or Context has changed. Preview changes again before applying.
            </p>
          )}
          {!hasChanges && previewCurrent && (
            <p role="status" className="text-sm">
              No changes to apply.
            </p>
          )}
          {mode === "replace" && hasChanges && (
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={replaceConfirmed}
                onChange={(e) => setReplaceConfirmed(e.target.checked)}
              />
              Replace the current tables and relations with this preview.
            </label>
          )}
          <Button
            size="sm"
            variant={mode === "replace" ? "destructive" : "default"}
            disabled={!previewCurrent || !hasChanges || (mode === "replace" && !replaceConfirmed)}
            onClick={() => {
              if (!previewCurrent) return;
              onApply(preview);
              setApplied(true);
              onInputChange(false);
            }}
          >
            {mode === "replace"
              ? "Replace schema"
              : changes.added.length && !changes.updated.length && !changes.relationsRemoved.length
                ? `Add ${changes.added.length} table${changes.added.length === 1 ? "" : "s"}`
                : "Apply changes"}
          </Button>
          <p className="text-xs text-muted-foreground">
            Applies to this editor. Save changes when you are done.
          </p>
        </div>
      )}
    </section>
  );
}
