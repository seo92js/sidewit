# SQL metric contracts

Metric contracts define reviewed aggregates, joins, filters and period rules under
`Context.definition.metrics`. The model selects a contract and inputs; the server compiles SQL.
This guarantees the structure of the selected contract, not that the model interpreted the
question correctly. Sidewit never executes the resulting SQL.

## Choose a generation path

Saved Tools are considered first for ordinary questions. The table below describes generation
when no saved Tool matches. Explicit metric options bypass Tool selection.

| Configuration | Model output | Server behavior |
|---|---|---|
| No metrics | SQL, values and period interpretation | Static SQL and request-value checks |
| Metrics with `allow_generated_sql: true` | Metric selection or model-written SQL | Compile a matching metric; otherwise validate generated SQL |
| `allow_generated_sql: false`, `require_metric: true`, or structured metric options | Registered metric, inputs and supported period | Compile and validate against the contract |
| Bound Tool invoke | No model call | Check the saved contract and inputs; return an Operation |

Responses identify the path as `source: tool`, `source: metric` or `source: generated_sql`. A question that
adds a condition absent from a metric may require generated SQL. The prompt asks that path to
follow registered business definitions, but the compiler does not enforce them there. Use a
suitable contract and require the metric path when those guarantees are needed. Prose business
rules are not converted into mandatory filters; put required conditions in `fixed_filters`.

## Try the example

Start the stack with a real provider configured, then run from the repository root:

```bash
docker compose run --rm -T --no-deps -v ./examples/ecommerce:/examples:ro api python /examples/quickstart.py --metric-contracts --context-name metric_commerce --tool-name metric_revenue
```

This uses [metric-context.json](../examples/ecommerce/metric-context.json) and the ecommerce
DDL. Use fresh names for repeated runs. In the UI, edit contracts in the Context's **Metrics**
tab and select a metric/date field in Playground. The server checks contract integrity on save.

## Contract format

```json
{
  "id": "regional_revenue",
  "description": "Completed payment revenue by region, measured by paid_at.",
  "resource": "orders",
  "aggregate": {"kind": "sum", "field": "orders.amount"},
  "joins": [{"from": "orders.customer_id", "to": "customers.id"}],
  "group_by": [],
  "fixed_filters": [{"field": "orders.status", "op": "=", "value": "COMPLETED"}],
  "inputs": [{"name": "region", "type": "string", "field": "customers.region", "op": "="}],
  "time_fields": ["orders.paid_at", "orders.ordered_at"],
  "default_time_field": "orders.paid_at"
}
```

- `id` is snake_case, at most 64 characters and unique within its Context. `description`,
  `resource` and `aggregate` are required.
- `row_count` compiles to `COUNT(*)` and takes neither a field nor `distinct`.
  `non_null_count` uses `COUNT(field)`; `distinct: true` counts distinct non-null values.
  `sum`/`avg` require numeric fields; `min`/`max` support numeric, text and date/time categories.
- Joins are ordered INNER JOINs along declared Context relations, introducing one resource
  at each step. Review cardinality and fan-out: aggregation happens after the joins.
- Fixed filters are AND-connected scalar comparisons. Inputs are required scalar comparisons.
  Operators are `=`, `!=`, `>`, `>=`, `<`, `<=`; a field cannot be both a fixed filter and an input.
  `start_at` and `end_at` are reserved for compiled period bounds.
- `group_by` is a fixed list of fields. The aggregate's output alias is `value`.
- DDL import preserves metrics. Replacing referenced fields produces warnings; invalid
  contracts prevent Context save. Registered constants cannot be turned into parameters in
  generated SQL, but this check does not prove those conditions were included.

## Period selection

Generate accepts `metric_id`, `date_field` and `period` in addition to the usual question and
`reference_time`. A `date_field` requires `metric_id` and must be one of its allowed fields.
Invalid structured options return 422 before calling the provider. An explicit `period`
overrides the model's proposal.

An explicit request `date_field` takes precedence. Otherwise, the model returns a structured
`date_field` choice based on the question, clarification answers, field descriptions and contract
default. The server checks that the choice belongs to the selected metric's `time_fields`, using
existing case-insensitive reference resolution. It does not select fields by matching names in
answer text: a mentioned field may be excluded or requested only as an output column.

A null choice produces `ambiguous` when a period needs a field; the compiler does not silently
substitute the default. An invalid model-selected field triggers repair. The model is instructed
to use the default only when the user has not requested a different or unresolved basis.
This semantic interpretation still requires review. All-time questions do not require a date
field. Bounds are `[start_at, end_at)`.

To answer a clarification, resend the original request with `clarification_answers`, the returned
`reference_time` and `expected_context_version`. Preserve explicit metric options: they still take
precedence over free-text answers. See [continuing a request](CLARIFICATIONS.md).

| `period.kind` | Meaning in the Context timezone |
|---|---|
| `rolling_days` | `days` must be 1–3660. Timestamps: N local calendar days before `reference_time` through that time. Dates: the previous N dates, excluding today. DST means this need not be N × 24 hours. |
| `previous_day` | Previous local midnight through today's midnight |
| `previous_week` | Previous Monday through this Monday |
| `previous_month` | First of the previous month through first of this month |

Omit `days` or use null for non-rolling periods. For 30 rolling days at
`2026-10-02T09:00:00+09:00`, timestamp bounds are `2026-09-02T09:00:00+09:00` and
`2026-10-02T09:00:00+09:00`. The compiler does not use database `NOW()`. The application must
handle driver conversion for timestamps without time zone using the contract's timezone.

## Bound Tools

Metric Operations retain `metric_binding: {metric_id, time_field}` through Generation, draft,
save and invoke. A `source_generation_id` must still exist in memory when saving; otherwise save
returns 422 `invalid_source_generation`. A client cannot strip or replace its binding. Tool
updates cannot change the binding either; manually bound Tools receive the same contract checks.

The saved SQL AST and input types must match the compiled contract. Whitespace, comments and
identifier quoting that resolves to the same name are accepted; arbitrary SQL rewrites are not.
Context changes revalidate Tools, and incompatible invocation returns 409. Use
`POST /tools/{tool_id}/refresh-contract` with `expected_version` (UI: **Rebuild from contract**)
to rebuild against the current definition. SQL and input names/types are read-only in the bound
Tool editor. An unavailable date binding returns 409 `metric_contract_unavailable`; it is never
silently replaced. Contract edits that leave SQL and input types unchanged need no rebuild.

Refresh preserves input descriptions when types match. Context allowed values take precedence
over a manual enum; without them, the existing enum is retained. A conflicting enum prevents
save/revalidation with `input_enum_conflict`. Omitting an enum does not bypass current Context
value checks at invoke. The Tool version increases when its SQL or inputs actually change.

## Limits

A contract supports one aggregate, declared INNER JOINs, fixed scalar comparisons, required
inputs, grouping and the four periods above. Absolute ranges, OR/NULL filters, optional/array
inputs, outer/self/composite joins and multi-metric composition are not supported. Metric
selection does not resolve or invoke an existing Tool by name.

See the [evaluation guide](../evals/README.md) for metric and hybrid regression suites, and the
[specification](SPEC.md) for the complete parameter and validation contracts.
