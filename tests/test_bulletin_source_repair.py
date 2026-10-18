"""Real SQLite coverage for explicit operator source corrections; no model/import calls."""
import base64
from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import stat


import pytest


from nannanews import bulletin_activities
from nannanews import bulletin_date_repair as dates
from nannanews import bulletin_source_repair as sources
from nannanews import bulletin_store as store
from nannanews import runtime
from nannanews import static_export
from nannanews import schema as migrations

OLD = 'https://job.nju.edu.cn/career/specifics/631465098736570369'
NEW = 'https://job.nju.edu.cn/career/specifics/631153401433952258'
TITLE = '海南鼎方集团&华研生物2026年招聘宣讲会'
PRIVATE = 'PRIVATE_OPERATOR_CAPTURE_SENTINEL'
PDF = b'%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n'


def capture(url=NEW):
    raw = ('<html><article>' + TITLE + PRIVATE + '</article></html>').encode()
    return {'url': url, 'resolved_url': url, 'title': TITLE,
        'text': TITLE + ' 2026-10-15 14:00 301大会议室 ' + PRIVATE,
        'raw_response_base64': base64.b64encode(raw).decode(),
        'raw_response_digest': hashlib.sha256(raw).hexdigest(),
        'fetched_at': '2026-10-16T10:00:00+08:00', 'published_date': None,
        'raw_date_text': None, 'source_type': 'public_html', 'content_type': 'text/html',
        'quotes': []}


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    private = tmp_path / 'private'
    upload = private / 'uploads'
    upload.mkdir(parents=True)
    settings = replace(runtime.settings, data_dir=private, upload_dir=upload,
                       database_path=private / 'database.db', model_base_url='', model_api_key='')
    monkeypatch.setattr(runtime, 'settings', settings)
    migrations.migrate()
    digest = hashlib.sha256(PDF).hexdigest()
    relative = 'bulletins/' + digest + '/original.pdf'
    original = upload / relative
    original.parent.mkdir(parents=True)
    original.write_bytes(PDF)
    with closing(runtime.connect_db()) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("INSERT INTO bulletin_issues VALUES(1,'issue:test','测试原刊','2026-10-13',1)")
        db.execute('''INSERT INTO bulletin_documents VALUES(1,1,?,'pdf','original.pdf','原刊日期',
            '{}',?,'原刊全文',?,NULL,'normalized.json','2026-10-13','operator:import')''',
            (digest, json.dumps({'issue_date': '2026-10-13', 'claimed_issue_date': '2026-10-13', 'warnings': []}), relative))
        db.executemany('INSERT INTO bulletin_items VALUES(?,?,?)', [(1, 'hainan', 'source'), (2, 'other', 'source')])
        for occurrence_id, item_id in ((1, 1), (2, 1), (3, 2)):
            raw = {'dates': []}
            db.execute('INSERT INTO bulletin_occurrences VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (occurrence_id, item_id, 1, 'p1-' + str(occurrence_id), 'detail', TITLE, '招聘',
                 '原刊：10月15日14:00-16:00 301大会议室 ' + OLD, '{}', '[]', json.dumps(raw)))
            db.execute('INSERT INTO bulletin_dates VALUES(?,?,?,?,?,?,?,?)',
                (occurrence_id, occurrence_id, 'event', '10月15日', None, 'year_unknown', '原刊10月15日', 'null'))
        db.executemany('INSERT INTO bulletin_sources VALUES(?,?)', [(1, OLD), (3, OLD)])
        # A document-level provenance link must not resurrect the corrected wrong link.
        db.execute('INSERT INTO bulletin_document_sources VALUES(?,?,?)', (1, OLD, '{}'))
        rows, originals, _ = dates.date_snapshot(db)
        old_capture = capture(OLD)
        old_capture['quotes'] = [TITLE]
        for row in rows:
            dates.record_revision(db, row, originals[row['id']], [{
                'kind': 'event', 'raw_text': '2027-10-15', 'normalized_date': '2027-10-15',
                'resolution': 'explicit', 'evidence': '2027-10-15', 'locator': None}],
                rules_version='old-review', origin='luna_review', reviewer='luna:old',
                status='resolved', reason='Old source-derived decision.', source_review={
                    'status': 'source_verified', 'reason': 'Old primary-source verdict.',
                    'sources': [old_capture], 'attempts': []})
    return settings


def connect(settings):
    db = sqlite3.connect(settings.database_path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def review(db, occurrence_id=1, url=NEW):
    row = next(row for row in dates.date_snapshot(db)[0] if row['id'] == occurrence_id)
    public_id = dates._public_id('occ-', row['source_sha256'] + ':' + row['local_key'])
    return sources.prepare_review(db, occurrence_id=public_id, original_url=OLD,
        corrected_url=url, capture=capture(url), actor='operator:' + PRIVATE,
        reason='人工核对活动名称、14:00及301大会议室，原链接属于另一招聘活动。')


def raw_evidence(db):
    return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)] for table in (
        'bulletin_documents', 'bulletin_items', 'bulletin_occurrences', 'bulletin_sources',
        'bulletin_dates', 'bulletin_date_revisions', 'bulletin_date_revision_claims')}


def test_atomic_append_idempotence_originals_and_date_fallback(evidence):
    with closing(connect(evidence)) as db:
        before = raw_evidence(db)
        previous_rows, previous_originals, _ = dates.date_snapshot(db)
        previous_inputs = {row['id']: dates.input_digest(row, previous_originals[row['id']]) for row in previous_rows}
        material = review(db)
        plan = sources.plan_repairs(db, reviews=[material])
        assert sources.apply_repairs(db, plan) == 1
        assert sources.apply_repairs(db, plan) == 0
        assert sources.apply_repairs(db, sources.plan_repairs(db, reviews=[material])) == 0
        assert raw_evidence(db) == before
        assert [row[0] for row in db.execute('SELECT source_url FROM bulletin_effective_sources WHERE occurrence_id=1')] == [NEW]
        rows, originals, heads = dates.date_snapshot(db)
        assert set(heads) == {3}  # Same-activity context reviews are invalidated too.
        row = next(row for row in rows if row['id'] == 1)
        assert row['source_urls'] == [NEW]
        assert dates._occurrence_source_urls(row) == [NEW]
        assert dates.input_digest(row, originals[1]) != previous_inputs[1]
        assert db.execute('SELECT normalized_date FROM bulletin_effective_dates WHERE occurrence_id=1').fetchone()[0] is None
        assert db.execute('SELECT count(*) FROM bulletin_source_date_invalidations').fetchone()[0] == 2
        correction = sources.public_corrections(db)[1][0]
        assert correction['title'] == TITLE
        assert set(correction) == {'original_url', 'corrected_url', 'reason', 'title', 'sha256', 'fetched_at'}
        for table in ('bulletin_source_revisions', 'bulletin_source_date_invalidations'):
            for operation in ('DELETE FROM ' + table, 'UPDATE ' + table + ' SET rowid=rowid'):
                with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                    db.execute(operation)
    assert (evidence.upload_dir / ('bulletins/' + hashlib.sha256(PDF).hexdigest() + '/original.pdf')).read_bytes() == PDF
    item = store.get_item(1)
    assert next(occurrence for occurrence in item['occurrences'] if occurrence['occurrence_id'] == 1)['source_urls'] == [NEW]
    assert item['date_review']['status'] == 'unreviewed'
    assert item['date_review']['source_review']['status'] == 'secondary_only'
    assert store.query_items({'source': NEW})['total'] == 1
    assert store.query_items({'source': OLD})['total'] == 1  # Uncorrected other item only.
    assert store.query_items({'source': OLD})['records'][0]['id'] == 2
    assert store.query_items({'date_status': 'known', 'date_type': 'event'})['total'] == 1


def test_stale_head_original_binding_and_old_review_replay_are_rejected(evidence):
    with closing(connect(evidence)) as db:
        first = review(db)
        stale = review(db, url='https://primary.example.org/another')
        plan = sources.plan_repairs(db, reviews=[first])
        for field, value in [('document_sha256', '0' * 64), ('local_key', 'wrong'),
                             ('input_sha256', '0' * 64), ('original_url', NEW)]:
            with pytest.raises(ValueError):
                sources.plan_repairs(db, reviews=[dict(first, **{field: value})])
        sources.apply_repairs(db, plan)
        with pytest.raises(ValueError, match='head changed'):
            sources.plan_repairs(db, reviews=[stale])
        second = review(db, url='https://primary.example.org/another')
        sources.apply_repairs(db, sources.plan_repairs(db, reviews=[second]))
        with pytest.raises(ValueError, match='head changed'):
            sources.apply_repairs(db, plan)


def test_changed_date_head_rejects_source_plan_without_partial_application(evidence):
    with closing(connect(evidence)) as db:
        plan = sources.plan_repairs(db, reviews=[review(db), review(db, 3)])
        row, originals, _ = dates.date_snapshot(db)
        target = next(value for value in row if value['id'] == 1)
        with db:
            dates.record_revision(db, target, originals[1], [], rules_version='new-review',
                origin='luna_review', reviewer='luna:new', status='unresolved', reason='A new review arrived.')
        with pytest.raises(ValueError, match='snapshot changed'):
            sources.apply_repairs(db, plan)
        assert db.execute('SELECT count(*) FROM bulletin_source_revisions').fetchone()[0] == 0


def test_invalid_second_capture_rolls_back_entire_application(evidence):
    with closing(connect(evidence)) as db:
        plan = sources.plan_repairs(db, reviews=[review(db), review(db, 3)])
        plan['entries'][1]['review']['capture']['raw_response_digest'] = '0' * 64
        with pytest.raises(ValueError, match='hash does not match'):
            sources.apply_repairs(db, plan)
        assert db.execute('SELECT count(*) FROM bulletin_source_revisions').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM bulletin_source_date_invalidations').fetchone()[0] == 0


def test_database_failure_rolls_back_prior_correction_and_invalidations(evidence):
    with closing(connect(evidence)) as db:
        plan = sources.plan_repairs(db, reviews=[review(db), review(db, 3)])
        db.execute('''CREATE TRIGGER reject_second_source BEFORE INSERT ON bulletin_source_revisions
            WHEN NEW.occurrence_id=3 BEGIN SELECT RAISE(ABORT,'second source rejected'); END''')
        db.commit()
        with pytest.raises(sqlite3.IntegrityError, match='second source rejected'):
            sources.apply_repairs(db, plan)
        assert db.execute('SELECT count(*) FROM bulletin_source_revisions').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM bulletin_source_date_invalidations').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM bulletin_effective_dates WHERE normalized_date=?',
                          ('2027-10-15',)).fetchone()[0] == 3


def test_source_change_preserves_explicit_literal_date_without_reusing_ai_verdict(evidence):
    with closing(connect(evidence)) as db:
        with db:
            db.execute('INSERT INTO bulletin_dates VALUES(?,?,?,?,?,?,?,?)',
                (4, 1, 'event', '2026年10月15日', '2026-10-15', 'explicit', '2026年10月15日', 'null'))
        sources.apply_repairs(db, sources.plan_repairs(db, reviews=[review(db)]))
        assert {row[0] for row in db.execute('SELECT normalized_date FROM bulletin_effective_dates WHERE occurrence_id=1')} == {None, '2026-10-15'}
        assert 1 not in dates.date_snapshot(db)[2]
    corrected = next(row for row in store.get_item(1)['occurrences'] if row['occurrence_id'] == 1)
    assert corrected['date_review']['status'] == 'unreviewed'
    assert store.query_items({'date_from': '2026-10-15', 'date_to': '2026-10-15'})['total'] == 1


def test_stale_cached_date_reviews_are_rejected_and_fresh_bound_review_can_apply(evidence, tmp_path):
    with closing(connect(evidence)) as db:
        rows, originals, _ = dates.date_snapshot(db)
        old_row = next(row for row in rows if row['id'] == 1)
        value = {'item_id': dates._public_id('item-', old_row['identity_key']),
            'occurrence_id': dates._public_id('occ-', old_row['source_sha256'] + ':' + old_row['local_key']),
            'document_sha256': old_row['source_sha256'], 'local_key': old_row['local_key'],
            'status': 'unresolved', 'claims': [], 'reason': 'No verified date.',
            'rules_version': dates.RULES_VERSION, 'reviewer': 'luna:test',
            'input_sha256': dates.input_digest(old_row, originals[1]),
            'related_context_sha256': old_row['related_context_sha256'],
            'source_review': {'status': 'secondary_only', 'reason': 'No cited primary date.',
                              'sources': [], 'attempts': [], 'activity': None}}
        path = tmp_path / 'dates.json'
        path.write_text(json.dumps([value]))
        stale_plan = dates.plan_repairs(db, review_files=[path])
        sources.apply_repairs(db, sources.plan_repairs(db, reviews=[review(db)]))
        with pytest.raises(ValueError, match='snapshot changed'):
            dates.apply_repairs(db, stale_plan)
        unbound = dict(value)
        unbound.pop('input_sha256')
        unbound.pop('related_context_sha256')
        path.write_text(json.dumps([unbound]))
        with pytest.raises(ValueError, match='fresh input-bound'):
            dates.plan_repairs(db, review_files=[path])
        value.update(input_sha256=dates.input_digest(old_row, originals[1]),
                     related_context_sha256=old_row['related_context_sha256'])
        path.write_text(json.dumps([value]))
        with pytest.raises(ValueError, match='input changed'):
            dates.plan_repairs(db, review_files=[path])
        rows, originals, _ = dates.date_snapshot(db)
        current = next(row for row in rows if row['id'] == 1)
        value.update(input_sha256=dates.input_digest(current, originals[1]),
                     related_context_sha256=current['related_context_sha256'])
        path.write_text(json.dumps([value]))
        assert dates.apply_repairs(db, dates.plan_repairs(db, review_files=[path])) == 1
        assert 1 in dates.date_snapshot(db)[2]


def test_capture_and_operator_validation_are_conservative(evidence):
    with closing(connect(evidence)) as db:
        valid = review(db)
        for invalid in (dict(valid, actor='luna:fake'), dict(valid, reason=''),
                        dict(valid, corrected_url='http://127.0.0.1/private'),
                        dict(valid, capture=dict(valid['capture'], resolved_url='https://job.nju.edu.cn/')),
                        dict(valid, capture=dict(valid['capture'], fetched_at='2026-10-16'))):
            with pytest.raises(ValueError):
                sources.plan_repairs(db, reviews=[invalid])


def test_source_capture_uses_corrected_url_and_does_not_reuse_wrong_url_cache(evidence, monkeypatch):
    from nannanews import acquisition
    with closing(connect(evidence)) as db:
        sources.apply_repairs(db, sources.plan_repairs(db, reviews=[review(db)]))
        row = next(row for row in dates.date_snapshot(db)[0] if row['id'] == 1)
    fetched = []
    snapshot = capture()
    document = dict(snapshot, canonical_url=NEW, source_published_date=None, images=[])
    def fetch(url):
        fetched.append(url)
        return document
    monkeypatch.setattr(acquisition, 'fetch_document', fetch)
    cache = {OLD: {'error': {'code': 'wrong_old_article', 'reason': 'Must not reuse this capture.'}}}
    captured, attempts, media, mismatched, _ = dates._capture_sources(row, cache)
    assert fetched == [NEW]
    assert captured[0]['url'] == NEW and captured[0]['title'] == TITLE
    assert attempts == [] and media == [] and mismatched is False
    assert cache[NEW]['document']['canonical_url'] == NEW


def test_dry_run_private_audits_and_public_export_never_leak_captures(evidence, tmp_path, monkeypatch):
    from nannanews import acquisition
    monkeypatch.setattr(acquisition, 'fetch_document', lambda *args, **kwargs: pytest.fail('unexpected source fetch'))
    monkeypatch.setattr(acquisition, 'request_model_json', lambda *args, **kwargs: pytest.fail('unexpected model call'))
    with closing(connect(evidence)) as db:
        material = review(db)
        occurrence_id = material['occurrence_id']
    capture_file = tmp_path / 'capture.json'
    capture_file.write_text(json.dumps(capture()))
    before = evidence.database_path.read_bytes()
    output = tmp_path / 'prepared'
    prepared = sources.prepare_source(occurrence_id=occurrence_id, original_url=OLD,
        corrected_url=NEW, capture_file=capture_file, reason=material['reason'],
        actor=material['actor'], output_dir=output, settings=evidence)
    dry = sources.repair_sources(review_files=[prepared['artifact']], audit_dir=tmp_path / 'dry', settings=evidence)
    assert dry['applied'] is False and dry['new_revisions'] == 1
    assert evidence.database_path.read_bytes() == before
    for directory in (output, tmp_path / 'dry'):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in directory.iterdir())
    applied = sources.repair_sources(review_files=[prepared['artifact']], apply=True,
                                    audit_dir=tmp_path / 'applied', settings=evidence)
    assert applied['appended_revisions'] == 1
    before_export = evidence.database_path.read_bytes()
    public = tmp_path / 'public'
    static_export.export_static(public, settings=evidence)
    assert evidence.database_path.read_bytes() == before_export
    manifest = json.loads((public / 'manifest.json').read_text())
    catalog = json.loads((public / manifest['files']['catalog']).read_text())
    occurrence = next(row for row in catalog['occurrences'] if row['id'] == occurrence_id)
    assert NEW in occurrence['source_urls'] and OLD not in occurrence['source_urls']
    assert occurrence['source_corrections'][0]['title'] == TITLE
    item = next(row for row in catalog['items'] if row['id'] == occurrence['item_id'])
    detail = json.loads((public / item['detail_path']).read_text())[item['id']]
    exported = next(row for row in detail['occurrences'] if row['id'] == occurrence_id)
    assert OLD in exported['body_text']  # Original text remains evidence, not a primary citation.
    assert exported['date_review'] is None
    assert exported['dates'][0]['normalized_date'] is None
    assert all(PRIVATE.encode() not in path.read_bytes() for path in public.rglob('*') if path.is_file())
    with closing(connect(evidence)) as db:
        candidates = bulletin_activities.activity_candidates(db, item_ids=[
            dates._public_id('item-', 'hainan'), dates._public_id('item-', 'other')])
    hainan = next(item for item in candidates['candidates'][0]['items']
                  if item['item_id'] == dates._public_id('item-', 'hainan'))
    assert hainan['source_urls'] == [NEW]
