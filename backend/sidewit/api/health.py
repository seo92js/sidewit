from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from sidewit.api.state import get_store
from sidewit.store import Store

router = APIRouter()


@router.get("/health")
async def health(response: Response, store: Annotated[Store, Depends(get_store)]) -> dict:
    if not store.healthy():
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "storage": "unavailable"}
    return {"status": "ok", "storage": "ok"}
