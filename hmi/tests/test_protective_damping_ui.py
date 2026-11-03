"""Offscreen UI, all actions mocked: no device/gateway connections."""
from unittest.mock import Mock,patch
from PySide6.QtWidgets import QMessageBox
from test_sample_selector import application,window


def protected(window,revoked=True):
    window._last_snapshot={'station':{'protective_damping':{'active':True,'kd':1.,'safe_confirmed':False}},'lease':{'revoked':revoked}}


def test_closing_protective_mode_keeps_heartbeat_and_window_alive(window):
    protected(window);event=Mock()
    with patch.object(window._lease_heartbeat,'stop') as stop,patch.object(window.diagnostics_page,'stop') as diagnostics,patch('factory_hmi.desktop.app.QMessageBox.warning') as warning:
        window.closeEvent(event)
    event.ignore.assert_called_once();stop.assert_not_called();diagnostics.assert_not_called()
    assert '可靠支撑' in warning.call_args.args[2]


def test_declined_disable_or_disconnect_keeps_damping(window):
    protected(window)
    with patch('factory_hmi.desktop.app.QMessageBox.warning',return_value=QMessageBox.StandardButton.Cancel),patch.object(window,'_run_rest') as run:
        window._software_disable();window._disconnect();run.assert_not_called()


def test_support_ack_uses_revoked_owner_release_only(window):
    protected(window)
    with patch('factory_hmi.desktop.app.QMessageBox.warning',return_value=QMessageBox.StandardButton.Yes),patch.object(window,'_run_rest') as run,patch.object(window.client,'disable_protective_stop') as release,patch.object(window.client,'disable_motors') as ordinary:
        window._software_disable();run.call_args.args[1]()
        release.assert_called_once_with(mechanical_support_confirmed=True);ordinary.assert_not_called()


def test_support_ack_for_active_owner_uses_normal_authorized_disable(window):
    protected(window,False)
    with patch('factory_hmi.desktop.app.QMessageBox.warning',return_value=QMessageBox.StandardButton.Yes),patch.object(window,'_run_rest') as run,patch.object(window.client,'disable_protective_stop') as release,patch.object(window.client,'disable_motors') as ordinary:
        window._software_disable();run.call_args.args[1]()
        ordinary.assert_called_once();release.assert_not_called()
