# Continuing an ambiguous request

Sidewit is an API worker, not a chat-session store. Your application displays clarification
questions, collects answers and resubmits the original request. The Playground implements the
same flow. Neither step executes SQL.

## 1. Submit a question

```json
{
  "context_id": "YOUR_CONTEXT_UUID",
  "question": "이번 달 리뷰왕"
}
```

POST this body to `/api/v1/generate`. An ambiguous response includes:

```json
{
  "generation_id": "GENERATION_UUID",
  "context_version": 2,
  "status": "ambiguous",
  "reason": "리뷰 집계에 사용할 날짜 기준이 필요합니다.",
  "clarification_questions": [
    {
      "id": "GENERATION_UUID:0",
      "question": "어떤 날짜 기준으로 집계할까요?",
      "options": ["리뷰 작성일", "리뷰 수정일"]
    }
  ],
  "reference_time": "2026-10-13T01:00:00Z"
}
```

Questions and choices depend on your Context. Render choices as buttons or a selector and always
offer a free-text answer. An empty `options` list means free text only. Keep the returned IDs;
do not infer meaning from their format. Use `clarification_questions` for both display and follow-up answers.

## 2. Resubmit with the answer

POST to the same endpoint:

```json
{
  "context_id": "YOUR_CONTEXT_UUID",
  "question": "이번 달 리뷰왕",
  "reference_time": "2026-10-13T01:00:00Z",
  "expected_context_version": 2,
  "clarification_answers": [
    {
      "id": "GENERATION_UUID:0",
      "question": "어떤 날짜 기준으로 집계할까요?",
      "answer": "리뷰 작성일"
    }
  ]
}
```

Preserve the original request's metric options, if any. An explicit request `date_field` takes
precedence over free-text answers. Without that override, the Metric planner interprets the
answer and returns a date-field choice for the server to validate; the compiler never treats a
field-name mention as a selection. An unresolved choice remains ambiguous. See
[metric period selection](METRIC_CONTRACTS.md#period-selection). Copy `reference_time` from the response
instead of computing a new time: “this month” must not change while the user answers. Use
`context_version` as `expected_context_version`.

Sidewit evaluates saved Tools and generation again with the added information and runs normal
validation. A new condition may make a previously suitable Tool unsuitable. The response can
be `ok`, `ambiguous` again, or `unanswerable`; no outcome guarantees execution or business accuracy.

For another clarification round, append new `{id, question, answer}` items and resend all prior
answers. To correct an existing answer, replace the item with that ID rather than duplicating it.
A new independent question should omit the answers and start with a new reference time.

## Client handling

- `ok`: review the returned Operation; your application decides whether and how to execute it.
- `ambiguous`: collect the next answers. Do not automatically retry without new user input.
- `unanswerable`: explain the missing data or definition; changing an answer may not resolve it.
- `409 context_changed`: retain the user's text, fetch the current Context and start a new request.
  Do not silently replace the expected version on an existing continuation.
- Other request errors: preserve typed answers for correction or retry. Avoid concurrent retries.

No session ID, resume endpoint or persistent conversation store is required. A server restart or
generation-history expiry does not prevent continuation. The submitted history is caller-owned,
untrusted input, not proof that Sidewit previously asked a question. It does not grant execution
permissions or bypass validation. The current Tool catalog is evaluated on every call; Tool
versions are not pinned across clarification rounds.

Up to 24 answers are accepted. Each question allows 1,000 characters, each answer 2,000, and the
original question plus all question/answer text allows 16,000 in total. IDs allow 100 ASCII
letters, digits, underscores, colons or hyphens. Nonempty answers require a timezone-aware
`reference_time` and a positive `expected_context_version`; invalid input returns 422 before
provider usage. Questions and answers reach the configured LLM provider and temporary in-memory
generation history; they are not written to application logs or the Context/Tool data directory.


The Playground lets users edit previous answers while answering the next round. Replacements
keep their question IDs. It blocks continuations over 24 answers or 16,000 total characters
(original question plus clarification questions and answers) before sending them.


The default automatic Tool selection also applies to follow-ups; no mode is required.
Keep the original question, reference time, Context version and answers. The Playground
preserves request inputs.
