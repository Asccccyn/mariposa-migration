"""JSON escaping must preserve business text; do not unescape or sanitize it twice."""
import json
import pytest
from starlette.responses import JSONResponse
from mariposa.capabilities.mcp_adapter import _tool_result
from mariposa.memory.model_projection import detail_view


@pytest.mark.parametrize('text',[
    '真实换行\n第二段\n\n他说"你好"。',
    r'D:\AI-Companion\reading-nook-study\new\test',
    r'字面量 \n、\t、\u4e2d 和 JSON {"text":"\\n"}',
    '  前后空格\t制表符\r\n换行\u0000控制符\u001f  ',
    '中文🙂👩\u200d💻𝄞\u2028行分隔\u2029段分隔',
    '日志原话：unexpected character "�" in variable name "\\xff"',
])
def test_transport_and_model_json_decode_exactly_once_without_text_loss(text):
    view=detail_view({'memory_id':'m','sections':{'memory':{
        'memory_id':'m','memory_date':'2026-07-09','text':text},
        'our_words':[{'word_id':'internal','speaker':'jiaming','text':text,'source_ref':'internal'}]}})
    result=_tool_result({'ok':True,'data':view})
    wire=JSONResponse({'jsonrpc':'2.0','id':1,'result':result}).body
    received=json.loads(wire)['result']['content'][0]['text']
    decoded=json.loads(received)
    assert isinstance(decoded,dict), 'model payload must be an object, not a JSON-encoded string of JSON'
    assert decoded['data']['text']==text
    assert decoded['data']['our_words']==[{'speaker':'jiaming','text':text}]
    assert not any(ord(c)<32 for c in received), 'control characters must be escaped in JSON wire text'


def test_unpaired_surrogate_cannot_silently_produce_invalid_utf8():
    result=_tool_result({'text':'bad\ud800'})
    with pytest.raises(UnicodeEncodeError):
        JSONResponse(result)
