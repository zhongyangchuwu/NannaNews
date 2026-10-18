"""Source-only recovery must not turn old paid or uncertain work into a retry."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from nannanews import acquisition
from nannanews import bulletin_date_repair as repair
from nannanews import bulletin_full_review as full
from nannanews.bulletin_historical_attempts import AttemptStore
from test_bulletin_full_review import corpus, database, uncertain_response
from test_bulletin_date_repair import _fetched_document, _model_response, reference_response, source_data_from_payload


def _unavailable(url):
    raise acquisition.AcquisitionError('The public article is unavailable.', 'source_blocked')


def _finished_source_gaps(corpus, monkeypatch):
    full.prepare_full_review(corpus.output, corpus.acceptance, corpus.settings)
    monkeypatch.setattr(acquisition, 'fetch_document', _unavailable)
    monkeypatch.setattr(acquisition, '_model_post', lambda *args, **kwargs: pytest.fail('A source gap was billed'))
    result = full.run_full_review(corpus.output, corpus.settings)
    assert result['counts']['blocked_no_primary'] == 4
    return result


def test_explicit_source_recovery_reopens_only_unpaid_gaps_and_retains_originals(corpus, monkeypatch, tmp_path):
    _finished_source_gaps(corpus, monkeypatch)
    originals = {path.relative_to(corpus.output): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in corpus.output.rglob('*') if path.is_file()}
    target = tmp_path / 'recovered'
    result = full.prepare_full_review(target, corpus.acceptance, corpus.settings,
        resume_from=corpus.output, recover_unpaid_sources=True)
    assert result['resume']['reopened_occurrences'] == 4
    worker = full.FullReviewWorker(target, corpus.settings)
    assert set(worker.done) == corpus.seed_ids
    assert worker.status['counts']['pending_unseen'] == 4
    fetched = []
    def still_unavailable(url):
        fetched.append(url)
        return _unavailable(url)
    monkeypatch.setattr(acquisition, 'fetch_document', still_unavailable)
    finished = worker.run()
    assert finished['counts']['blocked_no_primary'] == 4
    assert len(fetched) == 4
    full.run_full_review(target, corpus.settings)
    assert len(fetched) == 4
    assert all(hashlib.sha256((corpus.output / name).read_bytes()).hexdigest() == digest
               for name, digest in originals.items())
    with closing(sqlite3.connect(AttemptStore(corpus.settings.database_path).path)) as ledger:
        assert ledger.execute('SELECT count(*) FROM historical_attempts').fetchone()[0] == 0


def test_paid_failure_stays_complete_while_other_source_gaps_reopen(corpus, monkeypatch, tmp_path):
    full.prepare_full_review(corpus.output, corpus.acceptance, corpus.settings)
    unseen = next(row for row in corpus.rows if full._public_id(row) not in corpus.seed_ids)
    paid_url = repair._occurrence_source_urls(unseen)[0]
    def fetch(url):
        if url != paid_url:
            return _unavailable(url)
        value = _fetched_document()
        value.update(canonical_url=url, resolved_url=url)
        return value
    def failed_provider(*args, **kwargs):
        raise acquisition.AcquisitionError('Upstream request timeout.', 'model_http_error')
    monkeypatch.setattr(acquisition, 'fetch_document', fetch)
    monkeypatch.setattr(acquisition, '_model_post', failed_provider)
    previous = full.run_full_review(corpus.output, corpus.settings)
    assert previous['counts']['model_terminal_failed'] == 1
    target = tmp_path / 'no-paid-retry'
    result = full.prepare_full_review(target, corpus.acceptance, corpus.settings,
        resume_from=corpus.output, recover_unpaid_sources=True)
    assert result['resume']['reopened_occurrences'] == 3
    paid_id = full._public_id(unseen)
    worker = full.FullReviewWorker(target, corpus.settings)
    assert worker.done[paid_id] == 'model_terminal_failed'
    monkeypatch.setattr(acquisition, 'fetch_document', lambda url: pytest.fail('Old paid occurrence was fetched again')
                        if url == paid_url else _unavailable(url))
    monkeypatch.setattr(acquisition, '_model_post', lambda *args, **kwargs: pytest.fail('Old paid request was retried'))
    finished = worker.run()
    assert finished['counts']['model_request_attempts'] == 1
    assert finished['counts']['model_terminal_failed'] == 1


def test_uncertain_reservation_prevents_source_recovery_from_becoming_launchable(corpus, tmp_path):
    full.prepare_full_review(corpus.output, corpus.acceptance, corpus.settings)
    request = json.dumps({'rules_version': repair.RULES_VERSION, 'model_name': 'gpt-6-luna',
        'publications': [], 'date_claims': [], 'primary_sources': [], 'source_failures': []}, sort_keys=True)
    store = AttemptStore(corpus.settings.database_path)
    store.reserve(hashlib.sha256(request.encode()).hexdigest(), request)
    target = tmp_path / 'uncertain-recovery'
    with pytest.raises(ValueError, match='reservations'):
        full.prepare_full_review(target, corpus.acceptance, corpus.settings,
            resume_from=corpus.output, recover_unpaid_sources=True)
    assert not (target / 'scope.json').exists()
    with closing(sqlite3.connect(store.path)) as ledger:
        assert ledger.execute("SELECT state FROM historical_attempts").fetchone()[0] == 'reserved'


def test_changed_resume_manifest_is_rejected_before_model_work(corpus, monkeypatch, tmp_path):
    _finished_source_gaps(corpus, monkeypatch)
    target = tmp_path / 'changed-manifest'
    full.prepare_full_review(target, corpus.acceptance, corpus.settings,
        resume_from=corpus.output, recover_unpaid_sources=True)
    manifest = target / 'resume-manifest.json'
    manifest.write_bytes(manifest.read_bytes() + b'\n')
    with pytest.raises(ValueError, match='manifest changed'):
        full.run_full_review(target, corpus.settings)


def _progress_records(output):
    return [(path, [json.loads(line) for line in path.read_text().splitlines()])
            for path in sorted(output.glob('batches/*/review-progress*.jsonl'))]


def _replace_progress(path, values):
    path.write_text(''.join(json.dumps(value) + '\n' for value in values), encoding='utf-8')


def test_finalized_paid_intent_without_review_is_never_resubmitted_after_source_changes(corpus, monkeypatch, tmp_path):
    full.prepare_full_review(corpus.output, corpus.acceptance, corpus.settings)
    unseen = next(row for row in corpus.rows if full._public_id(row) not in corpus.seed_ids)
    protected_id = full._public_id(unseen)
    protected_url = repair._occurrence_source_urls(unseen)[0]
    def fetched(url):
        document = _fetched_document()
        document.update(canonical_url=url, resolved_url=url)
        return document
    monkeypatch.setattr(acquisition, 'fetch_document', fetched)
    monkeypatch.setattr(acquisition, '_model_post', lambda payload, audit=None: uncertain_response(payload))
    append = repair._append_private_line
    def crash_after_paid_finalization(path, value):
        if path.name == 'reviews.jsonl':
            raise OSError('simulated process loss after paid ledger finalization')
        return append(path, value)
    monkeypatch.setattr(repair, '_append_private_line', crash_after_paid_finalization)
    with pytest.raises(OSError, match='paid ledger finalization'):
        full.run_full_review(corpus.output, corpus.settings)
    monkeypatch.setattr(repair, '_append_private_line', append)
    store = AttemptStore(corpus.settings.database_path)
    with closing(sqlite3.connect(store.path)) as ledger:
        assert ledger.execute('SELECT state FROM historical_attempts').fetchone()[0] == 'completed'
    target = tmp_path / 'protected-crash-gap'
    full.prepare_full_review(target, corpus.acceptance, corpus.settings,
        resume_from=corpus.output, recover_unpaid_sources=True)
    cache_path = target / 'source-cache' / (hashlib.sha256(protected_url.encode()).hexdigest() + '.json')
    cache_path.unlink(missing_ok=True)
    def changed_source(url):
        if url != protected_url:
            return _unavailable(url)
        document = _fetched_document(text='Materially changed complete public source after the previous paid request.')
        document.update(canonical_url=url, resolved_url=url)
        return document
    monkeypatch.setattr(acquisition, 'fetch_document', changed_source)
    monkeypatch.setattr(acquisition, '_model_post', lambda *args, **kwargs: pytest.fail('Finalized paid intent was billed again'))
    worker = full.FullReviewWorker(target, corpus.settings)
    assert worker.done[protected_id] == 'request_blocked'
    assert worker.status['protected_prior_attempts_without_result'] == 1
    worker.run()
    restarted = full.FullReviewWorker(target, corpus.settings)
    assert restarted.done[protected_id] == 'request_blocked'
    restarted._publish(current_occurrence=protected_id)
    with pytest.raises(repair.ReviewDeferred) as blocked:
        restarted.request_gate('a-different-logical-request-key', {'sources': []})
    assert blocked.value.code == 'prior_attempt_protected'
    with closing(sqlite3.connect(store.path)) as ledger:
        assert ledger.execute('SELECT count(*) FROM historical_attempts').fetchone()[0] == 1


@pytest.mark.parametrize('reference', ['capture', 'journal'])
def test_parent_traversal_recovery_never_changes_external_bytes_or_modes(corpus, monkeypatch, tmp_path, reference):
    _finished_source_gaps(corpus, monkeypatch)
    progress, values = _progress_records(corpus.output)[0]
    source = Path(values[0]['capture_artifact'])
    external = tmp_path / 'external'
    external.mkdir(mode=0o750)
    capture = external / 'capture.json'
    capture.write_bytes(source.read_bytes())
    capture.chmod(0o640)
    external_mode, capture_mode, raw = external.stat().st_mode, capture.stat().st_mode, capture.read_bytes()
    escaping = str(corpus.output / '..' / 'external' / 'capture.json')
    if reference == 'capture':
        values[0]['capture_artifact'] = escaping
        _replace_progress(progress, values)
    else:
        journal = next(path for path in sorted((corpus.output / 'journal').glob('event-*.json'))
                       if json.loads(path.read_text()).get('event') == 'outcome')
        value = json.loads(journal.read_text())
        value['output'] = escaping
        journal.write_text(json.dumps(value), encoding='utf-8')
    target = tmp_path / ('traversal-' + reference)
    with pytest.raises(ValueError, match='parent directories'):
        full.prepare_full_review(target, corpus.acceptance, corpus.settings,
            resume_from=corpus.output, recover_unpaid_sources=True)
    assert not (target / 'scope.json').exists()
    assert external.stat().st_mode == external_mode
    assert capture.stat().st_mode == capture_mode
    assert capture.read_bytes() == raw
    if reference == 'capture':
        with pytest.raises(ValueError, match='parent directories'):
            full.FullReviewWorker(corpus.output, corpus.settings)


def test_progress_cannot_borrow_another_occurrences_valid_capture(corpus, monkeypatch, tmp_path):
    _finished_source_gaps(corpus, monkeypatch)
    progress, values = _progress_records(corpus.output)[0]
    assert values[0]['occurrence_id'] != values[1]['occurrence_id']
    values[0]['capture_artifact'] = values[1]['capture_artifact']
    _replace_progress(progress, values)
    target = tmp_path / 'misbound-capture'
    with pytest.raises(ValueError, match='different occurrence'):
        full.prepare_full_review(target, corpus.acceptance, corpus.settings,
            resume_from=corpus.output, recover_unpaid_sources=True)
    assert not (target / 'scope.json').exists()
    with pytest.raises(ValueError, match='different occurrence'):
        full.FullReviewWorker(corpus.output, corpus.settings)


def _legacy_blocked_duplicate(corpus, monkeypatch):
    # Same activity, same input, a second publication: older workers retained a
    # nonterminal capture instead of binding the already-paid complete verdict.
    first, second = [row for row in corpus.rows if full._public_id(row) not in corpus.seed_ids][:2]
    with closing(sqlite3.connect(corpus.settings.database_path)) as db, db:
        db.execute('UPDATE bulletin_occurrences SET item_id=? WHERE id=?', (first['item_id'], second['id']))
        db.execute('UPDATE bulletin_sources SET source_url=(SELECT source_url FROM bulletin_sources WHERE occurrence_id=?) WHERE occurrence_id=?',
            (first['id'], second['id']))
    full.prepare_full_review(corpus.output, corpus.acceptance, corpus.settings)
    def post(payload, **kwargs):
        data = source_data_from_payload(payload)
        verdict = _model_response(url=data['linked_primary_sources'][0]['url'])
        return {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': json.dumps(reference_response(verdict, data))}]}]}
    monkeypatch.setattr(acquisition, '_model_post', post)
    full.run_full_review(corpus.output, corpus.settings)
    event_path, event = next((path, full._load(path)) for path in sorted((corpus.output / 'journal').glob('event-*.json'))
        if full._load(path).get('event') == 'outcome' and not full._load(path).get('new_request'))
    identity = event['occurrence_id']
    artifact = Path(event['output'])
    values = list(full._lines(artifact))
    record = next(value for value in values if value['occurrence_id'] == identity)
    # Construct the old persisted contract, not an alternate implementation of
    # the obsolete policy. Recovery must consume these historical files intact.
    artifact.write_text(''.join(json.dumps(value) + '\n' for value in values if value['occurrence_id'] != identity))
    event_path.unlink()
    paid_event = next(full._load(path) for path in sorted((corpus.output / 'journal').glob('event-*.json'))
        if full._load(path).get('event') == 'intent' and full._load(path)['occurrence_id'] == next(
            value['occurrence_id'] for value in values if value['local_key'] == first['local_key']))
    with closing(sqlite3.connect(AttemptStore(corpus.settings.database_path).path)) as ledger, ledger:
        key = paid_event['request_sha256']
        old = json.loads(ledger.execute('SELECT decision_json FROM historical_attempts WHERE request_sha256=?', (key,)).fetchone()[0])
        old.pop('_model_audit')
        ledger.execute('UPDATE historical_attempts SET decision_json=? WHERE request_sha256=?', (json.dumps(old), key))
    storage = full.ReviewStorage(artifact, artifact.parent, response_bytes=acquisition.MAX_MODEL_BODY)
    progress = storage.compact_progress(record, progress='equivalent_request_not_reusable',
        request_state='completed', request_sha256=paid_event['request_sha256'])
    repair._append_private_line(storage.progress_path(progress), progress)
    return identity, artifact, storage.progress_path(progress)


@pytest.mark.parametrize('interrupt_after_append', [False, True])
def test_free_recovery_rebinds_old_progress_without_network_or_rewriting_evidence(corpus, monkeypatch, interrupt_after_append):
    identity, artifact, progress = _legacy_blocked_duplicate(corpus, monkeypatch)
    main_before = corpus.settings.database_path.read_bytes()
    old_receipts, old_progress = artifact.read_bytes(), progress.read_bytes()
    with closing(sqlite3.connect(AttemptStore(corpus.settings.database_path).path)) as ledger:
        attempts_before = list(ledger.execute('SELECT * FROM historical_attempts ORDER BY request_sha256'))
    monkeypatch.setattr(acquisition, 'fetch_document', lambda *args, **kwargs: pytest.fail('free recovery fetched source'))
    monkeypatch.setattr(acquisition, '_model_post', lambda *args, **kwargs: pytest.fail('free recovery sent model request'))
    if interrupt_after_append:
        journal = full.FullReviewWorker._journal
        def interrupted(self, event, **details):
            if event == 'outcome':
                raise OSError('crash after free receipt append')
            return journal(self, event, **details)
        with monkeypatch.context() as patch:
            patch.setattr(full.FullReviewWorker, '_journal', interrupted)
            with pytest.raises(OSError, match='crash after free receipt append'):
                full.reuse_full_reviews(corpus.output, corpus.settings)
    result = full.reuse_full_reviews(corpus.output, corpus.settings)
    assert result['new_model_requests'] == 0
    assert result['counts'].get('request_blocked', 0) == 0
    assert result['counts']['primary_confirmed'] == 4
    assert result['request_resolution']['reused_primary_confirmed'] == 1
    assert result['activity_coverage']['primary_confirmed'] == 3
    assert artifact.read_bytes().startswith(old_receipts)
    assert progress.read_bytes() == old_progress
    receipts = [value for path in corpus.output.glob('batches/*/reviews.jsonl') for value in full._lines(path)]
    assert sum(value['occurrence_id'] == identity for value in receipts) == 1
    full.reuse_full_reviews(corpus.output, corpus.settings)
    assert corpus.settings.database_path.read_bytes() == main_before
    with closing(sqlite3.connect(AttemptStore(corpus.settings.database_path).path)) as ledger:
        assert list(ledger.execute('SELECT * FROM historical_attempts ORDER BY request_sha256')) == attempts_before
