"""Original natural-language question, official model and synthetic QA accounts."""
import os
import pytest
from tests.test_assistant_integration import environment,signed_in
from tests.test_assistant_engineering_integration import ask,reset_finance

pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.getenv('ASSISTANT_QA_LIVE'),reason='official model isolated check only')]


def test_original_case_availability_question_produces_grounded_explanation(environment):
    _,users=environment
    with signed_in(users['first']) as client:
        job=ask(client,'为什么我的上位机没有upper_body的可选用例编号')
        assert job['state']=='COMPLETED',job
        result=job['result'];assert result['completion'] in {'answered','partial'}
        assert any(c['type']=='business_search' and c['data'].get('applied_filters',{}).get('target_part_code')=='UPPER' for c in result['cards']),job
        assert any(c['type']=='source_evidence' and c['data'].get('path') for c in result['cards']),job
        assert 'UPPER' in result['reply'] and any(s in result['reply'] for s in ['关联','上下文','profiles','计划']),job
        assert '模型链路中断' not in result['reply']
