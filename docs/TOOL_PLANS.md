# Independent operation plans

A plan is one response containing 1–5 independent SQL operations: saved Tools, newly generated
SQL, or SQL compiled from registered metrics. Sidewit never executes SQL or combines results.

Generate automatically reuses saved Tools for the parts they cover. The existing metric/SQL
planners generate operations for any remaining parts, subject to Context policy. If no saved
Tool fits, generation handles the whole question as a single operation. No UI toggle or API
mode is required. Steps that consume another step's result are not supported.

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
For example, “Count Seoul stores and list Busan store IDs” can reuse a saved count Tool and
generate an ID-list query. The count SQL remains unchanged. Both operations must validate.
Tool selection, decomposition and completeness are model judgments: review SQL and arguments.

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
| source | tool, generated_sql or metric; dispatch by source |
| selected_tool | Saved Tool name and version; present only for source tool |
| task | Subquestion covered by a generated/metric step |
| tool_suggestion | Suggested name/description for a generated step, when available |
| operation | SQL dialect, statement, bound parameters and parameter types |
| validation | Tool invocation validation, generation validation or metric contract validation |
| metric_binding | Metric binding for a bound Tool or compiled metric step; otherwise null |

All operations must pass validation before the response is returned. Two calls with the same
Tool and identical validated arguments are rejected. The limit is five total operations; a one-call
plan still uses the operations array. Each operation owns its parameters: do not merge
parameter maps or concatenate SQL statements.

## Application integration

1. Check HTTP status and the response status before accessing operations.
2. For a successful plan, review every operation and its parameters against the question.
3. Apply your application's execution authorization and limits to each operation.
4. Execute the operations against your own database, retaining step IDs with their results.
5. Present or compare the results in your application.

Independent means there are no step-output bindings or required result dependencies.
Your application can choose its execution order, concurrency, transaction policy and handling
of database failures. An all-valid plan is not an execution transaction or an execution-safety
guarantee. Standard least-privilege roles and query limits still apply.

The Playground displays each operation's source, SQL, parameters and validation, plus
**Copy plan JSON** and **Copy API request**. New SQL is explicitly marked as a draft.

To save one generated step as a Tool, send `POST /tools/draft` with `generation_id` and
`step_id` (for example `step_2`). The draft includes `source_generation_id` and `source_step_id`;
retain both when sending it to `POST /tools`. The selected step's metric binding cannot be
stripped or replaced. Saved Tool steps link to the existing Tool instead. Drafting the entire
plan without a step returns 409 plan_not_draftable; a saved Tool step returns 409 step_already_saved.

Generation runs concurrently for uncovered tasks (at most four), with one repair per task,
under the same 90-second overall request deadline. Every task uses the original request and
answers for literal validation and the same reference time. A model-written subquestion
cannot redefine the user's input values. Context and selected Tool versions are checked again
after generation. Results from separate versions cannot be combined into a successful plan.
Unexpected worker exceptions and request cancellation cancel all remaining workers and wait
for their cleanup before the request exits. Expected provider/validation failures retain the
normal error handling; no successful partial operations are published.

If multiple tasks need clarification, only the first unresolved task's question batch is
returned, in plan order. Questions from different tasks are neither merged nor deduplicated.
Submit answers with the original request and prior answers; the entire plan is evaluated
again, so another unresolved task may be clarified on a later request. Successful operations
are not cached or executed between these rounds. This can require additional model calls.

Before returning any clarification batch (including selection and standalone generation), the
server checks the remaining 24-answer limit and the 16,000-character continuation budget,
reserving at least one character for each new answer. If the batch cannot fit, it returns
unanswerable with a request-splitting instruction rather than truncating questions. Actual
answer length is still validated by the UI and API; a batch fitting the minimum does not
promise that arbitrarily long answers will fit.


## Non-success outcomes

- **ambiguous:** missing inputs or unclear intent. Return answers using the original question,
  resolved reference time and expected_context_version.
- **unanswerable:** too many calls, a need for dependent execution, or a question the Context
  cannot answer. Missing Tools/coverage use the normal generation path instead.
- **422:** invalid selection, arguments, duplicates, generated SQL or incompatible request options.
- **409:** Context/Tool changed during selection or generation, or a selected Tool no longer validates.
- **502/504:** provider failure or the existing overall generation deadline.

If any step fails or needs clarification, the response exposes no partial operations. Step-specific selection or
invocation errors identify step_id when available. Successful plans and masked failure
diagnostics use the existing bounded, in-memory generation history; SQL and arguments are
not added to application logs. Raw model selections in the administrative history are
debugging data, not executable plans.
