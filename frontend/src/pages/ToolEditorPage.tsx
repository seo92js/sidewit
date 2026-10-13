import { ApiError } from "@/api/client";
import { useQuery } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Plus, RotateCcw, Save, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import {
  useContexts,
  useCreateTool,
  useDeleteTool,
  useTool,
  useToolDraft,
  useUpdateTool,
  useRefreshContract,
  validate,
} from "@/api/queries";
import type { InputProperty, InputSchema, MetricBinding, ParamType, Tool } from "@/api/types";
import {
  ErrorAlert,
  JsonBlock,
  Loading,
  SectionTitle,
  ValidationPanel,
} from "@/components/app/common";
import { LazySqlEditor } from "@/components/app/LazySqlEditor";
import { NativeSelect } from "@/components/app/NativeSelect";
import { ValueListInput } from "@/components/app/ValueListInput";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { placeholders } from "./tool/placeholders";
import { orderedInputs, paramType } from "./tool/inputSchema";
import { SavedToolStatus } from "./tool/SavedToolStatus";
import { UnsavedChangesGuard } from "@/components/app/UnsavedChangesGuard";
import { InvokePanel } from "./tool/InvokePanel";

const PARAM_TYPES: ParamType[] = ["string", "integer", "number", "boolean", "date", "date-time"];
const NAME_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;

interface ParamRow {
  name: string;
  type: ParamType;
  description: string;
  /** Allowed values for string inputs, kept exactly as saved until edited. */
  enum: string[];
}

interface FormState {
  name: string;
  contextId: string;
  description: string;
  rows: ParamRow[];
  statement: string;
  sourceGenerationId: string | null;
  exampleArguments: Record<string, unknown>;
  metricBinding: MetricBinding | null;
}

function rowsFromSchema(schema: InputSchema): ParamRow[] {
  return orderedInputs(schema).map((name) => {
    const prop = schema.properties[name];
    return {
      name,
      type: paramType(prop),
      description: prop.description ?? "",
      enum: prop.enum ?? [],
    };
  });
}

function schemaFromRows(rows: ParamRow[]): InputSchema {
  const properties: Record<string, InputProperty> = {};
  for (const row of rows) {
    const prop: InputProperty =
      row.type === "date" || row.type === "date-time"
        ? { type: "string", format: row.type }
        : { type: row.type };
    if (row.description.trim()) prop.description = row.description.trim();
    if (row.type === "string" && row.enum.length) prop.enum = row.enum;
    properties[row.name] = prop;
  }
  return {
    type: "object",
    properties,
    required: rows.map((r) => r.name),
    additionalProperties: false,
  };
}

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return debounced;
}

export default function ToolEditorPage() {
  const { name } = useParams();
  const [search] = useSearchParams();
  const generationId = name ? undefined : (search.get("generation") ?? undefined);
  const tool = useTool(name);
  const draft = useToolDraft(generationId);
  const contexts = useContexts();

  if (tool.isLoading || draft.isLoading || contexts.isLoading) return <Loading />;
  if (tool.error && !tool.data)
    return <ErrorAlert error={tool.error} title={`Could not load tool ${name}`} />;
  if (contexts.error && !contexts.data)
    return <ErrorAlert error={contexts.error} title="Could not load contexts" />;

  let initial: FormState;
  if (tool.data) {
    initial = {
      name: tool.data.name,
      contextId: tool.data.context_id,
      description: tool.data.description,
      rows: rowsFromSchema(tool.data.input_schema),
      statement: tool.data.operation.statement,
      sourceGenerationId: tool.data.source_generation_id,
      exampleArguments: {},
      metricBinding: tool.data.metric_binding ?? null,
    };
  } else if (draft.data) {
    initial = {
      name: draft.data.name ?? "",
      contextId: draft.data.context_id,
      description: draft.data.description,
      rows: rowsFromSchema(draft.data.input_schema),
      statement: draft.data.operation.statement,
      sourceGenerationId: draft.data.source_generation_id,
      exampleArguments: draft.data.example_arguments,
      metricBinding: draft.data.metric_binding ?? null,
    };
  } else {
    initial = {
      name: "",
      contextId: contexts.data?.[0]?.id ?? "",
      description: "",
      rows: [],
      statement: "",
      sourceGenerationId: null,
      exampleArguments: {},
      metricBinding: null,
    };
  }

  return (
    <div className="space-y-4">
      <div className="text-sm text-muted-foreground">
        <Link className="hover:underline" to="/tools">
          Tools
        </Link>{" "}
        / {tool.data ? <span className="font-mono">{tool.data.name}</span> : "New tool"}
      </div>
      <ErrorAlert error={tool.error} title="Could not refresh tool; your edits are preserved" />
      <ErrorAlert
        error={contexts.error}
        title="Could not refresh contexts; your edits are preserved"
      />
      <ErrorAlert error={draft.error} title="Could not create a draft from this generation" />
      {draft.data && (
        <Alert>
          <AlertTitle>
            Draft from generation {draft.data.source_generation_id.slice(0, 8)}
          </AlertTitle>
          <AlertDescription>
            Draft material only — review the name, description, inputs and SQL. It is validated
            again against the current context when you save.
          </AlertDescription>
        </Alert>
      )}
      <ToolForm
        key={tool.data?.name ?? generationId ?? "blank"}
        initial={initial}
        saved={tool.data}
      />
    </div>
  );
}

function ToolForm({ initial, saved }: { initial: FormState; saved?: Tool }) {
  const navigate = useNavigate();
  const latest = useTool(saved?.name);
  const contexts = useContexts();
  const create = useCreateTool();
  const update = useUpdateTool(saved?.name ?? "");
  const remove = useDeleteTool();
  const refresh = useRefreshContract(saved?.name ?? "");
  const [reloading, setReloading] = useState(false);
  const busy =
    reloading || create.isPending || update.isPending || refresh.isPending || remove.isPending;
  const allowedNavigation = useRef<string | null>(null);
  const goAfterWrite = (path: string, state?: object) => {
    allowedNavigation.current = path;
    navigate(path, { replace: true, state });
  };
  const [form, setForm] = useState(initial);
  const [baseVersion, setBaseVersion] = useState(saved?.version);
  const reload = (tool: Tool) => {
    setForm((previous) => ({
      ...previous,
      description: tool.description,
      statement: tool.operation.statement,
      rows: rowsFromSchema(tool.input_schema),
      metricBinding: tool.metric_binding ?? null,
    }));
    setBaseVersion(tool.version);
    update.reset();
  };
  const set = (patch: Partial<FormState>) => {
    if (!busy) setForm((f) => ({ ...f, ...patch }));
  };
  // Metric-bound: SQL and input names/types are owned by the contract.
  const bound = !!form.metricBinding;

  const inputSchema = useMemo(() => schemaFromRows(form.rows), [form.rows]);
  const payload = useMemo(
    () => ({
      context_id: form.contextId,
      operation: { type: "sql" as const, statement: form.statement },
      input_schema: inputSchema,
      metric_binding: form.metricBinding,
    }),
    [form.contextId, form.statement, inputSchema, form.metricBinding],
  );
  const debounced = useDebounced(payload, 400);
  const context = contexts.data?.find((c) => c.id === form.contextId);
  const validation = useQuery({
    queryKey: ["validate", debounced, context?.version],
    queryFn: () => validate(debounced),
    enabled:
      !!context && debounced.context_id === context.id && !!debounced.operation.statement.trim(),
    placeholderData: (previous) => previous,
  });
  const validationCurrent =
    !!context &&
    debounced === payload &&
    validation.isSuccess &&
    !validation.isPlaceholderData &&
    !validation.isFetching;

  const used = placeholders(form.statement);
  const declared = form.rows.map((r) => r.name);
  const missing = used.filter((p) => !declared.includes(p));
  const nameValid = NAME_PATTERN.test(form.name);
  const dirty =
    !saved ||
    JSON.stringify({
      description: form.description.trim(),
      inputSchema,
      statement: form.statement,
    }) !==
      JSON.stringify({
        description: saved.description,
        inputSchema: schemaFromRows(rowsFromSchema(saved.input_schema)),
        statement: saved.operation.statement,
      });
  const canSave =
    nameValid &&
    !!form.description.trim() &&
    !!form.contextId &&
    (!saved || saved.version === baseVersion) &&
    !(update.error instanceof ApiError && update.error.status === 409) &&
    validationCurrent &&
    !!validation.data?.valid &&
    dirty;
  const saveError = saved ? update.error : create.error;

  const save = () => {
    if (!canSave || busy) return;
    const body = {
      description: form.description.trim(),
      input_schema: inputSchema,
      operation: { type: "sql" as const, statement: form.statement },
    };
    if (saved) {
      update.mutate(
        { ...body, expected_version: baseVersion ?? saved.version },
        { onSuccess: (tool) => setBaseVersion(tool.version) },
      );
    } else {
      create.mutate(
        {
          ...body,
          context_id: form.contextId,
          name: form.name,
          source_generation_id: form.sourceGenerationId,
          metric_binding: form.metricBinding,
        },
        {
          onSuccess: (tool) =>
            goAfterWrite(`/tools/${tool.name}`, { exampleArguments: form.exampleArguments }),
        },
      );
    }
  };

  const setRow = (index: number, patch: Partial<ParamRow>) =>
    set({ rows: form.rows.map((r, i) => (i === index ? { ...r, ...patch } : r)) });
  const moveRow = (index: number, delta: number) => {
    const rows = [...form.rows];
    const [row] = rows.splice(index, 1);
    rows.splice(index + delta, 0, row);
    set({ rows });
  };

  const hasUnsavedEdits = saved
    ? dirty
    : !!(form.name || form.description || form.statement || form.rows.length);
  return (
    <>
      <UnsavedChangesGuard
        dirty={hasUnsavedEdits}
        busy={busy}
        description="Your unsaved Tool name, description, inputs and SQL will be discarded."
        allowNavigation={(path) => allowedNavigation.current === path}
      />
      {busy && (
        <p role="status" className="text-sm text-muted-foreground">
          {reloading
            ? "Reloading Tool…"
            : refresh.isPending
              ? "Rebuilding from contract…"
              : remove.isPending
                ? "Deleting Tool…"
                : "Saving Tool…"}{" "}
          Editing is temporarily locked.
        </p>
      )}
      <div className="grid gap-5 xl:grid-cols-[3fr_2fr]">
        <fieldset
          disabled={busy}
          aria-label="Tool editor"
          aria-busy={busy}
          className="min-w-0 space-y-4"
        >
          {saved && (
            <SavedToolStatus
              tool={saved}
              currentContextVersion={context?.version}
              dirty={dirty}
              busy={busy}
              rebuilding={refresh.isPending}
              rebuildError={refresh.error}
              onRebuild={() => {
                if (!busy && !dirty && saved) refresh.mutate(saved.version, { onSuccess: reload });
              }}
            />
          )}
          {saved && saved.version !== baseVersion && (
            <Alert>
              <AlertTitle>Tool changed on the server</AlertTitle>
              <AlertDescription>
                Your edits are preserved. Reset to the latest version before saving again.
              </AlertDescription>
            </Alert>
          )}
          {form.metricBinding && (
            <Alert>
              <AlertTitle>Bound to metric {form.metricBinding.metric_id}</AlertTitle>
              <AlertDescription>
                The SQL and the input names and types come from this contract and are read-only.
                Change the contract in the Context, then rebuild the Tool from it. Descriptions and
                allowed values stay editable.
              </AlertDescription>
            </Alert>
          )}

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="tool-name" className="text-xs">
                Name
              </Label>
              <Input
                id="tool-name"
                className="font-mono"
                value={form.name}
                disabled={!!saved}
                onChange={(e) => set({ name: e.target.value })}
                placeholder="regional_revenue"
              />
              {!saved && form.name && !nameValid && (
                <p className="text-xs text-destructive">
                  snake_case: lowercase letters, digits and “_”, starting with a letter (max 64).
                </p>
              )}
            </div>
            <div className="space-y-1">
              <Label htmlFor="tool-context" className="text-xs">
                Context
              </Label>
              {saved ? (
                <div className="flex h-8 items-center gap-2 text-sm">
                  {context?.name ?? saved.context_id}
                  <span className="font-mono text-xs text-muted-foreground">
                    v{context?.version}
                  </span>
                </div>
              ) : (
                <NativeSelect
                  id="tool-context"
                  className="w-full"
                  value={form.contextId}
                  onChange={(e) => set({ contextId: e.target.value })}
                >
                  {contexts.data?.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name} (v{c.version})
                    </option>
                  ))}
                </NativeSelect>
              )}
            </div>
          </div>
          <div className="space-y-1">
            <Label htmlFor="tool-description" className="text-xs">
              Description
            </Label>
            <Textarea
              id="tool-description"
              rows={2}
              value={form.description}
              onChange={(e) => set({ description: e.target.value })}
              placeholder="What this tool returns, for humans and for future tool calling."
            />
          </div>

          <div>
            <SectionTitle
              action={
                !bound &&
                missing.length > 0 && (
                  <Button
                    variant="outline"
                    size="xs"
                    onClick={() =>
                      set({
                        rows: [
                          ...form.rows,
                          ...missing.map((n) => ({
                            name: n,
                            type: "string" as ParamType,
                            description: "",
                            enum: [],
                          })),
                        ],
                      })
                    }
                  >
                    <Plus /> Add inputs for {missing.map((m) => `:${m}`).join(", ")}
                  </Button>
                )
              }
            >
              Inputs (input_schema)
            </SectionTitle>
            <p className="mb-2 text-xs text-muted-foreground">
              v0.1: every input is required; no defaults, optional inputs or arrays. Each input must
              appear in the SQL as <code>:name</code>.
            </p>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-44">Name</TableHead>
                  <TableHead className="w-32">Type</TableHead>
                  <TableHead>Description</TableHead>
                  <TableHead className="w-40">Allowed values</TableHead>
                  <TableHead className="w-24" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {form.rows.map((row, index) => (
                  <TableRow key={index}>
                    <TableCell>
                      <Input
                        className="h-7 font-mono"
                        aria-label="Input name"
                        disabled={bound}
                        value={row.name}
                        onChange={(e) => setRow(index, { name: e.target.value })}
                      />
                    </TableCell>
                    <TableCell>
                      <NativeSelect
                        className="h-7 w-full"
                        aria-label="Input type"
                        disabled={bound}
                        value={row.type}
                        onChange={(e) => setRow(index, { type: e.target.value as ParamType })}
                      >
                        {PARAM_TYPES.map((t) => (
                          <option key={t} value={t}>
                            {t}
                          </option>
                        ))}
                      </NativeSelect>
                    </TableCell>
                    <TableCell>
                      <Input
                        className="h-7"
                        aria-label="Input description"
                        value={row.description}
                        onChange={(e) => setRow(index, { description: e.target.value })}
                      />
                    </TableCell>
                    <TableCell>
                      <ValueListInput
                        label="Allowed value"
                        type="string"
                        disabled={row.type !== "string"}
                        values={row.enum}
                        onChange={(values) => setRow(index, { enum: values.map(String) })}
                      />
                    </TableCell>
                    <TableCell className="whitespace-nowrap">
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        aria-label="Move up"
                        disabled={bound || index === 0}
                        onClick={() => moveRow(index, -1)}
                      >
                        <ArrowUp />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        aria-label="Move down"
                        disabled={bound || index === form.rows.length - 1}
                        onClick={() => moveRow(index, 1)}
                      >
                        <ArrowDown />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        aria-label="Remove input"
                        disabled={bound}
                        onClick={() => set({ rows: form.rows.filter((_, i) => i !== index) })}
                      >
                        <Trash2 />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <Button
              variant="ghost"
              size="xs"
              className="mt-1"
              disabled={bound}
              onClick={() =>
                set({
                  rows: [
                    ...form.rows,
                    {
                      name: `input_${form.rows.length + 1}`,
                      type: "string",
                      description: "",
                      enum: [],
                    },
                  ],
                })
              }
            >
              <Plus /> Add input
            </Button>
          </div>

          <div>
            <SectionTitle>SQL statement</SectionTitle>
            <LazySqlEditor
              value={form.statement}
              onChange={bound || busy ? undefined : (statement) => set({ statement })}
              readOnly={bound || busy}
              height={240}
            />
            <p className="mt-1 text-xs text-muted-foreground">
              Placeholders found: {used.length ? used.map((p) => `:${p}`).join(", ") : "none"}
            </p>
          </div>

          <div className="space-y-2">
            {!form.statement.trim() ? (
              <p className="text-xs text-muted-foreground">Write a statement to validate it.</p>
            ) : !context ? (
              <p className="text-xs text-muted-foreground">
                Select an available context to validate.
              </p>
            ) : validation.error ? (
              <ErrorAlert error={validation.error} title="Validation request failed" />
            ) : validationCurrent && validation.data ? (
              <ValidationPanel
                result={validation.data}
                label={validation.data.valid ? "Tool validated" : "Tool validation failed"}
              />
            ) : (
              <Loading label="Validating…" />
            )}
            <p className="text-xs text-muted-foreground">
              Tool validation uses the default policy: relative time functions such as{" "}
              <code>NOW()</code> are allowed in tools. This is not a Generate-compatibility check.
            </p>
          </div>

          <ErrorAlert error={saveError} title="Could not save tool" />
          {saved &&
            (saved.version !== baseVersion ||
              (saveError instanceof ApiError && saveError.status === 409)) && (
              <>
                <p className="text-sm">Your edits are preserved. Copy them before reloading.</p>
                <Button
                  variant="outline"
                  disabled={latest.isFetching}
                  onClick={async () => {
                    setReloading(true);
                    try {
                      const result = await latest.refetch();
                      if (result.data && !result.isError) reload(result.data);
                    } finally {
                      setReloading(false);
                    }
                  }}
                >
                  Reload latest (discards your edits)
                </Button>
              </>
            )}
          <ErrorAlert error={latest.error} title="Could not reload tool" />
          <ErrorAlert error={remove.error} title="Could not delete tool" />
          <div className="flex flex-wrap gap-2">
            <Button onClick={save} disabled={!canSave || create.isPending || update.isPending}>
              <Save />{" "}
              {saved ? (dirty ? `Save as v${saved.version + 1}` : "No changes") : "Save tool"}
            </Button>
            {saved && dirty && (
              <Button variant="ghost" onClick={() => reload(saved)}>
                <RotateCcw /> Discard
              </Button>
            )}
            {saved && (
              <Button
                variant="destructive"
                disabled={remove.isPending}
                onClick={() =>
                  confirm(`Delete tool "${saved.name}"?`) &&
                  remove.mutate(saved.name, { onSuccess: () => goAfterWrite("/tools") })
                }
              >
                <Trash2 /> Delete
              </Button>
            )}
          </div>
          <details className="text-xs">
            <summary className="cursor-pointer text-muted-foreground">input_schema JSON</summary>
            <JsonBlock value={inputSchema} />
          </details>
        </fieldset>

        <div className="min-w-0">
          {saved ? (
            <InvokePanel tool={saved} dirty={dirty} />
          ) : (
            <Card>
              <CardContent className="pt-6 text-sm text-muted-foreground">
                Save the tool to invoke it.
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
