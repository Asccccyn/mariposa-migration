"""Bounded page navigation over the visible source-message projection."""
from __future__ import annotations

import json

import pytest

from mariposa import db
from mariposa.capabilities import input_schemas, registry
from mariposa.errors import MariposaError
from mariposa.source import importer, query


def _import(tmp_path, conversations):
    path = tmp_path / 'page-navigation.json'
    path.write_text(json.dumps(conversations), encoding='utf-8')
    result = importer.import_file('jiaming', str(path))
    assert result['status'] == 'completed'


def _message(message_id):
    return {'uuid': message_id, 'sender': 'human',
            'created_at': '2026-10-01T10:00:00Z',
            'content': [{'type': 'text', 'text': 'Message ' + message_id}]}


@pytest.fixture()
def conversation(actors, tmp_path):
    _import(tmp_path, [{'uuid': 'page-conv', 'name': 'Page navigation',
                       'chat_messages': [_message(f'page-m{i}')
                                         for i in range(10)]}])
    # Import snapshots can repeat sequence numbers; live ingest can leave gaps.
    sequences = [10, 10, 10, 30, 50, 50, 90, 110, 0, 20]
    with db.formal() as conn:
        for index, sequence in enumerate(sequences):
            conn.execute('UPDATE source_messages SET sequence=?'
                         ' WHERE provider_message_id=?',
                         (sequence, f'page-m{index}'))
        conn.execute('UPDATE source_messages SET live_superseded=1'
                     " WHERE provider_message_id='page-m8'")
        conn.execute('UPDATE source_messages SET published=0'
                     " WHERE provider_message_id='page-m9'")
        rows = conn.execute('SELECT id, provider_message_id FROM source_messages'
                            ' WHERE published=1 AND live_superseded=0'
                            ' ORDER BY sequence, id').fetchall()
    return actors['jiaming'], [row['provider_message_id'] for row in rows]


def _get(actor, **args):
    return registry.invoke(actor, 'source.conversation.get',
                           {'conversation_id': 'page-conv', **args}, None)['data']


def _ids(result):
    return [message['provider_message_id'] for message in result['messages']]


def test_numbered_pages_have_no_gaps_duplicates_or_hidden_revisions(conversation):
    actor, expected = conversation
    pages = [_get(actor, page=page, limit=2) for page in range(1, 5)]
    assert [message for result in pages for message in _ids(result)] == expected
    assert len(set(expected)) == 8
    for page, result in enumerate(pages, 1):
        assert (result['page'], result['total'], result['page_size'],
                result['total_pages']) == (page, 8, 2, 4)
        assert result['has_more'] is (page < 4)
        assert result['published_total'] == result['conversation']['message_count'] == 8
    assert _ids(_get(actor, page=100000000000000000000, limit=2)) == expected[-2:]
    assert _get(actor, page=100000000000000000000, limit=2)['page'] == 4


def test_around_sequence_selects_actual_page_for_duplicates_gaps_and_edges(conversation):
    actor, expected = conversation
    for sequence, page in [(-1, 1), (10, 1), (11, 2), (30, 2),
                           (31, 3), (50, 3), (51, 4), (110, 4), (1000, 4),
                           (-(2**100), 1), (2**100, 4)]:
        result = _get(actor, page=99, around_seq=sequence, limit=2)
        assert result['page'] == page
        assert _ids(result) == expected[(page - 1) * 2:page * 2]
    # Duplicate sequence=10 straddles pages 1 and 2; choose the first member.
    assert _ids(_get(actor, page=1, around_seq=10, limit=2))[0] == expected[0]


def test_last_partial_page_and_empty_projection_are_normalized(conversation):
    actor, expected = conversation
    result = _get(actor, page=99, limit=3)
    assert (result['page'], result['total_pages']) == (3, 3)
    assert _ids(result) == expected[-2:]
    with db.formal() as conn:
        conn.execute('UPDATE source_messages SET published=0')
    result = _get(actor, page=99, around_seq=50, limit=3)
    assert (result['page'], result['total'], result['page_size'],
            result['total_pages']) == (1, 0, 3, 1)
    assert result['messages'] == [] and result['has_more'] is False
    assert result['next_after_cursor'] is None
    assert result['prev_before_cursor'] is None


def test_legacy_cursor_and_around_window_remain_available(conversation):
    actor, expected = conversation
    first = _get(actor, limit=2)
    second = _get(actor, limit=2, after_cursor=first['next_after_cursor'])
    previous = _get(actor, limit=2, before_cursor=second['prev_before_cursor'])
    assert _ids(first) == _ids(previous) == expected[:2]
    assert _ids(second) == expected[2:4]
    assert 'page' not in first and 'total_pages' not in first
    assert first['published_total'] == first['conversation']['message_count'] == 8
    around = _get(actor, around_seq=30, limit=2)
    assert [message['sequence'] for message in around['messages']] == [10, 30]
    assert 'page' not in around


@pytest.mark.parametrize('cursor', [
    {'after_seq': 0}, {'before_seq': 50},
    {'after_cursor': {'seq': 10, 'id': 'source-id'}},
    {'before_cursor': {'seq': 10, 'id': 'source-id'}},
])
def test_page_rejects_mixed_cursor_arguments(conversation, cursor):
    actor, _ = conversation
    with pytest.raises(MariposaError) as error:
        _get(actor, page=1, **cursor)
    assert error.value.code == 'INVALID_ARGUMENT'
    assert 'page' in str(error.value)


@pytest.mark.parametrize('page', [0, -1, True, 1.5, '2'])
def test_page_rejects_invalid_types_and_values(page):
    with pytest.raises(MariposaError):
        input_schemas.validate('source.conversation.get',
                               {'conversation_id': 'page-conv', 'page': page})
    with pytest.raises(MariposaError) as error:
        query.get_conversation('page-conv', page=page)
    assert error.value.code == 'INVALID_ARGUMENT'


def test_cursor_schema_validates_composite_shape():
    for cursor in [{'seq': True, 'id': 'message'}, {'seq': 1},
                   {'seq': 1, 'id': ''}, {'seq': 1, 'id': 'm', 'extra': 1}]:
        with pytest.raises(MariposaError):
            input_schemas.validate('source.conversation.get',
                                   {'conversation_id': 'c', 'after_cursor': cursor})


def test_conversation_pages_have_stable_order_when_dates_tie(actors, tmp_path):
    _import(tmp_path, [{'uuid': f'tie-c{i}', 'name': f'Conversation {i}',
                       'chat_messages': [_message(f'tie-m{i}')]}
                      for i in range(3)])
    for order in ('first_desc', 'first_asc', 'updated_desc'):
        all_rows = query.conversations_list(order=order)['conversations']
        expected = sorted(row['id'] for row in all_rows)
        actual = [query.conversations_list(order=order, limit=1, offset=offset)
                  ['conversations'][0]['id'] for offset in range(3)]
        assert actual == expected


def test_list_count_and_visibility_match_message_pages(conversation):
    actor, _ = conversation
    result = query.conversations_list()
    assert result['total'] == 1
    assert result['conversations'][0]['message_count'] == _get(actor, page=1)['total'] == 8
    with db.formal() as conn:
        conn.execute('UPDATE source_messages SET live_superseded=1')
    assert query.conversations_list()['total'] == 0
    assert query.conversations_list()['conversations'] == []
    assert _get(actor, page=1)['total'] == 0


def test_http_page_contract_and_mixed_cursor_error(conversation):
    from fastapi.testclient import TestClient
    from mariposa.app import app
    from tests.conftest import TOKENS

    _, expected = conversation
    headers = {'Authorization': 'Bearer ' + TOKENS['jiaming']}
    with TestClient(app) as client:
        response = client.post('/api/capability/source.conversation.get',
                               headers=headers, json={'arguments': {
                                   'conversation_id': 'page-conv',
                                   'page': 1, 'around_seq': 50, 'limit': 2}})
        assert response.status_code == 200, response.text
        data = response.json()['data']
        assert (data['page'], data['total'], data['page_size'],
                data['total_pages']) == (3, 8, 2, 4)
        assert _ids(data) == expected[4:6]
        response = client.post('/api/capability/source.conversation.get',
                               headers=headers, json={'arguments': {
                                   'conversation_id': 'page-conv', 'limit': 2,
                                   'after_cursor': data['next_after_cursor']}})
        assert response.status_code == 200, response.text
        assert _ids(response.json()['data']) == expected[-2:]
        response = client.post('/api/capability/source.conversation.get',
                               headers=headers, json={'arguments': {
                                   'conversation_id': 'page-conv', 'page': 1,
                                   'after_cursor': data['next_after_cursor']}})
        assert response.status_code == 400, response.text
        assert response.json()['error']['code'] == 'INVALID_ARGUMENT'
        response = client.post('/api/capability/source.conversation.get',
                               headers=headers, json={'arguments': {
                                   'conversation_id': 'page-conv', 'page': 0}})
        assert response.status_code == 403, response.text
        assert response.json()['error']['code'] == 'SCHEMA_VIOLATION'


def test_mcp_discovery_and_call_use_runtime_page_schema(conversation):
    from fastapi.testclient import TestClient
    from mariposa.app import app
    from tests.conftest import TOKENS

    _, expected = conversation
    headers = {'Authorization': 'Bearer ' + TOKENS['jiaming']}
    with TestClient(app) as client:
        response = client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        assert response.status_code == 200, response.text
        tool = next(tool for tool in response.json()['result']['tools']
                    if tool['name'] == 'mariposa_source_conversation_get')
        schema = tool['inputSchema']
        runtime = input_schemas.schema_for('source.conversation.get')
        assert schema['properties']['page'] == runtime['properties']['page']
        assert schema['properties']['after_cursor'] == runtime['properties']['after_cursor']
        assert schema['$defs']['cursor'] == runtime['$defs']['cursor']
        response = client.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
            'params': {'name': 'mariposa_source_conversation_get', 'arguments': {
                'conversation_id': 'page-conv', 'page': 3, 'limit': 2}}})
        assert response.status_code == 200, response.text
        result = response.json()['result']
        assert result['isError'] is False
        data = result['structuredContent']['data']
        assert (data['page'], data['total'], data['page_size'],
                data['total_pages']) == (3, 8, 2, 4)
        assert _ids(data) == expected[4:6]
