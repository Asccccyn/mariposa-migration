"""HTTP/MCP source-search filters work with an omitted keyword."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from mariposa.app import app
from mariposa.capabilities import input_schemas
from mariposa.source import importer
from tests.conftest import TOKENS


@pytest.fixture()
def client(actors, tmp_path):
    messages = [
        ('filter-early', 'human', '2026-07-01T02:00:00Z', 'alpha'),
        # July 2 in UTC is July 3 in the relationship timezone (Asia/Shanghai).
        ('filter-assistant', 'assistant', '2026-07-02T18:00:00Z', 'beta'),
        ('filter-human', 'human', '2026-07-03T04:00:00Z', 'gamma'),
    ]
    path = tmp_path / 'filter-contract.json'
    path.write_text(json.dumps([{
        'uuid': 'filter-conv', 'name': 'Filter contract',
        'chat_messages': [
            {'uuid': message_id, 'sender': sender, 'created_at': created,
             'content': [{'type': 'text', 'text': text}]}
            for message_id, sender, created, text in messages],
    }]), encoding='utf-8')
    assert importer.import_file('jiaming', str(path))['status'] == 'completed'
    with TestClient(app) as client:
        client.headers.update({'Authorization': 'Bearer ' + TOKENS['jiaming']})
        yield client


def _ids(data):
    return [hit['provider_message_id'] for hit in data['hits']]


@pytest.mark.parametrize(('arguments', 'expected'), [
    ({}, ['filter-human', 'filter-assistant', 'filter-early']),
    ({'senders': ['assistant']}, ['filter-assistant']),
    ({'senders': ['human']}, ['filter-human', 'filter-early']),
    ({'date_from': '2026-07-03', 'date_to': '2026-07-03'},
     ['filter-human', 'filter-assistant']),
    ({'date_from': '2026-07-02'}, ['filter-human', 'filter-assistant']),
    ({'date_to': '2026-07-01'}, ['filter-early']),
    ({'senders': ['human'], 'date_from': '2026-07-03', 'date_to': '2026-07-03'},
     ['filter-human']),
    ({'query': 'gamma'}, ['filter-human']),
    ({'query': 'gamma', 'senders': ['assistant']}, []),
])
def test_http_optional_keyword_preserves_filters_and_keyword_search(client, arguments, expected):
    response = client.post('/api/capability/source.search',
                           json={'arguments': arguments})
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert _ids(data) == expected
    assert data['senders'] == arguments.get('senders', ['human', 'assistant'])


def test_http_empty_filters_still_honor_limit_and_offset(client):
    first = client.post('/api/capability/source.search',
                        json={'arguments': {'limit': 1}})
    assert first.status_code == 200, first.text
    assert _ids(first.json()['data']) == ['filter-human']
    assert first.json()['data']['has_more'] is True
    second = client.post('/api/capability/source.search',
                         json={'arguments': {'limit': 1, 'offset': 1}})
    assert second.status_code == 200, second.text
    assert _ids(second.json()['data']) == ['filter-assistant']


@pytest.mark.parametrize('query', ['', None, 123, 'a' * 2001])
def test_provided_keyword_keeps_existing_validation(client, query):
    response = client.post('/api/capability/source.search',
                           json={'arguments': {'query': query}})
    assert response.status_code == 403, response.text
    assert response.json()['error']['code'] == 'SCHEMA_VIOLATION'


def test_mcp_schema_and_call_allow_filter_only_search(client):
    response = client.post('/mcp', json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    assert response.status_code == 200, response.text
    tool = next(tool for tool in response.json()['result']['tools']
                if tool['name'] == 'mariposa_source_search')
    schema = tool['inputSchema']
    assert 'query' not in schema.get('required', [])
    assert schema['properties']['query'] == input_schemas.schema_for(
        'source.search')['properties']['query']
    response = client.post('/mcp', json={
        'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
        'params': {'name': 'mariposa_source_search', 'arguments': {
            'senders': ['assistant'], 'date_from': '2026-07-03',
            'date_to': '2026-07-03', 'output_profile': 'legacy'}}})
    assert response.status_code == 200, response.text
    result = response.json()['result']
    assert result['isError'] is False
    data = json.loads(result['content'][0]['text'])['data']
    assert [hit['text'] for hit in data['hits']] == ['beta']
    assert all(hit['message_id'] for hit in data['hits'])
