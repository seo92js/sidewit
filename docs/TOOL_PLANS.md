# Independent saved Tool plans

A plan is one response containing 1–5 SQL operations. Sidewit selects saved Tools and fills
their arguments; it never executes SQL, combines query results or submits further work.

Generate automatically selects one or more saved Tools. If no saved Tools exist, or they
cannot cover the whole question, the existing metric/SQL generation path handles the whole
question subject to Context policy. No UI toggle or API mode is required. Mixed Tool/generated
SQL plans and steps that consume another step's result remain future work.

There is one automatic selection flow; the Generate API has no mode option.

## Request

POST /api/v1/generate with your Context ID:

~~~json
{
  "context_id": "<context UUID>",
  "question": "Count stores in Seoul and Busan separately",
  "reference_time": "2026-10-13T09:00:00+09:00"
}
~~~

If the Context has a count_stores Tool taking a region, Sidewit may select it twice with
different arguments. It may also select different Tools for different parts of a request.
Tool selection and completeness are model judgments: review the actual SQL and arguments.

The optional reference time and stateless clarification fields apply to all requests.
Explicit metric options (metric_id, date_field, period, require_metric=true) bypass Tool
selection. Metric-bound saved Tools remain eligible and keep their bindings.

## Success response

Plan responses have status **ok**, source **plan**, execution **independent**, generation_id,
context_version and an **operations** array. There is no top-level operation or validation.
Generation fallback instead returns the existing single operation response with source
generated_sql or metric. Clients must dispatch by source, even when a plan has just one step.

Each plan operation contains:

| Field | Meaning |
|---|---|
| id | Server-assigned step_1, step_2, etc.; unique within this response |
| source | tool in this release; clients should dispatch by source |
| selected_tool | Saved Tool name and version |
| operation | SQL dialect, original saved statement, bound parameters and parameter types |
| validation | The result of the same validation used for direct Tool invocation |
| metric_binding | Metric binding when the saved Tool has one; otherwise null |

All operations must pass validation before the response is returned. Two calls with the same
Tool and identical validated arguments are rejected. The limit is five calls; a one-call
plan still uses the operations array. Each operation owns its parameters: do not merge
parameter maps or concatenate SQL statements.

## Application integration

1. Check HTTP status and the response status before accessing operations.
2. For a successful plan, review all selected Tools and their arguments against the question.
3. Apply your application's execution authorization and limits to each operation.
4. Execute the operations against your own database, retaining step IDs with their results.
5. Present or compare the results in your application.

Independent means there are no step-output bindings or required result dependencies.
Your application can choose its execution order, concurrency, transaction policy and handling
of database failures. An all-valid plan is not an execution transaction or an execution-safety
guarantee. Standard least-privilege roles and query limits still apply.

The Playground automatically displays per-step SQL/parameter/validation cards, **Copy plan JSON** and **Copy API request**. There is no combined Tool-draft action:
the plan already references saved Tools. POST /tools/draft for a plan returns 409
plan_not_draftable.

## Non-success outcomes

- **ambiguous:** missing inputs or unclear intent. Return answers using the original question,
  resolved reference time and expected_context_version.
- **unanswerable:** too many calls, a need for dependent execution, or a question the Context
  cannot answer. Missing Tools/coverage use the normal generation path instead.
- **422:** invalid selection, arguments, duplicates or incompatible request options.
- **409:** Context/Tool changed during selection, or a selected Tool no longer validates.
- **502/504:** provider failure or the existing overall generation deadline.

If any step fails, the response exposes no partial operations. Step-specific selection or
invocation errors identify step_id when available. Successful plans and masked failure
diagnostics use the existing bounded, in-memory generation history; SQL and arguments are
not added to application logs. Raw model selections in the administrative history are
debugging data, not executable plans.
