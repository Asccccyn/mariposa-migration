"""Minimal reading JSON; full records and operational receipts stay server-side."""
from copy import deepcopy


def present(value):
    return value is not None and value != '' and value != [] and value != {}


def pick(value, keys):
    return {k: deepcopy(value[k]) for k in keys if k in value and present(value[k])}


def mood_view(value):
    if not isinstance(value, dict) or not (value.get('text') or value.get('tags')):
        return None
    return pick(value, ('text', 'tags', 'author'))


def memory_view(value):
    date = value.get('memory_date')
    out = pick(value, ('memory_id', 'version'))
    if isinstance(date, str) and date.strip():
        out['date'] = date
    else:
        out['date_unknown'] = True
    if value.get('original_title'): out['title'] = value['original_title']
    out.update(pick(value, ('text', 'categories', 'view_receipt', 'content_gap')))
    mood = mood_view(value.get('mood'))
    if mood: out['mood'] = mood
    return out


def hold_view(receipt):
    if not receipt.get('memory_id'):
        raise ValueError('hold receipt has no memory identity')
    return {'saved': True, 'memory_id': receipt['memory_id']}


def fragment(value, out):
    if value.get('partial_item'):
        out.update(pick(value, ('partial_item', 'item_index', 'text_range')))
    return out


def section_view(name, value, historical_ids=()):
    if value is None: return None
    if name == 'categories': return deepcopy(value)
    if name == 'mood':
        if isinstance(value, list):
            return [fragment(v, mood_view(v) or {}) for v in value if v.get('text') or v.get('tags')]
        return mood_view(value)
    if name == 'our_words':
        return [fragment(v, pick(v, ('speaker', 'text', 'source_gap'))) for v in value
                if isinstance(v.get('text'), str) and v['text'].strip()]
    if name == 'source':
        # Explicit third-layer results are flat messages, not internal binding rows.
        return [fragment(v, pick(v, ('range_index', 'conversation_id', 'conversation_title',
            'message_id', 'speaker', 'normalized_sender', 'occurred_date', 'occurred_at', 'text', 'attachments',
            'char_offset_start', 'char_offset_end', 'changed', 'unavailable', 'reason')))
            for v in value if 'message_id' in v or v.get('unavailable')]
    if name == 'recollections':
        result = []
        for v in value:
            if not isinstance(v.get('text'), str) or not v['text'].strip(): continue
            item = fragment(v, pick(v, ('recollection_id', 'author', 'text')))
            if v.get('recollection_id') in historical_ids: item['historical'] = True
            result.append(item)
        return result
    if name == 'keeps':
        def one(v):
            owners = v.get('active_keepers') or [m['owner'] for m in v.get('marks', [])
                      if m.get('owner') and not m.get('revoked_at')]
            marks = [pick(m, ('mark_id', 'owner', 'recollection_id')) for m in v.get('marks', []) if not m.get('revoked_at')]
            return {'marks': marks} if marks else ({'by': list(dict.fromkeys(owners))} if owners else None)
        return [one(v) for v in value if one(v)] if isinstance(value, list) else one(value)
    if name == 'relations':
        result = []
        for v in value:
            if v.get('domain') in ('source_binding', 'word_source'): continue
            item = pick(v, ('relation_id', 'domain', 'direction', 'relation_type', 'custom_label', 'reverse_label', 'text', 'unavailable'))
            if v.get('confidence') not in (None, 'exact', 'human', 'confirmed'):
                item['uncertain'] = True
            other = v.get('other')
            if isinstance(other, dict):
                target = {}
                for key in ('memory_id', 'plan_id', 'item_id', 'word_id', 'id'):
                    if other.get(key):
                        target[key] = other[key]
                        break
                if other.get('item_id') and 'revision' in other:
                    target['revision'] = other['revision']
                for key in ('title', 'original_title', 'name'):
                    if other.get(key):
                        target['title'] = other[key]
                        break
                if other.get('date'): target['date'] = other['date']
                if target: item['target'] = target
            if item: result.append(fragment(v, item))
        return result
    return None


def detail_view(page, historical_ids=()):
    sections = page.get('sections', {})
    out = pick(page, ('memory_id',))
    statuses = page.get('section_status', {})
    if 'memory' in sections and sections['memory'] is not None:
        memory = sections['memory']
        out.update(memory_view(memory))
        span = memory.get('text_range')
        if span and (span[0] > 0 or statuses.get('memory') == 'partial'):
            out['text_range'] = deepcopy(span)
    for name, value in sections.items():
        if name == 'memory': continue
        projected = section_view(name, value, historical_ids)
        if present(projected): out[name] = projected
    issues = {k:v for k,v in statuses.items() if (k != 'source' or any('message_id' in x or x.get('unavailable') for x in sections.get('source') or [])) and v in ('partial','unavailable')}
    if issues: out['incomplete_sections'] = issues
    if page.get('complete') is False: out['complete'] = False
    if page.get('next_cursor'): out['next_cursor'] = page['next_cursor']
    if page.get('changed_since_surface') is True: out['changed_since_surface'] = True
    return out
