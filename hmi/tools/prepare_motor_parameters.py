"""Extract a data-only, explicitly unpublished candidate from the pinned script.
Never import or execute the reference script (it contains real hardware writes).
"""
import ast
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import importlib.util
_spec=importlib.util.spec_from_file_location('parameter_contract',Path(__file__).resolve().parents[1]/'factory_hmi/motor_parameter_contract.py')
_contract=importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_contract)
SOURCE_SHA256,MODELS,validate=_contract.SOURCE_SHA256,_contract.MODELS,_contract.validate

def extract(path):
    raw=Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=SOURCE_SHA256:raise ValueError('参数脚本来源哈希不符')
    assignments={}
    for node in ast.parse(raw).body:
        if isinstance(node,ast.AnnAssign) and isinstance(node.target,ast.Name):
            if node.target.id in {'MODEL_PARAMS','PN_SETTINGS','MODEL_PN_SETTINGS','SCALE'}:
                assignments[node.target.id]=ast.literal_eval(node.value)
        if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name):
            if node.targets[0].id in {'MODEL_PARAMS','PN_SETTINGS','MODEL_PN_SETTINGS','SCALE'}:
                assignments[node.targets[0].id]=ast.literal_eval(node.value)
    rows=[]
    for model,p in assignments['MODEL_PARAMS'].items():
        bounds={'kt':[p['kt']],'kp':[0,p['kp_max']],'kd':[0,p['kd_max']],
                'pos':[-p['pos_max'],p['pos_max']],'spd':[-p['spd_max'],p['spd_max']],
                'tor':[-p['tau_max'],p['tau_max']],'cur':[-p['cur_max'],p['cur_max']],'can_timeout':[0]}
        entries=[]
        for key,values in bounds.items():
            raw_values=[round(v*assignments['SCALE'][key]) for v in values]
            entries.append(dict(key=key,expected_raw=raw_values,tolerance_raw=0 if key=='can_timeout' else max(1,int(max(map(abs,raw_values))*.01))))
        for pn,value in assignments['PN_SETTINGS']+assignments['MODEL_PN_SETTINGS'].get(model,())+((0x0e71,75),):
            entries.append(dict(key=f'pn_{pn:04x}',expected_raw=[value],tolerance_raw=0))
        t=dict(schema='rp1-motor-parameters-v1',template_id=hashlib.sha256(f'{SOURCE_SHA256}:{model}'.encode()).hexdigest()[:32],version='candidate-1',model=MODELS[model],source_sha256=SOURCE_SHA256,source=str(path),applicability=[],entries=entries,confirmation=None)
        rows.append(validate(t))
    return rows

def prepare_script_compatible(path, *, applicability, confirmation, version='script-draft-1'):
    """Prepare data for administrator review, never publish or contact devices.

    applicability maps explicit LRO model names to exact complete version rows.
    Only models with supplied evidence are included; no 108/109 mapping is guessed.
    confirmation uses the contract's evidence_sha256/notes fields and applies to
    the actual draft contents, including any subsequent administrator edits.
    """
    if not isinstance(applicability,dict) or not applicability or any(model not in MODELS for model in applicability):
        raise ValueError('请按LRO型号提供已确认的完整版本适用条件')
    rows=[]
    for candidate in extract(path):
        if candidate['model'] not in applicability:
            continue
        candidate.update(schema=_contract.SCHEMA_V2,protocol_profile=_contract.PN_SCRIPT_PROFILE,
                         template_id=hashlib.sha256(f'{SOURCE_SHA256}:{candidate["model"]}:{_contract.PN_SCRIPT_PROFILE}'.encode()).hexdigest()[:32],
                         version=version,applicability=deepcopy(applicability[candidate['model']]),
                         confirmation=deepcopy(confirmation))
        # Reuse publication validation to ensure this review artifact is complete;
        # this function does not publish it, grant permissions, or write parameters.
        rows.append(validate(candidate,published=True))
    return rows

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',help='已固定SHA256的参数脚本；仅AST读取，不执行')
    parser.add_argument('destination',help='未发布候选或管理员审阅草稿JSON')
    parser.add_argument('--script-compatible',action='store_true',help='生成lro-pn-script-v1管理员审阅草稿（不会发布）')
    parser.add_argument('--applicability',type=Path,help='JSON：LRO型号到精确完整固件/软件/硬件/协议条件列表')
    parser.add_argument('--confirmation-evidence',type=Path,help='实际确认依据文件；计算其SHA256，不执行')
    parser.add_argument('--confirmation-notes',help='管理员确认实际型号、完整版本及模板值的依据说明')
    parser.add_argument('--template-version',default='script-draft-1',help='草稿模板版本，不是电机固件版本')
    args=parser.parse_args(argv)
    supplied=(args.applicability,args.confirmation_evidence,args.confirmation_notes)
    if args.script_compatible:
        if not all(supplied):
            parser.error('--script-compatible需要显式--applicability、--confirmation-evidence和--confirmation-notes')
        evidence=args.confirmation_evidence.read_bytes()
        if not evidence:raise ValueError('确认依据文件不能为空')
        rows=prepare_script_compatible(args.source,
            applicability=json.loads(args.applicability.read_text()),
            confirmation=dict(evidence_sha256=hashlib.sha256(evidence).hexdigest(),notes=args.confirmation_notes),
            version=args.template_version)
        result=dict(status='draft_for_admin_publication',warnings=[
            '本文件未发布；须经平台管理员发布，执行员不能修改模板值',
            '适用条件为显式确认范围，不自动推断108/109兼容关系',
            'Pn响应不回显编号：仅单请求时序关联；超时、矛盾或关联不清须停止',
            'Pn值为32位无符号原始整数，scale=1，不套用串口16位定义或重复缩放',
            'CAN超时0关闭保护；请核对实际模板及掉电保存'],templates=rows)
    else:
        if any(supplied):parser.error('确认选项须与--script-compatible一起使用')
        result=dict(status='candidate_unconfirmed',warnings=['CAN超时0关闭保护；不能据此发布','Pn响应未回显编号；实机适用性待核实','编码范围KP/KD不等于运行增益'],templates=extract(args.source))
    Path(args.destination).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':
    main()
