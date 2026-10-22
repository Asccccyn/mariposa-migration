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
        sections.update(page)
        if 'text' in page:
            m=page;all_text+=m['text'];assert 'text_range' in m
            assert not {'held_at','creation_mode','visibility','instruction_authority'} & set(m)
        if not page.get('next_cursor'):break
        args['cursor']=page['next_cursor']
    else:raise AssertionError('pagination did not finish')
    assert all_text==LONG_TEXT
    assert {'text','our_words','recollections','keeps','relations'}<=sections
    assert 'source' not in sections and 'section_status' not in sections
    full=registry.invoke(actors['jiaming'],'memory.detail.read',{'detail_ref':'dref_test'},None)['data']
    assert 'domain_version_vector' in full and 'snapshot_id' in full

def test_open_retains_receipt_needed_for_explicit_confirmation(actors):
    args={'original_title':'合成记忆','categories':['daily'],'text':'正文','creation_mode':'contemporaneous'}
    held=registry.invoke(actors['jiaming'],'memory.hold',args,None)['data']
    opened=registry.invoke(actors['jiaming'],'memory.open',{'memory_id':held['memory_id'],'view':'model'},None)['data']
    assert opened['version']==1;assert opened['view_receipt'];assert not {'held_at','receipt_content_version','representation_version'} & set(opened)
    registry.invoke(actors['jiaming'],'memory.view.confirm',{'memory_id':held['memory_id'],'receipt_id':opened['view_receipt']},None)

def test_projection_does_not_mutate_records_or_hide_missing_content():
    src={'memory_id':'m','text':'原文','date_confidence':'unknown','memory_date':'2026-07-09','content_gap':{'code':'LEGACY_CONTENT_GAP'},'mood':{'text':'心情','tags':['爱'],'author':'jiaming','evidence_state':'synthetic'}}
    before=copy.deepcopy(src);view=memory_view(src)
    assert view['date']=='2026-07-09' and view['content_gap']==src['content_gap'] and src==before
    page={'memory_id':'m','complete':False,'next_cursor':'p1','section_status':{'our_words':'partial'},'sections':{'our_words':[{'word_id':'w','speaker':'jiaming','text':'长话语分片','partial_item':True,'item_index':0,'text_range':[0,5],'created_at':'internal'}]}}
    projected=detail_view(page)
    assert projected['next_cursor']=='p1';word=projected['our_words'][0]
    assert word['text_range']==[0,5] and word['partial_item'] is True and 'created_at' not in word


def test_normal_read_omits_envelope_empty_fields_and_duplicate_source_relations():
    source={'memory_id':'00453','complete':True,'next_cursor':None,'changed_since_surface':None,
            'section_status':{'memory':'complete','our_words':'complete','source':'complete','keeps':'empty'},
            'sections':{'memory':{'memory_id':'00453','original_title':'标题','text':'完整正文','memory_date':'2026-10-19','date_confidence':'unknown','held_at':'wrong-date','text_range':[0,4]},
             'our_words':[{'word_id':'w1','source_ref':'s1','speaker':'jiaming','text':'！！ '},{'speaker':'qiaosheng','text':'  '}],
             'source':[{'binding_id':'b','messages':[{'text':'duplicate'}]}],
             'recollections':[],'keeps':{'active_keepers':[],'marks':[]},
             'relations':[{'domain':'source_binding','relation_id':'b'},{'domain':'word_source','relation_id':'w1'}]}}
    before=copy.deepcopy(source)
    assert detail_view(source)=={'memory_id':'00453','title':'标题','date':'2026-10-19','text':'完整正文','our_words':[{'word_id':'w1','source_ref':'s1','speaker':'jiaming','text':'！！ '}]}
    assert source==before


def test_missing_event_date_retains_body_and_never_uses_written_time():
    out=memory_view({'memory_id':'m','text':'正文','held_at':'2026-10-19'})
    assert out=={'memory_id':'m','date_unknown':True,'text':'正文'}


def test_meaningful_relations_and_incomplete_state_are_not_hidden():
    page={'memory_id':'m','complete':False,'next_cursor':'p2','changed_since_surface':True,
          'section_status':{'mood':'unavailable','source':'unavailable'},
          'sections':{'relations':[{'relation_id':'private','domain':'memory_relation','direction':'out','confidence':'exact','other':{'type':'memory','memory_id':'m2','original_title':'另一个事件'}}]}}
    result=detail_view(page)
    assert result['relations'][0]['target']['memory_id']=='m2'
    assert result['relations'][0]['relation_id'] == 'private'  # correction target
    assert result['incomplete_sections']=={'mood':'unavailable'}
    assert result['next_cursor']=='p2' and result['complete'] is False
