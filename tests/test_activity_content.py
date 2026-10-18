"""Historical content is confirmed only by same-source admitted primary proof."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path


import pytest



from nannanews import bulletin_date_repair as repair
from nannanews.activity_content import FIELD_LIMITS
from test_acquisition_media import image_bytes
from test_bulletin_date_repair import (
    SOURCE_TEXT, SOURCE_URL, _fetched_document, _model_response, activity,
    claim, database, prepared, source_review, source_snapshot, reference_response,
)


def captured_image(index):
    raw = image_bytes()
    return {'image_index': index, 'url': f'https://primary.example.org/poster-{index}.png',
            'sha256': hashlib.sha256(raw).hexdigest(), 'mime': 'image/png',
            'data': base64.b64encode(raw).decode('ascii')}


def admit(review):
    return repair._validate_source_review(review, require_activity=True)


def test_current_activity_survives_prepared_plan_append_only_storage_and_public_projection(database, tmp_path):
    text = ' 物理学院组织学术报告，面向物理学院学生。\n时间：15:00，地点：报告厅。'
    snapshot = source_snapshot(text=text, quotes=[text])
    snapshot['images'] = [captured_image(7)]
    review = source_review(sources=[snapshot])
    content = review['activity']
    content['title']['value'] = '物理学院学术报告'
    content['venue'] = {'value': '报告厅', 'evidence': [
        {'url': SOURCE_URL, 'text': '地点：报告厅。', 'image_index': None}]}
    content['time'] = {'value': '15:00', 'evidence': [
        {'url': SOURCE_URL, 'text': '时间：15:00', 'image_index': None}]}
    content['colleges'] = [{'name': '物理学院', 'relation': relation, 'evidence':
        {'url': SOURCE_URL, 'text': text, 'image_index': None}}
        for relation in ('associated', 'audience')]
    content['image_indices'] = [7]
    original_review = deepcopy(review)
    rows, originals, _ = repair.date_snapshot(database)
    value = prepared(rows[0], originals[1], claims=[], status='no_relevant_date', review=review)
    path = tmp_path / 'content-review.json'
    path.write_text(json.dumps([value], ensure_ascii=False))
    plan = repair.plan_repairs(database, review_files=[path])
    assert plan['entries'][0]['source_review']['activity'] == content
    assert repair.apply_repairs(database, plan) == 1
    saved = database.execute('SELECT source_review_json FROM bulletin_date_revisions').fetchone()[0]
    assert json.loads(saved)['activity'] == content
    public = repair.public_source_review(saved, rules_version=repair.RULES_VERSION)
    assert public['status'] == 'source_verified' and public['activity'] == content
    assert 'raw_response_base64' not in json.dumps(public)
    assert 'data' not in public['sources'][0] and 'text' not in public['sources'][0]
    assert review == original_review
    assert database.execute('SELECT raw_text FROM bulletin_dates').fetchone()[0] == '刊物中未写日期'
    assert repair.apply_repairs(database, repair.plan_repairs(database, review_files=[path])) == 0


@pytest.mark.parametrize('change', [
    'article_but_not_admitted', 'source_title', 'metadata', 'wrong_url', 'another_source',
    'joined_quotes', 'whitespace_changed', 'unicode_changed', 'wrong_image',
])
def test_activity_cannot_promote_unadmitted_or_relabelled_primary_evidence(change):
    quote = 'Date:\n2025-11-27; cafe\u0301'
    snapshot = source_snapshot(text=quote + '\nUncited detail.', quotes=[quote])
    review = source_review(sources=[snapshot])
    citation = review['activity']['content']['evidence'][0]
    if change == 'article_but_not_admitted':
        citation['text'] = 'Uncited detail.'
    elif change == 'source_title':
        citation['text'] = snapshot['title']
    elif change == 'metadata':
        citation['text'] = snapshot['raw_date_text']
    elif change == 'wrong_url':
        citation['url'] = 'https://elsewhere.example.org/source'
    elif change == 'another_source':
        other_url = 'https://primary.example.org/other'
        review['sources'].append(source_snapshot(url=other_url, resolved_url=other_url,
            text='Other admitted text.', quotes=['Other admitted text.']))
        citation['url'] = other_url
    elif change == 'joined_quotes':
        snapshot['quotes'] = ['Date:', '2025-11-27; cafe\u0301']
        snapshot['quote_evidence'] = [{'text': item, 'image_index': None} for item in snapshot['quotes']]
    elif change == 'whitespace_changed':
        citation['text'] = 'Date: 2025-11-27'
    elif change == 'unicode_changed':
        citation['text'] = 'caf\u00e9'
    else:
        snapshot['images'] = [captured_image(2)]
        citation['image_index'] = 2
    with pytest.raises(ValueError):
        admit(review)
    assert repair.public_source_review(review, rules_version=repair.RULES_VERSION)['activity'] is None


@pytest.mark.parametrize('field', list(FIELD_LIMITS))
def test_activity_known_field_limits_and_unknown_null_shapes(field):
    review = source_review()
    value = review['activity'][field]
    value.update(value='x' * FIELD_LIMITS[field], evidence=[
        {'url': SOURCE_URL, 'text': SOURCE_TEXT, 'image_index': None}])
    assert admit(review)['activity'][field]['value'] == value['value']
    value['value'] += 'x'
    with pytest.raises(ValueError):
        admit(review)
    value.update(value=None, evidence=[])
    if field in {'title', 'summary', 'content'}:
        with pytest.raises(ValueError):
            admit(review)
    else:
        assert admit(review)['activity'][field] == {'value': None, 'evidence': []}
        value['evidence'] = [{'url': SOURCE_URL, 'text': SOURCE_TEXT, 'image_index': None}]
        with pytest.raises(ValueError):
            admit(review)


@pytest.mark.parametrize('change', [
    'missing_field', 'unknown_field', 'private_field', 'unknown_citation_key',
    'blank_value', 'wrong_value_type', 'missing_evidence', 'too_many_evidence',
    'null_with_evidence', 'long_quote',
])
def test_activity_rejects_incomplete_unknown_or_unbounded_shapes(change):
    review = source_review()
    content = review['activity']
    if change == 'missing_field':
        content.pop('venue')
    elif change == 'unknown_field':
        content['confidence'] = 100
    elif change == 'private_field':
        content['content']['raw_response'] = 'private'
    elif change == 'unknown_citation_key':
        content['content']['evidence'][0]['actor'] = 'private'
    elif change == 'blank_value':
        content['summary']['value'] = ' \n '
    elif change == 'wrong_value_type':
        content['summary']['value'] = 1
    elif change == 'missing_evidence':
        content['summary']['evidence'] = []
    elif change == 'too_many_evidence':
        content['summary']['evidence'] *= 9
    elif change == 'null_with_evidence':
        content['venue'] = {'value': None, 'evidence': content['summary']['evidence']}
    else:
        text = 'x' * 4001
        review['sources'] = [source_snapshot(text=text, quotes=[text])]
        content['content']['evidence'][0]['text'] = text
    with pytest.raises(ValueError):
        admit(review)


def test_colleges_preserve_distinct_roles_and_quotes_and_deduplicate_only_exact_evidence():
    first = '物理学院组织活动，面向物理学院学生。'
    second = '物理学院参与筹办。'
    review = source_review(sources=[source_snapshot(text=first + '\n' + second, quotes=[first, second])])
    associated = {'name': '物理学院', 'relation': 'associated',
        'evidence': {'url': SOURCE_URL, 'text': first, 'image_index': None}}
    audience = dict(associated, relation='audience')
    another = dict(associated, evidence={'url': SOURCE_URL, 'text': second, 'image_index': None})
    review['activity']['colleges'] = [associated, audience, deepcopy(associated), another]
    assert admit(review)['activity']['colleges'] == [associated, audience, another]


@pytest.mark.parametrize('change', ['publisher', 'name_elsewhere', 'expanded_name', 'wrong_image', 'unknown_key'])
def test_college_requires_named_same_quote_relation_and_exact_citation(change):
    text = '物理学院官网\n面向全校学生。\n我院组织活动。'
    review = source_review(sources=[source_snapshot(text=text,
        quotes=['物理学院官网', '面向全校学生。', '我院组织活动。'])])
    college = {'name': '物理学院', 'relation': 'audience',
        'evidence': {'url': SOURCE_URL, 'text': '面向全校学生。', 'image_index': None}}
    if change == 'publisher':
        college.update(relation='publisher', evidence={
            'url': SOURCE_URL, 'text': '物理学院官网', 'image_index': None})
    elif change == 'expanded_name':
        college['evidence']['text'] = '我院组织活动。'
    elif change == 'wrong_image':
        college['evidence'].update(text='物理学院官网', image_index=0)
        review['sources'][0]['images'] = [captured_image(0)]
    elif change == 'unknown_key':
        college['confidence'] = 100
    review['activity']['colleges'] = [college]
    with pytest.raises(ValueError):
        admit(review)


def test_image_citation_and_selection_keep_exact_global_source_identity():
    snapshot = source_snapshot()
    snapshot['images'] = [captured_image(8)]
    poster = '报告时间：15:00'
    snapshot['quote_evidence'].append({'text': poster, 'image_index': 8})
    snapshot['quotes'].append(poster)
    review = source_review(sources=[snapshot])
    review['activity']['time'] = {'value': '15:00', 'evidence': [
        {'url': SOURCE_URL, 'text': poster, 'image_index': 8}]}
    review['activity']['image_indices'] = [8]
    assert admit(review)['activity']['image_indices'] == [8]
    review['activity']['time']['evidence'][0]['image_index'] = None
    with pytest.raises(ValueError):
        admit(review)


@pytest.mark.parametrize('selection', [[True], [-1], [999], [0, 0], list(range(65))])
def test_selected_images_reject_boolean_missing_duplicate_or_excess_indices(selection):
    review = source_review()
    review['sources'][0]['images'] = [captured_image(0)]
    review['activity']['image_indices'] = selection
    with pytest.raises(ValueError):
        admit(review)


def test_images_from_uncited_source_cannot_be_selected():
    review = source_review()
    other = source_snapshot(url='https://primary.example.org/uncited',
        resolved_url='https://primary.example.org/uncited', quotes=[])
    other['images'] = [captured_image(4)]
    review['sources'].append(other)
    review['activity']['image_indices'] = [4]
    with pytest.raises(ValueError):
        admit(review)


@pytest.mark.parametrize('status', ['source_conflict', 'source_unavailable', 'source_mismatch', 'secondary_only'])
def test_only_verified_source_status_can_admit_confirmed_content(status):
    review = source_review(status=status)
    assert admit(review)['activity'] is None
    review['activity'] = activity()
    with pytest.raises(ValueError):
        admit(review)


@pytest.mark.parametrize('status', ['source_verified', 'secondary_only'])
def test_every_current_prepared_review_requires_explicit_activity(database, tmp_path, status):
    rows, originals, _ = repair.date_snapshot(database)
    review = source_review(status=status)
    review.pop('activity')
    value = prepared(rows[0], originals[1], review=review)
    path = tmp_path / 'missing-activity.json'
    path.write_text(json.dumps([value]))
    with pytest.raises(ValueError):
        repair.plan_repairs(database, review_files=[path])
    with pytest.raises(ValueError):
        repair.record_revision(database, rows[0], originals[1], [claim()],
            rules_version=repair.RULES_VERSION, origin='luna_review', reviewer='luna:test',
            status='resolved', reason='Same activity.', source_review=review)
    assert database.execute('SELECT count(*) FROM bulletin_date_revisions').fetchone()[0] == 0


def test_legacy_receipt_stays_readable_and_never_projects_activity_or_changes_input(database):
    review = source_review()
    review.pop('activity')
    original = deepcopy(review)
    assert repair._validate_source_review(review)['activity'] is None
    projected = repair.public_source_review(review, rules_version='luna-content-evidence-v3')
    assert projected['status'] == 'secondary_only' and projected['activity'] is None
    assert projected['sources'][0]['quotes'] == [] and review == original
    rows, originals, _ = repair.date_snapshot(database)
    with database:
        repair.record_revision(database, rows[0], originals[1], [claim()],
            rules_version='luna-content-evidence-v3', origin='luna_review', reviewer='luna:historical',
            status='resolved', reason='Historical date-only review.', source_review=source_review())
    saved = database.execute('SELECT source_review_json FROM bulletin_date_revisions').fetchone()[0]
    assert json.loads(saved)['activity'] is None
    assert repair.public_source_review(saved, rules_version='luna-content-evidence-v3')['activity'] is None


@pytest.mark.parametrize('capture', ['homepage', 'blocked', 'no_link'])
def test_programmatic_source_downgrade_clears_model_activity_before_admission(database, monkeypatch, capture):
    from nannanews import acquisition
    rows, originals, _ = repair.date_snapshot(database)
    occurrence = dict(rows[0], body_text=SOURCE_TEXT, related_occurrences=[])
    document = _fetched_document()
    if capture == 'homepage':
        document['resolved_url'] = 'https://primary.example.org/'
    elif capture == 'no_link':
        occurrence['source_urls'] = []
    cache = {SOURCE_URL: ({'error': {'code': 'source_blocked', 'reason': 'Public access blocked.'}}
                         if capture == 'blocked' else {'document': document, 'media': []})}
    response = _model_response()
    response['citations'] = []
    response['activity'] = None
    def request(instructions, data, **kwargs):
        output = reference_response(response, data)
        output['claims'][0]['quote'] = {'ref': next(entry['id'] for entry in
            data['evidence_catalog']['entries'] if entry['origin'] == 'bulletin.body_text')}
        return output, 'test-model'
    monkeypatch.setattr(acquisition, 'request_model_json', request)
    decision = repair.model_decision(occurrence, originals[1], source_cache=cache)
    expected = {'homepage': 'source_mismatch', 'blocked': 'source_unavailable', 'no_link': 'secondary_only'}
    assert decision['status'] == 'resolved'  # Independent secondary calendar proposal survives.
    assert decision['source_review']['status'] == expected[capture]
    assert decision['source_review']['activity'] is None
    assert admit(decision['source_review'])['activity'] is None


@pytest.mark.parametrize('change', ['missing_activity', 'null_activity', 'unadmitted_evidence'])
def test_fresh_verified_model_verdict_requires_complete_grounded_activity(database, monkeypatch, change):
    from nannanews import acquisition
    rows, originals, _ = repair.date_snapshot(database)
    response = _model_response()
    if change == 'missing_activity':
        response.pop('activity')
    elif change == 'null_activity':
        response['activity'] = None
    else:
        response['activity']['content']['evidence'][0]['text'] = 'Invented source content.'
    monkeypatch.setattr(acquisition, 'request_model_json', lambda instructions, data, **kwargs:
        (reference_response(response, data), 'test-model'))
    decision = repair.model_decision(rows[0], originals[1],
        source_cache={SOURCE_URL: {'document': _fetched_document(), 'media': []}})
    assert decision['status'] == 'unresolved'
    assert decision['source_review']['activity'] is None
    assert decision['source_review']['status'] != 'source_verified'


def test_source_correction_metadata_cannot_become_activity_quotation(database, monkeypatch):
    from nannanews import acquisition
    rows, originals, _ = repair.date_snapshot(database)
    metadata = 'Operator matched the corrected link to a 15:00 seminar.'
    occurrence = dict(rows[0], source_corrections=[{
        'original_url': 'https://primary.example.org/wrong', 'corrected_url': SOURCE_URL,
        'reason': metadata, 'title': 'Operator evidence title', 'sha256': 'a' * 64,
        'fetched_at': '2026-10-17T00:00:00+00:00'}])
    response = _model_response()
    response['activity']['content']['evidence'][0]['text'] = metadata
    monkeypatch.setattr(acquisition, 'request_model_json', lambda instructions, data, **kwargs:
        (reference_response(response, data), 'test-model'))
    decision = repair.model_decision(occurrence, originals[1],
        source_cache={SOURCE_URL: {'document': _fetched_document(), 'media': []}})
    assert decision['status'] == 'unresolved' and decision['source_review']['activity'] is None
    assert metadata not in decision['source_review']['sources'][0]['quotes']
