"""Cite-once historical response admission is identical offline and live."""
from copy import deepcopy
from pathlib import Path
import json


import pytest



from nannanews import acquisition
from nannanews import bulletin_date_repair as repair
from test_activity_content import captured_image
from test_bulletin_date_repair import (
    SOURCE_TEXT, SOURCE_URL, _fetched_document, _model_response, activity,
    database, prepared, source_snapshot, reference_response,
)


def test_field_only_poster_quote_survives_offline_live_and_prepared_storage(database, monkeypatch, tmp_path):
    rows, originals, _ = repair.date_snapshot(database)
    image = captured_image(0)
    snapshot = source_snapshot(quotes=[])
    snapshot['images'] = [image]
    poster_quote = ' 研究方向：数学物理。\n主讲人简介：长期从事基础研究。'
    response = _model_response()
    response['activity']['content'] = {'value': '主讲人从事数学物理基础研究。', 'evidence': [
        {'url': SOURCE_URL, 'text': poster_quote, 'image_index': 0}]}
    response['activity']['image_indices'] = [0]
    original_response, original_snapshot = deepcopy(response), deepcopy(snapshot)
    monkeypatch.setattr(acquisition, 'fetch_document', lambda *args: pytest.fail('unexpected fetch'))
    monkeypatch.setattr(acquisition, 'request_model_json', lambda *args, **kwargs: pytest.fail('offline admission sent a request'))
    offline = repair.decision_from_response(response, 'test-model', rows[0], sources=[snapshot])
    evidence = offline['source_review']['sources'][0]['quote_evidence']
    assert evidence == [{'text': SOURCE_TEXT, 'image_index': None},
                        {'text': poster_quote, 'image_index': 0}]
    assert offline['source_review']['activity']['content']['evidence'][0]['text'] == poster_quote
    assert response == original_response and snapshot == original_snapshot

    document = _fetched_document()
    document['images'] = [image['url']]
    media = {'url': image['url'], 'mime': image['mime'], 'data': image['data'], 'digest': image['sha256']}
    cache = {SOURCE_URL: {'document': document, 'media': [media]}}
    monkeypatch.setattr(acquisition, 'request_model_json', lambda instructions, data, **kwargs:
        (reference_response(response, data), 'test-model'))
    live = repair.model_decision(rows[0], originals[1], source_cache=cache)
    assert live == offline
    assert response == original_response and snapshot == original_snapshot

    material = prepared(rows[0], originals[1], claims=offline['claims'], status=offline['status'],
        review=offline['source_review'], reason=offline['reason'])
    path = tmp_path / 'retained-response-review.json'
    path.write_text(json.dumps([material], ensure_ascii=False))
    assert repair.apply_repairs(database, repair.plan_repairs(database, review_files=[path])) == 1
    saved = database.execute('SELECT source_review_json FROM bulletin_date_revisions').fetchone()[0]
    projected = repair.public_source_review(saved, rules_version=repair.RULES_VERSION)
    assert projected['activity']['content']['evidence'][0] == {
        'url': SOURCE_URL, 'text': poster_quote, 'image_index': 0}
    assert projected['activity']['image_indices'] == [0]
    assert repair.public_source_review(saved, rules_version='luna-content-evidence-v4')['activity'] is None


@pytest.mark.parametrize('common', [False, True])
def test_field_and_college_only_quotes_form_exact_deduplicated_source_union(database, common):
    rows, _, _ = repair.date_snapshot(database)
    college_quote = '物理学院组织此次活动。'
    snapshot = source_snapshot(text=SOURCE_TEXT + '\n' + college_quote, quotes=[])
    response = _model_response()
    if common:
        response['citations'][0]['quotes'] *= 2
    else:
        response['citations'] = []
    response['activity']['colleges'] = [{'name': '物理学院', 'relation': 'associated', 'evidence':
        {'url': SOURCE_URL, 'text': college_quote, 'image_index': None}}]
    decision = repair.decision_from_response(response, 'test-model', rows[0], sources=[snapshot])
    assert decision['status'] == 'resolved'
    assert decision['source_review']['status'] == 'source_verified'
    assert decision['source_review']['sources'][0]['quote_evidence'] == [
        {'text': SOURCE_TEXT, 'image_index': None}, {'text': college_quote, 'image_index': None}]
    assert decision['source_review']['activity']['colleges'] == response['activity']['colleges']


@pytest.mark.parametrize('change', [
    'forged_text', 'whitespace_changed', 'unicode_changed', 'joined_passages',
    'joined_fields', 'other_source_text', 'other_source_image', 'missing_image',
    'image_as_text', 'boolean_image', 'extra_citation_key', 'oversized_quote',
    'missing_field', 'wrong_field_shape', 'wrong_evidence_shape', 'unknown_field',
])
def test_union_still_rejects_forgery_relabeling_and_malformed_fields(database, change):
    rows, _, _ = repair.date_snapshot(database)
    other_url = 'https://primary.example.org/other'
    occurrence = dict(rows[0], source_urls=[SOURCE_URL, other_url])
    text = SOURCE_TEXT + '\nDate:\n2025-11-27\ncafe\u0301\n第一段。\n第二段。'
    snapshot = source_snapshot(text=text, quotes=[])
    snapshot['images'] = [captured_image(0)]
    other = source_snapshot(url=other_url, resolved_url=other_url, text='Different article.', quotes=[])
    other['images'] = [captured_image(1)]
    response = _model_response()
    response['citations'] = []
    citation = response['activity']['content']['evidence'][0]
    if change == 'forged_text':
        citation['text'] = 'Invented content.'
    elif change == 'whitespace_changed':
        citation['text'] = 'Date: 2025-11-27'
    elif change == 'unicode_changed':
        citation['text'] = 'caf\u00e9'
    elif change == 'joined_passages':
        citation['text'] = '第一段。第二段。'
    elif change == 'joined_fields':
        citation['text'] = snapshot['title'] + SOURCE_TEXT
    elif change == 'other_source_text':
        citation['url'] = other_url
    elif change == 'other_source_image':
        citation.update(url=other_url, text='Visible poster text.', image_index=0)
    elif change == 'missing_image':
        citation.update(text='Visible poster text.', image_index=9)
    elif change == 'image_as_text':
        citation.update(text='Visible poster text.', image_index=None)
    elif change == 'boolean_image':
        citation['image_index'] = False
    elif change == 'extra_citation_key':
        citation['metadata'] = 'not quotation'
    elif change == 'oversized_quote':
        citation['text'] = 'x' * 4001
        snapshot['text'] += '\n' + citation['text']
    elif change == 'missing_field':
        response['activity'].pop('venue')
    elif change == 'wrong_field_shape':
        response['activity']['venue'] = None
    elif change == 'wrong_evidence_shape':
        response['activity']['venue']['evidence'] = None
    else:
        response['activity']['confidence'] = 100
    original_response, original_sources = deepcopy(response), deepcopy([snapshot, other])
    with pytest.raises(ValueError):
        repair.decision_from_response(response, 'test-model', occurrence, sources=[snapshot, other])
    assert response == original_response and [snapshot, other] == original_sources


def test_canonical_union_keeps_existing_per_source_storage_quote_bound(database):
    rows, _, _ = repair.date_snapshot(database)
    quotes = [f'Literal quotation {index}.' for index in range(101)]
    snapshot = source_snapshot(text='\n'.join(quotes), quotes=[])
    response = {'status': 'no_relevant_date', 'reason': '没有完整活动日期。', 'claims': [],
        'source_status': 'source_verified', 'source_reason': '已匹配同一活动。',
        'citations': [{'url': SOURCE_URL, 'quotes': [
            {'text': quote, 'image_index': None} for quote in quotes[:100]]}],
        'activity': activity(quote=quotes[0])}
    response['activity']['content']['evidence'][0]['text'] = quotes[100]
    with pytest.raises(ValueError):
        repair.decision_from_response(response, 'test-model', rows[0], sources=[snapshot])
    response['activity']['content']['evidence'][0]['text'] = quotes[99]
    decision = repair.decision_from_response(response, 'test-model', rows[0], sources=[snapshot])
    assert len(decision['source_review']['sources'][0]['quote_evidence']) == 100


def test_offline_admission_rejects_unlinked_capture_even_with_literal_quote(database):
    rows, _, _ = repair.date_snapshot(database)
    url = 'https://unlinked.example.org/article'
    snapshot = source_snapshot(url=url, resolved_url=url, quotes=[])
    with pytest.raises(ValueError):
        repair.decision_from_response(_model_response(url=url), 'test-model', rows[0], sources=[snapshot])
