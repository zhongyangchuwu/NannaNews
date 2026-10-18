"""Immutable full-review continuation; only previously unpaid source gaps may reopen."""
from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3


FORMAT = 'nanna-full-review-resume-v2'


def _checked_record(value, scope, bindings):
    binding = bindings.get(value.get('occurrence_id'))
    if (binding is None or any(value.get(key) != binding[key]
            for key in ('input_sha256', 'related_context_sha256'))
            or value.get('rules_version') != scope['configuration']['rules_version']
            or value.get('reviewer') != 'luna:' + scope['configuration']['model']):
        raise ValueError('Previous result no longer binds the current frozen occurrence.')
    return binding['occurrence_id']


def _paid_occurrences(database, *, require_drained):
    from nannanews.bulletin_historical_attempts import ledger_path
    from nannanews import bulletin_full_review as full

    path = ledger_path(database)
    if not path.exists():
        return set()
    path = full._file(path, 2 ** 63 - 1).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)) as db:
        db.execute('PRAGMA query_only=ON')
        if require_drained and db.execute(
                "SELECT count(*) FROM historical_attempts WHERE state='reserved'").fetchone()[0]:
            raise ValueError('Uncertain paid reservations must remain protected; source recovery requires a drained ledger.')
        return {row[0] for row in db.execute('SELECT DISTINCT occurrence_id FROM historical_outcomes')}


def _copy_private(source, destination, copies):
    from nannanews import bulletin_full_review as full

    source = full._file(source, 2 ** 63 - 1)
    destination = full._safe_path(destination)
    full._directory(destination.parent)
    if destination.exists():
        with source.open('rb') as incoming, full._file(destination, 2 ** 63 - 1).open('rb') as existing:
            matches = hashlib.file_digest(incoming, 'sha256').digest() == hashlib.file_digest(existing, 'sha256').digest()
        if not matches:
            raise ValueError('Two previous artifacts conflict at the same continuation path.')
        return
    with source.open('rb') as incoming, destination.open('xb') as outgoing:
        os.fchmod(outgoing.fileno(), 0o600)
        shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    copies.append({'source': str(source), 'destination': str(destination),
                   'bytes': destination.stat().st_size})


def _write_progress(path, values):
    from nannanews import bulletin_full_review as full

    full._directory(path.parent)
    with path.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        for value in values:
            stream.write((json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False) + '\n').encode())
        stream.flush()
        os.fsync(stream.fileno())


def resume_previous_output(previous_output, output, scope, rows, originals, settings,
                           *, recover_unpaid_sources=False):
    """Copy admitted evidence into a new scope without model, network or MAIN writes.

    The caller writes scope.json only after this function succeeds. An interrupted
    clone consequently cannot be launched as an apparently prepared paid worker.
    """
    from nannanews import acquisition
    from nannanews import bulletin_date_repair as repair
    from nannanews import bulletin_full_review as full

    previous = full._directory(previous_output, create=False)
    output = full._directory(output, create=False)
    if output == previous or output.is_relative_to(previous):
        raise ValueError('Continuation output must be independent of the previous scope.')
    with full.worker_lock(settings.database_path):
        old_scope_path = full._file(previous / 'scope.json')
        old_scope = full._load(old_scope_path)
        status = full._load(previous / 'status.json', 1024 * 1024)
        if status.get('phase') not in full.TERMINAL_PHASES | {'prepared'}:
            raise ValueError('Stop the previous worker before preparing its immutable continuation.')
        if (old_scope.get('format') != full.FORMAT
                or any(old_scope.get(key) != scope[key]
                       for key in ('database_path', 'fingerprint', 'signature', 'occurrence_count'))
                or any(old_scope['configuration'].get(key) != scope['configuration'][key]
                       for key in ('model', 'protocol', 'rules_version'))):
            raise ValueError('Previous scope binds different input, database, model or rules.')
        old_seeds = {value['occurrence_id']: value['record_sha256'] for value in old_scope['seeds']}
        if old_seeds != {value['occurrence_id']: value['record_sha256'] for value in scope['seeds']}:
            raise ValueError('Continuation must retain the same admitted sample receipts.')
        row_by_id = {full._public_id(row): row for row in rows}
        bindings = {value['occurrence_id']: value for value in scope['occurrences']}
        protected = _paid_occurrences(settings.database_path, require_drained=recover_unpaid_sources)
        protected.update(old_seeds)
        artifacts, progress_files, categories = [], [], {}
        for directory in sorted((previous / 'batches').glob('batch-*')):
            full._directory(directory, create=False)
            artifact = directory / 'reviews.jsonl'
            if artifact.exists():
                repair._review_decisions((artifact,), rows, originals)
                for value in full._lines(artifact):
                    protected.add(_checked_record(value, scope, bindings))
                artifacts.append(artifact)
            for path in sorted(directory.glob('review-progress*.jsonl')):
                values = list(full._lines(path))
                for value in values:
                    identity = _checked_record(value, scope, bindings)
                    capture = value
                    if value.get('capture_artifact'):
                        capture_path = full._safe_path(value['capture_artifact'])
                        if not capture_path.is_relative_to(previous):
                            raise ValueError('Previous progress capture escapes its private scope.')
                        capture = full._load(capture_path, full.CACHE_RECORD_BYTES)
                        if _checked_record(capture, scope, bindings) != identity:
                            raise ValueError('Previous progress references a different occurrence capture.')
                    code = value.get('defer_code')
                    if code not in full.OPERATIONAL_PAUSES:
                        categories[identity] = full._category(capture, False, value.get('progress'), code)
                progress_files.append((path, values))
        journals = []
        for path in sorted((previous / 'journal').glob('event-*.json')):
            event = full._load(path, 65536)
            if event.get('output'):
                full._safe_path(event['output'])
            if event.get('event') == 'intent':
                identity = event.get('occurrence_id')
                if identity not in row_by_id:
                    raise ValueError('Previous paid intent has an unknown occurrence.')
                protected.add(identity)
            journals.append((path, event))
        reopened = {identity for identity, category in categories.items()
                    if recover_unpaid_sources and category == 'blocked_no_primary'
                    and identity not in protected
                    and repair._occurrence_source_urls(row_by_id[identity])}
        copies, rebased, omitted = [], 0, 0
        for path in artifacts:
            _copy_private(path, output / path.relative_to(previous), copies)
        for path, values in progress_files:
            kept = []
            for original in values:
                if original['occurrence_id'] in reopened:
                    omitted += 1
                    continue
                value = dict(original)
                if value.get('capture_artifact'):
                    source = full._safe_path(value['capture_artifact'])
                    target = output / source.relative_to(previous)
                    _copy_private(source, target, copies)
                    value['capture_artifact'] = str(target)
                    rebased += 1
                kept.append(value)
            if kept:
                _write_progress(output / path.relative_to(previous), kept)
        journal_count = 0
        full._directory(output / 'journal')
        for path, original in journals:
            if original.get('event') == 'outcome' and original.get('occurrence_id') in reopened:
                if original.get('new_request'):
                    raise ValueError('A paid outcome cannot be reopened for source recovery.')
                continue
            event = dict(original)
            if event.get('output'):
                source = full._safe_path(event['output'])
                if source.is_relative_to(previous):
                    target = output / source.relative_to(previous)
                    if not target.exists():
                        raise ValueError('Retained journal outcome has no copied evidence.')
                    event['output'] = str(target)
            full._atomic(output / path.relative_to(previous), event)
            journal_count += 1
        cache = full.SourceCache(full._directory(output / 'source-cache'), settings.database_path,
                                 hot_bytes=0)
        cached, dropped_failures, reparsed, skipped_incomplete = 0, 0, 0, 0
        for path in sorted((previous / 'source-cache').glob('*.json')):
            envelope = full._load(path, full.CACHE_RECORD_BYTES)
            url, capture = envelope.get('url'), envelope.get('capture')
            if envelope.get('sha256') != full._digest(capture):
                raise ValueError('Previous source cache integrity changed; refusing recovery.')
            full.SourceCache._validate(url, capture)
            if capture.get('error'):
                if recover_unpaid_sources:
                    dropped_failures += 1
                    continue
            else:
                document = capture['document']
                if document.get('source_capture_version') != acquisition.SOURCE_CAPTURE_VERSION:
                    try:
                        document = acquisition.reparse_source_document(document)
                    except acquisition.AcquisitionError:
                        skipped_incomplete += 1
                        continue
                    capture = {'document': document, 'media': capture.get('media', []),
                               'media_error': capture.get('media_error', '')}
                    reparsed += 1
                try:
                    from nannanews.source_media import apply_remote_media_policy
                    document, retained = apply_remote_media_policy(capture['document'], capture.get('media', []))
                    capture = dict(capture, document=document, media=retained)
                    full.SourceCache._validate(url, capture, reusable=True)
                except ValueError:
                    skipped_incomplete += 1
                    continue
            cache[url] = capture
            cached += 1
        protected_ids = protected.intersection(row_by_id)
        manifest = {'format': FORMAT, 'created_at': full._stamp(), 'run_id': scope['run_id'],
            'output': str(output), 'previous_output': str(previous),
            'previous_scope_sha256': hashlib.sha256(old_scope_path.read_bytes()).hexdigest(),
            'previous_configuration': old_scope['configuration'],
            'recover_unpaid_sources': recover_unpaid_sources,
            'protected_occurrences': len(protected_ids),
            'protected_occurrence_ids': sorted(protected_ids), 'reopened_occurrence_ids': sorted(reopened),
            'omitted_unpaid_progress_records': omitted, 'rebased_progress_records': rebased,
            'retained_journal_events': journal_count, 'source_cache_entries': cached,
            'retired_negative_cache_entries': dropped_failures,
            'reparsed_source_captures': reparsed, 'unseeded_incomplete_captures': skipped_incomplete,
            'copied_evidence': copies, 'automatic_live_apply': False,
            'paid_request_history_reset': False, 'original_artifacts_changed': False}
        manifest_path = output / 'resume-manifest.json'
        full._atomic(manifest_path, manifest)
        return {'manifest': manifest_path.name,
                'sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                'reopened_occurrences': len(reopened), 'previous_output': str(previous)}


def validate_resume_manifest(output, scope):
    from nannanews import bulletin_full_review as full

    descriptor = scope.get('resume')
    if descriptor is None:
        return frozenset()
    if (not isinstance(descriptor, dict) or descriptor.get('manifest') != 'resume-manifest.json'
            or not isinstance(descriptor.get('sha256'), str)):
        raise ValueError('Invalid frozen continuation descriptor.')
    path = full._file(output / descriptor['manifest'])
    if hashlib.sha256(path.read_bytes()).hexdigest() != descriptor['sha256']:
        raise ValueError('Frozen continuation manifest changed.')
    manifest = full._load(path)
    if (manifest.get('format') != FORMAT or manifest.get('run_id') != scope['run_id']
            or manifest.get('output') != str(output)
            or len(manifest.get('reopened_occurrence_ids', [])) != descriptor.get('reopened_occurrences')
            or manifest.get('paid_request_history_reset') is not False
            or manifest.get('original_artifacts_changed') is not False
            or manifest.get('automatic_live_apply') is not False):
        raise ValueError('Continuation manifest no longer binds its frozen scope.')
    protected = manifest.get('protected_occurrence_ids')
    known = {value['occurrence_id'] for value in scope['occurrences']}
    if (not isinstance(protected, list)
            or any(not isinstance(identity, str) or identity not in known for identity in protected)
            or len(protected) != len(set(protected))
            or len(protected) != manifest.get('protected_occurrences')
            or set(protected).intersection(manifest.get('reopened_occurrence_ids', []))):
        raise ValueError('Continuation request protection no longer binds the frozen occurrences.')
    return frozenset(protected)
