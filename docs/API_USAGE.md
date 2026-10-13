# Using the Generate API

Send natural-language requests to `POST /api/v1/generate`. Sidewit returns operations for
review, clarification questions, or a reason it cannot answer. It never executes SQL.
Use `/docs` on your API server for the interactive schema.

## Minimal Python client

This standard-library example needs a saved Context ID. Set `SIDEWIT_CONTEXT_ID` and, when
server authentication is enabled, `SIDEWIT_API_KEY`. The default API address is local.

```python
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def generate(payload):
    headers = {"Content-Type": "application/json"}
    if key := os.environ.get("SIDEWIT_API_KEY"):
        headers["Authorization"] = f"Bearer {key}"
    request = Request(
        os.environ.get("SIDEWIT_API_URL", "http://127.0.0.1:8000").rstrip("/")
        + "/api/v1/generate",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    # Longer than the server's 90-second generation budget.
    with urlopen(request, timeout=110) as response:
        return json.load(response)


payload = {
    "context_id": os.environ["SIDEWIT_CONTEXT_ID"],
    "question": "서울 매장 개수",
}
try:
    result = generate(payload)
except HTTPError as error:
    # Sidewit errors use {error, message, details}; HTTP failures are not generation statuses.
    print(error.code, error.read().decode())
    raise

if result["status"] == "ok":
    # One or many results use the same shape.
    for step in result["operations"]:
        print(json.dumps(step["operation"], ensure_ascii=False, indent=2))
elif result["status"] == "ambiguous":
    print(result["reason"])
    for question in result["clarification_questions"]:
        print(question["id"], question["question"], question["options"])
else:  # unanswerable
    print(result["reason"])
```

`ok` means structural validation passed. Review every operation and its parameters before
execution. Plan steps are independent; Sidewit does not combine their results. Bind named
parameters through your DB driver instead of interpolating them into SQL.

## Continue after user input

For an ambiguous result, display every returned question and collect a nonempty answer.
Use the server-provided request body so the original options, reference time, Context version
and previous answers stay together:

```python
# Supply one {id, question, answer} item per question the user has answered.
# Keep the exact returned question text and ID. Do not send unanswered empty strings.
new_answers = [
    {
        "id": question["id"],
        "question": question["question"],
        "answer": input(question["question"] + " ").strip(),
    }
    for question in result["clarification_questions"]
]
next_request = result["continuation_request"]
answers_by_id = {answer["id"]: answer for answer in next_request["clarification_answers"]}
answers_by_id.update({answer["id"]: answer for answer in new_answers})
next_request = {**next_request, "clarification_answers": list(answers_by_id.values())}
result = generate(next_request)
# Handle result.status again using the same three branches above.
```

Run this continuation block only for `ambiguous`, after collecting user input. A later round
uses the latest response's `continuation_request`. Replacing an existing answer by ID edits it;
appending the same ID twice is rejected. Keep the pending request if submission fails, so users
can correct it without retyping. See [clarification limits and version rules](CLARIFICATIONS.md).

## Handle errors separately

| Result | Application action |
| --- | --- |
| `200 ambiguous` | Ask the user; do not automatically retry. |
| `200 unanswerable` | Show the reason and let the user change the request or Context. |
| `401 unauthorized` | Correct the Sidewit API key, not the provider key. |
| `409 context_changed` | Preserve user text, reload the Context and start a new request. Do not silently replace the pinned version. |
| Other `409` conflicts | Read `error` and `details`; refresh the affected Tool/Context before starting a new request. |
| `422` | Show `message` and relevant `details`; correct inputs or review validation errors. Rejected SQL is not an operation. |
| `502`, `503`, `504` or network failure | Keep inputs; offer a deliberate retry after checking configuration or availability. A repeated request can incur another LLM call. |

Keep the management API key on your application server. This key also grants Context and Tool
management access; do not embed it in an end-user browser. Questions and continuation bodies
contain user text and should follow your application's data handling policy.
