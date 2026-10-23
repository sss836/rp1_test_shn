"""Real API→Pi→model→knowledge→answer path on isolated synthetic accounts."""
import os
import re
import httpx
import pytest
from tests.test_assistant_integration import environment,signed_in
from tests.test_assistant_engineering_integration import ask,reset_finance

pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.getenv('ASSISTANT_QA_ADMIN_URL'),reason='isolated stack only')]


@pytest.mark.skipif(bool(os.getenv('ASSISTANT_QA_LIVE')),reason='synthetic model callback check')
def test_synthetic_knowledge_tool_survives_http_and_receipt_persistence(environment):
    _,users=environment
    httpx.post('http://model-mock:3100/control',json={'mode':'normal','tool':'search_knowledge','arguments':{'query':'RP Hand主动自由度'}},trust_env=False).raise_for_status()
    with signed_in(users['first']) as client:
        job=ask(client,'请查询萝博派对RP Hand主动自由度')
        assert job['state']=='COMPLETED',job
        cards=[c for c in job['result']['cards'] if c['type']=='knowledge_evidence']
        assert cards and any('主动自由度6' in row['text'] for row in cards[0]['data']['items']),job
        assert cards[0]['data']['knowledge_version']=='2026-10-23.v1'


@pytest.mark.skipif(not os.getenv('ASSISTANT_QA_LIVE'),reason='official DeepSeek isolated check')
@pytest.mark.parametrize('question,expected',[
    ('萝博派对有哪些产品？请按官网知识库介绍，附来源和采集日期。',['ROBOTO','Origin','ROMOMO']),
    ('RP1和RP1 Pro的主动自由度、单臂自由度分别是多少？',['24','30','5','7']),
    ('RP Hand是不是11个主动自由度？',['11','6']),
    ('ROMOMO 10062的额定和峰值扭矩分别多少，160能持续输出吗？',['44','160']),
    ('ROMOMO8462的通信协议和额定电流是多少？不明确就说明。',['8462','未']),
])
def test_official_model_answers_with_local_provenance(environment,question,expected):
    _,users=environment
    with signed_in(users['first']) as client:
        job=ask(client,question)
        assert job['state']=='COMPLETED',job
        result=job['result'];reply=result['reply']
        cards=[c for c in result['cards'] if c['type']=='knowledge_evidence']
        assert cards and all(value in reply for value in expected),job
        assert 'https://roboparty.com/' in reply and '2026-10-23' in reply,job
        assert result['business_status']=='answered' and not result.get('setup'),job
        if '8462' in question:assert any(word in reply for word in ['未标注','未提供','未公开','未明确','未收录']),job
        if '10062' in question:
            assert any(word in reply for word in ['短时','持续','额定']),job
            assert set(re.findall(r'(?<![\d.])(\d+(?:\.\d+)?)\s*A\b',reply)) <= {'11','55'},job
            assert not any(word in reply for word in ['没有出现在当前归档','本平台没有10062']),job
