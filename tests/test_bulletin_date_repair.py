"""Source-first date reviews remain append-only and mechanically auditable."""
from contextlib import closing
import base64
import hashlib
from html import escape
import json
from pathlib import Path
import sqlite3
import stat

from types import SimpleNamespace

import pytest


from nannanews import bulletin_date_repair as repair

SOURCE_URL = 'https://primary.example.org/lecture'
SOURCE_TEXT = 'Event held on November 27, 2025. The organizing faculty presents a complete public lecture.'


@pytest.fixture
def database():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
        CREATE TABLE bulletin_issues(id INTEGER PRIMARY KEY,issue_date TEXT);
        CREATE TABLE bulletin_documents(id INTEGER PRIMARY KEY,issue_id INTEGER,source_sha256 TEXT);
        CREATE TABLE bulletin_items(id INTEGER PRIMARY KEY,identity_key TEXT);
        CREATE TABLE bulletin_occurrences(id INTEGER PRIMARY KEY,item_id INTEGER,document_id INTEGER,
            local_key TEXT,kind TEXT,title TEXT,body_text TEXT,locator_json TEXT);
        CREATE TABLE bulletin_dates(id INTEGER PRIMARY KEY,occurrence_id INTEGER,kind TEXT,
            raw_text TEXT,normalized_date TEXT,resolution TEXT,evidence TEXT,locator_json TEXT);
        CREATE TABLE bulletin_sources(occurrence_id INTEGER,source_url TEXT);
        INSERT INTO bulletin_issues VALUES(1,'2026-10-13');
        INSERT INTO bulletin_documents VALUES(1,1,'source-digest');
        INSERT INTO bulletin_items VALUES(1,'activity');
        INSERT INTO bulletin_occurrences VALUES(1,1,1,'section-1','detail','讲座','活动日期见链接','{}');
        INSERT INTO bulletin_dates VALUES(1,1,'event','刊物中未写日期',NULL,'ambiguous','原文链接中的日期待核实','null');
        INSERT INTO bulletin_sources VALUES(1,'https://primary.example.org/lecture');
    ''')
    repair.create_date_schema(db)
    yield db
    db.close()


def source_snapshot(*, url=SOURCE_URL, resolved_url=SOURCE_URL, text=SOURCE_TEXT, quotes=(SOURCE_TEXT,)):
    raw = base64.b64decode(_fetched_document(text=text)['raw_response_base64'])
    return {'url': url, 'resolved_url': resolved_url, 'title': 'Lecture details', 'text': text,
        'raw_response_base64': base64.b64encode(raw).decode('ascii'),
        'raw_response_digest': hashlib.sha256(raw).hexdigest(),
        'fetched_at': '2026-10-14T00:00:00+00:00', 'published_date': '2025-11-20',
        'raw_date_text': 'Published: 20 November 2025', 'source_type': 'public_html',
        'content_type': 'text/html; charset=utf-8', 'quotes': list(quotes), 'images': [],
        'quote_evidence': [{'text': quote, 'image_index': None} for quote in quotes]}


def activity(*, url=SOURCE_URL, quote=SOURCE_TEXT, image_index=None):
    citation = {'url': url, 'text': quote, 'image_index': image_index}
    return {
        'title': {'value': 'Activity announcement', 'evidence': [dict(citation)]},
        'summary': {'value': quote, 'evidence': [dict(citation)]},
        'content': {'value': quote, 'evidence': [dict(citation)]},
        'venue': {'value': None, 'evidence': []},
        'organizer': {'value': None, 'evidence': []},
        'audience': {'value': None, 'evidence': []},
        'participation': {'value': None, 'evidence': []},
        'time': {'value': None, 'evidence': []},
        'colleges': [], 'image_indices': [],
    }


def source_review(*, status='source_verified', sources=None, reason='Primary article confirms this date.'):
    sources = [source_snapshot()] if sources is None else sources
    content = (activity(url=sources[0]['url'], quote=sources[0]['quotes'][0])
               if status == 'source_verified' and sources and sources[0]['quotes'] else None)
    return {'status': status, 'reason': reason, 'sources': sources, 'attempts': [], 'activity': content}


def claim(day='2025-11-27', raw=SOURCE_TEXT):
    return {'kind': 'event', 'raw_text': raw, 'normalized_date': day, 'resolution': 'explicit',
            'evidence': raw, 'locator': None}


def prepared(row, original_claims, *, claims=None, status='resolved', review=None, reason='Primary article supports the event date.'):
    return {'item_id': repair._public_id('item-', row['identity_key']),
        'occurrence_id': repair._public_id('occ-', row['source_sha256'] + ':' + row['local_key']),
        'document_sha256': row['source_sha256'], 'local_key': row['local_key'], 'status': status,
        'claims': [claim()] if claims is None and status == 'resolved' else (claims or []),
        'reason': reason, 'rules_version': repair.RULES_VERSION, 'reviewer': 'luna:test',
        'source_review': source_review() if review is None else review,
        'input_sha256': repair.input_digest(row, original_claims),
        'related_context_sha256': row['related_context_sha256']}


def test_empty_plan_never_runs_or_applies_semantic_defaults(database):
    plan = repair.plan_repairs(database)
    assert plan['entries'] == []
    assert repair.apply_repairs(database, plan) == 0
    assert database.execute('SELECT count(*) FROM bulletin_date_revisions').fetchone()[0] == 0


def test_unreviewed_literal_dates_remain_unverified_until_ai_review(database):
    with database:
        database.execute("UPDATE bulletin_dates SET normalized_date='2026-10-15',resolution='explicit' WHERE id=1")
    assert database.execute('SELECT normalized_date FROM bulletin_effective_dates').fetchone()[0] == '2026-10-15'
    plan = repair.plan_repairs(database)
    assert plan['entries'] == []
    assert plan['summary']['before']['unknown_publication_groups'] == 0
    assert repair.public_source_review(None)['status'] == 'secondary_only'

def test_prepared_review_replaces_empty_or_raw_claims_and_preserves_immutability(database, tmp_path):
    before = [tuple(row) for row in database.execute('SELECT * FROM bulletin_dates')]
    rows, originals, _ = repair.date_snapshot(database)
    row = rows[0]
    path = tmp_path / 'review.json'
    path.write_text(json.dumps([prepared(row, originals[row['id']])]), encoding='utf-8')
    plan = repair.plan_repairs(database, review_files=[path])
    assert plan['entries'][0]['origin'] == 'luna_review'
    assert plan['entries'][0]['claims'][0]['normalized_date'] == '2025-11-27'
    with database:
        assert repair.apply_repairs(database, plan) == 1
    assert [tuple(row) for row in database.execute('SELECT * FROM bulletin_dates')] == before
    assert database.execute('SELECT normalized_date FROM bulletin_effective_dates').fetchone()[0] == '2025-11-27'
    saved = database.execute('SELECT source_review_json FROM bulletin_date_revisions').fetchone()[0]
    assert json.loads(saved)['sources'][0]['raw_response_digest'] == source_snapshot()['raw_response_digest']
    for table in ('bulletin_date_revisions', 'bulletin_date_revision_claims'):
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            database.execute(f'DELETE FROM {table}')
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            database.execute(f'UPDATE {table} SET id=id+10')


def test_stale_prepared_review_is_rejected_before_any_revision(database, tmp_path):
    rows, originals, _ = repair.date_snapshot(database)
    row = rows[0]
    path = tmp_path / 'review.json'
    path.write_text(json.dumps([prepared(row, originals[row['id']])]), encoding='utf-8')
    plan = repair.plan_repairs(database, review_files=[path])
    with database:
        database.execute("INSERT INTO bulletin_occurrences VALUES(2,1,1,'section-2','summary','讲座','日期待定','{}')")
    with pytest.raises(ValueError, match='snapshot changed'):
        repair.apply_repairs(database, plan)
    assert database.execute('SELECT count(*) FROM bulletin_date_revisions').fetchone()[0] == 0


def test_entire_application_rolls_back_when_a_prepared_claim_has_an_invalid_calendar(database, tmp_path):
    with database:
        database.execute("INSERT INTO bulletin_occurrences VALUES(2,1,1,'section-2','summary','讲座','日期待定','{}')")
    rows, originals, _ = repair.date_snapshot(database)
    path = tmp_path / 'reviews.json'
    path.write_text(json.dumps([prepared(row, originals[row['id']], claims=[], status='unresolved', review={
        'status': 'secondary_only', 'reason': 'No primary source.', 'sources': [], 'attempts': [], 'activity': None})
        for row in rows]), encoding='utf-8')
    plan = repair.plan_repairs(database, review_files=[path])
    plan['entries'][1]['claims'] = [claim(day='2026-02-30')]
    with pytest.raises(ValueError, match='invalid calendar'):
        repair.apply_repairs(database, plan)
    assert database.execute('SELECT count(*) FROM bulletin_date_revisions').fetchone()[0] == 0
    assert database.execute('SELECT normalized_date FROM bulletin_dates').fetchone()[0] is None


def test_apply_is_idempotent_and_old_review_cannot_replace_newer_revision(database, tmp_path):
    rows, originals, _ = repair.date_snapshot(database)
    row = rows[0]
    path = tmp_path / 'review.json'
    old = prepared(row, originals[row['id']])
    path.write_text(json.dumps([old]), encoding='utf-8')
    first = repair.plan_repairs(database, review_files=[path])
    assert repair.apply_repairs(database, first) == 1
    assert repair.apply_repairs(database, repair.plan_repairs(database, review_files=[path])) == 0
    newer = prepared(row, originals[row['id']], reason='A newer source review is retained.')
    newer['source_review'] = source_review(reason='Second capture review.')
    path.write_text(json.dumps([newer]), encoding='utf-8')
    assert repair.apply_repairs(database, repair.plan_repairs(database, review_files=[path])) == 1
    rows, originals, _ = repair.date_snapshot(database)
    with pytest.raises(ValueError, match='older decision'):
        with database:
            repair.record_revision(database, rows[0], originals[1], old['claims'],
                rules_version=old['rules_version'], origin='luna_review', reviewer=old['reviewer'],
                status=old['status'], reason=old['reason'], source_review=old['source_review'])
    assert database.execute('SELECT count(*) FROM bulletin_date_revisions').fetchone()[0] == 2


def test_legacy_unbound_artifacts_cannot_enter_current_audit(database, tmp_path):
    row = repair.date_snapshot(database)[0][0]
    legacy_claim = {'kind': 'event', 'raw_text': '活动日期见链接', 'normalized_date': None,
        'resolution': 'ambiguous', 'evidence': '活动日期见链接', 'locator': None}
    value = {'item_id': repair._public_id('item-', row['identity_key']),
        'occurrence_id': repair._public_id('occ-', row['source_sha256'] + ':' + row['local_key']),
        'document_sha256': row['source_sha256'], 'local_key': row['local_key'], 'status': 'needs_context',
        'claims': [legacy_claim], 'reason': 'Historical completed review.'}
    path = tmp_path / 'legacy.json'
    path.write_text(json.dumps([value]), encoding='utf-8')
    with pytest.raises(ValueError, match='fresh input-bound'):
        repair.plan_repairs(database, review_files=[path])
    assert database.execute('SELECT count(*) FROM bulletin_date_revisions').fetchone()[0] == 0


def test_fabricated_citations_and_unlinked_source_urls_are_rejected(database, tmp_path):
    rows, originals, _ = repair.date_snapshot(database)
    row = rows[0]
    fake_source = source_snapshot(url='https://evil.example/forged')
    value = prepared(row, originals[row['id']], review=source_review(sources=[fake_source]))
    path = tmp_path / 'review.json'
    path.write_text(json.dumps([value]), encoding='utf-8')
    with pytest.raises(ValueError):
        repair.plan_repairs(database, review_files=[path])
    fake_source = source_snapshot(text='Different article text.', quotes=['fabricated quotation'])
    value = prepared(row, originals[row['id']], review=source_review(sources=[fake_source]))
    path.write_text(json.dumps([value]), encoding='utf-8')
    with pytest.raises(ValueError):
        repair.plan_repairs(database, review_files=[path])


def test_public_source_projection_excludes_snapshot_bodies_and_legacy_is_not_verified():
    projected = repair.public_source_review(source_review(), rules_version=repair.RULES_VERSION)
    assert set(projected) == {'status', 'reason', 'sources', 'activity'}
    assert projected['activity'] == source_review()['activity']
    assert repair.public_source_review(source_review(), rules_version='luna-content-evidence-v3')['activity'] is None
    assert set(projected['sources'][0]) == {'url', 'resolved_url', 'title', 'sha256', 'fetched_at', 'published_date', 'quotes'}
    assert projected['sources'][0]['sha256'] == source_snapshot()['raw_response_digest']
    assert repair.public_source_review('{}')['status'] == 'secondary_only'


def _model_response(url=SOURCE_URL, quote=SOURCE_TEXT, *, source_status='source_verified'):
    return {'status': 'resolved', 'reason': 'The primary article gives the event date.',
        'claims': [claim()], 'source_status': source_status, 'source_reason': 'The linked article identifies the event.',
        'citations': [{'url': url, 'quotes': [{'text': quote, 'image_index': None}]}],
        'activity': activity(url=url, quote=quote) if source_status == 'source_verified' else None}


def reference_response(canonical, source_data):
    """Make fake live model selections; never used by production/offline admission."""
    from copy import deepcopy
    response = deepcopy(canonical)
    catalog = source_data['evidence_catalog']
    response['evidence_scope'] = catalog['scope']

    def selection(text, *, url=None, primary=False):
        entry = next((entry for entry in catalog['entries']
            if text in entry['text'] and (not primary or entry['origin'] == 'primary.text')
            and (url is None or entry['url'] == url)), None)
        return {'ref': entry['id'] if entry else 'unknown-test-reference'}

    def evidence(item):
        if item['image_index'] is not None:
            return dict(item)
        return {'url': item['url'], **selection(item['text'], url=item['url'], primary=True)}

    for citation in response.get('citations', []):
        citation['quotes'] = [dict(quote) if quote['image_index'] is not None else
            selection(quote['text'], url=citation['url'], primary=True) for quote in citation['quotes']]
    activity_value = response.get('activity')
    if activity_value is not None:
        for name in ('title', 'summary', 'content', 'venue', 'organizer', 'audience', 'participation', 'time'):
            if name in activity_value:
                activity_value[name]['evidence'] = [evidence(item) for item in activity_value[name]['evidence']]
        for college in activity_value['colleges']:
            college['evidence'] = evidence(college['evidence'])
    for item in response.get('claims', []):
        text = item.pop('raw_text')
        image = next((dict(quote, url=citation['url']) for citation in canonical.get('citations', [])
            for quote in citation['quotes'] if quote['image_index'] is not None and text == quote['text']), None)
        item['quote'] = image if image is not None else selection(text)
    return response


def source_data_from_payload(payload):
    content = (payload['input'] if 'input' in payload else payload['messages'])[-1]['content']
    return json.loads(content[0]['text'].split('\n', 1)[1])


def _fetched_document(*, resolved_url=SOURCE_URL, text=SOURCE_TEXT):
    raw = ('<html><head><meta property="article:published_time" content="2025-11-20"></head>'
           '<body><h1>Lecture details</h1><article>' + escape(text) + '</article></body></html>').encode('utf-8')
    return {'canonical_url': SOURCE_URL, 'resolved_url': resolved_url, 'title': 'Lecture details',
        'text': text, 'images': [], 'raw_response_base64': base64.b64encode(raw).decode('ascii'),
        'raw_response_digest': hashlib.sha256(raw).hexdigest(), 'fetched_at': '2026-10-14T00:00:00+00:00',
        'source_type': 'public_html', 'raw_date_text': 'Published: 20 November 2025',
        'source_published_date': '2025-11-20', 'content_type': 'text/html; charset=utf-8'}


def test_source_stage_measurements_do_not_change_capture_or_retime_cached_evidence(monkeypatch):
    from nannanews import acquisition
    clock = [0.0]
    monkeypatch.setattr(repair, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    image_url = SOURCE_URL + '/poster.png'
    raw_image = b'captured poster bytes'
    image = {'url': image_url, 'mime': 'image/png',
             'digest': hashlib.sha256(raw_image).hexdigest(),
             'data': base64.b64encode(raw_image).decode('ascii')}

    def fetch(url):
        clock[0] += 2
        return dict(_fetched_document(), images=[image_url])

    def prepare_media(document):
        clock[0] += 3
        return [image]

    monkeypatch.setattr(acquisition, 'fetch_document', fetch)
    monkeypatch.setattr(acquisition, '_prepare_media', prepare_media)
    cache, first_timing = {}, {}
    first = repair._capture_sources({'source_urls': [SOURCE_URL]}, cache, _timing=first_timing)
    assert len(first) == 5
    assert first_timing['source_fetch_seconds'] == 2
    assert first_timing['source_media_prepare_seconds'] == 3
    assert first_timing['source_cache_misses'] == 1
    retained = json.dumps(cache, sort_keys=True)
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: pytest.fail('cached source was refetched'))
    monkeypatch.setattr(acquisition, '_prepare_media', lambda document: pytest.fail('cached media was re-prepared'))
    clock[0] += 100
    reused_timing = {}
    assert repair._capture_sources({'source_urls': [SOURCE_URL]}, cache, _timing=reused_timing) == first
    assert reused_timing['source_cache_hits'] == 1
    assert 'source_fetch_seconds' not in reused_timing
    assert 'source_media_prepare_seconds' not in reused_timing
    assert json.dumps(cache, sort_keys=True) == retained
    legacy = {SOURCE_URL: {key: value for key, value in cache[SOURCE_URL].items() if key != '_timing'}}
    old_bytes = json.dumps(legacy, sort_keys=True)
    assert repair._capture_sources({'source_urls': [SOURCE_URL]}, legacy, _timing={}) == first
    assert json.dumps(legacy, sort_keys=True) == old_bytes


@pytest.mark.parametrize('failure_stage', ['fetch', 'media'])
def test_failed_source_stages_keep_actual_durations_without_automatic_retries(monkeypatch, failure_stage):
    from nannanews import acquisition
    clock = [0.0]
    monkeypatch.setattr(repair, 'time', SimpleNamespace(monotonic=lambda: clock[0]))

    def fetch(url):
        clock[0] += 2
        if failure_stage == 'fetch':
            raise acquisition.AcquisitionError('Unavailable source.', 'source_unavailable')
        return dict(_fetched_document(), images=[SOURCE_URL + '/poster.png'])

    def media(document):
        clock[0] += 3
        raise acquisition.AcquisitionError('Unavailable poster.', 'source_image_unavailable')

    monkeypatch.setattr(acquisition, 'fetch_document', fetch)
    monkeypatch.setattr(acquisition, '_prepare_media', media)
    cache, timing = {}, {}
    first = repair._capture_sources({'source_urls': [SOURCE_URL]}, cache, _timing=timing)
    assert timing['source_fetch_seconds'] == 2
    if failure_stage == 'media':
        assert timing['source_media_prepare_seconds'] == 3
        assert first[1][0]['status'] == 'media_unavailable'
    else:
        assert 'source_media_prepare_seconds' not in timing
        assert first[1][0]['status'] == 'unavailable'
    retained = json.dumps(cache, sort_keys=True)
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: pytest.fail('failed source was retried'))
    monkeypatch.setattr(acquisition, '_prepare_media', lambda document: pytest.fail('failed media was retried'))
    reused = {}
    assert repair._capture_sources({'source_urls': [SOURCE_URL]}, cache, _timing=reused) == first
    assert reused['source_cache_hits'] == 1
    assert 'source_fetch_seconds' not in reused and 'source_media_prepare_seconds' not in reused
    assert json.dumps(cache, sort_keys=True) == retained


def test_model_diagnostics_are_optional_and_never_enter_input_catalog_or_verdict(database, monkeypatch):
    from nannanews import acquisition
    rows, originals, _ = repair.date_snapshot(database)
    clock, requests = [0.0], []
    monkeypatch.setattr(repair, 'time', SimpleNamespace(monotonic=lambda: clock[0]))

    def fetch(url):
        clock[0] += 2
        return _fetched_document()

    def request(instructions, source_data, media=(), **kwargs):
        requests.append((instructions, json.dumps(source_data, sort_keys=True),
                         json.dumps(kwargs['response_schema'], sort_keys=True), list(media)))
        clock[0] += 7
        return reference_response(_model_response(), source_data), 'test-model'

    monkeypatch.setattr(acquisition, 'fetch_document', fetch)
    monkeypatch.setattr(acquisition, 'request_model_json', request)
    cache, timing = {}, {}
    measured = repair.model_decision(rows[0], originals[1], source_cache=cache, _timing=timing)
    unmeasured = repair.model_decision(rows[0], originals[1], source_cache=cache)
    assert measured == unmeasured
    assert requests[0] == requests[1]
    assert timing['source_capture_seconds'] == timing['source_fetch_seconds'] == 2
    assert timing['review_preparation_seconds'] == 2
    assert timing['model_request_seconds'] == 7
    assert '_timing' not in measured
    assert set(repair.public_source_review(measured['source_review'], rules_version=repair.RULES_VERSION)) == {
        'status', 'reason', 'sources', 'activity'}


def test_model_decision_uses_full_linked_source_and_accepts_non_regex_date_quote(database, monkeypatch):
    from nannanews import acquisition
    rows, originals, _ = repair.date_snapshot(database)
    seen = {}
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: _fetched_document(
        resolved_url='https://cdn.primary.example.net/lecture'))
    def request(instructions, source_data, media=(), **kwargs):
        seen['source'] = source_data['linked_primary_sources'][0]
        assert '20 November 2025' in seen['source']['raw_date_text']
        assert seen['source']['text'] == SOURCE_TEXT
        return reference_response(_model_response(), source_data), 'test-model'
    monkeypatch.setattr(acquisition, 'request_model_json', request)
    decision = repair.model_decision(rows[0], originals[1], source_cache={})
    assert decision['status'] == 'resolved'
    assert decision['claims'][0]['normalized_date'] == '2025-11-27'
    assert decision['source_review']['status'] == 'source_verified'
    assert decision['source_review']['sources'][0]['resolved_url'] == 'https://cdn.primary.example.net/lecture'
    assert decision['reviewer'] == 'luna:test-model'


def test_image_only_date_quote_is_bound_to_captured_poster_bytes(database, monkeypatch):
    from nannanews import acquisition
    rows, originals, _ = repair.date_snapshot(database)
    image_url = 'https://primary.example.org/poster.png'
    image_bytes = b'captured-poster-bytes'
    prepared = {'mime': 'image/png', 'digest': hashlib.sha256(image_bytes).hexdigest(),
                'data': base64.b64encode(image_bytes).decode('ascii'), 'url': image_url}
    document = _fetched_document(text='学术报告：Prof. James Kwok')
    document['images'] = [image_url]
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: document)
    monkeypatch.setattr(acquisition, '_prepare_media', lambda source: [prepared])
    quote = '时间：10月29日(星期三) 15:00'
    response = {'status': 'needs_context', 'reason': 'The poster gives month and day but no year.',
        'claims': [{'kind': 'event', 'raw_text': quote, 'normalized_date': None,
            'resolution': 'year_unknown', 'evidence': quote, 'locator': None}],
        'source_status': 'source_verified', 'source_reason': 'The linked poster is the activity announcement.',
        'citations': [{'url': SOURCE_URL, 'quotes': [{'text': quote, 'image_index': 0}]}],
        'activity': activity(quote=quote, image_index=0)}
    def request(instructions, source_data, media=(), **kwargs):
        assert source_data['linked_primary_sources'][0]['images'][0]['image_index'] == 0
        assert media[0]['url'] == image_url
        return reference_response(response, source_data), 'test-model'
    monkeypatch.setattr(acquisition, 'request_model_json', request)
    decision = repair.model_decision(rows[0], originals[1], source_cache={})
    assert decision['status'] == 'needs_context'
    assert decision['claims'][0]['normalized_date'] is None
    captured = decision['source_review']['sources'][0]
    assert base64.b64decode(captured['images'][0]['data']) == image_bytes
    assert captured['quote_evidence'] == [{'text': quote, 'image_index': 0}]
    public = repair.public_source_review(decision['source_review'], rules_version=decision['rules_version'])
    assert public['sources'][0]['quotes'] == [quote]
    assert 'images' not in public['sources'][0] and 'data' not in public['sources'][0]


def test_newsletter_archive_backlink_is_never_acquired_as_primary(database, monkeypatch):
    from nannanews import acquisition
    rows, originals, _ = repair.date_snapshot(database)
    occurrence = dict(rows[0], source_urls=['https://nik-nul.github.io/news/2026-10-14.html'])
    occurrence['related_occurrences'] = []
    def should_not_fetch(url):
        raise AssertionError('A secondary newsletter backlink was fetched as a primary source.')
    def secondary_only_review(instructions, source_data, media=(), **kwargs):
        canonical = {'status': 'no_relevant_date', 'reason': 'No primary source was linked.', 'claims': [],
            'source_status': 'secondary_only', 'source_reason': 'No primary evidence.',
            'citations': [], 'activity': None}
        return reference_response(canonical, source_data), 'test-model'
    monkeypatch.setattr(acquisition, 'fetch_document', should_not_fetch)
    monkeypatch.setattr(acquisition, 'request_model_json', secondary_only_review)
    decision = repair.model_decision(occurrence, originals[1], source_cache={})
    assert decision['source_review']['status'] == 'secondary_only'
    assert decision['source_review']['sources'] == []
def test_homepage_redirect_and_captcha_are_not_verified_sources(database, monkeypatch):
    from nannanews import acquisition
    rows, originals, _ = repair.date_snapshot(database)
    homepage = _fetched_document(resolved_url='https://primary.example.org/', text=SOURCE_TEXT)
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: homepage)
    monkeypatch.setattr(acquisition, 'request_model_json', lambda instructions, data, **kwargs:
        (reference_response(_model_response(), data), 'test-model'))
    mismatch = repair.model_decision(rows[0], originals[1], source_cache={})
    assert mismatch['source_review']['status'] == 'source_mismatch'
    assert mismatch['status'] == 'unresolved'
    def unavailable(url):
        error = RuntimeError('captcha challenge')
        error.code = 'source_blocked'
        raise error
    monkeypatch.setattr(acquisition, 'fetch_document', unavailable)
    def unavailable_review(instructions, source_data, media=(), **kwargs):
        canonical = {'status': 'unresolved', 'reason': 'Primary source unavailable.', 'claims': [],
            'source_status': 'source_unavailable', 'source_reason': 'Captcha blocked public reading.',
            'citations': [], 'activity': None}
        return reference_response(canonical, source_data), 'test-model'
    monkeypatch.setattr(acquisition, 'request_model_json', unavailable_review)
    blocked = repair.model_decision(rows[0], originals[1], source_cache={})
    assert blocked['source_review']['status'] == 'source_unavailable'
    assert blocked['source_review']['attempts'][0]['code'] == 'source_blocked'


def test_review_cli_artifact_can_be_applied_without_repeating_model_call(database, tmp_path, monkeypatch, capsys):
    from nannanews import acquisition
    from nannanews import cli as manage
    from nannanews import runtime
    with database:
        database.execute("INSERT INTO bulletin_issues VALUES(2,'2026-10-14')")
        database.execute("INSERT INTO bulletin_documents VALUES(2,2,'source-digest-2')")
        database.execute("INSERT INTO bulletin_occurrences VALUES(2,1,2,'section-2','detail','讲座','活动日期见链接','{}')")
        database.execute("INSERT INTO bulletin_dates VALUES(2,2,'event','刊物中未写日期',NULL,'ambiguous','原文链接中的日期待核实','null')")
        database.execute('INSERT INTO bulletin_sources VALUES(2,?)', (SOURCE_URL,))
    rows, _, _ = repair.date_snapshot(database)
    from nannanews import schema
    monkeypatch.setenv('NANNA_DATA_DIR', str(tmp_path / 'runtime'))
    monkeypatch.setattr(runtime, 'settings', runtime.load_settings())
    schema.migrate()
    database_path = runtime.settings.database_path
    with closing(runtime.connect_db()) as disk, disk:
        for row in database.execute('SELECT * FROM bulletin_issues'):
            disk.execute('INSERT INTO bulletin_issues(id,identity_key,title,issue_date) VALUES(?,?,?,?)',
                         (row['id'], f"issue:{row['id']}", '测试原刊', row['issue_date']))
        for row in database.execute('SELECT * FROM bulletin_documents'):
            disk.execute('''INSERT INTO bulletin_documents
                (id,issue_id,source_sha256,format,source_filename,issue_date_evidence,
                 parser_json,parsed_json,text,original_path,normalized_path,imported_at,actor)
                VALUES(?,?,?,'html','fixture.html','原刊日期','{}','{}','讲座原刊',
                       'fixture.html','normalized.json','2026-10-14','test')''',
                (row['id'], row['issue_id'], row['source_sha256']))
        for row in database.execute('SELECT * FROM bulletin_items'):
            disk.execute('INSERT INTO bulletin_items VALUES(?,?,?)',
                         (row['id'], row['identity_key'], 'test-source'))
        for row in database.execute('SELECT * FROM bulletin_occurrences'):
            disk.execute('''INSERT INTO bulletin_occurrences
                (id,item_id,document_id,local_key,kind,title,body_text,locator_json,image_refs_json,raw_item_json)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (*row, '[]', '{}'))
        for table in ('bulletin_dates', 'bulletin_sources'):
            for row in database.execute(f'SELECT * FROM {table}'):
                disk.execute(f'INSERT INTO {table} VALUES({",".join("?" for _ in row)})', tuple(row))
    monkeypatch.setattr(acquisition, '_model_config', lambda: SimpleNamespace(model_name='test-model', model_api_style='chat_completions'))
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: _fetched_document())
    calls = []
    def post(payload, *, audit=None):
        source_data = source_data_from_payload(payload)
        calls.append(source_data)
        assert set(source_data['bulletin']['related_occurrences'][0]['issue_dates']) == {'2026-10-13', '2026-10-14'}
        return {'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(reference_response(_model_response(), source_data))}}]}
    monkeypatch.setattr(acquisition, '_model_post', post)
    output = tmp_path / 'private-review'
    occurrence_ids = [repair._public_id('occ-', row['source_sha256'] + ':' + row['local_key']) for row in rows]
    command = ['review-bulletin-dates', '--output', str(output)]
    for occurrence_id in occurrence_ids:
        command.extend(['--occurrence-id', occurrence_id])
    assert manage.main(command) == 0
    generated = json.loads(capsys.readouterr().out)
    assert generated['new_reviews'] == 2 and generated['model_request_attempts'] == 1
    assert generated['reused_reviews'] == 1 and len(calls) == 1
    artifact = Path(generated['artifact'])
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert manage.main(command) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed['new_reviews'] == 0 and resumed['model_request_attempts'] == 0 and len(calls) == 1
    assert manage.main(['repair-bulletin-dates', '--review', str(artifact), '--apply']) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied['appended_revisions'] == 2 and len(calls) == 1
    with closing(sqlite3.connect(database_path)) as db:
        saved = [json.loads(row[0]) for row in db.execute('SELECT source_review_json FROM bulletin_date_revisions ORDER BY occurrence_id')]
    assert all(repair.public_source_review(review, rules_version=repair.RULES_VERSION)['status'] == 'source_verified'
               for review in saved)


def test_historical_model_input_records_exclusions_without_extending_canonical_sources(database, monkeypatch):
    from nannanews import acquisition
    from nannanews import source_media

    rows, originals, _ = repair.date_snapshot(database)
    document = _fetched_document()
    gif_url = 'https://mmbiz.qpic.cn/excluded/640?wx_fmt=gif'
    document['media_policy'] = source_media.media_policy()
    document['skipped_media'] = [source_media.skipped_media_record('gif', gif_url, 'declared_gif_excluded')]
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: document)
    seen = {}

    def request(instructions, source_data, media=(), **kwargs):
        seen.update(instructions=instructions, data=source_data, media=media)
        return reference_response(_model_response(), source_data), 'test-model'

    monkeypatch.setattr(acquisition, 'request_model_json', request)
    decision = repair.model_decision(rows[0], originals[1], source_cache={})
    metadata = seen['data']['source_media'][0]
    assert metadata['url'] == SOURCE_URL
    assert metadata['media_policy']['version'] == source_media.REMOTE_MEDIA_POLICY_VERSION
    assert metadata['skipped_media'][0]['url'] == gif_url
    assert seen['media'] == []
    assert source_media.MEDIA_POLICY_INSTRUCTIONS in seen['instructions']
    assert 'media_policy' not in decision['source_review']['sources'][0]
    assert 'skipped_media' not in decision['source_review']['sources'][0]
