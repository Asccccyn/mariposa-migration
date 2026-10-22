import json
import pytest
from mariposa.surfacing import judge, contract
from mariposa.surfacing.models import surface_policy_snapshot
from tests.unit.test_surface_selection_judge import _pool_entry,_HYP
from tests.conftest import reset_all

@pytest.fixture(autouse=True)
def isolated():
    reset_all();judge.clear_transport_override()
    yield
    judge.clear_transport_override()

def test_one_semantic_target_no_ids_hashes_hypotheses_or_domain_vectors():
    current={'message_id':'private-message','revision':7,'sequence':42,'body_hash':'private-hash','text':'你还记得2026年7月9日我们把记忆库建成那件事吗？','occurred_at':'2026-10-21T12:00:00+00:00'}
    state=judge._state(current,[{'message_id':'private-neighbor','role':'owner','text':'unneeded-neighbor'}],_HYP,current,[_pool_entry('memory:private')])
    assert state=={'target':current['text'],'candidates':[{'id':0,'date':'2026-07-09','title':'t','text':'事件正文'}]}
    wire=json.dumps(state,ensure_ascii=False)
    for private in ('private-message','private-hash','private-neighbor','memory:private','event_hypotheses','revision','domain_versions'):
        assert private not in wire

def test_context_only_for_ellipsis_and_relative_dates_use_frozen_basis():
    context=[{'role':'owner','text':'我们说起图书馆的小橘猫'},{'role':'assistant','text':'那次送去医院的事情？'}]
    source={'text':'那后来呢？','body_hash':'h'}
    assert judge._state(source,context,[],None)['context']==['用户：我们说起图书馆的小橘猫','助手：那次送去医院的事情？']
    source={'text':'你还记得昨天做饭吗？','body_hash':'h','occurred_at':'2026-10-21T12:00:00+00:00'}
    assert judge._state(source,[],[],None)['time_basis']=={'at':source['occurred_at'],'timezone':'Asia/Shanghai'}
    changed={**source,'occurred_at':'2026-10-22T12:00:00+00:00'}
    assert judge._context_hash('prepare',source,[])!=judge._context_hash('prepare',changed,[])
    assert 'time_basis' not in judge._state({**source,'occurred_at':None},[],[],None)

def test_short_question_ids_still_bind_cache_to_real_candidate():
    calls=[]
    def transport(payload):
        calls.append(payload)
        return {'model':'jev-test','answers':{k:{'type':'noul','noul':0.95 if len(calls)==1 else 0.01} for k in payload['questions']}}
    judge.set_transport_for_tests(transport)
    j=judge.SurfaceJudge('typesafe_jev',surface_policy_snapshot())
    src={'text':'那件事呢','body_hash':'h'}
    a=j.judge_prepare(src,[],_HYP,[_pool_entry('memory:a')]).assessments[0]
    b=j.judge_prepare(src,[],_HYP,[_pool_entry('memory:b')]).assessments[0]
    assert a.candidate_ref=='memory:a' and a.strictly_confirmed()
    assert b.candidate_ref=='memory:b' and b.outcome==contract.NOT_SAME
    assert len(calls)==2
    assert all(set(c['questions'])=={'c0_same_occurrence'} for c in calls)
    assert j.judge_prepare(src,[],_HYP,[_pool_entry('memory:a')]).assessments[0].strictly_confirmed()
    assert len(calls)==2


def test_changed_semantic_date_does_not_reuse_previous_judgement():
    calls=[]
    def transport(payload):
        calls.append(payload)
        return {'model':'jev-test','answers':{k:{'type':'noul','noul':0.95} for k in payload['questions']}}
    judge.set_transport_for_tests(transport)
    j=judge.SurfaceJudge('typesafe_jev',surface_policy_snapshot())
    src={'text':'那件事呢','body_hash':'h'}
    c=_pool_entry('memory:date-test')
    c['memory_date']='2026-07-09'; c['date_confidence']='unknown'
    j.judge_prepare(src,[],_HYP,[c])
    c['memory_date']='2026-07-10'
    j.judge_prepare(src,[],_HYP,[c])
    assert len(calls)==2
    assert [x['state']['candidates'][0]['date'] for x in calls]==['2026-07-09','2026-07-10']
