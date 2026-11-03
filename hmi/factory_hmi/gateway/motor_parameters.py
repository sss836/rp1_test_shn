"""Fetch current immutable templates with the current authenticated session.
No request may supply expected values, device frames or a different instance.
"""
import json
from urllib import parse,request
from factory_hmi.motor_parameter_contract import validate,sha,applicable
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
