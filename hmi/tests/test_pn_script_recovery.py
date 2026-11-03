"""Explicit CAN-down recovery of Pn uncertainty, using only fake CAN and files.

An archived host boundary is not evidence of a motor power cycle. Each new
session still has to observe its own quiet receive window before sending.
"""
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from factory_hmi.core import motor_parameter_transport as transport
from factory_hmi.core.service import FactoryService
from test_pn_script_session import Harness,entry,pn_reply,target


@pytest.fixture
def service_boundary(tmp_path):
    manager=Mock();manager.status.return_value=[SimpleNamespace(interface='can-pn-fixture',up=False)]
    service=FactoryService(data_root=tmp_path,can_manager=manager)
    service._backend_name='socketcan-fixture'
    service._can_interfaces=('can-pn-fixture',)
    service.snapshot=Mock(return_value={'can':{'all_up':False}})
    task=Mock();task.active=False;service._parameters=task
    root=service.report_manager.data_root/'pn-quarantine'
    failed=Harness();session=failed.session(quarantine_root=root)
    with pytest.raises(transport.SessionQuarantinedError,match='超时'):
        session.read(target(),entry(),threading.Event())
    session.close()
    marker=transport._quarantine_path(root,'can-pn-fixture')
    original=marker.read_bytes()
    try:
        yield service,manager,task,root,marker,original
    finally:
        task.cancel.side_effect=None
        service._controller=None
        service.close()


def test_verified_can_down_archives_original_reason_without_claiming_motor_power(service_boundary):
    service,manager,task,root,marker,original=service_boundary
    order=[]
    task.cancel.side_effect=lambda *args:order.append('cancel')
    manager.disconnect.side_effect=lambda interfaces:order.append('disconnect')
    manager.status.side_effect=lambda interfaces:(order.append('status') or [SimpleNamespace(interface='can-pn-fixture',up=False)])
    result=service.can_disconnect()
    assert result=={'can':{'all_up':False}}
    assert order==['cancel','disconnect','status']
    manager.disconnect.assert_called_once_with(('can-pn-fixture',))
    manager.status.assert_called_once_with(('can-pn-fixture',))
    assert not marker.exists()
    archived=list((root/'history').glob('*.json'))
    assert len(archived)==1
    record=json.loads(archived[0].read_text())
    for key,value in json.loads(original).items():
        assert record[key]==value
    assert '超时' in record['error']
    assert '用户显式断开CAN' in record['rearm_reason']
    assert '接口关闭已确认' in record['rearm_reason']
    assert '不代表检测到电机重新上电' in record['rearm_reason']
    assert record['motor_power_cycle_detected'] is False and record['rearmed_at']


@pytest.mark.parametrize('failure',[
    'disconnect_failed','still_up','status_missing','foreign_status',
    'status_query_failed','status_none','status_without_up','motor_connected','cancel_failed',
])
def test_unconfirmed_can_down_preserves_active_quarantine(service_boundary,failure):
    service,manager,task,root,marker,original=service_boundary
    if failure=='disconnect_failed':manager.disconnect.side_effect=OSError('synthetic CAN down failure')
    elif failure=='still_up':manager.status.return_value=[SimpleNamespace(interface='can-pn-fixture',up=True)]
    elif failure=='status_missing':manager.status.return_value=[]
    elif failure=='foreign_status':manager.status.return_value=[SimpleNamespace(interface='can-other-fixture',up=False)]
    elif failure=='status_query_failed':manager.status.side_effect=OSError('synthetic status query failure')
    elif failure=='status_none':manager.status.return_value=None
    elif failure=='status_without_up':manager.status.return_value=[SimpleNamespace(interface='can-pn-fixture')]
    elif failure=='motor_connected':service._controller=SimpleNamespace(backend=SimpleNamespace(connected=True))
    elif failure=='cancel_failed':task.cancel.side_effect=TimeoutError('synthetic maintenance cancel timeout')
    with pytest.raises((RuntimeError,OSError,TypeError,AttributeError)):
        service.can_disconnect()
    assert marker.read_bytes()==original
    assert not list((root/'history').glob('*.json'))
    service.snapshot.assert_not_called()
    if failure in ('motor_connected','cancel_failed'):
        manager.disconnect.assert_not_called();manager.status.assert_not_called()
    if failure=='disconnect_failed':manager.status.assert_not_called()


def test_archive_does_not_clear_other_interface_marker(tmp_path):
    root=tmp_path/'quarantine'
    for interface in ('can-first-fixture','can-second-fixture'):
        harness=Harness();session=harness.session(quarantine_root=root)
        with pytest.raises(transport.SessionQuarantinedError):
            session.read(target(interface=interface),entry(),threading.Event())
        session.close()
    second=transport._quarantine_path(root,'can-second-fixture')
    original_second=second.read_bytes()
    records=transport.archive_pn_quarantine(root,['can-first-fixture'],reason='synthetic explicitly verified interface down')
    assert len(records)==1 and json.loads(Path(records[0]).read_text())['interface']=='can-first-fixture'
    assert second.read_bytes()==original_second
    assert not transport._quarantine_path(root,'can-first-fixture').exists()


def test_archive_persistence_failure_keeps_active_marker(service_boundary,monkeypatch):
    service,manager,task,root,marker,original=service_boundary
    writer=transport._atomic_json
    def fail_archive(path,value):
        if path.parent.name=='history':raise OSError('synthetic archive persistence failed')
        return writer(path,value)
    monkeypatch.setattr(transport,'_atomic_json',fail_archive)
    with pytest.raises(OSError,match='archive persistence failed'):
        service.can_disconnect()
    assert marker.read_bytes()==original
    service.snapshot.assert_not_called()


def test_rearmed_new_session_observes_quiet_window_before_first_request(service_boundary):
    service,manager,task,root,marker,original=service_boundary
    service.can_disconnect()
    times=[]
    def respond(bus,packet):
        times.append(bus.clock.monotonic());bus.queue(pn_reply(),.001)
    harness=Harness(respond);session=harness.session(quarantine_root=root)
    start=harness.clock.monotonic()
    try:
        result=session.read(target(),entry(),threading.Event())
        assert result['status']=='read' and result['parameter_number_echoed'] is False
        assert times[0]>=start+session.quiet_period
        assert len(harness.bus.sent)==1 and not marker.exists()
    finally:
        session.close()
    assert len(list((root/'history').glob('*.json')))==1


def test_rearmed_new_session_with_residual_reply_sends_nothing_and_reisolates(service_boundary):
    service,manager,task,root,marker,original=service_boundary
    service.can_disconnect()
    harness=Harness(lambda bus,packet:bus.queue(pn_reply(),.001))
    harness.bus.queue(pn_reply(999))
    session=harness.session(quarantine_root=root)
    try:
        with pytest.raises(transport.SessionQuarantinedError,match='残留'):
            session.read(target(),entry(),threading.Event())
        assert harness.bus.sent==[]
        assert marker.exists()
        assert '残留' in json.loads(marker.read_text())['error']
    finally:
        session.close()
    assert len(list((root/'history').glob('*.json')))==1
