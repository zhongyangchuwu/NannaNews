"""Explicit public publishing against a tiny pack and migrated private audit DB.

Real local Node builders exercise search/share outputs. Only failures at the OS
process/commit boundary are injected; no model, network or canonical DB is used.
"""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

from bs4 import BeautifulSoup
import pytest

from nannanews import public_metadata_export, reader_review_export as publisher, static_export
from nannanews.reader_reviews import ReviewError, ReviewStore
from nannanews.static_export import GENERATOR, _json, _manifest_files

BASE = '1' * 64
ITEM = 'item-' + 'a' * 24
OTHER = 'item-' + 'b' * 24
OCCURRENCES = ['occ-' + value * 24 for value in ('a', 'b', 'c')]
ISSUES = ['issue-' + value * 24 for value in ('a', 'b')]
DOCUMENTS = ['2' * 64, '3' * 64]
PRIVATE = 'PRIVATE_REVIEWER_USERNAME_NOT_FOR_EXPORT'
READER = Path(__file__).resolve().parents[1] / 'reader'


def load(root):
    manifest = json.loads((root / 'manifest.json').read_bytes())
    catalog = json.loads((root / manifest['files']['catalog']).read_bytes())
    details = {}
    for relative in {item['detail_path'] for item in catalog['items']}:
        details.update(json.loads((root / relative).read_bytes()))
    return manifest, catalog, details

def load_site(root):
    script = r"""import fs from 'node:fs'; import path from 'node:path';
import {decodePartition, decodeItemDetail} from './src/public-format.mjs';
const root = process.argv[1], read = p => JSON.parse(fs.readFileSync(path.join(root, p), 'utf8'));
const manifest = read('manifest.json'), parts = Object.fromEntries(Object.entries(manifest.files.partitions).map(([k,p]) => [k,read(p)]));
const owners = new Map(Object.values(parts).flatMap(p => p.messages).map(m => [m.id,m]));
const records = Object.entries(parts).flatMap(([k,p]) => decodePartition(p,k,owners).map((row,i) => [p.occurrences[i].order,row])).sort((a,b) => a[0]-b[0]).map(x => x[1]);
const byId = new Map(records.map(row => [row.id,row]));
const items = read(manifest.files.item_index), details = {};
for (const item of items) {
  const extras = item.detail_path ? read(item.detail_path)[item.id] : undefined;
  details[item.id] = decodeItemDetail(item,item.occurrences.map(ref => byId.get(ref.id)),extras);
}
console.log(JSON.stringify({manifest, records, details}));"""
    result = subprocess.run(['node', '--input-type=module', '-e', script, str(root)],
                            cwd=READER, capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


def payload(status='verified', title='人工修正的活动'):
    return {'status': status, 'reason': '管理员已核对公开材料，纠正活动内容。', 'activity': {
        'title': title, 'summary': '人工确认的简介', 'content': '人工确认的完整正文 HumanSearchNeedle',
        'category': '讲座', 'time': '18:30', 'venue': '报告厅', 'organizer': '活动主办方',
        'audience': '全体学生', 'participation': '在公开链接报名',
        'source_urls': ['https://events.example.edu/corrected'],
        'dates': [{'kind': 'event', 'date': '2026-12-20'}, {'kind': 'deadline', 'date': '2026-12-18'}],
    }}


def make_pack(root):
    version_root = root / 'versions' / BASE
    (version_root / 'items').mkdir(parents=True)
    (version_root / 'assets').mkdir()
    image = b'public immutable poster bytes'
    pdf = b'%PDF-1.4 immutable public evidence'
    image_path = 'assets/' + hashlib.sha256(image).hexdigest() + '.png'
    pdf_path = 'assets/' + hashlib.sha256(pdf).hexdigest() + '.pdf'
    (version_root / image_path).write_bytes(image)
    (version_root / pdf_path).write_bytes(pdf)
    prefix = 'versions/' + BASE + '/'
    records, details = [], {}
    for index, occurrence_id in enumerate(OCCURRENCES):
        item_id = ITEM if index < 2 else OTHER
        row = {'id': occurrence_id, 'item_id': item_id, 'issue_id': ISSUES[index % 2],
               'document_id': DOCUMENTS[index % 2], 'kind': 'detail', 'title': '原始标题' + str(index),
               'preview': '原始摘要', 'category': '原分类', 'content_status': 'verified',
               'issue_date': '2026-10-01', 'source_urls': ['https://original.example.edu/item'],
               'source_hosts': ['original.example.edu'], 'colleges': ['原学院'],
               'dates': [{'kind': 'event', 'normalized_date': '2026-10-10',
                          'resolution': 'explicit', 'literal_source': 'primary'}],
               'expiry_date': '2026-10-10', 'image_count': 1,
               'activity_facts': dict(venue='原地点', time=None, audience=None, participation=None)}
        records.append(row)
        occurrence = deepcopy(row)
        occurrence.update(body_text='原始正文不应修改 ' + str(index),
            original_dates=[{'kind': 'event', 'raw_text': '10月10日', 'normalized_date': '2026-10-10',
                             'resolution': 'explicit', 'literal_source': 'bulletin', 'evidence': '原刊证据'}],
            college_mentions=[], images=[{'id': 'image-reference', 'path': prefix + image_path,
                                         'mime': 'image/png', 'width': 1, 'height': 1}], primary_images=[],
            locator={'anchor': 'original'}, table_cells=[],
            date_review={'status': 'confirmed', 'reason': '历史模型判断保留',
                         'rules_version': 'fixture-original-rules',
                         'source_review': {'status': 'source_verified', 'reason': '原始来源证据留存，尚无完整活动内容。',
                             'activity': None, 'sources': [{
                                 'url': 'https://original.example.edu/item',
                                 'resolved_url': 'https://original.example.edu/item', 'title': '原始来源',
                                 'sha256': '4' * 64, 'fetched_at': '2026-10-01T00:00:00+00:00',
                                 'published_date': '2026-10-01', 'quotes': ['历史逐字证据']}]}})
        occurrence['dates'][0].update(raw_text='2026-10-10', evidence='历史逐字证据')
        detail = details.setdefault(item_id, {'id': item_id, 'latest_occurrence_id': occurrence_id,
                                              'occurrences': []})
        detail['occurrences'].append(occurrence)
    items = [{'id': identity, 'latest_occurrence_id': detail['latest_occurrence_id'],
              'occurrence_count': len(detail['occurrences']), 'aliases': [],
              'detail_path': prefix + 'items/' + identity[5:7] + '.json'}
             for identity, detail in details.items()]
    catalog = {'items': items, 'occurrences': records}
    issues = [{'id': identity, 'title': '原始刊期' + str(index), 'issue_date': '2026-10-01',
               'issue_number': index + 1, 'occurrence_count': 2 if index == 0 else 1,
               'documents': [{'id': DOCUMENTS[index], 'format': 'pdf' if index == 0 else 'html',
                              'filename': 'original.pdf' if index == 0 else 'original.html',
                              'claimed_issue_date': '2026-10-01', 'issue_date': '2026-10-01',
                              'source_urls': ['https://original.example.edu/item'], 'warnings': [],
                              'pdf_path': prefix + pdf_path if index == 0 else None}]}
              for index, identity in enumerate(ISSUES)]
    values = {'catalog.json': catalog, 'issues.json': issues,
              'search-documents.json': [{'id': row['id'], 'title': row['title'],
                                         'text': '原始搜索正文'} for row in records],
              'search-index.json': {'old_index': True}}
    for item_id, detail in details.items():
        values['items/' + item_id[5:7] + '.json'] = {item_id: detail}
    for name, value in values.items():
        (version_root / name).write_bytes(_json(value))
    manifest = {'schema_version': 2, 'generator': GENERATOR, 'data_version': BASE,
                'content_rules_version': 'fixture-original-rules', 'generated_at': '2026-10-01T00:00:00+00:00',
                'counts': {'items': 2, 'occurrences': 3, 'issues': 2, 'documents': 2, 'images': 1, 'pdfs': 1},
                'files': _manifest_files(BASE)}
    (root / 'manifest.json').write_bytes(_json(manifest))
    return image_path


@pytest.fixture
def pack(tmp_path, monkeypatch):
    from nannanews import runtime, schema
    monkeypatch.setenv('NANNA_DATA_DIR', str(tmp_path / 'private'))
    monkeypatch.setenv('NANNA_MODEL_BASE_URL', '')
    monkeypatch.setenv('NANNA_MODEL_API_KEY', '')
    source, site = tmp_path / 'source-data', tmp_path / 'site' / 'data'
    image = make_pack(source)
    site.parent.mkdir()
    shutil.copytree(source, site)
    settings = replace(runtime.load_settings(), reader_data_dir=source,
                       reader_site_data_dir=site, reader_search_builder=READER / 'scripts' / 'build-search.mjs')
    monkeypatch.setattr(runtime, 'settings', settings)
    schema.migrate()
    store = ReviewStore(settings)
    original = {path.relative_to(source): path.read_bytes() for path in (source / 'versions' / BASE).rglob('*')
                if path.is_file()}
    return settings, store, original, image


@pytest.fixture
def node_builders():
    if shutil.which('node') is None or not (READER / 'node_modules' / 'minisearch').is_dir():
        pytest.skip('Real local Node and installed reader MiniSearch dependency are required.')


def save(store, value=None, expected=None):
    return store.save(ITEM, value or payload(), actor=PRIVATE, expected_head=expected, base_version=BASE)


def published_map(settings):
    with closing(sqlite3.connect(settings.database_path)) as db:
        rows = db.execute('SELECT revisions_json,result_json FROM reader_review_publications ORDER BY id').fetchall()
    return [json.loads(revisions) for revisions, result in rows if json.loads(result).get('phase') == 'completed']


@pytest.mark.parametrize('status', ['verified', 'needs_review'])
def test_publish_all_occurrences_preserves_raw_history_and_updates_search_share(pack, node_builders, status):
    settings, store, original, image = pack
    revision = save(store, payload(status))
    source = settings.reader_data_dir
    result = publisher.publish_reviews(settings=settings, actor=PRIVATE)
    assert result == {**result, 'source_published': True, 'site_published': True,
                      'share_updated': True, 'recorded': True, 'partial': False,
                      'revision_count': 1, 'occurrence_count': 2}
    manifest, catalog, details = load(source)
    assert result['version'] == manifest['data_version'] != BASE
    site = load_site(settings.reader_site_data_dir)
    assert site['manifest']['schema_version'] == 3
    assert site['manifest']['data_version'] == manifest['data_version']
    assert site['manifest']['counts'] == manifest['counts']
    assert site['records'] == catalog['occurrences']
    assert set(manifest['files']) == {'catalog', 'issues', 'search_documents', 'search_index'}
    version_dir = settings.reader_site_data_dir / 'versions' / result['version']
    assert not any((version_dir / name).exists() for name in
                   ('catalog.json', 'search-documents.json', 'activity-facts.json', 'issue-catalogs', 'items'))
    assert manifest['review_base_version'] == BASE
    for row in catalog['occurrences'][:2]:
        assert row['title'] == payload()['activity']['title']
        assert row['preview'] == payload()['activity']['summary']
        assert row['manual_review'] == {'revision': revision['revision'], 'status': status,
            'reviewed_at': revision['created_at'], 'reason': payload()['reason']}
        assert 'activity' not in row['manual_review']
        assert row['source_urls'] == payload()['activity']['source_urls']
        if status == 'verified':
            assert row['content_status'] == 'human_verified'
            assert row['activity_facts'] == {key: payload()['activity'][key]
                                            for key in ('venue', 'audience', 'participation', 'time')}
            assert all(value['literal_source'] == 'manual' and value['resolution'] == 'explicit'
                       for value in row['dates'])
        else:
            assert row['content_status'] == 'needs_review'
            assert row['dates'] == [] and row['expiry_date'] is None and 'activity_facts' not in row
    base_detail = json.loads(original[Path('versions') / BASE / 'items' / 'aa.json'])[ITEM]
    for old, new in zip(base_detail['occurrences'], details[ITEM]['occurrences']):
        for key in ('title', 'preview', 'body_text', 'date_review', 'original_dates', 'locator', 'table_cells'):
            assert new[key] == old[key]
        assert new['manual_review']['activity'] == payload()['activity']
        assert 'actor' not in new['manual_review']
    for identity, source_detail in details.items():
        for source_row, site_row in zip(source_detail['occurrences'], site['details'][identity]['occurrences']):
            for key in source_row:
                assert site_row[key] == source_row[key]
    assert catalog['occurrences'][2] == site['records'][2]
    assert catalog['occurrences'][2] == json.loads(original[Path('versions') / BASE / 'catalog.json'])['occurrences'][2]
    for relative, contents in original.items():
        assert (source / relative).read_bytes() == contents
    for root in (source, settings.reader_site_data_dir):
        public_bytes = b''.join(path.read_bytes() for path in (root / 'versions' / result['version']).rglob('*.json'))
        assert PRIVATE.encode() not in public_bytes
    new_image = source / 'versions' / result['version'] / image
    assert os.stat(new_image).st_ino == os.stat(source / 'versions' / BASE / image).st_ino
    assert os.stat(new_image).st_ino == os.stat(settings.reader_site_data_dir / 'versions' / result['version'] / image).st_ino
    index = json.loads((source / manifest['files']['search_index']).read_bytes())
    assert index['documentCount'] == 3 and index['serializationVersion'] == 2
    search = json.loads((source / manifest['files']['search_documents']).read_bytes())
    assert search[0]['title'] == payload()['activity']['title'] and 'HumanSearchNeedle' in search[0]['text']
    # Load and query the real serialized index with the same tokenizer as the SPA.
    check = """import fs from 'node:fs'; import MiniSearch from 'minisearch';
import {searchOptions} from './src/tokenize.mjs';
const index=MiniSearch.loadJSON(fs.readFileSync(process.argv[1],'utf8'),searchOptions);
console.log(JSON.stringify(index.search('HumanSearchNeedle').map(row=>row.id)));"""
    hits = subprocess.run(['node', '--input-type=module', '-e', check, str(source / manifest['files']['search_index'])],
                          cwd=READER, capture_output=True, text=True, check=True, timeout=30)
    assert set(json.loads(hits.stdout)) == set(OCCURRENCES[:2])
    for occurrence_id in OCCURRENCES[:2]:
        share = settings.reader_site_data_dir.parent / 'share' / 'items' / ITEM / occurrence_id / 'index.html'
        html = share.read_text()
        assert payload()['activity']['title'] in html and payload()['activity']['summary'] in html
        assert PRIVATE not in html
    assert not (settings.reader_site_data_dir.parent / 'share' / 'items' / OTHER).exists()
    assert published_map(settings) == [{ITEM: revision['revision']}]
    assert store.get_item(ITEM)['unpublished'] is False


def test_legacy_site_migrates_and_next_publication_updates_v3(pack, node_builders):
    settings, store, _, _ = pack
    for root in (settings.reader_data_dir, settings.reader_site_data_dir):
        manifest, _, _ = load(root)
        prefix = 'versions/' + BASE + '/'
        manifest['files'].update(item_index=prefix + 'item-index.json', issue_catalogs={
            issue: prefix + 'issue-catalogs/' + issue + '.json' for issue in ISSUES})
        (root / 'manifest.json').write_bytes(_json(manifest))
    assert store.get_item(ITEM)['base_version'] == BASE
    first_revision = save(store)
    first = publisher.publish_reviews(settings=settings, actor=PRIVATE)
    first_site = load_site(settings.reader_site_data_dir)
    assert first_site['manifest']['schema_version'] == 3
    second_revision = save(store, payload(title='第二次人工修订'), expected=first_revision['revision'])
    second = publisher.publish_reviews(settings=settings, actor=PRIVATE)
    manifest, catalog, _ = load(settings.reader_data_dir)
    site = load_site(settings.reader_site_data_dir)
    assert second['version'] != first['version']
    assert site['manifest']['schema_version'] == 3 and site['manifest']['data_version'] == second['version']
    assert site['records'] == catalog['occurrences']
    assert (settings.reader_site_data_dir / 'versions' / first['version']).is_dir()
    for reviewed in (row for row in site['records'] if row['item_id'] == ITEM):
        assert reviewed['title'] == '第二次人工修订'
        assert reviewed['manual_review']['revision'] == second_revision['revision']
        assert 'activity' not in reviewed['manual_review']


@pytest.mark.parametrize('fail_site_once', [False, True])
def test_republish_legacy_same_version_preserves_v2_and_retries(pack, node_builders, monkeypatch, fail_site_once):
    settings, store, _, _ = pack
    revision = save(store)
    first = publisher.publish_reviews(settings=settings, actor=PRIVATE)
    version = first['version']
    source = settings.reader_data_dir
    site = settings.reader_site_data_dir
    manifest, catalog, _ = load(source)
    # Model the previously published #4 layout at this exact reviewed version.
    # Its v2 projections collide with the old v3 paths but must remain immutable.
    site_version = site / 'versions' / version
    shutil.rmtree(site_version)
    shutil.copytree(source / 'versions' / version, site_version)
    prefix = 'versions/' + version + '/'
    issues = json.loads((source / manifest['files']['issues']).read_bytes())
    for root in (source, site):
        version_root = root / 'versions' / version
        (version_root / 'item-index.json').write_bytes(_json(catalog['items']))
        projections = {}
        for issue in issues:
            relative = 'issue-catalogs/' + issue['id'] + '.json'
            target = version_root / relative
            target.parent.mkdir(exist_ok=True)
            rows = [row for row in catalog['occurrences'] if row['issue_id'] == issue['id']]
            target.write_bytes(_json({'items': catalog['items'], 'occurrences': rows}))
            projections[issue['id']] = prefix + relative
        old_manifest = dict(manifest, files={**manifest['files'], 'item_index': prefix + 'item-index.json',
                                             'issue_catalogs': projections})
        (root / 'manifest.json').write_bytes(_json(old_manifest))
    old_versions = {root: {path.relative_to(root): path.read_bytes()
                           for path in (root / 'versions' / version).rglob('*') if path.is_file()}
                    for root in (source, site)}
    native_publish = publisher._publish
    if fail_site_once:
        def fail_site(stage, output, published_version, previous):
            if output == site:
                raise OSError('injected site switch failure')
            return native_publish(stage, output, published_version, previous)
        monkeypatch.setattr(publisher, '_publish', fail_site)
        with pytest.raises(publisher.ReviewPublishError) as caught:
            publisher.publish_reviews(settings=settings, actor=PRIVATE)
        assert caught.value.result['version'] == version
        assert caught.value.result['source_published'] and not caught.value.result['site_published']
        assert json.loads((site / 'manifest.json').read_bytes())['schema_version'] == 2
        monkeypatch.setattr(publisher, '_publish', native_publish)
    result = publisher.publish_reviews(settings=settings, actor=PRIVATE)
    assert result['version'] == version and result['recorded'] and not result['partial']
    assert store.get_item(ITEM)['head_revision'] == revision['revision']
    assert store.get_item(ITEM)['published_revision'] == revision['revision']
    deployed = load_site(site)
    assert deployed['manifest']['schema_version'] == 3 and deployed['manifest']['data_version'] == version
    assert deployed['records'] == catalog['occurrences']
    assert all(path.startswith(prefix + 'reader-v3/') for path in
               [deployed['manifest']['files']['issues'], deployed['manifest']['files']['item_index'],
                deployed['manifest']['files']['search_index'], *deployed['manifest']['files']['partitions'].values()])
    for root, contents in old_versions.items():
        assert all((root / path).read_bytes() == data for path, data in contents.items())


def test_same_source_and_site_rejected_before_mutation(pack):
    settings, store, original, _ = pack
    save(store)
    settings = replace(settings, reader_site_data_dir=settings.reader_data_dir)
    before = (settings.reader_data_dir / 'manifest.json').read_bytes()
    with pytest.raises(publisher.ReviewPublishError):
        publisher.publish_reviews(settings=settings, actor=PRIVATE)
    assert (settings.reader_data_dir / 'manifest.json').read_bytes() == before
    assert all((settings.reader_data_dir / path).read_bytes() == data for path, data in original.items())
    assert published_map(settings) == []


def test_preparation_failure_does_not_switch_either_live_manifest(pack, monkeypatch):
    settings, store, _, _ = pack
    save(store)
    before = [path.read_bytes() for path in (settings.reader_data_dir / 'manifest.json',
                                            settings.reader_site_data_dir / 'manifest.json')]
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(publisher.subprocess, 'run', fail)
    with pytest.raises(publisher.ReviewPublishError) as caught:
        publisher.publish_reviews(settings=settings, actor=PRIVATE)
    assert caught.value.result['partial'] is False
    assert [path.read_bytes() for path in (settings.reader_data_dir / 'manifest.json',
                                          settings.reader_site_data_dir / 'manifest.json')] == before
    assert published_map(settings) == [] and store.get_item(ITEM)['unpublished'] is True


@pytest.mark.parametrize('failure', ['site', 'share', 'audit'])
def test_partial_failure_is_honest_and_explicit_retry_finishes_exact_snapshot(pack, node_builders, monkeypatch, failure):
    settings, store, original, _ = pack
    revision = save(store)
    native_publish, native_share, native_record = publisher._publish, publisher._install_shares, ReviewStore.record_publication
    def publish(stage, output, version, previous):
        if failure == 'site' and output == settings.reader_site_data_dir:
            raise OSError('injected site switch failure')
        return native_publish(stage, output, version, previous)
    def share(*args):
        raise OSError('injected page install failure')
    def record(*args, **kwargs):
        raise OSError('injected private audit failure')
    monkeypatch.setattr(publisher, '_publish', publish)
    if failure == 'share':
        monkeypatch.setattr(publisher, '_install_shares', share)
    if failure == 'audit':
        monkeypatch.setattr(ReviewStore, 'record_publication', record)
    with pytest.raises(publisher.ReviewPublishError) as caught:
        publisher.publish_reviews(settings=settings, actor=PRIVATE)
    state = caught.value.result
    assert state['partial'] is True and state['source_published'] is True and state['recorded'] is False
    assert state['site_published'] is (failure != 'site')
    assert state['share_updated'] is (failure == 'audit')
    assert published_map(settings) == []
    assert store.get_item(ITEM)['base_version'] == BASE and store.get_item(ITEM)['unpublished'] is True
    later = save(store, payload(title='稍后保存但未发布'), expected=revision['revision'])
    monkeypatch.setattr(publisher, '_publish', native_publish)
    monkeypatch.setattr(publisher, '_install_shares', native_share)
    monkeypatch.setattr(ReviewStore, 'record_publication', native_record)
    retry = publisher.publish_reviews(settings=settings, actor=PRIVATE)
    assert retry['version'] == state['version'] and retry['recorded'] is True
    assert published_map(settings) == [{ITEM: revision['revision']}]
    assert store.get_item(ITEM)['head_revision'] == later['revision']
    assert store.get_item(ITEM)['published_revision'] == revision['revision']
    assert store.get_item(ITEM)['unpublished'] is True
    assert load(settings.reader_data_dir)[1]['occurrences'][0]['title'] == payload()['activity']['title']
    for relative, contents in original.items():
        assert (settings.reader_data_dir / relative).read_bytes() == contents


def test_concurrent_save_stays_unpublished_and_does_not_change_published_snapshot(pack, node_builders, monkeypatch):
    settings, store, _, _ = pack
    first = save(store)
    native_run = publisher._run_builder
    later = []
    def run(node, script, arguments, label):
        if not later:
            later.append(save(store, payload(title='稍后保存的新标题'), expected=first['revision']))
        return native_run(node, script, arguments, label)
    monkeypatch.setattr(publisher, '_run_builder', run)
    result = publisher.publish_reviews(settings=settings, actor=PRIVATE)
    assert result['recorded'] is True
    assert published_map(settings) == [{ITEM: first['revision']}]
    assert store.get_item(ITEM)['head_revision'] == later[0]['revision']
    assert store.get_item(ITEM)['unpublished'] is True
    assert load(settings.reader_data_dir)[1]['occurrences'][0]['title'] == payload()['activity']['title']
    second = publisher.publish_reviews(settings=settings, actor=PRIVATE)
    assert second['version'] != result['version']
    assert published_map(settings)[-1] == {ITEM: later[0]['revision']}
    assert len(store.get_item(ITEM)['history']) == 2


def test_second_publisher_fails_without_waiting_or_mutating_pack(pack, monkeypatch):
    settings, store, _, _ = pack
    save(store)
    monkeypatch.setattr(publisher.shutil, 'which', lambda command: '/unused/node')
    path = settings.data_dir / '.reader-review-publish.lock'
    with path.open('w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(publisher.ReviewPublishError, match='正在发布'):
            publisher.publish_reviews(settings=settings, actor=PRIVATE)
    assert load(settings.reader_data_dir)[0]['data_version'] == BASE
    assert published_map(settings) == []


@pytest.mark.parametrize('damage', ['catalog_count', 'missing_occurrence', 'asset_escape', 'duplicate_search', 'symlink_site'])
def test_malformed_public_boundaries_never_switch_manifest(pack, monkeypatch, damage):
    settings, store, _, _ = pack
    save(store)
    root = settings.reader_data_dir / 'versions' / BASE
    if damage == 'catalog_count':
        path = root / 'catalog.json'
        value = json.loads(path.read_bytes())
        value['items'].pop()
    elif damage in {'missing_occurrence', 'asset_escape'}:
        path = root / 'items' / 'aa.json'
        value = json.loads(path.read_bytes())
        if damage == 'missing_occurrence':
            value[ITEM]['occurrences'].pop()
        else:
            value[ITEM]['occurrences'][0]['images'][0]['path'] = 'versions/' + BASE + '/../private.png'
    elif damage == 'duplicate_search':
        path = root / 'search-documents.json'
        value = json.loads(path.read_bytes())
        value[1]['id'] = value[0]['id']
    else:
        path = settings.reader_site_data_dir / 'manifest.json'
        path.unlink()
        path.symlink_to(settings.reader_data_dir / 'manifest.json')
    if damage != 'symlink_site':
        path.write_bytes(_json(value))
    before = (settings.reader_data_dir / 'manifest.json').read_bytes()
    def unexpected(*args, **kwargs):
        raise AssertionError('Malformed pack reached Node execution')
    monkeypatch.setattr(publisher.subprocess, 'run', unexpected)
    with pytest.raises(ReviewError):
        publisher.publish_reviews(settings=settings, actor=PRIVATE)
    assert (settings.reader_data_dir / 'manifest.json').read_bytes() == before
    assert published_map(settings) == []


def test_external_manifest_change_during_preparation_aborts_before_own_switch(pack, node_builders, monkeypatch):
    settings, store, _, _ = pack
    save(store)
    native_run = publisher._run_builder
    def run(node, script, arguments, label):
        native_run(node, script, arguments, label)
        if script.name == 'build-share.mjs':
            path = settings.reader_data_dir / 'manifest.json'
            manifest = json.loads(path.read_bytes())
            manifest['generated_at'] = '2026-10-02T00:00:00+00:00'
            path.write_bytes(_json(manifest))
    monkeypatch.setattr(publisher, '_run_builder', run)
    with pytest.raises(publisher.ReviewPublishError) as caught:
        publisher.publish_reviews(settings=settings, actor=PRIVATE)
    assert caught.value.result['partial'] is False
    assert load(settings.reader_data_dir)[0]['data_version'] == BASE
    assert load(settings.reader_site_data_dir)[0]['data_version'] == BASE
    assert published_map(settings) == []


def test_share_builder_default_full_build_and_explicit_subset_use_same_metadata(pack, node_builders, tmp_path):
    settings, _, _, _ = pack
    script = READER / 'scripts' / 'build-share.mjs'
    full = tmp_path / 'full'
    subset = tmp_path / 'subset'
    full.mkdir()
    subset.mkdir()
    selection = tmp_path / 'subset.json'
    selection.write_bytes(_json({'item_ids': [ITEM], 'issue_ids': ISSUES}))
    subprocess.run(['node', str(script), '--data-root', str(settings.reader_data_dir), '--output-root', str(full)],
                   check=True, timeout=30, capture_output=True)
    subprocess.run(['node', str(script), '--data-root', str(settings.reader_data_dir), '--output-root', str(subset),
                    '--subset-file', str(selection)], check=True, timeout=30, capture_output=True)
    full_files = list((full / 'share').rglob('index.html'))
    subset_files = list((subset / 'share').rglob('index.html'))
    assert len(full_files) == 5 and len(subset_files) == 4
    for path in subset_files:
        assert path.read_bytes() == (full / path.relative_to(subset)).read_bytes()


@pytest.mark.parametrize('actor', ['', ' ', None, 'x' * 251, 'bad\nactor'])
def test_invalid_actor_cannot_switch_public_data(pack, actor):
    settings, store, _, _ = pack
    save(store)
    with pytest.raises(publisher.ReviewPublishError, match='身份无效'):
        publisher.publish_reviews(settings=settings, actor=actor)
    assert load(settings.reader_data_dir)[0]['data_version'] == BASE
    assert published_map(settings) == []


def test_legacy_detail_without_original_dates_retains_original_schedule(pack, node_builders):
    settings, store, _, _ = pack
    path = settings.reader_data_dir / 'versions' / BASE / 'items' / 'aa.json'
    shard = json.loads(path.read_bytes())
    original_dates = [deepcopy(row['dates']) for row in shard[ITEM]['occurrences']]
    for row in shard[ITEM]['occurrences']:
        row.pop('original_dates')
    path.write_bytes(_json(shard))
    save(store)
    publisher.publish_reviews(settings=settings, actor=PRIVATE)
    details = load(settings.reader_data_dir)[2]
    assert [row['original_dates'] for row in details[ITEM]['occurrences']] == original_dates


def test_published_version_uses_native_unprefixed_digest(pack, node_builders):
    settings, store, _, _ = pack
    save(store)
    result = publisher.publish_reviews(settings=settings, actor=PRIVATE)
    root = settings.reader_data_dir
    manifest, catalog, _ = load(root)
    version = result['version']
    prefix = 'versions/' + version + '/'
    names = {'catalog.json', 'issues.json', 'search-documents.json'}
    names.add('activity-facts.json')
    names.update(row['detail_path'][len(prefix):] for row in catalog['items'])
    assets = set()
    digest = hashlib.sha256()
    digest.update(_json({'schema_version': 2, 'generator': GENERATOR, 'counts': manifest['counts'],
                         'content_rules_version': manifest['content_rules_version']}))
    for name in sorted(names):
        value = json.loads((root / prefix / name).read_bytes())
        if name == 'catalog.json':
            for item in value['items']:
                item['detail_path'] = item['detail_path'][len(prefix):]
        elif name == 'issues.json':
            for issue in value:
                for document in issue['documents']:
                    if document['pdf_path']:
                        document['pdf_path'] = document['pdf_path'][len(prefix):]
                        assets.add(document['pdf_path'])
        elif name.startswith('items/'):
            for detail in value.values():
                for occurrence in detail['occurrences']:
                    for image in occurrence['images'] + occurrence['primary_images']:
                        image['path'] = image['path'][len(prefix):]
                        assets.add(image['path'])
        digest.update(_json(name))
        digest.update(_json(value))
    for name in sorted(assets):
        digest.update(_json(name))
        digest.update(hashlib.sha256((root / prefix / name).read_bytes()).digest())
    assert digest.hexdigest() == version


def test_public_facts_export_is_deterministic_and_whitelist_only(pack, node_builders, tmp_path):
    settings, store, _, _ = pack
    save(store)
    result = publisher.publish_reviews(settings=settings, actor=PRIVATE)
    source = settings.reader_data_dir
    versioned = source / 'versions' / result['version'] / 'activity-facts.json'
    facts = json.loads(versioned.read_bytes())
    record = next(row for item in facts['items'] if item['id'] == ITEM for row in item['occurrences'])

    assert facts['schema_version'] == 1 and isinstance(facts['data_version'], str) and len(facts['data_version']) == 64
    assert {'id', 'item_id', 'title', 'preview', 'source_urls', 'dates', 'activity_facts'} <= set(record)
    assert not {'body_text', 'content', 'evidence', 'images', 'actor', 'model_request', 'raw_response'} & set(record)
    assert b'HumanSearchNeedle' not in versioned.read_bytes()
    output = tmp_path / 'reader' / 'activity-facts.json'
    public_metadata_export.export_public_facts(source, output)
    before = output.read_bytes()
    result_again = public_metadata_export.export_public_facts(source, output)
    assert output.read_bytes() == before
    assert json.loads(before) == facts
    assert result_again['data_version'] == facts['data_version']


def test_explicit_publication_adds_standalone_registration_without_a_fake_issue(pack, node_builders, tmp_path):
    settings, store, _, _ = pack
    registration_key = 'private-test-registration-key-do-not-publish'
    registered = store.register_activity(registration_key, payload(), actor=PRIVATE)
    revision = store.save(registered['item_id'], payload(), actor=PRIVATE,
                          expected_head=None, base_version=BASE)

    result = publisher.publish_reviews(settings=settings, actor=PRIVATE)
    manifest, catalog, details = load(settings.reader_data_dir)
    row = next(value for value in catalog['occurrences'] if value['item_id'] == registered['item_id'])
    detail = details[registered['item_id']]['occurrences'][0]
    assert result['occurrence_count'] == 1 and result['revision_count'] == 1
    assert manifest['counts']['items'] == 3 and manifest['counts']['occurrences'] == 4
    assert row['id'] == registered['occurrence_id'] and row['activity_id'] == registered['activity_id']
    assert row['source_id'] == registered['source_id'] and row['kind'] == 'standalone'
    assert row['issue_id'] is row['document_id'] is row['issue_date'] is None
    assert row['source_urls'] == payload()['activity']['source_urls']
    assert row['content_status'] == 'human_verified' and row['manual_review']['revision'] == revision['revision']
    assert detail['body_text'] == '' and detail['date_review'] is None and 'original_dates' not in detail
    assert detail['images'] == detail['primary_images'] == []
    assert detail['manual_review']['activity'] == payload()['activity']
    site = load_site(settings.reader_site_data_dir)
    assert site['records'] == catalog['occurrences']
    assert site['details'][registered['item_id']]['occurrences'][0]['manual_review'] == detail['manual_review']
    assert 'standalone' in site['manifest']['files']['partitions']
    assert {issue['id'] for issue in json.loads((settings.reader_data_dir / manifest['files']['issues']).read_bytes())} == set(ISSUES)

    public_version = settings.reader_data_dir / 'versions' / result['version']
    public_bytes = b''.join(path.read_bytes() for path in public_version.rglob('*.json'))
    assert PRIVATE.encode() not in public_bytes and registration_key.encode() not in public_bytes
    facts_path = public_version / 'activity-facts.json'
    facts = json.loads(facts_path.read_bytes())
    fact = next(value for item in facts['items'] if item['id'] == registered['item_id'] for value in item['occurrences'])
    assert fact['kind'] == 'standalone' and fact['issue_id'] is fact['document_id'] is None
    assert fact['source_id'] == registered['source_id'] and fact['activity_id'] == registered['activity_id']
    assert 'content' not in fact and 'evidence' not in fact

    page = settings.reader_site_data_dir.parent / 'share' / 'items' / registered['item_id'] / registered['occurrence_id'] / 'index.html'
    markup = page.read_text()
    assert '独立收录' in markup and payload()['activity']['title'] in markup
    assert registration_key not in markup and PRIVATE not in markup
    output = tmp_path / 'reader' / 'activity-facts.json'
    public_metadata_export.export_public_facts(settings.reader_data_dir, output)
    assert json.loads(output.read_bytes()) == facts
def test_empty_source_can_publish_standalone_without_synthetic_issue(pack, node_builders):
    settings, store, _, _ = pack
    root = settings.reader_data_dir
    manifest = json.loads((root / 'manifest.json').read_bytes())
    manifest['counts'] = dict.fromkeys(('items', 'occurrences', 'issues', 'documents', 'images', 'pdfs'), 0)
    for key, value in (('catalog', {'items': [], 'occurrences': []}), ('issues', []), ('search_documents', [])):
        (root / manifest['files'][key]).write_bytes(_json(value))
    (root / 'manifest.json').write_bytes(_json(manifest))
    registration = store.register_activity('empty-source-registration', payload(), actor=PRIVATE)
    store.save(registration['item_id'], payload(), actor=PRIVATE, expected_head=None, base_version=BASE)
    publisher.publish_reviews(settings=settings, actor=PRIVATE)
    source_manifest, catalog, _ = load(root)
    site = load_site(settings.reader_site_data_dir)
    assert source_manifest['counts']['issues'] == 0
    assert set(site['manifest']['files']['partitions']) == {'standalone'}
    assert site['records'] == catalog['occurrences']




def test_private_exact_preparation_precedes_first_live_switch(pack, node_builders, monkeypatch):
    settings, store, _, _ = pack
    revision = save(store)
    native_publish = publisher._publish
    observed = []

    def publish(stage, output, version, previous):
        with closing(sqlite3.connect(settings.database_path)) as db:
            rows = db.execute('SELECT version,revisions_json,result_json FROM reader_review_publications').fetchall()
        prepared = [(saved_version, json.loads(revisions)) for saved_version, revisions, result in rows
                    if json.loads(result).get('phase') == 'prepared']
        assert (version, {ITEM: revision['revision']}) in prepared
        assert published_map(settings) == []
        assert store.get_item(ITEM)['unpublished']
        observed.append(output)
        return native_publish(stage, output, version, previous)

    monkeypatch.setattr(publisher, '_publish', publish)
    publisher.publish_reviews(settings=settings, actor=PRIVATE)
    assert observed == [settings.reader_data_dir, settings.reader_site_data_dir]
    assert published_map(settings) == [{ITEM: revision['revision']}]


def test_preparation_audit_failure_cannot_switch_live_manifest(pack, node_builders, monkeypatch):
    settings, store, _, _ = pack
    save(store)
    before = [(root / 'manifest.json').read_bytes()
              for root in (settings.reader_data_dir, settings.reader_site_data_dir)]

    def fail(*args, **kwargs):
        raise OSError('private preparation audit unavailable')

    monkeypatch.setattr(ReviewStore, 'record_preparation', fail)
    with pytest.raises(publisher.ReviewPublishError) as caught:
        publisher.publish_reviews(settings=settings, actor=PRIVATE)
    assert not caught.value.result['partial']
    assert [(root / 'manifest.json').read_bytes()
            for root in (settings.reader_data_dir, settings.reader_site_data_dir)] == before
    assert published_map(settings) == []
    assert store.get_item(ITEM)['unpublished']


def test_acknowledged_removed_head_allows_real_unrelated_publication(pack, node_builders):
    settings, store, original, _ = pack
    removed_revision = save(store)
    source = settings.reader_data_dir
    new_base = '5' * 64
    old_root = source / 'versions' / BASE
    new_root = source / 'versions' / new_base
    shutil.copytree(old_root, new_root)
    # A tiny external canonical export removes ITEM; old immutable evidence stays.
    for path in new_root.rglob('*.json'):
        path.write_bytes(path.read_bytes().replace(BASE.encode(), new_base.encode()))
    catalog_path = new_root / 'catalog.json'
    catalog = json.loads(catalog_path.read_bytes())
    catalog['items'] = [item for item in catalog['items'] if item['id'] != ITEM]
    catalog['occurrences'] = [row for row in catalog['occurrences'] if row['item_id'] != ITEM]
    catalog_path.write_bytes(_json(catalog))
    search_path = new_root / 'search-documents.json'
    search_path.write_bytes(_json([row for row in json.loads(search_path.read_bytes()) if row['id'] == OCCURRENCES[2]]))
    issues_path = new_root / 'issues.json'
    issues = json.loads(issues_path.read_bytes())
    for issue in issues:
        issue['occurrence_count'] = sum(row['issue_id'] == issue['id'] for row in catalog['occurrences'])
    issues_path.write_bytes(_json(issues))
    manifest = json.loads((source / 'manifest.json').read_bytes())
    manifest.update(data_version=new_base, files=_manifest_files(new_base),
                    counts={**manifest['counts'], 'items': 1, 'occurrences': 1})
    (source / 'manifest.json').write_bytes(_json(manifest))
    other = store.save(OTHER, payload(title='仍在公开包中的活动'), actor=PRIVATE,
                       expected_head=None, base_version=new_base)
    with pytest.raises(ReviewError):
        publisher.publish_reviews(settings=settings, actor=PRIVATE)
    retirement = store.retire_removed(ITEM, actor=PRIVATE, expected_head=removed_revision['revision'],
                                     base_version=new_base, reason='PRIVATE_REMOVAL_REASON')
    result = publisher.publish_reviews(settings=settings, actor=PRIVATE)
    assert result['recorded'] and result['revision_count'] == 1
    assert published_map(settings) == [{OTHER: other['revision']}]
    assert set(load(source)[2]) == {OTHER}
    assert store.get_item(ITEM)['retirement'] == retirement
    assert store.get_item(ITEM)['history'] == [removed_revision]
    public_bytes = b''.join(path.read_bytes() for path in (source / 'versions' / result['version']).rglob('*.json'))
    assert b'PRIVATE_REMOVAL_REASON' not in public_bytes and PRIVATE.encode() not in public_bytes
    for relative, contents in original.items():
        assert (source / relative).read_bytes() == contents


def test_share_metadata_distinguishes_verified_and_pending_without_wording_pins(pack, node_builders):
    settings, store, _, _ = pack
    first = save(store, payload('verified'))
    publisher.publish_reviews(settings=settings, actor=PRIVATE)
    page = settings.reader_site_data_dir.parent / 'share' / 'items' / ITEM / OCCURRENCES[0] / 'index.html'
    verified = BeautifulSoup(page.read_text(), 'html.parser')
    save(store, payload('needs_review'), expected=first['revision'])
    publisher.publish_reviews(settings=settings, actor=PRIVATE)
    pending = BeautifulSoup(page.read_text(), 'html.parser')
    for document in (verified, pending):
        assert document.h1.get_text() == payload()['activity']['title']
        description = document.find('meta', property='og:description')['content']
        assert payload()['activity']['summary'] in description
        assert description == document.find('meta', attrs={'name': 'description'})['content']
    assert verified.find('meta', property='og:description')['content'] != pending.find('meta', property='og:description')['content']
