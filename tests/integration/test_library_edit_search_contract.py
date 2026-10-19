"""Library editing and navigation regressions using public capability contracts."""
import json

import pytest
from fastapi.testclient import TestClient

from mariposa import db
from mariposa.app import app
from mariposa.capabilities import registry
from mariposa.errors import Forbidden, NotFound, VersionConflict
from mariposa.memory import service as memory
from mariposa.source import importer, query


def invoke(actor, name, **args):
    return registry.invoke(actor, name, args, None)['data']


def hold(actor, text='合成编辑内容', date=None, title='合成记忆'):
    return memory.hold(actor, text, original_title=title, categories=['daily'],
                       creation_mode='retrospective', memory_date=date)['memory_id']


def test_atomic_edit_rejects_invalid_category_without_partial_version(actors):
    j = actors['jiaming']
    mid = hold(j, date='2026-10-01')
    with pytest.raises(Forbidden) as caught:
        invoke(j, 'memory.update', memory_id=mid, expected_version=1,
               text='不能半成功', categories=['plan'])
    assert caught.value.code == 'PLAN_BINDING_REQUIRED'
    current = invoke(j, 'memory.get', memory_id=mid)
    assert current['version'] == 1 and current['text'] == '合成编辑内容'
    assert current['categories'] == ['daily']
    updated = invoke(j, 'memory.update', memory_id=mid, expected_version=1,
                     text='一次提交', categories=['sweet'], memory_date=None)
    current = invoke(j, 'memory.get', memory_id=mid)
    assert updated['version'] == current['version'] == 2
    assert current['text'] == '一次提交' and current['categories'] == ['sweet']
    assert current['memory_date'] is None
    with pytest.raises(VersionConflict):
        invoke(j, 'memory.update', memory_id=mid, expected_version=1,
               text='旧版本不得覆盖')


def test_missing_target_is_404_on_both_http_edit_paths(actors):
    with TestClient(app) as client:
        for name, args in [('memory.update', {'memory_id':'missing', 'expected_version':1, 'text':'x'}),
                           ('memory.mood.update', {'memory_id':'missing', 'mood':{'tags':[]}})]:
            response = client.post('/api/capability/'+name,
                                   headers={'Authorization':'Bearer tok-j'},
                                   json={'arguments':args})
            assert response.status_code == 404
            assert response.json()['error']['code'] == 'NOT_FOUND'


def test_null_date_list_and_browse_cursors_cover_every_memory_once(actors):
    j = actors['jiaming']
    ids = [hold(j, date=d) for d in ['2026-10-01','2026-10-01',None,None,None]]
    for capability in ['memory.list', 'memory.recall']:
        collected=[]; cursor=None
        for _ in range(5):
            args={'limit':2}
            if cursor is not None: args['cursor']=cursor
            result=invoke(j,capability,**args)
            collected.extend(x['memory_id'] for x in result.get('items',result.get('hits',[])))
            cursor=result['next_cursor']
            if cursor is None:break
        else:pytest.fail('cursor did not terminate')
        assert set(collected)==set(ids) and len(collected)==len(ids)


def test_all_keywords_match_before_pagination_across_distinct_fields(actors):
    j=actors['jiaming']
    # This common match used to fall outside one keyword's first 100 hits.
    for i in range(105):hold(j,'苹果',title=f'单词 {i}')
    target=hold(j,'苹果 香蕉 '+'背景 '*40,title='共同词命中')
    result=invoke(j,'memory.recall',lexical_terms=['苹果','香蕉'],keyword_match='all',limit=1)
    assert [h['memory_id'] for h in result['hits']]==[target]
    assert result['next_cursor'] is None
    # One token in a title and the other in event text may satisfy the same bucket.
    mid=memory.hold(j,'月亮事件',original_title='星星标题',categories=['milestone'],creation_mode='retrospective')['memory_id']
    both=invoke(j,'memory.recall',lexical_terms=['星星','月亮'],keyword_match='all')
    assert [h['memory_id'] for h in both['hits']]==[mid]
    assert set(both['hits'][0]['matched_fields'])=={'original_title','event_text'}
    first=invoke(j,'memory.recall',lexical_terms=['苹果','香蕉'],keyword_match='any',limit=100)
    second=invoke(j,'memory.recall',lexical_terms=['苹果','香蕉'],keyword_match='any',limit=100,cursor=first['next_cursor'])
    assert len(first['hits'])+len(second['hits'])==106


def test_open_date_ranges_and_reversed_range(actors):
    j=actors['jiaming'];mid=hold(j,date='2026-10-01')
    for bound in [{'from':'2026-10-01'},{'to':'2026-10-01'}]:
        result=invoke(j,'memory.recall',filters={'event_date':bound})
        assert [h['memory_id'] for h in result['hits']]==[mid]
    with pytest.raises(Forbidden):
        invoke(j,'memory.recall',filters={'event_date':{'from':'2026-10-02','to':'2026-10-01'}})


def test_source_identity_and_duplicate_sequence_anchor(actors,tmp_path):
    j=actors['jiaming'];path=tmp_path/'source.json'
    path.write_text(json.dumps([{'uuid':'test-conv','name':'合成会话','chat_messages':[
        {'uuid':f'm{i}','sender':'human','created_at':'2026-10-01T10:00:00Z',
         'content':[{'type':'text','text':f'查找消息 {i}'}]} for i in range(4)]}]))
    assert importer.import_file('jiaming',str(path))['status']=='completed'
    with db.formal() as c:
        c.execute('UPDATE source_messages SET sequence=10')
        rows=c.execute('SELECT id,conversation_id FROM source_messages ORDER BY id').fetchall()
    target=rows[-1]
    page=invoke(j,'source.conversation.get',conversation_id=target['conversation_id'],
                page=1,limit=2,around_message_id=target['id'])
    assert page['page']==2 and target['id'] in [m['message_id'] for m in page['messages']]
    assert all(m['conversation_id']==target['conversation_id'] for m in page['messages'])
    assert all(m['memories_referencing']==[] for m in page['messages'])
    hits=invoke(j,'source.search',query='查找')['hits']
    assert hits and all(h['conversation_id']==target['conversation_id'] for h in hits)
    with db.formal() as c:c.execute('UPDATE source_messages SET live_superseded=1 WHERE id=?',(target['id'],))
    with pytest.raises(NotFound) as caught:
        invoke(j,'source.conversation.get',conversation_id=target['conversation_id'],around_message_id=target['id'])
    assert caught.value.code=='SOURCE_MESSAGE_NOT_CURRENT'


def test_jiaming_mood_edit_remains_authorized_and_audited(actors):
    j=actors['jiaming']
    mid=memory.hold(j,'合成同期正文',original_title='心情授权保留',categories=['daily'],creation_mode='contemporaneous')['memory_id']
    invoke(j,'memory.mood.update',memory_id=mid,mood={'text':'整理后补充','tags':['开心']})
    got=invoke(j,'memory.get',memory_id=mid)
    assert got['mood']['tags']==['开心'] and got['version']==1
    with db.formal() as c:
        audit=c.execute("SELECT actor_principal FROM audit_events WHERE event_type='memory.mood_updated' AND resource_id=?",(mid,)).fetchone()
    assert audit['actor_principal']=='jiaming'


def test_batch_can_fetch_current_memories_without_per_card_http_calls(actors):
    j=actors['jiaming'];ids=[hold(j,title=f'批量{i}') for i in range(3)]
    result=invoke(j,'memory.cards.batch',memory_ids=ids,include_memory=True)
    assert set(result['cards'])==set(ids)
    assert all(card['memory']['memory_id']==mid and card['memory']['version']==1
               for mid,card in result['cards'].items())


def test_app_imports_in_a_cold_process_without_fixture_import_order(tmp_path):
    import os,subprocess,sys
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]
    env={**os.environ,'MARIPOSA_ROOT':str(tmp_path/'cold'),
         'MARIPOSA_ALLOW_CREATE':'1','PYTHONPATH':str(root/'backend'),
         'PYTHONDONTWRITEBYTECODE':'1'}
    run=subprocess.run([sys.executable,'-B','-c','from mariposa.app import app; assert app.title == "mariposa"'],
                       cwd=tmp_path,env=env,capture_output=True,text=True,timeout=15)
    assert run.returncode==0,run.stderr
