"""Human-only publication/download, independent from DEB, OTA and test archives."""
from __future__ import annotations
import os
import shutil
from typing import Annotated

import anyio
from fastapi import APIRouter, Header, Query, Request, Response
from starlette.concurrency import run_in_threadpool
from app.api.routes.hmi_updates import Human, Operator, Admin, PackageResponse
from app.core.config import get_settings
from app.core.errors import ApiError
from app.schemas.common import DataEnvelope
from app.schemas.motor_firmware_resources import MotorModel
from app.services.motor_firmware_resources import MotorFirmwareResourceStorage

router = APIRouter(prefix='/api/v1/hmi/motor-firmware-resources', tags=['motor-firmware-resources'])


@router.get('/releases')
def releases(_: Human, actor: Operator, response: Response, model: Annotated[MotorModel | None, Query()] = None):
    rows = MotorFirmwareResourceStorage(get_settings()).catalog(model)
    response.headers['Cache-Control'] = 'no-store'
    return DataEnvelope(data={'artifact_type':'motor_firmware_resource', 'flash_eligible':False,
                              'releases':[row.model_dump() for row in rows]})


@router.get('/releases/{release_id}/package')
def package(release_id: str, _: Human, actor: Operator):
    release, stream = MotorFirmwareResourceStorage(get_settings()).package(release_id)
    response = PackageResponse(release, stream)
    response.headers['content-type'] = 'application/octet-stream'
    response.headers['X-Artifact-Type'] = 'motor_firmware_resource'
    response.headers['X-Content-SHA256'] = release.manifest.sha256
    response.headers['X-Flash-Eligible'] = 'false'
    return response


@router.post('/releases', status_code=201)
async def publish(request: Request, response: Response, actor: Human, _: Admin,
                  x_motor_resource: Annotated[str, Header(alias='X-Motor-Resource')]):
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/octet-stream':
        raise ApiError(415, 'invalid_content_type', '电机资源发布须使用原始lsepbin请求体。')
    storage = await run_in_threadpool(MotorFirmwareResourceStorage, get_settings())
    release = await run_in_threadpool(storage.from_header, x_motor_resource)
    manifest = release.manifest
    if content_length := request.headers.get('content-length'):
        try:
            length = int(content_length)
        except ValueError as exc:
            raise ApiError(422, 'invalid_content_length', '电机资源Content-Length无效。') from exc
        if length > min(storage.settings.hmi_updates_max_package_bytes, manifest.size_bytes):
            raise ApiError(413, 'motor_resource_too_large', '电机资源超出声明或允许大小。')
        if length != manifest.size_bytes:
            raise ApiError(422, 'motor_resource_size_mismatch', '资源长度与签名清单不符。')
    stage = await run_in_threadpool(storage.stage)
    stream = None
    try:
        stream = await run_in_threadpool((stage / manifest.filename).open, 'xb')
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > min(manifest.size_bytes, storage.settings.hmi_updates_max_package_bytes):
                raise ApiError(413, 'motor_resource_too_large', '电机资源超出声明或允许大小。')
            if chunk:
                await run_in_threadpool(stream.write, chunk)
        await run_in_threadpool(stream.flush)
        await run_in_threadpool(os.fsync, stream.fileno())
        await run_in_threadpool(stream.close)
        if received != manifest.size_bytes:
            raise ApiError(422, 'motor_resource_size_mismatch', '电机资源上传不完整。')
        result = await run_in_threadpool(storage.publish, stage, release,
                                         actor_id=str(actor.public_id), request_id=actor.request_id)
        if not result['created']:
            response.status_code = 200
        response.headers['Cache-Control'] = 'no-store'
        return DataEnvelope(data=result)
    finally:
        with anyio.CancelScope(shield=True):
            if stream:
                await run_in_threadpool(stream.close)
            await run_in_threadpool(shutil.rmtree, stage, True)
