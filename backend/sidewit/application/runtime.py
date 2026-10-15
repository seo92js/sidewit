"""One store and generation history per process, shared by all transports."""

from functools import lru_cache
from pathlib import Path

from sidewit.config import get_settings
from sidewit.generation.history import GenerationHistory
from sidewit.generation.providers import build_provider
from sidewit.generation.providers.base import LLMProvider
from sidewit.store import Store


@lru_cache
def get_store() -> Store:
    return Store(Path(get_settings().data_dir))


@lru_cache
def get_history() -> GenerationHistory:
    return GenerationHistory(get_settings().generation_history)


def get_llm_provider() -> LLMProvider | None:
    return build_provider(get_settings())
