"""Read-only Pn metadata from the user-supplied RoboPartyDiver documentation.

Membus catalog metadata is not a CAN adapter, model mapping or published write
template. It never selects a target, sends a frame or converts a measured value.
"""
from functools import lru_cache
import hashlib
import json
from pathlib import Path

from factory_hmi.motor_parameter_contract import PN_NUMBERS, SOURCE_SHA256, validate

CATALOG_SHA256 = 'c9995c1535f101b86c5ecac548ff6a6b69955a5627234110ad0282dbdd273f62'
DOCUMENTATION_ZIP_SHA256 = 'de0c3701c67528b0fa2da945b9108535c8fc41c280993e46bf2f3a22db96ad68'
CATALOG_PATH = Path(__file__).resolve().parents[1]/'resources/motor-parameters/pn-robopartydiver-0.6.0.reference.json'
SCRIPT_REFERENCE_PATH = CATALOG_PATH.with_name('script-compatible-reference-v2.json')
SCRIPT_REFERENCE_SHA256 = 'a1379a5a97d4dc115f6e6f2b863297df33c15c68d848c9ea35dc413a243d53d2'


def script_reference(model):
    """Pinned reference values for read-only diagnosis; not a write template."""
    raw = SCRIPT_REFERENCE_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SCRIPT_REFERENCE_SHA256:
        raise ValueError('脚本参考表哈希不符')
    content = json.loads(raw)
    if content['source_sha256'] != SOURCE_SHA256:
        raise ValueError('脚本参考表来源不符')
    matches = [validate(value) for value in content['templates'] if value['model'] == model]
    if len(matches) != 1:
        raise ValueError('当前型号缺少唯一的脚本参考值')
    return matches[0]


def _validated_catalog(raw):
    if len(raw) > 128 * 1024 or hashlib.sha256(raw).hexdigest() != CATALOG_SHA256:
        raise ValueError('Pn参考资料哈希不符；不能冒称已核对来源')
    value = json.loads(raw.decode('utf-8-sig'))
    if (value.get('application'), value.get('version'), value.get('parameter_number_base')) != ('RoboPartyDiver', '0.6.0', 16):
        raise ValueError('Pn参考资料类型或版本无效')
    libraries = value['libraries']
    keys = {f'{number:04X}' for number in PN_NUMBERS}
    if set(libraries) != {'0707', '0808', '0901', 'C001'} or any(set(rows) != keys for rows in libraries.values()):
        raise ValueError('Pn参数集或编号范围与来源不符')
    for key in keys:
        definition = libraries['0707'][key]['definition']
        if any(rows[key]['definition'] != definition for rows in libraries.values()):
            raise ValueError('Pn参数集定义不一致，不能合并为通用参考')
    return value


@lru_cache(maxsize=1)
def _catalog():
    return _validated_catalog(CATALOG_PATH.read_bytes())


def _raw_integer(value):
    return int(value[:-1], 16) if value.endswith('H') else int(value)


def reference_for_parameter(key):
    if key not in {f'pn_{number:04x}' for number in PN_NUMBERS}:
        return None
    catalog = _catalog()
    number = key[3:].upper()
    definition = catalog['libraries']['0707'][number]['definition']
    return {
        'status': 'reference_only', 'can_applicability': 'unverified',
        'transport': 'usb_serial_membus', 'application': 'RoboPartyDiver', 'version': '0.6.0',
        'key': key, 'name': definition['ParaName'], 'raw_width_bits': 16, 'signed': False,
        'raw_range': [_raw_integer(definition['ParaMinVal']), _raw_integer(definition['ParaMaxVal'])],
        'raw_default': _raw_integer(definition['ParaDefVal']),
        'display_factor': str(definition['ParaUnitRate']), 'unit_text': definition['ParaUnit'],
        'show_base': definition['ParaShowBase'], 'requires_reboot': definition['ParaNeedReboot'],
        'catalog_read_only_flag': definition['ParaIsReadOnly'], 'catalog_level': definition['ParaLevel'],
        'parameter_set_ids': list(catalog['libraries']),
        'source_json_sha256': CATALOG_SHA256, 'documentation_zip_sha256': DOCUMENTATION_ZIP_SHA256,
        'source_definition_sha256': hashlib.sha256(json.dumps(definition, sort_keys=True,
            separators=(',', ':'), ensure_ascii=False).encode()).hexdigest(),
        'library_sources': [dict(parameter_set_id=library, path=rows[number]['source_path'],
                                 line=rows[number]['source_start_line'])
                            for library, rows in catalog['libraries'].items()],
        'limitations': ['型号和完整固件适用性待核实', '参数集ID不能当作LRO型号',
                        'USB/串口Membus定义不能推定CAN报文', '默认值不是实测值或管理员模板值',
                        '倍率与单位原文分开保留，不对未核验响应作物理量换算'],
    }


def reference_fields(key):
    reference = reference_for_parameter(key)
    return {'reference': reference} if reference else {}


def describe_reference(reference):
    low, high = reference['raw_range']
    bounds=f'{low:04X}H～{high:04X}H' if reference['show_base']=='NBL' else f'{low}～{high}'
    default=f"{reference['raw_default']:04X}H" if reference['show_base']=='NBL' else str(reference['raw_default'])
    return (f"Pn参考定义（USB/串口Membus，CAN适用性待核实）：{reference['name']}；"
            f"16位无符号；原始范围 {bounds}；文档默认 {default}（非实测/非模板）；"
            f"显示倍率 ×{reference['display_factor']}；单位原文 {reference['unit_text']}；"
            f"文档生效标记 {'重启' if reference['requires_reboot'] else '立即'}；"
            f"来源 {reference['application']} {reference['version']} · SHA256 {reference['source_json_sha256']}")


def reference_text():
    lines = ['Pn参考资料（用户提供的源码分析资料，适用性待核实）',
             '本页只读。参数集0707/0808/0901/C001没有与LRO型号或108/109完整版本建立对应。',
             'USB/串口Membus报文不能直接用于CAN；查看资料不会读取、发布模板或写入设备。',
             '文档默认值不会替换管理员模板或设备实测值；倍率与单位原文分开保留，避免重复缩放。',
             '“重启/立即”是资料中的生效标记，不是掉电保存或已生效的证明。',
             '资料包SHA256：' + DOCUMENTATION_ZIP_SHA256, '']
    for number in PN_NUMBERS:
        reference = reference_for_parameter(f'pn_{number:04x}')
        lines.append(f"Pn{number:04X} · " + describe_reference(reference))
        lines.append('')
    return '\n'.join(lines)
