"""Immutable administrator-confirmed files; never supplies OTA compatibility."""
import hashlib
import json
from pathlib import Path

from app.core.errors import ApiError
from app.schemas.motor_firmware_resources import SignedMotorFirmwareResource
from app.services.hmi_updates import HmiUpdateStorage, _json_object


def parse_resource(raw):
    try:
        return SignedMotorFirmwareResource.model_validate(json.loads(raw, object_pairs_hook=_json_object))
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ApiError(422, 'invalid_motor_resource', '电机下载资源清单无效，不能使用DEB或OTA清单。') from exc


class MotorFirmwareResourceStorage(HmiUpdateStorage):
    parse_release = staticmethod(parse_resource)

    def __init__(self, settings):
        resource_root = Path(settings.motor_firmware_resources_root).expanduser().resolve()
        for value in (settings.hmi_updates_root, settings.motor_firmware_root):
            if not value:
                continue
            other = Path(value).expanduser().resolve()
            if resource_root == other or resource_root in other.parents or other in resource_root.parents:
                raise ApiError(503, 'motor_resources_unavailable', '电机资源、OTA和DEB持久目录不能重叠。')
        super().__init__(settings.model_copy(update={
            'hmi_updates_enabled': settings.motor_firmware_resources_enabled,
            'hmi_updates_root': settings.motor_firmware_resources_root,
            'hmi_updates_public_key_file': settings.motor_firmware_resources_public_key_file,
        }))

    def validate_release_version(self, release):
        # Labels are not sortable device software versions. Schema validation
        # leaves the unavailable hardware/protocol/software mappings explicit.
        pass

    def target_version(self, manifest):
        return manifest.vendor, tuple(sorted(manifest.models)), manifest.version_label

    def same_content(self, old, manifest):
        return old.model_dump() == manifest.model_dump()

    def validate_package(self, path, release):
        m = release.manifest
        with path.open('rb') as stream:
            if stream.read(8) == b'!<arch>\n':
                raise ApiError(422, 'wrong_artifact_type', 'DEB不能作为电机下载资源。')
            stream.seek(0)
            if path.stat().st_size != m.size_bytes or hashlib.file_digest(stream, 'sha256').hexdigest() != m.sha256:
                raise ApiError(422, 'motor_resource_hash_mismatch', '电机下载资源长度或SHA256不符。')

    def catalog(self, model=None):
        rows = [row for row in self._catalog() if model is None or model in row.manifest.models]
        return sorted(rows, key=lambda row: (row.manifest.published_at, row.manifest.release_id), reverse=True)
