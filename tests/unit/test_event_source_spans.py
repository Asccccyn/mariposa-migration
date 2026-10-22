"""Explicit event scope, independent quotes, atomicity and full-member drift guards."""
import pytest
from mariposa import db
from mariposa.capabilities import registry
from mariposa.errors import Forbidden, IdempotencyConflict
from mariposa.source import binding
from tests.unit.test_hold_domain_op import seeded, _hold_args


def event_args(seeded, op='event-1'):
    args = _hold_args(seeded, op, specs=(0,1,2))
    args['event_source_ranges'] = args.pop('source_selections')
    return args


def test_no_quotes_atomic_event_hold_and_replay(seeded):
    actor=seeded['actors']['jiaming'];args=event_args(seeded)
    result=registry.invoke(actor,'memory.hold',args,None)['data']
    assert registry.invoke(actor,'memory.hold',args,None)['data']['memory_id']==result['memory_id']
    opened=binding.open_for_memory(result['memory_id'])
    assert [m['text'] for m in opened['ranges'][0]['messages']]==['第一句','第二句','第三句']
    with db.formal() as conn:
        assert conn.execute('SELECT count(*) FROM memory_source_spans').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM memory_our_words').fetchone()[0]==0
    changed=event_args(seeded);changed['event_source_ranges'][0]['members']=seeded['members'](0,1)
    with pytest.raises(IdempotencyConflict):registry.invoke(actor,'memory.hold',changed,None)


def test_legacy_quotes_never_claim_event_coverage(seeded):
    result=registry.invoke(seeded['actors']['jiaming'],'memory.hold',_hold_args(seeded),None)['data']
    opened=binding.open_for_memory(result['memory_id'])
    assert opened['ranges']==[] and opened['event_source_status']=='unbound'
    assert binding.open_legacy_for_memory(result['memory_id'])['ranges']


def test_gap_manifest_rejected_atomically(seeded):
    args=event_args(seeded);args['event_source_ranges'][0]['members']=seeded['members'](0,2)
    with pytest.raises(Forbidden) as e:registry.invoke(seeded['actors']['jiaming'],'memory.hold',args,None)
    assert e.value.code=='EVENT_SOURCE_INCOMPLETE'
    with db.formal() as conn:
        assert conn.execute('SELECT count(*) FROM memories').fetchone()[0]==0


def test_middle_revision_change_does_not_deliver_stale_body(seeded):
    result=registry.invoke(seeded['actors']['jiaming'],'memory.hold',event_args(seeded),None)['data']
    with db.formal() as conn:
        conn.execute('UPDATE source_messages SET content_hash=? WHERE id=?',('b'*64,seeded['members'](1)[0]['source_message_id']))
    out=binding.open_for_memory(result['memory_id'])
    assert out['ranges'][0]['status']=='unresolvable'
    assert out['ranges'][0]['reason']=='EVENT_SOURCE_CHANGED'
    assert 'messages' not in out['ranges'][0]


def test_broken_parent_path_rejected(seeded):
    with db.formal() as conn:
        conn.execute('UPDATE source_messages SET parent_provider_message_id=NULL WHERE id=?',(seeded['members'](1)[0]['source_message_id'],))
    with pytest.raises(Forbidden):registry.invoke(seeded['actors']['jiaming'],'memory.hold',event_args(seeded),None)


def test_event_replacement_requires_manifest_and_removal_preserves_memory(seeded):
    actor=seeded['actors']['jiaming'];result=registry.invoke(actor,'memory.hold',event_args(seeded),None)['data'];bid=result['binding_ids'][0]
    with pytest.raises(Forbidden):
        binding.correct('jiaming',bid,'replace_wrong_binding',replacement={'conversation_id':seeded['conv_id'],'start_message_id':seeded['members'](0)[0]['source_message_id'],'end_message_id':seeded['members'](2)[0]['source_message_id']})
    assert binding.open_for_memory(result['memory_id'])['ranges'][0]['messages']
    binding.correct('jiaming',bid,'remove_wrong_binding')
    assert binding.open_for_memory(result['memory_id'])['event_source_status']=='unbound'
    with db.formal() as conn:assert conn.execute('SELECT count(*) FROM memories').fetchone()[0]==1


def test_direct_pages_preserve_order_and_reject_mid_read_change(seeded,monkeypatch):
    from mariposa import config
    actor=seeded['actors']['jiaming'];args=event_args(seeded)
    # The ingested originals already have a canonical hash; make this isolated
    # fixture a long event before selecting its manifest.
    long='完整原文🙂\\n不是换行\n真换行"'*1000
    import hashlib
    members=args['event_source_ranges'][0]['members']
    members[1]['content_hash']=hashlib.sha256(long.encode()).hexdigest()
    with db.formal() as conn:conn.execute('UPDATE source_messages SET text=?,content_hash=? WHERE id=?',(long,members[1]['content_hash'],members[1]['source_message_id']))
    result=registry.invoke(actor,'memory.hold',args,None)['data']
    monkeypatch.setattr(config,'SURFACE_DETAIL_PAGE_BYTES',4096)
    cursor=None;items=[];count=0;first_cursor=None
    while True:
        page=registry.invoke(actor,'source.memory.open',{'memory_id':result['memory_id'],**({'cursor':cursor} if cursor else {})},None)['data']
        for rng in page['ranges']:
            for m in rng['messages']:
                if m.get('partial_item') and items and items[-1][0]==m['message_id']:items[-1]=(m['message_id'],items[-1][1]+m['text'])
                else:items.append((m['message_id'],m['text']))
        cursor=page.get('next_cursor');count+=1
        if first_cursor is None:first_cursor=cursor
        if not cursor:break
        assert count<200
    assert count>2
    assert [x[1] for x in items]==['第一句',long,'第三句']
    with db.formal() as conn:conn.execute('UPDATE source_messages SET content_hash=? WHERE id=?',('f'*64,members[1]['source_message_id']))
    with pytest.raises(Forbidden) as e:binding.open_page(result['memory_id'],first_cursor)
    assert e.value.code=='EVENT_SOURCE_SNAPSHOT_STALE'


def test_historical_agent_uses_endpoints_and_one_read_revision(seeded):
    actor=seeded['actors']['jiaming'];members=seeded['members'](0,1,2)
    args={'conversation_id':seeded['conv_id'],'start_message_id':members[0]['source_message_id'],'end_message_id':members[-1]['source_message_id']}
    from mariposa.capabilities.model_output import project
    read=registry.invoke(actor,'source.range.open',{**args,'for_binding':True},None)['data']
    slim=project('source.range.open',read,{})
    assert slim['source_revision'] and 'content_hash' not in str(slim)
    hold=event_args(seeded)
    hold['event_source_ranges']=[{**args,'source_revision':slim['source_revision']}]
    result=registry.invoke(actor,'memory.hold',hold,None)['data']
    assert len(binding.open_for_memory(result['memory_id'])['ranges'][0]['messages'])==3
    with db.formal() as conn:conn.execute('UPDATE source_messages SET content_hash=? WHERE id=?',('e'*64,members[1]['source_message_id']))
    with pytest.raises(Forbidden) as e:
        registry.invoke(actor,'source.binding.bind',{**args,'memory_id':result['memory_id'],'event_scope':True,'source_revision':slim['source_revision']},None)
    assert e.value.code=='EVENT_SOURCE_CHANGED'


def test_overlapping_spans_do_not_duplicate_evidence(seeded):
    args=event_args(seeded);args['event_source_ranges'].append(args['event_source_ranges'][0])
    with pytest.raises(Forbidden) as e:registry.invoke(seeded['actors']['jiaming'],'memory.hold',args,None)
    assert e.value.code=='EVENT_SOURCE_OVERLAP'
    with db.formal() as conn:assert conn.execute('SELECT count(*) FROM memories').fetchone()[0]==0


def test_imported_history_can_create_without_quotes_from_read_revision(actors,tmp_path):
    from tests.unit.test_memory_detail import _seed
    from mariposa.source import query
    _seed(tmp_path,actors)
    actor=actors['jiaming']
    read=registry.invoke(actor,'source.range.open',{'conversation_id':'dconv','start_message_id':'dm0','end_message_id':'dm3','for_binding':True},None)['data']
    out=registry.invoke(actor,'memory.hold',{'text':'历史事件','original_title':'历史','creation_mode':'retrospective','memory_date':'2026-07-09','categories':['daily'],'operation_id':'history-event','event_source_ranges':[{'conversation_id':'dconv','start_message_id':'dm0','end_message_id':'dm3','source_revision':read['source_revision']}]},None)['data']
    opened=registry.invoke(actor,'source.memory.open',{'memory_id':out['memory_id']},None)['data']
    assert [m['text'] for m in opened['ranges'][0]['messages']]==[f'原文消息{i}' for i in range(4)]
