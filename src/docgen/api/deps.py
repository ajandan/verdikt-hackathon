from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from docgen.store.db import AsyncSessionLocal


async def get_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


def get_tenant_id(request: Request) -> uuid.UUID:
    tid = getattr(request.state, "tenant_id", None)
    if tid is None:
        raise HTTPException(401, "unauthorized")
    return tid
