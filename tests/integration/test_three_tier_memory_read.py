"""Same-ref/source acceptance: real services on isolated synthetic data, no JEV calls."""
import json
import pytest
from fastapi.testclient import TestClient
from mariposa.app import app
from mariposa import db, config
from mariposa.capabilities import registry
from mariposa.memory import detail
from mariposa.source import binding
from tests.conftest import TOKENS
from tests.unit.test_memory_detail import _seed, _receipt_with_detail, LONG_TEXT


def rpc(client, section, cursor=None):
    args={'detail_ref':'dref_test','section':section}
    if cursor: args['cursor']=cursor
    response=client.post('/mcp',headers={'Authorization':'Bearer '+TOKENS['jiaming']},json={
        'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'mariposa_memory_detail_read','arguments':args}})
    result=response.json()['result']
    return result, json.loads(result['content'][0]['text'])


def test_source_can_be_first_call_and_uses_existing_bucket_source_service(actors,tmp_path,monkeypatch):
    mid,_=_seed(tmp_path,actors);_receipt_with_detail(mid,LONG_TEXT)
    expected=binding.open_for_memory(mid)
    calls=[];original=binding.open_for_memory
    def tracked(memory_id):
        calls.append(memory_id);return original(memory_id)
    monkeypatch.setattr(binding,'open_for_memory',tracked)
    monkeypatch.setattr(detail,'build_sections',lambda *a:pytest.fail('must not read ordinary/full details first'))
    monkeypatch.setattr(detail.mem_service,'get',lambda *a:pytest.fail('source must not load ordinary bucket body'))
    with TestClient(app) as client:
        result,out=rpc(client,'source')
    assert result['isError'] is False
    data=out['data'];assert data['memory_id']==mid and calls==[mid]
    assert [m['text'] for m in data['source']]==[m['text'] for r in expected['ranges'] for m in r['messages']]
    assert [m['message_id'] for m in data['source']]==[m['message_id'] for r in expected['ranges'] for m in r['messages']]
    assert not {'text','mood','our_words','relations'} & data.keys()
    assert 'binding_id' not in json.dumps(data)


def test_plain_bucket_only_has_second_layer_and_native_id_still_opens_source(actors,tmp_path):
    mid,_=_seed(tmp_path,actors);_receipt_with_detail(mid,LONG_TEXT)
    args={'detail_ref':'dref_test','section':'memory','view':'model'};words=[];text=''
    for _ in range(100):
        page=registry.invoke(actors['jiaming'],'memory.detail.read',args,None)['data']
        text+=page.get('text','');words.extend(page.get('our_words',[]))
        assert not {'source','relations','keeps','recollections'} & page.keys()
        if not page.get('next_cursor'):break
        args['cursor']=page['next_cursor']
    else:pytest.fail('pagination did not terminate')
    assert text==LONG_TEXT and words
    assert all(set(w)=={'word_id','speaker','text'} for w in words)
    original=registry.invoke(actors['jiaming'],'source.memory.open',{'memory_id':page['memory_id']},None)['data']
    assert [m['text'] for r in original['ranges'] for m in r['messages']]==[f'原文消息{i}' for i in range(4)]
    # Correction state is fetched through the existing explicit word lookup.
    word=registry.invoke(actors['jiaming'],'memory.words.get',{'word_id':words[0]['word_id']},None)['data']
    assert word['source_binding_version']==0


def test_relations_direct_read_includes_direction_and_related_body(actors,tmp_path,monkeypatch):
    mid,other=_seed(tmp_path,actors);_receipt_with_detail(mid,LONG_TEXT)
    monkeypatch.setattr(binding,'open_for_memory',lambda *a:pytest.fail('relations must not read sources'))
    with TestClient(app) as client:result,payload=rpc(client,'relations')
    assert not result['isError'];page=payload['data']
    assert 'text' not in page and 'source' not in page
    edge=next(x for x in page['relations'] if x.get('target',{}).get('memory_id')==other)
    assert edge['text']=='另一件事' and edge['direction'] in ('in','out') and edge['relation_type']


def test_source_pages_preserve_long_original_and_reject_cross_section_cursor(actors,tmp_path,monkeypatch):
    mid,_=_seed(tmp_path,actors);_receipt_with_detail(mid,LONG_TEXT)
    long='原文🙂\n！！'*2000
    with db.formal() as conn:conn.execute("UPDATE source_messages SET text=? WHERE provider_message_id='dm0'",(long,))
    monkeypatch.setattr(config,'SURFACE_DETAIL_PAGE_BYTES',2048)
    full=[];cursor=None
    with TestClient(app) as client:
        for _ in range(100):
            result,body=rpc(client,'source',cursor);assert not result['isError']
            full.extend(body['data'].get('source',[]));cursor=body['data'].get('next_cursor')
            if not cursor:break
            assert cursor.startswith('source:p')
            wrong,_=rpc(client,'relations',cursor);assert wrong['isError']
        else:pytest.fail('pagination did not terminate')
    first_id=full[0]['message_id']
    assert ''.join(x.get('text','') for x in full if x.get('message_id')==first_id)==long


def test_source_lookup_and_stale_correction_version_are_still_available(actors,tmp_path):
    from mariposa.memory import our_words
    from mariposa.capabilities.model_output import project
    from mariposa.errors import MariposaError
    mid,_=_seed(tmp_path,actors);_receipt_with_detail(mid,LONG_TEXT)
    with db.formal() as conn:
        sid=conn.execute("SELECT id FROM source_messages WHERE provider_message_id='dm0'").fetchone()['id']
    held=our_words.append('jiaming',mid,[{'speaker':'qiaosheng','text':'原文消息0','source_ref':'source_msg:'+sid}])
    wid=held['word_ids'][0]
    word=registry.invoke(actors['jiaming'],'memory.words.get',{'word_id':wid},None)['data']
    correction=project('memory.words.get',word,{'word_id':wid})
    assert correction['source_ref']=='source_msg:'+sid and correction['source_binding_version']==0
    ordinary=project('memory.our_words.list',{'words':[word]}, {})
    assert 'source_ref' not in ordinary['words'][0] and 'source_binding_version' not in ordinary['words'][0]
    args={'word_id':wid,'correction_action':'remove_wrong_binding','expected_source_version':0,'expected_source_ref':correction['source_ref'],'operation_id':'source-correct-once'}
    registry.invoke(actors['jiaming'],'memory.our_words.source.correct',args,None)
    with pytest.raises(MariposaError):
        registry.invoke(actors['jiaming'],'memory.our_words.source.correct',{**args,'operation_id':'source-correct-stale'},None)


def test_empty_and_failed_sources_remain_distinct(actors,tmp_path,monkeypatch):
    mid,_=_seed(tmp_path,actors);_receipt_with_detail(mid,LONG_TEXT)
    monkeypatch.setattr(binding,'open_for_memory',lambda memory_id:{'memory_id':memory_id,'ranges':[]})
    with TestClient(app) as client:
        result,out=rpc(client,'source');assert not result['isError']
        assert out['data']['source']==[] and 'incomplete_sections' not in out['data']
    monkeypatch.setattr(binding,'open_for_memory',lambda memory_id:{'memory_id':memory_id,'ranges':[
        {'binding_id':'hidden','status':'unresolvable','reason':'SOURCE_RANGE_NOT_PATH'},
        {'messages':[{'message_id':'good','speaker':'jiaming','text':'仍可读的原文'}]}]})
    with TestClient(app) as client:
        result,out=rpc(client,'source');assert not result['isError']
        assert out['data']['source'][0]['unavailable'] is True
        assert out['data']['source'][1]['text']=='仍可读的原文'
        assert out['data']['incomplete_sections']['source']=='partial'


def test_source_cursor_rejects_bucket_edit_instead_of_mixing_versions(actors,tmp_path,monkeypatch):
    mid,_=_seed(tmp_path,actors);_receipt_with_detail(mid,LONG_TEXT)
    with db.formal() as conn:conn.execute("UPDATE source_messages SET text=? WHERE provider_message_id='dm0'",('长原文'*3000,))
    monkeypatch.setattr(config,'SURFACE_DETAIL_PAGE_BYTES',2048)
    with TestClient(app) as client:
        result,body=rpc(client,'source');assert not result['isError']
        cursor=body['data']['next_cursor']
        registry.invoke(actors['jiaming'],'memory.update',{'memory_id':mid,'expected_version':1,'text':'修订后的事件正文'},None)
        result,body=rpc(client,'source',cursor)
        assert result['isError'] and body['error']['code']=='DETAIL_SNAPSHOT_STALE'


@pytest.mark.parametrize('section',['source','relations'])
def test_deep_page_invalidates_when_its_own_data_changes(actors,tmp_path,monkeypatch,section):
    mid,other=_seed(tmp_path,actors);_receipt_with_detail(mid,LONG_TEXT)
    if section=='source':
        with db.formal() as conn:conn.execute("UPDATE source_messages SET text=? WHERE provider_message_id='dm0'",('长原文'*3000,))
    else:
        registry.invoke(actors['jiaming'],'memory.update',{'memory_id':other,'expected_version':1,'text':'关联正文'*3000},None)
    monkeypatch.setattr(config,'SURFACE_DETAIL_PAGE_BYTES',2048)
    with TestClient(app) as client:
        result,body=rpc(client,section);assert not result['isError']
        cursor=body['data']['next_cursor']
        if section=='source':
            with db.formal() as conn:conn.execute("UPDATE source_messages SET text=? WHERE provider_message_id='dm0'",('原文已变',))
        else:
            registry.invoke(actors['jiaming'],'memory.update',{'memory_id':other,'expected_version':2,'text':'关联正文已变'},None)
        result,body=rpc(client,section,cursor)
        assert result['isError'] and body['error']['code']=='DETAIL_SNAPSHOT_STALE'
