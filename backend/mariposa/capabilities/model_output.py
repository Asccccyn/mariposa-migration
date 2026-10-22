"""Explicit model-only contracts; canonical API/operation receipts stay intact."""
from copy import deepcopy
from ..memory.model_projection import pick, memory_view, hold_view

REQUEST = ('request_id', 'memory_id', 'human_reason', 'status', 'rejection_reason')
RELATION = ('relation_id', 'from_memory', 'to_memory', 'relation_type',
            'custom_label', 'reverse_label', 'reversed')


def episode(v):
    out = pick(v, ('episode_id', 'label', 'state', 'revision', 'current_revision', 'current_state', 'last_reason_code'))
    def ref(x):
        return pick(x, ('source_message_id', 'content_hash', 'char_offset'))
    if v.get('source_selections'):
        out['source_selections'] = [{**pick(x, ('conversation_id', 'start_char_offset', 'end_char_offset')),
            'members': [ref(m) for m in x.get('members', [])]} for x in v['source_selections']]
    if v.get('terminal_source_ref'):
        out['terminal_source_ref'] = ref(v['terminal_source_ref'])
    if v.get('decision_source_refs'):
        out['decision_source_refs'] = [ref(x) for x in v['decision_source_refs']]
    return out


def _project_core(capability, v, arguments):
    if not isinstance(v, dict):
        return v
    if capability in ('memory.get', 'memory.open'):
        return {**memory_view(v), **pick(v, ('version',))} if 'memory_date' in v else v
    if capability == 'memory.update':
        return pick(v, ('memory_id', 'version', 'word_ids'))
    if capability in ('memory.delete', 'memory.deletion.direct'):
        return pick(v, ('memory_id', 'deleted'))
    if capability == 'source.binding.bind':
        return pick(v, ('binding_id', 'memory_id'))
    if capability in ('source.binding.list', 'source.bindings.of_conversation'):
        return {'bindings': [pick(x, ('binding_id', 'memory_id', 'conversation_id',
                'start_message_id', 'end_message_id', 'start_char_offset', 'end_char_offset',
                'bind_confidence')) for x in v.get('bindings', [])]}
    if capability == 'source.binding.correct':
        return pick(v, ('removed_binding_id', 'replacement_binding_id', 'action'))
    if capability == 'memory.hold.status':
        receipt = v.get('receipt') or {}
        if v.get('completed') is not True or not receipt.get('memory_id'):
            return {'status': 'unconfirmed'}
        out = hold_view(receipt)
        current = v.get('memory_current') or {}
        if current.get('exists') is False:
            out['deleted'] = True
        elif current.get('visibility') == 'hidden':
            out['hidden'] = True
        return out
    if capability == 'memory.hold':
        return hold_view(v)
    if capability in ('episode.get', 'episode.apply'):
        return episode(v)
    if capability == 'episode.list':
        out = {'episodes': [episode(x) for x in v.get('episodes', [])]}
        if v.get('next_cursor'):
            out['next_cursor'] = deepcopy(v['next_cursor'])
        return out
    if capability in ('memory.deletion.get', 'memory.deletion.list', 'memory.deletion.decide', 'memory.deletion.request', 'memory.deletion.withdraw'):
        return {'requests': [pick(x, REQUEST) for x in v['requests']]} if 'requests' in v else pick(v, REQUEST)
    if capability == 'memory.relations.list':
        return {'relations': [pick(x, RELATION) for x in v.get('relations', [])]}
    if capability == 'memory.relations.link':
        return pick(v, ('relation_id', 'from', 'to', 'relation_type'))
    if capability == 'memory.relations.correct':
        return pick(v, ('action', 'removed_relation_id', 'replacement_relation_id'))
    if capability in ('memory.recollections.append', 'memory.recollections.revise'):
        out = pick(v, ('memory_id', 'recollection_id', 'supersedes'))
        if v.get('keep'):
            out['keep'] = pick(v['keep'], ('mark_id', 'owner', 'recollection_id'))
        return out
    if capability == 'memory.our_words.append':
        return pick(v, ('memory_id', 'word_ids', 'next_ordinal'))
    if capability == 'memory.our_words.source.correct':
        return pick(v, ('word_id', 'removed_source_ref', 'new_source_ref', 'source_binding_version', 'action'))
    if capability == 'memory.recollections.list':
        return {'recollections': [pick(x, ('recollection_id', 'author', 'text', 'supersedes')) for x in v.get('recollections', [])]}
    if capability == 'memory.our_words.list':
        return {'words': [pick(x, ('word_id', 'speaker', 'text', 'source_ref', 'source_binding_version')) for x in v.get('words', [])]}
    if capability == 'memory.view.confirm':
        return {'confirmed': True, **pick(v, ('memory_id', 'receipt_id'))}
    if capability == 'memory.keep.revoke':
        return pick(v, ('mark_id', 'status'))
    if capability == 'memory.keeps.list':
        marks = []
        for x in v.get('marks', []):
            mark = pick(x, ('mark_id', 'owner', 'recollection_id'))
            if x.get('revoked_at'):
                mark['revoked'] = True
            marks.append(mark)
        return {'marks': marks}
    return v


def plan(v):
    return pick(v, ('plan_id', 'version', 'title', 'content', 'state', 'starts_at',
                    'due_at', 'date_start', 'date_end', 'timezone', 'all_day', 'weight'))


def i_record(v, history=False):
    out = pick(v, ('item_id', 'revision', 'current_revision', 'content', 'change_reason',
                   'informed_by_revision', 'restored_from_revision', 'is_current'))
    if history:
        out.update(pick(v, ('authored_by', 'created_at', 'change_type', 'based_on_revision', 'version_no')))
    if v.get('relations'):
        out['relations'] = [pick(x, ('memory_id', 'relation_type')) for x in v['relations']]
    return out


def recall(v):
    out = pick(v, ('recall_session_id', 'revision'))  # refine/accept/reject arguments
    out['candidates'] = []
    for c in v.get('candidates', []):
        item = pick(c, ('candidate_ref', 'memory_id', 'word_id', 'plan_id', 'memory_date',
                        'original_title', 'text', 'speaker', 'excerpt', 'source_ref',
                        'source_binding_version', 'version_receipt'))
        evidence = []
        for e in c.get('evidence', []):
            ev = pick(e, ('field', 'snippet', 'source_ref'))
            if e.get('structured_value') is not None:
                ev['structured_value'] = pick(e['structured_value'], ('categories',
                    'legacy_content_gap', 'source_ref_state', 'source_binding_version'))
            if e.get('truncated'):
                ev['truncated'] = True
            if e.get('evidence_kind') == 'word_unverified':
                ev['unverified'] = True
            evidence.append(ev)
        if evidence:
            item['evidence'] = evidence
            if item.get('excerpt') == evidence[0].get('snippet'):
                item.pop('excerpt', None)
        if c.get('truncated'):
            item['truncated'] = True
        judge = c.get('judge') or {}
        if isinstance(judge, dict) and judge.get('evaluation_status') not in (None, 'evaluated'):
            item['judgement'] = 'unconfirmed'
        out['candidates'].append(item)
    if v.get('judge_mode') == 'off':
        out['judgement'] = 'unjudged'
    elif v.get('judgement_status') not in (None, 'evaluated', 'complete'):
        out['judgement'] = v['judgement_status']
    coverage = v.get('retrieval_coverage') or v.get('coverage') or {}
    if coverage.get('truncated') or coverage.get('retrieval_scope_exhaustive') is False:
        out['coverage'] = 'incomplete'
    if v.get('search_status') not in (None, 'FOUND'):
        out['status'] = v['search_status']
    if v.get('invalidated'):
        out['invalidated'] = True
    for k in ('missing', 'conflicts', 'degraded_reasons'):
        if v.get(k):
            out[k] = deepcopy(v[k])
    p = v.get('pagination') or {}
    if p.get('has_more'):
        out['pagination'] = pick(p, ('result_set_id', 'next_cursor'))
    if v.get('continuation'):
        out['continuation'] = pick(v['continuation'], ('continue_request_ref', 'action', 'reason'))
    return out


def project(capability, v, arguments):
    if capability == 'memory.search' and isinstance(v, list):
        return [memory_card(x) for x in v]
    if not isinstance(v, dict):
        return v
    if capability in ('memory.list', 'memory.by_date', 'memory.by_category', 'memory.by_emotion', 'memory.recall'):
        key = 'hits' if 'hits' in v else 'items'
        out = {key: [memory_card(x) for x in v.get(key, [])]}
        if v.get('next_cursor'):
            out['next_cursor'] = deepcopy(v['next_cursor'])
        elif v.get('has_more'):
            out['incomplete'] = True
        if v.get('pool_truncated') or v.get('degraded'):
            out['coverage'] = 'incomplete'
        return out
    if capability == 'memory.cards.batch':
        cards = {}
        for mid, card in v.get('cards', {}).items():
            x = {}
            if card.get('memory'):
                x['memory'] = memory_card(card['memory'])
                x['memory'].pop('memory_id', None)
            for name, fields in (
                ('words', ('word_id', 'speaker', 'text', 'source_ref', 'source_binding_version')),
                ('recollections', ('recollection_id', 'author', 'text', 'supersedes')),
                ('bindings', ('binding_id', 'conversation_id', 'start_message_id', 'end_message_id')),
                ('relations', ('relation_id', 'direction', 'other_memory', 'relation_type', 'custom_label', 'reverse_label'))):
                if card.get(name):
                    x[name] = [pick(y, fields) for y in card[name]]
            cards[mid] = x
        return {'cards': cards}
    if capability in ('plan.get', 'plan.create', 'plan.update', 'plan.complete', 'plan.cancel'):
        return plan(v)
    if capability == 'plan.list':
        return {'plans': [plan(x) for x in v.get('plans', [])]}
    if capability in ('i.get', 'i.items.list'):
        if v.get('items'):
            return {'items': [i_record(x) for x in v['items']]}
        return pick(v, ('content',)) if v.get('content') else {'items': []}
    if capability in ('i.item.get', 'i.item.create', 'i.item.revise', 'i.item.restore'):
        return i_record(v)
    if capability == 'i.item.history':
        return {**pick(v, ('item_id', 'current_revision')), 'revisions': [i_record(x, True) for x in v.get('revisions', [])]}
    if capability == 'i.versions.read':
        return {'versions': [i_record(x, True) for x in v.get('versions', [])]}
    if capability in ('memory.recall.start', 'memory.recall.refine', 'memory.recall.status',
                       'memory.recall.navigate', 'memory.recall.round2', 'memory.recall.page',
                       'memory.words.recall', 'memory.find_words'):
        return recall(v)
    if capability in ('bootstrap.get', 'bootstrap.next'):
        return bootstrap(v)
    if capability in ('source.search', 'source.message.get', 'source.range.open',
                       'source.conversation.get', 'source.conversations.list', 'source.selection.open', 'source.memory.open'):
        return source_result(v, arguments)
    return _project_core(capability, v, arguments)


def source_message(v):
    # Native message/conversation IDs address source.message.get/source.range.open.
    out = pick(v, ('message_id', 'conversation_id', 'provider', 'speaker', 'occurred_date', 'text',
                   'attachments', 'thinking_text', 'thinking_summaries', 'memories_referencing', 'char_offset_start', 'char_offset_end'))
    if not v.get('speaker'):
        out.update(pick(v, ('normalized_sender',)))
    if v.get('excerpt') and v['excerpt'] not in (v.get('text') or ''):
        out['excerpt'] = v['excerpt']
    if v.get('excerpt_locator') == 'failed':
        out['excerpt_unlocated'] = True
    if v.get('version_drift'):
        out['changed'] = True
    if v.get('sibling_branch'):
        out['sibling_branch'] = True
    if v.get('published') is False:
        out['unpublished'] = True
    if 'content_json' in v:
        # Only an explicit include_content request returns original provider evidence.
        out['content_json'] = deepcopy(v['content_json'])
    return out


def source_result(v, arguments):
    out = {}
    if 'message' in v:
        out['message'] = source_message(v['message'])
    for key in ('hits', 'messages', 'members', 'context_before', 'context_after', 'off_path_messages'):
        if key in v and (v[key] or key in ('hits', 'messages', 'members')):
            out[key] = [source_message(x) for x in v[key]]
    if 'ranges' in v:
        out['memory_id'] = v['memory_id']
        out['ranges'] = []
        for r in v['ranges']:
            value = source_result(r, arguments)
            value.update(pick(r, ('binding_id', 'status', 'reason')))
            if r.get('version_drift'):
                value['changed'] = True
            if r.get('confidence') not in (None, 'exact', 'human', 'confirmed'):
                value['uncertain'] = True
            out['ranges'].append(value)
    def conversation(x):
        return pick(x, ('id', 'provider', 'title', 'summary'))
    if v.get('conversation'):
        out['conversation'] = conversation(v['conversation'])
    if 'conversations' in v:
        out['conversations'] = [conversation(x) for x in v['conversations']]
    if v.get('has_more'):
        if v.get('next_after_cursor'):
            out['next_after_cursor'] = deepcopy(v['next_after_cursor'])
        elif 'offset' in v and 'limit' in v:
            out['next_offset'] = v['offset'] + v['limit']
        else:
            out['incomplete'] = True
    if v.get('prev_before_cursor'):
        out['prev_before_cursor'] = deepcopy(v['prev_before_cursor'])
    return out


def bootstrap(v):
    def cursor(value):
        return isinstance(value, dict) and any(x is not None for x in value.values())
    def item(x):
        out = pick(x, ('memory_id', 'plan_id', 'original_title', 'memory_date', 'categories',
                       'mood_tags', 'mood_text', 'event_text', 'title', 'content', 'detail', 'state',
                       'starts_at', 'due_at', 'date_start', 'date_end', 'all_day', 'weight'))
        if x.get('our_words'):
            out['our_words'] = [pick(w, ('speaker', 'texts')) for w in x['our_words']]
        for key in ('event_text_next', 'our_words_next', 'mood_text_next', 'content_next_cursor'):
            if cursor(x.get(key)):
                out[key] = deepcopy(x[key])
        for key in ('event_text_truncated', 'our_words_truncated', 'mood_text_truncated', 'content_truncated'):
            if x.get(key):
                out[key] = True
        if x.get('relations'):
            out['relations'] = pick(x['relations'], ('types',))
        if x.get('late_entry'):
            out['late_entry'] = True
        return out
    def section(x):
        out = {}
        if 'items' in x:
            out['items'] = [item(y) for y in x['items']]
        if cursor(x.get('next_cursor')):
            out['next_cursor'] = deepcopy(x['next_cursor'])
        return out
    out = pick(v, ('section', 'memory_id', 'plan_id', 'field', 'content', 'offset', 'item_offset', 'char_offset'))
    if v.get('time'):
        out['time'] = pick(v['time'], ('local_date', 'timezone'))
    for name in ('memory_days', 'plans'):
        if v.get(name):
            out[name] = section(v[name])
    if v.get('i'):
        out['i'] = pick(v['i'], ('content', 'note'))
        if cursor(v['i'].get('next_cursor')):
            out['i']['next_cursor'] = deepcopy(v['i']['next_cursor'])
    if 'items' in v:
        out['items'] = ([pick(x, ('speaker', 'texts')) for x in v['items']]
                        if v.get('field') == 'our_words' else [item(x) for x in v['items']])
    if cursor(v.get('next_cursor')):
        out['next_cursor'] = deepcopy(v['next_cursor'])
    def has_cursor(x):
        if isinstance(x, dict):
            return any((k.endswith('_next') or k.endswith('cursor')) and cursor(val)
                       or has_cursor(val) for k, val in x.items())
        if isinstance(x, list):
            return any(has_cursor(y) for y in x)
        return False
    if has_cursor(out):
        out.update(pick(v, ('snapshot_id',)))
    if v.get('unchanged') is True:
        out['unchanged'] = True
    return out


def memory_card(v):
    out = pick(v, ('memory_id', 'version', 'memory_version', 'memory_date', 'original_title',
                   'text', 'excerpt', 'categories', 'mood_tags', 'content_gap'))
    if v.get('mood'):
        out['mood'] = pick(v['mood'], ('text', 'tags', 'author'))
    if v.get('forgotten'):
        out['content_gap'] = True
    return out
