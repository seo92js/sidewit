# Ecommerce example

A runnable API walkthrough using synthetic metadata. No application database is needed;
Sidewit does not execute the DDL or generated SQL. Start the stack with the root
[Quick Start](../../README.md#quick-start) before running these commands.

| File | Purpose |
|---|---|
| [schema.sql](schema.sql) | DDL for customers, orders and shipments |
| [context.json](context.json) | Schema descriptions, allowed values, relations and business rules |
| [metric-context.json](metric-context.json) | The same example with a registered revenue metric |
| [question.json](question.json) | Question and fixed reference time |
| [quickstart.py](quickstart.py) | Context → DDL preview/save → Generate → Tool draft/save/invoke |

## Register the Context

From the repository root:

```bash
docker compose run --rm -T --no-deps -v ./examples/ecommerce:/examples:ro api python /examples/quickstart.py --context-only
```

This does not call an LLM. It previews the DDL, checks warnings, adds descriptions and rules,
then saves the Context. Use [the UI flow](../../README.md#quick-start) to generate and review SQL.

For manual setup, create a PostgreSQL / Asia/Seoul Context in the UI. Paste `schema.sql`
into **Import DDL**, review **Preview import**, then **Apply to draft**. Add descriptions and
rules from `context.json` before saving: DDL alone does not define business meaning.

## Full API walkthrough

With a real provider configured:

```bash
docker compose run --rm -T --no-deps -v ./examples/ecommerce:/examples:ro api python /examples/quickstart.py
```

This calls the provider and, on `ok`, saves and invokes a Tool. It returns SQL and binding values,
not query results. Inspect their meaning before using them in an application.

- Context defaults to `ecommerce`. If an existing Context has conflicting schema or rules,
  the script stops; use `--context-name ecommerce_2` rather than overwriting it.
- Tool defaults to `ecommerce_revenue`. Names are globally unique; use
  `--tool-name ecommerce_revenue_2` on another run.
- `--api-url` defaults to `http://api:8000` and must be reachable from the container.
  Authentication uses `SIDEWIT_API_KEY` from the environment without printing it.
- Exit codes: 0 completed, 1 ambiguous/unanswerable, 2 HTTP/configuration/file error.
  An unconfigured provider returns `503 llm_not_configured`; no Tool is saved.

## Business meaning

Revenue is the sum of `amount` for `COMPLETED` orders, measured by `paid_at`. Order counts use
`ordered_at` and exclude `CANCELLED` orders. Shipment dates have no preferred field, so period
questions may be ambiguous. Subscription, refund and product-category data are absent.
`reference_time` anchors a period; it does not decide which date column to use.

Keep the original example values, including `서울` (Seoul). The authored Operation example
is in the root [API example](../../README.md#api-example). Evaluation results and known model
limitations are in the [evaluation guide](../../evals/README.md).

## Registered metric variant

```bash
docker compose run --rm -T --no-deps -v ./examples/ecommerce:/examples:ro api python /examples/quickstart.py --metric-contracts --context-name metric_commerce --tool-name metric_revenue
```

The server compiles the aggregate, fixed status filter and period bounds from
`metric-context.json`; the model selects inputs and a period. Use separate names from the free-SQL
example. See [metric contracts](../../docs/METRIC_CONTRACTS.md) for guarantees and limits.
