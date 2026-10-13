# Sidewit v0.1 SPEC

> Sidewit is an AI Worker, not a database proxy.
> Sidewit turns context and natural-language intent into reviewable SQL operations.
> Your application stays in control of execution.

This document defines the implementation contract for Sidewit v0.1. If the implementation needs to diverge from it, update this document before implementing the change.

---

## 0. Core principles

1. **Sidewit's responsibility boundary.**
   - **Generate** uses a Context and a question to produce an **Operation draft for review**, explaining what was checked and what could not be verified.
   - A **Tool** reuses a reviewed, saved Operation and its input contract. The same inputs deterministically produce the same Operation, without guaranteeing database execution or business correctness.
   - The **calling application** controls database privileges, user and tenant access, whether to execute, and time and resource limits.
2. **Operation ≠ Tool.** An Operation is an execution plan. A Tool is a reusable interface combining a validated Operation template with an input contract (JSON Schema).
3. **Generated operations are probabilistic. Saved tools are deterministic.** `/generate` uses an LLM; `/tools/{name}/invoke` does not.
4. **Sidewit does not connect to any database.** It accepts no application database credentials and does not execute SQL or EXPLAIN. Its own state (Contexts and Tools) is stored as files in a data directory (§11).
5. **Sidewit admits what it cannot answer.** Unanswerable or ambiguous questions return `unanswerable` or `ambiguous` without producing SQL.
6. **Validation is static.** It parses SQL to check statement types, Context scope, names, parameter contracts and supported value policies (§7). It does not guarantee actual type/function compatibility, executability, cost, function side effects or Business Rule compliance. SELECT describes a statement type, not harmlessness. Checks stay within their supported static scope and reject input when a required check cannot be performed.
7. **LLM DTO ≠ Domain Model ≠ Public API DTO.** Provider-oriented formats (§6.2) are not exposed as public API contracts.

The license is Apache-2.0.

---

## 1. Scope

### Included in v0.1

- Data Context management: DDL import, editing descriptions, relations, Business Rules and metric contracts, and versioning
- `POST /generate`: select a saved Tool first; otherwise generate a parameterized SQL Operation draft or return ambiguous/unanswerable
- Static SQL Validator
- Tools: drafts from Generations, user editing, validated saves and deterministic invocation
- Static revalidation of associated Tools when a Context changes
- Evaluation: Golden Case, CLI
- UI: Context, Playground, Tools
- Docker deployment: containers and data volumes

### Not included in v0.1

Database connections (both application databases and a Sidewit database), SQL execution, EXPLAIN, concurrent writes from multiple instances, persistent Generation history, dependent Tool chaining, Vector, RAG, MCP, API Operations, Composite Operations, multiple parameter styles, optional/default/array parameters, automatic generalization of Operation parameters into Tool inputs, Tool version history, multitenancy and official support for dialects other than PostgreSQL.

---

## 2. Terminology

| Term | Definition |
|---|---|
| **Context** | Versioned metadata describing one data source, including resources, fields, relations, rules and metrics. |
| **Resource** | A table or view. |
| **Field** | A Resource column. |
| **Relation** | A reference between Fields, such as a foreign key. |
| **Rule** | A natural-language Business Rule included in the generation prompt. Only its optional `condition` participates in static checks. |
| **Operation** | An execution plan. v0.1 supports only `type: "sql"`. |
| **Generation** | The result of one `/generate` call. Only recent records are retained, in memory (§3.4). |
| **Tool** | A reusable interface with a name, description, `input_schema` and Operation template. |
| **Golden Case** | An evaluation question paired with expected results. |

---

## 3. Domain models

### 3.1 Context

Contents of a Context file (§11):

```json
{
  "id": "uuid",
  "name": "ecommerce",
  "dialect": "postgresql",
  "timezone": "Asia/Seoul",
  "allow_generated_sql": true,
  "version": 1,
  "created_at": "2026-10-11T00:00:00Z",
  "updated_at": "2026-10-11T00:00:00Z",
  "definition": { "...": "see below" }
}
```

- `name` is unique across Contexts. v0.1 accepts only `"postgresql"` for `dialect`. `timezone` is an IANA name used for relative date calculations (default: `UTC`).

Structure of `definition`:

```json
{
  "resources": [
    {
      "name": "orders",
      "description": "주문",
      "fields": [
        { "name": "id", "type": "bigint", "primary_key": true, "nullable": false, "description": null },
        { "name": "customer_id", "type": "bigint", "nullable": false },
        { "name": "amount", "type": "numeric(12,2)", "description": "실제 결제 금액" },
        { "name": "status", "type": "text", "allowed_values": ["COMPLETED", "CANCELLED", "PENDING"] },
        { "name": "created_at", "type": "timestamptz" }
      ]
    }
  ],
  "relations": [
    { "from": "orders.customer_id", "to": "customers.id" }
  ],
  "rules": [
    { "id": "rule_1", "text": "취소 주문은 매출에서 제외한다.",
      "condition": { "field": "orders.status", "op": "!=", "value": "CANCELLED" } },
    { "id": "rule_2", "text": "매출 = SUM(orders.amount)" }
  ],
  "metrics": []
}
```

Rules:
- A Rule's optional `condition` records its **business constant as data**: `{field, op, value}`. It uses the same `FixedCondition` shape as metric `fixed_filters`. The model reads `text`; the server uses `condition` without interpreting the prose. Rule conditions and metric fixed filters together form **registered constants** (`registered_constants`), which generated SQL must keep as literals (§7 `constant_as_parameter`). **Omission of these conditions from generated SQL is not checked.** A missing field, value incompatible with its type or `allowed_values`, or empty string returns 422 `invalid_rule_condition`. Rules without conditions affect only the prompt.
- `allowed_values` lists all possible values **only for enum-like fields** (such as status, region or grade), using their exact stored spelling. Leave it empty for numeric, date, ID and free-text fields. When present, generated SQL and Tool inputs must use an exact member (§7 `value_not_allowed`). Duplicate values or values incompatible with the field type return 422 `invalid_allowed_values`. DDL import rules are in §5.1.
- User-defined type (enum) names are compared with schema names and quoted case preserved. `public.mood` and `mood` denote the same type under the default public schema; names from different schemas are not merged. Arbitrary `SET search_path` state is not reproduced.
- Names use the form stored by the database. DDL import follows dialect rules (PostgreSQL folds unquoted identifiers to lowercase and preserves quoted identifiers). Server-generated SQL (metric contracts) quotes only when needed for case, reserved words or special characters.
- Context references (relations, conditions and metrics) compare names case-insensitively. Resource names must be unique within a Context, and field names within a Resource. SQL identifier resolution follows §7.
- v0.1 uses Resource names without schema prefixes (`public.orders` is unsupported).
- Both ends of every entry in `relations` must refer to existing fields; otherwise saving returns 422. A relation has only `from` (referencing) and `to` (referenced). Cardinality is not modeled.
- `allow_generated_sql` (default: `true`) controls whether the model may write SQL when no registered metric matches (§3.1.1). It is required for updates (PUT).
- Changing any of `name`, `dialect`, `timezone`, `allow_generated_sql` or `definition` increments `version` by one.
- Limits: 128 KiB of UTF-8 JSON for the definition, 100 resources and 1,000 fields in total.

### 3.1.1 Optional SQL metric contracts

`definition.metrics` contains developer-registered contracts and defaults to `[]`. A contract specifies its metric ID, aggregate, explicit INNER JOINs, GROUP BY, fixed scalar comparisons, required inputs and allowed/default date columns. `row_count` means COUNT(*); `non_null_count` counts non-NULL values of the specified column. Overlapping fields between fixed conditions and caller inputs, or invalid types, references or JOINs, return 422 when saving the Context. DDL import preserves contracts and warns about invalid references.

When no saved Tool is selected (§5.3), generation tries **registered metrics first** through three paths:
- Without metrics, the model writes SQL (`generate_v10`).
- With metrics and `allow_generated_sql: true` (the default), **hybrid** generation (`hybrid_v5`) selects a path in one LLM call. If one registered metric answers the entire question, `route: metric` lets the server compile SQL from the contract; otherwise `route: sql` lets the model write SQL. The SQL path is also instructed to follow registered metric aggregates and fixed filters as business definitions.
- With `allow_generated_sql: false`, this generation stage uses metrics only (`metric_plan_v3`) and returns unanswerable when none matches. At least one registered metric is required for this setting; otherwise it returns 422 `no_generation_path`.

Successful responses identify `source` as `tool` (saved Tool reuse), `metric` (contract compilation) or `generated_sql` (model-written SQL). Contract compilation is deterministic, but **does not guarantee that the model selected the right metric, inputs, date field or period** (for example, extracting `교토` when the allowed value is `京都`). See [metric contracts](METRIC_CONTRACTS.md) for fields and time boundaries.

### 3.2 Operation (SQL)

```json
{
  "type": "sql",
  "dialect": "postgresql",
  "statement": "SELECT c.region, SUM(o.amount) AS revenue FROM orders o JOIN customers c ON c.id = o.customer_id WHERE c.region = :region AND o.created_at >= :start_date AND o.status = 'COMPLETED' GROUP BY c.region",
  "parameters": { "region": "서울", "start_date": "2026-09-02" },
  "parameter_types": { "region": "string", "start_date": "date" }
}
```

- `parameter_types` supports six values: `string`, `integer`, `number`, `boolean`, `date` and `date-time`.
- The key sets of `parameters` and `parameter_types` must exactly match the statement's placeholder set.
- Python v0.1 has one Operation type, `SqlOperation`. Non-SQL Operations are not implemented; adding one requires coordinated changes to the domain, API, generation, Tool, UI and evaluation layers.

### 3.3 Tool

Contents of a Tool file (§11):

```json
{
  "id": "uuid",
  "context_id": "uuid",
  "name": "regional_revenue",
  "description": "특정 지역의 기간 매출 조회",
  "input_schema": { "...": "§4.1" },
  "operation": { "type": "sql", "statement": "SELECT ... :region ..." },
  "version": 3,
  "context_version": 4,
  "status": "valid",
  "validation_errors": [],
  "validation": { "valid": true, "errors": [], "warnings": [], "checks": ["..."] },
  "source_generation_id": "uuid | null",
  "metric_binding": { "metric_id": "...", "time_field": "..." },
  "created_at": "...",
  "updated_at": "..."
}
```

- `name` matches `^[a-z][a-z0-9_]{0,63}$` and is globally unique.
- `status` is `valid | invalid | stale`. `context_version` is the Context version against which the Tool was last found valid.
- `validation` contains the most recent validation result. Non-blocking warnings are preserved in retrieval and invocation responses.
- Editing a Tool increments `version` by one. Version history is not retained.
- A Tool's `operation` contains no values, only a statement template. Its `dialect` comes from the Context.
- `source_generation_id` is a provenance reference only; the Tool survives after that Generation leaves memory.
- `metric_binding` is present only on Tools created from metric contracts (§5.4).

### 3.4 Generation

Generations are **not persisted.** The running process retains recent records in memory (default: 200, configured by `SIDEWIT_GENERATION_HISTORY`), and loses them on restart. They support Tool drafting and debugging of rejected generations and Tool selections.

```
id, context_id, context_version, question, reference_time,
status            -- "ok" | "ambiguous" | "unanswerable" | "failed"
operation, reason, clarification_questions, clarification_answers,
tool_suggestion, validation, metric_binding,
operations, tool_selection,
provider, model, prompt_version, attempts, latency_ms, input_tokens, output_tokens,
created_at
```

- `failed` is an internal state for structural validation failure (API 422), LLM failure (API 502), or Tool selection/argument/version validation failure (§5.3). The cause is recorded in `reason`.
- Each completed Generation emits one standard-output log line containing metadata only: id, context_id, context_version, status, provider, model, prompt_version, attempts, latency_ms and token counts. **Questions, SQL and parameter values are never logged.**
- Questions, Context content and the same Context's saved Tool catalog are sent to the LLM Provider during generation. Sidewit does not control the provider's retention policy.

---

## 4. Parameter contract

1. **The only canonical style is named `:name`.** Placeholder names match `^[a-z][a-z0-9_]{0,63}$`. `?`, `$1` and `%(x)s` produce `unsupported_placeholder_style`. The application adapts placeholders to its driver and must never interpolate values into SQL strings.
2. **Parameters represent variable values from the user request, not constants required by the Context or Business Rules.** For example, the request value "서울" becomes `:region`, while a Rule's `'COMPLETED'` remains a literal.
3. **Generate resolves relative time into absolute values.** Expressions such as "최근 30일", "지난달" and "어제" become absolute parameters based on `reference_time` and the Context `timezone`. Newly generated SQL must not use `NOW()` or `CURRENT_DATE`.
   - `reference_time` makes relative time reproducible; it does not select the date column. That decision uses Context field descriptions and Business Rules (§6.3).
   - Use `date-time` (with an offset) for timestamp columns and `date` for date columns.
4. **Operation parameters and Tool inputs are distinct concepts.** Operation parameters are concrete values for this request; Tool inputs define a reusable contract.
   - `/tools/draft` maps Operation parameters **directly** to Tool inputs without automatic generalization.
   - Users generalize inputs in the Tool Editor, for example replacing `:start_date` with `NOW() - :days * INTERVAL '1 day'` and using a `days: integer` input. Tool statements allow `NOW()`.
5. **Dates and times are ISO 8601 strings.** Their JSON Schema forms are `{"type":"string","format":"date"}` and `{"type":"string","format":"date-time"}`. The application handles type conversion.
6. **Unsupported in v0.1:** optional parameters, defaults, arrays (IN bindings) and placeholders inside string literals.
7. **Sidewit never interpolates values into SQL strings.** Statements retain placeholders, and values are returned separately in `parameters`.

### 4.1 Tool `input_schema` constraints

```json
{
  "type": "object",
  "properties": {
    "region":     { "type": "string", "description": "조회할 지역" },
    "start_date": { "type": "string", "format": "date", "description": "조회 시작일" }
  },
  "required": ["region", "start_date"],
  "additionalProperties": false
}
```

- The top level must specify `type: object` and `additionalProperties: false`.
- Each property `type` is `string`, `integer`, `number` or `boolean`. Only strings support `format: date | date-time`.
- Allowed property keywords are `type`, `format`, `description` and `enum` (strings only). `default` is rejected.
- `required` must contain every property key with no duplicates. Property names follow placeholder naming rules.
- Violations produce `invalid_input_schema`, with the location (such as `properties.days.default`) in `ref`.
- Mapping to `parameter_types`: `date` is `string` + `format: date`; `date-time` is `string` + `format: date-time`; other types retain their names.
- `enum` only restricts the input domain. The checks in §7 address how SQL uses those values.

---

## 5. API

Common behavior:
- The base path is `/api/v1`.
- When `SIDEWIT_API_KEY` is configured, every request except `/health` requires `Authorization: Bearer <key>`. Missing or incorrect keys return 401 `unauthorized`. Without this setting, authentication is disabled. One key grants management, generation and invocation access; do not give it directly to end users.
- `GET /health` requires no authentication. It returns 200 when the data directory is readable and writable, or 503 otherwise.
- Invalid request shapes (Pydantic validation failures) return 422 `invalid_request`; unknown routes return 404 `not_found`.
- SQL/DDL complexity limits: 32 levels of parentheses/brackets outside strings and comments, 20,000 tokens, AST depth 64 and 10,000 AST nodes. Exceeding them produces `sql_parse_error` for SQL validation or 422 `ddl_parse_error` for DDL. Recursion limits during further analysis produce `sql_analysis_error`.
- Limits: HTTP body 1 MiB (413 `request_too_large` if exceeded), questions 8,000 characters, DDL 256,000 characters, SQL 64,000 characters; Context limits are in §3.1. Exceeding model input limits returns 422.
- Error format:

```json
{ "error": "tool_invalid", "message": "Tool is incompatible with the current context.", "details": { } }
```

### 5.1 Context

| Method | Path | Description |
|---|---|---|
| GET | `/contexts` | List |
| POST | `/contexts` | Create `{name, dialect, timezone?, allow_generated_sql?, definition?}` |
| GET | `/contexts/{id}` | Retrieve |
| PUT | `/contexts/{id}` | Update `{name, dialect, timezone, allow_generated_sql, definition, expected_version}` |
| DELETE | `/contexts/{id}` | Delete; returns 409 `context_has_tools` when Tools reference it |
| POST | `/contexts/{id}/import-ddl` | Preview DDL parsing `{ddl, mode: "merge" \| "replace"}` without saving |

`PUT`:
- An `expected_version` different from the current version returns 409 `version_conflict` (optimistic locking). A name conflict with another Context also returns 409.
- Success includes the results of revalidating associated Tools (§5.2).

```json
{
  "context": { "...": "..." },
  "revalidation": {
    "checked": 3,
    "valid": ["regional_revenue", "monthly_sales"],
    "invalid": [{ "name": "customer_orders", "errors": [{ "code": "unknown_field", "message": "...", "ref": "orders.amount" }] }]
  }
}
```

`import-ddl`:
- If the optional request `expected_version` differs from the current Context version, return 409 `version_conflict`. The response `context_version` identifies the preview's base version; use it as the subsequent PUT's `expected_version`. Do not combine edits from different versions. The UI prevents applying an old preview when the Context version, DDL or mode changes, or while a request is pending or has failed.
- Parse `CREATE TABLE` using the specified dialect to populate resources, fields, primary keys and nullability, plus relations for foreign keys. Unsupported statements (`CREATE EXTENSION`, indexes, etc.) are ignored with warnings. pg_dump psql meta-commands such as `\restrict` are also ignored.
- Populate `allowed_values` from `CREATE TYPE ... AS ENUM` and value-list CHECKs: `col IN (...)`, `col = a OR col = b`, pg_dump's `col = ANY (ARRAY[...])`, and `(col)::text = ANY (...)` for varchar columns.
  - Evaluate literal casts as PostgreSQL does (§7.3, shared with the validator): `'1'::integer` becomes 1, `'ABC'::varchar(1)` becomes `'A'`, and `1.235::numeric(4,2)` becomes 1.24. Casts that PostgreSQL rejects (`'1.5'::integer`, overflow) and unsupported casts are skipped with warnings.
  - On the column side, only casts to unbounded text (`::text`) are treated as value lists for the original column. `col::varchar(1) IN (...)` does not constrain the original column's values and is not imported.
  - Multiple ENUM/CHECK constraints on one column all apply, so use their intersection. Empty intersections or lists containing non-literals are skipped with warnings.
  - Import pg_dump enum CHECKs (`m = ANY(ARRAY['ok'::public.mood, ...])`) only when their enum labels can be verified from declarations in the same DDL.
  - CHECKs that do not define value lists, such as ranges (`n > 0`), are out of scope.
  - Skip lists that fail Context validation (`invalid_allowed_values`) with warnings so that the preview remains a saveable Context.
  - Numeric CHECK values that cannot be represented exactly (§6.2 `exact_number`) return 422 `ddl_parse_error`.
- DDL identifiers that violate Context naming rules return 422 `ddl_parse_error` without changing the saved Context.
- In `merge` mode, preserve existing `description` values of resources and fields with matching names. Use DDL `allowed_values` when ENUM/CHECK declarations provide them; otherwise keep existing values. Add new items and retain items absent from the DDL with warnings.
- An optional `base_definition` merges against the unsaved draft and requires `expected_version`. If omitted, merge against the saved Context. Context size limits apply to both the input and merged result.
- Optional `pending_foreign_keys` contains unresolved single-column foreign keys from earlier imports: `{table, column, ref_table, ref_column}`. `ref_column: null` resolves to the target table's single primary key. Retry resolution on the next import and return entries still unresolved. They are not automatically saved in the Context.
- If tables from different schemas collide after schema removal within one DDL, return 422. Existing Contexts do not retain original schema names, so collisions across separate imports cannot be detected automatically. Review name transformations in the preview.
- The response is `{ "context_version": 1, "definition": {...}, "warning_details": [{"code": "ignored_statement", "message": "...", "ref": null}], "pending_foreign_keys": [] }`. After review, the client saves it with `PUT`. Use `warning_details[].code` to classify warnings.

### 5.2 Revalidation contract

> When a Context changes, affected Tools must be revalidated before invocation.

Revalidation runs synchronously during `PUT /contexts/{id}`, updating the Context and Tool files within the same write section (§11). For each Tool:
- Success: `status=valid`, `context_version=<new version>`, `validation_errors=[]`
- Failure: `status=invalid`, `validation_errors=[...]`

`stale` (the Tool's `context_version` differs from the current Context version) is a transitional state before revalidation. Invoking a stale Tool immediately revalidates it before proceeding.

### 5.3 Generate

`POST /generate`

Requests automatically select saved Tool calls and generate independent metric/SQL operations
for uncovered parts, up to five operations in total. If no Tool fits, generation handles the
whole question as one operation. No UI toggle or API mode is needed. Explicit metric options bypass Tool selection. With no
Tools, the selection call is skipped. Context generation policy still applies.

Selection uses `tool_plan_v3` and receives saved SQL and input contracts, Context-local
reference time and clarification answers. Successful plans return `status: "ok", source:
"plan", execution: "independent", operations: [...]`; there is no top-level operation or
validation, even for one step. Each step contains its ID, source, operation, validation and optional metric binding.
Saved Tool steps include Tool name/version; generated steps include their task. Generation fallback retains its single-operation
response (`source: "generated_sql"` or `"metric"`). Clients must handle both response shapes.
See [Tool plans](TOOL_PLANS.md) for integration details.

Plans may mix saved Tools with generated SQL or metric operations, but never bind results
between steps. Generated operations reuse the existing planner and repair loop. Tasks are
model-written subquestions; validation uses the original request/answers and reference time.
Workers run concurrently under the shared request deadline. Unexpected worker exceptions
cancel and join all remaining workers before the request exits. Tool and Context versions are
rechecked after generation. Each generated step exposes its task, source and optional
Tool suggestion. See [operation plans](TOOL_PLANS.md) for lifecycle and draft behavior.

When multiple generated tasks are ambiguous, return only the first unresolved task's
question batch in plan order. Do not merge or deduplicate questions across tasks. Follow-up
answers replan the whole request; successful steps are not cached between rounds.

All selected operations must validate before any are returned. Duplicate calls, invalid
arguments and inconsistent routes return 422; incompatible Tools or concurrent Tool/Context
changes return 409. These failures never fall back to generation. Missing inputs return
ambiguous; dependent execution or excessive call counts return unanswerable. Selection of
Tools, completeness and dependencies are model judgments, not static guarantees. Sidewit
never executes SQL. A whole plan cannot become one Tool via /tools/draft (409
plan_not_draftable). Use generation_id plus step_id to draft one generated step; saved Tool
steps return step_already_saved. Tool create accepts source_step_id alongside
source_generation_id and preserves the selected step's metric contract.

A Tool catalog exceeding 100,000 JSON characters returns 422 `tool_catalog_too_large`; it is not arbitrarily truncated.
Tool selection, argument and version validation failures after a model response are recorded as failed Generations, with `generation_id` included in the error details.
The debugging detail `tool_selection` contains the unvalidated model selection, not an executable Operation. `reason` records the error code, and token counts and latency are retained.
Selection content stays in memory; logs contain only existing metadata. Catalog size rejection before the call and Context deletion do not create records.
Selection, subsequent generation and repair share the existing 90-second deadline. Attempts and token counts include selection calls.
Dependent Tool chaining and automatic saving are not supported.

```json
{
  "context_id": "uuid",
  "question": "지난 30일 서울 매출 알려줘",
  "reference_time": "2026-10-02T09:00:00+09:00"
}
```

`metric_id`, `date_field`, `period` and `require_metric` are optional metric options. Specifying a metric option restricts the request to metrics; `require_metric: true` applies that restriction without choosing a metric. `date_field` requires a `metric_id` and an allowed field of that contract. Supported periods are rolling_days (days 1–3660) and previous_day/week/month (days=null), computed by the server. Invalid options return 422 `invalid_metric_request` before any LLM call.

When `reference_time` is omitted, use the server's current time converted to the Context `timezone`.

Every ambiguous response must fit the remaining answer slots and text budget, allowing
at least one character per new answer. Otherwise return unanswerable and instruct the caller
to split the request; do not silently truncate questions. Actual answer lengths are checked
on submission by the UI and API, so longer answers may still require shortening.

Optional `clarification_answers` contains up to 24 `{id, question, answer}` items in chronological
order. Nonempty answers require `reference_time` and `expected_context_version`; duplicate IDs,
blank text and oversized input return 422 before any LLM call. Question text is limited to 1,000
characters per item, answers to 2,000, IDs to 100; original question plus all clarification
question/answer text is limited to 16,000 characters. Preserve the original question and all
metric options on resubmission. Use the preceding ambiguous response's resolved `reference_time`
and `context_version`. A version mismatch before or during a pinned request returns 409
`context_changed` with no Operation; start a new request against the current Context.

This is stateless input refinement, not a server-side chat session. Clients retain the original
request and accumulated answers. IDs correlate answers with questions but do not authenticate
prior server output; all supplied content is untrusted user input. No generation-history lookup
is required. Re-run normal Tool selection, planning and validation with the supplied answers.
Offered alternatives are excluded from request-literal checks; the planner is instructed not to treat them as user-selected values. Explicit metric
options still take precedence. See [API integration example](CLARIFICATIONS.md).

A 200 response has one of three shapes:

```json
{
  "generation_id": "uuid",
  "context_version": 4,
  "status": "ok",
  "source": "generated_sql",
  "operation": {
    "type": "sql",
    "dialect": "postgresql",
    "statement": "SELECT ... WHERE c.region = :region AND o.created_at >= :start_date AND o.status = 'COMPLETED' ...",
    "parameters": { "region": "서울", "start_date": "2026-09-02" },
    "parameter_types": { "region": "string", "start_date": "date" }
  },
  "validation": { "valid": true, "errors": [], "warnings": [], "checks": ["syntax", "statement_policy", "resources", "fields", "parameters", "placeholder_safety"] },
  "tool_suggestion": { "name": "regional_revenue", "description": "특정 지역의 기간 매출 조회" },
  "metric_binding": null
}
```

```json
{ "generation_id": "uuid", "context_version": 4, "status": "ambiguous",
  "reason": "Revenue has multiple possible interpretations.",
  "reference_time": "2026-10-02T00:00:00Z",
  "clarification_questions": [{
    "id": "uuid:0",
    "question": "Should revenue be based on completed payments or placed orders?",
    "options": ["Completed payments", "Placed orders"]
  }] }
```

```json
{ "generation_id": "uuid", "context_version": 4, "status": "unanswerable",
  "reason": "The current context does not contain data required to calculate churn rate." }
```

Generation flow when no saved Tool is selected:
1. Choose a planner (`generation/planners.py`): `SqlPlanner` (`generate_v10`) without metrics; `HybridPlanner` (`hybrid_v5`) with metrics and generated SQL allowed; otherwise `MetricPlanner` (`metric_plan_v3`), also used for explicit metric options. Hybrid reuses SQL/Metric conversion and validation according to the model's `route`; a missing route triggers repair with `route_missing`. Metric options on a Context without metrics return 422 `invalid_metric_request` before the LLM call. The metric compiler builds aggregates, JOINs, filters and grouping. A structured `date_field` request option takes precedence. Otherwise, the model returns a `date_field` intent after interpreting the question, answers, field descriptions and metric default. The compiler accepts only a member of that metric's `time_fields` using the existing case-insensitive reference resolution; it never infers a choice from field-name mentions. A null choice returns ambiguous without silently applying a default; an invalid field triggers repair. Negated fields and fields requested only in the output must not select the period basis. Interpreting natural language remains probabilistic.
2. For `status=ok`, run the static Validator with the appropriate Generate policy (§7): `generate_policy(original question + answer values)` for SQL (including request-literal checks), or `CONTRACT_POLICY` for metrics. Return ambiguous/unanswerable without validation; discard any SQL the LLM supplied with those statuses.
3. On validation failure, attach the errors and request repair **once only**.
4. If repair also fails, return 422 `generation_invalid`. **The response contains no SQL.** Generated SQL remains only in the in-memory Generation record.
   ```json
   { "error": "generation_invalid", "message": "Generation failed validation.", "details": { "generation_id": "uuid", "validation_errors": [{ "code": "unknown_field", "message": "...", "ref": "orders.amt" }] } }
   ```
5. LLM failures return 502 `llm_error`, with only `generation_id` in details. Raw provider errors may contain credentials, so they are excluded from public responses and logs; only a masked version is retained in the in-memory `reason`. Mask configured key values and patterns such as `sk-…`, `AIza…`, `Bearer …` and `key=…`. Do not retain raw exceptions through `__cause__` chains. Configuration stores API keys as `SecretStr`.
6. Without a configured LLM provider, return 503 `llm_not_configured` without creating a Generation.
7. Apply a 90-second overall deadline. Each Provider call times out after 60 seconds; selection, initial generation and repair share the overall deadline. Exceeding it returns 504 `generation_timeout` and may leave no Generation record. nginx waits 100 seconds, and UI requests wait 110 seconds.
8. The Context is not locked while awaiting the LLM. If deleted during that time, return 404 `context_not_found`.

`GET /generations?context_id=&limit=` (recent in-memory records)
`GET /generations/{id}` (debug details, including SQL and validation results for failed Generations)

> `/generations*` is for the management UI and development, not public exposure: it can reveal rejected SQL and original questions. Records evicted from memory or lost on restart return 404 `generation_not_found`.

### 5.4 Tools

| Method | Path | Description |
|---|---|---|
| GET | `/tools?context_id=` | List, including status |
| GET | `/tools/{name}` | Retrieve |
| POST | `/tools/draft` | Draft from a Generation `{generation_id, step_id?}` without saving |
| POST | `/tools` | Create `{context_id, name, description, input_schema, operation, source_generation_id?, source_step_id?}`. Validation failure returns 422 `tool_validation_failed` (`details.validation_errors`, `details.warnings`); a name conflict returns 409 `tool_name_conflict` |
| PUT | `/tools/{name}` | Update `{expected_version, description, input_schema, operation}`. Version mismatch returns 409 `version_conflict`. Name and Context cannot change. Content changes increment version by one. Validation failure returns 422 and leaves the Tool unchanged |
| DELETE | `/tools/{name}` | Delete |
| POST | `/validate` | Validation without saving, for editors: `{context_id, operation, input_schema?}`. With `input_schema`, run Tool validation (§7.5); also supplying `operation.parameter_types`/`parameters` returns 422 `invalid_validate_request`. Otherwise validate SQL using those parameter fields. Always uses `DEFAULT_POLICY`; this is not a Generate-compatibility check |
| POST | `/tools/{name}/invoke` | Deterministic invocation `{arguments, expected_version?}` |
| POST | `/tools/{name}/refresh-contract` | Requires `{expected_version}` (409 on conflict). Rebuilds a metric-bound Tool's SQL and inputs from the current contract. Preserves input descriptions/enums when types match. Changes increment version by one. An unbound Tool or unavailable metric/date binding returns 409 `metric_contract_unavailable` |

`POST /tools/draft` makes no LLM call.
- Requires an in-memory Generation with `status=ok`. Missing records return 404 `generation_not_found`; other statuses return 409 `generation_not_ok`.
- Plans require `step_id` for a generated/metric step. Omit it for a standalone generation.
- Drafts are unvalidated starting material. Saving validates them against the Context at that time.
- Map `operation.parameter_types` one-to-one to `input_schema`. Populate name and description from `tool_suggestion`. For string inputs compared with fields that have allowed values, use those values as the input `enum`.
- Include `example_arguments` (the Generation's parameters) in the response.

```json
{
  "context_id": "uuid",
  "name": "regional_revenue",
  "description": "특정 지역의 기간 매출 조회",
  "input_schema": { "type": "object", "properties": { "...": {} }, "required": ["region", "start_date"], "additionalProperties": false },
  "operation": { "type": "sql", "statement": "SELECT ... :region ... :start_date ..." },
  "example_arguments": { "region": "서울", "start_date": "2026-09-02" },
  "source_generation_id": "uuid",
  "metric_binding": null
}
```

`source_generation_id` and `source_step_id` in `POST /tools`:
- For a plan, retain both IDs from the draft; `source_step_id` cannot be supplied alone.
- Must reference an in-memory `ok` Generation of the same Context; otherwise return 422 `invalid_source_generation`.
- A metric Generation passes on its `metric_binding`. Omitting it or sending null cannot remove it, and a different binding cannot replace it. PUT does not change bindings.

`POST /tools/{name}/invoke`

```json
{ "arguments": { "region": "서울", "start_date": "2026-09-02" }, "expected_version": 3 }
```

```json
{
  "tool": "regional_revenue",
  "tool_version": 3,
  "context_version": 4,
  "validation": { "valid": true, "errors": [], "warnings": [], "checks": ["..."] },
  "operation": {
    "type": "sql",
    "dialect": "postgresql",
    "statement": "SELECT ... WHERE c.region = :region AND o.created_at >= :start_date ...",
    "parameters": { "region": "서울", "start_date": "2026-09-02" },
    "parameter_types": { "region": "string", "start_date": "date" }
  }
}
```

Flow: retrieve Tool → check optional `expected_version` (409 `version_conflict` on mismatch) → revalidate and record the result if not valid or tied to an older Context version → return 409 `tool_invalid` if invalid → validate arguments against `input_schema` and the compared fields' `allowed_values` (422 `invalid_arguments` on failure) → assemble Operation. Allowed values per input are computed once per (Tool version, Context version) and reused. There is no LLM call, string substitution or SQL execution. `parameters` and `parameter_types` follow the order of `input_schema.required`.

Error responses:
- 404 `tool_not_found`
- 409 `tool_invalid`(`details.validation_errors`), 409 `version_conflict`
- 422 `invalid_arguments` (`details.errors` entries contain `{code, message, ref}`): `missing_argument`, `unexpected_argument`, `argument_type_mismatch` (including date/date-time formats), `argument_not_in_enum`, `value_not_allowed` (current Context allowed-value violation)
- Only `arguments` and `expected_version` are allowed at the request's top level. Other keys return 422 `invalid_request`.

Tool and Context constraints:
- Tool JSON Schema enums cannot weaken Context constraints. Saving, updating and revalidating check that enums are subsets of the field's `allowed_values`; conflicts produce `input_enum_conflict`. Parameters compared with multiple constrained fields use the intersection of their allowed values.
- Manually authored Tools without enums still check current Context allowed values on invocation, including numeric and boolean inputs.
- Invalid `input_schema` shapes are not server exceptions. `/validate` returns `valid: false` with `invalid_input_schema`; saving returns 422 `tool_validation_failed`.

Metric-bound Tools:
- The statement AST and parameter types must match the current contract template. Only whitespace, comments and quoting differences that resolve to the same name (`"orders"` and `orders`) are allowed. Other differences produce `metric_contract_mismatch`.
- Contracts belong to the Context and are revalidated when it changes. Incompatible Tools return 409 and can be updated with `refresh-contract`.
- SQL and input names/types of bound Tools are owned by the contract and read-only in the UI. Unbound Tools undergo ordinary static validation only.

---

## 6. LLM generation contract

### 6.1 Provider abstraction

```python
class LLMProvider(Protocol):
    name: str
    model: str
    async def generate_structured(self, system: str, messages: list[Message], schema: type[BaseModel]) -> StructuredResult: ...
    # StructuredResult: parsed(BaseModel), input_tokens, output_tokens
```

- Implementations are `OpenAIProvider`, `GeminiProvider` and the test-only `FakeProvider`, which returns scripted responses.
- Configuration variables: `SIDEWIT_LLM_PROVIDER`, `SIDEWIT_LLM_MODEL`, `OPENAI_API_KEY`, `GEMINI_API_KEY`.
- Code after the validator is provider-independent.
- Previously verified configurations include OpenAI `gpt-6-luna` and Gemini `gemini-3.8-flash`/`gemini-3.1-flash-lite`. The adapter sets thinking to `low` for those two Gemini models. Provider requests time out after 60 seconds, with SDK retries disabled. Verify other models using the evaluation suites (§9); see the evaluation reports for feature-specific coverage.

### 6.2 LLM output schema

For provider compatibility, use lists instead of objects with dynamic keys. `value` is always a string to avoid differences in union-type support; the server converts it according to `type`.

```json
{
  "status": "ok | ambiguous | unanswerable",
  "reason": "string | null",
  "clarifications": [{"question": "string", "options": ["string"]}],
  "statement": "string | null",
  "parameters": [{ "name": "region", "type": "string", "value": "서울" }, { "name": "days", "type": "integer", "value": "30" }],
  "tool_suggestion": { "name": "snake_case", "description": "string" }
}
```

- Convert integer/number values **only when exactly representable** (`domain/params.exact_number`, shared by metrics and DDL CHECK values). Integral values in the bigint range become ints (`"9007199254740993"`, `"3.0"`); others become floats only when representable under the exact-number rules (`"0.1"`, `"1e30"`). Values requiring rounding, becoming infinite or using excessive exponents remain strings and trigger repair through `parameter_type_mismatch`. The server never silently rounds numbers.
- Duplicate parameter names trigger repair with `duplicate_parameter` rather than choosing one value (a Generate conversion-stage code).
- `request_value_literal` is also Generate-only. `generate_policy(original question + answer values)` (`validation/request_literals.py`) requests repair when a direct literal in a comparison/IN/BETWEEN/LIMIT/OFFSET appears verbatim in the original question or an answer. Offered clarification questions are excluded. Numbers immediately followed by date/duration units (`1월`, `30일`, `7 days`) are period expressions, not matches. Literals matching registered constants on the same field are exempt. For Rules without conditions, values mentioned in Context descriptions or Rule prose are conservatively protected as potential constants. This does not validate semantics or Rule application and does not apply to manually authored Tools/`DEFAULT_POLICY`.
- This DTO is internal to `generation/`, not a domain model or public API contract.

### 6.2.1 Internal output for registered metrics

```json
{
  "status": "ok",
  "reason": null,
  "clarifications": [],
  "metric_id": "regional_revenue",
  "date_field": "orders.paid_at",
  "parameters": [{"name":"region","type":"string","value":"서울"}],
  "period": {"kind":"rolling_days","days":30},
  "tool_suggestion": null
}
```

This DTO contains a selected metric, inputs, period and date-field reference, but no statement, aggregate function, JOIN or fixed condition. All provider fields are required; nullable values use explicit null. The hybrid DTO also includes `date_field`, which is null for the SQL route. After validating metric/input names, date-field membership, types and periods, the server compiler produces canonical `:name` SQL and checks it with `CONTRACT_POLICY`. Failure follows the one-repair and 422/no-SQL contract. Semantic errors in metric, date-field or extracted-value selection are not validated.

### 6.3 Required prompt content

- dialect, `reference_time`, timezone
- Rendered Context: resources/fields with types, descriptions and allowed values; relations, rules and metrics
- Rules:
  - Use only Context resources and fields.
  - Choose period date columns using field descriptions and Business Rules. If multiple candidates remain unresolved, return ambiguous with the candidate columns.
  - Return unanswerable when required data is absent from the Context.
  - Return ambiguous with clarifications when multiple interpretations remain unresolved by Rules.
  - Generate a single SELECT statement.
  - Apply §4 rules 2 and 3: parameterize only variable values, resolve relative times to absolute values, prohibit `NOW()` in new SQL and prohibit placeholders in literals.
  - Apply Business Rules.
- Current generation prompts are `sidewit/prompts/generate_v10.md`, `sidewit/prompts/hybrid_v5.md` and `sidewit/prompts/metric_plan_v3.md`; each Generation records `prompt_version`. Tables/columns needing quotes for case or reserved words receive exact SQL spellings from the compiler's identifier renderer. Repairs for `unknown_resource`/`unknown_field` also include those spellings and quoting rules; SQL is not rewritten automatically and validation is not relaxed. `allowed_values` instructions require exact spelling without translation, transliteration or invention. Multiple undescribed date/timestamp fields produce an ambiguity hint, excluding bookkeeping timestamps such as `updated_at` and `deleted_at`. These are model instructions, not enforced validation.

---

## 7. Static SQL Validator

Implemented as pure functions with no database or LLM dependency.

```python
def validate_sql_operation(
    statement: str,
    dialect: str,
    context: ContextDefinition,
    declared_params: dict[str, ParamType],
    values: dict[str, Any] | None = None,
    policy: ValidationPolicy = DEFAULT_POLICY,
) -> ValidationResult
```

```json
{ "valid": false, "errors": [{ "code": "...", "message": "...", "ref": "orders.amount" }], "warnings": [], "checks": ["..."] }
```

### 7.1 Guarantees and limits

The Validator checks SQL parsing, a single SELECT-family statement, a closed function allowlist, tables belonging to the Context, name resolution (§7.4), parameter contracts, relative time (per policy), and allowed-value/registered-constant policies within supported expressions.

It does not check function/operator existence or argument types, type compatibility, actual database executability, privileges, cost, function side effects, Business Rule compliance or whether the result answers the question. Such problems surface when the application executes or EXPLAINs SQL against its own database. The UI says "✓ SQL structurally validated", never "Context validated" or "safe".

**Checks stay within their supported scope.** When required policies (allowed values or registered constants) cannot be analyzed outside supported expressions, reject with `value_unverifiable` rather than passing them.

### 7.2 Policies and checks

The functions check uses a closed allowlist on every SQL path, including saved Tools.
Unknown functions, schema-qualified calls, dynamic-SQL helpers, file access, sequence
mutation and administration helpers are rejected with function_not_allowed.
Supported table functions (unnest, generate_series, JSON element/entry functions) remain
available; other table functions are rejected by the same allowlist.
Context definition.sql_extensions defaults to []; ["postgis"] enables the explicitly
listed spatial functions. This declares validation support, not database installation.
DDL imports preserve it in both modes. See [SQL function policy](SQL_FUNCTIONS.md).

Dollar-quoted strings are analyzed as ordinary string values. Escape (E'...') and Unicode
(U&'...') strings are rejected with unsupported_string_literal, including in manually
saved Tools. Use parameters or ordinary quoted strings instead.


`ValidationPolicy` selects the policy for each path.

| Policy | Used by | `allow_relative_time` | `request_text` | `allow_constant_parameters` |
|---|---|---|---|---|
| `generate_policy(question)` | `/generate` SQL path, including repair | `false` | Question | `false` |
| `GENERATE_POLICY` | Evaluation revalidation of model-written SQL | `false` | None | `false` |
| `CONTRACT_POLICY` | `/generate` metric path and evaluation of metric results | `false` | None | `true` |
| `DEFAULT_POLICY` | Ordinary SQL validation, Tool save/revalidation, `/validate` | `true` | None | `true` |

Metric parameters are contract-declared inputs, so `constant_as_parameter` does not apply even when their fields match another metric's fixed filters.

| Check | Code | Severity |
|---|---|---|
| sqlglot parsing failure, SQL exceeding 64,000 characters or complexity limits | `sql_parse_error` | error |
| More than one statement | `multiple_statements` | error |
| Statements other than SELECT / WITH…SELECT / UNION, `SELECT INTO`, `FOR UPDATE` | `statement_not_allowed` | error |
| Resource absent from Context (excluding visible CTEs), schema-qualified table, system table such as `pg_roles`, or invisible alias | `unknown_resource` | error |
| Field absent from Context | `unknown_field` | error |
| Unqualified field present in multiple sources, or reference to a duplicated output column name within one source | `ambiguous_field` | error |
| Duplicate aliases within a FROM, set-operation arms with different column counts, unequal VALUES row lengths, or `SELECT *` without FROM | `sql_analysis_error` | error |
| JOIN condition uses a key pair not declared in Context relations | `undeclared_relation` | warning |
| `?`, `$1`, `%(x)s` placeholders | `unsupported_placeholder_style` | error |
| `:name` pattern inside a string literal | `placeholder_in_literal` | error |
| Placeholder present in the statement but undeclared | `undeclared_parameter` | error |
| Declared parameter absent from the statement | `unused_parameter` | error |
| Value does not match its declared type (when values are supplied) | `parameter_type_mismatch` | error |
| Expression depends on the database's current time (only when `allow_relative_time=false`) | `relative_time_function` | error |
| Parameter binds the **same field, operator and value** as a registered constant (`allow_constant_parameters=false`, with supplied values). `:x <= fee` is treated as `fee >= :x` | `constant_as_parameter` | error |
| Literal or supplied parameter compared with an `allowed_values` field using `=`, `!=`, `IS [NOT] DISTINCT FROM`, `[NOT] IN` or `= ANY/SOME/ALL(ARRAY[...])` is absent from the list. Text matches exactly; numbers compare by value. Range comparisons and LIKE are excluded | `value_not_allowed` | error |
| A value compared with a constrained field cannot be evaluated within the supported scope, e.g. `upper(:s)` | `value_unverifiable` | error |

PostgreSQL expressions covered by `relative_time_function`: `NOW()`, `CURRENT_DATE`, `CURRENT_TIME`, `CURRENT_TIMESTAMP`, `LOCALTIME`, `LOCALTIMESTAMP`, `clock_timestamp()`, `statement_timestamp()`, `transaction_timestamp()`, `timeofday()`, `'now'`/`'today'`/`'tomorrow'`/`'yesterday'` cast to date/time types, and single-argument `age(x)`. Special date words followed by a time (for example 'yesterday 10:00') and literals compared directly to known temporal fields are also checked, including aliases and CTE lineage. Its sole purpose is `reference_time` reproducibility; general nondeterministic functions such as `random()` are excluded.

### 7.3 Supported value policies

Allowed-value and constant checks apply anywhere in a SELECT: WHERE, HAVING, JOIN ON, `FILTER (WHERE ...)`, `CASE WHEN`, projections and ORDER BY.

Value provenance (`validation/sql_values.py`) tracks outputs through CTEs, derived tables, VALUES, column aliases, single-column subqueries (`(SELECT :s)`, `ANY(SELECT ...)`, `IN (VALUES ...)`) and set operations. It distinguishes literals/parameters with cast chains, database values, and values with unknown provenance or transformations. Unknown values are never treated as database values to bypass checks.

Column lineage: direct projections retain the original field's allowed-value and constant constraints through aliases, CTEs, derived tables, `*` and set operations whose arms all reference the same field. Aggregates, arithmetic, function results and set-operation columns with different origins do not inherit them. Parentheses, `COLLATE` and unbounded text casts (`::text`, `::varchar`) preserve **string-field** values and therefore field identity; boolean `flag::text` becomes a string and no longer inherits the original allowed values. Casting a column to the same enum preserves provenance. Quoted literals compared with boolean fields use PostgreSQL boolean input rules (`'true'`, `'t'`, `'yes'`, `'on'`, `'1'`, etc.).

CAST evaluation (`domain/dialect.cast_value`, shared with DDL import) depends on the source scalar category and target type/modifiers. SQL numeric literals remain distinct from string literals, and chained casts preserve each intermediate category. Casts are evaluated in order, not stripped.
- String → text/varchar: preserve the string, subject to varchar length limits (`'APPLE'::varchar(1)` becomes `'A'`).
- String/integer/number → smallint/integer/bigint: strings accept only a sign and decimal integer digits; numeric values are rounded before checking the target range.
- String/integer/number → numeric: check precision, scale and overflow only for exactly representable values.
- String/boolean → boolean: supported PostgreSQL boolean input spellings.
- String → the Context field's enum type: evaluate only within the union of registered string labels for that type, then check the final value against the compared field's `allowed_values`. Enum → text/varchar is supported; enum → number/boolean is not. Passing `okay` through `:m::mood::varchar(2)::mood` cannot hide an invalid intermediate label.

char, real/double precision, other user-defined types, and numeric/boolean → string conversions are not evaluated. If allowed-value validation needs such a conversion, reject with `value_unverifiable`. Parameters passing through value-changing casts are not restricted to the field's original allowed values; their results are checked using actual values at invocation. Unevaluable input expressions are rejected when saving a Tool.

### 7.4 Name resolution

Implemented in `validation/sql_analysis.py`.
- Identifiers follow PostgreSQL stored-name rules: fold unquoted names to lowercase and preserve quoted names. The same rules apply to tables, columns, aliases and CTEs (`"Orders"` differs from `orders`).
- Check columns directly referenced by each SELECT against that SELECT's FROM/JOIN sources.
- Visibility depends on **where** a name is used. FROM contains comma-separated groups, each a JOIN tree. JOIN binds more tightly than commas (`a, b JOIN c ON ...` means a and (b JOIN c)). Parenthesized JOINs form nested trees; an aliased parenthesized JOIN (`(a JOIN b) AS j`) is one item. Correlated subqueries see all outer FROM items. LATERAL items and FROM table-function arguments see **preceding** items, except that the right side of a RIGHT/FULL JOIN cannot see that JOIN's left side. JOIN ON sees only items up to that JOIN within the same group. Unqualified names use the JOIN state at their location. Non-LATERAL derived tables and CTEs cannot see sibling FROM items.
- `JOIN ... USING (c)` and `NATURAL JOIN` merge shared columns. A USING column missing on either side produces `unknown_field`; multiple matches on the left produce `ambiguous_field`.
- Table column aliases (`orders AS o(x, s)`) rename leading columns.
- SELECT aliases resolve only in GROUP BY and ORDER BY of the same SELECT.
- Sources with unknown column lists (table functions without explicit lists) skip field checks. An explicit list such as `unnest(...) AS t(x)` enables checking against that list.
- The placeholder-in-literal check matches `:name` only when not preceded by a letter or `:`. It catches `INTERVAL ':days days'`, but not `'10:30'` or `'a::b'`.
- `undeclared_relation` checks only `JOIN ... ON a = b` between two Context tables.
- Development tests in `backend/tests/test_postgres_parity.py` EXPLAIN SQL against real PostgreSQL to compare name/structure verdicts. They are not part of product runtime. Add supporting SQL cases there when changing name-resolution rules.

### 7.5 Tool validation

Tool validation (save, update, revalidation and `/validate` with `input_schema`) uses `DEFAULT_POLICY`.
1. Check the §4.1 input-schema constraints (`invalid_input_schema`).
2. Validate SQL using `input_schema` inputs as declared parameters. Differences between placeholder and input sets produce `undeclared_parameter` or `unused_parameter`.
3. Compare input enums with Context allowed values (`input_enum_conflict`).
4. Check metric-bound Tools against their contract (`metric_contract_mismatch`).
5. `checks` begins with `input_schema`.

Tools store no argument values, so Tool validation does not produce `parameter_type_mismatch`. Invocation checks actual arguments using the same static value validator.

---

## 8. State transitions

```
Question ──/generate──▶ Generation (in memory)
                          ├─ ambiguous     → Application asks for clarification
                          ├─ unanswerable  → Application handles the failure
                          ├─ (failed)      → Error response (see §5.3)
                          └─ ok            → Validated SQL Operation
                                               ├─ Saved Tool reused → Review and use
                                               └─ New draft
                                                    │ [Create Tool draft]
                                                    ▼
                                               /tools/draft (no LLM)
                                                    │ User review/edit
                                                    ▼
                                               /tools (validate, then save to a Tool file)
                                                    ▼
                                               Tool[valid]
                                                    │
                     Context changes → Revalidate ─┬─▶ valid (context_version updated)
                                                   └─▶ invalid → Edit and revalidate → valid
                                                    │
                                               /tools/{name}/invoke → SQL Operation
```

---

## 9. Evaluation

The goal is a reproducible regression suite for Generate quality. It executes no SQL and does not ask an LLM to judge correctness. All suites were exposed during development and review, so they are **regression evaluations**, not evidence of general accuracy.

### 9.1 Golden Case format

Each suite contains `evals/<suite>/context.json` and `cases.yaml`. The Context fixture uses the creation API's `name`, `dialect`, `timezone` and `definition`, with an evaluation-specific name such as `eval-commerce`. `cases.yaml` contains a shared `reference_time` and a `cases` array. Per-case reference times override the shared value. All reference times require timezone offsets.

```yaml
reference_time: "2026-10-02T09:00:00+09:00"
cases:
  - id: busan_revenue
    category: parameterization
    question: 부산 지역 매출
    expect:
      status: ok
      resources: [orders, customers]
      fields: [orders.amount, customers.region]
      joins: [[orders.customer_id, customers.id]]
      aggregates: [{ function: sum, field: orders.amount }]
      constraints:
        - { field: orders.status, op: "=", value: COMPLETED }
        - { field: customers.region, op: "=", parameter_value: 부산 }
      parameters: [{ value: 부산, type: string }]
      forbidden_parameter_values: [COMPLETED]
      forbidden_literals: [부산]
      forbidden_constructs: [relative_time_function]
```

- `expect.status` is required; other expectations are optional. `source` (`plan` | `metric` | `generated_sql`) may also be specified.
- `fields` / `forbidden_fields`: required / forbidden `resource.field` references.
- `joins`: require an equality join between two fields in WHERE or JOIN ON, regardless of operand order.
- `aggregates`: required SELECT aggregates (`count / sum / avg / min / max`, `field` as `resource.field` or `*`, `distinct` defaults to false). Treat `COUNT(*)`, `COUNT(1)` and `COUNT(field)` on a non-null field without outer JOINs as row counts.
- `group_by`: fields required in GROUP BY.
- `constraints`: field, comparison operator and an optional target (exactly one of `value`, `parameter_value` or `parameter: true`).
- `parameters`: match required parameters by value and optional type.
- `forbidden_parameter_values` / `forbidden_literals`: detect regressions that parameterize Rule constants or hard-code user values as literals.
- `forbidden_constructs`: `select_star`, `relative_time_function`.
- Optional `reference` is an authored expected Operation used as a test fixture, not for scoring.
- Empty suites, duplicate case IDs, invalid timezones or Context integrity, and contradictory comparison targets fail before execution.

For `source: plan`, a one-operation plan can use the ordinary SQL expectations above.
For multiple operations, set `expect.steps` to 1–5 objects containing those SQL expectations
and omit top-level SQL expectations. The count must match exactly. Independent steps are
matched one-to-one without requiring response order; one operation cannot satisfy two entries.
A multi-operation response without explicit step expectations fails with a setup guidance message.
Each operation is independently revalidated with its own source policy (Tool, generated SQL or metric contract). Reported validation
errors retain their step IDs. Malformed plans and invalid step IDs fail evaluation.

### 9.2 Scoring

- Compare status first. For `ok`, check the public validation result and rerun static validation.
- Extract resources, fields, joins, aggregates, group_by, constraints and forbidden constructs from the sqlglot AST; do not compare SQL strings. The scorer implements field tracking independently of the validator to avoid hiding the validator's tracking bugs.
- Supported comparisons: `= / != / <> / IN / NOT IN / >= / <= / > / <`, accounting for NOT/AND/OR, operand reversal, direct-field cast/date wrappers and BETWEEN. Compare numbers with Decimal, dates at midnight in the Context timezone, and date-times by instant.
- The evaluator checks explicit AST expectations. It does not prove complete semantic equivalence, aggregate results/row counts or Rule application on every path.
- Case results include pass/fail, failed expectations with explanations, generation_id, reference_time and validation errors. Request errors count as failures, and remaining cases continue.

The commerce suite has 18 cases (`ok`: 12, `ambiguous`: 3, `unanswerable`: 3). Period cases use half-open ranges with reference time `2026-10-02T09:00:00+09:00` and timezone `Asia/Seoul`.

### 9.3 CLI

Requests go to a running Sidewit API. Provider and model follow server configuration.

```bash
docker compose run --rm --no-deps -v ./evals:/evals:ro api sidewit-eval run /evals/commerce --api-url http://api:8000
```

- Defaults: `--api-url http://localhost:8000`, `--timeout 120` seconds. Use `SIDEWIT_API_KEY` or `--api-key` when authentication is enabled. Select cases with `--case` and write JSON reports with `--out`.
- Create the suite's Context if missing; update it with `expected_version` when it differs from the fixture. It remains after evaluation.
- Exit codes: 0 for all cases passing, 1 for case failures, 2 for setup, connection or report errors.
- Real-provider runs call an LLM and are excluded from default CI. Do not report CI FakeProvider results as real-model quality.

---

## 10. UI

JSON numbers that the browser cannot preserve exactly are never silently rounded. API responses produce `numeric_precision_loss`; Metrics JSON retains the original text and blocks saving. Integers must be within ±9007199254740991, and decimal values must survive a string round trip unchanged. Use an API client with exact numeric handling for values outside this range.

The UI is a React SPA with three screens. Every function is available through the API; the UI is an administrative convenience.

**Context**
- List and create Contexts.
- Import DDL within Schema: paste → Preview changes → apply table additions/changes. Imports use the current draft, allowing successive imports without saving.
- Preview additions, changes, deletions and relation changes. Add/merge is the default; full replacement is a separate Advanced action requiring preview confirmation.
- Block application if DDL, draft, pending foreign keys or server version changed after preview. Reapplying identical DDL shows no changes.
- Retain unresolved foreign keys for the editing session. Saving remains blocked until a later import resolves them or the user explicitly excludes them.
- Preserve DDL input across tab/panel changes. Confirm navigation away from unsaved Context/DDL edits, and retain edits after save failures.
- Edit Resources/Fields, including descriptions and `allowed_values` for enum-like fields only.
- Edit Relations, Business Rules (optional constant: field/operator/value) and Metrics JSON.
- **Allow model-written SQL**(`allow_generated_sql`)
- Show versions when saving and a warning banner when revalidation finds invalid Tools.

**Playground**
- Show the submitted question, Context version, reference time, metric and date basis beside the result. Warn when inputs or Context change; label draft creation as using the previous result when appropriate.
- Select Context, enter a question and generate. Contexts with metrics offer metric selection and, when model-written SQL is allowed, **Answer only with a registered metric** (`require_metric`).
- `ok`: read-only Monaco SQL, a Parameters table, validation results/warnings, provenance badge (saved Tool / metric contract / model-written SQL), and an explanation that execution safety, access control, cost and business correctness are not guaranteed. Provide [Copy SQL], [Copy JSON] and [Copy API request]. New results offer [Create Tool draft]; reused Tools link to their name/version instead. No feedback buttons are shown.
- `ambiguous`: reason, structured questions and an answer form. Choices fill an editable answer;
  free text is always allowed. Continue resubmits the original request, accumulated answers, fixed
  reference time and expected Context version. Disable continuation after inputs/Context change;
  retain answers on request failures. A fresh Generate action starts a new request without answers.
  Model-authored reasons/questions follow the original question's language; server-generated
  contract messages remain English. `unanswerable`: reason.
- `422 generation_invalid`: show "Rejected generation". Fetch rejected SQL through `GET /generations/{id}` and show it only in a collapsed debug view, visually distinct from a valid Operation (dashed red border, "Not validated · Do not execute"). Do not provide copy buttons or Tool draft entry points for rejected SQL. Other request errors use their own error messages.
- Explain that recent generation history is held in server memory and disappears on restart.

**Tools**
- List name, Context, status badge and version, with a `metric` badge for bound Tools, plus [New Tool].
- Editor: name, description, input editor (name/type/format/description/enum) and Monaco SQL editor. Debounce `/validate` calls while editing and show errors inline. Explain that `NOW()` is allowed and validation is not a Generate-compatibility check.
- Enable [Save] only after validation against the current edits and Context version. After Context changes, do not reuse an old passing verdict while revalidation is pending or failed. If the Tool changes elsewhere, notify the user without discarding edits; version conflicts return 409.
- Metric-bound Tools have read-only SQL and input names/types; **Rebuild from contract** updates them.
- Test invocation panel: form initialized with `example_arguments`, with the response displayed as JSON.

- Lock inputs and SQL editing during save, delete or contract rebuild requests. On failure, unlock without discarding inputs.
- Confirm navigation away from unsaved Tools; while requests are pending, ask users to remain until completion. Navigation after successful save/delete needs no additional confirmation. Browser close/refresh uses the native leave-page warning.
- Invocation parameter tables use the response Operation, not the current form. Warn when the result belongs to an earlier saved Tool or Context version.

**Implementation notes**
- Avoid labels suggesting SQL execution, such as `Run Query` or `Execute SQL`. Use "SQL operation" or "Resolved SQL operation" together with "not executed by Sidewit".
- Tool input lists and invocation forms follow `input_schema.required` order.
- Bundle Monaco locally and load it lazily only on screens that display SQL.
- Enter the Sidewit API key through the header dialog; store it in localStorage and send it as `Authorization: Bearer`. This is a convenience for local/self-hosted development, not a long-term authentication model.

---

## 11. Storage

Sidewit uses no database. State lives in files under the data directory (`SIDEWIT_DATA_DIR`, default `/data`), mounted as a volume when deployed.

```
/data
├─ contexts/<context_id>.json
└─ tools/<tool_name>.json
```

- Files use UTF-8 JSON, sorted keys and two-space indentation for readable diffs. Numbers are preserved without precision loss.
- **At startup**, read and validate every file. Unreadable directory listings or any format/integrity error (invalid JSON, failed Context validation, duplicate names, Tools referencing missing Contexts, etc.) report the path and cause and prevent startup. Missing directories are created empty; unreadable directories are never treated as empty.
- **Writes** use a temporary file in the same directory, fsync, rename and parent-directory fsync. One API process serializes changes synchronously without intervening awaits. Save the Context before Tool revalidation results. This is not a multi-file transaction: failures or interruptions can leave partial changes, so clients must retrieve current versions. Tools tied to older Context versions are revalidated before invocation. Failed writes restore memory and validation caches; if files cannot be reloaded, the API fails closed with 503 and requires restart.
- Version conflicts (`expected_version`) and name conflicts use current in-memory state.
- Tool validation caches belong to each store instance. Context/Tool saves invalidate related verdicts; recovery and shutdown clear the cache. Caching does not eliminate all SQL reparsing: invocation always checks actual input values.
- **Use one instance and one API worker.** Hold the data directory's `.lock` for the process lifetime; a second process opening the same directory refuses to start. Use a local filesystem. Network shares and multiple instances are unsupported.
- Manual file changes take effect only after restart, and a running Sidewit may overwrite them. Stop Sidewit before editing files directly.
- Back up by copying the directory. Restrict access because it contains Contexts with schema descriptions, business rules and allowed values.
- Generations are not stored in this directory (§3.4).
