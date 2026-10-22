"""Final JSON-RPC wire tests; no external client, provider or credentials used."""
import copy
import json
import pytest
from fastapi.testclient import TestClient
from mariposa.app import app
from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden
from tests.conftest import TOKENS


@pytest.fixture
def client(actors):
    with TestClient(app) as c:
        c.headers.update({'Authorization': 'Bearer ' + TOKENS['jiaming']})
        yield c


def call(client, mode=None, arguments=None, name='mariposa_time_now'):
    params={'name':name,'arguments':arguments or {}}
    if mode is not None:
        params['_meta']={'content_envelope':mode}
    return client.post('/mcp',json={'jsonrpc':'2.0','id':11,'method':'tools/call','params':params}).json()


def read(result):
    return result.get('structuredContent') or json.loads(result['content'][0]['text'])


@pytest.mark.parametrize('mode',[None,'text','single','dual'])
@pytest.mark.parametrize('pagination',[None,{'next_cursor':'p2','result_set_id':'rs'},{}])
def test_body_and_cursor_survive_once_on_default_wire(client,monkeypatch,mode,pagination):
    marker='唯一测试正文标记：原样保留！！'
    payload={'ok':True,'data':{'text':marker,'saved':False,'zero':0}}
    if pagination is not None:
        payload['data']['pagination']=pagination
    before=copy.deepcopy(payload)
    monkeypatch.setattr(registry,'invoke',lambda *a,**kw:payload)
    result=call(client,mode)['result']
    assert read(result)==before
    wire=json.dumps(result,ensure_ascii=False,separators=(',',':'))
    assert wire.count(marker)==(2 if mode=='dual' else 1)
    assert payload==before
    if mode in (None,'text'):
        # A text-only client receives everything. A client blindly collecting both
        # fields still has only one full body because the other field is absent.
        assert len(result['content'])==1 and 'structuredContent' not in result
        assert json.loads(result['content'][0]['text'])==payload
    else:
        assert result['structuredContent']==payload
    assert result['isError'] is False


@pytest.mark.parametrize('mode',[None,'text','single','dual'])
def test_error_is_readable_in_negotiated_channel(client,monkeypatch,mode):
    def fail(*args,**kwargs):
        raise Forbidden('synthetic failure',code='TEST_FAILURE')
    monkeypatch.setattr(registry,'invoke',fail)
    result=call(client,mode)['result']
    assert result['isError'] is True
    assert read(result)=={'ok':False,'error':{'code':'TEST_FAILURE','message':'synthetic failure'}}


@pytest.mark.parametrize('meta',[{'content_envelope':'typo'}, {'content_envelope':None},
                                 {'content_envelope':[]}, 'single'])
def test_invalid_negotiation_cannot_execute_a_write(client,monkeypatch,meta):
    def forbidden(*a,**kw):
        pytest.fail('business execution must not run for malformed negotiation')
    monkeypatch.setattr(registry,'invoke',forbidden)
    body=client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/call',
        'params':{'name':'mariposa_memory_hold','arguments':{},'_meta':meta}}).json()
    assert body['error']['code']==-32600


def test_envelope_selection_does_not_change_idempotent_write(client):
    args={'operation_id':'envelope-once','text':'合成写入正文','original_title':'合成测试',
          'categories':['daily'],'creation_mode':'retrospective',
          'memory_date':'2026-07-09','date_confidence':'exact'}
    ids=[]
    for mode in (None,'single','dual','text'):
        result=call(client,mode,args,'mariposa_memory_hold')['result']
        assert result['isError'] is False
        ids.append(read(result)['data']['memory_id'])
    assert len(set(ids))==1
    with db.formal() as conn:
        assert conn.execute('SELECT count(*) FROM memories WHERE memory_id=?',(ids[0],)).fetchone()[0]==1


def test_no_output_schema_requires_a_structured_response(client):
    result=client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'tools/list'}).json()['result']
    assert all('outputSchema' not in tool for tool in result['tools'])


def test_wire_measurement(client,monkeypatch):
    payload={'ok':True,'data':{'memory_id':'synthetic-m1','date':'2026-07-09',
        'text':'合成验证正文，保持完整。\n！！'*200,'next_cursor':'synthetic-next'}}
    monkeypatch.setattr(registry,'invoke',lambda *a,**kw:payload)
    before=call(client,'dual');after=call(client)
    encode=lambda x:json.dumps(x,ensure_ascii=False,separators=(',',':'))
    assert read(before['result'])==read(after['result'])==payload
    print('MCP_WIRE_SAMPLE='+json.dumps({'before_dual':before,'after_default_text':after,
        'before_bytes':len(encode(before).encode()),'after_bytes':len(encode(after).encode())},ensure_ascii=False))
