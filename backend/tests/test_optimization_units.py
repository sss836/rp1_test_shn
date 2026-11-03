import pytest
from app.services.assistant_presentation import presentation
from app.services.query_budget import query_slot
from app.core.errors import ApiError
@pytest.mark.parametrize('state,completion,outcome,expected',[
 ('COMPLETED','answered',None,'answered'),('FAILED','failed',None,'failed'),
 ('TIMED_OUT','failed',None,'unknown'),('WAITING_PERMISSION','awaiting_permission',None,'awaiting_permission'),
 ('CANCELLED','failed',None,'cancelled'),('COMPLETED','partial',{'receipt':{'id':'actual'}},'partial'),
 ('COMPLETED','answered',{'status':'needs_adoption'},'awaiting_confirmation'),
 ('COMPLETED','answered',{'receipt':{'id':'actual'}},'completed')])
def test_result_state_tracks_receipts_and_partial_work(state,completion,outcome,expected):
 result=presentation('已有结论',state=state,completion=completion,outcome=outcome,cards=[],termination='test')
 assert result['status']==expected
 if expected in {'partial','unknown','failed','awaiting_permission'}:assert result['unknown']
def test_query_budget_releases_after_errors():
 with query_slot(),query_slot():
  with pytest.raises(ApiError) as error:
   with query_slot():pass
  assert error.value.status_code==429
 with query_slot():pass
