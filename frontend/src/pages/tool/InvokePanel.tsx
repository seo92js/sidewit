import { parseScalarInput } from "@/lib/scalars";
import { Play } from "lucide-react";
import { useState } from "react";
import { useLocation } from "react-router";
import { ApiError } from "@/api/client";
import { useInvokeTool } from "@/api/queries";
import type { Issue, Tool } from "@/api/types";
import { ErrorAlert, IssueList, ValidationPanel } from "@/components/app/common";
import { NativeSelect } from "@/components/app/NativeSelect";
import { OperationView } from "@/components/app/OperationView";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { orderedInputs, paramType } from "./inputSchema";

// undefined: not entered yet. "" is a real string value (an enum may list it).
type FieldValue = string | boolean | undefined;

export function InvokePanel({ tool, dirty }: { tool: Tool; dirty: boolean }) {
  const location = useLocation();
  const invoke = useInvokeTool(tool.name, tool.version);
  const inputs = orderedInputs(tool.input_schema);
  const example =
    (location.state as { exampleArguments?: Record<string, unknown> } | null)?.exampleArguments ??
    {};
  const [inputErrors, setInputErrors] = useState<Record<string, string>>({});
  const [values, setValues] = useState<Record<string, FieldValue>>(() =>
    Object.fromEntries(
      inputs.map((name) => {
        const v = example[name];
        return [
          name,
          typeof v === "boolean"
            ? v
            : v === undefined
              ? paramType(tool.input_schema.properties[name]) === "boolean"
                ? false
                : undefined
              : String(v),
        ];
      }),
    ),
  );

  // invalid_arguments issues are shown under their input; only the rest go to the alert.
  // Other errors (e.g. 409 tool_invalid) are shown by the alert as they are.
  const fieldErrors: Record<string, Issue[]> = {};
  let otherIssues: Issue[] | undefined;
  if (invoke.error instanceof ApiError && invoke.error.error === "invalid_arguments") {
    otherIssues = [];
    for (const issue of invoke.error.issues("errors")) {
      if (issue.ref && inputs.includes(issue.ref)) (fieldErrors[issue.ref] ??= []).push(issue);
      else otherIssues.push(issue);
    }
  }
  const allShownPerField = otherIssues?.length === 0 && Object.keys(fieldErrors).length > 0;

  const submit = () => {
    const args: Record<string, unknown> = {};
    const errors: Record<string, string> = {};
    for (const name of inputs) {
      const type = paramType(tool.input_schema.properties[name]);
      const raw = values[name] ?? (type === "boolean" ? false : undefined);
      // Every input is required; only a string input can take the empty string.
      if (raw === undefined || (raw === "" && type !== "string")) {
        errors[name] = "This input is required.";
        continue;
      }
      const parsed = parseScalarInput(String(raw), type);
      if (parsed.error) errors[name] = parsed.error;
      else args[name] = parsed.value;
    }
    setInputErrors(errors);
    if (Object.keys(errors).length) return;
    invoke.mutate(args);
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Invoke Tool</CardTitle>
        <p className="text-xs text-muted-foreground">
          Resolves arguments into the saved v{tool.version} operation. No LLM, no SQL execution.
          {dirty && " Unsaved edits are not used."}
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {inputs.length === 0 && (
          <p className="text-sm text-muted-foreground">This tool has no inputs.</p>
        )}
        {inputs.map((name) => {
          const prop = tool.input_schema.properties[name];
          const type = paramType(prop);
          const value = values[name];
          const setValue = (v: FieldValue) => {
            setValues((current) => ({ ...current, [name]: v }));
            setInputErrors((current) => ({ ...current, [name]: "" }));
            invoke.reset();
          };
          const id = `arg-${name}`;
          return (
            <div key={name} className="space-y-1">
              <Label htmlFor={id} className="flex items-baseline gap-2 text-xs">
                <span className="font-mono">{name}</span>
                <span className="font-mono text-muted-foreground">{type}</span>
              </Label>
              {prop.description && (
                <p className="text-xs text-muted-foreground">{prop.description}</p>
              )}
              {type === "boolean" ? (
                <Checkbox
                  id={id}
                  checked={value === true}
                  onCheckedChange={(v) => setValue(v === true)}
                />
              ) : prop.enum ? (
                <NativeSelect
                  id={id}
                  className="w-full"
                  // Options are keyed by index: "" is a valid enum value, not "unselected".
                  value={value === undefined ? "" : String(prop.enum.indexOf(String(value)))}
                  onChange={(e) =>
                    setValue(
                      e.target.value === "" ? undefined : prop.enum?.[Number(e.target.value)],
                    )
                  }
                >
                  <option value="">—</option>
                  {prop.enum.map((v, i) => (
                    <option key={i} value={String(i)}>
                      {v === "" ? "(empty string)" : v}
                    </option>
                  ))}
                </NativeSelect>
              ) : (
                <Input
                  id={id}
                  className="font-mono"
                  type={
                    type === "date"
                      ? "date"
                      : type === "integer" || type === "number"
                        ? "number"
                        : "text"
                  }
                  step={type === "number" ? "any" : undefined}
                  placeholder={type === "date-time" ? "2026-09-02T00:00:00+09:00" : undefined}
                  value={String(value ?? "")}
                  onChange={(e) => setValue(e.target.value)}
                />
              )}
              {inputErrors[name] && (
                <p role="alert" className="text-xs text-destructive">
                  {inputErrors[name]}
                </p>
              )}
              {fieldErrors[name] && <IssueList issues={fieldErrors[name]} />}
            </div>
          );
        })}
        <Button onClick={submit} disabled={invoke.isPending}>
          <Play /> {invoke.isPending ? "Resolving…" : "Invoke Tool"}
        </Button>
        <ErrorAlert
          error={allShownPerField ? null : invoke.error}
          issues={otherIssues}
          title={
            invoke.error instanceof ApiError && invoke.error.error === "tool_invalid"
              ? "Tool is invalid (409)"
              : "Invoke failed"
          }
        />
        {invoke.data && (
          <div className="space-y-2">
            <div className="text-sm font-medium">Resolved SQL operation</div>
            <p className="text-xs text-muted-foreground">
              tool v{invoke.data.tool_version} · context v{invoke.data.context_version}. Run it with
              your application's database driver, binding parameters by name.
            </p>
            <ValidationPanel result={invoke.data.validation} label="Saved Tool checks" />
            <OperationView operation={invoke.data.operation} order={inputs} />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
