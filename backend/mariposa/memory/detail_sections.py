"""Three-tier reads reuse bucket/source/relation services, never retrieval or JEV."""
from ..source import binding as source_binding
from ..relations import routing
from ..plans import service as plans
from ..errors import NotFound
from . import service as memory, our_words


def build(memory_id, section, conn):
    if section == 'memory':
        full = memory.get(conn, memory_id)
        values = {'memory': full, 'categories': full.get('categories', []),
                  'mood': full.get('mood'), 'our_words': our_words.list_for(memory_id)}
    elif section == 'source':
        # The exact service used by the existing bucket's “view original” button.
        opened = source_binding.open_for_memory(memory_id)
        values = {'source': []}
        if opened.get('event_source_status') == 'unbound':
            values['source'].append({'unavailable': True, 'reason': 'EVENT_SOURCE_UNBOUND'})
        for index, rng in enumerate(opened['ranges']):
            common = {'range_index': index}
            conversation = rng.get('conversation') or {}
            if conversation.get('id'): common['conversation_id'] = conversation['id']
            if conversation.get('title'): common['conversation_title'] = conversation['title']
            if rng.get('version_drift'): common['changed'] = True
            if rng.get('status') == 'unresolvable':
                values['source'].append({**common, 'unavailable': True, 'reason': rng.get('reason', 'SOURCE_UNAVAILABLE')})
                continue
            for msg in rng.get('messages') or []:
                item = {**common, 'message_id': msg['message_id'], 'text': msg.get('text') or ''}
                for key in ('speaker', 'normalized_sender', 'occurred_date', 'attachments',
                            'char_offset_start', 'char_offset_end'):
                    if msg.get(key) is not None and msg.get(key) != []: item[key] = msg[key]
                if msg.get('created_at'):
                    # Source created_at is the original message timestamp, not
                    # the binding/import audit timestamp. Preserve its precision.
                    item['occurred_at'] = msg['created_at']; item.pop('occurred_date', None)
                if item.get('speaker'): item.pop('normalized_sender', None)
                values['source'].append(item)
    else:
        edges = []; offset = 0
        while True:
            batch = routing.list_relations({'memory_id': memory_id, 'direction': 'both', 'limit': 200, 'offset': offset}).get('relations', [])
            edges.extend(batch)
            if len(batch) < 200: break
            offset += 200
        values = {'relations': []}
        for edge in edges:
            if edge.get('domain') in ('source_binding', 'word_source'): continue
            other = dict(edge.get('other') or {})
            item = {k: v for k, v in edge.items() if k in ('relation_id', 'domain', 'direction', 'relation_type', 'custom_label', 'reverse_label', 'confidence')}
            try:
                if other.get('memory_id'):
                    target = memory.get(conn, other['memory_id'])
                    other.update(title=target.get('original_title'), date=target.get('memory_date'))
                    item['text'] = target.get('text') or ''
                elif other.get('plan_id'):
                    target = plans.get(conn, other['plan_id'])
                    other['title'] = target.get('title'); item['text'] = target.get('content') or ''
                elif other.get('item_id'):
                    target = conn.execute('SELECT content FROM i_item_revisions WHERE item_id=? AND revision=?', (other['item_id'], other.get('revision'))).fetchone()
                    if target is None: item['unavailable'] = True
                    else: item['text'] = target['content']
            except NotFound:
                item['unavailable'] = True
            item['other'] = other; values['relations'].append(item)
    status = {k: ('complete' if v else 'empty') for k, v in values.items()}
    if section == 'source' and any(x.get('unavailable') for x in values['source']): status['source'] = 'partial'
    return values, status


def fingerprint(conn, memory_id, section):
    """Internal snapshot guard for changes not covered by the bucket version."""
    import hashlib
    import json
    digest = hashlib.sha256()
    queries = []
    if section == 'source':
        queries = [
            ('SELECT s.* FROM memory_source_spans s JOIN memory_source_bindings b ON b.binding_id=s.binding_id WHERE b.memory_id=? ORDER BY s.binding_id', (memory_id,)),
            ('SELECT * FROM memory_source_bindings WHERE memory_id=? ORDER BY binding_id', (memory_id,)),
            ('SELECT id, content_hash, text, published, live_superseded FROM source_messages WHERE conversation_id IN (SELECT conversation_id FROM memory_source_bindings WHERE memory_id=?) ORDER BY id', (memory_id,)),
        ]
    elif section == 'relations':
        queries = [
            ('SELECT * FROM memory_relations WHERE from_memory=? OR to_memory=? ORDER BY relation_id', (memory_id, memory_id)),
            ('SELECT memory_id,current_version_no,updated_at,visibility,memory_date FROM memories WHERE memory_id IN (SELECT to_memory FROM memory_relations WHERE from_memory=? UNION SELECT from_memory FROM memory_relations WHERE to_memory=?) ORDER BY memory_id', (memory_id, memory_id)),
            ('SELECT * FROM plan_memory_links WHERE memory_id=? ORDER BY link_id', (memory_id,)),
            ('SELECT id,current_version_no,state FROM plans WHERE id IN (SELECT plan_id FROM plan_memory_links WHERE memory_id=?) ORDER BY id', (memory_id,)),
            ('SELECT r.relation_id,r.item_id,r.revision,r.relation_type,v.content FROM i_revision_memory_relations r LEFT JOIN i_item_revisions v ON v.item_id=r.item_id AND v.revision=r.revision WHERE r.memory_id=? ORDER BY r.relation_id', (memory_id,)),
        ]
    for sql, args in queries:
        digest.update(sql.encode())
        for row in conn.execute(sql, args):
            digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(',', ':')).encode());digest.update(b'\n')
    return digest.hexdigest()
