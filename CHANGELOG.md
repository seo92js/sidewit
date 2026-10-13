# Changelog

## 0.1.0 — Unreleased

Initial release candidate.

### Features

- Continue ambiguous requests with structured clarification choices or free-text answers in the
  Playground and API, preserving the reference time and checking the Context version.

- Select one matching saved Tool before generating a new operation, with validated arguments, explicit provenance and no silent fallback on Tool errors. Explicit metric requests retain their existing path.

- Context management for schemas, relations, descriptions and business rules, with DDL import.
- SQL draft generation with OpenAI or Gemini, static validation, and ambiguous/unanswerable responses.
- Optional metric contracts for server-compiled SQL.
- Reviewed SQL Tools with input validation, version-conflict checks, Context revalidation and
  deterministic invocation.
- JSON file storage for Contexts and Tools, plus bounded Generation history in memory.
- Docker deployment, an optional management UI, and regression evaluation suites.
- Apache-2.0 license.

### API changes

- Generate accepts optional `clarification_answers` and `expected_context_version`. Ambiguous
  responses add `clarification_questions` and resolved `reference_time`; the existing
  `clarifications` string list remains compatible. Prompt versions advance to `generate_v9`,
  `hybrid_v4`, `metric_plan_v3` and `tool_selection_v2` for structured clarification output.

- Generate can now return `source: "tool"` and `selected_tool` (name/version); clients with exhaustive source handling must support the new value. Natural-language requests with saved Tools may use an additional LLM call. Direct Tool invocation remains LLM-free.

### Removed

- Removed the temporary thumbs-up/down feedback UI, its generation feedback endpoint and response field. Feedback was not persisted or used by generation.

### Fixes

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
- Classify DDL warnings by stable codes while retaining legacy warning messages.
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
