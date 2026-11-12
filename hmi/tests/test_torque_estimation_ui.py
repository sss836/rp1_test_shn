from test_motor_version_monitor_ui import window, render, read, motor
from PySide6.QtCore import Qt


def test_monitor_labels_estimated_value_and_shows_actual_coefficient(window):
    sample={**motor('can0',1,0),'torque_nm':-6.16,'torque_is_estimated':True,'torque_kt_nm_a':3.08,'iq_current_a':-2.,'lro_protocol_profile':'v1.03'}
    render(window,[sample],[])
    assert read(window,0,'力矩(N·m)')=='-6.160（估算）'
    assert read(window,0,'实测Kt(N·m/A)')=='3.080'
    assert read(window,0,'Iq 电流(A)')=='-2.000'
    window._render_aging_status({'playback':{'statistics':{'joints':[{'torque_peak_nm':6.16,'torque_is_estimated':True}]}}})
    assert window.aging_statistics_model.index(0,4).data(Qt.DisplayRole)=='6.160（估算）'


def test_missing_kt_keeps_iq_visible_and_explains_reason(window):
    render(window,[{**motor('can0',1,0),'torque_nm':None,'torque_is_estimated':True,'torque_source':'Kt读取超时','iq_current_a':2.,'lro_protocol_profile':'v1.03'}],[])
    assert 'Kt读取超时' in read(window,0,'力矩(N·m)')
    assert read(window,0,'Iq 电流(A)')=='2.000'


def test_108_monitor_unchanged(window):
    render(window,[{**motor('can0',1,0),'torque_nm':3.,'torque_is_estimated':False,'lro_protocol_profile':'legacy'}],[])
    assert read(window,0,'力矩(N·m)')=='3.000'
