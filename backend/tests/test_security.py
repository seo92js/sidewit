import openai
import pytest

from sidewit.config import Settings
from sidewit.generation.dto import LLMGenerationOutput
from sidewit.generation.providers.base import LLMError, Message
from sidewit.security import REDACTED, redact
from tests.test_providers import Recorder, gemini_provider, openai_provider


@pytest.mark.parametrize(
    ("text", "secret_free"),
    [
        ("Incorrect API key provided: sk-invalid.", "Incorrect API key provided: [REDACTED]."),
        ("key: sk-proj-abc1****wxyz rejected", "key: [REDACTED] rejected"),
        ("API key AIzaSyA1234567890abcdefgh not valid", "API key [REDACTED] not valid"),
        ("header Authorization: Bearer abc.def-123", "header Authorization: Bearer [REDACTED]"),
        ("GET https://x/v1?key=AbC123&alt=json", "GET https://x/v1?key=[REDACTED]&alt=json"),
        ("plain message without secrets", "plain message without secrets"),
    ],
)
def test_redact_patterns(text, secret_free):
    assert redact(text) == secret_free


def test_redact_known_secret_values():
    assert redact("token my-custom-secret leaked", ["my-custom-secret", None, ""]) == (
        f"token {REDACTED} leaked"
    )


def test_settings_never_render_secrets():
    settings = Settings(
        api_key="sidewit-secret", openai_api_key="sk-live-123456", gemini_api_key="AIza-xyz"
    )
    for rendered in (repr(settings), str(settings), settings.model_dump_json()):
        for secret in ("sidewit-secret", "sk-live-123456", "AIza-xyz"):
            assert secret not in rendered


class _KeyEchoError(openai.APIConnectionError):
    def __str__(self) -> str:
        return "Connection failed for key secret-value-1234 / sk-secret-value-1234"


async def test_openai_error_message_is_redacted_and_unchained():
    provider = openai_provider(Recorder(error=_KeyEchoError(request=None)))
    provider._api_key = "secret-value-1234"
    with pytest.raises(LLMError) as raised:
        await provider.generate_structured("s", [Message("user", "q")], LLMGenerationOutput)
    assert "secret-value-1234" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


async def test_gemini_error_message_is_redacted():
    from google.genai import errors

    provider = gemini_provider(
        Recorder(error=errors.APIError(400, {"error": {"message": "bad key gemini-secret-99"}}))
    )
    provider._api_key = "gemini-secret-99"
    with pytest.raises(LLMError) as raised:
        await provider.generate_structured("s", [Message("user", "q")], LLMGenerationOutput)
    assert "gemini-secret-99" not in str(raised.value)
