"""Preserve original offline SQLite captures; never archive, recrawl or clean inputs.

The held ingestion is an evidence inventory, not permission to remove a native DB.
Capture manifests distinguish the database's declared digest from the SHA-256 of
its exact stored UTF-8 document JSON and of every decodable retained body.
"""
from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import json
import mmap
from pathlib import Path
import sqlite3
import stat
import tempfile

from .data_foundation import connect_foundation
from .data_foundation_blobs import BlobStore
from .data_foundation_extract import (
    MAX_RECORD_BYTES, MAX_SEMANTIC_BYTES, HeldInput, _Extractor, _decode_media,
    _fingerprint, _json, _semantic, _signature,
)


_CHUNK = 1024 * 1024
_MAX_ERRORS = 100


def _guard(info):
    return dict(zip(('device', 'inode', 'size', 'mtime_ns', 'ctime_ns'), _signature(info)))


def _reject_constant(value):
    raise ValueError('Non-finite JSON constant.')


def _sidecars(path):
    # immutable=1 avoids SQLite creating locks/SHM, but ignores WAL. Refuse a
    # nonempty transaction sidecar rather than silently reading an older DB.
    state = {}
    for suffix in ('-wal', '-journal', '-shm'):
        candidate = path.with_name(path.name + suffix)
        if candidate.exists() or candidate.is_symlink():
            digest, info = _fingerprint(candidate)
            state[suffix] = {'sha256': digest, **_guard(info)}
    return state


def _problem(code, *, ordinal=None):
    value = {'code': code}
    if ordinal is not None:
        value['ordinal'] = ordinal
    return value


def _original(source, row, extractor):
    """Read at most one bounded document; spool oversized evidence, not its JSON."""
    if row['storage_type'] not in {'text', 'blob'}:
        return None, None, [_problem('document_not_text')], None
    with source.blobopen('source_documents', 'document_json', row['capture_rowid'], readonly=True) as body:
        size = len(body)
        if size <= MAX_RECORD_BYTES:
            raw = body.read()
            digest = extractor.blob(raw, mime='application/json; charset=utf-8', kind='capture_document')
            return digest, raw, [], size
        # BlobStore already owns atomic SHA-addressed staging. mmap lets its
        # byte API consume a streamed oversized record without a Python-sized
        # allocation or a second permanent storage format.
        with tempfile.TemporaryFile() as spool:
            while chunk := body.read(_CHUNK):
                spool.write(chunk)
            spool.flush()
            with mmap.mmap(spool.fileno(), 0, access=mmap.ACCESS_READ) as raw:
                digest = extractor.blob(raw, mime='application/json; charset=utf-8', kind='capture_document')
        return digest, None, [_problem('document_oversize')], size


def _body(extractor, encoded, expected, *, mime, kind):
    if not isinstance(encoded, str):
        return None, False, 'body_base64_missing'
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError:
        return None, False, 'body_base64_malformed'
    digest = extractor.blob(data, mime=mime, kind=kind)
    valid = isinstance(expected, str) and expected.lower() == digest
    return digest, valid, None if valid else 'body_digest_mismatch'


def _capture(source, row, extractor, origin):
    original_sha, raw, errors, size = _original(source, row, extractor)
    manifest = {
        'format': 'nannanews-original-capture-v1',
        'source_database': origin,
        'source_row': {'rowid': row['capture_rowid'], 'id': row['id'],
                       'url': row['url'], 'declared_digest': row['digest']},
        'original_document_blob_sha': original_sha,
        'original_document_size': size,
        'raw_http': None, 'media': [], 'snapshot_id': None, 'errors': errors,
    }
    document = None
    if raw is not None:
        try:
            document = json.loads(raw.decode('utf-8'), parse_constant=_reject_constant)
            if not isinstance(document, dict):
                errors.append(_problem('document_not_object'))
                document = None
        except (ValueError, UnicodeError, RecursionError):
            errors.append(_problem('document_json_malformed'))
    valid_media, ordinals = [], []
    links = [('original_document', 0, original_sha)] if original_sha else []
    if document is not None:
        if 'raw_response_base64' in document or 'raw_response_digest' in document:
            sha, valid, code = _body(
                extractor, document.get('raw_response_base64'), document.get('raw_response_digest'),
                mime=str(document.get('content_type') or 'application/octet-stream'), kind='capture_raw_http')
            manifest['raw_http'] = {'blob_sha': sha, 'declared_digest': document.get('raw_response_digest'),
                                    'digest_valid': valid}
            if sha:
                links.append(('raw_http', 0, sha))
            if code:
                errors.append(_problem('raw_http_' + code))
        media = document.get('media', [])
        if not isinstance(media, list):
            errors.append(_problem('media_not_list'))
            media = []
        for ordinal, image in enumerate(media):
            entry = {'ordinal': ordinal, 'blob_sha': None, 'image': False, 'digest_valid': False}
            manifest['media'].append(entry)
            if not isinstance(image, dict):
                errors.append(_problem('media_not_object', ordinal=ordinal))
                continue
            mime = str(image.get('mime') or 'application/octet-stream')
            try:
                data, image_mime = _decode_media(image)
            except HeldInput:
                sha, valid, code = _body(extractor, image.get('data'), image.get('digest'),
                                         mime=mime, kind='capture_media')
                if code:
                    errors.append(_problem('media_' + code, ordinal=ordinal))
                elif valid:
                    errors.append(_problem('media_not_valid_image', ordinal=ordinal))
            else:
                sha = extractor.blob(data, mime=image_mime, kind='source_image')
                del data
                valid = True
                entry['image'] = True
                valid_media.append(image)
                ordinals.append(ordinal)
            entry.update(blob_sha=sha, digest_valid=valid, declared_digest=image.get('digest'),
                         mime=mime, url=image.get('url'))
            if sha and not entry['image']:
                links.append(('failed_media', ordinal, sha))
        # URL strings in images are references, not captured image bytes. Never
        # fall back to them (or to unvalidated embedded image dictionaries).
        readable = dict(document, images=[])
        if not any(readable.get(key) for key in ('url', 'canonical_url', 'resolved_url')):
            readable['url'] = row['url']
        try:
            snapshot_id, rejection = extractor.snapshot(readable, valid_media, media_ordinals=ordinals)
        except (HeldInput, TypeError, KeyError, UnicodeError, RecursionError):
            errors.append(_problem('snapshot_rejected'))
            snapshot_id = None
        else:
            if rejection:
                errors.append(_problem('snapshot_rejected'))
        manifest['snapshot_id'] = snapshot_id
        if snapshot_id:
            for role, ordinal, sha in links:
                extractor.db.execute('INSERT OR IGNORE INTO snapshot_blobs VALUES(?,?,?,?)',
                                     (snapshot_id, sha, role, ordinal))
            # Exact, already declared URL associations only. No title/date/ID
            # heuristics, new activities, or default activity for unknown URLs.
            extractor.db.execute('''INSERT OR IGNORE INTO activity_snapshots(activity_id,snapshot_id,source_id)
                SELECT source.activity_id,snapshot.snapshot_id,source.source_id
                FROM source_records AS source JOIN json_each(source.source_urls_json) AS declared
                JOIN snapshots AS snapshot ON snapshot.url=declared.value
                WHERE snapshot.snapshot_id=? AND source.activity_id IS NOT NULL AND declared.type='text' ''',
                                 (snapshot_id,))
    manifest_sha = extractor.blob(_json(manifest).encode('utf-8'), mime='application/json', kind='capture_manifest')
    if manifest['snapshot_id']:
        extractor.db.execute('INSERT OR IGNORE INTO snapshot_blobs VALUES(?,?,?,?)',
                             (manifest['snapshot_id'], manifest_sha, 'capture_manifest', 0))
    return errors


def preserve_capture_databases(root: Path, inputs: list[Path]) -> dict:
    """Preserve exact source_documents evidence in existing private foundation storage.

    Sources must be stable offline UTF-8 SQLite databases with a rowid table.
    They are opened mode=ro/immutable/query_only, fingerprinted before and after,
    and always left held with completed_at NULL. Even rejected, malformed,
    oversized and digest-mismatched captures retain their available exact bytes.
    No archive, network request, cleanup operation or native-source write occurs.
    The result contains counts and error codes, never captured URLs or bodies.
    """
    root = Path(root).expanduser().absolute()
    paths = list(dict.fromkeys(Path(path).expanduser().absolute() for path in inputs))
    destination = root / 'foundation.sqlite3'
    for path in paths:
        if path == destination or (destination.exists() and path.exists() and path.samefile(destination)):
            raise ValueError('The foundation database cannot also be a capture input.')
    result = {'ok': True, 'files': 0, 'captures': 0, 'snapshots': 0, 'blobs': 0,
              'held': 0, 'errors': 0, 'source_guards_unchanged': True, 'sources': []}
    with closing(connect_foundation(root)) as db:
        store = BlobStore(root, db)
        for path in paths:
            result['files'] += 1
            result['held'] += 1
            detail = {'captures': 0, 'errors': [], 'guard_unchanged': False}
            result['sources'].append(detail)
            try:
                if path != path.resolve():
                    raise HeldInput('Symlink source paths are protected.')
                digest, info = _fingerprint(path)
                sidecars = _sidecars(path)
            except (OSError, ValueError):
                detail['errors'].append(_problem('source_unavailable'))
                result['ok'] = False
                result['source_guards_unchanged'] = False
                result['errors'] += 1
                continue
            previous = db.execute('SELECT * FROM ingestions WHERE source_path=?', (str(path),)).fetchone()
            baseline = {'code': 'capture_source_retained', 'guard': _guard(info)}
            changed = previous and (previous['sha256'] != digest or previous['size'] != info.st_size
                                    or previous['mtime_ns'] != info.st_mtime_ns)
            if previous:
                for error in json.loads(previous['error_json']):
                    if error.get('code') == 'capture_source_retained' and error.get('guard') != _guard(info):
                        changed = True
            if changed:
                db.execute("UPDATE ingestions SET status='held',completed_at=NULL,error_json=? WHERE source_path=?",
                           (_json([error for error in json.loads(previous['error_json'])
                                   if error.get('code') != 'source_changed'] + [_problem('source_changed')]), str(path)))
                db.commit()
                detail['errors'].append(_problem('source_changed'))
                result['source_guards_unchanged'] = False
                result['errors'] += 1
                result['ok'] = False
                continue
            db.execute('''INSERT INTO ingestions(source_path,sha256,size,mtime_ns,status)
                VALUES(?,?,?,?,'held') ON CONFLICT(source_path) DO UPDATE SET status='held',completed_at=NULL''',
                       (str(path), digest, info.st_size, info.st_mtime_ns))
            db.commit()
            extractor = _Extractor(db, store, path)
            error_count = 0
            try:
                if any(sidecars.get(suffix, {}).get('size', 0) for suffix in ('-wal', '-journal')):
                    raise HeldInput('Offline source has a pending transaction sidecar.')
                if _signature(path.stat(follow_symlinks=False)) != _signature(info):
                    raise HeldInput('Source identity changed before opening.')
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as source:
                    source.row_factory = sqlite3.Row
                    source.execute('PRAGMA query_only=ON')
                    if source.execute('PRAGMA encoding').fetchone()[0] != 'UTF-8':
                        raise HeldInput('Source database encoding is not UTF-8.')
                    source.execute('BEGIN')
                    origin = {'source_path': str(path), 'sha256': digest, **_guard(info)}
                    rows = source.execute('''SELECT rowid AS capture_rowid,id,source_url AS url,digest,
                        typeof(document_json) AS storage_type
                        FROM source_documents ORDER BY rowid''')
                    for row in rows:
                        errors = _capture(source, row, extractor, origin)
                        detail['captures'] += 1
                        error_count += len(errors)
                        detail['errors'].extend(errors[:max(0, _MAX_ERRORS - len(detail['errors']))])
                        db.commit()
                    if not detail['captures']:
                        detail['errors'].append(_problem('source_empty'))
                        error_count += 1
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RuntimeError, RecursionError):
                detail['errors'].append(_problem('source_read_failed'))
                error_count += 1
            try:
                final_digest, final_info = _fingerprint(path)
                detail['guard_unchanged'] = (final_digest == digest and _signature(final_info) == _signature(info)
                                             and _sidecars(path) == sidecars)
            except (OSError, ValueError):
                pass
            if not detail['guard_unchanged']:
                detail['errors'].append(_problem('source_guard_changed'))
                error_count += 1
                result['source_guards_unchanged'] = False
            counts = dict(db.execute('SELECT kind,count(*) FROM ingestion_outputs WHERE source_path=? GROUP BY kind',
                                     (str(path),)))
            db.execute('''UPDATE ingestions SET status='held',record_count=?,snapshot_count=?,error_json=?,
                completed_at=NULL WHERE source_path=?''',
                       (detail['captures'], counts.get('snapshot', 0), _json([baseline] + detail['errors']), str(path)))
            db.commit()
            result['captures'] += detail['captures']
            result['snapshots'] += counts.get('snapshot', 0)
            result['blobs'] += counts.get('blob', 0)
            result['errors'] += error_count
            result['ok'] = result['ok'] and error_count == 0
    return result


_LEGACY_PREVIEW_BYTES = 256 * 1024
_LEGACY_JSON_COLUMNS = ('request_json', 'raw_output_json', 'decision_json')
_LEGACY_OUTCOME_COLUMNS = ('occurrence_id', 'input_sha256', 'context_sha256', 'rules_version', 'model_name')


def _legacy_stat(path):
    if path != path.resolve():
        raise HeldInput('Symlink source paths are protected.')
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise HeldInput('Legacy ledger is not a regular file.')
    return info


def _legacy_sidecars(path):
    state = {}
    for suffix in ('-wal', '-journal', '-shm'):
        candidate = path.with_name(path.name + suffix)
        if candidate.exists() or candidate.is_symlink():
            state[suffix] = _guard(_legacy_stat(candidate))
    return state


def _legacy_artifact(source, rowid, column, extractor, *, size, storage_type):
    """Preserve one exact stored TEXT cell, independently of its parseability."""
    artifact = {'blob_sha': None, 'size': size, 'storage_type': storage_type, 'parse_state': 'missing'}
    errors = []
    if storage_type == 'null':
        return artifact, None, errors
    if storage_type not in {'text', 'blob'}:
        artifact['parse_state'] = 'unsupported_storage'
        return artifact, None, [{'code': 'legacy_artifact_not_text', 'column': column}]
    mime = 'text/plain; charset=utf-8' if column == 'error' else 'application/json; charset=utf-8'
    kind = {'request_json': 'legacy_logical_request', 'raw_output_json': 'legacy_parsed_result',
            'decision_json': 'legacy_decision', 'error': 'legacy_source_error'}[column]
    with source.blobopen('historical_attempts', column, rowid, readonly=True) as body:
        if len(body) != size:
            raise HeldInput('Legacy artifact byte length changed.')
        if size > MAX_RECORD_BYTES:
            with tempfile.TemporaryFile() as spool:
                while chunk := body.read(_CHUNK):
                    spool.write(chunk)
                spool.flush()
                with mmap.mmap(spool.fileno(), 0, access=mmap.ACCESS_READ) as raw:
                    artifact['blob_sha'] = extractor.blob(raw, mime=mime, kind=kind)
            artifact['parse_state'] = 'oversize'
            errors.append({'code': 'legacy_artifact_oversize', 'column': column})
            return artifact, None, errors
        raw = body.read()
    artifact['blob_sha'] = extractor.blob(raw, mime=mime, kind=kind)
    parse_limit = MAX_RECORD_BYTES if column == 'request_json' else MAX_SEMANTIC_BYTES
    if column == 'error':
        # Preserve the full original above; only a bounded human-readable error
        # preview enters the review graph.
        try:
            value = raw[:8000].decode('utf-8') if len(raw) <= 8000 else raw[:8000].decode('utf-8', errors='ignore')
        except UnicodeError:
            artifact['parse_state'] = 'invalid_utf8'
            return artifact, None, [{'code': 'legacy_artifact_invalid_utf8', 'column': column}]
        artifact['parse_state'] = 'text_preview' if len(raw) > 8000 else 'text'
        return artifact, value[:2000], errors
    if size > parse_limit:
        artifact['parse_state'] = 'preview_omitted_size'
        return artifact, None, errors
    try:
        value = json.loads(raw.decode('utf-8'), parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        artifact['parse_state'] = 'malformed'
        errors.append({'code': 'legacy_json_malformed', 'column': column})
        return artifact, None, errors
    artifact['parse_state'] = 'parsed'
    return artifact, value, errors


def _legacy_canonical_sha(value):
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    for part in encoder.iterencode(value):
        digest.update(part.encode('utf-8'))
    return digest.hexdigest()


def _legacy_known_refs(extractor, decision):
    """Only explicit, existing decision identities may associate an activity."""
    refs = {}
    for key in ('activity_id', 'item_id'):
        value = decision.get(key)
        if not isinstance(value, str) or not value or value.isdecimal():
            continue
        known = extractor.db.execute('SELECT 1 FROM activities WHERE activity_id=?', (value,)).fetchone()
        if not known:
            known = extractor.db.execute('SELECT 1 FROM activity_aliases WHERE alias_id=?', (value,)).fetchone()
        if known:
            refs[key] = value
    occurrence = decision.get('occurrence_id')
    if isinstance(occurrence, str) and occurrence and not occurrence.isdecimal():
        if extractor.db.execute('SELECT 1 FROM source_records WHERE occurrence_id=?', (occurrence,)).fetchone():
            refs['occurrence_id'] = occurrence
    return refs


def _legacy_review_record(extractor, decision, result, state, error):
    record = {'status': state}
    decision = decision if isinstance(decision, dict) else {}
    result = result if isinstance(result, dict) else {}
    for key in ('status', 'reason'):
        value = decision.get(key)
        if not isinstance(value, str) or not value.strip():
            value = result.get(key)
        if isinstance(value, str) and value.strip():
            record[key] = value[:2000]
    record.update(_legacy_known_refs(extractor, decision))
    source_review = decision.get('source_review')
    if isinstance(source_review, dict):
        source = {key: source_review[key][:2000] for key in ('status', 'reason')
                  if isinstance(source_review.get(key), str)}
        attempts = source_review.get('attempts')
        if isinstance(attempts, list):
            source['attempts'] = [
                {'code': str(attempt.get('code') or attempt.get('status'))[:100],
                 'reason': attempt['reason'][:2000]}
                for attempt in attempts[:50] if isinstance(attempt, dict)
                and isinstance(attempt.get('reason'), str) and attempt['reason'].strip()
                and isinstance(attempt.get('code') or attempt.get('status'), str)]
        if source:
            record['source_review'] = source
    if isinstance(error, str) and error.strip():
        record['error'] = error
    preview = {}
    preview_state = 'absent'
    try:
        for value in (result, decision):
            preview.update(_semantic(value))
        if preview:
            if len(_json(preview).encode('utf-8')) <= _LEGACY_PREVIEW_BYTES:
                record['parsed_result'] = preview
                preview_state = 'recognized_fields'
            else:
                preview_state = 'omitted_size'
    except (ValueError, TypeError, KeyError, RecursionError):
        preview_state = 'omitted_unsupported'
    return record, preview_state


def _legacy_attempt_row(source, row, extractor, origin, guard_sha):
    artifacts, errors, values = {}, [], {}
    canonical_sha = None
    request_metadata = {}
    for column in _LEGACY_JSON_COLUMNS + ('error',):
        artifact, value, problems = _legacy_artifact(
            source, row['legacy_rowid'], column, extractor,
            size=row[column + '_size'], storage_type=row[column + '_type'])
        artifacts[column] = artifact
        errors.extend(problems)
        if column == 'request_json':
            if isinstance(value, dict):
                try:
                    canonical_sha = _legacy_canonical_sha(value)
                except (ValueError, TypeError, RecursionError):
                    errors.append(_problem('legacy_request_canonicalization_failed'))
                request_metadata = {key: value[key] for key in ('model_name', 'rules_version', 'input_sha256')
                                    if isinstance(value.get(key), str) and len(value[key]) <= 2000}
            elif artifact['parse_state'] == 'parsed':
                errors.append(_problem('legacy_request_not_object'))
            elif artifact['parse_state'] == 'missing':
                errors.append(_problem('legacy_request_missing'))
        else:
            values[column] = value
        del value
    key = row['request_sha256']
    key_valid = isinstance(key, str) and len(key) == 64 and all(character in '0123456789abcdef' for character in key)
    canonical_valid = canonical_sha == key if canonical_sha is not None else None
    if not key_valid:
        errors.append(_problem('legacy_request_key_invalid'))
    elif canonical_valid is False:
        errors.append(_problem('legacy_request_digest_mismatch'))
    state = row['state'] if isinstance(row['state'], str) and row['state'] else 'unknown'
    record, preview_state = _legacy_review_record(
        extractor, values.get('decision_json'), values.get('raw_output_json'), state, values.get('error'))
    paid = {'request_key': key, 'state': state, 'charge': 'unknown', 'source_ledger': extractor.source,
            'artifacts': {'request_blob_sha': artifacts['request_json']['blob_sha'],
                          'result_blob_sha': artifacts['raw_output_json']['blob_sha'],
                          'decision_blob_sha': artifacts['decision_json']['blob_sha']},
            'legacy_metadata': {'format': 'nannanews-legacy-json-v1',
                                'not_http_wire': True, 'source_rowid': row['legacy_rowid'],
                                'source_guard_blob_sha': guard_sha,
                                'logical_request_canonical_sha': canonical_sha,
                                'logical_request_digest_valid': canonical_valid,
                                'semantic_preview': preview_state,
                                'error_blob_sha': artifacts['error']['blob_sha']}}
    # These stored logical descriptors/results are never passed as `wire` or as
    # an assistant answer. The extractor labels its rendered view parsed_result.
    linkable_key = isinstance(key, str) and bool(key)
    review_errors = [] if linkable_key else [
        {'stage': 'legacy', 'code': 'legacy_request_key_invalid',
         'reason': 'Native request key is missing; stored artifacts retained without inventing an identity.'}]
    review_id = extractor.review(record, paid=paid, extra_errors=review_errors)
    previous = extractor.db.execute('SELECT * FROM attempts WHERE request_key=?', (key,)).fetchone()
    previous_review = previous['result_review_id'] if previous else None
    value = dict(request_metadata, state=state, reserved_at=row['reserved_at'], finished_at=row['finished_at'],
                 error=values.get('error'))
    if linkable_key:
        extractor.attempt(key, value, source_ref=key, review_id=review_id)
    conflict = previous is not None and (previous['state'] != state or previous_review not in (None, review_id))
    manifest = {'format': 'nannanews-legacy-attempt-v1', 'not_http_wire': True,
                'source_database': origin, 'source_rowid': row['legacy_rowid'],
                'request_key': key, 'request_key_semantics': 'legacy_canonical_logical_descriptor_sha256',
                'logical_request_canonical_sha': canonical_sha, 'logical_request_digest_valid': canonical_valid,
                'source_state': state, 'reserved_at': row['reserved_at'], 'finished_at': row['finished_at'],
                'artifacts': artifacts, 'review_id': review_id, 'semantic_preview': preview_state,
                'existing_attempt_state_preserved': previous['state'] if previous and previous['state'] != state else None,
                'existing_result_review_id_preserved': previous_review if previous_review != review_id else None,
                'existing_history_conflict': conflict, 'errors': errors}
    extractor.blob(_json(manifest).encode('utf-8'), mime='application/json', kind='legacy_attempt_manifest')
    return errors


def preserve_legacy_attempt_databases(root: Path, inputs: list[Path]) -> dict:
    """Link immutable native paid-ledger rows to their exact legacy JSON artifacts.

    Unlike capture preservation, this deliberately does not hash entire databases:
    native ledger provenance is an inode/size/mtime/ctime guard, plus exact cell
    blob SHA-256s. Fresh ingestion.sha256 identifies the explicitly labeled guard
    manifest, NOT database bytes. Existing ingestion hashes/history are retained.
    Every source stays held and is never cleaned, archived, or promoted to wire
    evidence. Outcome five-tuples have no request foreign key and stay unjoined.
    """
    root = Path(root).expanduser().absolute()
    paths = list(dict.fromkeys(Path(path).expanduser().absolute() for path in inputs))
    destination = root / 'foundation.sqlite3'
    for path in paths:
        if path == destination or (destination.exists() and path.exists() and path.samefile(destination)):
            raise ValueError('The foundation database cannot also be a legacy input.')
    result = {'ok': True, 'files': 0, 'attempts': 0, 'reviews': 0, 'outcomes': 0,
              'blobs': 0, 'held': 0, 'errors': 0, 'source_guards_unchanged': True, 'sources': []}
    with closing(connect_foundation(root)) as db:
        store = BlobStore(root, db)
        for path in paths:
            result['files'] += 1
            result['held'] += 1
            detail = {'attempts': 0, 'outcomes': 0, 'errors': [], 'guard_unchanged': False}
            result['sources'].append(detail)
            try:
                info, sidecars = _legacy_stat(path), _legacy_sidecars(path)
            except (OSError, ValueError):
                detail['errors'].append(_problem('source_unavailable'))
                result['errors'] += 1
                result['ok'] = result['source_guards_unchanged'] = False
                continue
            origin = {'source_path': str(path), 'guard_method': 'inode_size_mtime_ctime', **_guard(info)}
            guard_raw = _json({'format': 'nannanews-legacy-source-guard-v1', **origin}).encode('utf-8')
            guard_sha = hashlib.sha256(guard_raw).hexdigest()
            baseline = {'code': 'legacy_attempt_source_retained', 'guard': _guard(info),
                        'source_guard_blob_sha': guard_sha, 'guard_method': 'inode_size_mtime_ctime'}
            previous = db.execute('SELECT * FROM ingestions WHERE source_path=?', (str(path),)).fetchone()
            previous_errors = json.loads(previous['error_json']) if previous else []
            changed = previous and (previous['size'] != info.st_size or previous['mtime_ns'] != info.st_mtime_ns)
            if any(error.get('code') == 'legacy_attempt_source_retained' and error.get('guard') != _guard(info)
                   for error in previous_errors):
                changed = True
            if changed:
                db.execute("UPDATE ingestions SET status='held',completed_at=NULL,error_json=? WHERE source_path=?",
                           (_json([error for error in previous_errors if error.get('code') != 'source_changed']
                                  + [_problem('source_changed')]), str(path)))
                db.commit()
                detail['errors'].append(_problem('source_changed'))
                result['errors'] += 1
                result['ok'] = result['source_guards_unchanged'] = False
                continue
            baseline['ingestion_sha256_scope'] = 'existing_fingerprint_retained' if previous else 'legacy_source_guard_manifest'
            if previous:
                for error in previous_errors:
                    if error.get('code') == 'legacy_attempt_source_retained':
                        baseline['ingestion_sha256_scope'] = error.get('ingestion_sha256_scope')
            db.execute('''INSERT INTO ingestions(source_path,sha256,size,mtime_ns,status)
                VALUES(?,?,?,?,'held') ON CONFLICT(source_path) DO UPDATE SET status='held',completed_at=NULL''',
                       (str(path), guard_sha, info.st_size, info.st_mtime_ns))
            db.commit()
            extractor = _Extractor(db, store, path)
            extractor.blob(guard_raw, mime='application/json', kind='legacy_source_guard')
            error_count = 0
            try:
                if any(sidecars.get(suffix, {}).get('size', 0) for suffix in ('-wal', '-journal')):
                    raise HeldInput('Offline legacy ledger has a pending transaction sidecar.')
                if _signature(_legacy_stat(path)) != _signature(info):
                    raise HeldInput('Legacy source identity changed before opening.')
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as source:
                    source.row_factory = sqlite3.Row
                    source.execute('PRAGMA query_only=ON')
                    if source.execute('PRAGMA encoding').fetchone()[0] != 'UTF-8':
                        raise HeldInput('Legacy ledger encoding is not UTF-8.')
                    source.execute('BEGIN')
                    # Lengths bound each stored cell in bytes; blobopen then
                    # preserves it without selecting a whole large JSON row.
                    sizes = ','.join('length(CAST(' + column + ' AS BLOB)) AS ' + column + '_size,'
                                     'typeof(' + column + ') AS ' + column + '_type'
                                     for column in _LEGACY_JSON_COLUMNS + ('error',))
                    rows = source.execute('SELECT rowid AS legacy_rowid,request_sha256,state,reserved_at,finished_at,'
                                          + sizes + ' FROM historical_attempts ORDER BY rowid')
                    for row in rows:
                        errors = _legacy_attempt_row(source, row, extractor, origin, guard_sha)
                        detail['attempts'] += 1
                        error_count += len(errors)
                        detail['errors'].extend(errors[:max(0, _MAX_ERRORS - len(detail['errors']))])
                        db.commit()
                    if source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='historical_outcomes'").fetchone():
                        for row in source.execute('SELECT ' + ','.join(_LEGACY_OUTCOME_COLUMNS)
                                                  + ' FROM historical_outcomes ORDER BY ' + ','.join(_LEGACY_OUTCOME_COLUMNS)):
                            manifest = {'format': 'nannanews-legacy-outcome-v1', 'source_database': origin,
                                        'outcome': dict(row), 'request_key': None,
                                        'association': 'no_request_foreign_key'}
                            extractor.blob(_json(manifest).encode('utf-8'), mime='application/json', kind='legacy_outcome_manifest')
                            detail['outcomes'] += 1
                            db.commit()
                    if not detail['attempts'] and not detail['outcomes']:
                        detail['errors'].append(_problem('source_empty'))
                        error_count += 1
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RuntimeError, RecursionError):
                detail['errors'].append(_problem('legacy_source_read_failed'))
                error_count += 1
            try:
                detail['guard_unchanged'] = (_signature(_legacy_stat(path)) == _signature(info)
                                             and _legacy_sidecars(path) == sidecars)
            except (OSError, ValueError):
                pass
            if not detail['guard_unchanged']:
                detail['errors'].append(_problem('source_guard_changed'))
                error_count += 1
                result['source_guards_unchanged'] = False
            counts = dict(db.execute('SELECT kind,count(*) FROM ingestion_outputs WHERE source_path=? GROUP BY kind', (str(path),)))
            db.execute('''UPDATE ingestions SET status='held',record_count=?,snapshot_count=0,error_json=?,
                completed_at=NULL WHERE source_path=?''',
                       (counts.get('review', 0), _json([baseline] + detail['errors']), str(path)))
            db.commit()
            result['attempts'] += detail['attempts']
            result['reviews'] += counts.get('review', 0)
            result['outcomes'] += detail['outcomes']
            result['blobs'] += counts.get('blob', 0)
            result['errors'] += error_count
            result['ok'] = result['ok'] and error_count == 0
    return result
