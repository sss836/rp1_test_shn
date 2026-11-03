"""Human-session motor distribution, separate from DEB and CAN operations."""
from __future__ import annotations
import os
import shutil
from typing import Annotated
import anyio
from fastapi import APIRouter, Header, Request, Response, Query
from starlette.concurrency import run_in_threadpool
from app.api.routes.hmi_updates import Human, Operator, Admin, PackageResponse
from app.core.config import get_settings
from app.core.errors import ApiError
from app.schemas.common import DataEnvelope
from app.services.motor_firmware import MotorFirmwareStorage, compatible

router = APIRouter(prefix="/api/v1/hmi/motor-firmware", tags=["motor-firmware"])
Target = Annotated[str, Query(min_length=1, max_length=64)]

@router.get("/releases")
def releases(_: Human, actor: Operator, response: Response,
             model: Target, hardware: Target, protocol: Target, current_version: Target):
    rows = MotorFirmwareStorage(get_settings()).applicable(model=model, hardware=hardware,
        protocol=protocol, current_version=current_version)
    response.headers["Cache-Control"] = "no-store"
    return DataEnvelope(data={"artifact_type": "motor_firmware", "releases": [r.model_dump() for r in rows]})

@router.get("/releases/{release_id}/package")
def package(release_id: str, _: Human, actor: Operator,
            model: Target, hardware: Target, protocol: Target, current_version: Target):
    release, stream = MotorFirmwareStorage(get_settings()).package(release_id)
    if not compatible(release.manifest, model=model, hardware=hardware, protocol=protocol, current_version=current_version):
        stream.close()
        raise ApiError(409, "motor_firmware_incompatible", "固件与当前电机版本条件不兼容。")
    response = PackageResponse(release, stream)
    response.headers["content-type"] = "application/octet-stream"
    response.headers["X-Artifact-Type"] = "motor_firmware"
    response.headers["X-Content-SHA256"] = release.manifest.sha256
    return response

@router.post("/releases", status_code=201)
async def publish(request: Request, response: Response, actor: Human, _: Admin,
                  x_motor_release: Annotated[str, Header(alias="X-Motor-Release")]):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() not in {
            "application/octet-stream"}:
        raise ApiError(415, "invalid_content_type", "电机固件发布须使用原始 lsepbin 请求体。")
    storage = await run_in_threadpool(MotorFirmwareStorage, get_settings())
    release = await run_in_threadpool(storage.from_header, x_motor_release)
    manifest = release.manifest
    if content_length := request.headers.get("content-length"):
        try:
            length = int(content_length)
        except ValueError as exc:
            raise ApiError(422, "invalid_content_length", "电机固件包 Content-Length 无效。") from exc
        if length > storage.settings.hmi_updates_max_package_bytes or length > manifest.size_bytes:
            raise ApiError(413, "hmi_package_too_large", "电机固件包超出声明或允许大小。")
        if length != manifest.size_bytes:
            raise ApiError(422, "hmi_package_size_mismatch", "电机固件包声明大小与签名清单不符。")
    stage = await run_in_threadpool(storage.stage)
    stream = None
    try:
        stream = await run_in_threadpool((stage / manifest.filename).open, "xb")
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > min(manifest.size_bytes, storage.settings.hmi_updates_max_package_bytes):
                raise ApiError(413, "hmi_package_too_large", "电机固件包超出声明或允许大小。")
            if chunk:
                await run_in_threadpool(stream.write, chunk)
        await run_in_threadpool(stream.flush)
        await run_in_threadpool(os.fsync, stream.fileno())
        await run_in_threadpool(stream.close)
        if received != manifest.size_bytes:
            raise ApiError(422, "hmi_package_size_mismatch", "电机固件包上传不完整。")
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
