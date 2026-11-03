"""Offscreen controls only; all REST/network setup is mocked by the fixture."""
from unittest.mock import patch

from test_execution_history_desktop import app, window


def test_speed_defaults_and_initial_approach_preview(window):
    assert window.speed.minimum()==.1 and window.speed.maximum()==5.
    assert window.speed.value()==1.
    payload={'snapshot':{'trajectory':{'preflight':{'approach':{
        'required':True,'max_distance_rad':1.,'duration_s':10.75}}}}}
    with patch.object(window,'_render_snapshot'):
        window._armed_ok(payload)
    text=window.preflight_result.text()
    assert '57.3°' in text and '10.8秒' in text and '确认到位后开始测试计时' in text
    assert not window._preflight_pending
