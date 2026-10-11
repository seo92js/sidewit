from typing import Any

import httpx
from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from sidewit.generation.providers.base import LLMError, Message, StructuredResult, T
from sidewit.security import redact

TIMEOUT_MS = 60_000


class GeminiProvider:
    name = "gemini"

    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self._api_key = api_key
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=TIMEOUT_MS, retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )

    async def generate_structured(
        self, system: str, messages: list[Message], schema: type[T]
    ) -> StructuredResult[T]:
        contents = [
            types.Content(
                role="user" if m.role == "user" else "model", parts=[types.Part(text=m.content)]
            )
            for m in messages
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=inline_refs(schema.model_json_schema()),
        )
        if self.model.removeprefix("models/") in (
            "gemini-3.1-flash-lite",
            "gemini-3.8-flash",
        ):
            # Low keeps a bounded reasoning step for SQL planning on these tested models.
            config.thinking_config = types.ThinkingConfig(thinking_level="low")
        try:
            response = await self.client.aio.models.generate_content(
                model=self.model, contents=contents, config=config
            )
        except (errors.APIError, httpx.TransportError) as e:
            raise LLMError(redact(f"Gemini request failed: {e}", [self._api_key])) from None

        text = response.text
        if not text:
            raise LLMError("Gemini returned no structured output.")
        try:
            parsed = schema.model_validate_json(text)
        except ValidationError as e:
            raise LLMError(f"Gemini output does not match the schema: {e}") from None
        usage = response.usage_metadata
        return StructuredResult(
            parsed=parsed,
            input_tokens=usage.prompt_token_count if usage else None,
            output_tokens=usage.candidates_token_count if usage else None,
        )


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replaces `$ref`s to `$defs` with the definitions themselves, so the schema does not
    depend on reference support in the provider's JSON Schema subset."""
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(defs[node["$ref"].rsplit("/", 1)[-1]])
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)
