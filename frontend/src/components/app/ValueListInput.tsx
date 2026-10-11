import { Plus, X } from "lucide-react";

import type { ParamType, ScalarValue } from "@/api/types";
import { Button } from "@/components/ui/button";
import { ScalarInput } from "@/components/app/ScalarInput";

/**
 * Edits a value list with one ScalarInput per value, so every value (empty strings, commas,
 * surrounding spaces, line breaks) round-trips exactly; nothing is split or joined.
 */
export function ValueListInput({
  values,
  type,
  label,
  disabled,
  onChange,
}: {
  values: readonly ScalarValue[] | null | undefined;
  type: ParamType;
  label: string;
  disabled?: boolean;
  onChange: (values: ScalarValue[]) => void;
}) {
  const list = values ?? [];
  return (
    <div className="space-y-1">
      {list.map((value, index) => (
        <div key={index} className="flex items-center gap-1">
          <ScalarInput
            className="h-7 font-mono text-xs md:text-xs"
            aria-label={`${label} ${index + 1}`}
            disabled={disabled}
            type={type}
            value={value}
            onChange={(next) => onChange(list.map((v, i) => (i === index ? next : v)))}
          />
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={`Remove ${label} ${index + 1}`}
            disabled={disabled}
            onClick={() => onChange(list.filter((_, i) => i !== index))}
          >
            <X />
          </Button>
        </div>
      ))}
      <Button variant="ghost" size="xs" disabled={disabled} onClick={() => onChange([...list, ""])}>
        <Plus /> Add value
      </Button>
    </div>
  );
}
