"""Offscreen editor/callback checks; every HTTP and modal action is mocked."""
from copy import deepcopy
from unittest.mock import patch

from PySide6.QtWidgets import QDialog, QLabel, QMessageBox, QPushButton, QComboBox, QTextEdit

from factory_hmi.desktop.local_correction_dialog import LocalCorrectionDialog
from factory_hmi.core.local_corrections import FIELDS
from test_execution_history_desktop import app, window


def state():
    values = dict(sample_id='old', operator_id='tester', bench_id='A01', test_name='name', actual_object='object', notes='')
    return dict(test_id='test', revision=0, original=deepcopy(values), values=values, records=[], local_only=True)


def test_editor_has_grouped_business_fields_and_requires_reason(app):
    d = LocalCorrectionDialog(state())
    try:
        assert set(d.editors) == set(FIELDS)
        d.editors['sample_id'].setText('000123')
        with patch.object(QMessageBox, 'warning') as warning:
            d.save(); assert warning.called and d.payload is None
        d.reason.setPlainText('录入错误')
        d.save()
        assert d.result() == QDialog.DialogCode.Accepted
        assert d.payload['changes'] == {'sample_id':'000123'}
        assert d.payload['expected_revision'] == 0 and len(d.payload['request_id']) == 32
    finally:
        d.close()


def test_local_button_available_for_approved_record_and_marker_preserves_sync(window):
    correction = dict(revision=2, original={'sample_id':'old'}, actor='local', reason='why', recorded_at='time')
    window._execution_history_received({'items':[dict(test_id='test', sample_id='000123', verdict='failed', sync_state='approved', local_correction=correction)]})
    window.execution_history_table.selectRow(0); window._update_execution_actions()
    assert window.local_correction_button.isEnabled() and not window.submit_review_button.isEnabled()
    assert window.execution_history_table.item(0,3).text() == '已批准'
    assert window.execution_history_table.item(0,2).text() == 'failed'
    assert window.execution_history_table.item(0,7).text() == '本地已更正 · v2'
    assert 'old' in window.execution_history_table.item(0,1).toolTip()


def test_nested_get_save_callbacks_keep_pending_and_only_call_local_save(window):
    window._execution_history_received({'items':[dict(test_id='test', sample_id='old')]})
    window.execution_history_table.selectRow(0)
    with patch.object(window.client,'save_local_correction') as save, patch.object(window.client,'submit_execution') as submit:
        window._correct_local_execution(); load = window._run_rest.call_args
        def edit(dialog):
            dialog.editors['sample_id'].setText('000123'); dialog.reason.setPlainText('录入错误'); dialog.save()
            return dialog.result()
        with patch.object(LocalCorrectionDialog,'exec', edit):
            load.args[2]({'result':state()})
        saved = window._run_rest.call_args
        assert saved is not load
        load.kwargs['on_finished']()
        assert window._local_correction_pending and not window.local_correction_button.isEnabled()
        saved.args[1](); save.assert_called_once()
        assert save.call_args.args[0] == 'test'
        assert save.call_args.args[1]['changes'] == {'sample_id':'000123'}
        submit.assert_not_called()
        saved.kwargs['on_finished']()
        assert not window._local_correction_pending


def test_cancel_and_old_account_callback_do_not_save(window):
    window._execution_history_received({'items':[dict(test_id='test', sample_id='old')]})
    window.execution_history_table.selectRow(0)
    with patch.object(window.client,'save_local_correction') as save, patch.object(LocalCorrectionDialog,'exec',return_value=QDialog.DialogCode.Rejected):
        window._correct_local_execution(); load = window._run_rest.call_args
        load.args[2]({'result':state()}); load.kwargs['on_finished'](); save.assert_not_called()
    window._correct_local_execution(); old = window._run_rest.call_args
    window._update_generation += 1
    with patch.object(LocalCorrectionDialog,'exec') as editor:
        old.args[2]({'result':state()}); old.kwargs['on_finished'](); editor.assert_not_called()


def test_later_submission_shows_original_sn_and_rechecks_changed_bench(window):
    correction = dict(revision=1, original={'sample_id':'original-SN', 'bench_id':'A01'}, actor='local', reason='why', recorded_at='time')
    window._execution_history_received({'items':[dict(test_id='test', sample_id='local-SN', bench_id='B02', station_id='old-station', notes='local-notes', local_correction=correction)]})
    window._platform_stations = [dict(id='old-station',code='A01'), dict(id='new-station',code='B02')]
    seen = []
    def inspect(dialog):
        labels = '\n'.join(w.text() for w in dialog.findChildren(QLabel))
        assert 'original-SN' in labels and 'local-SN' in labels and '归档仍使用原始SN' in labels
        assert any(w.text() == '确认按原始SN关联提交' for w in dialog.findChildren(QPushButton))
        assert any(w.currentData() == 'new-station' for w in dialog.findChildren(QComboBox))
        assert any(w.toPlainText() == 'local-notes' for w in dialog.findChildren(QTextEdit))
        seen.append(True)
        return QDialog.DialogCode.Rejected
    with patch('factory_hmi.desktop.app.measured_lines',return_value='facts'), patch('factory_hmi.desktop.app.analysis_text',return_value='analysis'), patch.object(QDialog,'exec',inspect), patch.object(window.client,'submit_execution') as submit:
        window._open_submission_review('test',{'timing':{}})
        assert seen and not submit.called


def test_grouped_editor_scrolls_and_edits_typed_business_fields(app):
    from PySide6.QtWidgets import QScrollArea, QTabWidget
    d = LocalCorrectionDialog(state())
    try:
        tabs = d.findChild(QTabWidget, 'local_correction_tabs')
        assert tabs.count() == 4
        assert all(isinstance(tabs.widget(i), QScrollArea) and tabs.widget(i).widgetResizable() for i in range(3))
        assert not {'test_id', 'execution_uuid', 'sync_state', 'submission_id'} & set(d.editors)
        d.editors['display_test_code'].setText('LOCAL-DISPLAY')
        d.editors['verdict'].setCurrentIndex(d.editors['verdict'].findData('passed'))
        d.editors['completed_cycles'].setText('0')
        d.editors['data_domain'].setCurrentIndex(d.editors['data_domain'].findData('FACTORY'))
        d.reason.setPlainText('本地复核录入')
        d.save()
        assert d.payload['changes'] == dict(display_test_code='LOCAL-DISPLAY', verdict='passed', completed_cycles='0', data_domain='FACTORY')
    finally: d.close()


def test_display_alias_does_not_replace_selected_internal_record_id(window):
    window._execution_history_received({'items':[dict(test_id='real-id', execution_uuid='real-uuid',
        execution_code='PLATFORM-OFFICIAL', display_test_code='LOCAL-ALIAS', sample_id='SN', verdict='passed')]})
    window.execution_history_table.selectRow(0)
    assert window.execution_history_table.item(0,0).text() == 'LOCAL-ALIAS'
    assert window._selected_test_id() == 'real-id'
    assert 'PLATFORM' not in window._selected_test_id()
