"""Frozen scope, paid boundaries, durable capture and read-only supervision."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import multiprocessing
import os
import shutil
import signal
import sqlite3
import stat
from types import SimpleNamespace

import pytest

from nannanews import acquisition
from nannanews import bulletin_date_repair as repair
from nannanews import bulletin_full_review as full
from nannanews.bulletin_historical_attempts import AttemptStore
from test_bulletin_date_repair import (
    database, prepared, source_review, _fetched_document, SOURCE_URL,
    reference_response, source_data_from_payload,
)


@pytest.fixture
def corpus(database, tmp_path, monkeypatch):
    # Three accepted receipts plus four genuinely unseen activities/URLs.
    with database:
        for number in range(2, 8):
            database.execute('INSERT INTO bulletin_items VALUES(?,?)', (number, 'activity-' + str(number)))
            database.execute('INSERT INTO bulletin_occurrences VALUES(?,?,1,?,\'detail\',\'讲座\',\'活动日期见链接\',\'{}\')',
                             (number, number, 'section-' + str(number)))
            database.execute('INSERT INTO bulletin_sources VALUES(?,?)', (number, SOURCE_URL + '?id=' + str(number)))
    path = tmp_path / 'main.sqlite3'
    with closing(sqlite3.connect(path)) as disk:
        database.backup(disk)
    settings = SimpleNamespace(database_path=path, data_dir=tmp_path,
        model_name='gpt-6-luna', model_api_style='responses', collection_enabled=False, collection_time='04:00')
    rows, originals, _ = repair.date_snapshot(database)
    receipts = []
    for index, row in enumerate(rows[:3]):
        # Explicit unavailable samples are still accepted honest prior model outcomes.
        record = prepared(row, originals[row['id']], status='unresolved', claims=[],
            review=source_review(status='source_unavailable', sources=[]))
        record['reviewer'] = 'luna:gpt-6-luna'
        receipt = tmp_path / ('sample-' + str(index) + '.json')
        receipt.write_text(json.dumps(record), encoding='utf-8')
        receipts.append({'occurrence_id': record['occurrence_id'], 'review_file': str(receipt),
                         'sha256': hashlib.sha256(receipt.read_bytes()).hexdigest(), 'source_status': 'source_unavailable'})
    acceptance = tmp_path / 'acceptance.json'
    acceptance.write_text(json.dumps({'schema_version': 1, 'accepted': True, 'automatic_live_apply': False,
        **full._configuration(settings), 'samples': receipts}), encoding='utf-8')
    output = tmp_path / 'full'
    # Only network/model/time/OS boundaries are mocked; canonical reviewer/ledger/storage remain real.
    monkeypatch.setattr(acquisition, '_model_config', lambda: settings)
    monkeypatch.setattr(full, '_now', lambda: datetime(2026, 10, 17, 4, tzinfo=timezone.utc))
    monkeypatch.setattr(full.shutil, 'disk_usage', lambda path: SimpleNamespace(free=full.MIN_FREE_BYTES * 2))
    def fetch(url):
        document = _fetched_document()
        document.update(canonical_url=url, resolved_url=url)
        return document
    monkeypatch.setattr(acquisition, 'fetch_document', fetch)
    return SimpleNamespace(settings=settings, output=output, acceptance=acceptance, rows=rows,
                           seed_ids={value['occurrence_id'] for value in receipts}, database=database)


def prepare(corpus):
    return full.prepare_full_review(corpus.output, corpus.acceptance, corpus.settings)


def response(output):
    return {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'status': 'completed',
        'content': [{'type': 'output_text', 'text': json.dumps(output)}]}]}


def uncertain_response(payload):
    output = {'status': 'uncertain', 'reason': 'Unable to confirm identity.', 'claims': [],
        'source_status': 'secondary_only', 'source_reason': 'The article does not establish this activity.',
        'citations': [], 'activity': None}
    return response(reference_response(output, source_data_from_payload(payload)))


def test_prepare_freezes_every_occurrence_and_never_creates_paid_state(corpus, monkeypatch):
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, **kwargs: pytest.fail('prepare sent model request'))
    before = corpus.settings.database_path.read_bytes()
    result = prepare(corpus)
    scope = json.loads((corpus.output / 'scope.json').read_text())
    assert result['occurrences'] == len(corpus.rows) == 7
    assert result['max_new_requests'] == 4
    assert {value['occurrence_id'] for value in scope['occurrences']} == {full._public_id(row) for row in corpus.rows}
    assert len(scope['seeds']) == 3
    assert corpus.settings.database_path.read_bytes() == before
    assert not AttemptStore(corpus.settings.database_path).path.exists()
    assert stat.S_IMODE(corpus.output.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in corpus.output.rglob('*') if path.is_file())


def test_seed_receipts_skip_paid_calls_and_resume_receipt_state_gap(corpus, monkeypatch):
    prepare(corpus)
    posts = []
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, **kwargs: posts.append(payload) or uncertain_response(payload))
    result = full.run_full_review(corpus.output, corpus.settings)
    assert result['phase'] == 'completed'
    assert result['counts']['checked_occurrences'] == 7
    assert result['counts']['model_request_attempts'] == len(posts) == 4
    assert result['model_quality']['cumulative']['new_paid_outcomes'] == 4
    assert result['model_quality']['cumulative']['valid_unconfirmed'] == 4
    assert result['model_quality']['cumulative']['failures'] == 0
    assert result['source_coverage']['readable_primary'] == 4
    timing = result['_timing']
    assert timing['cumulative']['measured_outcomes'] == 4
    assert timing['cumulative']['unknown_outcomes'] == 0
    for stage in ('source_fetch_seconds', 'model_request_seconds', 'review_storage_seconds', 'review_seconds'):
        assert timing['cumulative']['fields'][stage]['samples'] == 4
    receipts = [value for path in corpus.output.glob('batches/*/reviews.jsonl') for value in full._lines(path)]
    assert {value['occurrence_id'] for value in receipts}.isdisjoint(corpus.seed_ids)
    assert all('_timing' not in value and '_timing' not in value['source_review'] for value in receipts)
    events = [full._load(path) for path in sorted((corpus.output / 'journal').glob('event-*.json'))
              if full._load(path).get('event') == 'outcome']
    assert len(events) == 4
    assert timing['cumulative']['fields']['review_seconds']['total'] == pytest.approx(
        sum(event['_timing']['review_seconds'] for event in events))
    # State is not decision authority: lose it after durable receipts, then resume.
    (corpus.output / 'state.json').unlink()
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, **kwargs: pytest.fail('receipt gap rebilled'))
    resumed = full.run_full_review(corpus.output, corpus.settings)
    assert resumed['counts']['checked_occurrences'] == 7
    assert resumed['counts']['model_request_attempts'] == 4
    assert resumed['model_quality']['cumulative']['new_paid_outcomes'] == 4
    assert resumed['model_quality']['cumulative']['valid_unconfirmed'] == 4
    assert resumed['model_quality']['cumulative']['failures'] == 0
    assert resumed['_timing'] == timing
    with closing(sqlite3.connect(corpus.settings.database_path)) as db:
        assert db.execute('SELECT count(*) FROM bulletin_date_revisions').fetchone()[0] == 0


@pytest.mark.parametrize('failure', ['unavailable', 'homepage', 'media'])
def test_source_blocked_never_posts_and_never_refetches_on_resume(corpus, monkeypatch, failure):
    prepare(corpus)
    fetches = []
    def fetch(url):
        fetches.append(url)
        if failure == 'unavailable':
            raise acquisition.AcquisitionError('No public article.', 'source_unavailable')
        document = _fetched_document()
        document.update(canonical_url=url, resolved_url='https://primary.example.org/' if failure == 'homepage' else url)
        if failure == 'media':
            document.update(capture_complete=False, capture_errors=[{'code': 'source_incomplete'}])
        return document
    monkeypatch.setattr(acquisition, 'fetch_document', fetch)
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, **kwargs: pytest.fail('source-blocked row billed'))
    result = full.run_full_review(corpus.output, corpus.settings)
    assert result['counts']['blocked_no_primary'] == 4
    assert result['counts']['model_request_attempts'] == 0
    assert result['model_quality']['cumulative']['new_paid_outcomes'] == 0
    assert result['model_quality']['cumulative']['failures'] == 0
    assert result['source_coverage']['reviewed_occurrences'] == 7
    assert sum(result['source_coverage'][kind] for kind in
        ('no_readable_primary', 'source_mismatch', 'capture_incomplete')) == 7
    assert len(fetches) == 4
    assert not list(corpus.output.glob('batches/*/reviews.jsonl'))
    full.run_full_review(corpus.output, corpus.settings)
    assert len(fetches) == 4


def test_no_primary_links_are_nonterminal_and_cost_nothing(corpus, monkeypatch):
    with closing(sqlite3.connect(corpus.settings.database_path)) as db, db:
        db.execute('DELETE FROM bulletin_sources WHERE occurrence_id<=4')
    prepare(corpus)
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: pytest.fail('unlinked source fetched'))
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, **kwargs: pytest.fail('empty-source row billed'))
    result = full.run_full_review(corpus.output, corpus.settings)
    assert result['counts']['blocked_no_primary'] == 4
    assert result['counts']['model_request_attempts'] == 0
    assert result['model_quality']['cumulative']['new_paid_outcomes'] == 0
    assert result['model_quality']['cumulative']['failures'] == 0
    assert not list(corpus.output.glob('batches/*/reviews.jsonl'))

def test_source_coverage_replaces_pending_capture_classification_without_double_count(corpus):
    prepare(corpus)
    worker = full.FullReviewWorker(corpus.output, corpus.settings)
    identity = next(identity for identity in worker.bindings if identity not in corpus.seed_ids)
    capture = {'source_review': {'status': 'secondary_only', 'sources': [{'text': '完整原文'}], 'attempts': []}}
    worker._record_source_coverage(identity, capture)
    worker.done[identity] = 'request_blocked'
    assert worker._source_coverage_summary()['readable_primary'] == 1
    verdict = {'source_review': {'status': 'source_mismatch', 'sources': [{'text': '完整原文'}], 'attempts': []}}
    worker._record_source_coverage(identity, verdict)
    worker._record_source_coverage(identity, verdict)
    summary = worker._source_coverage_summary()
    assert summary['readable_primary'] == 0
    assert summary['source_mismatch'] == 1
    assert summary['reviewed_occurrences'] == 4


def test_quality_window_ignores_unpaid_outcomes_and_is_bounded(corpus):
    prepare(corpus)
    worker = full.FullReviewWorker(corpus.output, corpus.settings)

    def outcome(*, new_request, paid_failure=False, failure_code=None, terminal=True,
                status='uncertain', activity=None):
        return {'new_request': new_request, 'paid_failure': paid_failure,
            'model_failure_code': failure_code, 'terminal': terminal,
            'record': {'source_review': {'status': status, 'activity': activity}}}

    no_source = outcome(new_request=False, terminal=False, status='source_unavailable')
    guarded = outcome(new_request=False, paid_failure=True, status='source_verified', activity={'id': 'verified'})
    reused = outcome(new_request=False, status='source_verified', activity={'id': 'verified'})
    assert worker._quality(no_source, 'blocked_no_primary') is None
    assert worker._quality(guarded, 'request_blocked') is None
    assert worker._quality(reused, 'primary_confirmed') is None
    assert worker._model_quality_summary()['cumulative']['new_paid_outcomes'] == 0

    paid = [
        (outcome(new_request=True, status='source_verified', activity={'id': 'verified'}), 'primary_confirmed'),
        (outcome(new_request=True, status='uncertain'), 'model_terminal_unconfirmed'),
        (outcome(new_request=True, paid_failure=True, failure_code='model_output_invalid'), 'model_terminal_failed'),
        (outcome(new_request=True, paid_failure=True, failure_code='model_unavailable'), 'model_terminal_failed'),
        (outcome(new_request=True, paid_failure=True), 'model_terminal_failed'),
    ]
    for result, category in paid:
        worker._quality(result, category)
    summary = worker._model_quality_summary()
    assert summary['cumulative']['new_paid_outcomes'] == 5
    assert summary['cumulative']['verified'] == 1
    assert summary['cumulative']['valid_unconfirmed'] == 1
    assert summary['cumulative']['malformed_model_failure'] == 1
    assert summary['cumulative']['transport_failure'] == 1
    assert summary['cumulative']['model_failure_unknown'] == 1
    assert summary['cumulative']['failures'] == 3

    for index in range(full.MODEL_QUALITY_WINDOW + 20):
        failed = index % 2 == 0
        result = outcome(new_request=True, paid_failure=failed,
            failure_code='model_http_error' if failed else None,
            status='uncertain')
        worker._quality(result, 'model_terminal_failed' if failed else 'model_terminal_unconfirmed')
    summary = worker._model_quality_summary()
    assert len(worker.model_quality_window) == full.MODEL_QUALITY_WINDOW
    assert summary['rolling']['sample_size'] == full.MODEL_QUALITY_WINDOW
    assert summary['rolling']['failures'] == full.MODEL_QUALITY_WINDOW // 2
    assert summary['cumulative']['new_paid_outcomes'] == 5 + full.MODEL_QUALITY_WINDOW + 20


def _duplicate_lock(database, queue):
    try:
        with full.worker_lock(database):
            queue.put('acquired')
    except ValueError:
        queue.put('blocked')


def test_duplicate_workers_are_blocked_across_output_directories(corpus):
    context = multiprocessing.get_context('forkserver')
    queue = context.Queue()
    process = context.Process(target=_duplicate_lock, args=(corpus.settings.database_path, queue))
    try:
        with full.worker_lock(corpus.settings.database_path):
            process.start()
            process.join(timeout=10)
            assert not process.is_alive()
            assert process.exitcode == 0
            assert queue.get(timeout=2) == 'blocked'
        with full.worker_lock(corpus.settings.database_path):
            pass
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        process.close()
        queue.close()
        queue.join_thread()


def test_clone_environment_is_refused_before_new_sidecar_or_lock(corpus):
    prepare(corpus)
    clone = corpus.settings.database_path.with_name('preview.sqlite3')
    shutil.copyfile(corpus.settings.database_path, clone)
    settings = SimpleNamespace(**vars(corpus.settings))
    settings.database_path = clone
    with pytest.raises(ValueError):
        full.run_full_review(corpus.output, settings)
    assert not AttemptStore(clone).path.exists()
    assert not clone.with_name('.preview.sqlite3.full-review.lock').exists()


def test_changed_raw_input_refuses_resume_without_request(corpus, monkeypatch):
    prepare(corpus)
    with closing(sqlite3.connect(corpus.settings.database_path)) as db, db:
        db.execute("UPDATE bulletin_occurrences SET body_text='Corrected evidence' WHERE id=1")
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, **kwargs: pytest.fail('changed scope billed'))
    with pytest.raises(ValueError):
        full.run_full_review(corpus.output, corpus.settings)
    assert not AttemptStore(corpus.settings.database_path).path.exists()


def test_inflight_crash_reservation_never_expires_or_resubmits(corpus, monkeypatch):
    prepare(corpus)
    calls = []
    def crash(payload, **kwargs):
        calls.append(payload)
        raise KeyboardInterrupt
    monkeypatch.setattr(acquisition, '_model_post', crash)
    with pytest.raises(KeyboardInterrupt):
        full.run_full_review(corpus.output, corpus.settings)
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, **kwargs: uncertain_response(payload))
    result = full.run_full_review(corpus.output, corpus.settings)
    assert result['counts']['request_blocked'] == 1
    assert result['counts']['checked_occurrences'] == 7
    with closing(sqlite3.connect(AttemptStore(corpus.settings.database_path).path)) as db:
        assert db.execute("SELECT count(*) FROM historical_attempts WHERE state='reserved'").fetchone()[0] == 1


def test_daily_priority_is_only_new_mode_and_uses_slot_completion(corpus):
    settings = corpus.settings
    settings.collection_enabled = True
    with closing(sqlite3.connect(settings.database_path)) as db, db:
        db.execute('CREATE TABLE daily_runs(id INTEGER PRIMARY KEY,mode TEXT,started_at TEXT,finished_at TEXT,status TEXT)')
        db.execute("INSERT INTO daily_runs VALUES(97,'history','2026-01-01T00:00:00+08:00',NULL,'running')")
    before = datetime(2026, 10, 17, 3, 54, tzinfo=full.BEIJING)
    assert full.daily_pause_reason(settings, before) is None
    assert full.daily_pause_reason(settings, before + timedelta(minutes=1)) == 'daily_priority'
    after = before + timedelta(minutes=10)
    assert full.daily_pause_reason(settings, after) == 'daily_priority'
    with closing(sqlite3.connect(settings.database_path)) as db, db:
        db.execute("INSERT INTO daily_runs VALUES(98,'new','2026-10-17T04:00:00+08:00',NULL,'running')")
    assert full.daily_pause_reason(settings, after) == 'daily_priority'
    with closing(sqlite3.connect(settings.database_path)) as db, db:
        db.execute("UPDATE daily_runs SET status='complete',finished_at='2026-10-17T04:03:00+08:00' WHERE id=98")
    assert full.daily_pause_reason(settings, after) is None


def test_disk_stop_and_budget_gate_leave_no_reservations(corpus, monkeypatch):
    prepare(corpus)
    worker = full.FullReviewWorker(corpus.output, corpus.settings)
    capture = source_review()
    monkeypatch.setattr(full.shutil, 'disk_usage', lambda path: SimpleNamespace(free=full.MIN_FREE_BYTES - 1))
    with pytest.raises(repair.ReviewDeferred) as disk:
        worker.request_gate('unpaid-disk', capture)
    assert disk.value.code == 'disk_low'
    worker.stop.set()
    with pytest.raises(repair.ReviewDeferred) as stop:
        worker.request_gate('unpaid-stop', capture)
    assert stop.value.code == 'stop_requested'
    worker.stop.clear()
    monkeypatch.setattr(full.shutil, 'disk_usage', lambda path: SimpleNamespace(free=full.MIN_FREE_BYTES * 2))
    worker.counters['model_request_attempts'] = worker.scope['max_new_requests']
    with pytest.raises(repair.ReviewDeferred) as budget:
        worker.request_gate('unpaid-budget', capture)
    assert budget.value.code == 'request_budget'
    assert all(worker.attempt_store.lookup(key) is None for key in ('unpaid-disk', 'unpaid-stop', 'unpaid-budget'))


def test_three_new_paid_failures_pause_and_stop_drains(corpus, monkeypatch):
    prepare(corpus)
    posts = []
    worker = full.FullReviewWorker(corpus.output, corpus.settings)
    def post(payload, **kwargs):
        posts.append(payload)
        return response({'invalid': 'protocol output'})
    monkeypatch.setattr(acquisition, '_model_post', post)
    # Observe genuine pause, then ask for graceful shutdown instead of waiting a minute.
    original_wait = worker.stop.wait
    def wait(timeout=None):
        if worker.quality_failures >= 3:
            worker.stop.set()
            return True
        return original_wait(timeout)
    monkeypatch.setattr(worker.stop, 'wait', wait)
    result = worker.run()
    assert len(posts) == 3
    assert result['phase'] == 'stopped'
    assert result['counts']['pending_unseen'] == 1
    assert result['model_quality']['cumulative']['new_paid_outcomes'] == 3
    assert result['model_quality']['cumulative']['malformed_model_failure'] == 3
    assert result['model_quality']['cumulative']['failures'] == 3
    resumed = full.FullReviewWorker(corpus.output, corpus.settings)
    assert resumed.quality_failures == 3
    assert resumed.pause_reason() == 'quality_circuit'
    assert resumed.model_quality_totals['malformed_model_failure'] == 3
    assert resumed._model_quality_summary()['cumulative']['new_paid_outcomes'] == 3


def test_sigterm_drains_inflight_receipt_before_stopping(corpus, monkeypatch):
    prepare(corpus)
    posts = []
    def post(payload, **kwargs):
        posts.append(payload)
        os.kill(os.getpid(), signal.SIGTERM)
        return uncertain_response(payload)
    monkeypatch.setattr(acquisition, '_model_post', post)
    result = full.run_full_review(corpus.output, corpus.settings)
    assert result['phase'] == 'stopped'
    assert len(posts) == result['counts']['model_request_attempts'] == 1
    receipts = [value for path in corpus.output.glob('batches/*/reviews.jsonl') for value in full._lines(path)]
    assert len(receipts) == 1
    with closing(sqlite3.connect(AttemptStore(corpus.settings.database_path).path)) as db:
        assert db.execute('SELECT state FROM historical_attempts').fetchone()[0] == 'completed'


def test_capture_protocol_change_refuses_frozen_resume_before_creating_paid_state(corpus, monkeypatch):
    prepare(corpus)
    monkeypatch.setattr(acquisition, 'SOURCE_CAPTURE_VERSION', 'different-capture-protocol')
    monkeypatch.setattr(acquisition, '_model_post', lambda *args, **kwargs: pytest.fail('changed capture protocol billed'))
    with pytest.raises(ValueError, match='Frozen model, protocol, rules or instructions changed'):
        full.run_full_review(corpus.output, corpus.settings)
    assert not AttemptStore(corpus.settings.database_path).path.exists()


def test_immutable_cached_capture_is_not_reparsed_after_protocol_change(corpus, tmp_path, monkeypatch):
    cache = full.SourceCache(tmp_path / 'frozen-cache', corpus.settings.database_path, hot_bytes=1)
    document = dict(_fetched_document(), source_capture_version='frozen-protocol')
    cache[SOURCE_URL] = {'document': document, 'media': [], 'media_error': ''}
    path = cache._path(SOURCE_URL)
    before = path.read_bytes()
    monkeypatch.setattr(acquisition, 'SOURCE_CAPTURE_VERSION', 'new-protocol')
    monkeypatch.setattr(acquisition, 'reparse_source_document', lambda *args: pytest.fail('frozen capture reparsed'))
    reopened = full.SourceCache(cache.directory, corpus.settings.database_path)
    retained = reopened.get(SOURCE_URL)
    assert retained['document']['source_capture_version'] == 'frozen-protocol'
    assert retained['document']['raw_response_digest'] == document['raw_response_digest']
    assert path.read_bytes() == before


def test_durable_cache_hash_failure_eviction_and_interrupted_get(corpus, tmp_path):
    cache = full.SourceCache(tmp_path / 'cache', corpus.settings.database_path, hot_bytes=1)
    value = {'document': _fetched_document(), 'media': [], 'media_error': ''}
    cache[SOURCE_URL] = value
    assert not cache.hot
    reopened = full.SourceCache(tmp_path / 'cache', corpus.settings.database_path)
    assert reopened.get(SOURCE_URL)['document']['raw_response_digest'] == value['document']['raw_response_digest']
    failed_url = SOURCE_URL + '?failed=1'
    cache[failed_url] = {'error': {'code': 'source_unavailable', 'reason': 'No public response.'}}
    assert reopened.get(failed_url)['error']['code'] == 'source_unavailable'
    interrupted = SOURCE_URL + '?interrupted=1'
    assert cache.get(interrupted) is None
    assert full.SourceCache(tmp_path / 'cache', corpus.settings.database_path).get(interrupted)['error']['code'] == 'capture_interrupted'
    path = cache._path(SOURCE_URL)
    envelope = json.loads(path.read_text())
    envelope['capture']['document']['text'] = 'Changed bytes'
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError):
        full.SourceCache(tmp_path / 'cache', corpus.settings.database_path).get(SOURCE_URL)
    envelope['capture']['document']['raw_response_digest'] = '0' * 64
    envelope['sha256'] = full._digest(envelope['capture'])
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError):
        full.SourceCache(tmp_path / 'cache', corpus.settings.database_path).get(SOURCE_URL)


@pytest.mark.parametrize('kind', ['public', 'pasted', 'missing_raw', 'incomplete_media'])
def test_source_document_reuse_requires_real_complete_public_capture(corpus, tmp_path, kind):
    document = _fetched_document()
    document['model_output'] = {'private': 'must not become facts'}
    document['draft'] = {'private': 'must not become facts'}
    if kind == 'pasted':
        document['source_type'] = 'pasted'
    elif kind == 'missing_raw':
        document.pop('raw_response_base64')
    elif kind == 'incomplete_media':
        document['images'] = [SOURCE_URL + '/poster.png']
    with closing(sqlite3.connect(corpus.settings.database_path)) as db, db:
        db.execute('CREATE TABLE source_documents(id INTEGER PRIMARY KEY,source_url TEXT,document_json TEXT,model_status TEXT)')
        db.execute("INSERT INTO source_documents VALUES(132,?,?,'failed')", (SOURCE_URL, json.dumps(document)))
    before = corpus.settings.database_path.read_bytes()
    cache = full.SourceCache(tmp_path / 'cache', corpus.settings.database_path)
    value = cache.get(SOURCE_URL)
    if kind == 'public':
        assert value['document']['raw_response_base64'] == document['raw_response_base64']
        assert 'draft' not in value['document'] and 'model_output' not in value['document']
    else:
        assert value is None  # Canonical caller may fetch once, never reconstruct bytes.
    assert corpus.settings.database_path.read_bytes() == before


def test_durable_cache_keeps_only_valid_source_measurements_and_never_retimes_legacy(corpus, tmp_path):
    directory = tmp_path / 'timed-cache'
    cache = full.SourceCache(directory, corpus.settings.database_path, hot_bytes=1)
    timing = {'source_fetch_seconds': 2.25, 'source_media_prepare_seconds': 3.5}
    cache[SOURCE_URL] = {'document': _fetched_document(), 'media': [], 'media_error': '', '_timing': timing}
    reopened = full.SourceCache(directory, corpus.settings.database_path, hot_bytes=1)
    assert reopened.get(SOURCE_URL)['_timing'] == timing
    legacy_url = SOURCE_URL + '?legacy=1'
    document = dict(_fetched_document(), canonical_url=legacy_url, resolved_url=legacy_url)
    cache[legacy_url] = {'document': document, 'media': [], 'media_error': ''}
    path = cache._path(legacy_url)
    before = path.read_bytes()
    assert '_timing' not in reopened.get(legacy_url)
    assert path.read_bytes() == before
    failed_url = SOURCE_URL + '?failed=timed'
    cache[failed_url] = {'error': {'code': 'source_unavailable', 'reason': 'Unavailable.'},
                         '_timing': {'source_fetch_seconds': 4}}
    assert reopened.get(failed_url)['_timing'] == {'source_fetch_seconds': 4}


@pytest.mark.parametrize('timing', [
    {'source_fetch_seconds': True}, {'source_fetch_seconds': -1},
    {'source_fetch_seconds': float('nan')}, {'source_fetch_seconds': float('inf')},
    {'source_fetch_seconds': 7 * 86400 + 1}, {'source_fetch_seconds': 10 ** 1000},
    {'source_fetch_seconds': '2'}, {'source_fetch_seconds': {'seconds': 2}},
    {'model_request_seconds': 2}, {'source_fetch_seconds': 1, 'provider_url': 'private'},
])
def test_invalid_private_cache_metadata_cannot_overwrite_capture(corpus, tmp_path, timing):
    cache = full.SourceCache(tmp_path / 'invalid-timing', corpus.settings.database_path)
    capture = {'document': _fetched_document(), 'media': [], 'media_error': ''}
    cache[SOURCE_URL] = capture
    path = cache._path(SOURCE_URL)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        cache[SOURCE_URL] = dict(capture, _timing=timing)
    assert path.read_bytes() == before
    assert '_timing' not in full.SourceCache(cache.directory, corpus.settings.database_path).get(SOURCE_URL)


def test_legacy_outcome_replay_keeps_missing_durations_unknown_without_rewriting_evidence(corpus, monkeypatch):
    prepare(corpus)
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, **kwargs: uncertain_response(payload))
    full.run_full_review(corpus.output, corpus.settings)
    evidence = {}
    for path in sorted((corpus.output / 'journal').glob('event-*.json')):
        event = json.loads(path.read_text())
        event.pop('_timing', None)
        path.write_text(json.dumps(event), encoding='utf-8')
        evidence[path] = path.read_bytes()
    for path in corpus.output.glob('batches/*/reviews.jsonl'):
        evidence[path] = path.read_bytes()
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: pytest.fail('resume refetched source'))
    monkeypatch.setattr(acquisition, '_model_post', lambda *args, **kwargs: pytest.fail('legacy outcome was rebilled'))
    result = full.run_full_review(corpus.output, corpus.settings)
    timing = result['_timing']
    assert timing['last'] is None
    assert timing['cumulative'] == {'measured_outcomes': 0, 'unknown_outcomes': 4, 'fields': {}}
    assert timing['rolling'] == {'sample_size': 4, 'measured_outcomes': 0, 'fields': {}}
    assert all(path.read_bytes() == original for path, original in evidence.items())
    # Polling reads the bounded status snapshot, never old journals/captures to invent timings.
    load = full._load
    def status_only(path, maximum=1024 * 1024):
        assert path.name == 'status.json'
        return load(path, maximum)
    monkeypatch.setattr(full, '_load', status_only)
    assert full.monitor_observation(corpus.output)['_timing'] == timing


def test_worker_timing_window_is_bounded_and_partial_measurements_are_not_zero_filled(corpus):
    prepare(corpus)
    worker = full.FullReviewWorker(corpus.output, corpus.settings)
    worker._record_timing(None)
    worker._record_timing({'source_fetch_seconds': 9})
    for _ in range(full.TIMING_WINDOW + 20):
        worker._record_timing({'review_seconds': 2})
    summary = worker._timing_summary()
    assert len(worker.timing_window) == summary['rolling']['sample_size'] == full.TIMING_WINDOW
    assert summary['cumulative']['unknown_outcomes'] == 1
    assert summary['cumulative']['fields']['source_fetch_seconds'] == {
        'samples': 1, 'total': 9, 'max': 9, 'mean': 9}
    assert 'source_fetch_seconds' not in summary['rolling']['fields']
    assert summary['rolling']['fields']['review_seconds']['mean'] == 2


def test_monitor_stale_pid_reuse_healthy_pause_and_terminal(corpus, monkeypatch):
    prepare(corpus)
    now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    status = {'run_id': 'test', 'pid': os.getpid(), 'start_identity': full.process_identity(os.getpid()),
        'phase': 'reviewing', 'terminal': False, 'heartbeat_at': (now - timedelta(minutes=20)).isoformat(),
        'last_progress_at': (now - timedelta(minutes=20)).isoformat(), 'counts': {'model_request_attempts': 2}}
    full._atomic(corpus.output / 'status.json', status)
    observation = full.monitor_observation(corpus.output, now)
    assert observation['process_alive'] is True
    assert set(observation['warnings']) == {'heartbeat_stale', 'progress_stale'}
    status.update(phase='paused', pause_reason='daily_priority', heartbeat_at=now.isoformat())
    full._atomic(corpus.output / 'status.json', status)
    assert full.monitor_observation(corpus.output, now)['warnings'] == []
    status['start_identity'] = 'previous-boot:1'
    full._atomic(corpus.output / 'status.json', status)
    assert 'worker_not_running_or_pid_reused' in full.monitor_observation(corpus.output, now)['warnings']
    status.update(phase='completed', terminal=True)
    full._atomic(corpus.output / 'status.json', status)
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, **kwargs: pytest.fail('monitor sent model request'))
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: pytest.fail('monitor fetched source'))
    assert full.watch_full_review(corpus.output, interval=600)['terminal'] is True

def test_monitor_uses_cached_journal_timestamp_without_enumeration(monitor_run, monkeypatch):
    run = monitor_run
    run.status['journal_mtime'] = 1760702400.0
    full._atomic(run.output / 'status.json', run.status)
    monkeypatch.setattr(full.Path, 'glob', lambda *args, **kwargs: pytest.fail('monitor enumerated journal files'))
    assert full.monitor_observation(run.output, run.now)['journal_mtime'] == 1760702400.0


def test_symlinked_artifacts_are_refused(corpus, tmp_path):
    prepare(corpus)
    target = tmp_path / 'external.json'
    target.write_text('{}')
    (corpus.output / 'status.json').unlink()
    (corpus.output / 'status.json').symlink_to(target)
    with pytest.raises(ValueError):
        full.monitor_observation(corpus.output)
    assert target.read_text() == '{}'


@pytest.fixture
def monitor_run(tmp_path):
    output = full._directory(tmp_path / 'monitor-run')
    now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    status = {'run_id': 'monitor-test', 'pid': os.getpid(),
        'start_identity': full.process_identity(os.getpid()), 'phase': 'paused', 'terminal': False,
        'heartbeat_at': now.isoformat(), 'last_progress_at': now.isoformat(),
        'pause_reason': 'quality_circuit', 'quality_failures': 3, 'consecutive_quality_failures': 3,
        'model_quality': {'window_limit': 100,
            'cumulative': {'verified': 48, 'valid_unconfirmed': 8, 'malformed_model_failure': 3,
                'model_failure_other': 1, 'model_failure_unknown': 0, 'transport_failure': 2,
                'unknown_outcome': 2, 'new_paid_outcomes': 64, 'failures': 6},
            'rolling': {'verified': 15, 'valid_unconfirmed': 3, 'malformed_model_failure': 1,
                'model_failure_other': 0, 'model_failure_unknown': 0, 'transport_failure': 1,
                'unknown_outcome': 0, 'sample_size': 20, 'failures': 2, 'failure_rate': 0.1}},
        'source_coverage': {'readable_primary': 180, 'no_readable_primary': 30,
            'source_mismatch': 0, 'capture_incomplete': 0, 'not_recorded': 0,
            'reviewed_occurrences': 210},
        'counts': {'checked_occurrences': 210, 'model_request_attempts': 64}}
    full._atomic(output / 'status.json', status)
    return SimpleNamespace(output=output, now=now, status=status)


@pytest.mark.parametrize('reason,severity', [
    ('quality_circuit', 'error'), ('disk_low', 'error'), ('scope_changed', 'error'),
    ('configuration_changed', 'error'), ('request_budget', 'warning'),
])
def test_monitor_fresh_heartbeat_pause_requires_action(monitor_run, reason, severity):
    run = monitor_run
    run.status['pause_reason'] = reason
    full._atomic(run.output / 'status.json', run.status)
    observation = full.monitor_observation(run.output, run.now)
    assert observation['process_alive'] is True
    assert observation['heartbeat_age_seconds'] == 0
    assert observation['warnings'] == [reason]
    assert observation['attention_required'] is True
    assert observation['pause_intentional'] is False
    assert observation['severity'] == severity
    assert observation['alerts'][0]['code'] == reason
    assert observation['alerts'][0]['severity'] == severity
    assert observation['alerts'][0]['action']
    assert observation['quality_failures'] == 3
    assert observation['consecutive_quality_failures'] == 3
    assert observation['model_quality']['cumulative']['failures'] == 6
    assert observation['source_coverage'] == run.status['source_coverage']
    if reason == 'quality_circuit':
        assert observation['alerts'][0]['details'] == {
            'consecutive_failures': 3, 'cumulative_failures': 6, 'cumulative_requests': 64,
            'rolling_failures': 2, 'rolling_requests': 20}
    assert observation['counts'] == run.status['counts']
    # Observation alone is read-only, unlike the explicitly recording API.
    assert {path.name for path in run.output.iterdir()} == {'status.json'}


@pytest.mark.parametrize('reason', ['daily_priority', 'operator_pause', 'stop_requested'])
def test_monitor_intentional_pause_only_suppresses_stale_work(monitor_run, reason):
    run = monitor_run
    run.status.update(pause_reason=reason, last_progress_at=(run.now - timedelta(hours=8)).isoformat())
    run.status['model_quality']['rolling'].update(sample_size=10, failures=5,
        malformed_model_failure=5, verified=5, failure_rate=0.5)
    full._atomic(run.output / 'status.json', run.status)
    observation = full.monitor_observation(run.output, run.now)
    assert observation['pause_intentional'] is True
    assert observation['attention_required'] is False
    assert observation['warnings'] == observation['alerts'] == []
    assert observation['severity'] == 'ok'
    assert observation['model_quality']['rolling']['failures'] == 5
    run.status['heartbeat_at'] = (run.now - timedelta(minutes=4)).isoformat()
    full._atomic(run.output / 'status.json', run.status)
    assert full.monitor_observation(run.output, run.now)['warnings'] == ['heartbeat_stale']


def test_quality_pause_does_not_hide_stale_progress(monitor_run):
    run = monitor_run
    run.status['last_progress_at'] = (run.now - timedelta(hours=8)).isoformat()
    full._atomic(run.output / 'status.json', run.status)
    observation = full.monitor_observation(run.output, run.now)
    assert set(observation['warnings']) == {'quality_circuit', 'progress_stale'}
    assert observation['process_alive'] is True

def test_rolling_quality_alert_distinguishes_consecutive_from_cumulative(monitor_run):
    run = monitor_run
    run.status.update(phase='reviewing', pause_reason=None, quality_failures=0,
                      consecutive_quality_failures=0)
    run.status['model_quality']['cumulative'].update(new_paid_outcomes=64, failures=12)
    run.status['model_quality']['rolling'].update(sample_size=10, failures=3,
        malformed_model_failure=2, transport_failure=1, verified=7, failure_rate=0.3)
    full._atomic(run.output / 'status.json', run.status)
    observation = full.monitor_observation(run.output, run.now)
    assert observation['quality_failures'] == observation['consecutive_quality_failures'] == 0
    assert observation['model_quality']['cumulative']['failures'] == 12
    assert observation['warnings'] == ['model_quality_degraded']
    assert observation['alerts'][0]['details'] == {
        'consecutive_failures': 0, 'cumulative_failures': 12, 'cumulative_requests': 64,
        'rolling_failures': 3, 'rolling_requests': 10}


@pytest.mark.parametrize('identity', [None, 'different-boot:reused-start'])
def test_monitor_unexpected_exit_and_pid_reuse_remain_actionable(monitor_run, monkeypatch, identity):
    run = monitor_run
    run.status.update(phase='reviewing', pause_reason=None)
    full._atomic(run.output / 'status.json', run.status)
    monkeypatch.setattr(full, 'process_identity', lambda pid: identity)
    observation = full.record_monitor_observation(run.output, run.now)
    assert observation['process_alive'] is False
    assert observation['warnings'] == ['worker_not_running_or_pid_reused']
    assert observation['attention_required'] is True
    assert observation['alerts'][0]['severity'] == 'error'
    latest = json.loads((run.output / 'monitor-alerts.json').read_text())
    assert latest['active_alerts'][0]['code'] == 'worker_not_running_or_pid_reused'


def test_monitor_terminal_failure_evidence_and_safe_guidance(monitor_run, capsys):
    run = monitor_run
    run.status.update(phase='failed', terminal=True,
        failure_type='ProviderError', failure_reason='secret-token raw provider output',
        heartbeat_at=(run.now - timedelta(hours=8)).isoformat())
    full._atomic(run.output / 'status.json', run.status)
    observation = full.watch_full_review(run.output, interval=1)
    assert observation['terminal'] is True
    assert observation['warnings'] == ['worker_failed']
    assert observation['attention_required'] is True
    stdout = capsys.readouterr().out
    latest = (run.output / 'monitor-alerts.json').read_text()
    periodic = (run.output / 'monitor.jsonl').read_text()
    assert 'FULL_REVIEW_ALERT ' in stdout
    assert 'worker_failed' in stdout and 'worker_failed' in latest and 'worker_failed' in periodic
    assert 'secret-token' not in stdout + latest + periodic
    assert 'raw provider output' not in stdout + latest + periodic


def test_monitor_alert_dedup_clear_and_reopen_are_durable(monitor_run):
    run = monitor_run
    first = full.record_monitor_observation(run.output, run.now)
    assert first['notification_required'] is True
    assert [(value['state'], value['code']) for value in first['alert_transitions']] == [('opened', 'quality_circuit')]
    repeated = full.record_monitor_observation(run.output, run.now + timedelta(seconds=1))
    assert repeated['notification_required'] is False
    assert repeated['alert_transitions'] == []
    assert repeated['attention_required'] is True
    assert repeated['alerts'][0]['code'] == 'quality_circuit'
    run.status.update(phase='reviewing', pause_reason=None, quality_failures=0)
    full._atomic(run.output / 'status.json', run.status)
    resolved = full.record_monitor_observation(run.output, run.now + timedelta(seconds=2))
    assert resolved['notification_required'] is True
    assert resolved['attention_required'] is False
    assert resolved['alert_transitions'][0]['state'] == 'resolved'
    latest = json.loads((run.output / 'monitor-alerts.json').read_text())
    assert latest['active_alerts'] == []
    assert latest['attention_required'] is False
    assert latest['transitions'][-1]['state'] == 'resolved'
    assert full.record_monitor_observation(run.output, run.now + timedelta(seconds=3))['alert_transitions'] == []
    run.status.update(phase='paused', pause_reason='quality_circuit', quality_failures=3)
    full._atomic(run.output / 'status.json', run.status)
    reopened = full.record_monitor_observation(run.output, run.now + timedelta(seconds=4))
    assert reopened['notification_required'] is True
    assert reopened['alert_transitions'][0]['state'] == 'opened'
    latest = json.loads((run.output / 'monitor-alerts.json').read_text())
    assert [value['state'] for value in latest['transitions']] == ['opened', 'resolved', 'opened']
    assert latest['active_alerts'][0]['opened_at'] == reopened['at']
    periodic = list(full._lines(run.output / 'monitor.jsonl'))
    assert len(periodic) == 5
    assert [value['attention_required'] for value in periodic] == [True, True, False, False, True]


def test_watcher_prints_one_transition_but_periodic_active_attention(monitor_run, monkeypatch, capsys):
    run = monitor_run
    class TwoObservations:
        def __init__(self):
            self.waits = 0
        def is_set(self):
            return self.waits == 2
        def set(self):
            self.waits = 2
        def wait(self, interval):
            self.waits += 1
    monkeypatch.setattr(full.threading, 'Event', TwoObservations)
    monkeypatch.setattr(full, '_now', lambda: run.now)
    full.watch_full_review(run.output, interval=600)
    lines = capsys.readouterr().out.splitlines()
    notifications = [json.loads(line.removeprefix('FULL_REVIEW_ALERT '))
                     for line in lines if line.startswith('FULL_REVIEW_ALERT ')]
    periodic = [json.loads(line.removeprefix('FULL_REVIEW_WATCH '))
                for line in lines if line.startswith('FULL_REVIEW_WATCH ')]
    assert len(notifications) == 1
    assert notifications[0]['state'] == 'opened' and notifications[0]['code'] == 'quality_circuit'
    assert len(periodic) == 2
    assert all(value['attention_required'] and value['warnings'] == ['quality_circuit'] for value in periodic)
    assert periodic[1]['notification_required'] is False
    assert list(full._lines(run.output / 'monitor.jsonl')) == periodic


def test_monitor_alert_history_is_bounded_and_private(monitor_run):
    run = monitor_run
    observations = full.MAX_MONITOR_ALERT_TRANSITIONS + 4
    for index in range(observations):
        run.status['pause_reason'] = 'quality_circuit' if index % 2 == 0 else 'operator_pause'
        full._atomic(run.output / 'status.json', run.status)
        full.record_monitor_observation(run.output, run.now + timedelta(seconds=index))
    path = run.output / 'monitor-alerts.json'
    latest = json.loads(path.read_text())
    assert len(latest['transitions']) == full.MAX_MONITOR_ALERT_TRANSITIONS
    assert latest['transitions'][0]['at'] == (run.now + timedelta(seconds=4)).isoformat()
    assert latest['transitions'][-1]['state'] == 'resolved'
    assert latest['active_alerts'] == []
    assert path.stat().st_size <= full.MAX_MONITOR_ALERT_BYTES
    assert stat.S_IMODE(run.output.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in run.output.iterdir())


@pytest.mark.parametrize('artifact', ['monitor-alerts.json', 'monitor.jsonl', '.monitor.lock'])
def test_monitor_rejects_symlinked_write_artifacts(monitor_run, tmp_path, artifact):
    run = monitor_run
    target = tmp_path / 'untouched-private-evidence'
    target.write_bytes(b'unchanged paid evidence')
    target.chmod(0o640)
    (run.output / artifact).symlink_to(target)
    with pytest.raises(ValueError, match='symlink'):
        full.record_monitor_observation(run.output, run.now)
    assert target.read_bytes() == b'unchanged paid evidence'
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_monitor_rejects_oversized_alert_state_without_overwrite(monitor_run):
    run = monitor_run
    path = run.output / 'monitor-alerts.json'
    path.write_bytes(b'x' * (full.MAX_MONITOR_ALERT_BYTES + 1))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='bounded regular file'):
        full.record_monitor_observation(run.output, run.now)
    assert path.read_bytes() == before
    assert not (run.output / 'monitor.jsonl').exists()


def test_monitor_never_mutates_worker_or_paid_authority(corpus, monkeypatch):
    prepare(corpus)
    worker = full.FullReviewWorker(corpus.output, corpus.settings)
    worker.quality_failures = 3
    worker._publish(phase='paused', pause_reason='quality_circuit')
    assert json.loads((corpus.output / 'status.json').read_text())['quality_failures'] == 3
    protected = [corpus.settings.database_path, worker.attempt_store.path,
                 *[path for path in corpus.output.rglob('*') if path.is_file()]]
    for name in ('pause-request', 'stop-request', 'resume-request'):
        path = corpus.output / name
        path.write_text('operator-owned permission')
        protected.append(path)
    before = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in protected}
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, **kwargs: pytest.fail('monitor sent model request'))
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: pytest.fail('monitor fetched source'))
    monkeypatch.setattr(full.os, 'kill', lambda *args: pytest.fail('monitor signalled a process'))
    for _ in range(2):
        observation = full.record_monitor_observation(corpus.output)
        assert observation['attention_required'] is True
        assert observation['quality_failures'] == 3
    assert worker.quality_failures == 3
    assert {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in protected} == before
    new_files = {path.name for path in corpus.output.rglob('*') if path.is_file() and path not in before}
    assert new_files == {'.monitor.lock', 'monitor-alerts.json', 'monitor.jsonl'}


class FakeReviewClock:
    def __init__(self, monkeypatch):
        self.now = datetime(2026, 10, 17, 4, tzinfo=timezone.utc)
        monkeypatch.setattr(full, '_now', lambda: self.now)

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def resume_waits(self, worker, monkeypatch):
        waits = []
        def wait(seconds):
            assert worker.status['pause_reason'] == 'transport_cooldown'
            assert 0 < seconds <= 60
            waits.append(seconds)
            assert len(waits) <= 200, 'Worker failed to leave a bounded fake cooldown'
            self.advance(seconds)
            return False
        monkeypatch.setattr(worker.stop, 'wait', wait)
        return waits


def transport_error(status=None, retry=None):
    error = acquisition.AcquisitionError(
        f'模型 API 返回 HTTP {status}；未创建草稿，不会自动重试。' if status else 'Network timeout.',
        'model_http_error' if status else 'model_unavailable')
    error.model_http_status = status
    error.model_retry_after_seconds = retry
    return error


@pytest.mark.parametrize('status', [None, 408, 429, 503])
def test_transient_failures_auto_resume_only_unseen_requests(corpus, monkeypatch, status):
    prepare(corpus)
    clock = FakeReviewClock(monkeypatch)
    worker = full.FullReviewWorker(corpus.output, corpus.settings)
    waits = clock.resume_waits(worker, monkeypatch)
    posts = []
    def failed(payload, **kwargs):
        posts.append(payload)
        raise transport_error(status)
    monkeypatch.setattr(acquisition, '_model_post', failed)
    result = worker.run()
    assert result['phase'] == 'completed'
    assert len(posts) == result['counts']['model_request_attempts'] == 4
    assert sum(waits) == 600 + 1800 + 1800
    assert result['quality_failures'] == 0
    assert result['transport_failures'] == 4
    assert result['model_quality']['cumulative']['failures'] == 4
    events = [full._load(path) for path in sorted((corpus.output / 'journal').glob('event-*.json'))]
    intents = [event['request_sha256'] for event in events if event['event'] == 'intent']
    assert len(intents) == len(set(intents)) == 4
    assert all(event['model_failure_code'] in full.MODEL_TRANSPORT_FAILURE_CODES
               for event in events if event['event'] == 'outcome')
    with closing(sqlite3.connect(worker.attempt_store.path)) as ledger:
        before = ledger.execute('SELECT request_sha256,state,error FROM historical_attempts ORDER BY request_sha256').fetchall()
    assert all(row[1] == 'failed' for row in before)
    monkeypatch.setattr(acquisition, '_model_post', lambda *args, **kwargs: pytest.fail('Failed paid key replayed'))
    replayed = full.run_full_review(corpus.output, corpus.settings)
    assert replayed['counts'] == result['counts']
    assert replayed['model_quality'] == result['model_quality']
    assert replayed['transport_failures'] == 4
    with closing(sqlite3.connect(worker.attempt_store.path)) as ledger:
        assert ledger.execute('SELECT request_sha256,state,error FROM historical_attempts ORDER BY request_sha256').fetchall() == before


def test_cooldown_restart_retry_after_and_success_reset(corpus, monkeypatch):
    prepare(corpus)
    clock = FakeReviewClock(monkeypatch)
    first = full.FullReviewWorker(corpus.output, corpus.settings)
    def first_failure(*args, **kwargs):
        raise transport_error(429, 1200)
    monkeypatch.setattr(acquisition, '_model_post', first_failure)
    def stop_in_cooldown(seconds):
        assert first.status['pause_reason'] == 'transport_cooldown'
        first.stop.set()
        return True
    monkeypatch.setattr(first.stop, 'wait', stop_in_cooldown)
    stopped = first.run()
    assert stopped['counts']['model_request_attempts'] == 1
    assert stopped['cooldown_remaining_seconds'] == 1200
    originals = {path: path.read_bytes() for path in (corpus.output / 'journal').glob('event-*.json')}
    frozen_scope = (corpus.output / 'scope.json').read_bytes()
    clock.advance(300)
    restarted = full.FullReviewWorker(corpus.output, corpus.settings)
    assert restarted.transport_failures == 1
    assert restarted.status['cooldown_remaining_seconds'] == 900
    assert all(path.read_bytes() == value for path, value in originals.items())
    waits = clock.resume_waits(restarted, monkeypatch)
    calls = []
    def post(payload, **kwargs):
        calls.append(clock.now)
        if len(calls) == 1:
            raise transport_error(503, 2400)
        if len(calls) == 2:
            return uncertain_response(payload)
        raise transport_error(429)
    monkeypatch.setattr(acquisition, '_model_post', post)
    result = restarted.run()
    assert sum(waits) == 900 + 2400
    assert (calls[1] - calls[0]).total_seconds() == 2400
    assert result['transport_failures'] == 1
    assert result['cooldown_remaining_seconds'] == 600
    assert result['quality_failures'] == 0
    assert result['counts']['model_request_attempts'] == 4
    assert (corpus.output / 'scope.json').read_bytes() == frozen_scope
    assert all(path.read_bytes() == value for path, value in originals.items())
    durable = full.FullReviewWorker(corpus.output, corpus.settings)
    assert durable.cooldown_until == result['cooldown_until']
    assert durable.transport_failures == 1


def test_legacy_malformed_then_two_429_reconstructs_without_old_circuit(corpus, monkeypatch):
    prepare(corpus)
    clock = FakeReviewClock(monkeypatch)
    worker = full.FullReviewWorker(corpus.output, corpus.settings)
    calls = []
    def post(payload, **kwargs):
        calls.append(payload)
        if len(calls) == 1:
            return response({'invalid': 'protocol output'})
        raise transport_error(429)
    monkeypatch.setattr(acquisition, '_model_post', post)
    def wait(seconds):
        if len(calls) == 3:
            worker.stop.set()
            return True
        clock.advance(seconds)
        return False
    monkeypatch.setattr(worker.stop, 'wait', wait)
    worker.run()
    assert worker.quality_failures == 1
    # Reproduce the old on-disk journal shape and obsolete aggregate counter.
    outcome_number = 0
    for path in sorted((corpus.output / 'journal').glob('event-*.json')):
        event = full._load(path)
        if event['event'] == 'outcome':
            for field in ('model_failure_code', 'model_http_status', 'model_retry_after_seconds',
                          'transport_failures', 'cooldown_until'):
                event.pop(field, None)
            outcome_number += 1
            event['quality_failures'] = outcome_number
            full._atomic(path, event)
    legacy_status = full._load(corpus.output / 'status.json')
    legacy_status.update(phase='stopped', pause_reason='quality_circuit', quality_failures=3,
                         consecutive_quality_failures=3)
    full._atomic(corpus.output / 'status.json', legacy_status)
    originals = {path: path.read_bytes() for path in (corpus.output / 'journal').glob('event-*.json')}
    clock.advance(9 * 3600)
    monkeypatch.setattr(AttemptStore, 'lookup', lambda *args: pytest.fail('Replay loaded full paid payload'))
    resumed = full.FullReviewWorker(corpus.output, corpus.settings)
    assert resumed.quality_failures == 1
    assert resumed.transport_failures == 2
    assert resumed.pause_reason() is None
    assert resumed.status['cooldown_remaining_seconds'] == 0
    assert resumed.status['counts']['model_request_attempts'] == 3
    assert resumed._model_quality_summary()['cumulative']['failures'] == 3
    assert resumed._model_quality_summary()['cumulative']['new_paid_outcomes'] == 3
    assert all(path.read_bytes() == value for path, value in originals.items())


def test_transport_preserves_manual_quality_streak_and_free_results_prove_nothing(corpus):
    prepare(corpus)
    worker = full.FullReviewWorker(corpus.output, corpus.settings)
    malformed = {'new_request': True, 'paid_failure': True, 'model_failure_code': 'model_evidence_invalid'}
    transient = {'new_request': True, 'paid_failure': True, 'model_failure_code': 'model_http_error',
                 'model_http_status': 429}
    worker._quality(malformed, 'model_terminal_failed')
    worker._quality(transient, 'model_terminal_failed')
    until = worker.cooldown_until
    worker._quality({'new_request': False, 'terminal': True}, 'model_terminal_unconfirmed')
    assert worker.quality_failures == worker.transport_failures == 1
    assert worker.cooldown_until == until
    worker._quality(malformed, 'model_terminal_failed')
    worker._quality(transient, 'model_terminal_failed')
    worker._quality(malformed, 'model_terminal_failed')
    assert worker.quality_failures == 3
    assert worker.transport_failures == 2
    assert worker.pause_reason() == 'quality_circuit'
    worker._quality({'new_request': True, 'terminal': True}, 'model_terminal_unconfirmed')
    assert worker.quality_failures == worker.transport_failures == 0
    assert worker.cooldown_until is None


@pytest.mark.parametrize('code,status', [('model_http_error', 401), ('model_http_error', 403),
                                      ('model_http_error', None), ('unknown_failure', None)])
def test_permanent_or_unknown_failures_remain_manual(corpus, code, status):
    prepare(corpus)
    worker = full.FullReviewWorker(corpus.output, corpus.settings)
    for _ in range(3):
        worker._quality({'new_request': True, 'paid_failure': True, 'model_failure_code': code,
                         'model_http_status': status}, 'model_terminal_failed')
    assert worker.pause_reason() == 'quality_circuit'
    assert worker.transport_failures == 0
    assert worker.cooldown_until is None


def test_cooldown_preserves_stop_pause_daily_disk_scope_config_and_budget_gates(corpus, monkeypatch):
    prepare(corpus)
    worker = full.FullReviewWorker(corpus.output, corpus.settings)
    worker._quality({'new_request': True, 'paid_failure': True, 'model_failure_code': 'model_unavailable'},
                    'model_terminal_failed')
    assert worker.pause_reason() == 'transport_cooldown'
    worker.stop.set()
    assert worker.pause_reason() == 'stop_requested'
    worker.stop.clear()
    pause = corpus.output / 'pause-request'
    pause.write_text('operator pause')
    assert worker.pause_reason() == 'operator_pause'
    pause.unlink()
    with monkeypatch.context() as patch:
        patch.setattr(full, 'daily_pause_reason', lambda settings: 'daily_priority')
        assert worker.pause_reason() == 'daily_priority'
    with monkeypatch.context() as patch:
        patch.setattr(full.shutil, 'disk_usage', lambda path: SimpleNamespace(free=0))
        assert worker.pause_reason() == 'disk_low'
    with monkeypatch.context() as patch:
        patch.setattr(full, '_configuration', lambda settings: {})
        assert worker.pause_reason() == 'configuration_changed'
    with monkeypatch.context() as patch:
        patch.setattr(full, '_signature', lambda db: {})
        assert worker.pause_reason() == 'scope_changed'
    worker.counters['model_request_attempts'] = worker.scope['max_new_requests']
    with pytest.raises(repair.ReviewDeferred) as blocked:
        worker.request_gate('must-not-reserve', source_review())
    assert blocked.value.code == 'request_budget'
    assert worker._attempt_metadata('must-not-reserve') is None


def test_monitor_healthy_cooldown_expires_without_masking_health_faults(monitor_run):
    run = monitor_run
    run.status.update(pause_reason='transport_cooldown', transport_failures=4,
        cooldown_until=(run.now + timedelta(minutes=30)).isoformat(),
        last_progress_at=(run.now - timedelta(hours=1)).isoformat())
    run.status['model_quality']['rolling'].update(sample_size=10, failures=5, transport_failure=5)
    full._atomic(run.output / 'status.json', run.status)
    observation = full.monitor_observation(run.output, run.now)
    assert observation['pause_intentional'] is True
    assert observation['severity'] == 'ok'
    assert observation['warnings'] == []
    assert observation['transport_failures'] == 4
    assert observation['cooldown_remaining_seconds'] == 1800
    assert observation['model_quality']['rolling']['failures'] == 5
    run.status['heartbeat_at'] = (run.now - timedelta(minutes=4)).isoformat()
    full._atomic(run.output / 'status.json', run.status)
    assert full.monitor_observation(run.output, run.now)['warnings'] == ['heartbeat_stale']
    run.status['heartbeat_at'] = (run.now + timedelta(minutes=31)).isoformat()
    full._atomic(run.output / 'status.json', run.status)
    expired = full.monitor_observation(run.output, run.now + timedelta(minutes=31))
    assert expired['pause_intentional'] is False
    assert expired['cooldown_remaining_seconds'] == 0
    assert set(expired['warnings']) == {'progress_stale', 'model_quality_degraded'}


@pytest.mark.parametrize('flag', [True, False, None])
def test_partial_transport_flag_is_durable_without_reclassifying_malformed(corpus, monkeypatch, flag):
    prepare(corpus)
    FakeReviewClock(monkeypatch)
    worker = full.FullReviewWorker(corpus.output, corpus.settings)
    def failed(payload, **kwargs):
        worker.stop.set()
        error = acquisition.AcquisitionError(
            '模型 API 响应不完整，未创建草稿。' if flag is True else 'Malformed JSON or oversized response.',
            'model_response_invalid')
        error.model_transport_failure = flag
        raise error
    monkeypatch.setattr(acquisition, '_model_post', failed)
    result = worker.run()
    assert result['counts']['model_request_attempts'] == 1
    expected_kind = 'transport_failure' if flag is True else 'malformed_model_failure'
    assert result['model_quality']['cumulative'][expected_kind] == 1
    assert result['model_quality']['cumulative']['failures'] == 1
    assert result['quality_failures'] == (0 if flag is True else 1)
    assert result['transport_failures'] == (1 if flag is True else 0)
    outcome_path = next(path for path in (corpus.output / 'journal').glob('event-*.json')
                        if full._load(path)['event'] == 'outcome')
    outcome = full._load(outcome_path)
    assert outcome['model_failure_code'] == 'model_response_invalid'
    assert outcome.get('model_transport_failure') is (True if flag is True else None)
    monkeypatch.setattr(acquisition, '_model_post', lambda *args, **kwargs: pytest.fail('Partial paid response replayed'))
    resumed = full.FullReviewWorker(corpus.output, corpus.settings)
    assert resumed.quality_failures == result['quality_failures']
    assert resumed.transport_failures == result['transport_failures']
    assert resumed.cooldown_until == result['cooldown_until']
    assert resumed.pause_reason() == ('transport_cooldown' if flag is True else None)
    with closing(sqlite3.connect(worker.attempt_store.path)) as ledger:
        assert ledger.execute('SELECT state FROM historical_attempts').fetchall() == [('failed',)]
    if flag is True:
        # Legacy partial bodies were counted as malformed. Reconstruct downtime
        # from the exact safe ledger message without rewriting historical metrics.
        outcome.pop('model_transport_failure')
        outcome['model_quality_kind'] = 'malformed_model_failure'
        full._atomic(outcome_path, outcome)
        original = outcome_path.read_bytes()
        legacy = full.FullReviewWorker(corpus.output, corpus.settings)
        assert legacy.quality_failures == 0
        assert legacy.transport_failures == 1
        assert legacy.pause_reason() == 'transport_cooldown'
        assert legacy._model_quality_summary()['cumulative']['malformed_model_failure'] == 1
        assert legacy._model_quality_summary()['cumulative']['transport_failure'] == 0
        assert outcome_path.read_bytes() == original


@pytest.mark.parametrize('status', [401, 403])
@pytest.mark.parametrize('code', ['model_unavailable', 'model_response_invalid'])
def test_permanent_http_precedes_transport_flags(corpus, status, code):
    prepare(corpus)
    worker = full.FullReviewWorker(corpus.output, corpus.settings)
    for _ in range(3):
        worker._quality({'new_request': True, 'paid_failure': True, 'model_failure_code': code,
                         'model_http_status': status, 'model_transport_failure': True},
                        'model_terminal_failed')
    assert worker.quality_failures == 3
    assert worker.transport_failures == 0
    assert worker.cooldown_until is None
    assert worker.pause_reason() == 'quality_circuit'


@pytest.mark.parametrize('code', ['model_audit_failed', 'model_preparation_failed', 'model_evidence_invalid'])
def test_nontransport_failure_precedes_preserved_http_facts(corpus, code):
    prepare(corpus)
    worker = full.FullReviewWorker(corpus.output, corpus.settings)
    for _ in range(3):
        worker._quality({'new_request': True, 'paid_failure': True, 'model_failure_code': code,
                         'model_http_status': 503, 'model_transport_failure': True,
                         'model_retry_after_seconds': 3600}, 'model_terminal_failed')
    assert worker.quality_failures == 3
    assert worker.transport_failures == 0
    assert worker.cooldown_until is None
    assert worker.pause_reason() == 'quality_circuit'
    assert worker.model_quality_totals['transport_failure'] == 0


@pytest.mark.parametrize('partial', [False, True])
def test_finalized_attempt_before_outcome_replays_scalar_retry_after_without_rebilling(corpus, monkeypatch, partial):
    from nannanews import bulletin_historical_attempts as attempts

    prepare(corpus)
    clock = FakeReviewClock(monkeypatch)
    monkeypatch.setattr(attempts, 'datetime', SimpleNamespace(now=lambda zone: clock.now))
    worker = full.FullReviewWorker(corpus.output, corpus.settings)
    posts = []
    def post(payload, **kwargs):
        posts.append(payload)
        error = transport_error(200 if partial else 429, 3600)
        if partial:
            error.code = 'model_response_invalid'
            error.model_transport_failure = True
        raise error
    monkeypatch.setattr(acquisition, '_model_post', post)
    finish = AttemptStore.finish
    def finish_then_crash(store, key, **kwargs):
        finish(store, key, **kwargs)
        raise KeyboardInterrupt('simulated crash after durable paid finalization')
    with monkeypatch.context() as patch:
        patch.setattr(AttemptStore, 'finish', finish_then_crash)
        with pytest.raises(KeyboardInterrupt, match='durable paid finalization'):
            worker.run()
    assert len(posts) == 1
    originals = {path: path.read_bytes() for path in (corpus.output / 'journal').glob('event-*.json')}
    assert all(full._load(path)['event'] != 'outcome' for path in originals)
    with closing(sqlite3.connect(worker.attempt_store.path)) as ledger:
        protected = ledger.execute('SELECT * FROM historical_attempts').fetchone()
    key = protected[0]
    clock.advance(700)
    resumed = full.FullReviewWorker(corpus.output, corpus.settings)
    assert resumed.quality_failures == 0
    assert resumed.transport_failures == 1
    assert resumed.status['cooldown_remaining_seconds'] == 2900
    metadata = resumed._attempt_failure_facts(key)
    assert metadata['model_retry_after_seconds'] == 3600
    assert metadata['model_http_status'] == (200 if partial else 429)
    assert metadata['model_transport_failure'] is partial
    assert not {'decision_json', 'raw_output_json', 'request_json'}.intersection(metadata)
    waits = clock.resume_waits(resumed, monkeypatch)
    def success(payload, **kwargs):
        assert payload != posts[0], 'Previously failed logical request was sent again'
        posts.append(payload)
        return uncertain_response(payload)
    monkeypatch.setattr(acquisition, '_model_post', success)
    result = resumed.run()
    assert sum(waits) == 2900
    assert len(posts) == result['counts']['model_request_attempts'] == 4
    assert result['counts']['request_blocked'] == 1
    with closing(sqlite3.connect(worker.attempt_store.path)) as ledger:
        assert ledger.execute('SELECT * FROM historical_attempts WHERE request_sha256=?', (key,)).fetchone() == protected
    assert all(path.read_bytes() == value for path, value in originals.items())


@pytest.mark.parametrize('decision', [
    {'_model_failure_code': 'model_http_error', '_model_http_status': '429',
     '_model_retry_after_seconds': '3600', '_model_transport_failure': 'true'},
    {'_model_failure_code': ['model_unavailable'], '_model_http_status': True,
     '_model_retry_after_seconds': -1, '_model_transport_failure': 1},
    None,
])
def test_replay_scalar_extraction_rejects_untyped_metadata(corpus, decision):
    prepare(corpus)
    worker = full.FullReviewWorker(corpus.output, corpus.settings)
    key = 'a' * 64
    worker.attempt_store.reserve(key, '{}')
    worker.attempt_store.finish(key, decision=decision, error='Unknown retained failure.')
    metadata = worker._attempt_failure_facts(key)
    assert metadata['model_http_status'] is None
    assert metadata['model_retry_after_seconds'] is None
    assert metadata['model_transport_failure'] is False
    assert not {'decision_json', 'raw_output_json', 'request_json'}.intersection(metadata)


def test_replay_scalar_extraction_guards_invalid_legacy_json(corpus):
    prepare(corpus)
    worker = full.FullReviewWorker(corpus.output, corpus.settings)
    key = 'b' * 64
    worker.attempt_store.reserve(key, '{}')
    worker.attempt_store.finish(key, decision={}, error='Unknown retained failure.')
    with closing(sqlite3.connect(worker.attempt_store.path)) as ledger, ledger:
        ledger.execute('UPDATE historical_attempts SET decision_json=? WHERE request_sha256=?', ('{broken', key))
    metadata = worker._attempt_failure_facts(key)
    assert metadata['state'] == 'failed'
    assert metadata['model_failure_code'] is None
    assert metadata['model_http_status'] is None
    assert metadata['model_retry_after_seconds'] is None
    assert metadata['model_transport_failure'] is False


@pytest.mark.parametrize('failed', [False, True])
def test_complete_outcome_replay_never_reads_decision_json(corpus, monkeypatch, failed):
    from contextlib import contextmanager

    prepare(corpus)
    FakeReviewClock(monkeypatch)
    worker = full.FullReviewWorker(corpus.output, corpus.settings)
    def post(payload, **kwargs):
        worker.stop.set()
        if failed:
            raise transport_error(429, 3600)
        return uncertain_response(payload)
    monkeypatch.setattr(acquisition, '_model_post', post)
    worker.run()
    queries = []
    readonly = full._readonly
    @contextmanager
    def traced(path):
        with readonly(path) as db:
            db.set_trace_callback(queries.append)
            yield db
    monkeypatch.setattr(full, '_readonly', traced)
    replayed = full.FullReviewWorker(corpus.output, corpus.settings)
    assert replayed.counters['model_request_attempts'] == 1
    assert replayed.transport_failures == (1 if failed else 0)
    assert not any('decision_json' in query for query in queries)
    key = next(iter(replayed.intents))
    assert set(replayed._attempt_metadata(key)) == {'state', 'finished_at', 'error'}
