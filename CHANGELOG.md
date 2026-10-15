# Changelog

## 0.1.0 — Unreleased

Initial release candidate.

### Breaking changes

- Tool resource URLs now use the immutable UUID: `/tools/{tool_id}` and its invoke/refresh
  subpaths. Use `id` from create/list/get or `selected_tool.id`; names are editable labels.
  Name-based resource URLs are removed. Invoke responses also include `tool_id`.

- Every successful `POST /generate` response now returns `operations`, even for a single
  generated SQL or metric result. Read operation, validation, metric_binding and tool_suggestion
  from each step instead of the top level. No legacy response mode is retained.

### Fixes

- Keep failed MCP discovery checks out of the validation cache so later REST or MCP invocation
  persists the Tool's invalid status instead of leaving it displayed as valid.

- Protect navigation during pending logout and avoid immediately signing out a new login from
  `/logout`. Reject malformed Unicode passwords with 422 instead of an unhandled 500.

- Re-read Tool availability and versions after failed activation requests, including storage
  errors and lost responses. Preserve local edits and avoid automatically retrying writes.

- Reject contradictory generation/step sources before evaluation so generated SQL cannot
  be checked as a saved Tool. Preserve inferred step IDs in Tool drafts and saved provenance.

### Features

- Add an opt-in MCP Streamable HTTP endpoint in the API container with a separate Bearer key,
  Host/Origin checks, paginated Context/Tool discovery, generation and deterministic invocation.
  Share application processing with REST, including validation, version checks, clarification
  continuation, generation deadlines and temporary history. Document no-auth and client limits.

- Add a single environment-configured web administrator with `admin/admin` defaults, cookie
  sessions and logout. Remove browser API-key storage and the API-key dialog; public API
  authentication remains separate and unchanged. Preserve unsaved edits across session expiry
  and document credential recovery, container recreation and HTTPS deployment settings.

- Allow renaming saved Tools in the editor and update API. Preserve identity and activation,
  reject duplicate names, and keep atomic file writes independent of mutable names.

- Add persisted Tool activation, independent of SQL validity. Disabled Tools remain editable
  but are excluded from selection and invocation. Re-enabling validates against the current
  Context; version changes reject stale plans. Add list/editor controls and conflict handling.

- Return a ready-to-resubmit `continuation_request` for ambiguous generation responses,
  preserving options, prior answers, reference time and Context version. Use it in the
  Playground and document a minimal API client flow.

- Simplify the Playground with collapsed advanced options and visible active settings.
  Summarize previous clarification answers while keeping them editable. Collapse validation
  explanations and check lists while leaving errors and warnings visible.

- Reuse saved Tools and generate uncovered SQL/metric tasks in one independent plan. Reuse
  normal validation/repair, preserve original request values, and recheck versions after
  generation. No partial operations are returned on failure or clarification.
- Label generated steps in the Playground and allow saving each as a Tool draft with its
  generation/step provenance and metric binding. Selection uses tool_plan_v3.

- Evaluate saved Tool plans with independent validation and one-to-one step expectations;
  remove the unused Playground clarification display branch.

- Automatically select independent saved Tool plans in the API and Playground, falling back
  to metric/SQL generation when no saved Tool matches. Plans contain 1–5 validated
  operations with per-step Tool versions, SQL, parameters and validation results. Repeated
  Tools with different arguments are supported; partial success, duplicate calls, dependent
  chaining are not supported.


- Fix SQLGlot canonical names for boolean aggregates and string position; remove unreachable
  function allowlist entries. Avoid treating temporal-to-text comparisons as database-clock
  expressions. Lock Tool editing during explicit reloads and preserve drafts on reload failure.

- Add Tool conflict recovery, protect navigation after Context deletion, and allow correction
  of previous clarification answers with a client-side total-length check.
- Close dollar-string value/placeholder checks and database-clock bypasses in supported
  temporal comparisons; reject non-finite Context allowed values even for custom types.
- Render Tool-selection reference times in the Context timezone.
- Remove unused historical prompt files; previous versions remain in Git history.
- Add production CSP, framing and MIME-sniffing headers; use Node.js 24 for Docker and CI.


- Continue ambiguous requests with structured clarification choices or free-text answers in the
  Playground and API, preserving the reference time and checking the Context version.

- Select saved Tools with validated arguments, explicit provenance and no silent fallback on Tool errors. Explicit metric requests retain their existing path.

- Context management for schemas, relations, descriptions and business rules, with DDL import.
- SQL draft generation with OpenAI or Gemini, static validation, and ambiguous/unanswerable responses.
- Optional metric contracts for server-compiled SQL.
- Reviewed SQL Tools with input validation, version-conflict checks, Context revalidation and
  deterministic invocation.
- JSON file storage for Contexts and Tools, plus bounded Generation history in memory.
- Docker deployment, an optional management UI, and regression evaluation suites.
- Apache-2.0 license.

### API changes

- Generate automatically selects saved Tools without a mode option. Saved Tool success returns
  `source: "plan"`, `execution: "independent"` and `operations`, including one-call plans.
  Plans may include generated/metric steps. Whole-question generation also returns an operations
  array containing one step. Administrative generation detail retains its diagnostic fields.
  Selection uses tool_plan_v3.
- Remove legacy single-Tool response handling and duplicate text-only clarification/DDL warning
  fields. Use clarification_questions and warning_details. Unknown Generate request fields
  are rejected instead of silently ignored.


- SQL function policy and timezone-aware Tool selection use prompt versions generate_v10,
  hybrid_v5 and tool_plan_v3.

- Enforce a closed SQL function allowlist for generation, validation and Tool invocation.
  Context definitions add optional sql_extensions: ["postgis"] for supported spatial functions.
  **Compatibility:** unknown/schema-qualified functions and escape/Unicode string literals
  that previously passed validation are now rejected. Revalidate existing Tools after upgrading.
- Report stale and omit the old validation verdict when a Tool's checked Context version differs.


- Generate accepts optional `clarification_answers` and `expected_context_version`. Ambiguous
  responses add `clarification_questions` and resolved `reference_time`; text-only `clarifications` field is removed.

- Natural-language requests with saved Tools may use additional LLM calls. Direct Tool invocation remains LLM-free.

### Removed

- Removed the temporary thumbs-up/down feedback UI, its generation feedback endpoint and response field. Feedback was not persisted or used by generation.

### Fixes

- Clarify one unresolved plan task at a time and check follow-up capacity before returning
  questions. Keep the same count/text budgets for incoming answers and outgoing questions.
- Cancel and join concurrent generation workers on unexpected exceptions or request cancellation.

- Resolve metric date choices through structured planner intent instead of matching field mentions
  in clarification answers; validate membership and keep unresolved choices ambiguous.
- Preserve the continuation Context version after request failures so stale initial UI metadata
  cannot incorrectly disable retries.

- Record failed Tool selections in generation history with usage, debugging details and a generation ID in the error response, without falling back to SQL generation.

- Preserve unsaved Tool edits when background refreshes fail.
- Reset incompatible invocation inputs when a saved Tool schema changes, keeping displayed and submitted values consistent.
- Lock Context creation inputs while saving and retain them on failure.

- Provide exact quoted SQL identifiers in generation and repair prompts for mixed-case or reserved Context names (`generate_v8`, `hybrid_v3`).

- Render rejected SQL as plain text without loading Monaco; show explicit messages for missing generation logs or SQL.

- Ignore SQL comments and quoted contents when suggesting Tool inputs.
- Classify DDL warnings by stable codes in warning_details.
- Prevent navigation during Context saves and remove unused UI options and stale comments.

- Lock Tool editing during save, delete and contract rebuild requests; preserve input on failure and confirm unsaved navigation.
- Render invocation parameters from the returned operation, with a notice when the saved Tool has changed.
- Show the submitted Playground question and settings, and distinguish previous results after input changes.

- Merge DDL imports into the current unsaved editor contents; preserve sequential imports and descriptions.
- Preview table/column/relation changes, separate schema replacement, and retain unresolved foreign keys for subsequent imports.
- Keep DDL input across editor tabs and confirm navigation away from unsaved Context edits.

- Reject browser JSON numbers that would lose precision, including API responses and metric edits.
- Tie DDL previews to their source Context version and prevent stale preview application.
- Remove SQL and private values from SQLGlot diagnostic logs.
- Bound SQL/DDL complexity and report recursion failures as validation errors.
- Include prompt resources in Python wheels; verify all generation prompts from the built wheel.

### Operating limits

- No database connection or SQL execution. Static validation does not guarantee execution
  safety or business correctness; the application controls execution and permissions.
- One API process per local data directory. Generation history is lost on restart.
- Real-provider evaluations include known value-translation failures. See the
  [evaluation summary](evals/README.md#file-storage-acceptance-2026-10-12-kst).

See [deployment and recovery](docs/DEPLOYMENT.md) for backups and migration from development
versions that used PostgreSQL metadata storage.
