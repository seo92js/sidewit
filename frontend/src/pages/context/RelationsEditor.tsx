import { Plus, Trash2 } from "lucide-react";
import type { ContextDefinition } from "@/api/types";
import { NativeSelect } from "@/components/app/NativeSelect";
import { Button } from "@/components/ui/button";

export function RelationsEditor({
  definition,
  onChange,
}: {
  definition: ContextDefinition;
  onChange: (d: ContextDefinition) => void;
}) {
  const refs = definition.resources.flatMap((r) => r.fields.map((f) => `${r.name}.${f.name}`));
  const options = (value: string) => (refs.includes(value) || !value ? refs : [value, ...refs]);
  const set = (index: number, key: "from" | "to", value: string) =>
    onChange({
      ...definition,
      relations: definition.relations.map((r, i) => (i === index ? { ...r, [key]: value } : r)),
    });

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        <code>from</code> references <code>to</code> (e.g. a foreign key). Joins on undeclared pairs
        are allowed but produce an <code>undeclared_relation</code> warning.
      </p>
      {definition.relations.length === 0 && (
        <p className="text-sm text-muted-foreground">No relations.</p>
      )}
      {definition.relations.map((relation, index) => (
        <div key={index} className="flex flex-wrap items-center gap-2">
          <NativeSelect
            className="font-mono"
            aria-label="From"
            value={relation.from}
            onChange={(e) => set(index, "from", e.target.value)}
          >
            {options(relation.from).map((ref) => (
              <option key={ref} value={ref}>
                {ref}
              </option>
            ))}
          </NativeSelect>
          <span className="text-muted-foreground">→</span>
          <NativeSelect
            className="font-mono"
            aria-label="To"
            value={relation.to}
            onChange={(e) => set(index, "to", e.target.value)}
          >
            {options(relation.to).map((ref) => (
              <option key={ref} value={ref}>
                {ref}
              </option>
            ))}
          </NativeSelect>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Remove relation"
            onClick={() =>
              onChange({
                ...definition,
                relations: definition.relations.filter((_, i) => i !== index),
              })
            }
          >
            <Trash2 />
          </Button>
        </div>
      ))}
      <Button
        variant="outline"
        size="sm"
        disabled={refs.length < 2}
        onClick={() =>
          onChange({
            ...definition,
            relations: [...definition.relations, { from: refs[0], to: refs[1] }],
          })
        }
      >
        <Plus /> Add relation
      </Button>
    </div>
  );
}
