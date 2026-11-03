"""Real ASGI routing and fake station services, without sockets or httpx."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factory_hmi.gateway.app import create_app

HMI=Path(__file__).resolve().parents[1]


async def request(app,path,owner,body=None,**extra):
    payload=json.dumps(body or {}).encode()
    headers=[(b'content-type',b'application/json'),(b'x-rp1-client-id',owner.encode())]
    headers.extend((k.replace('_','-').encode(),str(v).encode()) for k,v in extra.items())
    sent=[]
    consumed=False
    async def receive():
        nonlocal consumed
        if consumed:
            await asyncio.Future()
        consumed=True
        return {'type':'http.request','body':payload,'more_body':False}
    async def send(message):sent.append(message)
    scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1',
           'method':'POST','scheme':'http','path':path,'raw_path':path.encode(),
           'query_string':b'','headers':headers,'client':('127.0.0.1',1),'server':('test',80)}
    # In the packaged, socketless test runtime cross-thread callbacks do not
    # wake selector polling on their own. This harness pulse exercises ASGI
    # routing, not native server/loop wakeup or TCP performance.
    async def pulse():
        while True: await asyncio.sleep(.01)
    clock=asyncio.create_task(pulse())
    try:
        await asyncio.wait_for(app(scope,receive,send),3)
    finally:
        clock.cancel()
        try: await clock
        except asyncio.CancelledError: pass
    status=next(s['status'] for s in sent if s['type']=='http.response.start')
    result=json.loads(b''.join(s.get('body',b'') for s in sent if s['type']=='http.response.body'))
    return status,result


def configuration(interface):
    return dict(config_path=str(HMI/'scripts/config/arm_motors.yaml'),limb='left_arm',backend='fake',
                bus_bindings=[dict(index=0,interface=interface,mode='canfd',bitrate=1_000_000,dbitrate=5_000_000)])


class GatewayLeaseAsgiTests(unittest.TestCase):
    def test_expired_generation_cannot_renew_until_explicit_reconfigure(self):
        async def scenario(root):
            app=create_app(data_root=root,lease_timeout_s=.08)
            async with app.router.lifespan_context(app):
                status,configured=await request(app,'/api/v1/configure','a',configuration('can0'))
                self.assertEqual(status,200,configured)
                generation=configured['snapshot']['lease']['generation']
                status,_=await request(app,'/api/v1/lease/heartbeat','a',x_rp1_lease_generation=generation)
                self.assertEqual(status,200)
                status,_=await request(app,'/api/v1/lease/heartbeat','a')
                self.assertEqual(status,409)
                await asyncio.sleep(.25)
                self.assertNotIn('can0',app.state.runtime.interface_owners())
                status,_=await request(app,'/api/v1/lease/heartbeat','a',x_rp1_lease_generation=generation)
                self.assertEqual(status,409)
                status,_=await request(app,'/api/v1/connect','a')
                self.assertEqual(status,409)
                status,configured=await request(app,'/api/v1/configure','a',configuration('can0'))
                self.assertEqual(status,200,configured)
                new_generation=configured['snapshot']['lease']['generation']
                self.assertGreater(new_generation,generation)
                status,_=await request(app,'/api/v1/lease/heartbeat','a',x_rp1_lease_generation=generation)
                self.assertEqual(status,409)
                status,_=await request(app,'/api/v1/lease/heartbeat','a',x_rp1_lease_generation=new_generation)
                self.assertEqual(status,200)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{
            'RP1_FACTORY_OPERATOR_TOKEN':'','RP1_FACTORY_ALLOWED_CONFIG_ROOT':str(HMI/'scripts/config'),
            'RP1_FACTORY_PLATFORM_URL':''}), patch('factory_hmi.gateway.app.PlcCabinetService') as plc:
            plc.return_value.snapshot.return_value={}
            asyncio.run(scenario(Path(temp)))

    def test_cleanup_failure_prevents_new_owner_until_recovery(self):
        async def scenario(root):
            app=create_app(data_root=root,lease_timeout_s=.08)
            async with app.router.lifespan_context(app):
                status,result=await request(app,'/api/v1/configure','a',configuration('can0'))
                self.assertEqual(status,200,result)
                runtime=app.state.runtime.session('a')
                with patch.object(runtime.controller,'can_disconnect',side_effect=OSError('synthetic CAN close failure')):
                    await asyncio.sleep(.25)
                    self.assertEqual(app.state.runtime.interface_owners().get('can0'),'a')
                    self.assertTrue(runtime.lease_snapshot()['quarantined'])
                    status,_=await request(app,'/api/v1/configure','b',configuration('can0'))
                    self.assertEqual(status,409)
                    status,_=await request(app,'/api/v1/lease/recover','a')
                    self.assertEqual(status,409)
                status,result=await request(app,'/api/v1/lease/recover','a')
                self.assertEqual(status,200,result)
                self.assertNotIn('can0',app.state.runtime.interface_owners())
                status,result=await request(app,'/api/v1/configure','b',configuration('can0'))
                self.assertEqual(status,200,result)
                self.assertEqual(app.state.runtime.interface_owners().get('can0'),'b')
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{
            'RP1_FACTORY_OPERATOR_TOKEN':'','RP1_FACTORY_ALLOWED_CONFIG_ROOT':str(HMI/'scripts/config'),
            'RP1_FACTORY_PLATFORM_URL':''}), patch('factory_hmi.gateway.app.PlcCabinetService') as plc:
            plc.return_value.snapshot.return_value={}
            asyncio.run(scenario(Path(temp)))


if __name__=='__main__':unittest.main()
