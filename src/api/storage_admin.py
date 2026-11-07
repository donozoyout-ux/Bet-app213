"""Admin-only, explicit storage maintenance endpoints."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from src.api.routes import require_scraper_token
from src.db import database
from src.storage.board_archive import BoardArchiveError
from src.storage.board_archive_pilot import archive_first_board_editions

router = APIRouter()


class BoardEditionArchiveRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    limit: Annotated[StrictInt, Field(gt=0, le=10)]


@router.post('/admin/storage/board-editions/archive',
             dependencies=[Depends(require_scraper_token)])
async def archive_board_editions(request: BoardEditionArchiveRequest):
    """Explicitly archive at most ten editions; never runs during application startup."""
    try:
        return await archive_first_board_editions(database, request.limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except BoardArchiveError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
