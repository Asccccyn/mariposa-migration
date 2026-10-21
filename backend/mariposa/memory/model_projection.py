"""Model-facing views only. Formal records, receipts and full owner views stay intact."""
from copy import deepcopy

def pick(value, keys):
    return {k: deepcopy(value[k]) for k in keys if k in value}

FRAGMENT = ('partial_item', 'item_index', 'text_range')

def memory_view(value):
    out = pick(value, ('memory_id', 'original_title', 'text', 'categories',
                       'view_receipt', 'content_gap', 'text_range'))
    out['date'] = value.get('memory_date') if value.get('date_confidence') == 'exact' else None
    if value.get('occurred_start') or value.get('occurred_end'):
        out['date_range'] = [value.get('occurred_start'), value.get('occurred_end')]
    if value.get('mood'):
        out['mood'] = pick(value['mood'], ('text', 'tags', 'author', 'mood_written_at'))
    return out

def hold_view(receipt):
    if not receipt.get('memory_id'):
        raise ValueError('hold receipt has no memory identity')
    return {'saved': True, 'memory_id': receipt['memory_id']}

def section_view(name, value):
    if value is None: return None
    if name == 'memory': return memory_view(value)
    if name == 'categories': return deepcopy(value)
    if name == 'mood':
        if isinstance(value, list): return [pick(v, ('text','tags','author','mood_written_at')+FRAGMENT) for v in value]
        return pick(value, ('text','tags','author','mood_written_at')+FRAGMENT)
    if name == 'our_words':
        return [pick(v, ('word_id','speaker','text','source_ref')+FRAGMENT) for v in value]
    if name == 'recollections':
        # These identify edits/history; do not make superseded text look current.
        return [pick(v, ('recollection_id','author','text','supersedes','written_at')+FRAGMENT) for v in value]
    if name == 'keeps':
        def one(v):
            result = pick(v, ('active_keepers',)+FRAGMENT)
            if 'marks' in v:
                result['marks'] = [pick(mark, ('mark_id','owner','scope','revoked_at')) for mark in v['marks']]
            return result
        return [one(v) for v in value] if isinstance(value,list) else one(value)
    if name == 'source':
        out=[]
        for v in value:
            item=pick(v, ('binding_id','state')+FRAGMENT)
            if 'messages' in v:
                item['messages'] = None if v['messages'] is None else [pick(m, ('message_id','speaker','occurred_date','text','attachments')) for m in v['messages']]
            out.append(item)
        return out
    if name == 'relations':
        out=[]
        for v in value:
            item=pick(v, ('relation_id','domain','direction','confidence')+FRAGMENT)
            if 'other' in v: item['other']=deepcopy(v['other']) # target identity needed for navigation/correction
            out.append(item)
        return out
    raise ValueError('unknown memory section')

def detail_view(page):
    out=pick(page, ('memory_id','complete','next_cursor','section_status','changed_since_surface'))
    out['sections']={name:section_view(name,value) for name,value in page.get('sections',{}).items()}
    return out
