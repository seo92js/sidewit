import { ChevronDown, ChevronRight, HelpCircle, Sparkles, Wrench, XCircle } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { ApiError, getApiKey } from "@/api/client";
import { useContexts, useGenerate, useGeneration } from "@/api/queries";
import type {
  ClarificationAnswer,
  GenerateRequest,
  GenerateResponse,
  OperationSource,
} from "@/api/types";
import {
  CopyButton,
  Empty,
  ErrorAlert,
  IssueList,
  Loading,
  ValidationPanel,
} from "@/components/app/common";
import { NativeSelect } from "@/components/app/NativeSelect";
import { OperationView } from "@/components/app/OperationView";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

import { generateRequestCurl } from "@/lib/apiRequest";

import { ClarificationForm } from "./ClarificationForm";

const CONTEXT_STORAGE = "sidewit.playground.context";

export default function PlaygroundPage() {
  const contexts = useContexts();
  const generate = useGenerate();
  const [storedContextId, setStoredContextId] = useState(
    () => localStorage.getItem(CONTEXT_STORAGE) ?? "",
  );
  const [question, setQuestion] = useState("");
  const [referenceTime, setReferenceTime] = useState("");
  const [metricId, setMetricId] = useState("");
  const [dateField, setDateField] = useState("");
  const [requireMetric, setRequireMetric] = useState(false);
  const [submittedContext, setSubmittedContext] = useState<{
    name: string;
    version: number;
  } | null>(null);

  const [baseRequest, setBaseRequest] = useState<GenerateRequest | null>(null);
  const [continuation, setContinuation] = useState<{
    result: Extract<GenerateResponse, { status: "ambiguous" }>;
    request: GenerateRequest;
  } | null>(null);

  // Fall back to the first context when the stored one no longer exists.
  const selected =
    contexts.data?.find((c) => c.id === storedContextId) ?? contexts.data?.[0] ?? undefined;
  const contextId = selected?.id ?? "";
  const setContextId = (id: string) => {
    localStorage.setItem(CONTEXT_STORAGE, id);
    setStoredContextId(id);
  };
  const metrics = selected?.definition.metrics ?? [];
  // Pinning to metrics only matters when the Context also allows model-written SQL.
  const canRequireMetric = metrics.length > 0 && !!selected?.allow_generated_sql;
  const metric = metrics.find((m) => m.id === metricId);

  const requestPayload: GenerateRequest = {
    context_id: contextId,
    question,
    ...(referenceTime.trim() ? { reference_time: referenceTime.trim() } : {}),
    ...(metric ? { metric_id: metric.id } : {}),
    ...(metric?.time_fields?.includes(dateField) ? { date_field: dateField } : {}),
    ...(canRequireMetric && requireMetric ? { require_metric: true } : {}),
  };
  const resultVersion =
    generate.data?.context_version ??
    continuation?.result.context_version ??
    submittedContext?.version;
  const inputsChanged =
    !!generate.variables &&
    (JSON.stringify(baseRequest) !== JSON.stringify(requestPayload) ||
      resultVersion !== selected?.version);
  const clarificationBlock = inputsChanged
    ? "Inputs or Context changed. Generate a new request to use your current inputs."
    : generate.error instanceof ApiError && generate.error.error === "context_changed"
      ? "The Context changed on the server. Generate a new request to continue."
      : continuation &&
          (continuation.request.clarification_answers?.length ?? 0) +
            (continuation.result.clarification_questions?.length ?? 0) >
            24
        ? "This request reached the clarification limit. Start a new request with the details you have provided."
        : null;
  const send = (payload: GenerateRequest) => {
    generate.mutate(payload, {
      onSuccess: (result) => {
        setContinuation(
          result.status === "ambiguous" &&
            result.clarification_questions?.length &&
            result.reference_time
            ? { result, request: payload }
            : null,
        );
      },
    });
  };
  const continueWith = (answers: ClarificationAnswer[]) => {
    if (!continuation || inputsChanged || generate.isPending) return;
    send({
      ...continuation.request,
      reference_time: continuation.result.reference_time,
      expected_context_version: continuation.result.context_version,
      clarification_answers: [...(continuation.request.clarification_answers ?? []), ...answers],
    });
  };
  const submit = () => {
    if (!selected || !question.trim() || generate.isPending) return;
    setSubmittedContext({ name: selected.name, version: selected.version });
    setBaseRequest(requestPayload);
    setContinuation(null);
    send(requestPayload);
  };

  if (contexts.isLoading) return <Loading />;
  if (contexts.error) return <ErrorAlert error={contexts.error} title="Could not load contexts" />;
  if (!contexts.data?.length) {
    return (
      <Empty>
        No context yet.{" "}
        <Link className="underline" to="/contexts">
          Create a context
        </Link>{" "}
        with your schema first.
      </Empty>
    );
  }

  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(320px,2fr)_3fr]">
      <section className="space-y-4">
        <div>
          <h1 className="text-lg font-semibold">Playground</h1>
          <p className="text-sm text-muted-foreground">
            Ask in natural language. Sidewit first looks for a matching saved Tool, otherwise
            creates a new SQL draft. It never runs SQL; your application does.
          </p>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="context">Context</Label>
          <NativeSelect
            id="context"
            disabled={generate.isPending}
            className="w-full"
            value={contextId}
            onChange={(e) => {
              setContextId(e.target.value);
              setMetricId("");
              setDateField("");
              generate.reset();
              setSubmittedContext(null);
              setContinuation(null);
              setBaseRequest(null);
            }}
          >
            {contexts.data.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} (v{c.version})
              </option>
            ))}
          </NativeSelect>
          {selected && (
            <p className="text-xs text-muted-foreground">
              {selected.definition.resources.length} resources · {selected.definition.rules.length}{" "}
              business rules · timezone {selected.timezone} · saved Tools first, then{" "}
              {metrics.length === 0
                ? "model-written SQL"
                : selected.allow_generated_sql
                  ? `${metrics.length} metrics first, then model-written SQL`
                  : `${metrics.length} metrics only`}
            </p>
          )}
        </div>

        {metrics.length > 0 && (
          <div className="space-y-2">
            <Label htmlFor="metric">Metric contract</Label>
            <NativeSelect
              id="metric"
              className="w-full"
              value={metric?.id ?? ""}
              onChange={(e) => {
                setMetricId(e.target.value);
                setDateField("");
              }}
            >
              <option value="">Automatic (saved Tool, metric, or SQL if allowed)</option>
              {metrics.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id} — {m.description}
                </option>
              ))}
            </NativeSelect>
            <p className="text-xs text-muted-foreground">
              Choosing a metric pins this request to it; the server builds the SQL from the
              contract. Review the model's inputs and period.
            </p>
            {canRequireMetric && !metric && (
              <label className="flex items-center gap-2 text-xs">
                <Checkbox
                  id="require-metric"
                  checked={requireMetric}
                  onCheckedChange={(v) => setRequireMetric(v === true)}
                />
                Answer only with a registered metric (no model-written SQL)
              </label>
            )}
            {!!metric?.time_fields?.length && (
              <>
                <Label htmlFor="date-field">Date basis (for period questions)</Label>
                <NativeSelect
                  id="date-field"
                  className="w-full"
                  value={dateField}
                  onChange={(e) => setDateField(e.target.value)}
                >
                  <option value="">Explicit field in question or contract default</option>
                  {metric.time_fields.map((ref) => (
                    <option key={ref} value={ref}>
                      {ref}
                    </option>
                  ))}
                </NativeSelect>
              </>
            )}
          </div>
        )}

        <div className="space-y-1.5">
          <Label htmlFor="question">Question</Label>
          <Textarea
            id="question"
            rows={4}
            placeholder="지난 30일 서울 결제 매출 알려줘"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
            }}
          />
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="reference-time">Reference time (optional)</Label>
          <Input
            id="reference-time"
            placeholder="now · e.g. 2026-10-02T09:00:00+09:00"
            value={referenceTime}
            onChange={(e) => setReferenceTime(e.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            Resolves relative periods (“last 30 days”) into absolute parameters. ISO 8601 with UTC
            offset. Empty means the server's current time in the context timezone.
          </p>
        </div>

        <Button onClick={submit} disabled={!contextId || !question.trim() || generate.isPending}>
          <Sparkles /> {generate.isPending ? "Generating…" : "Generate operation"}
        </Button>
        <span className="ml-2 text-xs text-muted-foreground">⌘/Ctrl + Enter</span>
      </section>

      <section className="min-w-0 space-y-3">
        {generate.variables && submittedContext && (
          <div className="space-y-2 rounded-md border p-3 text-sm" aria-label="Submitted request">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="font-medium">
                {generate.isPending ? "Request in progress" : "Request for this result"}
              </p>
              <CopyButton
                label="Copy API request"
                text={generateRequestCurl(
                  generate.variables,
                  window.location.origin,
                  !!getApiKey(),
                )}
              />
            </div>
            <p className="text-xs text-muted-foreground">
              Copies a curl command for this submitted request. Re-running may produce a different
              result.
              {generate.variables.expected_context_version
                ? " This request requires the same Context version."
                : " Re-running uses the latest Context."}
              {getApiKey() &&
                " Set SIDEWIT_API_KEY in your shell before running; your saved key is not copied."}
            </p>
            <p className="whitespace-pre-wrap break-words">{generate.variables.question}</p>
            {!!generate.variables.clarification_answers?.length && (
              <dl className="space-y-2" aria-label="Submitted clarification answers">
                {generate.variables.clarification_answers.map((a) => (
                  <div key={a.id}>
                    <dt className="text-xs text-muted-foreground">{a.question}</dt>
                    <dd className="whitespace-pre-wrap break-words">{a.answer}</dd>
                  </div>
                ))}
              </dl>
            )}
            <p className="text-xs text-muted-foreground">
              Context: {submittedContext.name} · v{resultVersion}
            </p>
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
              <dt>Reference time</dt>
              <dd className="break-words">
                {generate.variables.reference_time ??
                  (generate.data?.status === "ambiguous"
                    ? generate.data.reference_time
                    : undefined) ??
                  "Server time when this request was made"}
              </dd>
              <dt>Metric</dt>
              <dd>
                {generate.variables.metric_id ??
                  (generate.variables.require_metric
                    ? "Registered metrics only"
                    : "Saved Tool first, then generation")}
              </dd>
              <dt>Date basis</dt>
              <dd>{generate.variables.date_field ?? "Question or contract default"}</dd>
            </dl>
            {inputsChanged && (
              <p role="status" className="text-amber-800">
                Inputs or Context changed since this request. The result belongs to the request
                shown here. Generate again to use your current inputs.
              </p>
            )}
          </div>
        )}
        {generate.isPending && <Loading label="Generating and validating…" />}
        {!generate.isPending && generate.error && <GenerateError error={generate.error} />}
        {continuation && (
          <ClarificationForm
            key={continuation.result.generation_id}
            result={continuation.result}
            pending={generate.isPending}
            blockedReason={clarificationBlock}
            onContinue={continueWith}
          />
        )}
        {!generate.isPending && generate.data && !continuation && (
          <GenerateResult result={generate.data} inputsChanged={inputsChanged} />
        )}
        {!generate.isPending && !generate.data && !generate.error && (
          <Empty>
            The generated operation, or why Sidewit could not produce one, appears here.
          </Empty>
        )}
      </section>
    </div>
  );
}

function StatusHeader({
  status,
  generationId,
  contextVersion,
}: {
  status: string;
  generationId: string;
  contextVersion?: number;
}) {
  const styles: Record<string, string> = {
    ok: "bg-emerald-600 text-white",
    ambiguous: "bg-amber-500 text-white",
    unanswerable: "bg-slate-600 text-white",
    rejected: "bg-red-600 text-white",
  };
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Badge className={styles[status]}>{status}</Badge>
      <span className="font-mono text-xs text-muted-foreground">
        generation {generationId.slice(0, 8)}
        {contextVersion !== undefined && ` · context v${contextVersion}`}
      </span>
    </div>
  );
}

function GenerateResult({
  result,
  inputsChanged,
}: {
  result: GenerateResponse;
  inputsChanged: boolean;
}) {
  const navigate = useNavigate();
  if (result.status === "ambiguous") {
    return (
      <Card className="border-amber-300">
        <CardHeader>
          <StatusHeader
            status="ambiguous"
            generationId={result.generation_id}
            contextVersion={result.context_version}
          />
          <CardTitle className="flex items-center gap-2 text-base">
            <HelpCircle className="size-4 text-amber-600" /> The question needs clarification
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <p>{result.reason}</p>
          <div>
            <div className="mb-1 text-xs font-medium">Ask the user</div>
            <ul className="list-disc space-y-1 pl-5">
              {result.clarifications.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          </div>
          <p className="text-xs text-muted-foreground">
            No SQL was generated. Rephrase the question or add descriptions/business rules to the
            context.
          </p>
        </CardContent>
      </Card>
    );
  }
  if (result.status === "unanswerable") {
    return (
      <Card>
        <CardHeader>
          <StatusHeader
            status="unanswerable"
            generationId={result.generation_id}
            contextVersion={result.context_version}
          />
          <CardTitle className="text-base">The context cannot answer this question</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          <p>{result.reason}</p>
          <p className="text-xs text-muted-foreground">No SQL was generated.</p>
        </CardContent>
      </Card>
    );
  }
  return (
    <Card>
      <CardHeader>
        <StatusHeader
          status="ok"
          generationId={result.generation_id}
          contextVersion={result.context_version}
        />
        <CardTitle className="text-base">SQL operation</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <SourceBadge source={result.source} />
        {result.selected_tool && (
          <p className="text-sm">
            Tool:{" "}
            <Link
              className="underline"
              to={`/tools/${encodeURIComponent(result.selected_tool.name)}`}
            >
              {result.selected_tool.name}
            </Link>{" "}
            · v{result.selected_tool.version}
          </p>
        )}
        {result.metric_binding && (
          <p className="text-sm">
            Metric contract: <code>{result.metric_binding.metric_id}</code>
            {result.metric_binding.time_field && (
              <>
                {" "}
                · date basis: <code>{result.metric_binding.time_field}</code>
              </>
            )}
          </p>
        )}
        <OperationView operation={result.operation} />
        <ValidationPanel
          result={result.validation}
          label={
            result.metric_binding
              ? "SQL and metric contract validated"
              : "SQL structurally validated"
          }
        />
        <div className="flex flex-wrap items-center gap-2">
          {result.source !== "tool" && (
            <Button onClick={() => navigate(`/tools/new?generation=${result.generation_id}`)}>
              <Wrench /> {inputsChanged ? "Create draft from previous result" : "Create Tool draft"}
            </Button>
          )}
          {result.tool_suggestion && (
            <span className="text-xs text-muted-foreground">
              suggested: <code className="font-mono">{result.tool_suggestion.name}</code>
            </span>
          )}
        </div>
        {result.source !== "tool" && (
          <p className="text-xs text-muted-foreground">
            Generation history is temporary. Create and save a Tool before this record expires or
            the server restarts.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function GenerateError({ error }: { error: unknown }) {
  const [showSql, setShowSql] = useState(false);
  const rejected = error instanceof ApiError && error.error === "generation_invalid";
  const generationId =
    rejected && typeof error.details.generation_id === "string"
      ? error.details.generation_id
      : undefined;
  const log = useGeneration(showSql ? generationId : undefined);
  const statement = log.data?.operation?.statement;

  if (!rejected) return <ErrorAlert error={error} title="Generation failed" />;
  return (
    <Card className="border-red-300">
      <CardHeader>
        <StatusHeader
          status="rejected"
          generationId={generationId ?? ""}
          contextVersion={log.data?.context_version}
        />
        <CardTitle className="flex items-center gap-2 text-base">
          <XCircle className="size-4 text-red-600" /> Rejected generation
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <p>
          The generated SQL failed structural validation, and so did the one repair attempt. The API
          returned <strong>no operation</strong>, so an application cannot use unvalidated SQL by
          mistake.
        </p>
        <IssueList issues={error.issues("validation_errors")} />
        <button
          className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          aria-expanded={showSql}
          onClick={() => setShowSql((v) => !v)}
        >
          {showSql ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
          Show rejected SQL (debugging information from the generation log)
        </button>
        {showSql && (
          <div className="space-y-2 rounded-md border-2 border-dashed border-red-300 bg-red-50/50 p-3">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="destructive">Not validated · Do not execute</Badge>
              <span className="text-xs text-muted-foreground">
                Not an operation and not part of the API response. Shown only to debug the context
                or prompt.
              </span>
            </div>
            {!generationId ? (
              <p>No generation log is available for this error.</p>
            ) : log.isLoading ? (
              <Loading />
            ) : log.error ? (
              <ErrorAlert error={log.error} />
            ) : typeof statement === "string" && statement.length > 0 ? (
              <pre
                aria-label="Rejected SQL"
                className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md border bg-background p-3 font-mono text-xs"
              >
                <code>{statement}</code>
              </pre>
            ) : (
              <p>No rejected SQL was recorded for this generation.</p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function SourceBadge({ source }: { source: OperationSource }) {
  if (source === "tool")
    return (
      <p className="flex flex-wrap items-center gap-2 text-sm">
        <Badge className="bg-emerald-600 text-white">saved Tool</Badge>
        <span className="text-muted-foreground">
          Reused saved SQL. Review the model-selected Tool and arguments.
        </span>
      </p>
    );
  return source === "metric" ? (
    <p className="flex items-center gap-2 text-sm">
      <Badge className="bg-emerald-600 text-white">metric contract</Badge>
      <span className="text-muted-foreground">
        The server built this SQL from a registered metric.
      </span>
    </p>
  ) : (
    <p className="flex items-center gap-2 text-sm">
      <Badge className="bg-amber-500 text-white">model-written SQL</Badge>
      <span className="text-muted-foreground">
        Structurally validated only; review that it means what was asked.
      </span>
    </p>
  );
}
