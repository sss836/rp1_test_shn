#!/usr/bin/env python3
"""Prepare a signed motor release for explicit administrator publication.

The metadata must come from verified vendor/maintainer evidence. This tool never
infers firmware/model/hardware versions from filenames and never publishes.
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from factory_hmi.motor_firmware_contract import MotorFirmwareManifest, SignedMotorFirmware


def prepare(firmware, metadata, private_key, output):
    with firmware.open("rb") as stream:
        if stream.read(8) == b"!<arch>\n":
            raise ValueError("DEB不能作为电机固件")
        stream.seek(0)
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    data = {**metadata, "schema_version": "rp1-motor-firmware-v1", "artifact_type": "motor_firmware",
            "release_id": uuid4().hex, "filename": firmware.name, "size_bytes": firmware.stat().st_size,
            "sha256": digest, "published_at": datetime.now(timezone.utc).isoformat()}
    manifest = MotorFirmwareManifest.model_validate(data).model_dump()
    with tempfile.TemporaryDirectory(prefix="motor-release-sign-") as directory:
        message = Path(directory) / "manifest.json"
        message.write_bytes(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
        result = subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(private_key), "-rawin", "-in", str(message)],
                                capture_output=True, check=True, timeout=15)
    release = SignedMotorFirmware(manifest=MotorFirmwareManifest.model_validate(manifest),
        signature=base64.b64encode(result.stdout).decode()).model_dump()
    with output.open("x", encoding="utf-8") as stream:
        json.dump(release, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return release


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.firmware, json.loads(args.metadata.read_text()), args.private_key, args.output)
    print("已生成签名清单；尚未发布、安装或刷写。")
