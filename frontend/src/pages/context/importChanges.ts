import type { ContextDefinition, ContextField } from "@/api/types";

function fieldValue(field: ContextField) {
  return {
    name: field.name,
    type: field.type,
    description: field.description ?? "",
    primary_key: field.primary_key ?? false,
    nullable: field.nullable ?? true,
    allowed_values: field.allowed_values ?? [],
  };
}

export function importChanges(before: ContextDefinition, after: ContextDefinition) {
  const added: string[] = [];
  const updated: { name: string; details: string[] }[] = [];
  const removed: string[] = [];
  const oldTables = new Map(before.resources.map((r) => [r.name.toLowerCase(), r]));
  for (const table of after.resources) {
    const old = oldTables.get(table.name.toLowerCase());
    if (!old) {
      added.push(`${table.name} · ${table.fields.length} columns`);
      continue;
    }
    oldTables.delete(table.name.toLowerCase());
    const fields = new Map(old.fields.map((f) => [f.name.toLowerCase(), f]));
    const details: string[] = [];
    for (const field of table.fields) {
      const previous = fields.get(field.name.toLowerCase());
      fields.delete(field.name.toLowerCase());
      if (!previous) details.push(`Add column ${field.name} (${field.type})`);
      else if (JSON.stringify(fieldValue(previous)) !== JSON.stringify(fieldValue(field))) {
        const changes: string[] = [];
        if (previous.type !== field.type) changes.push(`${previous.type} → ${field.type}`);
        if ((previous.nullable ?? true) !== (field.nullable ?? true))
          changes.push(field.nullable ? "nullable" : "not null");
        if (!!previous.primary_key !== !!field.primary_key)
          changes.push(field.primary_key ? "primary key" : "remove primary key");
        if (
          JSON.stringify(previous.allowed_values ?? []) !==
          JSON.stringify(field.allowed_values ?? [])
        )
          changes.push(
            `allowed values: ${JSON.stringify(previous.allowed_values ?? [])} → ${JSON.stringify(field.allowed_values ?? [])}`,
          );
        if ((previous.description ?? "") !== (field.description ?? ""))
          changes.push("description changed");
        if (previous.name !== field.name) changes.push(`name: ${previous.name} → ${field.name}`);
        details.push(`${field.name}: ${changes.join("; ")}`);
      }
    }
    for (const field of fields.values()) details.push(`Remove column ${field.name}`);
    if ((old.description ?? "") !== (table.description ?? ""))
      details.push("Table description changed");
    if (old.name !== table.name) details.push(`Table name: ${old.name} → ${table.name}`);
    if (details.length) updated.push({ name: table.name, details });
  }
  for (const table of oldTables.values()) removed.push(table.name);
  const relationKey = (r: { from: string; to: string }) => `${r.from} → ${r.to}`;
  const oldRelations = new Set(before.relations.map(relationKey));
  const newRelations = new Set(after.relations.map(relationKey));
  const relationsAdded = [...newRelations].filter((r) => !oldRelations.has(r));
  const relationsRemoved = [...oldRelations].filter((r) => !newRelations.has(r));
  return {
    added,
    updated,
    removed,
    relationsAdded,
    relationsRemoved,
    unchanged: after.resources.length - added.length - updated.length,
    changed: !!(
      added.length +
      updated.length +
      removed.length +
      relationsAdded.length +
      relationsRemoved.length
    ),
  };
}
