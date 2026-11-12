#!/usr/bin/env python3
"""Sign an export of administrator-published parameter templates for offline use."""

import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from factory_hmi.motor_parameter_catalog import MAX_CATALOG_BYTES, catalog_body, read_catalog_json
from factory_hmi.motor_parameter_contract import canonical


def prepare(records, private_key, output):
    body = catalog_body(records)
    with tempfile.TemporaryDirectory(prefix="rp1-parameter-catalog-sign-") as directory:
        message = Path(directory) / "catalog.json"
        message.write_bytes(canonical(body))
        signed = subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(private_key),
                                 "-rawin", "-in", str(message)], check=True, capture_output=True, timeout=15)
    if len(signed.stdout) != 64:
        raise ValueError("请使用 Ed25519 签名私钥")
    envelope = {**body, "signature": base64.b64encode(signed.stdout).decode("ascii")}
    data = json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode() + b"\n"
    if len(data) > MAX_CATALOG_BYTES:
        raise ValueError("签名参数模板目录不超过 1 MiB")
    with output.open("xb") as stream:
        stream.write(data)
    return envelope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, required=True,
                        help="已发布记录数组，或含 data.templates / templates 的平台导出 JSON")
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = read_catalog_json(args.metadata.expanduser().absolute())
    if isinstance(records, dict):
        data = records.get("data", records)
        records = data.get("templates") if isinstance(data, dict) else None
    signed = prepare(records, args.private_key, args.output)
    print(f"已签名 {len(signed['templates'])} 个参数模板，可从上位机导入。")


if __name__ == "__main__":
    main()
