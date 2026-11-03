"""Resource library does not require discovery, a control lease or device I/O."""
import asyncio
import io
import json
import os
from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import Mock,patch

import pytest
from PySide6.QtCore import QCoreApplication,QEvent
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from factory_hmi.desktop.motor_firmware_dialog import MotorFirmwareDialog,MotorFirmwareResourcesDialog
from factory_hmi.gateway.motor_firmware_resources import MotorFirmwareResourceDownloads
from factory_hmi.gateway.app import create_app
from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient
from factory_hmi.client import GatewayClient
from test_software_update_integration import asgi_calls


def catalog():
    return {'artifact_type':'motor_firmware_resource','flash_eligible':False,'releases':[{'manifest':{
        'release_id':'a'*32,'version_label':'5.0.0.109','models':['LRO_5550','LRO_6562','LRO_8462','LRO_10062'],
        'software_version':None,'hardware_versions':None,'protocol_versions':None,'from_versions':None,
        'compatibility_statement':'当前全部支持型号适用（隔离样本）','flash_eligible':False}}]}


@pytest.fixture
def library():
    app=QApplication.instance() or QApplication([])
    identity={'account':('executor',1)};client=Mock();run=Mock()
    dialog=MotorFirmwareResourcesDialog(client,run,lambda:identity['account']);dialog.timer.stop()
    yield dialog,client,run,identity,app
    if isValid(dialog):dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


def complete(run,value):
    call=run.call_args;call.args[2]({'result':value});call.kwargs['on_finished']()


def test_library_queries_and_downloads_without_any_motor_observation(library):
    dialog,client,run,*_=library
    run.call_args.args[1]();client.motor_firmware_resource_releases.assert_called_once()
    complete(run,catalog())
    assert '5.0.0.109' in dialog.files.currentText()
    assert '不是实际电机版本' in dialog.details.toPlainText()
    assert 'flash_eligible=false' in dialog.details.toPlainText()
    dialog.download();run.call_args.args[1]()
    client.motor_firmware_resource_download.assert_called_once_with('a'*32)
    complete(run,{'artifact_type':'motor_firmware_resource','resource_path':'fixture','installed':False,'actual_version_verified':False,'flash_eligible':False})
    assert '未安装、未刷写、未核验实际版本' in dialog.details.toPlainText()
    assert [call[0] for call in client.method_calls]==['motor_firmware_resource_releases','motor_firmware_resource_download']


def test_library_duplicate_requests_are_single_flight(library):
    dialog,client,run,*_=library
    assert run.call_count==1
    dialog.check();dialog.check()
    assert run.call_count==1 and not dialog.download_button.isEnabled()


def test_library_ignores_old_account_reply_and_clears_views(library):
    dialog,client,run,identity,*_=library;call=run.call_args
    identity['account']=('executor',2)
    call.args[2]({'result':catalog()});call.kwargs['on_finished']()
    assert dialog.releases==[] and dialog.files.count()==0 and dialog.details.toPlainText()==''
    dialog.check();complete(run,catalog());assert dialog.releases
    identity['account']=None;dialog.refresh()
    assert not dialog.check_button.isEnabled() and not dialog.download_button.isEnabled()
    assert dialog.releases==[] and dialog.files.count()==0


def test_closed_library_stops_timer_and_drops_late_callbacks(library):
    dialog,client,run,*_=library;call=run.call_args
    dialog.reject()
    assert dialog._closed and not dialog.timer.isActive()
    call.args[2]({'result':catalog()});call.kwargs['on_finished']()
    assert dialog.releases==[]


def test_parent_opens_library_without_scan_and_reuses_only_open_child():
    app=QApplication.instance() or QApplication([]);client=Mock();run=Mock()
    parent=MotorFirmwareDialog(client,run,lambda:[],lambda:('executor',1));parent.timer.stop()
    try:
        parent.resource_library_button.click();child=parent.resource_dialog;child.timer.stop()
        assert isinstance(child,MotorFirmwareResourcesDialog) and run.call_count==1
        parent.resource_library_button.click();assert parent.resource_dialog is child and run.call_count==1
        assert parent.versions.toPlainText()=='尚未扫描电机；没有实际版本证据。'
        parent.reject();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        assert not isValid(child)
        client.motor_firmware_releases.assert_not_called()
        client.motor_maintenance_action.assert_not_called()
    finally:
        if isValid(parent):parent.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


def test_platform_resource_transport_uses_human_cookie_channel_and_no_target():
    client=ReliabilityPlatformClient('https://platform.invalid');opener=Mock()
    opener.open.return_value=io.BytesIO(json.dumps({'data':catalog()}).encode())
    with patch.object(client,'_software_opener',return_value=opener):
        assert client.motor_firmware_resource_releases()==catalog()
    request=opener.open.call_args.args[0]
    assert request.full_url=='https://platform.invalid/api/v1/hmi/motor-firmware-resources/releases'
    assert not request.has_header('Authorization') and '?' not in request.full_url
    with pytest.raises(ValueError):client.motor_firmware_resource_package('../escape')


def test_gateway_client_resources_use_separate_endpoints_and_bounded_timeout():
    client=GatewayClient('http://127.0.0.1:1')
    with patch.object(client,'_request',return_value={}) as request:
        client.motor_firmware_resource_releases()
        assert request.call_args.args==('GET','/api/v1/motor-firmware-resources/releases')
        client.motor_firmware_resource_download('a'*32)
        assert request.call_args.args==('POST','/api/v1/motor-firmware-resources/'+'a'*32+'/download')
        assert request.call_args.kwargs['timeout']==360
        with pytest.raises(ValueError):client.motor_firmware_resource_download('../escape')


def test_resource_gateway_routes_require_token_but_never_take_control_lease(tmp_path,monkeypatch):
    monkeypatch.setenv('RP1_FACTORY_OPERATOR_TOKEN','fixture-token')
    controller=SimpleNamespace(snapshot=Mock(return_value={'state':'disconnected','motors':[]}),close=Mock())
    with patch('factory_hmi.gateway.app.PlcCabinetService'):
        app=create_app(controller,data_root=tmp_path)
    resources=Mock();resources.check.return_value=catalog();resources.download.return_value={'flash_eligible':False,'installed':False}
    app.state.runtime.motor_firmware_resources=resources
    responses=asyncio.run(asgi_calls(app,[
        ('GET','/api/v1/motor-firmware-resources/releases',{}),
        ('GET','/api/v1/motor-firmware-resources/releases',{'headers':{'X-RP1-Operator-Token':'fixture-token'}}),
        ('POST','/api/v1/motor-firmware-resources/'+'a'*32+'/download',{'headers':{'X-RP1-Operator-Token':'fixture-token'}}),
    ]))
    assert [row.status_code for row in responses]==[401,200,200]
    resources.check.assert_called_once();resources.download.assert_called_once_with('a'*32)
    assert not app.state.runtime._sessions


def test_resource_coordinator_rejects_account_change_after_catalog(tmp_path,monkeypatch):
    client=Mock();client.motor_firmware_resource_releases.return_value=catalog()
    sessions=SimpleNamespace(_session=Mock(side_effect=[(client,'first'),(client,'second')]))
    downloads=MotorFirmwareResourceDownloads(sessions,tmp_path)
    monkeypatch.setattr(downloads,'key',lambda:tmp_path/'trusted-public.pem')
    with patch('factory_hmi.gateway.motor_firmware_resources.verify_resource',return_value={}):
        with pytest.raises(ValueError,match='账号已变化'):downloads.check()
    client.motor_firmware_resource_package.assert_not_called()
    assert not downloads.cache_root.exists()


def test_launcher_does_not_seed_or_replace_trusted_motor_key(tmp_path,monkeypatch):
    import factory_hmi
    root=Path(factory_hmi.__file__).resolve().parents[1]
    path=root/'launch.py'
    if not path.is_file():path=root.parent/'launch.py'
    assert path.is_file()
    launcher=runpy.run_path(str(path))
    monkeypatch.delenv('RP1_MOTOR_FIRMWARE_PUBLIC_KEY',raising=False)
    environment=launcher['gateway_environment'](tmp_path,None,offline=True,driver='mock',mode='monitor')
    assert 'RP1_MOTOR_FIRMWARE_PUBLIC_KEY' not in environment
    key=tmp_path/'config/motor-firmware-public-key.pem';key.parent.mkdir();key.write_text('TEST PATH ONLY, NOT A REAL KEY')
    environment=launcher['gateway_environment'](tmp_path,None,offline=True,driver='mock',mode='monitor')
    assert environment['RP1_MOTOR_FIRMWARE_PUBLIC_KEY']==str(key)
    monkeypatch.setenv('RP1_MOTOR_FIRMWARE_PUBLIC_KEY','/explicit/admin/provisioned.pem')
    environment=launcher['gateway_environment'](tmp_path,None,offline=True,driver='mock',mode='monitor')
    assert environment['RP1_MOTOR_FIRMWARE_PUBLIC_KEY']=='/explicit/admin/provisioned.pem'


def test_file_summary_uses_fields_and_keeps_internal_manifest_out_of_view(library):
    dialog,client,run,*_=library
    value=catalog();release=value['releases'][0];m=release['manifest']
    m.update(filename='synthetic.sleepbin',size_bytes=501284,sha256='b'*64,
             confirmation_evidence_sha256='c'*64,compatibility_statement='INTERNAL_APPROVAL_QUOTE')
    release['signature']='INTERNAL_SIGNATURE'
    complete(run,value)
    assert dialog.tabs.currentIndex()==0
    assert '5.0.0.109' in dialog.metadata['version'].text()
    assert dialog.metadata['filename'].text()=='synthetic.sleepbin'
    assert '501,284' in dialog.metadata['size'].text()
    assert '未登记' in dialog.metadata['mapping'].text()
    texts=' '.join(label.text() for label in dialog.metadata.values())+dialog.summary.text()+dialog.details.toPlainText()
    assert 'null' not in texts and 'INTERNAL_SIGNATURE' not in texts and 'INTERNAL_APPROVAL_QUOTE' not in texts
    assert '"release_id"' not in texts
    assert dialog.details.isReadOnly() and 'b'*64 in dialog.details.toPlainText()
    dialog.download()
    assert not dialog.files.isEnabled()  # selected resource cannot change during a download
    complete(run,dict(status='downloaded',resource_path='/synthetic/file.sleepbin',installed=False,actual_version_verified=False))
    assert dialog.metadata['path'].text()=='/synthetic/file.sleepbin'
    assert '下载完成' in dialog.summary.text() and '未核验实际版本' in dialog.summary.text()
