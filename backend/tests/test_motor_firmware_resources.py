"""Disposable signatures, ASGI and HMI caches; no production or device actions."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_hmi_updates import materials,actor,app_for,call,run
from app.api.routes import motor_firmware_resources as routes
from app.core.errors import ApiError
from app.schemas.motor_firmware_resources import SignedMotorFirmwareResource
from app.services.motor_firmware_resources import MotorFirmwareResourceStorage,parse_resource
from app.services.motor_firmware import parse_firmware
from app.services.hmi_updates import parse_release
from factory_hmi.motor_firmware_resource_contract import SignedMotorFirmwareResource as HmiContract
from factory_hmi.motor_firmware_resources import stage_resource,verify_resource
from factory_hmi.motor_firmware import verify_firmware

BASE='/api/v1/hmi/motor-firmware-resources'
MODELS=['LRO_5550','LRO_6562','LRO_8462','LRO_10062']


@pytest.fixture
def resource(materials):
    root,settings,*_=materials
    settings=settings.model_copy(update={'motor_firmware_resources_enabled':True,
        'motor_firmware_resources_root':str(root/'resource-store'),
        'motor_firmware_resources_public_key_file':str(root/'test-public.pem'),
        'motor_firmware_root':str(root/'strict-motor-store')})
    content=b'SYNTHETIC-RESOURCE-NEVER-FLASH'*50
    def sign(**changes):
        manifest=dict(schema_version='rp1-motor-firmware-resource-v1',artifact_type='motor_firmware_resource',
            usage='download_only',flash_eligible=False,release_id=uuid4().hex,version_label='5.0.0.109',
            version_label_source='filename_label',vendor='LRO',models=MODELS.copy(),
            software_version=None,hardware_versions=None,protocol_versions=None,from_versions=None,
            compatibility_source='user_direct_confirmation',compatibility_statement='当前全部四型号适用（隔离样本）',
            confirmation_evidence_sha256='1'*64,source_kind='user_supplied_file',source='隔离合成文件',
            filename='fixture.lsepbin',size_bytes=len(content),sha256=hashlib.sha256(content).hexdigest(),
            scope='隔离检查，不可刷写',notes='版本标签不是已回读实际版本',published_at='2026-10-30T18:00:00+08:00')
        manifest.update(changes)
        message=root/(uuid4().hex+'.json')
        message.write_bytes(json.dumps(manifest,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())
        signature=run('openssl','pkeyutl','-sign','-inkey',root/'test-private.pem','-rawin','-in',message).stdout
        return dict(manifest=manifest,signature=base64.b64encode(signature).decode())
    return root,settings,content,sign


def headers(release):
    return {'Content-Type':'application/octet-stream','X-Motor-Resource':base64.b64encode(json.dumps(release).encode()).decode()}


def application(settings,monkeypatch,identity):
    app=app_for(settings,monkeypatch,identity);app.include_router(routes.router)
    monkeypatch.setattr(routes,'get_settings',lambda:settings)
    return app


def publish(fixture,release):
    _,settings,content,_=fixture;storage=MotorFirmwareResourceStorage(settings)
    signed=SignedMotorFirmwareResource.model_validate(release);storage.verify_signature(signed)
    stage=storage.stage();(stage/signed.manifest.filename).write_bytes(content)
    try:return storage.publish(stage,signed,actor_id='isolated-admin',request_id='isolated-resource')
    finally:shutil.rmtree(stage,ignore_errors=True)


@pytest.mark.parametrize('role,status',[('SYSTEM_ADMIN',201),('TEST_EXECUTOR',403),('VIEWER',403)])
def test_resource_only_administrator_publishes(resource,monkeypatch,role,status):
    _,settings,content,sign=resource
    response=call(application(settings,monkeypatch,actor(role)),'POST',BASE+'/releases',headers=headers(sign()),content=content)
    assert response.status_code==status,response.text


@pytest.mark.parametrize('role,status',[('SYSTEM_ADMIN',200),('TEST_EXECUTOR',200),('VIEWER',403)])
def test_resources_download_without_device_versions_uses_real_human_role(resource,monkeypatch,role,status):
    root,settings,content,sign=resource;release=sign();publish(resource,release)
    app=application(settings,monkeypatch,actor(role))
    catalog=call(app,'GET',BASE+'/releases')
    download=call(app,'GET',BASE+'/releases/'+release['manifest']['release_id']+'/package')
    assert catalog.status_code==download.status_code==status
    if status==200:
        assert catalog.json()['data']['releases']==[release]
        assert catalog.json()['data']['flash_eligible'] is False
        assert download.content==content and download.headers['x-artifact-type']=='motor_firmware_resource'
        assert download.headers['x-flash-eligible']=='false' and download.headers['x-content-sha256']==release['manifest']['sha256']
        record=json.loads((root/'resource-store/releases'/release['manifest']['release_id']/'publication.json').read_text())
        assert record['publisher_id']=='isolated-admin' and record['request_id']=='isolated-resource'
        for model in MODELS:
            assert len(call(app,'GET',BASE+'/releases?model='+model).json()['data']['releases'])==1
        assert call(app,'GET',BASE+'/releases?model=FUTURE-UNKNOWN').status_code==422


@pytest.mark.parametrize('identity',[actor(kind='SERVICE'),actor(session=False),None])
def test_resource_service_or_no_session_cannot_publish_or_download(resource,monkeypatch,identity):
    _,settings,content,sign=resource;app=application(settings,monkeypatch,identity)
    for method,path,kwargs in [('GET',BASE+'/releases',{}),('POST',BASE+'/releases',{'headers':headers(sign()),'content':content})]:
        assert call(app,method,path,**kwargs).status_code in (401,403)


def test_resource_publication_uses_existing_real_session_csrf_gate(resource,monkeypatch):
    from app.core import db as db_module
    from app.schemas.security import Principal
    from app.services.auth import AuthService
    _,settings,content,sign=resource
    principal=Principal(id=uuid4(),username='admin',display_name='Admin',role='SYSTEM_ADMIN',kind='HUMAN',
        session_id=uuid4(),session_type='BROWSER',expires_at=datetime.now(timezone.utc)+timedelta(hours=1),
        csrf_token_hash=AuthService.secret_hash('fixture-csrf'))
    class Session:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def commit(self):pass
    monkeypatch.setattr(db_module,'SessionLocal',Session)
    monkeypatch.setattr(AuthService,'resolve',lambda self,token:principal if token=='fixture-session' else None)
    app=application(settings,monkeypatch,None)
    upload=headers(sign());upload['Cookie']='rp1_session=fixture-session; rp1_csrf=fixture-csrf'
    denied=call(app,'POST',BASE+'/releases',headers=upload,content=content)
    assert denied.status_code==403 and denied.json()['error']['code']=='csrf_invalid'
    upload['X-CSRF-Token']='fixture-csrf'
    accepted=call(app,'POST',BASE+'/releases',headers=upload,content=content)
    assert accepted.status_code==201,accepted.text


@pytest.mark.parametrize('changes',[dict(flash_eligible=True),dict(flash_eligible=0),dict(usage='flash'),
    dict(software_version='1.0.9'),dict(hardware_versions=['all']),dict(protocol_versions=['1']),
    dict(from_versions=['1.0.8']),dict(models=['LRO_5550','LRO_5550']),dict(models=['UNKNOWN']),dict(filename='file.deb')])
def test_resource_contract_does_not_invent_unknown_versions_or_allow_flash(resource,changes):
    *_,sign=resource
    with pytest.raises(ValueError):SignedMotorFirmwareResource.model_validate(sign(**changes))


def test_resource_signature_truncation_deb_and_v1_type_are_rejected(resource,materials,monkeypatch):
    _,settings,content,sign=resource;app=application(settings,monkeypatch,actor())
    tampered=sign();tampered['manifest']['notes']='tampered'
    assert call(app,'POST',BASE+'/releases',headers=headers(tampered),content=content).status_code==422
    assert call(app,'POST',BASE+'/releases',headers=headers(sign()),content=content[:-1]).status_code==422
    deb=materials[2].read_bytes()
    disguised=sign(size_bytes=len(deb),sha256=hashlib.sha256(deb).hexdigest())
    assert call(app,'POST',BASE+'/releases',headers=headers(disguised),content=deb).status_code==422
    signed=sign();signed['manifest']['artifact_type']='motor_firmware'
    assert call(app,'POST',BASE+'/releases',headers=headers(signed),content=content).status_code==422
    assert not list((Path(settings.motor_firmware_resources_root)/'.incoming').glob('upload-*'))


def test_resource_idempotency_full_metadata_and_original_receipt_are_immutable(resource):
    root,settings,_,sign=resource;release=sign()
    with ThreadPoolExecutor(max_workers=2) as pool:
        result=list(pool.map(lambda _:publish(resource,release),range(2)))
    assert sorted(row['created'] for row in result)==[False,True]
    for changes in (dict(notes='different'),dict(models=['LRO_5550']),dict(confirmation_evidence_sha256='2'*64)):
        changed=sign(**{**changes,'release_id':release['manifest']['release_id']})
        with pytest.raises(ApiError) as exc:publish(resource,changed)
        assert exc.value.status_code==409
    assert MotorFirmwareResourceStorage(settings).catalog()[0].model_dump()==release
    path=root/'resource-store/releases'/release['manifest']['release_id']/'fixture.lsepbin'
    path.write_bytes(b'corrupt')
    with pytest.raises(ApiError):MotorFirmwareResourceStorage(settings).package(release['manifest']['release_id'])


def test_resource_cache_truncation_hit_corruption_and_old_release_preserved(resource):
    root,_,content,sign=resource;release=sign();kwargs=dict(key=root/'test-public.pem',cache_root=root/'resource-cache')
    with pytest.raises(ValueError):stage_resource(release,**kwargs,open_stream=lambda:io.BytesIO(content[:5]))
    assert not list((root/'resource-cache/releases').glob('.partial-*'))
    first=stage_resource(release,**kwargs,open_stream=lambda:io.BytesIO(content),downloaded_by='fixture-user')
    assert first['flash_eligible'] is False and not first['installed'] and not first['actual_version_verified']
    assert first['downloaded_by']=='fixture-user' and 'firmware_path' not in first and 'target_observation' not in first
    assert all(first['manifest'][name] is None for name in ('software_version','hardware_versions','protocol_versions','from_versions'))
    cached=stage_resource(release,**kwargs,open_stream=lambda:pytest.fail('cache hit must not open stream'))
    assert cached['cache_hit']
    Path(first['resource_path']).write_bytes(b'corrupt')
    repaired=stage_resource(release,**kwargs,open_stream=lambda:io.BytesIO(content))
    assert not repaired['cache_hit'] and Path(repaired['resource_path']).read_bytes()==content
    assert list((root/'resource-cache/releases').glob('.corrupt-*'))
    orphan=root/'resource-cache/releases/.partial-crashed';orphan.mkdir();(orphan/'part').write_bytes(b'old')
    with pytest.raises(ValueError):stage_resource(sign(version_label='other'),**kwargs,open_stream=lambda:io.BytesIO(b'truncated'))
    assert not orphan.exists() and Path(first['resource_path']).read_bytes()==content


def test_resource_cache_stops_on_identity_change_without_success_receipt(resource):
    root,_,content,sign=resource;checks=[]
    def check():
        checks.append(1)
        if len(checks)>1:raise ValueError('account changed')
    with pytest.raises(ValueError,match='account changed'):
        stage_resource(sign(),key=root/'test-public.pem',cache_root=root/'cache',
                       open_stream=lambda:io.BytesIO(content),check_session=check)
    assert not list((root/'cache').glob('**/download.json'))
    assert not list((root/'cache/releases').glob('.partial-*'))


def test_resource_contract_cannot_be_used_for_v1_deb_or_ota(resource,materials):
    root,_,content,sign=resource;release=sign()
    assert HmiContract.model_json_schema()==SignedMotorFirmwareResource.model_json_schema()
    raw=json.dumps(release).encode()
    for parser in (parse_firmware,parse_release):
        with pytest.raises(ApiError):parser(raw)
    target=dict(status='known',model='LRO_5550',hardware='fixture',protocol='fixture',raw_firmware='fixture',
                software='1.0.8',current_version='fixture',interface='can-fixture',motor_id=1,
                observed_at=datetime.now(timezone.utc).isoformat())
    with pytest.raises(ValueError):verify_firmware(release,root/'test-public.pem',target)
    # Even manually copying a resource into the legacy OTA cache cannot make
    # its strict signed manifest eligible for the existing writer.
    folder=root/'legacy-motor-cache/releases'/release['manifest']['release_id'];folder.mkdir(parents=True)
    (folder/'release.json').write_text(json.dumps(release));(folder/'fixture.lsepbin').write_bytes(content)
    from factory_hmi.gateway.motor_maintenance import artifact,cached_releases
    downloads=SimpleNamespace(cache_root=root/'legacy-motor-cache',key=lambda:root/'test-public.pem')
    assert cached_releases(downloads,target)==[]
    with pytest.raises(ValueError):artifact(downloads,release['manifest']['release_id'],target)
    deb_envelope=materials[4](materials[2])
    with pytest.raises(ApiError):parse_resource(json.dumps(deb_envelope).encode())


@pytest.mark.parametrize('boundary',['hmi_updates_root','motor_firmware_root','archive_root'])
def test_resource_storage_does_not_overlap_other_stores(resource,boundary):
    _,settings,_,_=resource
    with pytest.raises(ApiError):
        MotorFirmwareResourceStorage(settings.model_copy(update={'motor_firmware_resources_root':getattr(settings,boundary)}))


def test_resource_prepare_tool_keeps_all_unknowns_and_no_flashing(resource):
    root,_,content,sign=resource
    path=Path(__file__).resolve().parents[2]/'hmi/tools/prepare_motor_firmware_resource.py'
    spec=importlib.util.spec_from_file_location('prepare_resource',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    firmware=root/'user.lsepbin';firmware.write_bytes(content)
    metadata={name:value for name,value in sign()['manifest'].items() if name not in {'release_id','filename','size_bytes','sha256','published_at','schema_version','artifact_type','usage','flash_eligible'}}
    result=module.prepare(firmware,metadata,root/'test-private.pem',root/'prepared-resource.json')
    manifest=verify_resource(result,root/'test-public.pem')
    assert manifest['version_label']=='5.0.0.109' and manifest['software_version'] is None
    assert manifest['models']==MODELS and manifest['flash_eligible'] is False
    with pytest.raises(ValueError,match='不得填写'):
        module.prepare(firmware,dict(metadata,software_version='1.0.9'),root/'test-private.pem',root/'invalid.json')
