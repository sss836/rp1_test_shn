"""Read immutable templates from the platform or an installed signed catalog.
No request may supply expected values, device frames or a different instance.
"""
import fcntl
import json
import os
from pathlib import Path
from urllib import parse,request

from factory_hmi.core.motor_parameters import parameter_template_applicable
from factory_hmi.core.production_parameters import production_record
from factory_hmi.motor_parameter_catalog import MAX_CATALOG_BYTES, read_catalog_json, verify_catalog
from factory_hmi.motor_parameter_contract import validate,sha,applicable
from factory_hmi.updates import _atomic_json, _private_directory
from .motor_maintenance import authorize

class Templates:
    def __init__(self,downloads):self.downloads=downloads
    def query(self,observation):
        actor=authorize(self.downloads)
        if observation.get('status')!='known':raise ValueError('实际版本未知，无法选用适用参数模板')
        client,identity=self.downloads.sessions._session()
        query=parse.urlencode({k:observation.get(k,'') for k in ('model','hardware','protocol','software','raw_firmware')})
        req=request.Request(f'{client.base_url}/api/v1/hmi/motor-parameters/templates?{query}',headers={'Accept':'application/json'})
        with client._software_opener().open(req,timeout=3) as stream:raw=stream.read(262145)
        if len(raw)>262144:raise ValueError('参数模板目录过大')
        records=json.loads(raw)['data']['templates']
        if not isinstance(records,list):raise ValueError('模板目录无效')
        for row in records:
            t=validate(row['template'],published=True)
            if sha(t)!=row['sha256'] or not applicable(t,observation) or not row['published_by'] or not row['published_at']:raise ValueError('模板哈希、发布状态或适用性无效')
        current,current_identity=self.downloads.sessions._session()
        if current is not client or identity!=current_identity or authorize(self.downloads)!=actor:raise ValueError('查询期间账号已变化')
        return records
    def get(self,template_id,observation):
        for row in self.query(observation):
            if row['template']['template_id']==template_id:return row
        raise ValueError('模板未发布、已变化或不适用当前实际型号及版本')


class LocalTemplates:
    def __init__(self, downloads, *, root):
        self.downloads = downloads
        self.root = Path(root).expanduser().absolute()
        self.path = self.root / "catalog.json"

    def _records(self):
        try:
            value = read_catalog_json(self.path)
        except FileNotFoundError:
            return []
        return verify_catalog(value, self.downloads.key())

    def query(self, observation):
        if observation.get("status") != "known":
            raise ValueError("实际版本未知，无法选用本机参数模板")
        builtin = production_record(observation)
        try:
            installed = self._records()
        except ValueError:
            if builtin is None:
                raise
            # The package's exact script is an independent source. A damaged
            # imported catalog does not change or authorize any of its values.
            installed = []
        return ([builtin] if builtin else []) + [row for row in installed
                if parameter_template_applicable(row["template"], observation, local=True)]

    def get(self, template_id, observation):
        for row in self.query(observation):
            if row["template"]["template_id"] == template_id:
                return row
        raise ValueError("本机签名目录中没有适用于当前实际型号及版本的模板")

    def install(self, manifest_path):
        if not isinstance(manifest_path, str) or not manifest_path:
            raise ValueError("请选择本机签名参数模板目录")
        envelope = read_catalog_json(manifest_path)
        records = verify_catalog(envelope, self.downloads.key())
        if len(json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode()) > MAX_CATALOG_BYTES:
            raise ValueError("参数模板目录不超过 1 MiB")
        root = _private_directory(self.root)
        fd = os.open(root / "catalog.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path.is_symlink():
                raise ValueError("本机参数目录不能为符号链接")
            try:
                previous = self._records()
            except ValueError:
                # A trusted import can repair a damaged or obsolete signature.
                previous = []
            old_ids = {row["template"]["template_id"]: row["sha256"] for row in previous}
            old_versions = {(row["template"]["model"], row["template"]["version"]): row["sha256"] for row in previous}
            for row in records:
                template = row["template"]
                digest = row["sha256"]
                if (old_ids.get(template["template_id"], digest) != digest
                        or old_versions.get((template["model"], template["version"]), digest) != digest):
                    raise ValueError("已导入的模板编号或型号版本不可覆盖，请签发新编号与版本")
            _atomic_json(self.path, envelope)
        return {"installed": True, "template_count": len(records)}
