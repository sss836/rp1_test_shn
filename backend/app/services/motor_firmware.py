"""Immutable, separately configured motor-firmware store. No flashing here."""
import hashlib
import json
from pathlib import Path

from app.core.errors import ApiError
from app.schemas.motor_firmware import SignedMotorFirmware
from app.services.hmi_updates import HmiUpdateStorage, _json_object


def parse_firmware(raw):
    try:
        return SignedMotorFirmware.model_validate(json.loads(raw, object_pairs_hook=_json_object))
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ApiError(422, "invalid_motor_firmware", "电机固件清单无效，不能使用DEB清单。") from exc


def compatible(m, *, model, hardware, protocol, current_version):
    import re
    if re.fullmatch(r"\d+\.\d+\.\d+", current_version):
        if tuple(map(int, m.software_version.split("."))) <= tuple(map(int, current_version.split("."))):
            return False
    return (m.model == model and hardware in m.hardware_versions and protocol in m.protocol_versions
            and current_version in m.from_versions and current_version != m.version)


class MotorFirmwareStorage(HmiUpdateStorage):
    parse_release = staticmethod(parse_firmware)

    def __init__(self, settings):
        motor_root = Path(settings.motor_firmware_root).expanduser().resolve()
        if settings.hmi_updates_root:
            deb_root = Path(settings.hmi_updates_root).expanduser().resolve()
            if motor_root == deb_root or motor_root in deb_root.parents or deb_root in motor_root.parents:
                raise ApiError(503, "motor_firmware_unavailable", "电机与DEB持久目录不能重叠。")
        super().__init__(settings.model_copy(update={
            "hmi_updates_enabled": settings.motor_firmware_enabled,
            "hmi_updates_root": settings.motor_firmware_root,
            "hmi_updates_public_key_file": settings.motor_firmware_public_key_file}))

    def validate_release_version(self, release):
        pass # Exact numeric software version already validated by schema; no Debian semantics.

    def target_version(self, manifest):
        return manifest.vendor, manifest.model, manifest.version

    def same_content(self, old, manifest):
        # Compatibility and provenance cannot change on an idempotent retry.
        return old.model_dump() == manifest.model_dump()

    def validate_package(self, path, release):
        m = release.manifest
        with path.open("rb") as stream:
            if stream.read(8) == b"!<arch>\n":
                raise ApiError(422, "wrong_artifact_type", "DEB包不能发布为电机固件。")
            stream.seek(0)
            if path.stat().st_size != m.size_bytes or hashlib.file_digest(stream, "sha256").hexdigest() != m.sha256:
                raise ApiError(422, "motor_firmware_hash_mismatch", "电机固件长度或SHA256不符。")

    def applicable(self, **target):
        releases = [r for r in self._catalog() if compatible(r.manifest, **target)]
        # Numeric software order, never filename, publish time, or lexical order.
        current = target["current_version"]
        def numeric(value):
            import re
            return tuple(map(int, value.split("."))) if re.fullmatch(r"\d+\.\d+\.\d+", value) else None
        floor = numeric(current)
        return sorted([r for r in releases if floor is None or numeric(r.manifest.software_version) > floor],
                      key=lambda r: (numeric(r.manifest.software_version), r.manifest.release_id), reverse=True)
