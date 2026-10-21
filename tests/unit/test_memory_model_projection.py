import copy
from mariposa import db
from mariposa.capabilities import registry
from mariposa.memory.model_projection import detail_view,memory_view
from tests.unit.test_memory_detail import _seed,_receipt_with_detail,LONG_TEXT

def test_hold_model_reply_is_small_and_full_atomic_receipt_replays(actors):
    args={'operation_id':'model-view-hold','original_title':'合成记忆','categories':['daily'],'text':'完整正文\n不缩写','creation_mode':'contemporaneous','memory_date':'2026-07-09','date_confidence':'exact'}
    brief=registry.invoke(actors['jiaming'],'memory.hold',{**args,'view':'model'},None)['data']
    assert brief=={'saved':True,'memory_id':brief['memory_id']}
    full=registry.invoke(actors['jiaming'],'memory.hold',{**args,'view':'full'},None)['data']
    assert full['memory_id']==brief['memory_id'] and full['operation_id']==args['operation_id']
    assert 'committed_at' in full and 'binding_ids' in full
    with db.formal() as c:assert c.execute('SELECT COUNT(*) FROM memories').fetchone()[0]==1

def test_model_detail_keeps_full_text_pagination_and_relations_without_internal_vectors(actors,tmp_path):
    mid,_=_seed(tmp_path,actors);_receipt_with_detail(mid,LONG_TEXT)
    args={'detail_ref':'dref_test','view':'model'};all_text='';sections=set()
    for _ in range(80):
        page=registry.invoke(actors['jiaming'],'memory.detail.read',args,None)['data']
        assert not {'snapshot_id','domain_version_vector','memory_version','page_index'} & set(page)
        sections.update(page['sections'])
        if 'memory' in page['sections']:
            m=page['sections']['memory'];all_text+=m['text'];assert 'text_range' in m
            assert not {'version','held_at','creation_mode','visibility','instruction_authority'} & set(m)
        if page['complete']:break
        args['cursor']=page['next_cursor']
    else:raise AssertionError('pagination did not finish')
    assert all_text==LONG_TEXT
    assert {'memory','mood','our_words','source','recollections','keeps','relations'}<=sections
    full=registry.invoke(actors['jiaming'],'memory.detail.read',{'detail_ref':'dref_test'},None)['data']
    assert 'domain_version_vector' in full and 'snapshot_id' in full

def test_open_retains_receipt_needed_for_explicit_confirmation(actors):
    args={'original_title':'合成记忆','categories':['daily'],'text':'正文','creation_mode':'contemporaneous'}
    held=registry.invoke(actors['jiaming'],'memory.hold',args,None)['data']
    opened=registry.invoke(actors['jiaming'],'memory.open',{'memory_id':held['memory_id'],'view':'model'},None)['data']
    assert opened['view_receipt'];assert not {'version','held_at','receipt_content_version','representation_version'} & set(opened)
    registry.invoke(actors['jiaming'],'memory.view.confirm',{'memory_id':held['memory_id'],'receipt_id':opened['view_receipt']},None)

def test_projection_does_not_mutate_records_or_hide_missing_content():
    src={'memory_id':'m','text':'原文','date_confidence':'unknown','memory_date':'2026-07-09','content_gap':{'code':'LEGACY_CONTENT_GAP'},'mood':{'text':'心情','tags':['爱'],'author':'jiaming','evidence_state':'synthetic'}}
    before=copy.deepcopy(src);view=memory_view(src)
    assert view['date'] is None and view['content_gap']==src['content_gap'] and src==before
    page={'memory_id':'m','complete':False,'next_cursor':'p1','section_status':{'our_words':'partial'},'sections':{'our_words':[{'word_id':'w','speaker':'jiaming','text':'长话语分片','partial_item':True,'item_index':0,'text_range':[0,5],'created_at':'internal'}]}}
    projected=detail_view(page)
    assert projected['next_cursor']=='p1';word=projected['sections']['our_words'][0]
    assert word['text_range']==[0,5] and word['partial_item'] is True and 'created_at' not in word
