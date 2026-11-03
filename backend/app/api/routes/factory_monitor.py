from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core.auth import RequestActor, require_roles
from app.core.db import DbSession
from app.repositories.factory_monitor import FactoryMonitorRepository
from app.schemas.common import DataEnvelope

router = APIRouter(prefix='/api/v1/factory', tags=['factory-console'])
FactoryReader = Annotated[RequestActor, Depends(require_roles('SYSTEM_ADMIN', 'TEST_EXECUTOR', 'FACTORY_REVIEWER'))]


@router.get('/monitor')
def monitor(actor: FactoryReader, session: DbSession, cursor: UUID | None = None,
            limit: Annotated[int, Query(ge=1, le=100)] = 50,
            work_before: Annotated[int | None, Query(ge=1)] = None,
            work_limit: Annotated[int, Query(ge=1, le=50)] = 20):
    return DataEnvelope(data=FactoryMonitorRepository(session).monitor(cursor, limit, work_before, work_limit))
