import { Plus, Trash2 } from "lucide-react";
import type { ContextDefinition, ContextField, Resource } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { ValueListInput } from "@/components/app/ValueListInput";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { fieldInputType, setFieldType } from "@/lib/scalars";

export function SchemaEditor({
  definition,
  onChange,
}: {
  definition: ContextDefinition;
  onChange: (d: ContextDefinition) => void;
}) {
  const setResource = (index: number, resource: Resource) =>
    onChange({
      ...definition,
      resources: definition.resources.map((r, i) => (i === index ? resource : r)),
    });

  return (
    <div className="space-y-4">
      <p className="text-xs text-muted-foreground">
        <strong>Allowed values</strong> are only for enum-like fields (status, region, grade): list
        every value the field holds, spelled as stored. DDL import fills them from ENUM types and{" "}
        <code>CHECK (col IN (...))</code>. Generated SQL and Tool inputs must then use one of them
        exactly. Leave numbers, dates, ids and free text empty.
      </p>
      {definition.resources.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No resources. Import DDL or add a resource manually.
        </p>
      )}
      {definition.resources.map((resource, index) => (
        <div key={index} className="rounded-md border">
          <div className="flex flex-wrap items-center gap-2 border-b bg-muted/30 p-2">
            <Input
              className="h-7 w-48 font-mono"
              value={resource.name}
              aria-label="Resource name"
              onChange={(e) => setResource(index, { ...resource, name: e.target.value })}
            />
            <Input
              className="h-7 min-w-60 flex-1"
              placeholder="Resource description"
              value={resource.description ?? ""}
              onChange={(e) =>
                setResource(index, { ...resource, description: e.target.value || null })
              }
            />
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Remove resource"
              onClick={() =>
                onChange({
                  ...definition,
                  resources: definition.resources.filter((_, i) => i !== index),
                })
              }
            >
              <Trash2 />
            </Button>
          </div>
          <FieldsTable
            fields={resource.fields}
            onChange={(fields) => setResource(index, { ...resource, fields })}
            onTypeChange={(fieldIndex, type) =>
              onChange(setFieldType(definition, index, fieldIndex, type))
            }
          />
        </div>
      ))}
      <Button
        variant="outline"
        size="sm"
        onClick={() =>
          onChange({
            ...definition,
            resources: [
              ...definition.resources,
              {
                name: `resource_${definition.resources.length + 1}`,
                description: null,
                fields: [],
              },
            ],
          })
        }
      >
        <Plus /> Add resource
      </Button>
    </div>
  );
}

function FieldsTable({
  fields,
  onChange,
  onTypeChange,
}: {
  fields: ContextField[];
  onChange: (fields: ContextField[]) => void;
  /** Also re-types the field's allowed values and rule constants. */
  onTypeChange: (index: number, type: string) => void;
}) {
  const set = (index: number, field: ContextField) =>
    onChange(fields.map((f, i) => (i === index ? field : f)));
  return (
    <div className="p-2">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-40">Field</TableHead>
            <TableHead className="w-44">Type</TableHead>
            <TableHead className="w-12">PK</TableHead>
            <TableHead className="w-16">Nullable</TableHead>
            <TableHead>Description</TableHead>
            <TableHead
              className="w-48"
              title="Only for enum-like fields (status, region, grade): every value the field holds. Generated SQL and Tool inputs must use one of them exactly. Leave empty otherwise."
            >
              Allowed values (enum)
            </TableHead>
            <TableHead className="w-8" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {fields.map((field, index) => (
            <TableRow key={index}>
              <TableCell>
                <Input
                  className="h-7 font-mono"
                  aria-label="Field name"
                  value={field.name}
                  onChange={(e) => set(index, { ...field, name: e.target.value })}
                />
              </TableCell>
              <TableCell>
                <Input
                  className="h-7 font-mono"
                  aria-label="Field type"
                  value={field.type}
                  onChange={(e) => onTypeChange(index, e.target.value)}
                />
              </TableCell>
              <TableCell>
                <Checkbox
                  aria-label="Primary key"
                  checked={!!field.primary_key}
                  onCheckedChange={(v) => set(index, { ...field, primary_key: v === true })}
                />
              </TableCell>
              <TableCell>
                <Checkbox
                  aria-label="Nullable"
                  checked={field.nullable ?? true}
                  onCheckedChange={(v) => set(index, { ...field, nullable: v === true })}
                />
              </TableCell>
              <TableCell>
                <Input
                  className="h-7"
                  placeholder="What this field means"
                  value={field.description ?? ""}
                  onChange={(e) => set(index, { ...field, description: e.target.value || null })}
                />
              </TableCell>
              <TableCell>
                <ValueListInput
                  label="Allowed value"
                  type={fieldInputType(field.type)}
                  values={field.allowed_values}
                  onChange={(allowed_values) => set(index, { ...field, allowed_values })}
                />
              </TableCell>
              <TableCell>
                <Button
                  variant="ghost"
                  size="icon-xs"
                  aria-label="Remove field"
                  onClick={() => onChange(fields.filter((_, i) => i !== index))}
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
        onClick={() =>
          onChange([
            ...fields,
            { name: `field_${fields.length + 1}`, type: "text", nullable: true },
          ])
        }
      >
        <Plus /> Add field
      </Button>
    </div>
  );
}
