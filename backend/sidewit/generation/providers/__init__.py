from functools import lru_cache

from pydantic import SecretStr

from sidewit.config import Settings
from sidewit.generation.providers.base import LLMProvider


def build_provider(settings: Settings) -> LLMProvider | None:
    """None when the provider, model or its API key is not configured."""
    return _build(
        settings.llm_provider,
        settings.llm_model,
        _reveal(settings.openai_api_key),
        _reveal(settings.gemini_api_key),
    )


def _reveal(secret: SecretStr | None) -> str | None:
    return secret.get_secret_value() if secret else None


@lru_cache
def _build(
    provider: str | None, model: str | None, openai_key: str | None, gemini_key: str | None
) -> LLMProvider | None:
    if not model:
        return None
    if provider == "openai" and openai_key:
        from sidewit.generation.providers.openai import OpenAIProvider

        return OpenAIProvider(model, openai_key)
    if provider == "gemini" and gemini_key:
        from sidewit.generation.providers.gemini import GeminiProvider

        return GeminiProvider(model, gemini_key)
    return None
