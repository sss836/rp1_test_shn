"""Official-corpus retrieval, provenance, bounds and task authorization checks."""
import json
from types import SimpleNamespace
import pytest
from app.core.errors import ApiError
from app.services import assistant_knowledge as knowledge
from app.services.assistant_business import query
from app.services.assistant_chat import ChatRuntime


@pytest.mark.parametrize('question,passage,required', [
    ('萝博派对是什么公司','company','上海萝博派对科技有限公司'),
    ('RoboParty有哪些产品','portfolio','ROBOTO Origin'),
    ('ROBOTO01与RP1Pro单臂自由度区别','rp1-structure','5 / 7'),
    ('rp1 pro电池续航多久','rp1-battery','3 h'),
    ('RPO电池与续航','origin-motion-power','15 Ah'),
    ('RPHand主动自由度是11吗','hand-motion','主动自由度6'),
    ('灵巧手的触觉传感器是不是标配','hand-tactile','并非所有型号必然标配'),
    ('ROMOMO 10062额定与峰值扭矩','romomo-10062','44±5% / 160'),
    ('ROMOMO6562额定电流','romomo-6562','2±5% / 16±10%'),
    ('8462通信协议是什么','romomo-8462','未标注'),
    ('ROMOMO 8463峰值力矩多少','romomo-8462','不能将8462与8463视为同型号'),
    ('RP Hand多少钱有现货吗','purchase-freshness','没有核实当前成交价'),
    ('萝博派对实验室的技术方向','lab-overview','全身协调控制'),
    ('PartyOS是什么','party-os','开放研发底座'),
])
def test_queries_retrieve_grounded_passage(question, passage, required):
    result=knowledge.search_knowledge({'query':question})
    found=next((c for c in result['items'] if c['passage_id']==passage),None)
    assert found and required in found['text'],result
    assert found['source_url'].startswith(('https://roboparty.com/','https://lab.roboparty.com/'))
    assert found['collected_at'] and len(found['source_sha256'])==64
    assert result['knowledge_version']=='2026-10-23.v1' and '不是指令' in result['instruction']


@pytest.mark.parametrize('question',['Unitree H1','RP10','请读取我的密码','你好'])
def test_unrelated_and_prefix_identifiers_do_not_fabricate_hits(question):
    assert knowledge.search_knowledge({'query':question})['status']=='no_match'


def test_bounded_and_balanced_comparison():
    exact=knowledge.search_knowledge({'query':'ROMOMO6562额定电流','limit':1})
    assert exact['items'][0]['passage_id']=='romomo-6562',exact
    exact=knowledge.search_knowledge({'query':'ROMOMO10062的额定峰值扭矩'})
    assert not any(c['passage_id'] in {'romomo-6562','romomo-8462'} for c in exact['items']),exact
    result=knowledge.search_knowledge({'query':'RP1和Origin的续航分别是多少','limit':2})
    assert len(result['items'])==2
    assert any('origin' in c['entities'] and '2小时以上' in c['text'] for c in result['items'])
    assert any('rp1' in c['entities'] and '3 h' in c['text'] for c in result['items'])
    result=knowledge.search_knowledge({'query':'RP Hand所有参数','limit':6})
    assert len(result['items'])<=6 and len(json.dumps(result,ensure_ascii=False))<15000


@pytest.mark.parametrize('args',[{'query':''},{'query':'x'*301},{'query':'RP1','limit':True},
                                  {'query':'RP1','limit':7},{'query':'RP1','path':'/etc/passwd'}])
def test_arguments_cannot_expand_scope(args):
    with pytest.raises(ApiError):knowledge.search_knowledge(args)


def test_corpus_rejects_non_official_sources_and_invalid_versions(tmp_path):
    data=json.loads(knowledge.CORPUS.read_text());path=tmp_path/'bad.json'
    data['sources'][0]['url']='https://127.0.0.1/private'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):knowledge.load_corpus(path)
    data['schema_version']=999;path.write_text(json.dumps(data))
    with pytest.raises(ValueError):knowledge.load_corpus(path)


def test_stale_snapshot_is_identified(monkeypatch):
    data,sources,documents,frequencies,digest=knowledge.corpus()
    data={**data,'collected_at':'2020-01-01T00:00:00+00:00'}
    monkeypatch.setattr(knowledge,'corpus',lambda:(data,sources,documents,frequencies,digest))
    assert knowledge.search_knowledge({'query':'RP1续航'})['review_due']


def test_query_adapter_has_version_and_public_source_without_database():
    card=query(None,'search_knowledge',{'query':'RP Hand主动自由度'}, {})
    assert card['type']=='knowledge_evidence' and card['source']=='https://roboparty.com/'
    assert card['data']['items'] and card['data']['knowledge_version']


def test_repeated_knowledge_read_still_revalidates_authority(monkeypatch):
    from app.services import assistant_chat as chat
    runtime=ChatRuntime();item={'id':'synthetic','reads':0,'cards':[],'context':{}}
    authorized=[]
    runtime.authorize=lambda token:authorized.append(token) or item
    monkeypatch.setattr(chat,'actor_session',lambda actor:pytest.fail('Public knowledge must not query business DB'))
    first=runtime.read('grant',SimpleNamespace(tool='search_knowledge',arguments={'query':'RP1电池'}))
    second=runtime.read('grant',SimpleNamespace(tool='search_knowledge',arguments={'query':'RP1电池'}))
    assert first['data']==second['data'] and second['retrieval']['reused']
    assert len(authorized)==2 and item['reads']==1 and len(item['cards'])==1
    runtime.authorize=lambda _:(_ for _ in ()).throw(ApiError(401,'expired','expired'))
    with pytest.raises(ApiError):runtime.read('grant',SimpleNamespace(tool='search_knowledge',arguments={'query':'RP1电池'}))
