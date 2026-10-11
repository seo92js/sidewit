import { fieldInputType, scalarValueError } from "@/lib/scalars";
import { ScalarInput } from "@/components/app/ScalarInput";
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";
import type { ContextDefinition, Comparison, FixedCondition } from "@/api/types";
import { NativeSelect } from "@/components/app/NativeSelect";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

export function RulesEditor({
  definition,
  onChange,
}: {
  definition: ContextDefinition;
  onChange: (d: ContextDefinition) => void;
}) {
  const rules = definition.rules;
  const setRules = (next: typeof rules) => onChange({ ...definition, rules: next });
  const nextId = () => {
    let n = rules.length + 1;
    while (rules.some((r) => r.id === `rule_${n}`)) n += 1;
    return `rule_${n}`;
  };
  const move = (index: number, delta: number) => {
    const next = [...rules];
    const [rule] = next.splice(index, 1);
    next.splice(index + delta, 0, rule);
    setRules(next);
  };

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        Rules guide generation (for example “revenue only includes completed orders”). Prose alone
        is not enforced. When a rule fixes a value, also set its <strong>constant</strong> (field,
        operator, value): the server then requires generated SQL to keep it as a literal, never as a
        caller input.
      </p>
      {rules.length === 0 && <p className="text-sm text-muted-foreground">No business rules.</p>}
      {rules.map((rule, index) => (
        <div key={index} className="flex items-start gap-2">
          <Input
            className="h-8 w-28 font-mono"
            aria-label="Rule id"
            value={rule.id}
            onChange={(e) =>
              setRules(rules.map((r, i) => (i === index ? { ...r, id: e.target.value } : r)))
            }
          />
          <div className="flex-1 space-y-1.5">
            <Textarea
              rows={2}
              aria-label="Rule text"
              value={rule.text}
              onChange={(e) =>
                setRules(rules.map((r, i) => (i === index ? { ...r, text: e.target.value } : r)))
              }
            />
            <RuleConditionEditor
              definition={definition}
              condition={rule.condition ?? null}
              onChange={(condition) =>
                setRules(rules.map((r, i) => (i === index ? { ...r, condition } : r)))
              }
            />
          </div>
          <div className="flex flex-col">
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label="Move up"
              disabled={index === 0}
              onClick={() => move(index, -1)}
            >
              <ArrowUp />
            </Button>
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label="Move down"
              disabled={index === rules.length - 1}
              onClick={() => move(index, 1)}
            >
              <ArrowDown />
            </Button>
          </div>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Remove rule"
            onClick={() => setRules(rules.filter((_, i) => i !== index))}
          >
            <Trash2 />
          </Button>
        </div>
      ))}
      <Button
        variant="outline"
        size="sm"
        onClick={() => setRules([...rules, { id: nextId(), text: "" }])}
      >
        <Plus /> Add rule
      </Button>
    </div>
  );
}

const COMPARISONS: Comparison[] = ["=", "!=", ">", ">=", "<", "<="];

function RuleConditionEditor({
  definition,
  condition,
  onChange,
}: {
  definition: ContextDefinition;
  condition: FixedCondition | null;
  onChange: (condition: FixedCondition | null) => void;
}) {
  const fields = definition.resources.flatMap((r) =>
    r.fields.map((f) => ({ ref: `${r.name}.${f.name}`, field: f })),
  );
  if (!condition) {
    return (
      <Button
        variant="ghost"
        size="xs"
        disabled={fields.length === 0}
        onClick={() => onChange({ field: fields[0].ref, op: "=", value: "" })}
      >
        <Plus /> Add constant
      </Button>
    );
  }
  const target = fields.find((f) => f.ref === condition.field)?.field;
  const inputType = fieldInputType(target?.type ?? "text");
  const valueError = scalarValueError(condition.value, inputType);
  const allowed = target?.allowed_values ?? [];

  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className="text-muted-foreground">Constant</span>
      <NativeSelect
        className="h-7 font-mono"
        aria-label="Constant field"
        value={condition.field}
        onChange={(e) => onChange({ ...condition, field: e.target.value, value: "" })}
      >
        {fields.map((f) => (
          <option key={f.ref} value={f.ref}>
            {f.ref}
          </option>
        ))}
      </NativeSelect>
      <NativeSelect
        className="h-7 font-mono"
        aria-label="Constant operator"
        value={condition.op ?? "="}
        onChange={(e) => onChange({ ...condition, op: e.target.value as Comparison })}
      >
        {COMPARISONS.map((op) => (
          <option key={op} value={op}>
            {op}
          </option>
        ))}
      </NativeSelect>
      {allowed.length > 0 ? (
        <NativeSelect
          className="h-7 font-mono"
          aria-label="Constant value"
          // Keyed by index: "" can be an allowed value, distinct from "not chosen".
          value={String(allowed.findIndex((v) => Object.is(v, condition.value)))}
          onChange={(e) => onChange({ ...condition, value: allowed[Number(e.target.value)] })}
        >
          <option value="-1" disabled>
            —
          </option>
          {allowed.map((v, i) => (
            <option key={i} value={String(i)}>
              {v === "" ? "(empty string)" : String(v)}
            </option>
          ))}
        </NativeSelect>
      ) : (
        <ScalarInput
          className="h-7 w-40 font-mono"
          aria-label="Constant value"
          value={condition.value}
          type={inputType}
          onChange={(value) => onChange({ ...condition, value })}
        />
      )}
      {valueError && (
        <span role="alert" className="text-destructive">
          {valueError}
        </span>
      )}
      <Button
        variant="ghost"
        size="icon-xs"
        aria-label="Remove constant"
        onClick={() => onChange(null)}
      >
        <Trash2 />
      </Button>
    </div>
  );
}

/**
 * Keeps the typed text, so "0." or "1e5" are never rewritten. While typing, only text that
 * parses back to itself is committed as a number; after an edit, blur commits any valid number.
 */
