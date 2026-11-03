"""Real ASGI -> engine -> script codec, with fake CAN/PLC and synthetic templates."""
import asyncio
from copy import deepcopy
from unittest.mock import patch

import pytest

from factory_hmi.core import motor_parameters as engine_module
from factory_hmi.core.motor_parameter_transport import FRAME
from factory_hmi.gateway.app import create_app
from factory_hmi.motor_parameter_contract import PN_SCRIPT_PROFILE,SCHEMA_V2,sha
from test_module_parameters import template,record
from test_module_firmware_gateway import setup_service
from test_module_firmware_maintenance import observation
from test_pn_script_session import Harness,pn_reply


@pytest.mark.parametrize('write_takes_effect',[True,False])
def test_asgi_published_script_template_to_wire_and_separate_power_recheck(tmp_path,monkeypatch,write_takes_effect):
    import httpx
    from factory_hmi.gateway import motor_maintenance as permissions,motor_parameters as templates
    from factory_hmi.core import motor_ota_transport as versions
    t=template()
    t.update(schema=SCHEMA_V2,protocol_profile=PN_SCRIPT_PROFILE,
             entries=[dict(key='pn_0401',expected_raw=[200],tolerance_raw=0)])
    rec=record(t);assert rec['sha256']==sha(t)
    # Complete versions here are synthetic fixtures, not a 108/109 certification.
    monkeypatch.setattr(permissions,'authorize',lambda d:dict(username='executor',client_session='fixture',role='TEST_EXECUTOR'))
    monkeypatch.setattr(templates.Templates,'query',lambda self,o:[deepcopy(rec)])
    monkeypatch.setattr(versions,'read_version',lambda row,stop:observation(row['index']))
    actual=[100];harnesses=[];sent=[]
    def respond(bus,packet):
        motor_id,length,_,padded=FRAME.unpack(packet);data=padded[:length]
        sent.append(data.hex())
        if data[:2]==b'\xc0\x11':bus.queue(pn_reply(actual[0],motor_id),.001)
        elif data[:2]==b'\xc0\x10':
            assert data==b'\xc0\x10\x04\x01\x00\x00\x00\xc8'
            if write_takes_effect:actual[0]=int.from_bytes(data[4:8],'big')
        else:raise AssertionError('Unexpected device command: '+data.hex())
    def session_factory(**kwargs):
        harness=Harness(respond);harnesses.append(harness)
        return harness.session(**kwargs)
    original=engine_module.ModuleParameterMaintenance
    def create_engine(*args,**kwargs):
        return original(*args,script_session_factory=session_factory,**kwargs)
    monkeypatch.setattr(engine_module,'ModuleParameterMaintenance',create_engine)
    service=setup_service(tmp_path)
    with patch('factory_hmi.gateway.app.PlcCabinetService'):
        app=create_app(service,data_root=tmp_path/'gateway',lease_timeout_s=5)
    hub=app.state.runtime;runtime=hub.session('paired');runtime.lease.claim('paired')
    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://isolated',headers={'X-RP1-Client-ID':'paired'}) as client:
                async def action(name,**body):return await client.post('/api/v1/motor-parameters/'+name,json=body)
                async def wait_for(expected):
                    for _ in range(250):
                        response=await client.get('/api/v1/motor-parameters');assert response.status_code==200,response.text
                        job=response.json()['result']['job']
                        if job['status']==expected and (expected=='awaiting_power' or not job['active']):return job
                        await asyncio.sleep(.005)
                    raise AssertionError(job)
                selections=[dict(index=0,template_id=t['template_id'])]
                response=await action('read',selections=selections);assert response.status_code==200,response.text
                read=await wait_for('checked_with_differences')
                assert sent==['c0110401'] and actual==[100]
                item=read['targets'][0]['items'][0]
                assert item['actual']['raw']==[100] and item['actual']['parameter_number_echoed'] is False
                response=await action('preview',read_job_id=read['job_id']);assert response.status_code==200,response.text
                preview=response.json()['result'];assert preview['targets'][0]['items'][0]['old_raw']==[100]
                response=await action('start',preview_id=preview['preview_id'],confirmed=False);assert response.status_code==409
                assert sent==['c0110401']
                response=await action('start',preview_id=preview['preview_id'],confirmed=True);assert response.status_code==200,response.text
                if write_takes_effect:
                    job=await wait_for('awaiting_power')
                    assert not job['targets'][0]['post_power_verified']
                    response=await action('readback',job_id=job['job_id']);assert response.status_code==409
                    beat=await client.post('/api/v1/lease/heartbeat',headers={'X-RP1-Lease-Generation':str(runtime.lease.snapshot().generation)})
                    assert beat.status_code==200
                    response=await action('acknowledge',job_id=job['job_id'],confirmed=True)
                    assert response.status_code==200 and not response.json()['result']['targets'][0]['post_power_verified']
                    response=await action('readback',job_id=job['job_id']);assert response.status_code==200
                    job=await wait_for('verified')
                    assert job['targets'][0]['post_power_verified'] and job['summary']['written_verified']==1
                    assert sent==['c0110401','c0110401','c0100401000000c8','c0110401','c0110401']
                else:
                    job=await wait_for('failed')
                    assert job['summary']['written_unverified']==1
                    assert not job['targets'][0]['post_power_verified']
                    assert sent==['c0110401','c0110401','c0100401000000c8','c0110401']
                assert all(h.bus.closed for h in harnesses)
                assert service._parameters.pn_quarantine_root==service.report_manager.data_root/'pn-quarantine'
    asyncio.run(run())
    assert service._closed and not service._parameters.active
