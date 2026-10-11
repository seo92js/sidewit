"""Exercise the documented API flow. No application database connection or SQL execution."""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class ExampleError(Exception):
    pass


def request(base_url, api_key, method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = Request(f"{base_url.rstrip('/')}/api/v1{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=150) as response:
            return json.load(response)
    except HTTPError as e:
        try:
            code = json.load(e).get("error", "http_error")
        except (ValueError, AttributeError):
            code = "http_error"
        raise ExampleError(f"{method} {path}: HTTP {e.code} {code}") from None
    except URLError:
        raise ExampleError(f"{method} {path}: API connection failed") from None


def run(args):
    parsed = urlsplit(args.api_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.query
        or parsed.fragment
    ):
        raise ExampleError("Use an absolute HTTP(S) API root URL without credentials or query.")
    directory = Path(__file__).resolve().parent
    context_file = "metric-context.json" if args.metric_contracts else "context.json"
    body = json.loads((directory / context_file).read_text())
    body["name"] = args.context_name
    api_key = os.environ.get("SIDEWIT_API_KEY", "")

    def call(method, path, value=None):
        return request(args.api_url, api_key, method, path, value)

    contexts = call("GET", "/contexts")["items"]
    existing = next((c for c in contexts if c["name"] == body["name"]), None)
    if existing is not None:
        # Refuse schema-name/rule conflicts before rebuilding the example descriptions.
        expected = {
            (r["name"], f["name"]) for r in body["definition"]["resources"] for f in r["fields"]
        }
        actual = {
            (r["name"], f["name"]) for r in existing["definition"]["resources"] for f in r["fields"]
        }
        if actual != expected or existing["definition"]["rules"] != body["definition"]["rules"]:
            raise ExampleError(
                "Existing context differs from this example. Choose a fresh --context-name."
            )
        context = existing
    else:
        # Created empty; allow_generated_sql is set together with the metrics (below), since
        # disallowing generated SQL requires registered metrics.
        created = {
            k: v for k, v in body.items() if k not in {"definition", "allow_generated_sql"}
        }
        context = call("POST", "/contexts", created)
    context_id = context["id"]
    preview = call(
        "POST",
        f"/contexts/{context_id}/import-ddl",
        {
            "ddl": (directory / "schema.sql").read_text(),
            "mode": "merge",
            "expected_version": context["version"],
        },
    )
    if preview["warnings"]:
        raise ExampleError("Example DDL produced warnings; inspect /import-ddl before saving.")
    definition = preview["definition"]
    descriptions = {r["name"]: r for r in body["definition"]["resources"]}
    for resource in definition["resources"]:
        source = descriptions[resource["name"]]
        resource["description"] = source.get("description")
        fields = {f["name"]: f for f in source["fields"]}
        for field in resource["fields"]:
            field["description"] = fields[field["name"]].get("description")
            field["allowed_values"] = fields[field["name"]].get("allowed_values", [])
    definition["rules"] = body["definition"]["rules"]
    definition["metrics"] = body["definition"].get("metrics", [])
    context = call(
        "PUT",
        f"/contexts/{context_id}",
        {
            "name": body["name"],
            "dialect": body["dialect"],
            "timezone": body["timezone"],
            "allow_generated_sql": body.get("allow_generated_sql", True),
            "definition": definition,
            "expected_version": preview["context_version"],
        },
    )["context"]
    result = {
        "context_id": context_id,
        "context_version": context["version"],
        "ddl_warnings": preview["warnings"],
    }
    if args.context_only:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    question = json.loads((directory / "question.json").read_text())
    generated = call("POST", "/generate", question | {"context_id": context_id})
    result["generation_id"] = generated["generation_id"]
    result["status"] = generated["status"]
    if generated["status"] != "ok":
        result["reason"] = generated["reason"]
        result["clarifications"] = generated.get("clarifications", [])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    if not generated["validation"]["valid"]:
        raise ExampleError("Generate did not return a validated operation.")
    draft = call("POST", "/tools/draft", {"generation_id": generated["generation_id"]})
    arguments = draft.pop("example_arguments")
    draft["name"] = args.tool_name
    draft["description"] = draft["description"] or question["question"]
    # All model-produced material is revalidated by the save endpoint.
    call("POST", "/tools", draft)
    invoked = call("POST", f"/tools/{args.tool_name}/invoke", {"arguments": arguments})
    result["tool"] = invoked["tool"]
    result["operation"] = invoked["operation"]
    result["validation"] = generated["validation"]
    result["note"] = "Resolved SQL operation; not executed by Sidewit."
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://api:8000")
    parser.add_argument(
        "--context-only", action="store_true", help="register the context without an LLM call"
    )
    parser.add_argument("--context-name", default="ecommerce")
    parser.add_argument("--metric-contracts", action="store_true",
                        help="use the server-compiled regional revenue contract")
    parser.add_argument("--tool-name", default="ecommerce_revenue")
    args = parser.parse_args()
    try:
        return run(args)
    except (ExampleError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
