"""Historical request prevention is global, durable, and distinct from verdict reuse."""
from collections import Counter
from contextlib import closing
from copy import deepcopy
import base64
import hashlib
import json
import multiprocessing
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace

import pytest

from test_bulletin_date_repair import (
    SOURCE_TEXT, SOURCE_URL, _fetched_document, _model_response, activity, database, source_review,
    reference_response, source_data_from_payload,
)
from test_acquisition_media import image_bytes
from nannanews import acquisition
from nannanews import bulletin_date_repair as repair
from nannanews import bulletin_historical_attempts as historical_attempts
from nannanews.bulletin_historical_attempts import AttemptStore, ReviewStorage, ledger_path, logical_request, validate_schema


@pytest.fixture
def historical(database, tmp_path, monkeypatch):
    with database:
        database.execute("INSERT INTO bulletin_issues VALUES(2,'2026-10-14')")
        database.execute("INSERT INTO bulletin_documents VALUES(2,2,'source-digest-2')")
        database.execute("INSERT INTO bulletin_occurrences VALUES(2,1,2,'section-2','detail','讲座','活动日期见链接','{}')")
        database.execute("INSERT INTO bulletin_dates SELECT 2,2,kind,raw_text,normalized_date,resolution,evidence,locator_json FROM bulletin_dates WHERE id=1")
        database.execute('INSERT INTO bulletin_sources VALUES(2,?)', (SOURCE_URL,))
    path = tmp_path / 'bulletins.sqlite3'
    with closing(sqlite3.connect(path)) as disk:
        database.backup(disk)
    rows, _, _ = repair.date_snapshot(database)
    ids = [repair._public_id('occ-', row['source_sha256'] + ':' + row['local_key']) for row in rows]
    monkeypatch.setattr(acquisition, '_model_config', lambda: SimpleNamespace(model_name='test-model', model_api_style='chat_completions'))
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: _fetched_document())
    return SimpleNamespace(settings=SimpleNamespace(database_path=path), ids=ids, rows=rows,
        output=tmp_path / 'reviews', ledger=AttemptStore(path).path)


def envelope(output):
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}]}


def reference_envelope(payload, output=None):
    return envelope(reference_response(_model_response() if output is None else output,
        source_data_from_payload(payload)))


def run_review(historical, **kwargs):
    return repair.review_dates(output_dir=kwargs.pop('output_dir', historical.output),
        settings=historical.settings, occurrence_ids=kwargs.pop('occurrence_ids', historical.ids), **kwargs)


def records(result):
    return [json.loads(line) for line in Path(result['artifact']).read_text().splitlines()]


def attempts(historical):
    if not historical.ledger.exists():
        return []
    with closing(sqlite3.connect(historical.ledger)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute('SELECT * FROM historical_attempts')]


@pytest.mark.parametrize('output', [
    {'rejected_private_marker': 'bad schema'},
])
def test_failed_equivalents_each_get_bound_receipt_once(historical, monkeypatch, output):
    posts = []
    def post(payload, *, audit=None):
        posts.append(payload)
        # The reservation is committed and no write lock spans the external call.
        with closing(sqlite3.connect(historical.ledger, timeout=0.1)) as db:
            assert db.execute('SELECT state FROM historical_attempts').fetchone()[0] == 'reserved'
            db.execute('BEGIN IMMEDIATE')
            db.rollback()
        return reference_envelope(payload, output) if 'claims' in output else envelope(output)
    monkeypatch.setattr(acquisition, '_model_post', post)
    result = run_review(historical)
    assert result['model_request_attempts'] == len(posts) == 1
    assert result['blocked_requests'] == 1 and result['reused_reviews'] == 0
    assert result['completed'] == result['new_reviews'] == result['pending_reviews'] == 1
    saved = records(result)
    progress = [json.loads(line) for path in result['progress_artifacts']
                for line in Path(path).read_text().splitlines()]
    assert {value['occurrence_id'] for value in saved + progress} == set(historical.ids)
    assert len(saved) == len(progress) == 1
    assert progress[0]['progress'] == 'equivalent_request_not_reusable'
    assert all(value['rules_version'] == repair.RULES_VERSION and value['input_sha256']
               and value['related_context_sha256'] for value in saved + progress)
    with closing(sqlite3.connect(historical.ledger)) as db:
        assert {row[0] for row in db.execute('SELECT occurrence_id FROM historical_outcomes')} == {saved[0]['occurrence_id']}
    assert all('raw_output_json' not in value for value in saved)
    ledger = attempts(historical)
    expected = reference_response(output, source_data_from_payload(posts[0])) if 'claims' in output else output
    assert len(ledger) == 1 and json.loads(ledger[0]['raw_output_json']) == expected
    assert stat.S_IMODE(historical.ledger.stat().st_mode) == 0o600
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        assert db.execute('SELECT count(*) FROM bulletin_date_revisions').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM bulletin_dates').fetchone()[0] == 2
    fresh = run_review(historical, output_dir=historical.output.with_name('fresh'))
    assert fresh['model_request_attempts'] == 0 and fresh['blocked_requests'] == 2
    assert fresh['new_reviews'] == fresh['completed'] == 0 and fresh['pending_reviews'] == 2
    assert records(fresh) == [] and len(posts) == 1
    fresh_progress = [json.loads(line) for path in fresh['progress_artifacts']
                      for line in Path(path).read_text().splitlines()]
    assert {value['occurrence_id'] for value in fresh_progress} == set(historical.ids)
    assert all(value['progress'] == 'equivalent_request_not_reusable' for value in fresh_progress)


def test_success_reuse_still_requires_strict_primary_gate(historical, monkeypatch):
    posts = []
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: posts.append(payload) or reference_envelope(payload))
    result = run_review(historical)
    assert result['model_request_attempts'] == len(posts) == 1 and result['reused_reviews'] == 1
    assert result['blocked_requests'] == 0
    assert all(value['status'] == 'resolved' for value in records(result))
    resumed = run_review(historical)
    assert resumed['new_reviews'] == resumed['model_request_attempts'] == 0
    fresh = run_review(historical, output_dir=historical.output.with_name('fresh'))
    assert fresh['model_request_attempts'] == 0 and fresh['reused_reviews'] == 2
    assert len(posts) == 1


def test_interrupted_reservation_is_never_resent_from_fresh_output(historical, monkeypatch):
    def interrupt(payload, *, audit=None):
        raise KeyboardInterrupt
    monkeypatch.setattr(acquisition, '_model_post', interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_review(historical)
    assert [row['state'] for row in attempts(historical)] == ['reserved']
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: pytest.fail('interrupted request resent'))
    result = run_review(historical, output_dir=historical.output.with_name('resume'))
    assert result['blocked_requests'] == 2 and result['model_request_attempts'] == 0
    assert result['pending_reviews'] == 2 and result['completed'] == result['new_reviews'] == 0
    progress = [json.loads(line) for line in Path(result['progress_artifact']).read_text().splitlines()]
    assert {value['occurrence_id'] for value in progress} == set(historical.ids)
    assert all(value['status'] == 'unresolved' and value['request_sha256']
               and value['input_sha256'] and value['related_context_sha256'] for value in progress)
    again = run_review(historical, output_dir=historical.output.with_name('resume'))
    assert again['pending_reviews'] == 2 and again['model_request_attempts'] == 0


def test_bad_json_raw_provider_response_is_retained_privately(historical, monkeypatch):
    raw = {'choices': [{'finish_reason': 'stop', 'message': {'content': 'private invalid JSON'}}]}
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: raw)
    result = run_review(historical)
    assert result['model_request_attempts'] == 1
    assert json.loads(attempts(historical)[0]['raw_output_json']) == raw
    assert 'private invalid JSON' not in Path(result['artifact']).read_text()


@pytest.mark.parametrize('preparation', ['size', 'unsafe_image', 'invalid_capture'])
def test_preparation_failures_never_reserve_or_send(historical, monkeypatch, preparation):
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: pytest.fail('unsafe request sent'))
    cache = None
    if preparation == 'size':
        monkeypatch.setattr(acquisition, 'MAX_MODEL_REQUEST_BYTES', 1)
    elif preparation == 'invalid_capture':
        document = _fetched_document()
        document['raw_response_digest'] = '0' * 64
        monkeypatch.setattr(acquisition, 'fetch_document', lambda url: document)
    else:
        image = {'url': 'https://primary.example.org/image.png', 'mime': 'image/png',
            'data': 'bm90IGFuIGltYWdl', 'digest': hashlib.sha256(b'not an image').hexdigest()}
        cache = {SOURCE_URL: {'document': _fetched_document(), 'media': [image], 'media_error': ''}}
    result = run_review(historical, source_cache=cache)
    assert result['model_request_attempts'] == 0 and result['preparation_failures'] == 2
    assert attempts(historical) == []
    assert result['new_reviews'] == result['completed'] == 0 and records(result) == []
    assert result['pending_reviews'] == 2
    progress = [json.loads(line) for path in result['progress_artifacts']
                for line in Path(path).read_text().splitlines()]
    assert len(progress) == 2
    assert all(value['progress'] == 'review_preparation_failed'
               and value['source_review']['activity'] is None for value in progress)
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        assert repair.plan_repairs(db, review_files=(result['artifact'],))['entries'] == []
    assert AttemptStore(historical.settings.database_path).bound_outcomes() == set()


def test_legacy_interrupted_output_guard_remains_conservative(historical, monkeypatch):
    historical.output.mkdir()
    (historical.output / '.attempted').write_text('\n'.join(json.dumps(value) for value in historical.ids) + '\n')
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: pytest.fail('legacy interrupted review retried'))
    result = run_review(historical)
    assert result['model_request_attempts'] == 0 and result['blocked_requests'] == 2
    assert records(result) == [] and attempts(historical) == []
    assert result['completed'] == result['new_reviews'] == 0 and result['pending_reviews'] == 2
    progress = [json.loads(line) for path in result['progress_artifacts']
                for line in Path(path).read_text().splitlines()]
    assert {value['occurrence_id'] for value in progress} == set(historical.ids)
    assert all(value['progress'] == 'legacy_attempt_interrupted' for value in progress)


def test_old_rule_receipts_are_preserved_not_counted_as_current(historical, monkeypatch):
    historical.output.mkdir()
    row = historical.rows[0]
    old = {'item_id': repair._public_id('item-', row['identity_key']), 'occurrence_id': historical.ids[0],
        'document_sha256': row['source_sha256'], 'local_key': row['local_key'], 'status': 'unresolved',
        'claims': [], 'reason': 'Historical receipt.', 'rules_version': 'old-rules', 'reviewer': 'luna:test-model',
        'source_review': {'status': 'secondary_only', 'reason': 'Old receipt.', 'sources': [], 'attempts': []}}
    original_bytes = (json.dumps(old) + '\n').encode()
    (historical.output / 'reviews.jsonl').write_bytes(original_bytes)
    (historical.output / '.attempted').write_text(json.dumps(historical.ids[0]) + '\n')
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: reference_envelope(payload))
    result = run_review(historical)
    assert result['new_reviews'] == 2 and result['model_request_attempts'] == 1
    archived = list(historical.output.glob('reviews.previous-*.jsonl'))
    assert len(archived) == 1 and archived[0].read_bytes() == original_bytes
    assert all(value['rules_version'] == repair.RULES_VERSION for value in records(result))


def test_changed_capture_requires_fresh_bound_review(historical, monkeypatch):
    posts = []
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: posts.append(payload) or envelope({'invalid': True}))
    run_review(historical)
    changed = _fetched_document(text=SOURCE_TEXT + ' Updated activity details.')
    changed['fetched_at'] = '2026-10-15T00:00:00+00:00'
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: changed)
    fresh = run_review(historical, output_dir=historical.output.with_name('changed'))
    assert fresh['model_request_attempts'] == 1 and len(posts) == len(attempts(historical)) == 2


def test_logical_key_ignores_receipt_identity_and_time_but_binds_semantics():
    occurrence = {'id': 1, 'local_key': 'first', 'source_sha256': 'old-document',
        'related_context_sha256': 'incidental-ids', 'related_occurrences': [
            {'kind': 'detail', 'title': 'Activity', 'body_text': '2025-11-27',
             'issue_dates': ['2026-10-13'], 'source_urls': [SOURCE_URL], 'source_corrections': []}]}
    claims = [{'kind': 'event', 'raw_text': '2025-11-27', 'normalized_date': None,
        'resolution': 'ambiguous', 'evidence': '2025-11-27', 'locator': {'row': 1}}]
    source_data = {'linked_primary_sources': [{'url': SOURCE_URL, 'resolved_url': SOURCE_URL,
        'text': SOURCE_TEXT, 'fetched_at': 'one', 'images': [{'sha256': 'image-one'}]}],
        'source_acquisition_attempts': []}
    def key(row=occurrence, originals=claims, data=source_data, rules=repair.RULES_VERSION, model='test-model'):
        return logical_request(row, originals, data, rules_version=rules, model_name=model)[0]
    original = key()
    other = deepcopy(occurrence)
    other.update(id=99, local_key='other', source_sha256='other-document', related_context_sha256='other-ids')
    other_data = deepcopy(source_data)
    other_data['linked_primary_sources'][0]['fetched_at'] = 'another time'
    other_claims = deepcopy(claims)
    other_claims[0]['locator'] = {'row': 99}
    assert key(other, other_claims, other_data) == original
    for change in ('text', 'image', 'correction', 'context', 'date', 'rules', 'model'):
        row, originals, data = deepcopy(occurrence), deepcopy(claims), deepcopy(source_data)
        rules, model = repair.RULES_VERSION, 'test-model'
        if change == 'text':
            data['linked_primary_sources'][0]['text'] += ' Changed.'
        elif change == 'image':
            data['linked_primary_sources'][0]['images'][0]['sha256'] = 'image-two'
        elif change == 'correction':
            row['related_occurrences'][0]['source_corrections'] = [{'original_url': SOURCE_URL, 'corrected_url': SOURCE_URL + '-corrected'}]
        elif change == 'context':
            row['related_occurrences'][0]['body_text'] += ' Changed.'
        elif change == 'date':
            originals[0]['raw_text'] = '2025-11-28'
        elif change == 'rules':
            rules = 'luna-content-evidence-v3'
        else:
            model = 'next-model'
        assert key(row, originals, data, rules, model) != original


def _concurrent_review_worker(settings, occurrence_ids, output, successful, entered, release, results):
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(acquisition, '_model_config', lambda: SimpleNamespace(
            model_name='test-model', model_api_style='chat_completions'))
        patcher.setattr(acquisition, 'fetch_document', lambda url: _fetched_document())

        def post(payload, *, audit=None):
            entered.set()
            if not release.wait(20):
                raise RuntimeError('Test release was not received.')
            return reference_envelope(payload) if successful else envelope({'invalid': 'private rejected output'})

        patcher.setattr(acquisition, '_model_post', post)
        try:
            result = repair.review_dates(output_dir=output, settings=settings, occurrence_ids=occurrence_ids)
            results.put(result)
        except BaseException as error:
            results.put({'error': repr(error)})


@pytest.mark.parametrize('successful', [False, True])
def test_concurrent_invocations_share_one_committed_reservation(historical, monkeypatch, successful):
    context = multiprocessing.get_context('forkserver')
    entered, release = context.Event(), context.Event()
    results = context.Queue()
    first = context.Process(target=_concurrent_review_worker, args=(
        historical.settings, historical.ids, historical.output, successful, entered, release, results))
    second = context.Process(target=_concurrent_review_worker, args=(
        historical.settings, historical.ids, historical.output.with_name('concurrent'),
        successful, entered, release, results))
    first.start()
    try:
        assert entered.wait(10)
        second.start()
        blocked = results.get(timeout=10)
        assert blocked.get('blocked_requests') == 2 and blocked['model_request_attempts'] == 0
        assert blocked['pending_reviews'] == 2 and blocked['completed'] == 0
        progress = [json.loads(line) for line in Path(blocked['progress_artifact']).read_text().splitlines()]
        assert {value['occurrence_id'] for value in progress} == set(historical.ids)
        release.set()
        sent = results.get(timeout=10)
        assert sent.get('model_request_attempts') == 1
        assert sent['reused_reviews'] == (1 if successful else 0)
        assert sent['blocked_requests'] == (0 if successful else 1)
        assert sent['new_reviews'] == (2 if successful else 1)
        assert sent['pending_reviews'] == (0 if successful else 1)
    finally:
        release.set()
        for process in (first, second):
            if process.pid is not None:
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
        exit_codes = (first.exitcode, second.exitcode)
        for process in (first, second):
            process.close()
        results.close()
        results.join_thread()
    assert exit_codes == (0, 0)
    assert len(attempts(historical)) == 1
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: pytest.fail('in-flight resume resent a request'))
    resumed = run_review(historical, output_dir=historical.output.with_name('concurrent'))
    assert resumed['model_request_attempts'] == 0
    assert resumed['pending_reviews'] == (0 if successful else 2)
    assert resumed['new_reviews'] == resumed['completed'] == (2 if successful else 0)
    assert resumed['reused_reviews'] == (2 if successful else 0)
    assert resumed['blocked_requests'] == (0 if successful else 2)


def test_pending_selection_requires_current_bound_context_not_only_head(historical, monkeypatch):
    posts = []
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: posts.append(payload) or reference_envelope(payload))
    generated = run_review(historical)
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        plan = repair.plan_repairs(db, review_files=(generated['artifact'],))
        assert repair.apply_repairs(db, plan) == 2
    current = run_review(historical, output_dir=historical.output.with_name('current'), occurrence_ids=())
    assert current['selected'] == current['model_request_attempts'] == 0
    with closing(sqlite3.connect(historical.settings.database_path)) as db, db:
        db.execute("UPDATE bulletin_occurrences SET body_text=body_text || ' 新的同活动上下文。' WHERE id=2")
    changed = run_review(historical, output_dir=historical.output.with_name('new-context'), occurrence_ids=())
    assert changed['selected'] == changed['new_reviews'] == 2
    assert changed['model_request_attempts'] == 1 and len(posts) == 2


def test_apply_rejects_stale_input_and_heads_after_guarded_review(historical, monkeypatch):
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: reference_envelope(payload))
    generated = run_review(historical)
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        plan = repair.plan_repairs(db, review_files=(generated['artifact'],))
        rows, originals, _ = repair.date_snapshot(db)
        with db:
            repair.record_revision(db, rows[0], originals[rows[0]['id']], [],
                rules_version=repair.RULES_VERSION, origin='luna_review', reviewer='luna:other',
                status='unresolved', reason='A newer head arrived.',
                source_review=source_review(status='secondary_only', sources=[]))
        with pytest.raises(ValueError, match='snapshot changed'):
            repair.apply_repairs(db, plan)
        with db:
            db.execute("UPDATE bulletin_occurrences SET body_text='Changed original input' WHERE id=1")
        with pytest.raises(ValueError, match='input changed'):
            repair.plan_repairs(db, review_files=(generated['artifact'],))


def test_canonical_ledger_path_supports_restore_target_before_db_exists(tmp_path):
    database = tmp_path / 'missing-evidence.sqlite3'
    assert ledger_path(database) == tmp_path / '.missing-evidence.sqlite3.historical-attempts.sqlite3'
    assert not database.exists() and not ledger_path(database).exists()
    real = tmp_path / 'real'
    real.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(real, target_is_directory=True)
    assert ledger_path(alias / 'evidence.sqlite3') == ledger_path(real / 'evidence.sqlite3')


@pytest.mark.parametrize('invalid_schema', ['extra_table', 'missing_column', 'trigger'])
def test_restore_schema_validator_rejects_unexpected_sidecars(historical, monkeypatch, invalid_schema):
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: envelope({'invalid': True}))
    run_review(historical)
    with closing(sqlite3.connect(historical.ledger)) as db:
        validate_schema(db)
        if invalid_schema == 'extra_table':
            db.execute('CREATE TABLE unrelated(id INTEGER)')
        elif invalid_schema == 'missing_column':
            db.execute('ALTER TABLE historical_attempts DROP COLUMN raw_output_json')
        else:
            db.execute('''CREATE TRIGGER unexpected AFTER INSERT ON historical_attempts
                BEGIN SELECT 1; END''')
        with pytest.raises(ValueError, match='Historical attempt ledger'):
            validate_schema(db)


def test_oversized_receipt_is_unpaid_nonterminal_and_retains_every_capture(historical, monkeypatch):
    monkeypatch.setattr(historical_attempts, 'MAX_REVIEW_ARTIFACT_BYTES', 2048)
    document = _fetched_document(text=SOURCE_TEXT + ' Captured detail.' * 1000)
    raw_image = image_bytes()
    image = {'url': 'https://primary.example.org/poster.png', 'mime': 'image/png',
        'data': base64.b64encode(raw_image).decode('ascii'), 'digest': hashlib.sha256(raw_image).hexdigest()}
    cache = {SOURCE_URL: {'document': document, 'media': [image], 'media_error': ''}}
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: pytest.fail('unreadable receipt was charged'))
    blocked = run_review(historical, source_cache=cache)
    assert blocked['model_request_attempts'] == 0 and attempts(historical) == []
    assert blocked['storage_blocked'] == blocked['preparation_failures'] == blocked['pending_reviews'] == 2
    assert blocked['new_reviews'] == blocked['completed'] == 0 and records(blocked) == []
    progress = [json.loads(line) for path in blocked['progress_artifacts']
                for line in Path(path).read_text().splitlines()]
    assert {value['occurrence_id'] for value in progress} == set(historical.ids)
    for value in progress:
        assert value['progress'] == 'review_storage_blocked' and value['status'] == 'unresolved'
        capture_path = Path(value['capture_artifact'])
        assert stat.S_IMODE(capture_path.stat().st_mode) == 0o600
        capture = json.loads(capture_path.read_text())
        source = capture['source_review']['sources'][0]
        assert source['text'] == document['text']
        assert source['raw_response_base64'] == document['raw_response_base64']
        assert base64.b64decode(source['images'][0]['data']) == raw_image
        assert capture['input_sha256'] == value['input_sha256']
        assert capture['related_context_sha256'] == value['related_context_sha256']
    # No completion marker or paid reservation prevents an explicit resume after
    # the storage prerequisite is changed. Evidence is never silently dropped.
    monkeypatch.setattr(historical_attempts, 'MAX_REVIEW_ARTIFACT_BYTES', 128 * 1024 * 1024)
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: reference_envelope(payload))
    resumed = run_review(historical, source_cache=cache)
    assert resumed['model_request_attempts'] == 1 and resumed['reused_reviews'] == 1
    assert resumed['new_reviews'] == resumed['completed'] == 2
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        assert len(repair.plan_repairs(db, review_files=(resumed['artifact'],))['entries']) == 2


def test_cumulative_capacity_blocks_next_distinct_request_before_reservation(historical, monkeypatch):
    # Full coherent HTTP evidence is retained as well as its extracted text.
    # This controlled quota admits one source plus response reserve, not two.
    monkeypatch.setattr(historical_attempts, 'MAX_REVIEW_ARTIFACT_BYTES', 180000)
    monkeypatch.setattr(acquisition, 'MAX_MODEL_BODY', 2048)
    with closing(sqlite3.connect(historical.settings.database_path)) as db, db:
        db.execute("UPDATE bulletin_dates SET raw_text='不同的待核实日期' WHERE occurrence_id=1")
    document = _fetched_document(text=SOURCE_TEXT + 'a' * 30000)
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: document)
    posts = []
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: posts.append(payload) or reference_envelope(payload))
    result = run_review(historical)
    assert result['model_request_attempts'] == len(posts) == len(attempts(historical)) == 1
    assert result['completed'] == result['new_reviews'] == result['pending_reviews'] == result['storage_blocked'] == 1
    assert Path(result['artifact']).stat().st_size <= historical_attempts.MAX_REVIEW_ARTIFACT_BYTES
    progress = [json.loads(line) for path in result['progress_artifacts']
                for line in Path(path).read_text().splitlines()]
    assert len(progress) == 1 and progress[0]['progress'] == 'review_storage_blocked'
    assert Path(progress[0]['capture_artifact']).is_file()
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        assert len(repair.plan_repairs(db, review_files=(result['artifact'],))['entries']) == 1
    # A fresh output for only the blocked identity is not a retry: its original
    # storage preparation failed before any reservation or model request existed.
    resumed = run_review(historical, output_dir=historical.output.with_name('remaining'),
        occurrence_ids=[progress[0]['occurrence_id']])
    assert resumed['model_request_attempts'] == 1 and len(posts) == len(attempts(historical)) == 2
    assert resumed['completed'] == 1


def test_contextual_review_reuses_without_upgrading_or_replacing_valid_head(historical, monkeypatch):
    text = 'Published on November 20, 2025. Event held on November 27.'
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: _fetched_document(text=text))
    contextual = {'status': 'resolved', 'reason': '同一活动原文的2025年发布日期提供年份锚点。',
        'claims': [{'kind': 'event', 'raw_text': 'November 27', 'normalized_date': '2025-11-27',
            'resolution': 'contextual', 'evidence': '同一活动原文的2025年发布日期提供年份锚点，11月27日为活动日。', 'locator': None}],
        'source_status': 'source_verified', 'source_reason': '已核实同一活动及原文年份锚点。',
        'citations': [{'url': SOURCE_URL, 'quotes': [{'text': text, 'image_index': None}]}],
        'activity': activity(quote=text)}
    posts = []
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: posts.append(payload) or reference_envelope(payload, contextual))
    selected = [historical.ids[0]]
    original = run_review(historical, occurrence_ids=selected)
    assert original['new_reviews'] == original['model_request_attempts'] == 1
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        assert repair.apply_repairs(db, repair.plan_repairs(db, review_files=(original['artifact'],))) == 1
        before = [tuple(row) for row in db.execute('SELECT * FROM bulletin_date_revisions ORDER BY id')]
    frozen = attempts(historical)
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: pytest.fail('contextual duplicate resent'))
    duplicate = run_review(historical, output_dir=historical.output.with_name('same-id-fresh'), occurrence_ids=selected)
    assert duplicate['model_request_attempts'] == duplicate['blocked_requests'] == duplicate['pending_reviews'] == 0
    assert duplicate['reused_reviews'] == duplicate['completed'] == duplicate['new_reviews'] == 1
    assert records(duplicate) == records(original)
    assert len(posts) == 1 and attempts(historical) == frozen
    assert records(duplicate)[0]['claims'][0]['resolution'] == 'contextual'
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        assert repair.apply_repairs(db, repair.plan_repairs(db, review_files=(duplicate['artifact'],))) == 0
        assert [tuple(row) for row in db.execute('SELECT * FROM bulletin_date_revisions ORDER BY id')] == before



def test_unpaid_preparation_failure_cannot_replace_confirmed_content_head(historical, monkeypatch):
    posts = []
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: posts.append(payload) or reference_envelope(payload))
    generated = run_review(historical, occurrence_ids=[historical.ids[0]])
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        assert repair.apply_repairs(db, repair.plan_repairs(db, review_files=[generated['artifact']])) == 1
        before = db.execute('SELECT revision_sha256,source_review_json FROM bulletin_date_revisions').fetchone()
        assert json.loads(before['source_review_json'])['activity'] is not None
    prior_attempts = attempts(historical)
    monkeypatch.setattr(acquisition, 'MAX_MODEL_REQUEST_BYTES', 1)
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: _fetched_document(text=SOURCE_TEXT + ' Additional captured detail.'))
    failed = run_review(historical, occurrence_ids=[historical.ids[0]],
        output_dir=historical.output.with_name('preparation-failed'))
    assert failed['model_request_attempts'] == failed['new_reviews'] == failed['completed'] == 0
    assert failed['pending_reviews'] == failed['preparation_failures'] == 1
    assert records(failed) == [] and len(posts) == 1 and attempts(historical) == prior_attempts
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        empty = repair.plan_repairs(db, review_files=[failed['artifact']])
        assert empty['entries'] == [] and repair.apply_repairs(db, empty) == 0
        after = db.execute('SELECT revision_sha256,source_review_json FROM bulletin_date_revisions').fetchone()
        assert tuple(after) == tuple(before)


def review_one(historical, *, row=None, output_dir=None, source_cache=None, counters=None, **kwargs):
    output = output_dir or historical.output
    output.mkdir(mode=0o700, exist_ok=True)
    storage = ReviewStorage(output / 'reviews.jsonl', output, response_bytes=acquisition.MAX_MODEL_BODY)
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        rows, originals, _ = repair.date_snapshot(db)
    row = row or rows[0]
    return repair.review_occurrence(row, originals[row['id']], model_name='test-model',
        source_cache={} if source_cache is None else source_cache,
        attempt_store=AttemptStore(historical.settings.database_path), storage=storage,
        counters=Counter() if counters is None else counters, **kwargs)


@pytest.mark.parametrize('outcome', ['verified', 'failed', 'reserved'])
def test_new_stage_timings_never_authorize_equivalent_paid_requests(historical, monkeypatch, outcome):
    clock, posts = [0.0], []
    fetch_durations = iter((2.0, 200.0))
    monkeypatch.setattr(repair, 'time', SimpleNamespace(monotonic=lambda: clock[0]))

    def fetch(url):
        clock[0] += next(fetch_durations)
        return _fetched_document()

    def post(payload, *, audit=None):
        posts.append(payload)
        clock[0] += 11
        if outcome == 'reserved':
            raise KeyboardInterrupt
        return reference_envelope(payload) if outcome == 'verified' else envelope({'invalid': True})

    append = repair._append_private_line
    def store(path, value):
        clock[0] += 3
        return append(path, value)

    monkeypatch.setattr(acquisition, 'fetch_document', fetch)
    monkeypatch.setattr(acquisition, '_model_post', post)
    monkeypatch.setattr(repair, '_append_private_line', store)
    if outcome == 'reserved':
        with pytest.raises(KeyboardInterrupt):
            review_one(historical)
    else:
        first = review_one(historical)
        assert first['new_request'] is True and first['terminal'] is True
        assert first['_timing']['source_fetch_seconds'] == 2
        assert first['_timing']['model_request_seconds'] == 11
        assert first['_timing']['review_storage_seconds'] == 3
        assert first['_timing']['review_seconds'] == 16
        assert '_timing' not in first['record']
        saved = json.loads((historical.output / 'reviews.jsonl').read_text())
        assert saved == first['record'] and '_timing' not in saved
    frozen = attempts(historical)
    assert len(frozen) == len(posts) == 1
    assert '_timing' not in json.loads(frozen[0]['request_json'])
    if frozen[0]['decision_json']:
        assert '_timing' not in json.loads(frozen[0]['decision_json'])
    repeated = review_one(historical, output_dir=historical.output.with_name('different-timing'))
    assert len(posts) == 1 and repeated['new_request'] is False
    assert repeated['_timing']['source_fetch_seconds'] == 200
    assert 'model_request_seconds' not in repeated['_timing']
    assert attempts(historical) == frozen
    assert repeated['terminal'] is (outcome == 'verified')
    if outcome == 'verified':
        assert repeated['record'] == first['record']
    else:
        assert repeated['progress'] == ('equivalent_request_reserved' if outcome == 'reserved'
                                         else 'equivalent_request_not_reusable')
        progress = json.loads((historical.output.with_name('different-timing') / 'review-progress.jsonl').read_text())
        assert progress['_timing']['source_fetch_seconds'] == 200
        capture = json.loads(Path(progress['capture_artifact']).read_text())
        assert '_timing' not in capture and '_timing' not in capture['source_review']


def test_shared_defer_preserves_captured_article_and_poster_without_paid_or_terminal_effects(historical, monkeypatch):
    raw_image = image_bytes()
    document = _fetched_document()
    image = {'url': 'https://primary.example.org/poster.png', 'mime': 'image/png',
        'data': base64.b64encode(raw_image).decode('ascii'), 'digest': hashlib.sha256(raw_image).hexdigest()}
    cache = {SOURCE_URL: {'document': document, 'media': [image], 'media_error': ''}}
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: pytest.fail('deferred request was sent'))
    counters, observed = Counter(), []
    def gate(request_key, captured_review):
        assert AttemptStore(historical.settings.database_path).lookup(request_key) is None
        observed.append(captured_review)
        raise repair.ReviewDeferred('daily_priority', 'The daily collection slot has priority.')
    result = review_one(historical, source_cache=cache, counters=counters, request_gate=gate)
    assert result['terminal'] is False and result['new_request'] is False
    assert result['progress'] == 'review_deferred' and result['defer_code'] == 'daily_priority'
    assert result['record']['reason'] == 'The daily collection slot has priority.'
    assert result['record']['claims'] == [] and result['record']['source_review']['activity'] is None
    assert len(observed) == 1 and observed[0]['sources'][0]['text'] == document['text']
    assert counters['pending_reviews'] == counters['deferred_reviews'] == 1
    assert counters['preparation_failures'] == counters['model_request_attempts'] == 0
    assert attempts(historical) == []
    assert AttemptStore(historical.settings.database_path).bound_outcomes() == set()
    assert not (historical.output / 'reviews.jsonl').exists()
    progress = json.loads((historical.output / 'review-progress.jsonl').read_text())
    assert progress['defer_code'] == result['defer_code'] and progress['reason'] == result['record']['reason']
    assert progress['source_review']['activity'] is None
    capture = json.loads(Path(progress['capture_artifact']).read_text())
    assert capture == result['record']
    source = capture['source_review']['sources'][0]
    assert source['raw_response_base64'] == document['raw_response_base64']
    assert source['text'] == document['text'] and base64.b64decode(source['images'][0]['data']) == raw_image
    assert stat.S_IMODE(Path(progress['capture_artifact']).stat().st_mode) == 0o600


def test_deferred_resume_and_manual_consumer_share_paid_once_receipts(historical, monkeypatch):
    posts, admitted, counters, cache = [], [], Counter(), {}
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: posts.append(payload) or reference_envelope(payload))
    def pause(request_key, captured_review):
        raise repair.ReviewDeferred('stop_requested', 'The worker is stopping before new requests.')
    deferred = review_one(historical, source_cache=cache, counters=counters, request_gate=pause)
    assert deferred['terminal'] is False and attempts(historical) == []
    first = review_one(historical, source_cache=cache, counters=counters,
        request_gate=lambda request_key, captured_review: admitted.append(request_key))
    assert first['terminal'] is True and first['progress'] is None and first['new_request'] is True
    other = next(row for row in historical.rows if row['id'] != historical.rows[0]['id'])
    second = review_one(historical, row=other, source_cache=cache, counters=counters,
        request_gate=lambda *args: pytest.fail('free primary reuse passed through paid admission'))
    assert second['terminal'] is True and second['new_request'] is False
    assert len(posts) == len(admitted) == len(attempts(historical)) == 1
    assert counters['model_request_attempts'] == counters['reused_reviews'] == 1
    assert len(AttemptStore(historical.settings.database_path).bound_outcomes()) == 2
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        assert len(repair.plan_repairs(db, review_files=[historical.output / 'reviews.jsonl'])['entries']) == 2
    resumed = run_review(historical, source_cache=cache)
    assert resumed['completed'] == 2 and resumed['new_reviews'] == resumed['model_request_attempts'] == 0
    assert len(posts) == 1


def test_no_readable_primary_gate_retains_real_failure_without_ai_judgment(historical, monkeypatch):
    def unavailable(url):
        raise acquisition.AcquisitionError('The linked public article is unavailable.', 'source_unavailable')
    monkeypatch.setattr(acquisition, 'fetch_document', unavailable)
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: pytest.fail('missing primary was sent'))
    def primary_only(request_key, captured_review):
        assert captured_review['sources'] == []
        assert captured_review['attempts'][0]['code'] == 'source_unavailable'
        raise repair.ReviewDeferred('no_readable_primary', 'No readable primary article was captured.')
    result = review_one(historical, request_gate=primary_only)
    assert result['terminal'] is False and result['defer_code'] == 'no_readable_primary'
    assert result['record']['source_review']['attempts'][0]['url'] == SOURCE_URL
    assert result['record']['source_review']['activity'] is None
    assert attempts(historical) == [] and AttemptStore(historical.settings.database_path).bound_outcomes() == set()
    assert not (historical.output / 'reviews.jsonl').exists()


@pytest.mark.parametrize('blocker', ['request_size', 'raster', 'capture', 'storage'])
def test_operational_admission_cannot_bypass_existing_preflight(historical, monkeypatch, blocker):
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: pytest.fail('invalid request was sent'))
    cache = None
    if blocker == 'request_size':
        monkeypatch.setattr(acquisition, 'MAX_MODEL_REQUEST_BYTES', 1)
    elif blocker == 'raster':
        raw = b'not a decodable raster'
        cache = {SOURCE_URL: {'document': _fetched_document(), 'media': [{
            'url': 'https://primary.example.org/bad.png', 'mime': 'image/png',
            'data': base64.b64encode(raw).decode('ascii'), 'digest': hashlib.sha256(raw).hexdigest()}]}}
    elif blocker == 'capture':
        document = _fetched_document()
        document['raw_response_digest'] = '0' * 64
        monkeypatch.setattr(acquisition, 'fetch_document', lambda url: document)
    else:
        monkeypatch.setattr(historical_attempts, 'MAX_REVIEW_ARTIFACT_BYTES', 1)
    result = review_one(historical, source_cache=cache,
        request_gate=lambda *args: pytest.fail('invalid evidence reached operational admission'))
    assert result['terminal'] is False and result['new_request'] is False
    assert result['progress'] == ('review_storage_blocked' if blocker == 'storage' else 'review_preparation_failed')
    assert 'defer_code' not in result
    assert attempts(historical) == [] and AttemptStore(historical.settings.database_path).bound_outcomes() == set()


def test_request_gate_without_canonical_store_fails_closed_before_transport(historical, monkeypatch):
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        rows, originals, _ = repair.date_snapshot(db)
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: pytest.fail('unguarded request was sent'))
    decision = repair.model_decision(rows[0], originals[rows[0]['id']], source_cache={},
        request_gate=lambda *args: pytest.fail('admission ran without a canonical attempt store'))
    assert decision['_review_deferred'] == 'attempt_store_required'
    assert decision['claims'] == [] and decision['source_review']['activity'] is None
    assert decision['source_review']['sources'][0]['raw_response_digest'] == _fetched_document()['raw_response_digest']
    assert attempts(historical) == []


def test_deferred_progress_cannot_replace_confirmed_head(historical, monkeypatch):
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: reference_envelope(payload))
    generated = run_review(historical, occurrence_ids=[historical.ids[0]])
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        assert repair.apply_repairs(db, repair.plan_repairs(db, review_files=[generated['artifact']])) == 1
        before = tuple(db.execute('SELECT revision_sha256,source_review_json FROM bulletin_date_revisions').fetchone())
    paid_before = attempts(historical)
    outcomes_before = AttemptStore(historical.settings.database_path).bound_outcomes()
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: _fetched_document(text=SOURCE_TEXT + ' New captured detail.'))
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: pytest.fail('deferred replacement was sent'))
    def gate(request_key, captured_review):
        raise repair.ReviewDeferred('disk_low', 'Private evidence disk is below the required free space.')
    output = historical.output.with_name('deferred-replacement')
    result = review_one(historical, output_dir=output, request_gate=gate)
    assert result['terminal'] is False and result['defer_code'] == 'disk_low'
    assert attempts(historical) == paid_before
    assert AttemptStore(historical.settings.database_path).bound_outcomes() == outcomes_before
    assert not (output / 'reviews.jsonl').exists()
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(ValueError):
            repair.plan_repairs(db, review_files=[output / 'review-progress.jsonl'])
        empty = repair.plan_repairs(db)
        assert empty['entries'] == [] and repair.apply_repairs(db, empty) == 0
        after = tuple(db.execute('SELECT revision_sha256,source_review_json FROM bulletin_date_revisions').fetchone())
        assert after == before


def test_shared_legacy_interruption_never_captures_or_admits(historical, monkeypatch):
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: pytest.fail('legacy work was recaptured'))
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: pytest.fail('legacy work was resent'))
    counters = Counter()
    result = review_one(historical, legacy_interrupted=True, counters=counters,
        request_gate=lambda *args: pytest.fail('legacy work reached admission'))
    assert result['terminal'] is False and result['progress'] == 'legacy_attempt_interrupted'
    assert result['request_state'] == 'legacy_interrupted' and result['new_request'] is False
    assert counters['blocked_requests'] == counters['pending_reviews'] == 1
    assert counters['model_request_attempts'] == 0 and attempts(historical) == []


def test_shared_paid_protocol_failure_is_terminal_once_then_blocked_before_gate(historical, monkeypatch):
    posts = []
    rejected = {'invalid_schema': 'Retained only in the private ledger.'}
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: posts.append(payload) or envelope(rejected))
    cache = {}
    first = review_one(historical, source_cache=cache)
    assert first['terminal'] is True and first['new_request'] is True and first['paid_failure'] is True
    assert first['record']['status'] == 'unresolved' and first['record']['source_review']['activity'] is None
    ledger = attempts(historical)
    assert len(ledger) == 1 and ledger[0]['state'] == 'failed'
    assert json.loads(ledger[0]['raw_output_json']) == rejected
    assert '_paid_failure' not in json.loads(ledger[0]['decision_json'])
    other = next(row for row in historical.rows if row['id'] != historical.rows[0]['id'])
    blocked = review_one(historical, row=other, source_cache=cache,
        request_gate=lambda *args: pytest.fail('failed equivalent reached new paid admission'))
    assert blocked['terminal'] is False and blocked['new_request'] is False and blocked['paid_failure'] is False
    assert blocked['progress'] == 'equivalent_request_not_reusable' and blocked['request_state'] == 'failed'
    assert len(posts) == 1 and len(AttemptStore(historical.settings.database_path).bound_outcomes()) == 1


def test_shared_interrupted_reservation_remains_blocked_before_gate(historical, monkeypatch):
    cache = {}
    def interrupt(payload, *, audit=None):
        raise KeyboardInterrupt
    monkeypatch.setattr(acquisition, '_model_post', interrupt)
    with pytest.raises(KeyboardInterrupt):
        review_one(historical, source_cache=cache)
    assert [value['state'] for value in attempts(historical)] == ['reserved']
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: pytest.fail('reserved request was resent'))
    blocked = review_one(historical, source_cache=cache,
        request_gate=lambda *args: pytest.fail('reserved equivalent reached new paid admission'))
    assert blocked['terminal'] is False and blocked['new_request'] is False and blocked['paid_failure'] is False
    assert blocked['progress'] == 'equivalent_request_reserved' and blocked['request_state'] == 'reserved'
    assert AttemptStore(historical.settings.database_path).bound_outcomes() == set()
    assert not (historical.output / 'reviews.jsonl').exists()


@pytest.mark.parametrize('mode', ['success', 'unknown_ref', 'canonical_not_transport'])
def test_actual_reference_raw_output_and_guard_identity_are_preserved(historical, database, monkeypatch, mode):
    sent = []
    outputs = []
    def post(payload, *, audit=None):
        data = source_data_from_payload(payload)
        sent.append(data)
        output = (_model_response() if mode == 'canonical_not_transport' else
            reference_response(_model_response(), data))
        if mode == 'unknown_ref':
            output['activity']['content']['evidence'][0]['ref'] = 'unknown-model-selected-id'
        outputs.append(deepcopy(output))
        return envelope(output)
    monkeypatch.setattr(acquisition, '_model_post', post)
    result = run_review(historical, occurrence_ids=[historical.ids[0]])
    ledger = attempts(historical)
    assert len(sent) == len(outputs) == len(ledger) == result['model_request_attempts'] == 1
    assert json.loads(ledger[0]['raw_output_json']) == outputs[0]
    assert ledger[0]['state'] == ('completed' if mode == 'success' else 'failed')
    decision = json.loads(ledger[0]['decision_json'])
    captured = decision['source_review']['sources'][0]
    assert captured['text'] == SOURCE_TEXT
    assert captured['raw_response_base64'] == _fetched_document()['raw_response_base64']
    assert captured['raw_response_digest'] == _fetched_document()['raw_response_digest']
    if mode == 'success':
        assert outputs[0]['claims'][0]['quote'] and 'raw_text' not in outputs[0]['claims'][0]
        assert decision['claims'][0]['raw_text'] == SOURCE_TEXT
        assert decision['source_review']['activity']['content']['evidence'][0]['text'] == SOURCE_TEXT
    else:
        assert decision['status'] == 'unresolved' and decision['source_review']['activity'] is None
    rows, originals, _ = repair.date_snapshot(database)
    without_catalog = dict(sent[0])
    without_catalog.pop('evidence_catalog')
    assert logical_request(rows[0], originals[rows[0]['id']], sent[0],
        rules_version=repair.RULES_VERSION, model_name='test-model') == logical_request(
        rows[0], originals[rows[0]['id']], without_catalog,
        rules_version=repair.RULES_VERSION, model_name='test-model')
    assert ledger[0]['request_sha256'] == logical_request(rows[0], originals[rows[0]['id']], sent[0],
        rules_version=repair.RULES_VERSION, model_name='test-model')[0]
    duplicate = run_review(historical, output_dir=historical.output.with_name('reference-receipt-again'),
        occurrence_ids=[historical.ids[0]])
    assert duplicate['model_request_attempts'] == 0 and len(sent) == 1


def test_catalog_preparation_bound_retains_capture_without_reserving_or_sending(historical, monkeypatch):
    document = _fetched_document(text='x' * (acquisition.MAX_TEXT + 1))
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: document)
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: pytest.fail('incomplete catalog sent'))
    result = run_review(historical, occurrence_ids=[historical.ids[0]])
    assert result['model_request_attempts'] == 0 and result['preparation_failures'] == 1
    assert attempts(historical) == [] and records(result) == []
    progress = json.loads(Path(result['progress_artifact']).read_text().splitlines()[0])
    capture = json.loads(Path(progress['capture_artifact']).read_text())
    assert capture['source_review']['sources'][0]['text'] == document['text']
    assert capture['source_review']['sources'][0]['raw_response_digest'] == document['raw_response_digest']


def test_mixed_dates_reuse_original_metadata_across_issue_dates_without_new_judgment(historical, monkeypatch):
    bulletin_text = '刊载日期2026-10-14；报名11月26日；11月30日申请。'
    with closing(sqlite3.connect(historical.settings.database_path)) as db, db:
        db.execute('UPDATE bulletin_occurrences SET body_text=? WHERE id=2', (bulletin_text,))
    text = 'Published November 20, 2025. ' + SOURCE_TEXT
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: _fetched_document(text=text))
    output = _model_response()
    output['reason'] = '原文发布日期2025-11-20为报名年份锚点；2026-10-14仅是原刊载日期。'
    for kind, raw, day, resolution, evidence in [
        ('registration', '11月26日', '2025-11-26', 'contextual', '同活动原文发布日期2025-11-20提供年份锚点。'),
        ('application', '11月30日', None, 'year_unknown', '原文没有可确认的申请年份，仍然未知。'),
        ('listed', '2026-10-14', '2026-10-14', 'explicit', '原刊载正文逐字写出的刊载日期，不是活动日。'),
        ('source', 'November 20, 2025', '2025-11-20', 'explicit', '原文逐字公布的来源日期。'),
    ]:
        output['claims'].append({'kind': kind, 'raw_text': raw, 'normalized_date': day,
            'resolution': resolution, 'evidence': evidence, 'locator': None})
    posts = []
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None:
        posts.append(payload) or reference_envelope(payload, output))
    first = run_review(historical, occurrence_ids=[historical.ids[0]])
    assert first['model_request_attempts'] == first['new_reviews'] == 1
    frozen = attempts(historical)
    audit_receipt = Path(json.loads(frozen[0]['decision_json'])['_model_audit']['receipt'])
    audit_request = audit_receipt.with_name('request.json')
    audit_bytes = (audit_receipt.read_bytes(), audit_request.read_bytes())
    original = records(first)[0]
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        rows, originals, _ = repair.date_snapshot(db)
        immutable_dates = [tuple(row) for row in db.execute('SELECT * FROM bulletin_dates')]
    current = next(row for row in rows if row['id'] == 1)
    monkeypatch.setattr(acquisition, '_model_post', lambda *args, **kwargs: pytest.fail('free reuse sent a request'))
    monkeypatch.setattr(acquisition, 'fetch_document', lambda *args: pytest.fail('offline helper fetched a source'))
    previous = deepcopy(frozen[0])
    admitted = repair.reuse_completed_review(previous, current, originals[current['id']], model_name='test-model')
    assert admitted is not None and admitted['claims'] == original['claims']
    assert admitted['source_review'] == original['source_review']
    assert admitted['_reuse'] == {'request_sha256': frozen[0]['request_sha256'], 'original_issue_date': '2026-10-14'}
    assert 'current bulletin issue_date=2026-10-13' in admitted['reason']
    assert 'originally reviewed with bulletin issue_date=2026-10-14' in admitted['reason']
    assert original['reason'] in admitted['reason']
    assert previous == frozen[0]
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: _fetched_document(text=text))
    second = run_review(historical, occurrence_ids=[historical.ids[1]],
        output_dir=historical.output.with_name('mixed-equivalent'))
    assert second['model_request_attempts'] == second['blocked_requests'] == second['pending_reviews'] == 0
    assert second['reused_reviews'] == second['new_reviews'] == second['completed'] == 1
    reused = records(second)[0]
    assert reused['claims'] == original['claims'] and reused['source_review'] == original['source_review']
    assert reused['input_sha256'] == repair.input_digest(current, originals[current['id']])
    assert reused['related_context_sha256'] == current['related_context_sha256']
    assert reused['occurrence_id'] != original['occurrence_id'] and '_reuse' not in reused
    assert next(claim for claim in reused['claims'] if claim['kind'] == 'application')['normalized_date'] is None
    assert next(claim for claim in reused['claims'] if claim['kind'] == 'registration')['resolution'] == 'contextual'
    assert next(claim for claim in reused['claims'] if claim['kind'] == 'listed')['normalized_date'] == '2026-10-14'
    assert attempts(historical) == frozen and len(posts) == 1
    assert (audit_receipt.read_bytes(), audit_request.read_bytes()) == audit_bytes
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        assert len(repair.plan_repairs(db, review_files=[second['artifact']])['entries']) == 1
        assert [tuple(row) for row in db.execute('SELECT * FROM bulletin_dates')] == immutable_dates
        assert db.execute('SELECT count(*) FROM bulletin_date_revisions').fetchone()[0] == 0


@pytest.mark.parametrize(('status', 'source_status'), [
    ('uncertain', 'secondary_only'), ('needs_context', 'source_verified'),
    ('source_missing', 'source_unavailable'), ('invalid_source', 'source_mismatch'),
    ('uncertain', 'source_conflict'), ('no_relevant_date', 'source_verified'),
])
def test_completed_unconfirmed_or_dateless_verdicts_reuse_without_promotion(historical, monkeypatch, status, source_status):
    output = _model_response()
    output.update(status=status, claims=[], source_status=source_status)
    if source_status != 'source_verified':
        output['activity'] = None
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: reference_envelope(payload, output))
    first = run_review(historical, occurrence_ids=[historical.ids[0]])
    assert first['model_request_attempts'] == first['new_reviews'] == 1
    frozen = attempts(historical)
    assert frozen[0]['state'] == 'completed'
    monkeypatch.setattr(acquisition, '_model_post', lambda *args, **kwargs: pytest.fail('completed unknown verdict resent'))
    repeated = run_review(historical, occurrence_ids=[historical.ids[0]], output_dir=historical.output.with_name('unconfirmed'))
    assert repeated['model_request_attempts'] == repeated['blocked_requests'] == repeated['pending_reviews'] == 0
    assert repeated['reused_reviews'] == repeated['completed'] == repeated['new_reviews'] == 1
    assert records(repeated) == records(first) and attempts(historical) == frozen
    assert records(repeated)[0]['status'] == status
    assert records(repeated)[0]['source_review']['status'] == source_status
    assert records(repeated)[0]['claims'] == []


@pytest.mark.parametrize('corruption', [
    'decision_json', 'raw_reference', 'source_sha', 'image_sha', 'image_index', 'activity_quote',
    'college_quote', 'request_json', 'wire_sha', 'missing_audit', 'failed', 'reserved',
])
def test_corrupt_or_noncompleted_history_is_unpaid_block_not_retry(historical, monkeypatch, corruption):
    raw_image = image_bytes()
    cache = {SOURCE_URL: {'document': _fetched_document(), 'media': [{
        'url': 'https://primary.example.org/poster.png', 'mime': 'image/png',
        'data': base64.b64encode(raw_image).decode('ascii'), 'digest': hashlib.sha256(raw_image).hexdigest()}],
        'media_error': ''}}
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: reference_envelope(payload))
    first = run_review(historical, occurrence_ids=[historical.ids[0]], source_cache=cache)
    assert first['new_reviews'] == first['model_request_attempts'] == 1
    previous = attempts(historical)[0]
    decision = json.loads(previous['decision_json'])
    raw = json.loads(previous['raw_output_json'])
    column, value = 'decision_json', None
    if corruption == 'decision_json':
        value = '{broken'
    elif corruption == 'raw_reference':
        raw['activity']['content']['evidence'][0]['ref'] = 'unknown-corrupt-reference'
        column, value = 'raw_output_json', json.dumps(raw)
    elif corruption == 'source_sha':
        decision['source_review']['sources'][0]['raw_response_digest'] = '0' * 64
    elif corruption == 'image_sha':
        decision['source_review']['sources'][0]['images'][0]['sha256'] = '0' * 64
    elif corruption == 'image_index':
        decision['source_review']['sources'][0]['images'][0]['image_index'] += 1
    elif corruption == 'activity_quote':
        decision['source_review']['activity']['content']['evidence'][0]['text'] = 'Fabricated activity evidence'
    elif corruption == 'college_quote':
        decision['source_review']['activity']['colleges'] = [{'name': 'Invented college', 'relation': 'associated',
            'evidence': {'url': SOURCE_URL, 'text': 'Invented college quote', 'image_index': None}}]
    elif corruption == 'request_json':
        column, value = 'request_json', '{}'
    elif corruption == 'wire_sha':
        decision['_model_audit']['request_sha256'] = '0' * 64
    elif corruption == 'missing_audit':
        decision.pop('_model_audit')
    else:
        column, value = 'state', corruption
    value = json.dumps(decision) if value is None else value
    with closing(sqlite3.connect(historical.ledger)) as db, db:
        db.execute('UPDATE historical_attempts SET ' + column + '=? WHERE request_sha256=?',
            (value, previous['request_sha256']))
    frozen = attempts(historical)
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        rows, originals, _ = repair.date_snapshot(db)
    monkeypatch.setattr(acquisition, '_model_post', lambda *args, **kwargs: pytest.fail('damaged history was resent'))
    monkeypatch.setattr(acquisition, 'fetch_document', lambda *args: pytest.fail('offline helper fetched'))
    assert repair.reuse_completed_review(frozen[0], rows[0], originals[rows[0]['id']], model_name='test-model') is None
    repeated = run_review(historical, occurrence_ids=[historical.ids[0]], source_cache=cache,
        output_dir=historical.output.with_name('corrupt-history'))
    assert repeated['model_request_attempts'] == repeated['reused_reviews'] == repeated['new_reviews'] == 0
    assert repeated['blocked_requests'] == repeated['pending_reviews'] == 1
    assert records(repeated) == [] and attempts(historical) == frozen
    assert len(AttemptStore(historical.settings.database_path).bound_outcomes()) == 1


@pytest.mark.parametrize('change', ['publication', 'date_claim', 'model', 'rules', 'source_text'])
def test_offline_reuse_rejects_changed_current_semantics(historical, monkeypatch, change):
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: reference_envelope(payload))
    run_review(historical, occurrence_ids=[historical.ids[0]])
    previous = attempts(historical)[0]
    row = deepcopy(historical.rows[0])
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        _, originals, _ = repair.date_snapshot(db)
    claims, model = deepcopy(originals[row['id']]), 'test-model'
    if change == 'publication':
        row['related_occurrences'][0]['body_text'] += ' Changed same-activity context.'
    elif change == 'date_claim':
        claims[0]['evidence'] += ' Changed date evidence.'
    elif change == 'model':
        model = 'other-model'
    elif change == 'rules':
        monkeypatch.setattr(repair, 'RULES_VERSION', 'different-rules')
    else:
        decision = json.loads(previous['decision_json'])
        decision['source_review']['sources'][0]['text'] += ' Text absent from original request.'
        previous['decision_json'] = json.dumps(decision)
    monkeypatch.setattr(acquisition, 'fetch_document', lambda *args: pytest.fail('offline reuse fetched'))
    monkeypatch.setattr(acquisition, '_model_post', lambda *args, **kwargs: pytest.fail('offline reuse posted'))
    assert repair.reuse_completed_review(previous, row, claims, model_name=model) is None


def test_cached_original_request_is_rejected_after_wire_bytes_change(historical, monkeypatch):
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: reference_envelope(payload))
    run_review(historical, occurrence_ids=[historical.ids[0]])
    previous = attempts(historical)[0]
    row = historical.rows[0]
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        _, originals, _ = repair.date_snapshot(db)
    monkeypatch.setattr(acquisition, 'fetch_document', lambda *args: pytest.fail('offline cache reuse fetched'))
    monkeypatch.setattr(acquisition, '_model_post', lambda *args, **kwargs: pytest.fail('offline cache reuse posted'))
    assert repair.reuse_completed_review(previous, row, originals[row['id']], model_name='test-model') is not None
    receipt = Path(json.loads(previous['decision_json'])['_model_audit']['receipt'])
    wire = receipt.with_name('request.json')
    wire.write_bytes(wire.read_bytes() + b' ')
    assert repair.reuse_completed_review(previous, row, originals[row['id']], model_name='test-model') is None
    assert attempts(historical) == [previous]


@pytest.mark.parametrize('anchor', ['wire_audit', 'original_receipt'])
def test_original_canonical_wire_without_reference_catalog_reuses_strictly(historical, monkeypatch, anchor):
    posts = []
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None:
        posts.append(payload) or reference_envelope(payload))
    first = run_review(historical, occurrence_ids=[historical.ids[0]])
    previous = attempts(historical)[0]
    # Generate the pre-reference wire format through the real request serializer.
    # Only the paid transport is mocked; the retained canonical response is not
    # reconstructed from the decoded decision or converted into reference JSON.
    source_data = deepcopy(source_data_from_payload(posts[0]))
    source_data.pop('evidence_catalog')
    original_output = _model_response()
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None:
        posts.append(payload) or envelope(original_output))
    identities = []
    raw, model = acquisition.request_model_json(repair.HISTORICAL_REVIEW_INSTRUCTIONS,
        source_data, audit_directory=AttemptStore(historical.settings.database_path).audit_directory,
        audit_ready=identities.append)
    assert raw == original_output and 'evidence_catalog' not in source_data_from_payload(posts[1])
    decision = json.loads(previous['decision_json'])
    decision['_model_audit'] = identities[0]
    if anchor == 'original_receipt':
        decision.pop('_model_audit')
    with closing(sqlite3.connect(historical.ledger)) as db, db:
        db.execute('UPDATE historical_attempts SET decision_json=?,raw_output_json=? WHERE request_sha256=?',
            (json.dumps(decision), json.dumps(raw), previous['request_sha256']))
    frozen = attempts(historical)
    row = historical.rows[0]
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        rows, originals, _ = repair.date_snapshot(db)
        repair._review_decisions([first['artifact']], rows, originals)
    original_row = dict(row, original_date_claims=originals[row['id']],
        _review_receipt_sha256=repair._reuse_receipt_digest(records(first)[0]))
    monkeypatch.setattr(acquisition, 'fetch_document', lambda *args: pytest.fail('legacy offline reuse fetched'))
    monkeypatch.setattr(acquisition, '_model_post', lambda *args, **kwargs: pytest.fail('legacy canonical request resent'))
    admitted = repair.reuse_completed_review(frozen[0], row, originals[row['id']], model_name=model,
        original_occurrence=original_row if anchor == 'original_receipt' else None)
    assert admitted is not None
    assert admitted['claims'] == decision['claims'] and admitted['source_review'] == decision['source_review']
    assert attempts(historical) == frozen and len(posts) == 2
    if anchor == 'original_receipt':
        current = next(value for value in rows if value['id'] != row['id'])
        resolver = lambda key: original_row if key == previous['request_sha256'] else None
        repeated = review_one(historical, row=current,
            source_cache={SOURCE_URL: {'document': _fetched_document(), 'media': [], 'media_error': ''}},
            original_occurrence_resolver=resolver)
        assert repeated['terminal'] is True and repeated['new_request'] is False
        assert repeated['record']['claims'] == decision['claims']
        assert repeated['_reuse']['original_issue_date'] == row['issue_date']
        assert attempts(historical) == frozen


@pytest.mark.parametrize('corruption', [
    None, 'missing_proof', 'receipt_digest', 'original_claims', 'raw_scope',
    'wrong_reference_origin', 'invalid_present_audit', 'current_context',
])
def test_preaudit_reference_reuse_requires_proven_receipt_and_exact_original_scope(historical, monkeypatch, corruption):
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: reference_envelope(payload))
    first = run_review(historical, occurrence_ids=[historical.ids[0]])
    frozen = attempts(historical)
    previous = deepcopy(frozen[0])
    decision = json.loads(previous['decision_json'])
    decision.pop('_model_audit')
    with closing(sqlite3.connect(historical.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        rows, originals, _ = repair.date_snapshot(db)
        repair._review_decisions([first['artifact']], rows, originals)
    original = rows[0]
    current = deepcopy(rows[1])
    proof = dict(original, original_date_claims=deepcopy(originals[original['id']]),
        _review_receipt_sha256=repair._reuse_receipt_digest(records(first)[0]))
    if corruption == 'missing_proof':
        proof = None
    elif corruption == 'receipt_digest':
        proof['_review_receipt_sha256'] = '0' * 64
    elif corruption == 'original_claims':
        proof['original_date_claims'][0]['evidence'] += ' changed original input'
    elif corruption == 'raw_scope':
        raw = json.loads(previous['raw_output_json'])
        raw['evidence_scope'] = '0' * 64
        previous['raw_output_json'] = json.dumps(raw)
    elif corruption == 'wrong_reference_origin':
        proof.update({key: current[key] for key in ('source_sha256', 'local_key', 'issue_date')})
    elif corruption == 'invalid_present_audit':
        decision['_model_audit'] = None
    elif corruption == 'current_context':
        current['related_occurrences'][0]['body_text'] += ' Changed current context.'
    previous['decision_json'] = json.dumps(decision)
    monkeypatch.setattr(acquisition, 'fetch_document', lambda *args: pytest.fail('preaudit reuse fetched'))
    monkeypatch.setattr(acquisition, '_model_post', lambda *args, **kwargs: pytest.fail('preaudit reuse posted'))
    admitted = repair.reuse_completed_review(previous, current, originals[current['id']],
        model_name='test-model', original_occurrence=proof)
    if corruption is None:
        assert admitted is not None and admitted['claims'] == decision['claims']
        assert admitted['source_review'] == decision['source_review']
        assert 'originally reviewed with bulletin issue_date=' + original['issue_date'] in admitted['reason']
        assert 'current bulletin issue_date=' + current['issue_date'] in admitted['reason']
    else:
        assert admitted is None
    assert attempts(historical) == frozen
