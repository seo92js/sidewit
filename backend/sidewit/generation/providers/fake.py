from sidewit.generation.providers.base import LLMError, Message, StructuredResult, T


class FakeProvider:
    """Returns scripted responses in order; records every call. For tests and local runs
    without an LLM. An Exception in `responses` is raised as LLMError."""

    name = "fake"

    def __init__(self, responses: list, model: str = "fake-model") -> None:
        self.model = model
        self.responses = list(responses)
        self.calls: list[tuple[str, list[Message]]] = []

    async def generate_structured(
        self, system: str, messages: list[Message], schema: type[T]
    ) -> StructuredResult[T]:
        self.calls.append((system, list(messages)))
        if not self.responses:
            raise LLMError("FakeProvider has no scripted response left.")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise LLMError(str(response)) from response
        return StructuredResult(parsed=schema.model_validate(response.model_dump()))
