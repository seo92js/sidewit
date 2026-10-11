import openai
from pydantic import ValidationError

from sidewit.generation.providers.base import LLMError, Message, StructuredResult, T
from sidewit.security import redact

TIMEOUT_SECONDS = 60


class OpenAIProvider:
    name = "openai"

    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self._api_key = api_key
        self.client = openai.AsyncOpenAI(api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=0)

    async def generate_structured(
        self, system: str, messages: list[Message], schema: type[T]
    ) -> StructuredResult[T]:
        try:
            completion = await self.client.chat.completions.parse(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    *({"role": m.role, "content": m.content} for m in messages),
                ],
                response_format=schema,
            )
        except (openai.OpenAIError, ValidationError) as e:
            raise LLMError(redact(f"OpenAI request failed: {e}", [self._api_key])) from None

        message = completion.choices[0].message
        if message.refusal:
            raise LLMError(f"OpenAI refused the request: {message.refusal}")
        if message.parsed is None:
            raise LLMError("OpenAI returned no structured output.")
        usage = completion.usage
        return StructuredResult(
            parsed=message.parsed,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
        )
