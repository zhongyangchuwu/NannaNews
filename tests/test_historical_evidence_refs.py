"""Source-bound historical transport removes copying, never literal admission."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path


import pytest
from jsonschema import Draft202012Validator



from nannanews import acquisition
from nannanews import bulletin_date_repair as repair
from nannanews import historical_evidence_refs as refs
from test_activity_content import captured_image
from test_bulletin_date_repair import (
    SOURCE_TEXT, SOURCE_URL, _model_response, database, reference_response, source_snapshot,
)


def case(database, *, text=SOURCE_TEXT, occurrence=None, images=()):
    rows, _, _ = repair.date_snapshot(database)
    occurrence = rows[0] if occurrence is None else occurrence
    source = source_snapshot(text=text, quotes=[])
    source['images'] = list(images)
    catalog = refs.build_evidence_catalog(occurrence, [source])
    response = reference_response(_model_response(), {'evidence_catalog': catalog})
    return occurrence, source, catalog, response


def admit(response, occurrence, sources):
    canonical = refs.decode_evidence_response(response, occurrence, sources=sources)
    return repair.decision_from_response(canonical, 'test-model', occurrence, sources=sources)


@pytest.mark.parametrize('text', [
    '    "theme": "校园招聘",\n    "date": "2025-11-27"\n',
    json.dumps({'intro': '<p class="notice">Event held on November 27, 2025.</p>'}, ensure_ascii=False),
    '<p>前面的文字，Event held on November 27, 2025. 后面的文字。</p><ul><li>请报名</li></ul>',
    '  Cafe\u0301\n活动：2025年11月27日。\n',
])
def test_escaped_json_indentation_html_and_unicode_resolve_exactly(database, text):
    occurrence, source, catalog, _ = case(database, text=text)
    response = reference_response(_model_response(quote=text), {'evidence_catalog': catalog})
    response['claims'][0]['quote'] = {'ref': catalog['entries'][0]['id']}
    response['claims'][0]['evidence'] = '原文明确活动日期。'
    response['activity']['summary']['value'] = '校园活动摘要。'
    response['activity']['content']['value'] = '活动安排见原文。'
    original = deepcopy((response, source))
    # This is what a model transports: IDs, never a retyped primary quotation.
    encoded = json.dumps(response, ensure_ascii=False)
    assert '\"ref\"' in encoded and '\"raw_text\"' not in encoded
    assert '"text"' not in encoded
    for field in ('title', 'summary', 'content'):
        assert set(response['activity'][field]['evidence'][0]) == {'url', 'ref'}
    decision = admit(json.loads(encoded), occurrence, [source])
    assert decision['claims'][0]['raw_text'] == text
    assert decision['source_review']['sources'][0]['quote_evidence'] == [{'text': text, 'image_index': None}]
    assert decision['source_review']['activity']['content']['evidence'][0]['text'] == text
    assert (response, source) == original
    # The unchanged canonical API still rejects even a small invented boundary.
    canonical = refs.decode_evidence_response(response, occurrence, sources=[source])
    canonical['activity']['content']['evidence'][0]['text'] = ' ' + text + ' '
    with pytest.raises(ValueError):
        repair.decision_from_response(canonical, 'test-model', occurrence, sources=[source])


@pytest.mark.parametrize('change', [
    'missing_scope', 'wrong_scope', 'extra_top', 'unknown_ref', 'ref_list', 'literal_text',
    'ref_plus_text', 'wrong_url', 'wrong_claim_shape', 'free_claim', 'claim_list',
    'unknown_field', 'malformed_field', 'malformed_college', 'boolean_image',
])
def test_malformed_or_unbound_transport_fails_before_canonical_admission(database, change):
    occurrence, source, _, response = case(database)
    item = response['activity']['content']['evidence'][0]
    if change == 'missing_scope':
        response.pop('evidence_scope')
    elif change == 'wrong_scope':
        response['evidence_scope'] = 'different scope'
    elif change == 'extra_top':
        response['transport'] = refs.VERSION
    elif change == 'unknown_ref':
        item['ref'] = 'e-unknown'
    elif change == 'ref_list':
        item['ref'] = [item['ref']]
    elif change == 'literal_text':
        item.clear()
        item.update(url=SOURCE_URL, text=SOURCE_TEXT, image_index=None)
    elif change == 'ref_plus_text':
        item['text'] = SOURCE_TEXT
    elif change == 'wrong_url':
        item['url'] = 'https://primary.example.org/other'
    elif change == 'wrong_claim_shape':
        response['claims'][0]['raw_text'] = SOURCE_TEXT
    elif change == 'free_claim':
        response['claims'][0]['quote'] = {'text': SOURCE_TEXT, 'image_index': None}
    elif change == 'claim_list':
        response['claims'][0]['quote'] = [response['claims'][0]['quote']]
    elif change == 'unknown_field':
        response['activity']['confidence'] = 99
    elif change == 'malformed_field':
        response['activity']['venue'] = None
    elif change == 'malformed_college':
        response['activity']['colleges'] = [{'name': '学院', 'relation': 'associated'}]
    else:
        item.clear()
        item.update(url=SOURCE_URL, text='Visible image text.', image_index=False)
    original = deepcopy(response)
    with pytest.raises(ValueError):
        refs.decode_evidence_response(response, occurrence, sources=[source])
    assert response == original


@pytest.mark.parametrize('change', ['occurrence', 'text', 'bytes', 'image'])
def test_references_are_bound_to_request_source_capture_and_occurrence(database, change):
    occurrence, source, _, response = case(database, images=[captured_image(0)])
    if change == 'occurrence':
        occurrence = dict(occurrence, local_key='another-item')
    elif change == 'text':
        source['text'] += ' Changed.'
    elif change == 'bytes':
        source['raw_response_digest'] = 'a' * 64
    else:
        source['images'][0]['sha256'] = 'b' * 64
    with pytest.raises(ValueError, match='different request scope'):
        refs.decode_evidence_response(response, occurrence, sources=[source])


def test_reference_cannot_be_rebound_to_another_captured_url(database):
    occurrence, source, _, _ = case(database)
    other_url = 'https://primary.example.org/other'
    occurrence = dict(occurrence, source_urls=[SOURCE_URL, other_url])
    other = source_snapshot(url=other_url, resolved_url=other_url, text=SOURCE_TEXT, quotes=[])
    catalog = refs.build_evidence_catalog(occurrence, [source, other])
    response = reference_response(_model_response(), {'evidence_catalog': catalog})
    item = response['activity']['content']['evidence'][0]
    item['ref'] = next(entry['id'] for entry in catalog['entries'] if entry['url'] == other_url)
    with pytest.raises(ValueError, match='bound to this primary URL'):
        refs.decode_evidence_response(response, occurrence, sources=[source, other])
    foreign = refs.build_evidence_catalog(dict(occurrence, local_key='foreign'), [source, other])
    item['ref'] = foreign['entries'][0]['id']
    with pytest.raises(ValueError, match='unknown or unbound'):
        refs.decode_evidence_response(response, occurrence, sources=[source, other])


@pytest.mark.parametrize('location', ['common', 'field', 'college'])
def test_bulletin_reference_cannot_certify_primary_even_when_text_is_identical(database, location):
    rows, _, _ = repair.date_snapshot(database)
    occurrence = dict(rows[0], body_text=SOURCE_TEXT)
    occurrence, source, catalog, response = case(database, occurrence=occurrence)
    bulletin_id = next(entry['id'] for entry in catalog['entries'] if entry['origin'] == 'bulletin.body_text')
    if location == 'common':
        response['citations'][0]['quotes'] = [{'ref': bulletin_id}]
    elif location == 'field':
        response['activity']['content']['evidence'][0]['ref'] = bulletin_id
    else:
        response['activity']['colleges'] = [{'name': 'Event', 'relation': 'associated',
            'evidence': {'url': SOURCE_URL, 'ref': bulletin_id}}]
    with pytest.raises(ValueError, match='bound to this primary URL'):
        refs.decode_evidence_response(response, occurrence, sources=[source])


@pytest.mark.parametrize('related', [False, True])
def test_same_activity_bulletin_references_can_support_secondary_date_claims(database, related):
    rows, _, _ = repair.date_snapshot(database)
    occurrence = dict(rows[0], source_urls=[], related_occurrences=[])
    if related:
        occurrence['related_occurrences'] = [{'title': 'Same activity', 'body_text': SOURCE_TEXT}]
        origin = 'bulletin.related_occurrences[0].body_text'
    else:
        occurrence['body_text'] = SOURCE_TEXT
        origin = 'bulletin.body_text'
    catalog = refs.build_evidence_catalog(occurrence, [])
    response = _model_response(source_status='secondary_only')
    response['citations'] = []
    response = reference_response(response, {'evidence_catalog': catalog})
    response['claims'][0]['quote'] = {'ref': next(entry['id'] for entry in catalog['entries'] if entry['origin'] == origin)}
    decision = admit(response, occurrence, [])
    assert decision['status'] == 'resolved'
    assert decision['claims'][0]['raw_text'] == SOURCE_TEXT
    assert decision['source_review']['status'] == 'secondary_only'
    assert decision['source_review']['activity'] is None


def test_primary_application_date_reference_is_explicitly_cited_without_copying(database):
    deadline = 'Application deadline: December 1, 2025.'
    text = SOURCE_TEXT + '\n' + 'x' * 4500 + '\n' + deadline
    occurrence, source, catalog, response = case(database, text=text)
    response['citations'] = []
    selected = next(entry for entry in catalog['entries'] if deadline in entry['text'])
    response['claims'].append({'kind': 'application', 'quote': {'ref': selected['id']},
        'normalized_date': '2025-12-01', 'resolution': 'explicit', 'evidence': '原文给出申请截止日。', 'locator': None})
    canonical = refs.decode_evidence_response(response, occurrence, sources=[source])
    assert canonical['claims'][1]['raw_text'] == selected['text']
    assert canonical['citations'] == [{'url': SOURCE_URL, 'quotes': [{'text': selected['text'], 'image_index': None}]}]
    decision = repair.decision_from_response(canonical, 'test-model', occurrence, sources=[source])
    assert selected['text'] in decision['source_review']['sources'][0]['quotes']
    assert len(decision['source_review']['sources'][0]['quotes']) == 2
    assert response['citations'] == []  # No transport mutation or inferred uncited string.


@pytest.mark.parametrize('status', ['source_mismatch', 'source_unavailable', 'secondary_only'])
def test_primary_claim_reference_cannot_survive_unmatched_verdict(database, status):
    occurrence, source, _, response = case(database)
    response['source_status'] = status
    response['activity'] = None
    with pytest.raises(ValueError, match='matched primary verdict'):
        refs.decode_evidence_response(response, occurrence, sources=[source])
    response['source_status'] = 'source_verified'
    with pytest.raises(ValueError, match='matched primary verdict'):
        refs.decode_evidence_response(response, occurrence, sources=[source], mismatched=True)


@pytest.mark.parametrize('change', ['valid', 'absent', 'wrong_source', 'boolean', 'null', 'oversize'])
def test_images_keep_visible_text_and_real_source_bound_global_index(database, change):
    occurrence, source, catalog, response = case(database, images=[captured_image(7)])
    visible = '海报日期：2025年11月27日。'
    image_quote = {'text': visible, 'image_index': 7}
    response['citations'] = []
    response['claims'][0]['quote'] = dict(image_quote, url=SOURCE_URL)
    response['activity']['content']['evidence'] = [dict(image_quote, url=SOURCE_URL)]
    response['activity']['image_indices'] = [7]
    if change == 'absent':
        response['activity']['content']['evidence'][0]['image_index'] = 0
    elif change == 'wrong_source':
        response['activity']['content']['evidence'][0]['url'] = 'https://primary.example.org/other'
    elif change == 'boolean':
        response['activity']['content']['evidence'][0]['image_index'] = True
    elif change == 'null':
        response['activity']['content']['evidence'][0]['image_index'] = None
    elif change == 'oversize':
        response['activity']['content']['evidence'][0]['text'] = 'x' * 4001
    if change != 'valid':
        with pytest.raises(ValueError):
            refs.decode_evidence_response(response, occurrence, sources=[source])
        return
    decision = admit(response, occurrence, [source])
    evidence = decision['source_review']['sources'][0]['quote_evidence']
    assert image_quote in evidence
    assert decision['claims'][0]['raw_text'] == visible
    assert decision['source_review']['activity']['image_indices'] == [7]
    assert source['images'][0]['data'] == captured_image(7)['data']
    assert all(entry['origin'] != 'image' for entry in catalog['entries'])


@pytest.mark.parametrize('newlines', [False, True])
def test_catalog_covers_every_character_and_suffix_with_bounded_overlap(database, newlines):
    text = ('line with spaces  \n' * 4000 if newlines else '字' * acquisition.MAX_TEXT)[:acquisition.MAX_TEXT - 7] + 'SUFFIX!'
    rows, _, _ = repair.date_snapshot(database)
    occurrence = dict(rows[0], title='T\n', body_text=' B ', related_occurrences=[
        {'title': ' related ', 'body_text': text, 'source_urls': [SOURCE_URL]}])
    occurrence, source, catalog, _ = case(database, text=text, occurrence=occurrence)
    fields = {'primary.text': text, 'bulletin.title': occurrence['title'], 'bulletin.body_text': occurrence['body_text'],
        'bulletin.related_occurrences[0].title': ' related ', 'bulletin.related_occurrences[0].body_text': text}
    assert catalog == refs.build_evidence_catalog(occurrence, [source])
    assert len(catalog['entries']) < 50
    for origin, original in fields.items():
        end = 0
        previous = None
        for entry in (entry for entry in catalog['entries'] if entry['origin'] == origin):
            assert entry['start'] <= end
            assert 0 < len(entry['text']) <= refs.CHUNK_CHARS
            assert entry['text'] == original[entry['start']:entry['end']]
            assert entry['sha256'] == hashlib.sha256(entry['text'].encode()).hexdigest()
            if previous is not None:
                assert previous['end'] - entry['start'] == refs.OVERLAP_CHARS
            previous = entry
            end = entry['end']
        assert end == len(original)
        assert previous['text'].endswith(original[-7:])


@pytest.mark.parametrize('bound', ['source', 'bulletin', 'related', 'sources', 'fields', 'entries', 'total', 'overlap_total'])
def test_catalog_bounds_reject_entire_input_never_return_partial_prefix(database, monkeypatch, bound):
    occurrence, source, _, _ = case(database)
    sources = [source]
    if bound == 'source':
        source['text'] = 'x' * (acquisition.MAX_TEXT + 1)
    elif bound == 'bulletin':
        occurrence = dict(occurrence, body_text='x' * (acquisition.MAX_TEXT + 1))
    elif bound == 'related':
        occurrence = dict(occurrence, related_occurrences=[{'title': '', 'body_text': 'x' * (acquisition.MAX_TEXT + 1)}])
    elif bound == 'sources':
        sources *= 101
    elif bound == 'fields':
        occurrence = dict(occurrence, related_occurrences=[{}] * refs.MAX_CATALOG_ENTRIES)
    elif bound == 'entries':
        monkeypatch.setattr(refs, 'MAX_CATALOG_ENTRIES', 3)
        source['text'] = 'x' * 8000
        occurrence = dict(occurrence, related_occurrences=[])
    elif bound == 'total':
        monkeypatch.setattr(refs, 'MAX_CATALOG_TEXT_CHARS', 10)
    else:
        source['text'] = 'x' * 4001
        occurrence = dict(occurrence, title='', body_text='', related_occurrences=[])
        monkeypatch.setattr(refs, 'MAX_CATALOG_TEXT_CHARS', 4001)
    with pytest.raises(ValueError, match='catalog'):
        refs.build_evidence_catalog(occurrence, sources)


@pytest.mark.parametrize('bound', ['claims', 'sources', 'quotes', 'fields', 'empty_field', 'colleges', 'images', 'image_quote'])
def test_decoder_retains_all_canonical_output_bounds(database, bound):
    occurrence, source, _, response = case(database, images=[captured_image(0)])
    if bound == 'claims':
        response['claims'] *= 1001
    elif bound == 'sources':
        response['citations'] *= 101
    elif bound == 'quotes':
        response['citations'][0]['quotes'] *= 101
    elif bound == 'fields':
        response['activity']['content']['evidence'] *= 9
    elif bound == 'empty_field':
        response['activity']['content']['evidence'] = []
    elif bound == 'colleges':
        response['activity']['colleges'] = [{}] * 31
    elif bound == 'images':
        response['activity']['image_indices'] = list(range(65))
    else:
        response['citations'][0]['quotes'] = [{'text': 'x' * 12001, 'image_index': 0}]
    with pytest.raises(ValueError):
        refs.decode_evidence_response(response, occurrence, sources=[source])


def test_source_union_limit_is_not_bypassed_by_reference_decoding(database):
    occurrence, source, _, response = case(database, images=[captured_image(0)])
    response['citations'][0]['quotes'] = [{'text': f'Visible image line {index}.', 'image_index': 0} for index in range(100)]
    response['claims'] = []
    response['status'] = 'no_relevant_date'
    canonical = refs.decode_evidence_response(response, occurrence, sources=[source])
    with pytest.raises(ValueError, match='union exceeds'):
        repair.decision_from_response(canonical, 'test-model', occurrence, sources=[source])


def test_catalog_excludes_metadata_and_rejects_unlinked_captures(database):
    occurrence, source, catalog, _ = case(database)
    source['title'] = 'Metadata-only title'
    source['raw_date_text'] = 'Metadata-only date'
    assert all(entry['text'] not in {'Metadata-only title', 'Metadata-only date'} for entry in catalog['entries'])
    occurrence = dict(occurrence, source_urls=[], related_occurrences=[])
    with pytest.raises(ValueError, match='linked to this item'):
        refs.build_evidence_catalog(occurrence, [source])


def test_canonical_offline_api_does_not_implicitly_decode_reference_responses(database):
    occurrence, source, _, response = case(database)
    with pytest.raises(ValueError, match='unsupported date-review shape'):
        repair.decision_from_response(response, 'test-model', occurrence, sources=[source])
    canonical = refs.decode_evidence_response(response, occurrence, sources=[source])
    assert repair.decision_from_response(canonical, 'test-model', occurrence, sources=[source])['status'] == 'resolved'


def test_reference_expansion_cannot_bypass_canonical_response_storage_bound(database, monkeypatch):
    occurrence, source, _, response = case(database)
    canonical = refs.decode_evidence_response(response, occurrence, sources=[source])
    size = len(json.dumps(canonical, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    monkeypatch.setattr(acquisition, 'MAX_MODEL_BODY', size)
    assert refs.decode_evidence_response(response, occurrence, sources=[source]) == canonical
    monkeypatch.setattr(acquisition, 'MAX_MODEL_BODY', size - 1)
    with pytest.raises(ValueError, match='canonical response byte bound'):
        refs.decode_evidence_response(response, occurrence, sources=[source])


@pytest.mark.parametrize('wrong_name', [False, True])
def test_college_references_keep_same_quote_name_and_relation_contract(database, wrong_name):
    text = SOURCE_TEXT + '\n物理学院组织此次讲座，面向物理学院学生。'
    occurrence, source, catalog, response = case(database, text=text)
    evidence = {'url': SOURCE_URL, 'ref': catalog['entries'][0]['id']}
    name = '数学学院' if wrong_name else '物理学院'
    response['activity']['colleges'] = [
        {'name': name, 'relation': 'associated', 'evidence': dict(evidence)},
        {'name': name, 'relation': 'audience', 'evidence': dict(evidence)},
    ]
    if wrong_name:
        with pytest.raises(ValueError, match='same relation quotation'):
            refs.decode_evidence_response(response, occurrence, sources=[source])
    else:
        colleges = admit(response, occurrence, [source])['source_review']['activity']['colleges']
        assert {item['relation'] for item in colleges} == {'associated', 'audience'}
        assert all(item['evidence']['text'] == text for item in colleges)


def test_strict_schema_binds_entire_scope_and_only_actual_reference_url_image_enums(database):
    occurrence, source, catalog, _ = case(database, images=[captured_image(7)])
    original = deepcopy((catalog, source))
    schema = refs.historical_response_schema(catalog, [source])
    assert schema['properties']['evidence_scope']['enum'] == [catalog['scope']]
    assert len(schema['properties']['evidence_scope']['enum'][0]) == 64
    assert schema['$defs']['primary_url']['enum'] == [SOURCE_URL]
    assert schema['$defs']['image_index']['enum'] == [7]
    assert set(schema['$defs']['primary_ref']['enum']) == {
        entry['id'] for entry in catalog['entries'] if entry['origin'] == 'primary.text'}
    assert set(schema['$defs']['bulletin_ref']['enum']) == {
        entry['id'] for entry in catalog['entries'] if entry['origin'] != 'primary.text'}

    def closed_objects(node):
        if isinstance(node, dict):
            if node.get('type') == 'object':
                assert node['additionalProperties'] is False
                assert set(node['required']) == set(node['properties'])
            if 'enum' in node:
                assert node['enum']
            for value in node.values():
                closed_objects(value)
        elif isinstance(node, list):
            for value in node:
                closed_objects(value)

    closed_objects(schema)
    assert (catalog, source) == original


@pytest.mark.parametrize('quote_kind', ['text', 'image'])
@pytest.mark.parametrize('resolution', ['explicit', 'contextual', 'year_unknown', 'ambiguous', 'invalid'])
@pytest.mark.parametrize('day', ['2025-11-27', None])
def test_strict_schema_matches_canonical_date_resolution_pairs(database, quote_kind, resolution, day):
    occurrence, source, catalog, response = case(database, images=[captured_image(7)])
    response['claims'][0].update(normalized_date=day, resolution=resolution,
        evidence='按同一活动的日期证据独立解释，无法确定时保留未解析状态。')
    response['status'] = 'resolved' if day is not None else 'needs_context'
    if quote_kind == 'image':
        response['claims'][0]['quote'] = {'url': SOURCE_URL, 'text': SOURCE_TEXT, 'image_index': 7}
    coherent = (resolution in {'explicit', 'contextual'}) == (day is not None)
    validator = Draft202012Validator(refs.historical_response_schema(catalog, [source]))
    assert validator.is_valid(response) is coherent
    if coherent:
        decision = admit(response, occurrence, [source])
        assert decision['claims'][0]['resolution'] == resolution
        assert decision['claims'][0]['normalized_date'] == day
    else:
        with pytest.raises(ValueError):
            admit(response, occurrence, [source])


def test_strict_schema_keeps_image_only_primary_path_without_text_refs(database):
    rows, _, _ = repair.date_snapshot(database)
    occurrence = rows[0]
    source = source_snapshot(text='', quotes=[])
    source['images'] = [captured_image(7)]
    catalog = refs.build_evidence_catalog(occurrence, [source])
    schema = refs.historical_response_schema(catalog, [source])
    assert 'primary_ref' not in schema['$defs']
    assert schema['$defs']['image_index']['enum'] == [7]
    visible = SOURCE_TEXT + '\n物理学院组织此次讲座。'
    response = _model_response(quote=visible)
    response['evidence_scope'] = catalog['scope']
    for claim in response['claims']:
        claim.pop('raw_text')
        claim['quote'] = {'url': SOURCE_URL, 'text': visible, 'image_index': 7}
    for citation in response['citations']:
        for quote in citation['quotes']:
            quote['image_index'] = 7
    for name in refs.FIELD_LIMITS:
        for evidence in response['activity'][name]['evidence']:
            evidence['image_index'] = 7
    response['activity']['colleges'] = [{'name': '物理学院', 'relation': 'associated',
        'evidence': {'url': SOURCE_URL, 'text': visible, 'image_index': 7}}]
    response['activity']['image_indices'] = [7]
    decision = admit(response, occurrence, [source])
    assert decision['source_review']['status'] == 'source_verified'
    assert decision['source_review']['activity']['image_indices'] == [7]
    assert decision['claims'][0]['raw_text'] == visible
    bad = deepcopy(response)
    bad['activity']['colleges'][0]['evidence']['url'] = source['images'][0]['url']
    with pytest.raises(ValueError, match='primary URL'):
        refs.decode_evidence_response(bad, occurrence, sources=[source])


def test_empty_evidence_schema_has_empty_arrays_not_invalid_empty_enums(database):
    rows, _, _ = repair.date_snapshot(database)
    occurrence = dict(rows[0], title='', body_text='', related_occurrences=[])
    catalog = refs.build_evidence_catalog(occurrence, [])
    schema = refs.historical_response_schema(catalog, [])
    assert schema['properties']['claims']['maxItems'] == 0
    assert schema['properties']['citations']['maxItems'] == 0
    assert schema['properties']['activity'] == {'type': 'null'}


@pytest.mark.parametrize('bound', ['enum_count', 'enum_text', 'total_strings'])
def test_schema_protocol_limits_reject_complete_request_without_shortening_enums(database, bound):
    _, source, catalog, _ = case(database)
    if bound in {'enum_count', 'enum_text'}:
        catalog['entries'] = [dict(catalog['entries'][0],
            id=('e' + str(index) if bound == 'enum_count' else 'e' + 'a' * 40 + str(index)))
            for index in range(1001 if bound == 'enum_count' else 400)]
        sources = [source]
    else:
        sources = [dict(source, url='https://public.example/' + str(index) + 'x' * 2000)
            for index in range(70)]
    before = deepcopy((catalog, sources))
    with pytest.raises(ValueError, match='strict schema exceeds'):
        refs.historical_response_schema(catalog, sources)
    assert (catalog, sources) == before


def test_original_college_name_is_preserved_not_display_normalized(database):
    name = '计算机 科学与技术学院（苏州校区）'
    text = SOURCE_TEXT + '\n' + name + '组织此次讲座。'
    occurrence, source, catalog, response = case(database, text=text)
    response['activity']['colleges'] = [{'name': name, 'relation': 'associated',
        'evidence': {'url': SOURCE_URL, 'ref': catalog['entries'][0]['id']}}]
    original = deepcopy(response)
    decision = admit(response, occurrence, [source])
    assert decision['source_review']['activity']['colleges'][0]['name'] == name
    response['activity']['colleges'][0]['name'] = name.replace(' ', '')
    with pytest.raises(ValueError, match='same relation quotation'):
        admit(response, occurrence, [source])
    assert original['activity']['colleges'][0]['name'] == name
