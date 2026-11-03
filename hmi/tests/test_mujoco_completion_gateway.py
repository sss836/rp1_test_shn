"""Transport checks with a stub controller and an explicitly mock PLC fixture."""
from unittest.mock import patch
from fastapi.testclient import TestClient
from factory_hmi.gateway.app import create_app
from factory_hmi.gateway.quick_run import QuickRun


class Controller:
    def snapshot(self): return {'state':'armed','motors':[]}
    def start(self,**kwargs): return kwargs
    def stop(self,**kwargs): pass
    def disconnect(self): pass
    def request_motion_stop(self): pass


def test_both_routes_forward_selection_and_reject_missing_confirmation(tmp_path):
    controller=Controller()
    with TestClient(create_app(controller,data_root=tmp_path)) as client:
        runtime=client.app.state.runtime.session('isolated-test')
        runtime.lease.claim('isolated-test')
        headers={'X-RP1-Client-ID':'isolated-test'}
        normal={'test_case_id':'REL-ARM-001','completion_mode':'mujoco_hanging','parking_pose_confirmed':True}
        with patch.object(controller,'start',wraps=controller.start) as start:
            response=client.post('/api/v1/playback/start',json=normal,headers=headers)
            assert response.status_code==200,response.text
            assert start.call_args.kwargs['completion_mode']=='mujoco_hanging'
            assert start.call_args.kwargs['parking_pose_confirmed'] is True
            start.reset_mock()
            response=client.post('/api/v1/playback/start',json={**normal,'parking_pose_confirmed':False},headers=headers)
            assert response.status_code==422
            start.assert_not_called()
        quick={'duration_hours':1,'sample_id':'TEST','physical_estop_confirmed':True,
               'power_source':'external','completion_mode':'mujoco_hanging','parking_pose_confirmed':True}
        with patch.object(QuickRun,'start',return_value={'phase':'running'}) as start:
            response=client.post('/api/v1/quick/start',json=quick,headers=headers)
            assert response.status_code==200,response.text
            assert start.call_args.kwargs['completion_mode']=='mujoco_hanging'
            assert start.call_args.kwargs['parking_pose_confirmed'] is True
            start.reset_mock()
            response=client.post('/api/v1/quick/start',json={**quick,'parking_pose_confirmed':False},headers=headers)
            assert response.status_code==422
            start.assert_not_called()
