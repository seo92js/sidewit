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
