"""Real stdlib chunk parser: recovery is application-complete, not blind JSON salvage."""
import http.client
import io
import json
import time
import pytest
from mariposa.retrieval.judges import typesafe_jev as ts

PAYLOAD = {'model': 'jev-latest', 'questions': {'a': {'type':'noul'}, 'b': {'type':'noul'}}}

def body():
    return {'model':'jev-1.13.0','answers':{
        'a':{'type':'noul','noul':0.91},'b':{'type':'noul','noul':0.86}},
        'usage':{'input_tokens':100,'output_tokens':30}}

def response(raw, ending=b'', *, headers=None, data_size=None):
    fields={'Content-Type':'application/json','Transfer-Encoding':'chunked'}
    fields.update(headers or {})
    encoded=b'HTTP/1.1 200 OK\r\n'+b''.join(f'{k}: {v}\r\n'.encode() for k,v in fields.items())+b'\r\n'
    encoded += f'{data_size if data_size is not None else len(raw):x}\r\n'.encode()+raw+b'\r\n'+ending
    class Sock:
        def makefile(self,*args): return io.BytesIO(encoded)
    r=http.client.HTTPResponse(Sock());r.begin();return r

def run(monkeypatch, raw, *, ending=b'', headers=None, data_size=None, payload=PAYLOAD):
    calls=[]
    def urlopen(req,**kw):
        calls.append(req)
        return response(raw,ending,headers=headers,data_size=data_size)
    monkeypatch.setattr(ts.urllib.request,'urlopen',urlopen)
    return ts.TypeSafeJevJudge()._post_with_limited_retry(payload),calls

def test_missing_final_chunk_recovers_exact_complete_answers_once(monkeypatch,caplog):
    original=body()
    out,calls=run(monkeypatch,json.dumps(original).encode())
    assert out==original and len(calls)==1
    assert 'typesafe_missing_chunk_terminator_recovered' in caplog.text
    assert 'jev-1.13.0' not in caplog.text
    assert '0.91' not in caplog.text

def test_well_framed_response_is_unchanged(monkeypatch,caplog):
    original=body();out,calls=run(monkeypatch,json.dumps(original).encode(),ending=b'0\r\n\r\n')
    assert out==original and len(calls)==1
    assert 'recovered' not in caplog.text

@pytest.mark.parametrize('change',[
 lambda b:b['answers'].pop('b'),
 lambda b:b['answers'].update(other={'type':'noul','noul':0.9}),
 lambda b:b['answers']['a'].update(noul=True),
 lambda b:b['answers']['a'].update(noul='0.9'),
 lambda b:b['answers']['a'].update(noul=1.1),
 lambda b:b['answers']['a'].update(noul=-0.1),
 lambda b:b['answers']['a'].update(noul=10**400),
 lambda b:b['answers']['a'].update(noul=float('nan')),
 lambda b:b['answers']['a'].update(noul=float('inf')),
 lambda b:b['answers']['a'].update(type='boolean'),
 lambda b:b.update(model=''),
 lambda b:b.pop('usage'),
 lambda b:b['usage'].update(input_tokens=-1),
 lambda b:b['usage'].update(output_tokens=True),
 lambda b:b.update(error='upstream error'),
])
def test_incomplete_or_invalid_answer_contract_rejected(monkeypatch,change):
    b=body();change(b)
    with pytest.raises(ts.JudgeUnavailable,match='incomplete_response_invalid'):
        run(monkeypatch,json.dumps(b).encode())

@pytest.mark.parametrize('raw',[
 b'{"model":"m"',
 b'{}{}',
 b'\xff',
 b'{}'+b' '*262144,
 json.dumps(body()).replace('"model": "jev-1.13.0"','"model":"other","model":"jev-1.13.0"').encode(),
 json.dumps(body()).replace('"noul": 0.91','"noul":0.1,"noul":0.91').encode(),
])
def test_strict_json_utf8_size_duplicates_and_trailing_data(monkeypatch,raw):
    with pytest.raises(ts.JudgeUnavailable,match='incomplete_response_invalid'):
        run(monkeypatch,raw)

@pytest.mark.parametrize('options',[
 {'ending':b'INVALID\r\n'},
 {'data_size':10000},
 {'headers':{'Content-Length':'123'}},
 {'headers':{'Content-Type':'text/plain'}},
 {'headers':{'Content-Encoding':'gzip'}},
])
def test_other_framing_errors_and_unsupported_headers_rejected(monkeypatch,options):
    with pytest.raises(ts.JudgeUnavailable):run(monkeypatch,json.dumps(body()).encode(),**options)

def test_recovery_obeys_absolute_deadline(monkeypatch):
    clock=[1.0]
    monkeypatch.setattr(time,'monotonic',lambda:clock[0])
    def urlopen(req,**kw):
        clock[0]=3.0
        return response(json.dumps(body()).encode())
    monkeypatch.setattr(ts.urllib.request,'urlopen',urlopen)
    with pytest.raises(ts.JudgeUnavailable,match='deadline_exceeded'):
        ts.TypeSafeJevJudge()._post_with_limited_retry(PAYLOAD,deadline_monotonic=2.0)

def test_surface_cache_reuses_recovered_value_but_never_invalid_partial(monkeypatch):
    from tests.conftest import reset_all
    from tests.unit.test_surface_selection_judge import _pool_entry,_SOURCE,_HYP
    from mariposa.surfacing import judge
    from mariposa.surfacing.models import surface_policy_snapshot,SurfaceBlocked
    from mariposa import db
    reset_all();judge.clear_transport_override()
    calls=[];valid=[True]
    def urlopen(req,**kw):
        q=json.loads(req.data)['questions'];calls.append(q)
        b={'model':'jev-1.13.0','answers':{k:{'type':'noul','noul':0.95} for k in q},
           'usage':{'input_tokens':100,'output_tokens':30}}
        if not valid[0]:b['answers']={}
        return response(json.dumps(b).encode())
    monkeypatch.setattr(ts.urllib.request,'urlopen',urlopen)
    j=judge.SurfaceJudge('typesafe_jev',surface_policy_snapshot())
    first=j.judge_prepare(_SOURCE,[],_HYP,[_pool_entry()])
    second=j.judge_prepare(_SOURCE,[],_HYP,[_pool_entry()])
    assert first.assessments[0].rank_signal==second.assessments[0].rank_signal==0.95
    assert len(calls)==1 and j.cache_hits==1
    with db.recall_runtime() as c:count=c.execute('SELECT COUNT(*) FROM jev_feature_cache').fetchone()[0]
    valid[0]=False
    with pytest.raises(SurfaceBlocked,match='SURFACE_UNKNOWN_OUTCOME'):
        j.judge_prepare({**_SOURCE,'body_hash':'new-input'},[],_HYP,[_pool_entry()])
    with db.recall_runtime() as c:assert c.execute('SELECT COUNT(*) FROM jev_feature_cache').fetchone()[0]==count
    assert len(calls)==2
