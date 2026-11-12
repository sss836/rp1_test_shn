#!/usr/bin/env python3
"""Sign administrator-verified exact OTA compatibility evidence; never publish or flash."""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile

# Keep the administrator's signing tool usable with only Python and OpenSSL.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "factory_hmi"))
from motor_ota_policy import validate_policy


def prepare(metadata, private_key, output):
    validate_policy(metadata)
    with tempfile.TemporaryDirectory(prefix='motor-ota-sign-') as temp:
        path=Path(temp)/'policy.json'
        path.write_bytes(json.dumps(metadata,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())
        signed=subprocess.run(['openssl','pkeyutl','-sign','-inkey',str(private_key),'-rawin','-in',str(path)],check=True,capture_output=True,timeout=15)
    envelope={'policy':metadata,'signature':base64.b64encode(signed.stdout).decode()}
    with output.open('x') as stream:json.dump(envelope,stream,ensure_ascii=False,indent=2)
    return envelope


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata',type=Path,required=True)
    parser.add_argument('--private-key',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    prepare(json.loads(args.metadata.read_text()),args.private_key,args.output)
    print('已签名兼容证据；尚未部署、发布或刷写。')
