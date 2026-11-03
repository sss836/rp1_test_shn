from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import (
    access,
    admin,
    archives,
    assistant,
    assistant_chat,
    assistant_local_diagnostics,
    assistant_permissions,
    archive_imports,
    auth,
    dashboard,
    health,
    hmi_presence,
    factory_monitor,
    hmi_updates,
    motor_firmware,
    motor_firmware_resources,
    motor_parameters,
    mtbf,
    read_models,
    setup,
    submissions,
)
from app.core.auth import RequestActorDep
from app.core.config import get_settings
from app.core.db import SessionLocal, DbSession
from app.core.errors import ApiError
from app.repositories.security import SecurityRepository
from app.services.auth import AuthService


settings = get_settings()
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("rp1.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.seed_admin_username and settings.seed_admin_password is not None:
        with SessionLocal() as session:
            bootstrapped = AuthService(
                settings, SecurityRepository(session)
            ).bootstrap_initial_admin()
        if bootstrapped:
            logger.info("initial administrator credential bootstrapped")
    from app.services.assistant_chat import runtime
    if settings.assistant_enabled and settings.assistant_runtime_enabled:
        import os
        from app.services.assistant import require_isolation,service_secret
        require_isolation(settings)
        service_secret(settings)
        if os.getenv('WEB_CONCURRENCY','1')!='1':raise RuntimeError('Assistant requires a single API worker')
        await runtime.start()
    try:yield
    finally:
        if runtime.workers:await runtime.stop()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "RP1整机与模块可靠性平台API。Campaign/scope实时MTBF由"
        "mtbf.poisson.exposure_estimate.v1确定性规则计算。"
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "If-Match",
        "X-Request-Id",
        "X-Change-Reason",
        "X-CSRF-Token",
        "X-HMI-Release",
        "X-Filename",
        "X-Filename-UTF8",
        *(["X-User-Public-Id"] if settings.auth_mode == "development" else []),
    ],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        {
            "field": ".".join(str(item) for item in error["loc"] if item != "body"),
            "message": error["msg"],
            "code": error["type"],
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "请求参数不符合接口契约。",
                "details": details,
            }
        },
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "unexpected_error",
        extra={"method": request.method, "path": request.url.path},
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "服务发生未预期错误，请使用响应中的请求ID查询日志。",
                "details": [],
            }
        },
    )


app.include_router(health.router)
app.include_router(assistant.router)
app.include_router(assistant_chat.router)
app.include_router(assistant_local_diagnostics.router)
app.include_router(assistant_permissions.router)
from app.api.routes import assistant_support
app.include_router(assistant_support.router)
app.include_router(hmi_presence.router)
app.include_router(factory_monitor.router)
app.include_router(hmi_updates.router)
app.include_router(motor_firmware.router)
app.include_router(motor_firmware_resources.router)
app.include_router(motor_parameters.router)
app.include_router(auth.router)
app.include_router(access.router)
app.include_router(admin.router)
app.include_router(dashboard.router)
app.include_router(read_models.router)
app.include_router(mtbf.router)
app.include_router(setup.router)
app.include_router(submissions.router)
app.include_router(archives.router)
app.include_router(archives.factory_router)
app.include_router(archive_imports.router)

@app.get("/api/v1/archive-capabilities")
def archive_capabilities(actor: RequestActorDep):
    from app.schemas.common import DataEnvelope
    return DataEnvelope(data={"run_segments":True,"schema_version":"run-segments-v1","max_segments":4096})

from app.api.routes import showcase
app.include_router(showcase.router)

@app.get("/api/v1/archive-ledger.csv")
def archive_ledger(actor: RequestActorDep, session: DbSession, request: Request):
    import csv,io,json
    from app.repositories.archives import ArchiveRepository
    from app.schemas.archives import ArchiveLedgerFilters
    from fastapi.responses import StreamingResponse
    filters=ArchiveLedgerFilters.model_validate(dict(request.query_params))
    def chunks():
        yield b"\xef\xbb\xbf"
        buffer=io.StringIO(newline="");writer=csv.writer(buffer)
        writer.writerow(['执行编号','测试','SN或对象','台架','审批状态','开始时间','分区'])
        before=None
        while True:
            page=ArchiveRepository(session).list(**{("query" if k=="q" else k):v for k,v in filters.model_dump(exclude_none=True).items()},before=before,limit=100)
            for item in page['items']:
                cells=[item.get('execution_code') or item['archive_code'],item['test_name'],item.get('serial_number') or item['actual_object'],item.get('bench_id') or '',item.get('submission_status') or '未送审',(item.get('facts') or {}).get('started_at') or '',item.get('data_domain') or 'UNCLASSIFIED']
                writer.writerow(["'"+str(v) if str(v).lstrip().startswith(('=','+','-','@')) else str(v) for v in cells])
            yield buffer.getvalue().encode('utf-8');buffer.seek(0);buffer.truncate(0)
            before=page['next_cursor']
            if before is None:break
    return StreamingResponse(chunks(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="archive-ledger.csv"','Cache-Control':'no-store'})
