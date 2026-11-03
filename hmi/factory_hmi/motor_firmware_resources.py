"""Signed firmware-resource cache; no target observations, OTA or device writes."""
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from .motor_firmware_resource_contract import SignedMotorFirmwareResource
from .updates import _private_directory, _atomic_json, _sync_directory, _public_key, _run


def verify_resource(release, key):
    envelope = SignedMotorFirmwareResource.model_validate(release).model_dump()
    signature = base64.b64decode(envelope['signature'], validate=True)
    if len(signature) != 64:
        raise ValueError('电机下载资源签名长度无效')
    trusted = _public_key(key)
    with tempfile.TemporaryDirectory(prefix='motor-resource-verify-') as work:
        work = Path(work)
        (work/'manifest').write_bytes(json.dumps(envelope['manifest'],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())
        (work/'signature').write_bytes(signature)
        result = _run(['openssl','pkeyutl','-verify','-pubin','-inkey',str(trusted),'-rawin',
                       '-in',str(work/'manifest'),'-sigfile',str(work/'signature')])
        if result.returncode:
            raise ValueError('电机下载资源未通过可信签名校验')
    return envelope['manifest']


def _valid_resource(path, manifest):
    if path.is_symlink():
        raise ValueError('电机下载资源缓存不能是符号链接')
    with path.open('rb') as stream:
        if stream.read(8) == b'!<arch>\n':
            raise ValueError('DEB不能用作电机下载资源')
        stream.seek(0)
        if os.fstat(stream.fileno()).st_size != manifest['size_bytes'] or hashlib.file_digest(stream,'sha256').hexdigest() != manifest['sha256']:
            raise ValueError('电机下载资源缓存长度或SHA256不符')


def stage_resource(release, *, key, cache_root, open_stream, downloaded_by='', check_session=lambda:None):
    manifest = verify_resource(release,key)
    check_session()
    root = _private_directory(Path(cache_root))
    fd = os.open(root/'download.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('已有电机下载资源任务，请稍后重试') from exc
        releases = _private_directory(root/'releases')
        destination = releases/manifest['release_id']
        cached = False
        if destination.is_symlink():
            raise ValueError('资源缓存发布目录不能是符号链接')
        if destination.exists():
            metadata = destination/'release.json'
            if metadata.is_symlink() or json.loads(metadata.read_text()) != release:
                raise ValueError('同一资源发布编号的清单冲突，禁止覆盖')
            try:
                _valid_resource(destination/manifest['filename'],manifest)
                cached = True
            except (ValueError,OSError):
                os.rename(destination,releases/('.corrupt-'+manifest['release_id']+'-'+str(time.time_ns())))
        if not cached:
            for old in releases.glob('.partial-*'):
                if old.is_dir() and not old.is_symlink():
                    shutil.rmtree(old)
            temporary = Path(tempfile.mkdtemp(prefix='.partial-',dir=releases))
            try:
                path = temporary/manifest['filename']
                started,length = time.monotonic(),0
                with open_stream() as reader,path.open('xb') as writer:
                    while True:
                        check_session()
                        if time.monotonic()-started>300:
                            raise TimeoutError('电机资源下载超过五分钟，请重试')
                        chunk = reader.read(min(1024**2,manifest['size_bytes']-length+1))
                        if not chunk:
                            break
                        length += len(chunk)
                        if length>manifest['size_bytes']:
                            raise ValueError('电机资源超过签名长度')
                        writer.write(chunk)
                    writer.flush();os.fsync(writer.fileno())
                _valid_resource(path,manifest)
                check_session()
                _atomic_json(temporary/'release.json',release)
                os.rename(temporary,destination);_sync_directory(releases)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        check_session()
        receipt = dict(artifact_type='motor_firmware_resource',usage='download_only',flash_eligible=False,
                       status='downloaded',cache_hit=cached,manifest=manifest,resource_path=str(destination/manifest['filename']),
                       downloaded_at=datetime.now(timezone.utc).isoformat(),downloaded_by=downloaded_by,
                       installed=False,actual_version_verified=False)
        _atomic_json(destination/'download.json',receipt)
        return receipt
