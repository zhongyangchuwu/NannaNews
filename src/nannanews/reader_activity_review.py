"""Explicit, local-only review of one stable private registration.

The registry remains the editable truth. This operation collects evidence and
associates exact normalized review IDs; it never saves, approves or publishes
activity facts and never archives to a cloud repository.
"""
from collections import Counter
from contextlib import closing
from pathlib import Path

from nannanews import acquisition, bulletin_date_repair as repair
from nannanews.bulletin_historical_attempts import AttemptStore, ReviewStorage
from nannanews.data_foundation import _no_symlinks, _open_existing, connect_foundation
from nannanews.data_foundation_extract import extract_paths
from nannanews.reader_reviews import ReviewValidationError, _actor


_REFS = ('activity_id', 'item_id', 'occurrence_id', 'source_id')


def _state(decision):
    if decision.get('_request_state') or decision.get('_review_deferred'):
        return 'admission_refused'
    if decision.get('_paid_failure'):
        return 'model_failed'
    if decision.get('_preparation_failed') or decision.get('_storage_blocked'):
        return 'capture_failed' if decision['source_review'].get('attempts') else 'preparation_failed'
    return 'reviewed'


def _review_ids(root, paths):
    identities = set()
    with closing(_open_existing(root)) as db:
        for path in paths:
            identities.update(row[0] for row in db.execute(
                "SELECT row_id FROM ingestion_outputs WHERE source_path=? AND kind='review'",
                (str(path),)))
    return sorted(identities)


def review_registered_activity(*, store, item_id, actor, allow_fetch, allow_paid, foundation_root):
    """Acquire and review exactly one registered item with explicit authorization.

    Source URLs and all four identity references come from ReviewStore, never
    caller-provided source text or title/date matching. Paid refusal and failure
    are explicit result states, not permission to retry. Original source captures
    and actual ModelAudit files remain private on every reachable failure path.
    """
    if allow_fetch is not True or allow_paid is not True:
        raise ReviewValidationError('必须分别明确授权公开来源读取和一次付费模型审阅。')
    actor = _actor(actor)
    selected = store.get_item(item_id)
    registration = selected.get('registration')
    if (not isinstance(registration, dict)
            or any(not isinstance(registration.get(key), str) or not registration[key] for key in _REFS)
            or registration['item_id'] != item_id or registration['activity_id'] != item_id):
        raise ReviewValidationError('仅可审阅已有完整稳定身份的私有登记活动。')
    refs = {key: registration[key] for key in _REFS}
    payload = (selected['head']['payload'] if selected.get('head') else selected['initial_payload'])
    activity = payload['activity']
    urls = list(dict.fromkeys(acquisition.canonical_url(url) for url in activity['source_urls']))
    if not urls:
        raise ReviewValidationError('登记活动没有明确的公开来源链接；未读取来源或调用模型。')

    root = Path(foundation_root).expanduser().absolute()
    if root != store.evidence().root():
        raise ReviewValidationError('核查必须使用配置的本地资料库。')
    with closing(_open_existing(root)):
        pass
    # Configuration validation is local and precedes source collection.
    acquisition._model_config()

    output = root / 'activity-review'
    _no_symlinks(output)
    output.mkdir(mode=0o700, exist_ok=True)
    output.chmod(0o700)
    storage = ReviewStorage(output / 'review.jsonl', output, response_bytes=acquisition.MAX_MODEL_BODY)
    attempts = AttemptStore(store.settings.database_path)
    cache, counters, capture_paths = {}, Counter(), {}

    def preserve_captures():
        for url, capture in cache.items():
            if url not in capture_paths:
                capture_paths[url] = Path(storage.preserve({'url': url, 'capture': capture})).absolute()

    def admit(source_review):
        # Persist complete originals (including raw image/PDF/failure documents)
        # before the canonical attempt reservation permits any paid transport.
        preserve_captures()
        storage.admit(source_review, refs)

    def gate(request_key, source_review):
        if source_review['attempts'] or source_review['status'] == 'source_mismatch':
            raise repair.ReviewDeferred('source_capture_failed',
                'Registered public sources were not captured completely; originals and failures are retained, and no paid request was sent.')

    occurrence = dict(refs, title=activity['title'], body_text=activity['content'],
                      source_urls=urls, issue_date=None, kind='standalone',
                      source_sha256=None, local_key=refs['occurrence_id'],
                      related_context_sha256=None)
    original_claims = [{'kind': entry['kind'], 'raw_text': entry['date'],
        'normalized_date': entry['date'], 'resolution': 'explicit',
        'evidence': 'Explicit date in the private activity registration; not primary-source verification.',
        'locator': None} for entry in activity['dates']]
    try:
        decision = repair.model_decision(occurrence, original_claims, source_cache=cache,
            attempt_store=attempts, counters=counters, storage_check=admit,
            request_gate=gate, audit_context=dict(refs, actor=actor),
            reuse_completed=False)
    finally:
        # Media/preflight failures can return before the paid-admission callback.
        preserve_captures()

    state = _state(decision)
    if decision.get('_review_deferred') == 'source_capture_failed':
        state = 'capture_failed'
    record = dict(refs, **{key: decision[key] for key in
        ('status', 'reason', 'rules_version', 'origin', 'reviewer', 'claims', 'source_review')})
    for key in ('_model_audit', '_model_failure_code', '_paid_failure', '_receipt_finalize_failed'):
        if key in decision:
            record[key] = decision[key]
    record['workflow'] = {'state': state, 'actor': actor,
        'request_state': decision.get('_request_state'),
        'request_key': decision.get('_request_pending'),
        'admission_code': decision.get('_review_deferred')}
    if state != 'reviewed':
        record['error'] = {'code': decision.get('_model_failure_code') or
            decision.get('_review_deferred') or state, 'reason': decision['reason']}
    result_path = Path(storage.preserve(record)).absolute()
    receipt_paths = []
    if isinstance(decision.get('_model_audit'), dict):
        receipt_paths.append(Path(decision['_model_audit']['receipt']).absolute())
    paths = list(capture_paths.values()) + [result_path] + receipt_paths
    # Never rebuild sent inputs from source snapshots or trigger autoarchiving.
    # Link only exact outputs committed by this run.
    extract_paths(root, paths)
    review_ids = _review_ids(root, [result_path] + receipt_paths)
    if not review_ids:
        raise RuntimeError('Local review evidence was retained but no exact review ID was normalized; no new request is authorized.')
    with closing(connect_foundation(root)) as db, db:
        for path in capture_paths.values():
            for snapshot in db.execute(
                    "SELECT row_id FROM ingestion_outputs WHERE source_path=? AND kind='snapshot'",
                    (str(path),)):
                db.executemany('INSERT OR IGNORE INTO review_snapshots(review_id,snapshot_id) VALUES(?,?)',
                               ((review_id, snapshot[0]) for review_id in review_ids))
    for review_id in review_ids:
        store.associate_review(item_id, review_id, actor=actor)
    return {'state': state, 'item_id': item_id,
        'review_ids': review_ids, 'receipt_review_ids': _review_ids(root, receipt_paths),
        'result_review_ids': _review_ids(root, [result_path]),
        'new_request': counters['model_request_attempts'] == 1,
        'decision_status': decision['status'], 'reason': decision['reason']}
