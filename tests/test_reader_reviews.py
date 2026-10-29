"""Review-store contracts use only a tiny public pack and isolated private app DB."""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3

import pytest

from nannanews import runtime, schema
from nannanews.reader_reviews import (
    ReviewConflict, ReviewNotFound, ReviewStore, ReviewUnavailable,
    ReviewValidationError, validate_payload, validate_public_url,
)
from nannanews.static_export import GENERATOR, _json, _manifest_files

ITEM = 'item-' + 'a' * 24
OTHER = 'item-' + 'b' * 24
BASE = '1' * 64
NEW = '2' * 64
PUBLISHED = '3' * 64


def write_pack(root, version=BASE, *, revisions=(), changed_source=False, removed=False):
    """Native manifest/catalog/shard shape; no private source database fixture."""
    root.mkdir(parents=True, exist_ok=True)
    prefix = 'versions/' + version + '/'
    records, items, shards = [], [], {}
    for item_id, count in ([(OTHER, 1)] if removed else [(ITEM, 3), (OTHER, 1)]):
        rows = []
        for index in range(count):
            row = {'id': 'occ-' + item_id[5] * 23 + str(index), 'item_id': item_id,
                   'issue_id': 'issue-' + str(index), 'title': 'Original activity ' + item_id[5],
                   'kind': 'notice', 'category': '讲座', 'preview': 'Original public excerpt',
                   'content_status': 'needs_review' if item_id == ITEM else 'verified',
                   'issue_date': '2020-01-01', 'source_urls': ['https://www.nju.edu.cn/a'],
                   'source_hosts': ['www.nju.edu.cn'], 'dates': [], 'expiry_date': '2020-01-02',
                   'colleges': [], 'image_count': 0}
            detail = dict(row, body_text='Changed source body' if changed_source else 'Original complete body',
                          images=[], primary_images=[], date_review=None, original_dates=[],
                          college_mentions=[], locator={}, table_cells=[])
            rows.append(detail)
            records.append(row)
        items.append({'id': item_id, 'latest_occurrence_id': rows[0]['id'],
                      'occurrence_count': count, 'aliases': [], 'detail_path': prefix + 'items/' + item_id[5:7] + '.json'})
        shards.setdefault(item_id[5:7], {})[item_id] = {
            'id': item_id, 'latest_occurrence_id': rows[0]['id'], 'occurrences': rows}
    for revision in revisions:
        stamp = {'revision': revision['revision'], 'status': revision['payload']['status'],
                 'reviewed_at': revision['created_at'], 'reason': revision['payload']['reason']}
        for row in records:
            if row['item_id'] == revision['item_id']:
                row['manual_review'] = stamp
                row['title'] = revision['payload']['activity']['title']
                verified = revision['payload']['status'] == 'verified'
                row['content_status'] = 'human_verified' if verified else 'needs_review'
                row['dates'] = [dict(kind=entry['kind'], normalized_date=entry['date'],
                                     resolution='explicit', literal_source='manual')
                                for entry in revision['payload']['activity']['dates']] if verified else []
    files = {'catalog.json': {'items': items, 'occurrences': records}, 'issues.json': [],
             'search-documents.json': [], 'search-index.json': {}}
    files.update({'items/' + bucket + '.json': shard for bucket, shard in shards.items()})
    for name, value in files.items():
        destination = root / prefix / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_json(value))
    manifest = {'schema_version': 2, 'generator': GENERATOR, 'data_version': version,
                'content_rules_version': 'fixture-rules',
                'generated_at': '2026-10-25T00:00:00+00:00', 'counts': {'items': len(items), 'occurrences': len(records)},
                'files': _manifest_files(version)}
    if revisions:
        manifest['review_base_version'] = BASE
    (root / 'manifest.json').write_bytes(_json(manifest))
    return manifest


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('NANNA_DATA_DIR', str(tmp_path / 'private-console'))
    monkeypatch.setenv('NANNA_READER_DATA_DIR', str(tmp_path / 'public-data'))
    monkeypatch.delenv('NANNA_READER_SITE_DATA_DIR', raising=False)
    monkeypatch.delenv('NANNA_READER_SEARCH_BUILDER', raising=False)
    monkeypatch.delenv('NANNA_READER_URL', raising=False)
    monkeypatch.setattr(runtime, 'settings', runtime.load_settings())
    schema.migrate()
    write_pack(runtime.settings.reader_data_dir)
    return ReviewStore(runtime.settings)


def payload(title='Human corrected activity'):
    return {'status': 'verified', 'reason': '已人工核对原刊与公开来源。', 'activity': {
        'title': title, 'summary': 'Corrected summary', 'content': 'Complete human-reviewed body',
        'category': '活动', 'time': '14:00', 'venue': '仙林校区', 'organizer': None,
        'audience': '全校师生', 'participation': None,
        'source_urls': ['https://www.nju.edu.cn/a'],
        'dates': [{'kind': 'event', 'date': '2026-10-26'}]}}


def save_first(store):
    item = store.get_item(ITEM)
    return store.save(ITEM, payload(), actor='editor', expected_head=None, base_version=item['base_version'])


def test_saved_draft_is_private_and_canonical_scope_is_complete(store):
    root = store.settings.reader_data_dir
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    original = store.get_item(ITEM)
    assert len(original['occurrences']) == len(original['catalog_records']) == 3
    assert original['initial_source'] == 'original_excerpt'
    with closing(sqlite3.connect(store.settings.database_path)) as db:
        assert db.execute('SELECT count(*) FROM reader_review_project').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM reader_review_revisions').fetchone()[0] == 0
    first = save_first(store)
    item = store.get_item(ITEM)
    assert item['head']['payload'] == payload()
    assert item['head_revision'] == first['revision']
    assert item['unpublished'] and not item['stale'] and item['published_revision'] is None
    assert item['initial_payload'] == original['initial_payload']
    assert all(row['title'] != payload()['activity']['title'] for row in item['occurrences'])
    assert {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()} == before
    snapshot = store.publication_snapshot()
    assert snapshot['base_version'] == BASE
    assert snapshot['revisions'] == [first]
    assert snapshot['active_manifest'] == snapshot['base_manifest']


def test_queue_groups_filters_searches_and_never_loads_detail_shards(store):
    root = store.settings.reader_data_dir
    for shard in (root / 'versions' / BASE / 'items').glob('*.json'):
        shard.unlink()
    queue = store.list_items()
    assert queue['total'] == 1 and queue['items'][0]['occurrence_count'] == 3
    assert queue['items'][0]['problem_statuses'] == ['needs_review']
    # The verified item's old expiry is not a fact error.
    assert store.list_items(status='all')['total'] == 2
    assert store.list_items(q='activity B', status='all')['items'][0]['item_id'] == OTHER
    assert store.list_items(status='all', per_page=1, page=2)['page'] == 2
    assert store.list_items(status='all', per_page=1, page=999)['page'] == 2
    assert store.list_items(q='not present')['total'] == 0
    with pytest.raises(ReviewUnavailable):
        store.get_item(ITEM)


def test_verified_content_with_unresolved_dates_is_a_problem_but_contextual_or_elapsed_is_not(store):
    path = store.settings.reader_data_dir / _manifest_files(BASE)['catalog']
    catalog = json.loads(path.read_bytes())
    record = next(row for row in catalog['occurrences'] if row['item_id'] == OTHER)
    record['dates'] = [{'kind': 'event', 'normalized_date': None,
                        'resolution': 'year_unknown', 'literal_source': 'notice'}]
    path.write_bytes(_json(catalog))
    assert store.list_items()['total'] == 2
    result = store.list_items(status='unresolved_dates')
    assert result['total'] == 1 and result['items'][0]['item_id'] == OTHER
    assert result['items'][0]['problem_statuses'] == ['unresolved_dates']
    record['dates'][0].update(normalized_date='2020-01-02', resolution='contextual')
    path.write_bytes(_json(catalog))
    assert store.list_items()['total'] == 1
    assert store.list_items(status='unresolved_dates')['total'] == 0


def test_expected_previous_head_and_stale_source_are_distinct_conflicts(store):
    first = save_first(store)
    with pytest.raises(ReviewConflict):
        store.save(ITEM, payload('Concurrent'), actor='other', expected_head=None, base_version=BASE)
    assert len(store.get_item(ITEM)['history']) == 1
    write_pack(store.settings.reader_data_dir, NEW, changed_source=True)
    stale = store.get_item(ITEM)
    assert stale['stale'] and stale['base_version'] == NEW
    assert stale['head']['base_version'] == BASE
    with pytest.raises(ReviewConflict):
        store.save(ITEM, payload(), actor='editor', expected_head=first['revision'], base_version=BASE)
    with pytest.raises(ReviewConflict):
        store.publication_snapshot()
    second = store.save(ITEM, payload('Rechecked new source'), actor='editor',
                        expected_head=first['revision'], base_version=NEW)
    assert not store.get_item(ITEM)['stale']
    assert store.get_item(ITEM)['history'] == [second, first]
    assert store.publication_snapshot()['base_version'] == NEW


def test_history_is_append_only_and_publication_marks_exact_snapshot_not_later_draft(store):
    first = save_first(store)
    snapshot = store.publication_snapshot()
    second = store.save(ITEM, payload('Later saved draft'), actor='other-editor',
                        expected_head=first['revision'], base_version=BASE)
    store.record_preparation(actor='admin', snapshot=snapshot, result={'version': PUBLISHED})
    write_pack(store.settings.reader_data_dir, PUBLISHED, revisions=[first])
    store.record_publication(actor='admin', snapshot=snapshot, result={'version': PUBLISHED})
    item = store.get_item(ITEM)
    assert item['head_revision'] == second['revision']
    assert item['published_revision'] == first['revision'] and item['unpublished']
    assert not item['stale'] and item['base_version'] == BASE
    assert item['history'] == [second, first]
    assert item['occurrences'][0]['body_text'] == 'Original complete body'
    assert store.publication_snapshot()['revisions'] == [second]
    with closing(sqlite3.connect(store.settings.database_path)) as db:
        for statement in ('DELETE FROM reader_review_revisions',
                          "UPDATE reader_review_revisions SET actor='rewritten'",
                          'DELETE FROM reader_review_publications'):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                db.execute(statement)


def test_partial_publish_retry_uses_exact_private_preparation_not_newer_head(store):
    first = save_first(store)
    snapshot = store.publication_snapshot()
    store.record_preparation(actor='admin', snapshot=snapshot, result={'version': PUBLISHED})
    second = store.save(ITEM, payload('Later concurrent draft'), actor='editor',
                        expected_head=first['revision'], base_version=BASE)
    write_pack(store.settings.reader_data_dir, PUBLISHED, revisions=[first])
    retry = store.publication_snapshot()
    assert retry['base_manifest'] == snapshot['base_manifest']
    assert retry['active_version'] == PUBLISHED and retry['revisions'] == [first]
    assert store.get_item(ITEM)['published_revision'] is None
    store.record_publication(actor='admin', snapshot=retry, result={'version': PUBLISHED})
    assert store.get_item(ITEM)['published_revision'] == first['revision']
    assert store.get_item(ITEM)['unpublished']
    assert store.publication_snapshot()['revisions'] == [second]


def test_preparation_is_audited_but_does_not_mark_saved_edits_published(store):
    first = save_first(store)
    snapshot = store.publication_snapshot()
    store.record_preparation(actor='admin', snapshot=snapshot, result={'version': PUBLISHED})
    assert store.get_item(ITEM)['unpublished']
    assert store.get_item(ITEM)['published_revision'] is None
    assert store.publication_snapshot()['revisions'] == [first]
    with closing(sqlite3.connect(store.settings.database_path)) as db:
        event = db.execute('SELECT result_json,revisions_json FROM reader_review_publications').fetchone()
    assert json.loads(event[0])['phase'] == 'prepared'
    assert json.loads(event[1]) == {ITEM: first['revision']}


def test_external_pack_with_copied_known_stamps_is_not_private_preparation(store):
    first = save_first(store)
    snapshot = store.publication_snapshot()
    store.record_preparation(actor='admin', snapshot=snapshot, result={'version': PUBLISHED})
    write_pack(store.settings.reader_data_dir, NEW, revisions=[first], changed_source=True)
    item = store.get_item(ITEM)
    assert item['base_version'] == NEW and item['stale']
    assert item['occurrences'][0]['body_text'] == 'Changed source body'
    with pytest.raises(ReviewConflict):
        store.publication_snapshot()
    with pytest.raises(ReviewConflict):
        store.record_publication(actor='admin', snapshot=snapshot, result={'version': NEW})


def test_removed_head_requires_audited_acknowledgement_before_unrelated_publish(store):
    first = save_first(store)
    write_pack(store.settings.reader_data_dir, NEW, removed=True)
    removed = store.get_item(ITEM)
    assert removed['removed'] and not removed['retired']
    assert removed['history'] == [first]
    assert removed['original_available'] and removed['original_version'] == BASE
    assert removed['occurrences'][0]['body_text'] == 'Original complete body'
    assert store.list_items()['items'][0]['item_id'] == ITEM
    assert store.list_items(status='removed')['total'] == 1
    other = store.save(OTHER, payload('Unrelated correction'), actor='editor', expected_head=None, base_version=NEW)
    with pytest.raises(ReviewConflict):
        store.publication_snapshot()
    for expected_head, base_version in ((None, NEW), (first['revision'], BASE)):
        with pytest.raises(ReviewConflict):
            store.retire_removed(ITEM, actor='admin', expected_head=expected_head,
                                 base_version=base_version, reason='Canonical item merged.')
    with pytest.raises(ReviewConflict):
        store.retire_removed(OTHER, actor='admin', expected_head=other['revision'],
                             base_version=NEW, reason='Cannot retire a surviving item.')
    retirement = store.retire_removed(ITEM, actor='admin', expected_head=first['revision'],
                                     base_version=NEW, reason='Canonical item merged; preserve its history.')
    acknowledged = store.get_item(ITEM)
    assert acknowledged['removed'] and acknowledged['retired']
    assert acknowledged['retirement'] == retirement and acknowledged['history'] == [first]
    assert store.list_items(status='removed')['total'] == 0
    assert store.list_items(status='retired')['items'][0]['item_id'] == ITEM
    assert store.publication_snapshot()['revisions'] == [other]
    write_pack(store.settings.reader_data_dir, PUBLISHED)
    second = store.save(ITEM, payload('Reintroduced activity'), actor='editor',
                        expected_head=first['revision'], base_version=PUBLISHED)
    assert not store.get_item(ITEM)['retired']
    with pytest.raises(ReviewConflict):
        store.publication_snapshot()  # The surviving OTHER head still needs a recheck.
    store.save(OTHER, payload('Rechecked other'), actor='editor', expected_head=other['revision'], base_version=PUBLISHED)
    assert second in store.publication_snapshot()['revisions']


def test_removed_history_without_old_detail_is_not_replaced_by_fake_original(store):
    first = save_first(store)
    write_pack(store.settings.reader_data_dir, NEW, removed=True)
    (store.settings.reader_data_dir / 'versions' / BASE / 'items' / 'aa.json').unlink()
    item = store.get_item(ITEM)
    assert item['removed'] and not item['original_available']
    assert item['occurrences'] == item['catalog_records'] == []
    assert item['initial_source'] == 'saved_revision' and item['head'] == first


def test_record_publication_rejects_external_change_without_erasing_history(store):
    save_first(store)
    snapshot = store.publication_snapshot()
    write_pack(store.settings.reader_data_dir, NEW, changed_source=True)
    with pytest.raises(ReviewConflict):
        store.record_publication(actor='admin', snapshot=snapshot, result={'version': PUBLISHED})
    assert len(store.get_item(ITEM)['history']) == 1
    assert store.get_item(ITEM)['published_revision'] is None


@pytest.mark.parametrize('url', [
    'file:///etc/passwd', 'javascript:alert(1)', 'https://user:secret@nju.edu.cn/a',
    'http://localhost/a', 'http://host.local/a', 'http://host.internal/a', 'http://private/a',
    'http://127.0.0.1/a', 'http://10.1.1.1/a', 'http://169.254.169.254/a', 'http://[::1]/a',
    'http://[::ffff:127.0.0.1]/a', 'http://127.1/a', 'http://2130706433/a', 'http://0x7f000001/a',
    'https://www.nju.edu.cn/\nsecret', 'https://www.nju.edu.cn/\x7f',
    'https://www.nju.edu.cn\\@127.0.0.1/a', 'http://localhost./a',
    'http://%31%32%37.0.0.1/a', 'https://%6cocalhost/a', 'https://%77ww.nju.edu.cn/a',
    'http://0177.0.0.1/a', 'http://0x7f.1/a', 'http://０１７７.０.０.１/a',
])
def test_unsafe_public_links_are_rejected(url):
    value = payload()
    value['activity']['source_urls'] = [url]
    with pytest.raises(ReviewValidationError):
        validate_payload(value)
    with pytest.raises(ReviewValidationError):
        validate_public_url(url)


@pytest.mark.parametrize('field,value', [
    ('title', ''), ('title', 'x' * 251), ('summary', None), ('summary', 'x' * 1501),
    ('content', ' '), ('content', 'x' * 60001), ('venue', 4), ('category', 'x' * 201),
    ('time', 'x' * 1001), ('organizer', 'x' * 501), ('audience', 'x' * 1501),
    ('participation', 'x' * 3001), ('dates', [{'kind': 'event', 'date': '2026-02-30'}]),
    ('dates', [{'kind': 'event', 'date': '20261025'}]),
    ('dates', [{'kind': 'event', 'date': '2026-1-01'}]),
    ('dates', [{'kind': 'unknown', 'date': '2026-01-01'}]),
    ('dates', [{'kind': 'event', 'date': '2026-01-01', 'extra': True}]),
    ('dates', [{'kind': 'event', 'date': '2026-01-01'}] * 101), ('source_urls', ['https://nju.edu.cn/'] * 101),
])
def test_malformed_activity_fields_and_dates_are_rejected(field, value):
    data = payload()
    data['activity'][field] = value
    with pytest.raises(ReviewValidationError):
        validate_payload(data)


@pytest.mark.parametrize('change', [
    {'extra': 'unexpected'}, {'reason': ''}, {'reason': 'x' * 2001}, {'status': 'approved'},
])
def test_top_level_payload_is_strict(change):
    with pytest.raises(ReviewValidationError):
        validate_payload(payload() | change)


def test_validation_normalizes_unknown_optional_fields_and_deduplicates():
    value = payload()
    value['activity']['organizer'] = '  '
    value['activity']['dates'] *= 2
    value['activity']['source_urls'] *= 2
    clean = validate_payload(value)
    assert clean['activity']['organizer'] is None
    assert len(clean['activity']['dates']) == len(clean['activity']['source_urls']) == 1
    assert value['activity']['organizer'] == '  '
    assert len(value['activity']['dates']) == 2


@pytest.mark.parametrize('rejection', [None, 'legacy_rules', 'missing_rules', 'unverified_source', 'corrected_source'])
def test_initial_payload_prefers_only_current_admitted_full_activity_without_truncation(store, rejection):
    shard_path = store.settings.reader_data_dir / 'versions' / BASE / 'items' / 'aa.json'
    shard = json.loads(shard_path.read_bytes())
    admitted = shard[ITEM]['occurrences'][1]
    admitted['content_status'] = 'verified'
    human = payload()['activity']
    activity = {key: {'value': value, 'evidence': []} for key, value in human.items()
                if key not in {'category', 'dates', 'source_urls'}}
    activity['content']['value'] = 'Full admitted content ' * 300
    admitted['date_review'] = {'rules_version': 'fixture-rules', 'source_review': {
        'status': 'source_verified', 'activity': activity,
        'sources': [{'url': 'https://www.nju.edu.cn/a'}]}}
    if rejection == 'legacy_rules':
        admitted['date_review']['rules_version'] = 'legacy-content-evidence'
    elif rejection == 'missing_rules':
        admitted['date_review'].pop('rules_version')
    elif rejection == 'unverified_source':
        admitted['date_review']['source_review']['status'] = 'secondary_only'
    elif rejection == 'corrected_source':
        admitted['source_corrections'] = [{'original_url': 'https://www.nju.edu.cn/a',
                                         'corrected_url': 'https://www.nju.edu.cn/corrected'}]
    shard_path.write_bytes(_json(shard))
    item = store.get_item(ITEM)
    if rejection is None:
        assert item['initial_source'] == 'admitted_activity'
        assert item['initial_payload']['activity']['content'] == activity['content']['value']
        assert item['initial_payload']['activity']['title'] == human['title']
    else:
        assert item['initial_source'] == 'original_excerpt'
        assert item['initial_payload']['activity']['content'] == 'Original complete body'
        assert item['initial_payload']['activity']['dates'] == []


def test_public_reads_reject_symlink_shards_and_mismatched_canonical_scope(store, tmp_path):
    root = store.settings.reader_data_dir
    shard = root / 'versions' / BASE / 'items' / 'aa.json'
    outside = tmp_path / 'untrusted.json'
    outside.write_bytes(shard.read_bytes())
    shard.unlink()
    shard.symlink_to(outside)
    with pytest.raises(ReviewUnavailable):
        store.get_item(ITEM)
    shard.unlink()
    value = json.loads(outside.read_bytes())
    value[ITEM]['occurrences'].pop()
    shard.write_bytes(_json(value))
    with pytest.raises(ReviewUnavailable):
        store.get_item(ITEM)
    with pytest.raises(ReviewNotFound):
        store.get_item('../database.db')


def test_missing_configuration_and_reads_create_no_private_state(tmp_path, monkeypatch):
    monkeypatch.setenv('NANNA_DATA_DIR', str(tmp_path / 'absent-private'))
    for name in ('READER_DATA_DIR', 'READER_SITE_DATA_DIR', 'READER_SEARCH_BUILDER', 'READER_URL'):
        monkeypatch.delenv('NANNA_' + name, raising=False)
    settings = runtime.load_settings()
    assert settings.reader_data_dir is settings.reader_site_data_dir is settings.reader_search_builder is None
    assert settings.reader_url == ''
    with pytest.raises(ReviewUnavailable, match='NANNA_READER_DATA_DIR'):
        ReviewStore(settings).list_items()
    assert not settings.data_dir.exists()
    root = tmp_path / 'public'
    write_pack(root)
    with pytest.raises(ReviewUnavailable, match='migrate'):
        ReviewStore(replace(settings, reader_data_dir=root)).list_items()
    assert not settings.data_dir.exists()


def test_outdated_private_schema_is_not_migrated_by_public_reads(tmp_path, monkeypatch):
    monkeypatch.setenv('NANNA_DATA_DIR', str(tmp_path / 'old-private'))
    monkeypatch.setenv('NANNA_READER_DATA_DIR', str(tmp_path / 'public'))
    monkeypatch.delenv('NANNA_READER_SITE_DATA_DIR', raising=False)
    settings = runtime.load_settings()
    settings.data_dir.mkdir()
    write_pack(settings.reader_data_dir)
    with closing(sqlite3.connect(settings.database_path)) as db, db:
        db.execute('CREATE TABLE nannanews_schema(id INTEGER PRIMARY KEY, version INTEGER NOT NULL)')
        db.execute('INSERT INTO nannanews_schema VALUES(1,1)')
    with pytest.raises(ReviewUnavailable, match='migrate'):
        ReviewStore(settings).list_items()
    with closing(sqlite3.connect(settings.database_path)) as db:
        assert db.execute('SELECT version FROM nannanews_schema').fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='reader_review_revisions'").fetchone()


def test_reader_configuration_paths_preserve_symlinks_and_reader_url_is_separate(tmp_path, monkeypatch):
    monkeypatch.setenv('NANNA_DATA_DIR', str(tmp_path / 'uncreated'))
    target = tmp_path / 'public'
    write_pack(target)
    link = tmp_path / 'public-link'
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv('NANNA_READER_DATA_DIR', str(link))
    monkeypatch.setenv('NANNA_READER_SITE_DATA_DIR', str(tmp_path / 'site' / 'data'))
    monkeypatch.setenv('NANNA_READER_SEARCH_BUILDER', str(tmp_path / 'build-search.mjs'))
    monkeypatch.setenv('NANNA_READER_URL', 'https://reader.example.edu/nested/')
    settings = runtime.load_settings()
    assert settings.reader_data_dir == link
    assert settings.reader_url == 'https://reader.example.edu/nested'
    with pytest.raises(ReviewUnavailable, match='符号链接'):
        ReviewStore(settings).list_items()
    assert not settings.data_dir.exists()
    for url in ('https://user:pass@example.edu/', 'https://example.edu/#private',
                'https://example.edu/?query=1', 'file:///tmp/site',
                'https://example.edu:invalid/', 'https://exam\nple.edu/', 'https://example.edu\\@private/'):
        monkeypatch.setenv('NANNA_READER_URL', url)
        with pytest.raises(runtime.ConfigurationError, match='NANNA_READER_URL'):
            runtime.load_settings()


def test_private_public_directories_cannot_overlap(store):
    unsafe = replace(store.settings, reader_data_dir=store.settings.data_dir)
    with pytest.raises(ReviewUnavailable, match='相互独立'):
        ReviewStore(unsafe).list_items()


def test_private_schema_upgrade_does_not_repeat_initial_migrators(tmp_path, monkeypatch):
    monkeypatch.setenv('NANNA_DATA_DIR', str(tmp_path / 'private-upgrade'))
    monkeypatch.setattr(runtime, 'settings', runtime.load_settings())
    runtime.prepare_runtime()
    with closing(sqlite3.connect(runtime.settings.database_path)) as db, db:
        db.execute('CREATE TABLE nannanews_schema(id INTEGER PRIMARY KEY, version INTEGER NOT NULL)')
        db.execute('INSERT INTO nannanews_schema VALUES(1,1)')
        db.execute('CREATE TABLE sentinel(value TEXT)')
        db.execute("INSERT INTO sentinel VALUES('preserved')")
    from nannanews import acquisition, bulletin_store, daily_acquisition, information

    def forbidden(*args, **kwargs):
        raise AssertionError('initial migrator/backfill repeated on private console upgrade')

    monkeypatch.setattr(runtime, '_migrate_core', forbidden)
    for module in (acquisition, bulletin_store, daily_acquisition, information):
        monkeypatch.setattr(module, '_migrate_schema', forbidden)
    assert schema.migrate() == schema.migrate() == schema.CURRENT_VERSION
    with closing(schema.open_database(readonly=True)) as db:
        assert schema.require_schema(db) == schema.CURRENT_VERSION
        assert db.execute('SELECT value FROM sentinel').fetchone()[0] == 'preserved'
        assert db.execute('SELECT count(*) FROM reader_review_revisions').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM reader_private_activities').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM reader_private_evidence_links').fetchone()[0] == 0
