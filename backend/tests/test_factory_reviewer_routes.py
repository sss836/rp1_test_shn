from app.core.auth import factory_route_allowed
import pytest

@pytest.mark.parametrize('method,path',[
 ('GET','/api/v1/factory/monitor'),('GET','/api/v1/factory/statistics'),
 ('GET','/api/v1/archives'),('GET','/api/v1/archives/known-record/curves'),
 ('GET','/api/v1/archive-ledger.csv'),('POST','/api/v1/archives/record/factory-review'),
 ('POST','/api/v1/auth/change-password'),('GET','/api/v1/auth/me'),
])
def test_scoped_reader_and_reviewer_paths(method,path):
 assert factory_route_allowed(method,path)

@pytest.mark.parametrize('method,path',[
 ('POST','/api/v1/archives'),('PUT','/api/v1/archives/known-record/content'),
 ('POST','/api/v1/archives/record/domain-classification'),('POST','/api/v1/archives/record/submit'),
 ('PUT','/api/v1/hmi/presence/record'),('DELETE','/api/v1/hmi/presence/record'),
 ('GET','/api/v1/hmi/stations'),('GET','/api/v1/admin/users'),('POST','/api/v1/setup/stations'),
 ('GET','/api/v1/assets'),('GET','/api/v1/dashboard/home'),
 ('POST','/api/v1/factory/station-scope'),('POST','/api/v1/archives/record/factory-review/extra'),
])
def test_factory_account_cannot_gain_operator_or_research_scope(method,path):
 assert not factory_route_allowed(method,path)
