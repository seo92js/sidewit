# Evaluation suites

Golden suites for Generate regressions. Each directory holds a `context.json` (the Context to
create or update) and `cases.yaml` (questions, expected status and deterministic AST
expectations). Scoring never compares SQL strings and never asks an LLM to judge.

| Suite | Role | Generate path |
|---|---|---|
| [`commerce`](commerce/cases.yaml) | **Development set.** The prompt (`generate_v7`) was iterated against its failures, and the prompt's illustrations cover the same kinds of decisions. A high score here shows the prompt works on what it was tuned for, not that it generalizes. | model-written SQL |
| [`library`](library/cases.yaml) | **Separate-schema regression set.** Different schema, values and periods, including joins, business-rule constants, date selection and unavailable data. | model-written SQL |
| [`metric-library`](metric-library/cases.yaml) | **Separate-schema regression set**, metrics only: the library questions answered through registered metric contracts. | metrics only (`allow_generated_sql: false`) |
| [`hybrid-library`](hybrid-library/cases.yaml) | **Separate-schema regression set** for hybrid routing: questions a metric answers (`source: metric`), questions none does (`source: generated_sql`), and near misses — a metric plus an extra condition — where the generated SQL must still apply the metric's fixed filters. | hybrid (`allow_generated_sql: true`) |

Report all suite results and their exposure history when describing quality. These suites
were initially held out from prompt tuning, but cases have since appeared in reviews and
regressions. They are not a fresh independent benchmark. Every `ok` case may carry an authored
`reference` answer; backend tests check that each reference satisfies its own expectations in
every suite, which proves the cases are satisfiable. References are never used for scoring.

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

Exit code: 0 all passed, 1 some case failed, 2 setup error. No SQL is executed. Run results
are not committed to the repository.

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
