#!/usr/bin/env python3
"""Sign administrator-verified exact OTA compatibility evidence; never publish or flash."""
import argparse
import base64
import json
from pathlib import Path
import re
import subprocess
import tempfile


def prepare(metadata, private_key, output):
    fields={'model','hardware','protocol','raw_version','software_version','ota_protocol','evidence_url','evidence_sha256'}
    if set(metadata)!={'schema','entries'} or metadata['schema']!='rp1-lro-ota-compatibility-v1' or not 1<=len(metadata['entries'])<=1024:
        raise ValueError('invalid OTA mapping schema or entry count')
    seen=set()
    for row in metadata['entries']:
        if (set(row)!=fields or any(not isinstance(v,str) for v in row.values()) or
            row['model'] not in {'LRO_5550','LRO_6562','LRO_8462','LRO_10062'} or
            not re.fullmatch(r'\d+\.\d+\.\d+',row['software_version']) or
            tuple(map(int,row['software_version'].split('.')))<(1,0,8) or
            row['ota_protocol']!='lro-canfd-xcp-v1' or not row['evidence_url'].startswith('https://') or
            not re.fullmatch(r'[0-9a-f]{64}',row['evidence_sha256'])):
            raise ValueError('explicit 108+ version relationship and source evidence required')
        identity=tuple(row[k] for k in ('model','hardware','protocol','raw_version'))
        if identity in seen:raise ValueError('duplicate OTA identity')
        seen.add(identity)
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
