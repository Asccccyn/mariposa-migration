"""Synthetic fixture; only usable under tests.conftest's temporary-root fuse."""
import json
import os
from pathlib import Path
from tests.conftest import reset_all
from mariposa import db, oauth
from mariposa.identity.service import authenticate
from mariposa.memory import service as memory
from mariposa.source import importer, binding

reset_all()
oauth.set_password('jiaming','browser-jiaming-password')
oauth.set_password('qiaosheng','browser-qiaosheng-password')
j=authenticate('tok-j')
for i in range(29):
    regular=memory.hold(j,f'普通合成事件{i}',original_title=f'普通记忆{i:02}',categories=['daily'],creation_mode='retrospective',memory_date='2026-10-01')['memory_id']
target=memory.hold(j,'紫藤合成事件，供编辑与心情回归使用。',original_title='紫藤记忆',categories=['daily'],creation_mode='contemporaneous',memory_date='2026-10-02')['memory_id']
unknown=memory.hold(j,'无日期合成事件',original_title='无日期记忆',categories=['daily'],creation_mode='retrospective')['memory_id']
p=Path(os.environ['MARIPOSA_ROOT'])/'browser-source.json'
p.write_text(json.dumps([{'uuid':'browser-conv','name':'浏览器原文','chat_messages':[
    {'uuid':f'browser-m{i}','sender':'human','created_at':'2026-10-01T10:00:00Z',
     'content':[{'type':'text','text':f'橙花原文第{i:02}条。'}]} for i in range(35)]}],ensure_ascii=False))
assert importer.import_file('jiaming',str(p))['status']=='completed'
with db.formal() as c:
    msg=c.execute("SELECT id,conversation_id FROM source_messages WHERE provider_message_id='browser-m0'").fetchone()
binding.bind('jiaming',regular,msg['conversation_id'],msg['id'],msg['id'])
Path(os.environ['MARIPOSA_ROOT'],'fixture.json').write_text(json.dumps({'target':target,'unknown':unknown,'conversation':msg['conversation_id']}))
