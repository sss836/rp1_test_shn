"""Versioned parameter data only; no executable expressions or driver settings.

Keep the backend copy identical. Values are wire integers; dimensions and scales
are fixed here, independently of editable station running gains.
"""
from copy import deepcopy
import hashlib
import json
import re

SOURCE_SHA256 = '34f2214d52cf5e879e9ec169ec91725b22cdc99228a47d2671a491c9c85b0f3c'
MODELS = ('LRO_5550', 'LRO_6562', 'LRO_8462', 'LRO_10062')
SCHEMA_V1 = 'rp1-motor-parameters-v1'
SCHEMA_V2 = 'rp1-motor-parameters-v2'
PN_SCRIPT_PROFILE = 'lro-pn-script-v1'
# query, config, scale, signed, scalar, unit, meaning
SPECS = {
 'kt': (0x16,0x04,100,False,True,'N·m/A','转矩常数；Iq/RMS关系须按型号核实'),
 'kp': (0x17,0x05,1,False,False,'编码值','MIT编码范围KP，非运行增益'),
 'kd': (0x18,0x06,1,False,False,'编码值','MIT编码范围KD，非运行增益'),
 'pos': (0x19,0x07,100,True,False,'rad','位置编码范围'),
 'spd': (0x1a,0x08,100,True,False,'rad/s','速度编码范围'),
 'tor': (0x1b,0x09,10,True,False,'N·m','转矩编码范围'),
 'cur': (0x1c,0x0a,10,True,False,'A','电流编码范围；电流定义须核实'),
 'can_timeout': (0x1f,0x0b,1,False,True,'ms','CAN超时；0=关闭保护'),
}
PN_NUMBERS = (0x0401,0x0402,0x0403,0x0407,0x0e05,0x0eb2,0x0f09,0x0f0a,0x0e71)

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()

def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()

def spec(key):
    if key in SPECS:
        q,c,scale,signed,scalar,unit,meaning=SPECS[key]
        return dict(query=q,config=c,scale=scale,signed=signed,scalar=scalar,unit=unit,meaning=meaning,pn=None)
    if re.fullmatch(r'pn_[0-9a-f]{4}',key) and int(key[3:],16) in PN_NUMBERS:
        return dict(query=0x11,config=0x10,scale=1,signed=False,scalar=True,
                    unit='V' if key=='pn_0e71' else '厂商原始值',meaning='Pn参数（适用性及单位须按厂商资料确认）',pn=int(key[3:],16))
    raise ValueError('不支持的参数编号')

def validate(value, *, published=False):
    fields={'schema','template_id','version','model','source_sha256','source','applicability','entries','confirmation'}
    if not isinstance(value,dict):
        raise ValueError('参数模板类型或字段无效')
    schema=value.get('schema')
    if schema==SCHEMA_V2:
        fields.add('protocol_profile')
    if set(value)!=fields or schema not in (SCHEMA_V1,SCHEMA_V2):
        raise ValueError('参数模板类型或字段无效')
    if schema==SCHEMA_V2 and (value['protocol_profile']!=PN_SCRIPT_PROFILE or value['source_sha256']!=SOURCE_SHA256):
        raise ValueError('Pn脚本兼容配置必须使用已确认的协议及脚本来源SHA256')
    if not re.fullmatch('[0-9a-f]{32}',str(value['template_id'])) or value['model'] not in MODELS:
        raise ValueError('模板编号或型号无效')
    if not re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z._+-]{0,63}',str(value['version'])):
        raise ValueError('模板版本无效')
    if not re.fullmatch('[0-9a-f]{64}',str(value['source_sha256'])) or not isinstance(value['source'],str) or not 1<=len(value['source'])<=2048:
        raise ValueError('缺少模板来源与SHA256')
    apps=value['applicability']
    if not isinstance(apps,list) or len(apps)>128 or (published and not apps):
        raise ValueError('发布前须逐条确认完整固件、软件、硬件、协议适用性')
    for row in apps:
        if not isinstance(row,dict) or set(row)!={'raw_firmware','software','hardware','protocol'} or any(not isinstance(v,str) or not re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z._+-]{0,63}',v) for v in row.values()):
            raise ValueError('适用条件必须为明确版本，不能使用通配符')
    if len({sha(r) for r in apps})!=len(apps):raise ValueError('重复适用条件')
    c=value['confirmation']
    if c is not None and (not isinstance(c,dict) or set(c)!={'evidence_sha256','notes'} or not re.fullmatch('[0-9a-f]{64}',str(c['evidence_sha256'])) or not isinstance(c['notes'],str) or not 1<=len(c['notes'])<=4000):
        raise ValueError('管理员确认必须包含适用性证据哈希及说明')
    if published and not c:raise ValueError('候选模板尚未由管理员确认')
    entries=value['entries']
    if not isinstance(entries,list) or not 1<=len(entries)<=32:raise ValueError('零项或过多参数不能通过')
    keys=set()
    for row in entries:
        if not isinstance(row,dict) or set(row)!={'key','expected_raw','tolerance_raw'}:raise ValueError('参数只接受预期原始整数和绝对容差')
        key=row['key']; s=spec(key)
        if key in keys:raise ValueError('重复参数')
        keys.add(key)
        raw=row['expected_raw']; low=-32768 if s['signed'] else 0
        high=32767 if s['signed'] else (0xffffffff if s['pn'] else 65535)
        if not isinstance(raw,list) or len(raw)!=(1 if s['scalar'] else 2) or any(type(v)is not int or not low<=v<=high for v in raw) or (len(raw)==2 and raw[0]>raw[1]):
            raise ValueError('参数原始值超界、量纲或范围无效')
        if type(row['tolerance_raw'])is not int or not 0<=row['tolerance_raw']<=min(10000,high):raise ValueError('容差必须为有界原始整数')
    return deepcopy(value)

def pn_profile(template):
    """Return only a fully confirmed script profile; no version inference.

    v1 remains valid for ordinary parameters and does not authorize Pn writes.
    An unconfirmed or malformed v2 must raise, rather than silently falling back
    to the legacy no-profile path.
    """
    value=validate(template,published=isinstance(template,dict) and template.get('schema')==SCHEMA_V2)
    return value['protocol_profile'] if value['schema']==SCHEMA_V2 else None

def applicable(template, observation):
    if observation.get('status')!='known' or observation.get('model')!=template['model']:
        return False
    return any(all(observation.get(k)==v for k,v in row.items()) for row in template['applicability'])

def compare(entry, result):
    raw=result.get('raw')
    if result.get('status')!='read' or not isinstance(raw,list) or len(raw)!=len(entry['expected_raw']) or any(type(v)is not int for v in raw):
        return result.get('status','unknown') if result.get('status')!='read' else 'unknown'
    return 'passed' if all(abs(a-b)<=entry['tolerance_raw'] for a,b in zip(raw,entry['expected_raw'])) else 'mismatch'

def display(entry, raw):
    s=spec(entry['key'])
    return {'value':[v/s['scale'] for v in raw] if raw is not None else None,
            'unit':s['unit'],'meaning':s['meaning'],'tolerance':entry['tolerance_raw']/s['scale']}
