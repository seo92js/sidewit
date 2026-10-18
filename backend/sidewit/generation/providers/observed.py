"""Count actual provider calls, including cancelled/failed calls, in the current request."""

from sidewit.generation.providers.base import LLMProvider, Message, StructuredResult, T
from sidewit.observability import annotate, current


class ObservedProvider:
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider
        self.name = provider.name
        self.model = provider.model

    async def generate_structured(
        self, system: str, messages: list[Message], schema: type[T]
    ) -> StructuredResult[T]:
        observation = current()
        if observation:
            observation.fields["llm_calls"] += 1
            annotate(token_usage_complete=False)
        result = await self.provider.generate_structured(system, messages, schema)
        if observation:
            for key in ("input_tokens", "output_tokens"):
                count = getattr(result, key)
                if count is not None:
                    observation.fields[key] = (observation.fields.get(key) or 0) + count
            if result.input_tokens is not None and result.output_tokens is not None:
                observation.usage_reports += 1
            # Compare reports with started calls, including concurrent/in-flight calls.
            # A later success cannot hide another call's failed or missing usage report.
            annotate(
                token_usage_complete=observation.usage_reports == observation.fields["llm_calls"]
            )
        return result
