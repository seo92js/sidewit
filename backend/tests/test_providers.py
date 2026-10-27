"""Provider adapters, with the SDK clients replaced by stubs (no network)."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from google.genai import errors as genai_errors

from sidewit.config import Settings
from sidewit.generation.dto import LLMGenerationOutput
from sidewit.generation.providers import build_provider
from sidewit.generation.providers.base import LLMError, Message
from sidewit.generation.providers.fake import FakeProvider
from sidewit.generation.providers.gemini import GeminiProvider, inline_refs
from sidewit.generation.providers.openai import OpenAIProvider

OUTPUT = LLMGenerationOutput(
    status="unanswerable",
    reason="missing data",
    clarifications=[],
    statement=None,
    parameters=[],
    tool_suggestion=None,
)
MESSAGES = [Message("user", "q"), Message("assistant", "a"), Message("user", "fix")]


class Recorder:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.kwargs = result, error, None

    async def __call__(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.result


def openai_provider(recorder):
    provider = OpenAIProvider("gpt-test", "key")
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(parse=recorder))
    )
    return provider


def openai_completion(parsed=OUTPUT, refusal=None):
    message = SimpleNamespace(parsed=parsed, refusal=refusal)
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


async def test_openai_sends_system_first_and_returns_parsed_dto():
    recorder = Recorder(openai_completion())
    result = await openai_provider(recorder).generate_structured(
        "system", MESSAGES, LLMGenerationOutput
    )
    assert result.parsed == OUTPUT
    assert (result.input_tokens, result.output_tokens) == (11, 7)
    assert recorder.kwargs["model"] == "gpt-test"
    assert recorder.kwargs["response_format"] is LLMGenerationOutput
    assert [m["role"] for m in recorder.kwargs["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]


@pytest.mark.parametrize(
    "recorder",
    [
        Recorder(openai_completion(refusal="no")),
        Recorder(openai_completion(parsed=None)),
        Recorder(SimpleNamespace(choices=[], usage=None)),
        Recorder(error=openai.APIConnectionError(request=None)),
    ],
)
async def test_openai_failures_become_llm_error(recorder):
    with pytest.raises(LLMError):
        await openai_provider(recorder).generate_structured("s", MESSAGES, LLMGenerationOutput)


def gemini_provider(recorder, model="gemini-test"):
    provider = GeminiProvider(model, "key")
    provider.client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=recorder))
    )
    return provider


def gemini_response(text):
    usage = SimpleNamespace(prompt_token_count=5, candidates_token_count=3)
    return SimpleNamespace(text=text, usage_metadata=usage)


@pytest.mark.parametrize(
    "model",
    [
        "gemini-test",
        "gemini-2.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-3.8-flash",
        "models/gemini-3.1-flash-lite",
    ],
)
async def test_gemini_maps_roles_and_parses_json(model):
    recorder = Recorder(gemini_response(OUTPUT.model_dump_json()))
    result = await gemini_provider(recorder, model).generate_structured(
        "system", MESSAGES, LLMGenerationOutput
    )
    assert result.parsed == OUTPUT
    assert (result.input_tokens, result.output_tokens) == (5, 3)
    assert [c.role for c in recorder.kwargs["contents"]] == ["user", "model", "user"]
    config = recorder.kwargs["config"]
    assert config.system_instruction == "system"
    assert config.response_mime_type == "application/json"
    assert "$ref" not in json.dumps(config.response_json_schema)
    if model.removeprefix("models/") in ("gemini-3.1-flash-lite", "gemini-3.8-flash"):
        assert config.thinking_config.thinking_level == "LOW"
    else:
        assert config.thinking_config is None


@pytest.mark.parametrize(
    "recorder",
    [
        Recorder(gemini_response("")),
        Recorder(gemini_response('{"status": "maybe"}')),
        Recorder(error=genai_errors.APIError(500, {"error": {"message": "down"}})),
    ],
)
async def test_gemini_failures_become_llm_error(recorder):
    with pytest.raises(LLMError):
        await gemini_provider(recorder).generate_structured("s", MESSAGES, LLMGenerationOutput)


@pytest.mark.parametrize(
    "error_type",
    [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError],
)
async def test_gemini_transport_errors_are_redacted_llm_errors(error_type):
    key = "fixture-private-key"
    provider = gemini_provider(Recorder(error=error_type(f"connection failed: {key}")))
    provider._api_key = key
    with pytest.raises(LLMError, match="Gemini request failed") as caught:
        await provider.generate_structured("s", MESSAGES, LLMGenerationOutput)
    assert key not in str(caught.value)


@pytest.mark.parametrize("error_type", [asyncio.CancelledError, ValueError])
async def test_gemini_does_not_mask_cancellation_or_programming_errors(error_type):
    provider = gemini_provider(Recorder(error=error_type()))
    with pytest.raises(error_type):
        await provider.generate_structured("s", MESSAGES, LLMGenerationOutput)


def test_inline_refs_resolves_nested_definitions():
    schema = inline_refs(LLMGenerationOutput.model_json_schema())
    assert "$defs" not in schema
    params = schema["properties"]["parameters"]["items"]
    assert params["properties"]["type"]["enum"] == [
        "string",
        "integer",
        "number",
        "boolean",
        "date",
        "date-time",
    ]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, None),
        ({"llm_provider": "openai", "llm_model": "m"}, None),
        ({"llm_provider": "openai", "openai_api_key": "k"}, None),
        ({"llm_provider": "openai", "llm_model": "m", "openai_api_key": "k"}, OpenAIProvider),
        ({"llm_provider": "gemini", "llm_model": "m", "openai_api_key": "k"}, None),
        ({"llm_provider": "gemini", "llm_model": "m", "gemini_api_key": "k"}, GeminiProvider),
    ],
)
def test_build_provider(env, expected):
    provider = build_provider(Settings(**env))
    if expected is None:
        assert provider is None
    else:
        assert isinstance(provider, expected)


def test_empty_env_values_count_as_unset(monkeypatch):
    monkeypatch.setenv("SIDEWIT_LLM_PROVIDER", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    settings = Settings()
    assert settings.llm_provider is None
    assert settings.openai_api_key is None


def test_gemini_disables_sdk_retries(monkeypatch):
    options = {}

    def client(**kwargs):
        options.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("sidewit.generation.providers.gemini.genai.Client", client)
    GeminiProvider("gemini-test", "fixture-key")
    assert options["http_options"].retry_options.attempts == 1
    assert options["http_options"].timeout == 60000


async def test_fake_provider_scripts_responses_and_errors():
    fake = FakeProvider([OUTPUT, RuntimeError("down")])
    result = await fake.generate_structured("s", MESSAGES, LLMGenerationOutput)
    assert result.parsed == OUTPUT
    with pytest.raises(LLMError):
        await fake.generate_structured("s", MESSAGES, LLMGenerationOutput)
    assert len(fake.calls) == 2
