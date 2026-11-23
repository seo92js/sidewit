# Evaluation suites

Golden suites for Generate regressions. Each directory holds a `context.json` (the Context to
create or update) and `cases.yaml` (questions, expected status and deterministic AST
expectations). Default scoring never compares SQL strings, executes SQL or asks an LLM to judge.
The optional execution-result suite compares rows in a disposable PostgreSQL instance.

| Suite | Role | Generate path |
|---|---|---|
| [`commerce`](commerce/cases.yaml) | **Development set.** The SQL prompt was iterated against its failures, and the prompt's illustrations cover the same kinds of decisions. A high score here shows the prompt works on what it was tuned for, not that it generalizes. | model-written SQL |
| [`library`](library/cases.yaml) | **Separate-schema regression set.** Different schema, values and periods, including joins, business-rule constants, date selection and unavailable data. | model-written SQL |
| [`metric-library`](metric-library/cases.yaml) | **Separate-schema regression set**, metrics only: the library questions answered through registered metric contracts. | metrics only (`allow_generated_sql: false`) |
| [`hybrid-library`](hybrid-library/cases.yaml) | **Separate-schema regression set** for hybrid routing: questions a metric answers (`source: metric`), questions none does (`source: generated_sql`), and near misses — a metric plus an extra condition — where the generated SQL must still apply the metric's fixed filters. | hybrid (`allow_generated_sql: true`) |
| [`execution-workshops`](execution-workshops/cases.yaml) | **Execution-result baseline.** 24 new questions over synthetic workshops, bookings and check-ins. Reference SQL supplies expected rows, including duplicate-sensitive and ordered cases. | model-written SQL |

Report all suite results and their exposure history when describing quality. These suites
were initially held out from prompt tuning, but cases have since appeared in reviews and
regressions. They are not a fresh independent benchmark. Every `ok` case may carry an authored
`reference` answer; backend tests check that each reference satisfies its own expectations in
every structural suite, which proves those cases are satisfiable. Structural scoring ignores
references; execution scoring uses their result rows as the oracle.

## Coverage

- `commerce`: development cases used during prompt tuning.
- `library`: 9 cases covering value preservation, joins, fixed rules, date-field selection,
  ambiguity and unavailable data. Bookkeeping timestamps must not introduce false ambiguity.
- `metric-library`: 10 cases using registered contracts, including nullable-ID counting,
  non-null counts and grouping. Model selection and input extraction remain probabilistic.
- `hybrid-library`: 9 cases covering 3 metric routes, 4 generated-SQL routes (including
  2 near misses), one ambiguous question and one unanswerable question.

Use the separate-schema suites to detect regressions rather than repeatedly tuning prompts
to their expected answers. Compiler guarantees and limits are described in
[metric contracts](../docs/METRIC_CONTRACTS.md).

## Run

The provider and model are the API server's configuration (`SIDEWIT_LLM_*`). From the
repository root, with the stack running:

```bash
mkdir -p eval-results
docker compose run --rm -T --no-deps -v ./evals:/evals:ro -v ./eval-results:/results \
  api sidewit-eval run /evals/library --api-url http://api:8000 --out /results/library.json
```

Exit code: 0 all passed, 1 some case failed, 2 setup error. The default run executes no SQL.
Run results are not committed to the repository.

## Execution-result baseline

Configure a real provider in `.env`, then run from the repository root:

```bash
bash scripts/run-execution-eval.sh
# Or one case while developing:
bash scripts/run-execution-eval.sh seoul_member_count
```

The script creates a separate Compose project with a disposable Sidewit API and PostgreSQL
database. It does not mount `./data` or publish a database port. PostgreSQL receives only
[synthetic fixture data](execution-workshops/init.sql); Sidewit receives the Context and questions,
not the reference SQL. A non-superuser role with `SELECT` grants executes both reference and
generated SQL in read-only transactions with a five-second statement timeout and a 1,000-row
limit. Containers and the temporary database are removed when the command exits.

The timestamped report under `eval-results/execution-workshops-*.json` includes status, structural checks,
execution row counts and match/failure, request latency and provider-reported token counts.
It does not contain result rows, database credentials or a dollar-cost estimate. Row comparison
ignores order unless the reference sets `ordered: true`, and preserves duplicate rows. A case
passes only when both structural expectations and result rows match. A missing or failing
reference query is a setup error; an invalid generated query is a case failure.

These published, authored cases are a reproducible baseline, **not an independent accuracy
benchmark**. One small dataset can let semantically different SQL return the same rows; review
failures and add discriminating data before drawing broader conclusions. This optional evaluator
connects to PostgreSQL only during development; the Sidewit API does not execute SQL.
The first suite has no saved Tools, so it measures generated SQL rather than Tool selection or
mixed plans.

### First real-provider run (2026-11-03 KST)

OpenAI `gpt-6-luna` passed **23/24** new execution cases. All 24 returned structurally valid
SQL; one produced the wrong result. For `intro_paid_booking_count`, the model bound the title
`AI 입문 워크숍` although the stored workshop title is `AI 입문`. The query therefore counted no
matching bookings. This is a semantic value-extraction failure, not a database or evaluator
error. The run reported 57,742 input and 6,030 output tokens; summed Generate request latency
was 94.4 seconds. Results can vary across model calls. The local JSON report is ignored by Git.

## Evaluating operation plans

A successful saved Tool or mixed plan returns `source: plan` and `operations`. One-operation
plans use the usual SQL expectations. For multiple operations, define expectations per step:

```yaml
expect:
  status: ok
  source: plan
  steps:
    - resources: [customers]
      constraints:
        - {field: customers.region, op: "=", parameter_value: Seoul}
    - resources: [customers]
      constraints:
        - {field: customers.region, op: "=", parameter_value: Busan}
```

Do not mix `steps` with top-level SQL expectations. The evaluator requires the exact number
of operations and matches them one-to-one regardless of order. Each step is independently
validated using its Tool, generated-SQL or metric policy; failures identify the expected step and returned step ID. Tool registration is
still separate from suite setup. Without saved Tools, Generate uses the normal generation path.

## File-storage acceptance (2026-10-12 KST)

One run per suite against the file-storage candidate, using real provider calls through the
Docker web proxy. Both providers also passed the ecommerce DDL → Generate → Tool draft →
save → invoke flow, and saved Tools still invoked after restart and backup restoration.

| Provider / model | commerce | library | metric-library | hybrid-library |
|---|---:|---:|---:|---:|
| OpenAI / `gpt-6-luna` | 18/18 | 9/9 | 10/10 | 8/9 |
| Gemini / `gemini-3.1-flash-lite` | 18/18 | 8/9 | 10/10 | 9/9 |

Known failures: OpenAI's hybrid metric route changed the requested value `京都` to `교토`;
Gemini's generated SQL route changed it to `Kyoto`. Both results passed static validation,
but failed the authored value expectation. Review parameter values as well as SQL before
using a generated Operation. Compiling a metric contract does not prove the model selected
the intended parameter value.

These are exposed regression cases, not an independent accuracy benchmark. The repair check
used an injected invalid first response followed by a real provider repair response; it is a
mixed fixture/real-provider test. Browser click-through was not verified in this run.

## Saved Tool selection smoke check — 2026-10-13

Using `gemini-3.5-flash-lite` with `tool_selection_v1` and one synthetic
`count_stores_by_region` Tool (Store.id, Store.regionSido; allowed regions 서울/부산):

| Question | Observed result |
|---|---|
| 서울 매장 개수 | `source: tool`, saved SQL with region 서울; one model call |
| 부산 매장의 id 목록 | `source: generated_sql`, ID projection; two model calls |
| 지역별 매장 개수를 알려줘 | `source: generated_sql`, grouped count; two model calls |
| 그 지역 매장 개수 | `ambiguous`, asks for the region; one model call |

All four requests returned HTTP 200 with the expected route. This is a small synthetic smoke
check, not a routing accuracy benchmark. OpenAI Tool selection has not been tested with a real
provider. Selection correctness and argument meaning still require application review.

### Stateless clarification smoke check (2026-10-13)

A real `gemini-3.5-flash-lite` check used a temporary synthetic Store Context with region values
`서울`/`부산`, two timestamp fields (`opened_at`, `closed_at`) and a saved region-count Tool.
No application database was connected and no SQL was executed.

- `그 지역 매장 개수` returned Korean clarification choices `서울` and `부산`.
  Answering `서울` reused the saved Tool with that exact bound argument.
- `지난달 매장 개수` asked which date to use. Answering `Store.opened_at 기준, 지역 조건 없이 전체 매장`
  produced new SQL using `opened_at` and the range September 1 inclusive to October 1 exclusive
  in Asia/Seoul, with an October 13 reference time.
- Generation history was cleared between each question and answer. Both continuations still
  succeeded using the original request, answers, reference time and expected Context version.

This is a small smoke check, not a routing-accuracy benchmark. Regression tests additionally
cover accumulated answers, metric date choices, request-literal validation, stale Contexts,
input limits and Playground failure/retry behavior. Real OpenAI continuation calls were not run.

The follow-up Metric date-choice review also used real `gemini-3.5-flash-lite` calls with a
synthetic order-count metric, `paid_at` as its default, and descriptions for `paid_at` and
`ordered_at`. In both metric-only and hybrid modes, `주문 생성일 기준` and
`orders.paid_at은 사용하지 마세요` selected `orders.ordered_at` (four successful requests).
The compiler now validates structured date intent instead of extracting names from answer text.
These observations do not guarantee semantic accuracy for arbitrary phrasing or other models.

## Small-model prompt and server-period check — 2026-11-19

`gemini-3.5-flash-lite` asked needless clarification questions where descriptions or the data
already settled the answer, and miscomputed calendar ranges. Five repeats per case, before
(`generate_v10`/`hybrid_v5`) and after (`generate_v11`/`hybrid_v6`, server-computed periods):

| Case | Before | After |
|---|---:|---:|
| library `fees_last_14_days` | 1/5 | 5/5 |
| hybrid `near_miss_regional_fees_with_minimum` | 4/5 | 5/5 |
| hybrid `near_miss_average_settled_fee` | 0/5 | 5/5 |
| commerce `order_count_last_week_uses_ordered_at` | 3/5 | 5/5 |
| hybrid `sql_route_rental_count_by_zone` | 4/5 | 3/5 |

The last-week case had chosen the right date but the wrong week; the server now computes
relative periods. A full run with the final prompts passed commerce 18/18, library 9/9,
metric-library 10/10 and hybrid-library 8/9 (`sql_route_rental_count_by_zone`, which also
fluctuates with the old prompts). Every case expecting `ambiguous` or `unanswerable` passed.

Prompt wording moved lite results sharply, so each change was measured:

- An example presenting the SQL route as "metric plus extra conditions" made
  `sql_route_rental_count_by_zone` fail 10/10; it was dropped.
- An instruction to ask about undescribed dates, scoped to its resource, still made
  `shipment_carriers_with_amount` (no date involved) ask 5/5. The Context now lists those
  dates as a fact; when to ask stays in the system prompt. The join passed 5/5 and the
  date-ambiguity cases still asked 5/5.
- Lite wrote `date_field: "settled_at"` for `rentals.settled_at` even after repair. An
  unqualified name now resolves when exactly one of the metric's time fields has it.

`gemini-3.5-flash` passed the first three cases 13/13 with the old prompts.
