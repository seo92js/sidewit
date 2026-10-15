"""FastAPI dependency declarations for shared process state."""

from typing import Annotated

from fastapi import Depends

from sidewit.application.runtime import get_history as get_history
from sidewit.application.runtime import get_store as get_store
from sidewit.generation.history import GenerationHistory
from sidewit.store import Store


async def available_store(store: Annotated[Store, Depends(get_store)]) -> Store:
    store.require_available()
    return store


StoreDep = Annotated[Store, Depends(available_store)]
HistoryDep = Annotated[GenerationHistory, Depends(get_history)]
