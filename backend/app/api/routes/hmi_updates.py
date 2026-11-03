"""Session-authenticated software distribution; never an ingestion endpoint."""
from __future__ import annotations

import os
import shutil
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.core.auth import RequestActor, RequestActorDep, require_roles
from app.core.config import get_settings
from app.core.errors import ApiError
from app.schemas.common import DataEnvelope
from app.schemas.hmi_updates import LatestRelease
from app.services.hmi_updates import HmiUpdateStorage


router = APIRouter(prefix="/api/v1/hmi/updates", tags=["hmi-software-updates"])


def require_human_session(actor: RequestActorDep) -> RequestActor:
    if actor.principal_kind != "HUMAN" or actor.session_public_id is None:
        raise ApiError(403, "hmi_update_session_required", "软件更新需要已登录 HUMAN 账号的有效会话。")
    return actor


Human = Annotated[RequestActor, Depends(require_human_session)]
Operator = Annotated[RequestActor, Depends(require_roles("SYSTEM_ADMIN", "TEST_EXECUTOR"))]
Admin = Annotated[RequestActor, Depends(require_roles("SYSTEM_ADMIN"))]
Target = Annotated[str, Query(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")]


@router.get("/latest")
def latest(actor: Human, response: Response, os_id: Target, os_version: Target, architecture: Target,
           current_version: Annotated[str, Query(min_length=1, max_length=200)]) -> DataEnvelope[LatestRelease]:
    storage = HmiUpdateStorage(get_settings())
    release = storage.latest(os_id, os_version, architecture, current_version)
    response.headers["Cache-Control"] = "no-store"
    return DataEnvelope(data=LatestRelease(available=release is not None,
        can_update=actor.role in {"TEST_EXECUTOR", "SYSTEM_ADMIN"},
        current_version=current_version, release=release))


class PackageResponse(StreamingResponse):
    def __init__(self, release, stream):
        self.stream = stream
        manifest = release.manifest
        super().__init__(self.chunks(), media_type="application/vnd.debian.binary-package", headers={
            "Content-Length": str(manifest.size_bytes), "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{manifest.filename}"',
            "X-Content-Type-Options": "nosniff"})

    def chunks(self):
        try:
            while chunk := self.stream.read(1024 * 1024):
                yield chunk
        finally:
            self.stream.close()

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(self.stream.close)


@router.get("/releases/{release_id}/package")
def package(release_id: str, _: Human, actor: Operator):
    release, stream = HmiUpdateStorage(get_settings()).package(release_id)
    return PackageResponse(release, stream)


@router.post("/releases", status_code=201)
async def publish(request: Request, response: Response, actor: Human, _: Admin,
                  x_hmi_release: Annotated[str, Header(alias="X-HMI-Release")]):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() not in {
            "application/octet-stream", "application/vnd.debian.binary-package"}:
        raise ApiError(415, "invalid_content_type", "软件发布须使用原始 DEB 请求体。")
    storage = await run_in_threadpool(HmiUpdateStorage, get_settings())
    release = await run_in_threadpool(storage.from_header, x_hmi_release)
    manifest = release.manifest
    if content_length := request.headers.get("content-length"):
        try:
            length = int(content_length)
        except ValueError as exc:
            raise ApiError(422, "invalid_content_length", "软件包 Content-Length 无效。") from exc
        if length > storage.settings.hmi_updates_max_package_bytes or length > manifest.size_bytes:
            raise ApiError(413, "hmi_package_too_large", "软件包超出声明或允许大小。")
        if length != manifest.size_bytes:
            raise ApiError(422, "hmi_package_size_mismatch", "软件包声明大小与签名清单不符。")
    stage = await run_in_threadpool(storage.stage)
    stream = None
    try:
        stream = await run_in_threadpool((stage / manifest.filename).open, "xb")
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > min(manifest.size_bytes, storage.settings.hmi_updates_max_package_bytes):
                raise ApiError(413, "hmi_package_too_large", "软件包超出声明或允许大小。")
            if chunk:
                await run_in_threadpool(stream.write, chunk)
        await run_in_threadpool(stream.flush)
        await run_in_threadpool(os.fsync, stream.fileno())
        await run_in_threadpool(stream.close)
        if received != manifest.size_bytes:
            raise ApiError(422, "hmi_package_size_mismatch", "软件包上传不完整。")
        result = await run_in_threadpool(storage.publish, stage, release,
                                        actor_id=str(actor.public_id), request_id=actor.request_id)
        if not result["created"]:
            response.status_code = 200
        response.headers["Cache-Control"] = "no-store"
        return DataEnvelope(data=result)
    finally:
        with anyio.CancelScope(shield=True):
            if stream:
                await run_in_threadpool(stream.close)
            await run_in_threadpool(shutil.rmtree, stage, True)
