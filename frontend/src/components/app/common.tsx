import { AlertCircle, Check, Copy, Loader2 } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { ApiError } from "@/api/client";
import type { Issue, ValidationResult } from "@/api/types";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
      <Loader2 className="size-4 animate-spin" /> {label}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
      {children}
    </div>
  );
}

export function IssueList({
  issues,
  tone = "error",
}: {
  issues: Issue[];
  tone?: "error" | "warning";
}) {
  if (!issues.length) return null;
  return (
    <ul className="space-y-1 text-sm">
      {issues.map((issue, i) => (
        <li key={i} className="flex flex-wrap items-baseline gap-2">
          <code
            className={cn(
              "rounded px-1.5 py-0.5 font-mono text-xs",
              tone === "error"
                ? "bg-destructive/10 text-destructive"
                : "bg-amber-100 text-amber-900",
            )}
          >
            {issue.code}
          </code>
          <span>{issue.message}</span>
          {issue.ref && (
            <code className="font-mono text-xs text-muted-foreground">{issue.ref}</code>
          )}
        </li>
      ))}
    </ul>
  );
}

const ERROR_HINTS: Record<string, string> = {
  version_conflict:
    "Someone else saved this context first. Reload to get the latest version, then re-apply your edits.",
  unauthorized: "Set a valid Sidewit API key with the key button in the header.",
  network_error: "Check that the API is running.",
  llm_not_configured:
    "Configure SIDEWIT_LLM_PROVIDER, SIDEWIT_LLM_MODEL and the provider API key on the server.",
  context_has_tools: "Delete the tools that use this context first.",
};

/** Renders any API error, including the issue lists carried in `details`. */
export function ErrorAlert({
  error,
  title,
  action,
  issues: shownIssues,
}: {
  error: unknown;
  title?: string;
  action?: ReactNode;
  /** Replaces the error's own issue list, e.g. when some issues are shown elsewhere. */
  issues?: Issue[];
}) {
  if (!error) return null;
  const apiError = error instanceof ApiError ? error : null;
  const issues =
    shownIssues ??
    (apiError ? [...apiError.issues("validation_errors"), ...apiError.issues("errors")] : []);
  const requestErrors =
    apiError?.error === "invalid_request"
      ? (apiError.details.errors as { loc: (string | number)[]; message: string }[] | undefined)
      : undefined;
  const tools = apiError?.details.tools as string[] | undefined;
  return (
    <Alert variant="destructive">
      <AlertCircle />
      <AlertTitle>
        {title ?? "Request failed"}
        {apiError && (
          <code className="ml-2 font-mono text-xs">
            {apiError.status || ""} {apiError.error}
          </code>
        )}
      </AlertTitle>
      <AlertDescription className="space-y-2">
        <p>{error instanceof Error ? error.message : String(error)}</p>
        {apiError && ERROR_HINTS[apiError.error] && (
          <p className="text-muted-foreground">{ERROR_HINTS[apiError.error]}</p>
        )}
        {issues.length > 0 && <IssueList issues={issues} />}
        {requestErrors && (
          <ul className="list-disc pl-5 text-sm">
            {requestErrors.map((e, i) => (
              <li key={i}>
                <code className="font-mono text-xs">
                  {e.loc.filter((l) => l !== "body").join(".")}
                </code>
                : {e.message}
              </li>
            ))}
          </ul>
        )}
        {tools && <p>Tools: {tools.join(", ")}</p>}
        {action}
      </AlertDescription>
    </Alert>
  );
}

export function ValidationPanel({ result, label }: { result: ValidationResult; label: string }) {
  return (
    <div className="space-y-2 rounded-md border p-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        {result.valid ? (
          <Badge className="bg-emerald-600 text-white">✓ {label}</Badge>
        ) : (
          <Badge variant="destructive">✗ {label}</Badge>
        )}
        <span className="text-xs text-muted-foreground">checks: {result.checks.join(", ")}</span>
      </div>
      <IssueList issues={result.errors} />
      {result.warnings.length > 0 && (
        <div>
          <div className="mb-1 text-xs font-medium text-amber-900">Warnings</div>
          <IssueList issues={result.warnings} tone="warning" />
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        Structural validation only: syntax, statement policy, resources/fields and the parameter
        contract. SELECT statements can still have side effects. These checks do not establish
        execution safety, access permissions, query cost or business correctness.
      </p>
    </div>
  );
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const [fallback, setFallback] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  return (
    <Dialog
      open={fallback !== null}
      onOpenChange={(open) => {
        if (!open) setFallback(null);
      }}
    >
      <DialogTrigger asChild onClick={(event) => event.preventDefault()}>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={async () => {
            clearTimeout(timer.current);
            setCopied(false);
            try {
              await navigator.clipboard.writeText(text);
              setCopied(true);
              timer.current = setTimeout(() => setCopied(false), 1200);
            } catch {
              setFallback(text);
            }
          }}
        >
          {copied ? <Check /> : <Copy />} {copied ? "Copied" : label}
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Copy manually</DialogTitle>
          <DialogDescription>
            Clipboard access failed. Select the text below and press ⌘/Ctrl+C, or use your device's
            copy menu.
          </DialogDescription>
        </DialogHeader>
        <Textarea
          aria-label={`Text to copy: ${label}`}
          readOnly
          rows={10}
          className="font-mono text-xs"
          value={fallback ?? ""}
          onFocus={(event) => event.currentTarget.select()}
        />
      </DialogContent>
    </Dialog>
  );
}

export function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="max-h-80 overflow-auto rounded-md border bg-muted/40 p-3 font-mono text-xs leading-relaxed">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function SectionTitle({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="mb-2 flex items-center justify-between gap-2">
      <h2 className="text-sm font-semibold">{children}</h2>
      {action}
    </div>
  );
}
