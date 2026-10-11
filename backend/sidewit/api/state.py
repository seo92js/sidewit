"""Process-wide state the API depends on: the file store and recent Generations."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends

from sidewit.config import get_settings
from sidewit.generation.history import GenerationHistory
from sidewit.store import Store


@lru_cache
def get_store() -> Store:
    return Store(Path(get_settings().data_dir))


@lru_cache
def get_history() -> GenerationHistory:
    return GenerationHistory(get_settings().generation_history)


async def available_store(store: Annotated[Store, Depends(get_store)]) -> Store:
    store.require_available()
    return store


StoreDep = Annotated[Store, Depends(available_store)]
HistoryDep = Annotated[GenerationHistory, Depends(get_history)]
