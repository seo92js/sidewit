import { contextValueErrors } from "@/lib/scalars";
import { parseExactJson } from "@/lib/json";
import { FileInput, RotateCcw, Save, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";
import { ApiError } from "@/api/client";
import { useContextDetail, useDeleteContext, useUpdateContext } from "@/api/queries";
import type { Context, ContextDefinition, MetricDefinition, Revalidation } from "@/api/types";
import { ErrorAlert, IssueList, Loading } from "@/components/app/common";
import { TimezoneInput } from "@/components/app/TimezoneInput";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { SchemaEditor } from "./context/SchemaEditor";
import { RelationsEditor } from "./context/RelationsEditor";
import { RulesEditor } from "./context/RulesEditor";
import { DdlImport } from "./context/DdlImport";

interface Draft {
  version: number; // the saved version this draft started from
  name: string;
  timezone: string;
  allow_generated_sql: boolean;
  /** Without metrics: while editing, metricsText is their only source. */
  definition: ContextDefinition;
  metricsText: string;
}

function draftOf(context: Context): Draft {
  return {
    version: context.version,
    name: context.name,
    timezone: context.timezone,
    allow_generated_sql: context.allow_generated_sql,
    definition: { ...context.definition, metrics: undefined },
    metricsText: JSON.stringify(context.definition.metrics ?? [], null, 2),
  };
}

/** Syntax and number precision; the server validates contract shape and integrity. */
function parseMetricsJson(text: string): { metrics: MetricDefinition[]; error: string | null } {
  try {
    const value = parseExactJson(text);
    if (!Array.isArray(value))
      return { metrics: [], error: "Metric contracts must be a JSON array." };
    return { metrics: value as MetricDefinition[], error: null };
  } catch (error) {
    return { metrics: [], error: error instanceof Error ? error.message : "Invalid JSON" };
  }
}

export function ContextEditor({ id }: { id: string }) {
  const navigate = useNavigate();
  const context = useContextDetail(id);
  const update = useUpdateContext(id);
  const remove = useDeleteContext();
  const [edits, setEdits] = useState<Draft | null>(null);
  const [tab, setTab] = useState("schema");
  const [revalidation, setRevalidation] = useState<Revalidation | null>(null);

  // Derived per keystroke, so each piece is recomputed only when its input changes.
  const saved = useMemo(() => (context.data ? draftOf(context.data) : null), [context.data]);
  const savedJson = useMemo(() => JSON.stringify(saved), [saved]);
  // A refetch never discards edits. Their original version remains the write precondition.
  const draft = edits ?? saved;
  const dirty = useMemo(
    () => edits !== null && JSON.stringify(edits) !== savedJson,
    [edits, savedJson],
  );
  const metricsText = draft?.metricsText ?? "[]";
  const { metrics, error: metricsError } = useMemo(
    () => parseMetricsJson(metricsText),
    [metricsText],
  );
  const definition = draft?.definition;
  const valueErrors = useMemo(
    () => (definition ? contextValueErrors(definition) : []),
    [definition],
  );

  if (context.isLoading) return <Loading />;
  if (context.error) return <ErrorAlert error={context.error} title="Could not load context" />;
  if (!context.data || !saved || !draft) return null;
  const current = context.data;
  const remoteChanged = draft.version !== current.version;

  const edit = (patch: Partial<Draft>) =>
    setEdits((latest) => ({ ...(latest ?? saved), ...patch }));
  const setDefinition = (definition: ContextDefinition) => edit({ definition });

  const save = () => {
    if (metricsError || valueErrors.length) return;
    setRevalidation(null);
    const submitted = draft;
    update.mutate(
      {
        name: draft.name,
        timezone: draft.timezone,
        allow_generated_sql: draft.allow_generated_sql,
        definition: { ...draft.definition, metrics },
        dialect: current.dialect,
        expected_version: draft.version,
      },
      {
        onSuccess: (result) => {
          setRevalidation(result.revalidation);
          // Clear the submitted draft even for a no-op save. Edits made while saving survive
          // and now refer to the version returned by this successful write.
          setEdits((latest) =>
            latest === submitted || latest === null
              ? null
              : { ...latest, version: result.context.version },
          );
        },
      },
    );
  };

  const conflict = update.error instanceof ApiError && update.error.error === "version_conflict";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1">
          <Label htmlFor="ctx-name" className="text-xs">
            Name
          </Label>
          <Input
            id="ctx-name"
            className="w-56"
            value={draft.name}
            onChange={(e) => edit({ name: e.target.value })}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="ctx-tz" className="text-xs">
            Timezone
          </Label>
          <TimezoneInput
            id="ctx-tz"
            className="w-48"
            value={draft.timezone}
            onChange={(e) => edit({ timezone: e.target.value })}
          />
        </div>
        <label className="flex max-w-72 items-start gap-2 pb-1 text-xs">
          <Checkbox
            id="ctx-generated-sql"
            className="mt-0.5"
            checked={draft.allow_generated_sql}
            onCheckedChange={(v) => edit({ allow_generated_sql: v === true })}
          />
          <span>
            <span className="font-medium">Allow model-written SQL</span>
            <span className="block text-muted-foreground">
              Registered metrics are tried first. Off: answer only with a metric.
            </span>
          </span>
        </label>
        <div className="flex items-center gap-2 pb-1.5">
          <Badge variant="outline" className="font-mono">
            {current.dialect}
          </Badge>
          <Badge variant="secondary" className="font-mono">
            v{current.version}
          </Badge>
          {dirty && <Badge className="bg-amber-500 text-white">unsaved changes</Badge>}
        </div>
        <div className="ml-auto flex gap-2">
          <Button variant="ghost" size="sm" disabled={!dirty} onClick={() => setEdits(null)}>
            <RotateCcw /> Discard
          </Button>
          <Button
            size="sm"
            disabled={!dirty || !!metricsError || valueErrors.length > 0 || update.isPending}
            onClick={save}
          >
            <Save /> {update.isPending ? "Saving…" : `Save as v${current.version + 1}`}
          </Button>
          <Button
            variant="destructive"
            size="sm"
            disabled={remove.isPending}
            onClick={() => {
              if (confirm(`Delete context "${current.name}"? Its generation log is deleted too.`)) {
                remove.mutate(id, { onSuccess: () => navigate("/contexts") });
              }
            }}
          >
            <Trash2 /> Delete
          </Button>
        </div>
      </div>

      {remoteChanged && (
        <Alert variant="destructive">
          <AlertTitle>A newer Context version is available. Your edits have been kept.</AlertTitle>
          <AlertDescription>
            Saving this draft will report a version conflict. Copy your edits before reloading.
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setEdits(null);
                update.reset();
              }}
            >
              Reload latest (discards your edits)
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {valueErrors.length > 0 && (
        <Alert variant="destructive">
          <AlertTitle>Fix input values before saving</AlertTitle>
          <AlertDescription>
            <ul>
              {valueErrors.map((error) => (
                <li key={error}>{error}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}
      <ErrorAlert
        error={update.error}
        title={conflict ? "Version conflict" : "Could not save context"}
        action={
          conflict && (
            <Button
              size="sm"
              variant="outline"
              onClick={async () => {
                update.reset();
                setEdits(null);
                await context.refetch();
              }}
            >
              Reload latest (discards your edits)
            </Button>
          )
        }
      />
      <ErrorAlert error={remove.error} title="Could not delete context" />
      {revalidation && <RevalidationNotice revalidation={revalidation} />}

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="schema">Schema ({draft.definition.resources.length})</TabsTrigger>
          <TabsTrigger value="relations">
            Relations ({draft.definition.relations.length})
          </TabsTrigger>
          <TabsTrigger value="rules">Business rules ({draft.definition.rules.length})</TabsTrigger>
          <TabsTrigger value="metrics">Metrics ({metrics.length})</TabsTrigger>
          <TabsTrigger value="import">
            <FileInput /> Import DDL
          </TabsTrigger>
        </TabsList>
        <TabsContent value="schema" className="pt-3">
          <SchemaEditor definition={draft.definition} onChange={setDefinition} />
        </TabsContent>
        <TabsContent value="relations" className="pt-3">
          <RelationsEditor definition={draft.definition} onChange={setDefinition} />
        </TabsContent>
        <TabsContent value="rules" className="pt-3">
          <RulesEditor definition={draft.definition} onChange={setDefinition} />
        </TabsContent>
        <TabsContent value="metrics" className="space-y-3 pt-3">
          <p className="text-sm text-muted-foreground">
            Register reviewed aggregate contracts. They are used for Generate only when the context
            is in <strong>metric</strong> mode: the model then chooses a contract and the server
            builds the SQL. Fixed filters, aggregation and date fields stay under your control.
            Business-rule prose is not automatically enforced.
          </p>
          <Label htmlFor="metric-contracts">Metric contracts (JSON array)</Label>
          <Textarea
            id="metric-contracts"
            className="font-mono text-xs"
            rows={18}
            value={draft.metricsText}
            onChange={(e) => edit({ metricsText: e.target.value })}
          />
          {metricsError && (
            <p role="alert" className="text-sm text-destructive">
              {metricsError}
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            Example:{" "}
            {`[{"id":"order_count","description":"All orders, including rows with a null id","resource":"orders","aggregate":{"kind":"row_count"},"time_fields":["orders.paid_at"],"default_time_field":"orders.paid_at"}]`}{" "}
            · See docs/METRIC_CONTRACTS.md for joins, fixed filters and inputs. The server checks
            each contract on save. Removing or changing a contract revalidates its saved Tools.
          </p>
        </TabsContent>
        <TabsContent value="import" className="pt-3">
          <DdlImport
            contextId={id}
            contextVersion={current.version}
            draftVersion={draft.version}
            dirty={dirty}
            onApply={({ definition, context_version }) => {
              if (context_version !== current.version || context_version !== draft.version) return;
              // The preview carries the saved metrics; the Metrics tab text stays authoritative.
              edit({ version: context_version, definition: { ...definition, metrics: undefined } });
              setTab("schema");
            }}
          />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function RevalidationNotice({ revalidation }: { revalidation: Revalidation }) {
  if (!revalidation.checked) return null;
  if (!revalidation.invalid.length) {
    return (
      <Alert>
        <AlertTitle>Saved. {revalidation.checked} tool(s) revalidated, all valid.</AlertTitle>
      </Alert>
    );
  }
  return (
    <Alert variant="destructive">
      <AlertTitle>Saved, but {revalidation.invalid.length} tool(s) are now invalid</AlertTitle>
      <AlertDescription className="space-y-2">
        <p>Invalid tools cannot be invoked (409) until the tool or this context is fixed.</p>
        {revalidation.invalid.map((tool) => (
          <div key={tool.name}>
            <Link className="font-mono text-sm underline" to={`/tools/${tool.name}`}>
              {tool.name}
            </Link>
            <IssueList issues={tool.errors} />
          </div>
        ))}
      </AlertDescription>
    </Alert>
  );
}
