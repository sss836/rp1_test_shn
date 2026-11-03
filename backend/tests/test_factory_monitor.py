import pytest
from app.repositories.factory_monitor import receipt_state

@pytest.mark.parametrize(('value','expected'), [
 ({'upload_state':'CORRUPT'},'upload_error'),
 ({'upload_state':'UPLOADING'},'uploading'),
 ({'upload_state':'COMPLETE','processing_state':'FAILED'},'parse_error'),
 ({'upload_state':'COMPLETE','processing_state':'PROCESSING'},'parsing'),
 ({'upload_state':'COMPLETE','processing_state':'READY','submission_status':'APPROVED'},'archived'),
 ({'upload_state':'COMPLETE','processing_state':'READY','submission_status':'PENDING'},'awaiting_review'),
 ({'upload_state':'COMPLETE','processing_state':'READY','submission_status':'REJECTED'},'rejected'),
 ({'upload_state':'COMPLETE','processing_state':'READY','verified':False},'awaiting_verification'),
 ({'upload_state':'COMPLETE','processing_state':'READY','verified':True,'classified':False},'awaiting_context'),
 ({'upload_state':'COMPLETE','processing_state':'READY','verified':True,'classified':True},'ready_to_submit'),
])
def test_work_receipt_is_not_automatic_approval(value,expected):
 assert receipt_state(value)==expected
