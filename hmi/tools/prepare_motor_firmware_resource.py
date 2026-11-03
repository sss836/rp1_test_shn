#!/usr/bin/env python3
"""Prepare a signed download-only resource; never publishes, installs or flashes."""
import argparse
import base64
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from factory_hmi.motor_firmware_resource_contract import MotorFirmwareResourceManifest,SignedMotorFirmwareResource


def prepare(firmware,metadata,private_key,output):
    mappings=('software_version','hardware_versions','protocol_versions','from_versions')
    if any(metadata.get(name) is not None for name in mappings):
        raise ValueError('下载资源不得填写或推断设备版本映射；四项均须为null')
    with firmware.open('rb') as stream:
        if stream.read(8)==b'!<arch>\n':raise ValueError('DEB不能作为电机下载资源')
        stream.seek(0);digest=hashlib.file_digest(stream,'sha256').hexdigest()
    data={**metadata,'schema_version':'rp1-motor-firmware-resource-v1','artifact_type':'motor_firmware_resource',
          'usage':'download_only','flash_eligible':False,'release_id':uuid4().hex,'filename':firmware.name,
          'size_bytes':firmware.stat().st_size,'sha256':digest,'published_at':datetime.now(timezone.utc).isoformat(),
          **{name:None for name in mappings}}
    manifest=MotorFirmwareResourceManifest.model_validate(data).model_dump()
    with tempfile.TemporaryDirectory(prefix='motor-resource-sign-') as directory:
        message=Path(directory)/'manifest.json'
        message.write_bytes(json.dumps(manifest,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())
        signature=subprocess.run(['openssl','pkeyutl','-sign','-inkey',str(private_key),'-rawin','-in',str(message)],
                                 capture_output=True,check=True,timeout=15).stdout
    release=SignedMotorFirmwareResource(manifest=manifest,signature=base64.b64encode(signature).decode()).model_dump()
    with output.open('x',encoding='utf-8') as stream:json.dump(release,stream,ensure_ascii=False,indent=2);stream.write('\n')
    return release


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('firmware','metadata','private-key','output'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    prepare(args.firmware,json.loads(args.metadata.read_text()),args.private_key,args.output)
    print('已准备仅下载资源签名清单；未发布、安装或刷写。')
