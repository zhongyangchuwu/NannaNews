"""Synthetic native paid ledgers retain exact non-wire JSON and prevention state."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from nannanews import data_foundation_capture as capture
from nannanews.data_foundation import connect_foundation


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def stored(value):
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def request(**changes):
    value = {'model_name': 'legacy-model', 'rules_version': 'legacy-rules', 'input_sha256': 'input-old',
             'description': '合成逻辑请求，不是 HTTP 请求实体。'}
    value.update(changes)
    return value


def row(descriptor=None, *, state='completed', result=None, decision=None, error=None, key=None):
    descriptor = request() if descriptor is None else descriptor
    encoded = stored(descriptor)
    if key is None:
        key = digest(canonical(descriptor).encode('utf-8'))
    return (key, encoded, state, '2026-10-01T00:00:00Z',
            None if state == 'reserved' else '2026-10-01T00:01:00Z', error, stored(result), stored(decision))


def ledger(path, rows, outcomes=()):
    with closing(sqlite3.connect(path)) as db:
        db.execute('''CREATE TABLE historical_attempts(request_sha256 TEXT PRIMARY KEY,request_json TEXT,
                      state TEXT,reserved_at TEXT,finished_at TEXT,error TEXT,raw_output_json TEXT,decision_json TEXT)''')
        db.execute('''CREATE TABLE historical_outcomes(occurrence_id TEXT,input_sha256 TEXT,context_sha256 TEXT,
                      rules_version TEXT,model_name TEXT,
                      PRIMARY KEY(occurrence_id,input_sha256,context_sha256,rules_version,model_name))''')
        db.executemany('INSERT INTO historical_attempts VALUES(?,?,?,?,?,?,?,?)', rows)
        db.executemany('INSERT INTO historical_outcomes VALUES(?,?,?,?,?)', outcomes)
        db.commit()


def guard(path):
    info = path.stat()
    return (path.read_bytes(), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def blob(db, sha):
    path = db.execute('SELECT local_path FROM blobs WHERE sha256=?', (sha,)).fetchone()[0]
    return Path(path).read_bytes()


def manifests(db, kind='legacy_attempt_manifest'):
    return [json.loads(blob(db, value[0])) for value in db.execute('SELECT sha256 FROM blobs WHERE kind=?', (kind,))]


def semantic_rows(db):
    tables = ('attempts', 'attempt_origins', 'reviews', 'blobs', 'ingestions', 'ingestion_outputs')
    return {table: sorted(tuple(value) for value in db.execute('SELECT * FROM ' + table)) for table in tables}


def seed_identity(root):
    with closing(connect_foundation(root)) as db, db:
        db.execute('''INSERT INTO activities(activity_id,title,summary,review_status,record_version,input_sha256)
                      VALUES('known-activity','Approved title','Approved summary','approved','1','current-input')''')
        db.execute("INSERT INTO activity_aliases VALUES('known-alias','known-activity')")
        db.execute('''INSERT INTO source_records(source_id,activity_id,occurrence_id,source_urls_json,locator_json,parse_status)
                      VALUES('known-source','known-activity','known-occurrence','[]','{}','parsed')''')


def test_exact_json_linkage_is_non_wire_idempotent_and_never_hashes_source(tmp_path, monkeypatch):
    root, path = tmp_path / 'foundation', tmp_path / '.native.historical-attempts.sqlite3'
    seed_identity(root)
    entry = row(result={'status': 'confirmed', 'reason': '保存的解析结论，不是提供商逐字回答。'},
                decision={'status': 'confirmed', 'reason': 'Stored decision', 'item_id': 'known-alias',
                          'occurrence_id': 'known-occurrence', 'private_unrecognized': 'retained only in original'})
    ledger(path, [entry])
    before = guard(path)

    def forbidden(*args, **kwargs):
        pytest.fail('No whole-source fingerprint, archive, cleanup, or model call is allowed.')

    monkeypatch.setattr(capture, '_fingerprint', forbidden)
    result = capture.preserve_legacy_attempt_databases(root, [path])
    assert result['ok'] and result['attempts'] == 1 and result['reviews'] == 1
    assert result['held'] == 1 and result['source_guards_unchanged']
    assert guard(path) == before
    with closing(connect_foundation(root)) as db:
        attempt = db.execute('SELECT * FROM attempts').fetchone()
        assert attempt['request_key'] == entry[0] and attempt['state'] == 'completed'
        assert attempt['source_ledger'] == str(path)
        review = db.execute('SELECT * FROM reviews WHERE review_id=?', (attempt['result_review_id'],)).fetchone()
        fields = json.loads(review['fields_json'])
        paid = fields['paid_facts']
        assert paid['request_key'] == entry[0] and paid['state'] == 'completed' and paid['charge'] == 'unknown'
        assert paid['source_ledger'] == str(path)
        assert paid['legacy_metadata']['not_http_wire'] is True
        assert paid['legacy_metadata']['logical_request_digest_valid'] is True
        assert '_model_wire' not in fields
        assert fields['answer_kind'] == 'parsed_result'
        assert blob(db, review['answer_blob_sha']).startswith(b'Parsed review result (not a verbatim model answer)')
        assert review['activity_id'] == 'known-activity' and review['occurrence_id'] == 'known-occurrence'
        assert blob(db, paid['artifacts']['request_blob_sha']) == entry[1].encode('utf-8')
        assert blob(db, paid['artifacts']['result_blob_sha']) == entry[6].encode('utf-8')
        assert blob(db, paid['artifacts']['decision_blob_sha']) == entry[7].encode('utf-8')
        assert paid['artifacts']['request_blob_sha'] != entry[0]
        manifest, = manifests(db)
        assert manifest['review_id'] == review['review_id'] and manifest['request_key'] == entry[0]
        assert manifest['source_database']['guard_method'] == 'inode_size_mtime_ctime'
        assert 'sha256' not in manifest['source_database']
        assert manifest['source_database']['inode'] == before[2]
        ingestion = db.execute('SELECT * FROM ingestions').fetchone()
        assert ingestion['source_path'] == str(path)
        assert ingestion['status'] == 'held' and ingestion['completed_at'] is None
        assert ingestion['cleaned_at'] is None
        assert ingestion['sha256'] == paid['legacy_metadata']['source_guard_blob_sha']
        assert json.loads(blob(db, ingestion['sha256']))['format'] == 'nannanews-legacy-source-guard-v1'
        assert db.execute('SELECT count(*) FROM cleanup_log').fetchone()[0] == 0
        assert db.execute('SELECT title FROM activities').fetchone()[0] == 'Approved title'
        first = semantic_rows(db)
    again = capture.preserve_legacy_attempt_databases(root, [path, path])
    assert again['files'] == 1 and again['reviews'] == 1
    with closing(connect_foundation(root)) as db:
        assert semantic_rows(db) == first
    assert guard(path) == before


def test_existing_paid_state_and_nonnull_result_are_preserved_with_conflict_labels(tmp_path):
    root, path = tmp_path / 'foundation', tmp_path / 'preserved.sqlite3'
    entry = row(state='completed', decision={'status': 'confirmed', 'reason': 'New historical evidence'})
    ledger(path, [entry])
    with closing(connect_foundation(root)) as db, db:
        db.execute("INSERT INTO reviews(review_id,status,fields_json,error_json) VALUES('existing-review','approved','{}','[]')")
        db.execute('''INSERT INTO attempts(request_key,state,result_review_id,reserved_at,source_ledger)
                      VALUES(?,'reserved','existing-review','earliest-reservation','prior-ledger')''', (entry[0],))
    result = capture.preserve_legacy_attempt_databases(root, [path])
    assert result['ok'] and result['reviews'] == 1
    with closing(connect_foundation(root)) as db:
        attempt = db.execute('SELECT * FROM attempts').fetchone()
        assert attempt['state'] == 'reserved' and attempt['result_review_id'] == 'existing-review'
        assert attempt['reserved_at'] == 'earliest-reservation' and attempt['source_ledger'] == 'prior-ledger'
        manifest, = manifests(db)
        assert manifest['existing_history_conflict'] is True
        assert manifest['existing_attempt_state_preserved'] == 'reserved'
        assert manifest['existing_result_review_id_preserved'] == 'existing-review'
        assert manifest['review_id'] != 'existing-review'
        assert db.execute('SELECT state FROM attempt_origins').fetchone()[0] == 'completed'
        new_review = db.execute('SELECT fields_json FROM reviews WHERE review_id=?', (manifest['review_id'],)).fetchone()
        assert json.loads(new_review[0])['paid_facts']['request_key'] == entry[0]
        first = semantic_rows(db)
    capture.preserve_legacy_attempt_databases(root, [path])
    with closing(connect_foundation(root)) as db:
        assert semantic_rows(db) == first


def test_null_result_is_filled_by_exact_key_without_changing_paid_state(tmp_path):
    root, path = tmp_path / 'foundation', tmp_path / 'fill-null.sqlite3'
    first = row(request(index=1), decision={'status': 'confirmed', 'reason': 'First exact key'})
    second = row(request(index=2), state='failed', error='Original provider error', decision={'status': 'failed'})
    ledger(path, [first, second])
    with closing(connect_foundation(root)) as db, db:
        db.execute("INSERT INTO attempts(request_key,state) VALUES(?,'unknown')", (first[0],))
        db.execute("INSERT INTO attempts(request_key,state) VALUES(?,'completed')", (second[0],))
    result = capture.preserve_legacy_attempt_databases(root, [path])
    assert result['ok'] and result['attempts'] == 2
    with closing(connect_foundation(root)) as db:
        for key, expected_state, source_state in ((first[0], 'unknown', 'completed'), (second[0], 'completed', 'failed')):
            attempt = db.execute('SELECT * FROM attempts WHERE request_key=?', (key,)).fetchone()
            assert attempt['state'] == expected_state and attempt['result_review_id']
            review = db.execute('SELECT * FROM reviews WHERE review_id=?', (attempt['result_review_id'],)).fetchone()
            fields = json.loads(review['fields_json'])
            assert fields['paid_facts']['request_key'] == key and fields['paid_facts']['state'] == source_state
        errors = json.loads(review['error_json'])
        assert errors[0]['reason'] == 'Original provider error'


def test_outcome_five_tuples_never_invent_request_foreign_keys_or_activity_links(tmp_path):
    root, path = tmp_path / 'foundation', tmp_path / 'outcomes.sqlite3'
    seed_identity(root)
    # Even a request descriptor's known occurrence is not an explicit decision
    # identity, and a coincident five-tuple supplies no request foreign key.
    entry = row(request(occurrence_id='known-occurrence'), decision={'status': 'confirmed', 'title': 'Approved title',
                                                                   'item_id': 'unknown-alias', 'occurrence_id': 'unknown-occurrence'})
    outcome = ('known-occurrence', 'input-old', 'context-old', 'legacy-rules', 'legacy-model')
    ledger(path, [entry], [outcome])
    result = capture.preserve_legacy_attempt_databases(root, [path])
    assert result['ok'] and result['outcomes'] == 1
    with closing(connect_foundation(root)) as db:
        review = db.execute('SELECT * FROM reviews').fetchone()
        assert review['activity_id'] is None and review['occurrence_id'] is None
        outcome_manifest, = manifests(db, 'legacy_outcome_manifest')
        assert outcome_manifest['request_key'] is None
        assert outcome_manifest['association'] == 'no_request_foreign_key'
        assert tuple(outcome_manifest['outcome'][key] for key in capture._LEGACY_OUTCOME_COLUMNS) == outcome
        assert db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM activity_snapshots').fetchone()[0] == 0


def test_malformed_nonjson_and_oversize_exact_cells_are_held_and_retained(tmp_path, monkeypatch):
    root, path = tmp_path / 'foundation', tmp_path / 'malformed.sqlite3'
    monkeypatch.setattr(capture, 'MAX_RECORD_BYTES', 512)
    malformed_request = 'historical non-JSON request metadata\n'
    first = ('a' * 64, malformed_request, 'failed', 'old-reservation', 'old-finish', 'Original failure\n',
             'not JSON result\n', '{ malformed decision\n')
    second = row(request(index=2), decision={'status': 'confirmed', 'reason': 'x' * 2000})
    ledger(path, [first, second])
    before = guard(path)
    result = capture.preserve_legacy_attempt_databases(root, [path])
    assert not result['ok'] and result['attempts'] == 2 and result['reviews'] == 2
    assert result['errors'] == 4 and result['source_guards_unchanged']
    with closing(connect_foundation(root)) as db:
        by_key = {value['request_key']: value for value in manifests(db)}
        for entry in (first, second):
            manifest = by_key[entry[0]]
            for column, index in (('request_json', 1), ('raw_output_json', 6), ('decision_json', 7)):
                if entry[index] is not None:
                    assert blob(db, manifest['artifacts'][column]['blob_sha']) == entry[index].encode('utf-8')
        assert by_key[first[0]]['artifacts']['request_json']['parse_state'] == 'malformed'
        assert by_key[second[0]]['artifacts']['decision_json']['parse_state'] == 'oversize'
        assert by_key[first[0]]['logical_request_digest_valid'] is None
        for value in db.execute('SELECT fields_json FROM reviews'):
            assert '_model_wire' not in json.loads(value[0])
    assert guard(path) == before


def test_canonical_key_mismatch_is_reported_not_rewritten(tmp_path):
    root, path = tmp_path / 'foundation', tmp_path / 'mismatch.sqlite3'
    entry = row(key='f' * 64, decision={'status': 'confirmed', 'reason': 'Historical result'})
    ledger(path, [entry])
    result = capture.preserve_legacy_attempt_databases(root, [path])
    assert not result['ok'] and result['errors'] == 1
    with closing(connect_foundation(root)) as db:
        manifest, = manifests(db)
        assert manifest['logical_request_digest_valid'] is False
        assert manifest['errors'] == [{'code': 'legacy_request_digest_mismatch'}]
        assert db.execute('SELECT request_key FROM attempts').fetchone()[0] == 'f' * 64
        assert db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1


def test_reserved_missing_outcomes_still_have_original_request_evidence(tmp_path):
    root, path = tmp_path / 'foundation', tmp_path / 'reserved.sqlite3'
    entry = row(state='reserved')
    ledger(path, [entry])
    result = capture.preserve_legacy_attempt_databases(root, [path])
    assert result['ok'] and result['reviews'] == 1
    with closing(connect_foundation(root)) as db:
        review = db.execute('SELECT * FROM reviews').fetchone()
        paid = json.loads(review['fields_json'])['paid_facts']
        assert paid['state'] == 'reserved' and paid['artifacts']['result_blob_sha'] is None
        assert paid['artifacts']['decision_blob_sha'] is None
        assert blob(db, paid['artifacts']['request_blob_sha']) == entry[1].encode('utf-8')
        assert db.execute('SELECT state FROM attempts').fetchone()[0] == 'reserved'


def test_large_semantic_preview_omitted_without_losing_exact_original(tmp_path, monkeypatch):
    root, path = tmp_path / 'foundation', tmp_path / 'preview.sqlite3'
    seed_identity(root)
    monkeypatch.setattr(capture, 'MAX_SEMANTIC_BYTES', 128)
    entry = row(decision={'status': 'confirmed', 'item_id': 'known-alias', 'reason': 'x' * 500})
    ledger(path, [entry])
    result = capture.preserve_legacy_attempt_databases(root, [path])
    assert result['ok']
    with closing(connect_foundation(root)) as db:
        manifest, = manifests(db)
        assert manifest['artifacts']['decision_json']['parse_state'] == 'preview_omitted_size'
        assert blob(db, manifest['artifacts']['decision_json']['blob_sha']) == entry[7].encode('utf-8')
        review = db.execute('SELECT * FROM reviews').fetchone()
        assert review['activity_id'] is None


def test_source_guard_change_retains_evidence_and_blocks_reinterpretation(tmp_path, monkeypatch):
    root, path = tmp_path / 'foundation', tmp_path / 'changed.sqlite3'
    ledger(path, [row(decision={'status': 'confirmed', 'reason': 'Stored result'})])
    original_row = capture._legacy_attempt_row

    def change_after_preservation(source, native_row, extractor, origin, guard_sha):
        value = original_row(source, native_row, extractor, origin, guard_sha)
        with path.open('ab') as stream:
            stream.write(b'changed-native-bytes')
        return value

    monkeypatch.setattr(capture, '_legacy_attempt_row', change_after_preservation)
    result = capture.preserve_legacy_attempt_databases(root, [path])
    assert not result['ok'] and not result['source_guards_unchanged']
    assert result['sources'][0]['errors'][-1] == {'code': 'source_guard_changed'}
    repeated = capture.preserve_legacy_attempt_databases(root, [path])
    assert repeated['attempts'] == 0 and not repeated['source_guards_unchanged']
    assert repeated['sources'][0]['errors'] == [{'code': 'source_changed'}]
    with closing(connect_foundation(root)) as db:
        assert db.execute('SELECT count(*) FROM reviews').fetchone()[0] == 1
        ingestion = db.execute('SELECT * FROM ingestions').fetchone()
        assert ingestion['status'] == 'held' and ingestion['completed_at'] is None


def test_readonly_native_connection_does_not_create_journals(tmp_path, monkeypatch):
    root, path = tmp_path / 'foundation', tmp_path / 'readonly.sqlite3'
    ledger(path, [row()])
    before = guard(path)
    original_row = capture._legacy_attempt_row

    def readonly(source, native_row, extractor, origin, guard_sha):
        assert source.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            source.execute("UPDATE historical_attempts SET state='failed'")
        return original_row(source, native_row, extractor, origin, guard_sha)

    monkeypatch.setattr(capture, '_legacy_attempt_row', readonly)
    result = capture.preserve_legacy_attempt_databases(root, [path])
    assert result['ok'] and guard(path) == before
    assert not any(path.with_name(path.name + suffix).exists() for suffix in ('-wal', '-journal', '-shm'))


def test_pending_native_journal_is_not_consumed_or_deleted(tmp_path):
    root, path = tmp_path / 'foundation', tmp_path / 'pending.sqlite3'
    ledger(path, [row()])
    journal = path.with_name(path.name + '-journal')
    journal.write_bytes(b'synthetic pending native transaction')
    before = guard(path)
    result = capture.preserve_legacy_attempt_databases(root, [path])
    assert not result['ok'] and result['attempts'] == 0
    assert guard(path) == before and journal.read_bytes() == b'synthetic pending native transaction'


def test_missing_request_key_retains_row_artifacts_without_inventing_key(tmp_path):
    root, path = tmp_path / 'foundation', tmp_path / 'missing-key.sqlite3'
    entry = list(row(decision={'status': 'unresolved', 'reason': 'Missing native identity'}))
    entry[0] = None
    ledger(path, [entry])
    result = capture.preserve_legacy_attempt_databases(root, [path])
    assert not result['ok'] and result['reviews'] == 1
    with closing(connect_foundation(root)) as db:
        assert db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0
        manifest, = manifests(db)
        assert manifest['request_key'] is None and manifest['review_id']
        assert blob(db, manifest['artifacts']['request_json']['blob_sha']) == entry[1].encode('utf-8')


def test_same_key_across_ledgers_keeps_each_exact_outcome_and_native_source_path(tmp_path):
    root = tmp_path / 'foundation'
    first_path, second_path = tmp_path / 'first.sqlite3', tmp_path / 'second.sqlite3'
    first = row(decision={'status': 'confirmed', 'reason': 'First retained outcome'})
    second = row(state='failed', error='Second retained source error',
                 decision={'status': 'unresolved', 'reason': 'Different retained outcome'})
    assert first[0] == second[0]
    ledger(first_path, [first])
    ledger(second_path, [second])
    first_guard, second_guard = guard(first_path), guard(second_path)
    result = capture.preserve_legacy_attempt_databases(root, [first_path, second_path])
    assert result['ok'] and result['reviews'] == 2 and result['attempts'] == 2
    with closing(connect_foundation(root)) as db:
        assert db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM attempt_origins').fetchone()[0] == 2
        by_path = {value['source_database']['source_path']: value for value in manifests(db)}
        assert set(by_path) == {str(first_path), str(second_path)}
        canonical_review = db.execute('SELECT result_review_id FROM attempts').fetchone()[0]
        assert canonical_review == by_path[str(first_path)]['review_id']
        assert by_path[str(second_path)]['review_id'] != canonical_review
        assert by_path[str(second_path)]['existing_history_conflict'] is True
        for path, entry in ((first_path, first), (second_path, second)):
            manifest = by_path[str(path)]
            fields = json.loads(db.execute('SELECT fields_json FROM reviews WHERE review_id=?',
                                           (manifest['review_id'],)).fetchone()[0])
            assert fields['paid_facts']['source_ledger'] == str(path)
            assert blob(db, fields['paid_facts']['artifacts']['decision_blob_sha']) == entry[7].encode('utf-8')
        original = semantic_rows(db)
    capture.preserve_legacy_attempt_databases(root, [first_path, second_path])
    with closing(connect_foundation(root)) as db:
        assert semantic_rows(db) == original
    assert guard(first_path) == first_guard and guard(second_path) == second_guard
