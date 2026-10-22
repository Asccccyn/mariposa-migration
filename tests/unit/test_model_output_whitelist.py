from mariposa.capabilities.model_output import project
from mariposa.memory.model_projection import detail_view
from mariposa.surfacing.judge import _candidate_view


def test_occurrence_date_is_not_erased_by_confidence():
    assert _candidate_view({'memory_date':'2026-07-09','date_confidence':'unknown','event_text':'正文！！'})['date']=='2026-07-09'


def test_episode_keeps_cas_and_only_required_anchor_fields():
    v={'episode_id':'ep','revision':4,'state':'OPEN','scope_id':'x','created_at':'now',
       'source_selections':[{'conversation_id':'c','members':[{'source_message_id':'m','content_hash':'h'}]}]}
    result=project('episode.get',v,{})
    assert result['revision']==4
    assert result['source_selections']==v['source_selections']
    assert 'scope_id' not in result and 'created_at' not in result
    assert v['created_at']=='now'


def test_full_business_relation_survives_detail_projection():
    result=detail_view({'sections':{'relations':[
        {'relation_id':'rel','domain':'memory_relation','direction':'in','relation_type':'contradicts',
         'confidence':'inferred','other':{'memory_id':'m','type':'memory','title':'标题','original_title':'标题'}},
        {'domain':'source_binding','relation_id':'internal'}]}})
    assert result['relations']==[{'relation_id':'rel','domain':'memory_relation','direction':'in',
        'relation_type':'contradicts','uncertain':True,'target':{'memory_id':'m','title':'标题'}}]


def test_operable_marks_and_quotes_are_not_lost():
    result=detail_view({'sections':{'our_words':[{'word_id':'w','speaker':'jiaming','text':'！！','source_ref':'s','source_binding_version':0}],
        'keeps':{'marks':[{'mark_id':'k','owner':'jiaming','created_at':'now'}]}}})
    assert result['our_words'][0]=={'speaker':'jiaming','text':'！！'}
    assert result['keeps']['marks']==[{'mark_id':'k','owner':'jiaming'}]


def test_successful_hold_does_not_change_canonical_receipt():
    v={'operation_id':'op','memory_id':'m','version':1,'committed_at':'now','binding_ids':['b']}
    assert project('memory.hold',v,{})=={'saved':True,'memory_id':'m'}
    assert v['operation_id']=='op'


def test_recall_contract_preserves_uncertainty_and_next_call_arguments():
    raw={'recall_session_id':'r','revision':2,'judge_mode':'off','search_status':'FOUND',
         'retrieval_coverage':{'retrieval_scope_exhaustive':False,'stage_filter':{'secret':5}},
         'pagination':{'has_more':True,'result_set_id':'rs','next_cursor':'next','candidate_total':4},
         'candidates':[{'candidate_ref':'memory:m','memory_id':'m','scores':{'x':1},
             'evidence':[{'field':'event_text','snippet':'完整正文','source_version':'private'}]}]}
    out=project('memory.recall.start',raw,{})
    assert out['judgement']=='unjudged' and out['coverage']=='incomplete'
    assert out['pagination']=={'result_set_id':'rs','next_cursor':'next'}
    assert out['candidates'][0]['evidence']==[{'field':'event_text','snippet':'完整正文'}]
    assert 'scores' not in out['candidates'][0]
    assert out['revision']==2


def test_i_history_is_explicit_and_current_body_is_not_duplicated():
    raw={'content':'same','items':[{'item_id':'i','revision':3,'content':'same','updated_at':'technical'}]}
    assert project('i.get',raw,{})=={'items':[{'item_id':'i','revision':3,'content':'same'}]}
    h=project('i.item.history',{'item_id':'i','current_revision':3,'revisions':[{'revision':1,'content':'old','created_at':'then','payload_hash':'secret'}]}, {})
    assert h['revisions']==[{'revision':1,'content':'old','created_at':'then'}]


def test_bootstrap_retains_every_business_fragment_and_its_continuation():
    raw={'snapshot_id':'snap','state_hash':'secret','memory_days':{'items':[
        {'memory_id':'m','original_title':'题','memory_date':'2026-07-09','event_text':'第一段',
         'event_text_truncated':True,'event_text_next':{'memory_id':'m','field':'event_text','offset':3},
         'our_words':[{'speaker':'jiaming','texts':['！！']}]}], 'count':1},
         'i':{'content':'我的话','has_history':False}, 'plans':{'items':[],'next_cursor':{'plans_offset':None}}}
    out=project('bootstrap.get',raw,{})
    assert out['snapshot_id']=='snap'
    assert out['memory_days']['items'][0]['our_words']==[{'speaker':'jiaming','texts':['！！']}]
    assert out['memory_days']['items'][0]['event_text_next']['offset']==3
    assert 'state_hash' not in out and 'count' not in out['memory_days']
    assert 'next_cursor' not in out['plans']


def test_source_read_keeps_original_text_and_explicit_thinking_and_refs():
    raw={'messages':[{'message_id':'s','conversation_id':'c','speaker':'jiaming',
         'text':'完整原文','excerpt':'原文','thinking_text':'已存思考原文','occurred_date':'2026-07-09',
         'provider_message_id':'duplicate-id','id_synthetic':False}],
         'has_more':True,'next_after_cursor':{'seq':1,'id':'s'}}
    out=project('source.conversation.get',raw,{})
    assert out['messages'][0]['text']=='完整原文'
    assert out['messages'][0]['thinking_text']=='已存思考原文'
    assert 'provider_message_id' not in out['messages'][0]
    assert 'excerpt' not in out['messages'][0]
    assert out['next_after_cursor']=={'seq':1,'id':'s'}


def test_write_status_distinguishes_unknown_and_historical_success_now_deleted():
    assert project('memory.hold.status',{'completed':False,'receipt':None},{})=={'status':'unconfirmed'}
    assert project('memory.hold.status',{'completed':True,'receipt':{'memory_id':'m','operation_id':'op'},'memory_current':{'exists':False}}, {})=={'saved':True,'memory_id':'m','deleted':True}
