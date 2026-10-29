"""Real store/collector/parser transitions; only public HTTP, paid POST and wait
boundaries are faked. No live source, credentials, or model calls are required.
"""
import base64
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from threading import Barrier, Event, Lock, Thread, enumerate as running_threads
from types import SimpleNamespace

import pytest


from nannanews import acquisition
from nannanews import daily_acquisition as daily
from nannanews.acquisition import fetch_public_resource as real_public_fetch
from nannanews import information
from nannanews import runtime
from nannanews import schema as migrations

NOW = datetime(2026, 10, 14, 20, 0, tzinfo=timezone(timedelta(hours=8)))
ROOT = 'https://college.nju.edu.cn/notices.htm'
JOB_ROOT = 'https://job.nju.edu.cn/api/career/job/fair/specifics?page=0&size=15'


def put_job_pages(boundary, pages):
    for page, records in enumerate(pages):
        boundary.put(JOB_ROOT.replace('page=0', f'page={page}'),
                     json.dumps({'content': records, 'page': {'number': page, 'totalPages': len(pages)}}),
                     'application/json')
        for record in records:
            identity = record['id']
            boundary.put(f'https://job.nju.edu.cn/api/career/job/fair/specifics/{identity}',
                         json.dumps({'id': identity, 'theme': f'公开招聘会{identity}',
                                     'intro': '公开活动正文，未说明来源发布日期。',
                                     'status': 'PUBLISHED'}), 'application/json')


def listing(ids, next_page=None, identity='南京大学测试学院'):
    links = ''.join(f'<li><a href="/info/1001/{identity_}.htm">公开通知{identity_}</a></li>' for identity_ in ids)
    pager = f'<a href="{next_page}">下一页&gt;&gt;</a>' if next_page else ''
    return f'<html><head><title>{identity}通知公告</title></head><body><ul>{links}</ul>{pager}</body></html>'


def article(identity, version='原始正文', published='2026-10-14'):
    metadata = f'<meta name="pubdate" content="{published}">' if published else ''
    return (f'<html><head><title>公开通知{identity}</title>{metadata}</head><body>'
            f'<h1>公开通知{identity}</h1><article>公开通知{identity}：{version}。'
            '这是一篇公开的校园活动通知，包含真实来源正文，不推测未说明的日期与学院关联。</article></body></html>')


class PublicBoundary:
    def __init__(self):
        self.resources = {}
        self.fetches = []
        self.posts = []
        self.model_failure = None

    def put(self, url, raw, mime='text/html; charset=utf-8', resolved=None):
        self.resources[url] = (raw.encode() if isinstance(raw, str) else raw, mime, resolved or url)

    def fetch(self, url, max_bytes=acquisition.MAX_BODY, deadline=None):
        self.fetches.append(url)
        response = self.resources[url]
        if isinstance(response, BaseException):
            raise response
        return response

    def model(self, payload, *, audit=None):
        self.posts.append(payload)
        if self.model_failure:
            raise self.model_failure
        source = json.loads(payload['input'][0]['content'][0]['text'].split('\n', 1)[1])
        quote = source['text']
        item = {'title': source['title'], 'description': quote, 'tag': '校级活动'}
        return {'status': 'completed', 'error': None, 'incomplete_details': None,
                'output': [{'type': 'message', 'role': 'assistant', 'status': 'completed',
                            'content': [{'type': 'output_text', 'text': json.dumps({'items': [item]}, ensure_ascii=False)}]}]}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv('NANNA_DATA_DIR', str(tmp_path / 'private'))
    monkeypatch.setenv('NANNA_MODEL_BASE_URL', 'https://model.example/v1')
    monkeypatch.setenv('NANNA_MODEL_API_KEY', 'boundary-test-only')
    monkeypatch.setenv('NANNA_MODEL_NAME', 'gpt-6-luna')
    monkeypatch.setenv('NANNA_MODEL_API_STYLE', 'responses')
    monkeypatch.setenv('NANNA_COLLECTION_ENABLED', 'false')
    monkeypatch.setenv('NANNA_COLLECTION_TIME', '04:00')
    monkeypatch.setenv('NANNA_COLLECTION_PAGE_LIMIT', '1')
    monkeypatch.setenv('NANNA_COLLECTION_FETCH_WORKERS', '6')
    monkeypatch.setattr(runtime, 'settings', runtime.load_settings())
    runtime.prepare_runtime()
    collection_now = daily._now
    monkeypatch.setattr(daily, '_now', lambda value=None: collection_now(value if value is not None else NOW))
    seed = tmp_path / 'automation_sources.json'
    seed.write_text(json.dumps([{
        'id': 'test-college', 'name': '测试学院通知', 'url': ROOT, 'kind': 'html', 'enabled': True,
        'publisher_college': '测试学院', 'identity_url': ROOT, 'expected_identity': '南京大学测试学院',
        'article_pattern': r'^/info/1001/[0-9]+\.htm$'
    }]), encoding='utf-8')
    catalog = runtime.settings.data_dir / 'source_catalog.json'
    catalog.write_text('[]', encoding='utf-8')
    monkeypatch.setattr(daily, 'SEED_PATH', seed)
    monkeypatch.setattr(daily, 'CATALOG_PATH', catalog)
    migrations.migrate()
    boundary = PublicBoundary()
    monkeypatch.setattr(acquisition, 'fetch_public_resource', boundary.fetch)
    monkeypatch.setattr(acquisition, '_model_post', boundary.model)
    boundary.put(ROOT, listing([1]))
    for identity in range(1, 20):
        boundary.put(f'https://college.nju.edu.cn/info/1001/{identity}.htm', article(identity))
    return boundary


@pytest.fixture
def job_environment(environment):
    daily.SEED_PATH.write_text(json.dumps([{
        'id': 'job', 'name': '公开招聘会', 'url': JOB_ROOT,
        'identity_url': 'https://job.nju.edu.cn/', 'expected_identity': '南京大学就业门户',
        'kind': 'nju_job', 'enabled': True, 'detail_path': '/career/specifics/', 'scan_all_pages': True
    }]), encoding='utf-8')
    environment.put('https://job.nju.edu.cn/', '<html><title>南京大学就业门户</title></html>')
    return environment


def source_result(run):
    return next(source for source in run['sources'] if source['source_id'] == 'test-college')


def test_embedded_listing_retains_current_articles_without_promoting_its_archive(environment):
    current = int(NOW.timestamp() * 1000)
    previous = int((NOW - timedelta(days=30)).timestamp() * 1000)
    data = [
        {'infolist': [{'url': '/info/1001/1.htm', 'releasetime': current},
                      {'url': 'https://other.nju.edu.cn/info/1001/8.htm'},
                      {'url': '/unverified/9.htm', 'releasetime': 'not-publication-metadata'}]},
        {'infolist': [{'url': '/info/1001/2.htm', 'releasetime': previous}]},
    ]
    environment.put(ROOT, '<html><title>南京大学测试学院</title><body>'
        '<div id="ajaxpage-list"></div><script id="ajaxpage-template" type="text/html">'
        '{{each infolist info i}}<a href="{{@info.url}}">{{@info.title}}</a>{{/each}}'
        '</script><script>var dataList=' + json.dumps(data) + ';'
        'throw new Error("listing scripts must never execute");</script></body></html>')
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert result['status'] == 'complete'
    assert result['discovered'] == 2 and result['extracted'] == 1
    assert len(environment.posts) == 1
    assert 'https://college.nju.edu.cn/info/1001/2.htm' not in environment.fetches
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT url,eligible,priority_date FROM daily_links ORDER BY url').fetchall() == [
            ('https://college.nju.edu.cn/info/1001/1.htm', 1, NOW.date().isoformat()),
            ('https://college.nju.edu.cn/info/1001/2.htm', 0, (NOW - timedelta(days=30)).date().isoformat()),
        ]
    records = information.query_records({})['records']
    assert len(records) == 1 and records[0]['verification_status'] != 'approved'


@pytest.mark.parametrize('payload', [
    'loadRemoteArticles()',
    '[{"infolist":[{"url":"/info/1001/1.htm","releasetime":0}]}].concat([])',
    '{}',
    '[{"infolist":{}}]',
    '[{"infolist":[{"url":"/info/1001/1.htm","releasetime":null}]}]',
])
def test_unreadable_embedded_listing_never_claims_coverage_or_calls_model(environment, payload):
    environment.put(ROOT, '<html><title>南京大学测试学院</title><body>'
        '<div id="ajaxpage-list"></div><script id="ajaxpage-template" type="text/html"></script>'
        '<script>var dataList=' + payload + ';</script></body></html>')
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert result['status'] == 'failed'
    assert result['last_success_at'] is None and result['discovered'] == 0
    assert environment.posts == []
    assert information.query_records({})['records'] == []


def test_government_form_wrapped_article_retains_body_and_poster_without_form_chrome(environment):
    url = 'https://college.nju.edu.cn/info/1001/1.htm'
    image_url = 'https://college.nju.edu.cn/poster.png'
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
    text = '公开讲座介绍真实的研究主题与参加方式，活动时间地点须按原文逐项核对，不从列表标题推断其他活动。'
    raw = ('<html><head><title>政府管理学院公开讲座</title>'
           '<meta name="PubDate" content="2026-10-14 10:51"></head><body>'
           '<form><input name="__VIEWSTATE" value="opaque"><div class="hd">模板导航</div>'
           '<div class="mn_content"><table><tr><td class="table-style"><p>' + text + '</p>'
           '<img src="/poster.png"></td></tr></table></div><footer>模板页脚</footer></form></body></html>')
    environment.put(url, raw)
    environment.put(image_url, png, 'image/png')
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert result['status'] == 'complete' and result['extracted'] == 1
    record = information.query_records({})['records'][0]
    captured = information.get_document(record['document_id'])
    assert captured['text'] == text
    assert captured['raw_content'] == raw and captured['capture_complete'] is True
    assert captured['source_published_date'] == '2026-10-14'
    assert len(captured['media']) == 1 and base64.b64decode(captured['media'][0]['data']) == png
    assert record['verification_status'] != 'approved'


@pytest.mark.parametrize('failure', ['unavailable_image', 'unsafe_svg', 'dynamic_pdf'])
def test_media_preparation_failures_retain_raw_without_claiming_a_model_request(environment, failure):
    url = 'https://college.nju.edu.cn/info/1001/1.htm'
    image_url = 'https://college.nju.edu.cn/poster.svg'
    body = article(1).replace('</article>', f'<img src="{image_url}"></article>')
    if failure == 'dynamic_pdf':
        body = ('<html><head><title>PDF 通知</title></head><body><article><script>'
                'var vsb_pdf_image_data = loadPages();'
                'showVsbpdfIframe("/original.pdf","100%","600","0","",vsb_pdf_image_data);'
                '</script></article></body></html>')
    environment.put(url, body)
    if failure == 'unavailable_image':
        environment.resources[image_url] = acquisition.AcquisitionError('图片读取失败。', 'source_unavailable')
    elif failure == 'unsafe_svg':
        environment.put(image_url, '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><script>alert(1)</script></svg>', 'image/svg+xml')
    result = daily.run_once(now=NOW, mode='new')
    assert source_result(result)['status'] == 'partial'
    assert environment.posts == []
    with closing(runtime.connect_db()) as db:
        row = db.execute('SELECT state,document_id,message FROM daily_links WHERE url=?', (url,)).fetchone()
        assert row[0] == 'fetch_failed' and row[1] is not None
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM entries').fetchone()[0] == 0
    captured = information.get_document(row[1])
    assert captured['raw_content'] == body
    assert captured['model_status'] == 'not_extracted'
    if failure == 'unsafe_svg':
        assert base64.b64decode(captured['media'][0]['data']).startswith(b'<svg')
    elif failure == 'unavailable_image':
        assert captured['capture_complete'] is False
        assert captured['capture_errors'][0]['url'] == image_url
    else:
        assert captured['text'] == ''
        assert captured['capture_complete'] is False
        assert captured['capture_errors'][0]['stage'] == 'parse'
        assert captured['raw_response_digest'] == hashlib.sha256(body.encode()).hexdigest()

def test_newest_updated_and_identical_content_use_real_store_without_remodelling(environment):
    first = daily.run_once(now=NOW, mode='new')
    assert source_result(first)['extracted'] == 1
    environment.put(ROOT, listing([2, 1]))
    environment.put('https://college.nju.edu.cn/info/1001/1.htm', article(1, '更新后的公开正文'))
    second = daily.run_once(now=NOW + timedelta(days=1), mode='new')
    assert source_result(second)['discovered'] == 1
    assert source_result(second)['extracted'] == 2
    assert len(environment.posts) == 3
    third = daily.run_once(now=NOW + timedelta(days=2), mode='new')
    assert third['status'] == 'no_change'
    assert source_result(third)['skipped'] == 2
    assert len(environment.posts) == 3
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 3
        assert db.execute('SELECT COUNT(*) FROM entries').fetchone()[0] == 3
    records = information.query_records(information.parse_filters({}))['records']
    assert len(records) == 2  # only current content versions in normal query
    assert all(record['verification_status'] != 'approved' for record in records)
    assert all(record['event_date'] is None and record['deadline_date'] is None for record in records)


def test_daily_model_receives_publication_metadata_separate_from_activity_time(environment):
    url = 'https://college.nju.edu.cn/info/1001/1.htm'
    html = ('<html><head><meta name="pubdate" content="2026-10-14"></head><body>'
            '<h1>公开活动通知</h1><article>活动时间：2026年10月20日。'
            '公开正文说明该活动的地点和参加方式，不包含其他日期。</article></body></html>')
    environment.put(url, html)
    result = daily.run_once(now=NOW, mode='new')
    assert source_result(result)['extracted'] == 1
    source_data = json.loads(environment.posts[0]['input'][0]['content'][0]['text'].split('\n', 1)[1])
    assert source_data['source_published_date'] == '2026-10-14'
    assert source_data['raw_date_text'] == '2026-10-14'
    assert '活动时间：2026年10月20日' in source_data['text']
    record = information.query_records({})['records'][0]
    snapshot = information.get_document(record['document_id'])
    assert snapshot['source_published_date'] == '2026-10-14'
    assert snapshot['fetched_at'] != snapshot['source_published_date']
    assert '活动时间：2026年10月20日' in snapshot['text']


def test_bounded_pagination_retains_raw_history_while_current_originals_drain(environment):
    environment.put(ROOT, listing([1, 2], '/notices/2.htm'))
    environment.put('https://college.nju.edu.cn/notices/2.htm', listing([3, 4], '/notices/1.htm'))
    environment.put('https://college.nju.edu.cn/notices/1.htm', listing([5]))
    for identity in (3, 4, 5):
        environment.put(f'https://college.nju.edu.cn/info/1001/{identity}.htm', article(identity, published='2026-10-13'))
    first = source_result(daily.run_once(now=NOW, mode='new'))
    assert first['discovered'] == 2 and first['extracted'] == 2
    assert first['pending_pages'] == 0 and first['status'] == 'complete'
    assert first['last_success_at'] == NOW.isoformat()
    assert 'https://college.nju.edu.cn/notices/2.htm' not in environment.fetches
    for day in range(1, 4):
        history = source_result(daily.run_once(now=NOW + timedelta(days=day), mode='history'))
        assert history['fetched'] <= runtime.settings.collection_page_limit
        assert history['extracted'] == 0
    status = daily.get_status(mode='history')['sources'][0]
    assert status['pending'] == 0 and status['pending_pages'] == 0
    assert status['history_deferred'] == 3
    assert daily.get_status(mode='new')['last_run']['started_at'] == NOW.isoformat()
    assert information.list_sources()[0]['last_success_at'] == NOW.isoformat()
    assert len(environment.posts) == 2
    records = information.query_records(information.parse_filters({}))['records']
    assert len(records) == 5
    assert sum(record['model_status'] == 'not_extracted' for record in records) == 3




@pytest.mark.parametrize('retain_root_cohort', [True, False])
def test_current_updates_drain_without_promoting_retained_raw_archive(environment, retain_root_cohort):
    environment.put(ROOT, listing([1], '/notices/2.htm'))
    environment.put('https://college.nju.edu.cn/notices/2.htm', listing(range(8, 19)))
    for identity in range(8, 19):
        environment.resources[f'https://college.nju.edu.cn/info/1001/{identity}.htm'] = acquisition.AcquisitionError('公开历史原文暂时不可用。', 'source_unavailable')
    daily.run_once(now=NOW, mode='new')
    daily.run_once(now=NOW, mode='history')
    for identity in range(8, 19):
        environment.put(f'https://college.nju.edu.cn/info/1001/{identity}.htm', article(identity, published='2026-10-13'))
    environment.put(ROOT, listing(range(1 if retain_root_cohort else 2, 8), '/notices/2.htm'))
    if retain_root_cohort:
        environment.put('https://college.nju.edu.cn/info/1001/1.htm', article(1, '历史队列期间的公开更正'))
    result = source_result(daily.run_once(now=NOW + timedelta(days=1), mode='new'))
    extracted = 7 if retain_root_cohort else 6
    assert result['extracted'] == extracted and result['pending'] == 0
    assert result['status'] == 'complete'
    assert daily.get_status(mode='history')['pending'] > 0
    assert len(environment.posts) == extracted + 1
    with closing(runtime.connect_db()) as db:
        assert db.execute("SELECT COUNT(*) FROM source_documents WHERE source_url LIKE '%/1.htm'").fetchone()[0] == (2 if retain_root_cohort else 1)
        assert db.execute("SELECT COUNT(*) FROM daily_links WHERE eligible=1 AND state IN ('pending','ready')").fetchone()[0] == 0
        for identity in range(8, 19):
            assert db.execute('SELECT eligible FROM daily_links WHERE url=?',
                              (f'https://college.nju.edu.cn/info/1001/{identity}.htm',)).fetchone()[0] == 0




@pytest.mark.parametrize('previous_success', [False, True])
@pytest.mark.parametrize('error_code', ['source_unavailable', 'source_empty'])
def test_continuation_failure_preserves_validated_current_work(environment, previous_success, error_code):
    checkpoint = None
    if previous_success:
        daily.run_once(now=NOW, mode='new')
        checkpoint = NOW.isoformat()
    current = 2 if previous_success else 1
    clock = NOW + timedelta(days=int(previous_success))
    continuation = 'https://college.nju.edu.cn/notices/2.htm'
    environment.put(ROOT, listing([current, 1] if previous_success else [current], '/notices/2.htm'))
    environment.resources[continuation] = acquisition.AcquisitionError('历史分页不可读取。', error_code)

    run = daily.run_once(now=clock, mode='new')
    result = source_result(run)
    assert result['status'] == 'complete' and result['failed'] == 0
    assert result['extracted'] == 1 and result['pending'] == result['pending_pages'] == 0
    checkpoint = clock.isoformat()
    assert result['last_success_at'] == checkpoint
    assert continuation not in environment.fetches
    history = daily.run_once(now=clock + timedelta(minutes=1), mode='history')
    history_source = source_result(history)
    assert history_source['status'] == 'partial' and history_source['failed'] == 1
    assert history_source['extracted'] == 0 and history_source['pending_pages'] == 1
    assert len(environment.posts) == current
    source = information.list_sources()[0]
    assert source['enabled'] and source['status'] == 'verified'
    assert source['last_success_at'] == checkpoint
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT state,attempt_run FROM daily_pages WHERE url=?',
                          (continuation,)).fetchone() == ('pending', history['id'])
        assert db.execute('SELECT state FROM daily_links WHERE url=?',
                          (f'https://college.nju.edu.cn/info/1001/{current}.htm',)).fetchone()[0] == 'done'
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == current
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == current
        assert db.execute('SELECT bootstrap_at,last_success_at FROM daily_source_state').fetchone() == (NOW.isoformat(), checkpoint)


@pytest.mark.parametrize('failure_stage', ['identity', 'root'])
def test_identity_or_root_failure_blocks_current_updates(environment, failure_stage):
    identity = 'https://college.nju.edu.cn/about.htm'
    definitions = json.loads(daily.SEED_PATH.read_text())
    definitions[0]['identity_url'] = identity
    daily.SEED_PATH.write_text(json.dumps(definitions), encoding='utf-8')
    environment.put(identity, '<html><title>南京大学测试学院</title></html>')
    first = daily.run_once(now=NOW, mode='new')
    assert source_result(first)['extracted'] == 1
    original = 'https://college.nju.edu.cn/info/1001/1.htm'
    environment.put(original, article(1, '正文已更新，但本轮发布者或首页验证失败'))
    environment.resources[identity if failure_stage == 'identity' else ROOT] = acquisition.AcquisitionError('公开读取失败。', 'source_unavailable')
    environment.fetches.clear()

    run = daily.run_once(now=NOW + timedelta(days=1), mode='new')
    result = source_result(run)
    assert result['status'] == 'failed' and result['fetched'] == result['extracted'] == 0
    assert result['last_success_at'] == NOW.isoformat()
    assert original not in environment.fetches and len(environment.posts) == 1
    source = information.list_sources()[0]
    assert source['status'] == 'failed' and source['last_success_at'] == NOW.isoformat()
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 1


def test_failed_source_never_advances_success_checkpoint_and_can_catch_up(environment):
    first = source_result(daily.run_once(now=NOW, mode='new'))
    previous = first['last_success_at']
    environment.resources[ROOT] = acquisition.AcquisitionError('公开源暂时不可用。', 'source_unavailable')
    failed = source_result(daily.run_once(now=NOW + timedelta(days=1), mode='new'))
    assert failed['status'] == 'failed' and failed['last_success_at'] == previous
    assert information.list_sources()[0]['status'] == 'failed'
    assert information.list_sources()[0]['last_success_at'] == previous
    environment.put(ROOT, listing([2, 1]))
    restored = source_result(daily.run_once(now=NOW + timedelta(days=2), mode='new'))
    assert restored['discovered'] == 1 and restored['extracted'] == 1
    assert restored['last_success_at'] == (NOW + timedelta(days=2)).isoformat()


def test_identity_validation_before_activation_and_operator_edits_survive_merge(environment):
    daily.initialize_sources()
    initial = information.list_sources()[0]
    assert initial['enabled'] is False and initial['status'] == 'pending_validation'
    assert environment.fetches == [] and environment.posts == []
    environment.put(ROOT, listing([1], identity='其他学校'))
    failed = source_result(daily.run_once(now=NOW, mode='new'))
    assert failed['status'] == 'failed'
    assert information.list_sources()[0]['enabled'] is False
    assert not environment.posts
    environment.put(ROOT, listing([1]))
    daily.run_once(now=NOW + timedelta(days=1), mode='new')
    assert information.list_sources()[0]['enabled'] is True
    information.update_source('test-college', enabled=False, message='操作员暂停')
    daily.initialize_sources()
    assert information.list_sources()[0]['enabled'] is False
    environment.fetches.clear()
    disabled = source_result(daily.run_once(now=NOW + timedelta(days=2), mode='new'))
    assert disabled['status'] == 'disabled' and environment.fetches == []


def test_historical_article_pdf_and_personal_reference_never_become_publisher_feeds(environment):
    (runtime.settings.data_dir / 'source_catalog.json').write_text(json.dumps([{'id': 'old-person', 'name': '历史个人目录名',
        'source_urls': ['https://www.nju.edu.cn/article/old.htm', 'https://external.example/old.pdf']}]), encoding='utf-8')
    sources = daily.initialize_sources()
    historical = [source for source in sources if source['id'].startswith('historical-')]
    assert len(historical) == 2
    assert all(not source['enabled'] and source['status'] == 'unsupported' and source['publisher_college'] is None for source in historical)
    run = daily.run_once(now=NOW, mode='new')
    assert run['status'] == 'partial'
    assert all(source['status'] == 'unsupported' for source in run['sources'] if source['source_id'].startswith('historical-'))
    assert not any('external.example' in url or '/article/old' in url for url in environment.fetches)


def test_model_failure_persists_raw_and_never_paid_retries_same_content(environment):
    environment.model_failure = acquisition.AcquisitionError('模型请求失败。', 'model_http_error')
    first = source_result(daily.run_once(now=NOW, mode='new'))
    assert first['failed'] == 1 and first['model_failures'] == 1
    assert first['last_success_at'] is None
    records = information.query_records(information.parse_filters({}))['records']
    assert len(records) == 1 and records[0]['model_status'] == 'failed'
    environment.model_failure = None
    second = daily.run_once(now=NOW + timedelta(days=1), mode='new')
    assert source_result(second)['model_failures'] == 1 and len(environment.posts) == 1


def test_interrupted_fetch_is_recoverable_but_sent_model_call_is_not_retried(environment):
    url = 'https://college.nju.edu.cn/info/1001/1.htm'
    environment.resources[url] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        daily.run_once(now=NOW, mode='new')
    assert daily.get_status()['last_run']['status'] == 'interrupted'
    assert daily.get_status()['pending'] == 1
    environment.put(url, article(1))
    environment.model_failure = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        daily.run_once(now=NOW + timedelta(days=1), mode='new')
    assert daily.get_status()['sources'][0]['model_failures'] == 1
    assert len(environment.posts) == 1
    environment.model_failure = None
    daily.run_once(now=NOW + timedelta(days=2), mode='new')
    assert len(environment.posts) == 1
    records = information.query_records(information.parse_filters({}))['records']
    assert len(records) == 1 and records[0]['model_status'] == 'extracting'


def test_process_death_recovers_persisted_fetch_claim_and_uncertain_model_claim(environment):
    daily.initialize_sources()
    environment.put('https://college.nju.edu.cn/info/1001/1.htm', article(1, published='2026-10-15'))
    with closing(runtime.connect_db()) as db, db:
        run_id = db.execute("INSERT INTO daily_runs(actor,started_at,status) VALUES ('system:daily',?,'running')", (NOW.isoformat(),)).lastrowid
        db.execute("INSERT INTO daily_links(source_id,url,state,eligible,first_seen,last_seen,attempt_run) VALUES ('test-college',?,'fetching',1,?,?,?)",
                   ('https://college.nju.edu.cn/info/1001/1.htm', NOW.isoformat(), NOW.isoformat(), run_id))
        document = acquisition.fetch_document('https://college.nju.edu.cn/info/1001/2.htm')
    saved = information.save_document(document, source_id='test-college', actor='system:daily')
    with closing(runtime.connect_db()) as db, db:
        db.execute("INSERT INTO daily_links(source_id,url,state,eligible,first_seen,last_seen,attempt_run,document_id) VALUES ('test-college',?,'modelling',1,?,?,?,?)",
                   ('https://college.nju.edu.cn/info/1001/2.htm', NOW.isoformat(), NOW.isoformat(), run_id, saved['id']))
    result = source_result(daily.run_once(now=NOW + timedelta(days=1), mode='new'))
    assert result['extracted'] == 1 and result['model_failures'] == 1
    assert len(environment.posts) == 1
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT status FROM daily_runs WHERE id=?', (run_id,)).fetchone()[0] == 'interrupted'


def test_beijing_schedule_and_worker_interruption_catchup_without_polling(environment, monkeypatch):
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, collection_time='20:00'))
    utc = datetime(2026, 10, 14, 11, 59, tzinfo=timezone.utc)
    slot = daily.next_run_at(utc)
    assert slot.isoformat() == '2026-10-14T20:00:00+08:00'
    assert daily.next_run_at(NOW + timedelta(seconds=1)).isoformat() == '2026-10-15T20:00:00+08:00'
    with pytest.raises(ValueError):
        daily.next_run_at(datetime(2026, 10, 14, 20))
    with pytest.raises(RuntimeError):
        daily.run_worker()
    daily.run_once(now=NOW - timedelta(days=2), mode='new')
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, collection_enabled=True))
    before_today_slot = NOW - timedelta(hours=1)
    original_now = daily._now
    monkeypatch.setattr(daily, '_now', lambda value=None: original_now(value if value is not None else before_today_slot))
    waits = []

    class WaitBoundary:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def notify_all(self):
            pass

        def wait(self, timeout):
            waits.append(timeout)
            raise KeyboardInterrupt

    monkeypatch.setattr(daily, 'threading', SimpleNamespace(
        Condition=WaitBoundary, current_thread=daily.threading.current_thread,
        main_thread=daily.threading.main_thread))
    daily.run_worker()
    assert daily.get_status()['last_run']['started_at'] == before_today_slot.isoformat()
    assert waits and 0 < waits[0] <= 3600
    assert len(environment.posts) == 1  # catch-up crawl did not re-bill unchanged content


def test_four_am_schedule_catches_up_once_after_previous_evening_run(environment, monkeypatch):
    slot = NOW.replace(hour=4)
    previous = slot - timedelta(hours=7)
    environment.put('https://college.nju.edu.cn/info/1001/1.htm',
                    article(1, published=previous.date().isoformat()))
    daily.run_once(now=previous, mode='new')
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, collection_time='04:00'))

    assert daily.next_run_at((slot - timedelta(minutes=1)).astimezone(timezone.utc)) == slot
    assert daily.next_run_at(slot) == slot
    assert daily.next_run_at(slot + timedelta(seconds=1)) == slot + timedelta(days=1)
    assert not daily._worker_due(slot - timedelta(minutes=1))
    assert daily._worker_due(slot)
    catchup = slot + timedelta(hours=4)
    assert daily._worker_due(catchup)
    daily.run_once(now=catchup, mode='new')
    assert not daily._worker_due(catchup + timedelta(seconds=1))
    assert daily.next_run_at(catchup) == slot + timedelta(days=1)
    assert len(environment.posts) == 1


def test_status_accessor_does_not_create_schema_or_contact_external_resources(environment):
    before = runtime.settings.database_path.read_bytes()
    status = daily.get_status()
    assert status['last_run'] is None and status['sources'] == [] and status['pending'] == 0
    assert daily.list_items(mode='history') == {'items': [], 'total': 0, 'page': 1, 'pages': 0, 'per_page': 20}
    assert status['last_runs'] == {'new': None, 'history': None, 'legacy': None}
    assert environment.fetches == [] and environment.posts == []
    assert runtime.settings.database_path.read_bytes() == before


def test_public_job_api_pagination_and_nonpublic_records_are_honest(environment):
    seed = [{
        'id': 'job', 'name': '就业公开资讯', 'url': 'https://job.nju.edu.cn/api/career/content/informations?page=0&size=15',
        'identity_url': 'https://job.nju.edu.cn/', 'expected_identity': '南京大学就业门户',
        'kind': 'nju_job', 'enabled': True, 'detail_path': '/career/info/'
    }]
    daily.SEED_PATH.write_text(json.dumps(seed), encoding='utf-8')
    environment.put('https://job.nju.edu.cn/', '<html><title>南京大学就业门户</title></html>')
    for page, records in [(0, [{'id': '11', 'title': '公开就业资讯', 'publishedAt': '2026-10-14 00:00:00', 'loginRequired': False},
                              {'id': '12', 'loginRequired': True}]), (1, [{'id': 'j_recruit_news_1340', 'publishedAt': '2024-10-13 00:00:00'}])]:
        environment.put(f'https://job.nju.edu.cn/api/career/content/informations?page={page}&size=15',
            json.dumps({'content': records, 'page': {'number': page, 'totalPages': 2, 'size': 15, 'totalElements': 3}}), 'application/json')
    for identity in ('11', 'j_recruit_news_1340'):
        environment.put(f'https://job.nju.edu.cn/api/career/content/informations/{identity}',
            json.dumps({'id': str(identity), 'title': f'公开就业资讯{identity}', 'content': '公开就业资讯内容。未说明活动日期或截止日期，不得将招聘活动时间视为来源发布日期。',
                        'publishedAt': '2026-10-14 00:00:00' if identity == '11' else '2024-10-13 00:00:00',
                        'status': 'PUBLISHED', 'loginRequired': False}), 'application/json')
    run = daily.run_once(now=NOW, mode='new')
    source = run['sources'][0]
    assert source['discovered'] == 2 and source['disallowed'] == 1
    assert source['status'] == 'partial' and source['last_success_at'] is None
    assert source['history_deferred'] == 0 and len(environment.posts) == 1
    history = daily.run_once(now=NOW, mode='history')['sources'][0]
    assert history['history_deferred'] == 1 and history['extracted'] == 0
    assert not any(url.endswith('/12') for url in environment.fetches)
    records = information.query_records(information.parse_filters({}))['records']
    assert len(records) == 2
    assert {record['source_url'] for record in records} == {
        'https://job.nju.edu.cn/career/info/11',
        'https://job.nju.edu.cn/career/info/j_recruit_news_1340',
    }


def test_full_query_identity_and_missing_publication_dates_are_preserved(environment):
    urls = [f'https://college.nju.edu.cn/info/1001/1.htm?mid={mid}&x=1&x=2' for mid in (10, 20)]
    anchors = ''.join(f'<li><a href="{url.replace("&", "&amp;")}">公开通知</a></li>' for url in urls)
    environment.put(ROOT, '<html><title>南京大学测试学院通知</title><ul>' + anchors + '</ul></html>')
    for url in urls:
        environment.put(url, article(1, '报告将于2026年10月23日举行，这只是活动日期', published=None))
    first = source_result(daily.run_once(now=NOW, mode='new'))
    assert first['discovered'] == 2 and first['extracted'] == 2
    records = information.query_records(information.parse_filters({}))['records']
    assert {record['source_url'] for record in records} == set(urls)
    assert all(record['source_published_date'] is None for record in records)
    daily.run_once(now=NOW + timedelta(days=1), mode='new')
    assert len(environment.posts) == 2


def test_changed_failed_content_can_extract_but_same_digest_cannot_retry(environment):
    environment.model_failure = acquisition.AcquisitionError('已发送请求失败。', 'model_http_error')
    daily.run_once(now=NOW, mode='new')
    environment.model_failure = None
    environment.put('https://college.nju.edu.cn/info/1001/1.htm', article(1, '更正后的新原文'))
    updated = source_result(daily.run_once(now=NOW + timedelta(days=1), mode='new'))
    assert updated['extracted'] == 1 and updated['model_failures'] == 0
    assert len(environment.posts) == 2
    environment.put('https://college.nju.edu.cn/info/1001/1.htm', article(1))
    reverted = source_result(daily.run_once(now=NOW + timedelta(days=2), mode='new'))
    assert reverted['model_failures'] == 1 and len(environment.posts) == 2


def test_event_sorted_job_pagination_is_rescanned_after_known_front_page(environment):
    seed = [{
        'id': 'job', 'name': '公开招聘会',
        'url': 'https://job.nju.edu.cn/api/career/job/fair/specifics?page=0&size=15',
        'identity_url': 'https://job.nju.edu.cn/', 'expected_identity': '南京大学就业门户',
        'kind': 'nju_job', 'enabled': True, 'detail_path': '/career/specifics/', 'scan_all_pages': True
    }]
    daily.SEED_PATH.write_text(json.dumps(seed), encoding='utf-8')
    environment.put('https://job.nju.edu.cn/', '<html><title>南京大学就业门户</title></html>')
    for page, identity in [(0, 11), (1, 13)]:
        environment.put(f'https://job.nju.edu.cn/api/career/job/fair/specifics?page={page}&size=15',
            json.dumps({'content': [{'id': str(identity), 'startAt': '2026-11-01 10:00:00'}],
                        'page': {'number': page, 'totalPages': 2}}), 'application/json')
    for identity in (11, 13, 14):
        environment.put(f'https://job.nju.edu.cn/api/career/job/fair/specifics/{identity}',
            json.dumps({'id': str(identity), 'theme': f'公开招聘会{identity}',
                        'intro': '这是一场可公开读取的招聘活动，未说明来源发布日期。',
                        'startAt': '2026-11-01 10:00:00', 'status': 'PUBLISHED'}), 'application/json')
    daily.run_once(now=NOW, mode='new')
    environment.put('https://job.nju.edu.cn/api/career/job/fair/specifics?page=1&size=15',
        json.dumps({'content': [{'id': '14'}, {'id': '13'}], 'page': {'number': 1, 'totalPages': 2}}), 'application/json')
    second = daily.run_once(now=NOW + timedelta(days=1), mode='new')
    assert second['sources'][0]['discovered'] == 1
    assert information.query_records(information.parse_filters({}))['total'] == 2
    assert not any(url.endswith('/14') for url in environment.fetches)
    assert all(record['source_published_date'] is None for record in information.query_records(information.parse_filters({}))['records'])
    assert len(environment.posts) == 2  # upcoming continuation event qualifies; undated history does not


def test_sources_share_uncapped_current_work_and_archives_remain_separate(environment):
    seeds = []
    for source_id, ids in [('a', [1, 2, 3]), ('b', [11, 12, 13]), ('c', [17, 18, 19])]:
        root = f'https://college.nju.edu.cn/{source_id}.htm'
        seeds.append({'id': source_id, 'name': source_id, 'url': root, 'kind': 'html', 'enabled': True,
                      'publisher_college': '测试学院', 'identity_url': root,
                      'expected_identity': '南京大学测试学院', 'article_pattern': r'^/info/1001/[0-9]+\.htm$'})
        environment.put(root, listing(ids, '/a/2.htm' if source_id == 'a' else None))
    environment.put('https://college.nju.edu.cn/a/2.htm', listing([4, 5]))
    for identity in (4, 5):
        environment.put(f'https://college.nju.edu.cn/info/1001/{identity}.htm', article(identity, published='2026-10-13'))
    daily.SEED_PATH.write_text(json.dumps(seeds), encoding='utf-8')
    run = daily.run_once(now=NOW, mode='new')
    assert sum(source['extracted'] for source in run['sources']) == 9
    posted = [json.loads(payload['input'][0]['content'][0]['text'].split('\n', 1)[1])['canonical_url']
              for payload in environment.posts]
    assert set(posted) == {f'https://college.nju.edu.cn/info/1001/{identity}.htm'
                           for identity in (1, 2, 3, 11, 12, 13, 17, 18, 19)}
    archive = 'https://college.nju.edu.cn/info/1001/4.htm'
    assert archive not in environment.fetches
    for day in (1, 2):
        daily.run_once(now=NOW + timedelta(days=day), mode='history')
    assert environment.fetches.index(archive) > max(environment.fetches.index(url) for url in posted)
    status = {source['source_id']: source for source in daily.get_status(mode='history')['sources']}
    assert all(source['pending'] == 0 for source in status.values())
    assert status['a']['history_deferred'] == 2 and status['a']['status'] == 'complete'
    assert all(source['status'] == 'complete' for source in daily.get_status(mode='new')['sources'])
    assert len(environment.posts) == 9
    records = information.query_records(information.parse_filters({}), per_page=50)['records']
    assert len(records) == 11




def test_job_view_counter_changes_do_not_rebill_stable_public_content(environment):
    daily.SEED_PATH.write_text(json.dumps([{
        'id': 'job', 'name': '就业公开资讯',
        'url': 'https://job.nju.edu.cn/api/career/content/informations?page=0&size=15',
        'identity_url': 'https://job.nju.edu.cn/', 'expected_identity': '南京大学就业门户',
        'kind': 'nju_job', 'enabled': True, 'detail_path': '/career/info/'
    }]), encoding='utf-8')
    environment.put('https://job.nju.edu.cn/', '<html><title>南京大学就业门户</title></html>')
    environment.put('https://job.nju.edu.cn/api/career/content/informations?page=0&size=15',
        json.dumps({'content': [{'id': '11', 'loginRequired': False}],
                    'page': {'number': 0, 'totalPages': 1}}), 'application/json')
    detail = {'id': '11', 'title': '公开就业资讯', 'content': '完整公开资讯正文。原文没有活动日期，不推断。',
              'publishedAt': '2026-10-14 00:00:00', 'status': 'PUBLISHED', 'loginRequired': False,
              'trafficStat': {'view': 1}, 'updatedBy': 'operator-id', 'updatedAt': '2026-10-14 11:00:00'}
    url = 'https://job.nju.edu.cn/api/career/content/informations/11'
    environment.put(url, json.dumps(detail), 'application/json')
    daily.run_once(now=NOW, mode='new')
    detail['trafficStat']['view'] = 100
    detail['updatedAt'] = '2026-10-15 11:00:00'
    environment.put(url, json.dumps(detail), 'application/json')
    second = daily.run_once(now=NOW + timedelta(days=1), mode='new')
    assert second['sources'][0]['skipped'] == 1
    assert len(environment.posts) == 1
    assert information.query_records(information.parse_filters({}))['total'] == 1


def test_private_source_file_is_copied_once_and_operator_edits_are_not_overwritten(environment):
    private = runtime.settings.data_dir / 'automation_sources.json'
    assert not private.exists()
    seed = daily.SEED_PATH.read_bytes()
    daily.initialize_sources()
    assert private.read_bytes() == seed
    assert private.stat().st_mode & 0o777 == 0o600
    definitions = json.loads(private.read_text(encoding='utf-8'))
    definitions[0].update(name='操作员文件中的名称', enabled=False, evidence='操作员保留的来源说明')
    edited = json.dumps(definitions, ensure_ascii=False, indent=2)
    private.write_text(edited, encoding='utf-8')
    daily.SEED_PATH.write_text('[]', encoding='utf-8')
    daily.initialize_sources()
    assert private.read_text(encoding='utf-8') == edited
    source = information.list_sources()[0]
    assert source['name'] == '操作员文件中的名称' and not source['enabled']
    with closing(runtime.connect_db()) as db:
        stored = json.loads(db.execute('SELECT definition_json FROM daily_source_state').fetchone()[0])
    assert stored['evidence'] == '操作员保留的来源说明' and stored['enabled'] is False
    daily.run_once(now=NOW, mode='new')
    assert environment.fetches == [] and environment.posts == []


def test_private_file_merges_new_source_without_overwriting_unchanged_registry_edits(environment):
    daily.run_once(now=NOW, mode='new')
    information.update_source('test-college', name='注册表操作员名称', enabled=False, message='操作员暂停')
    private = runtime.settings.data_dir / 'automation_sources.json'
    definitions = json.loads(private.read_text(encoding='utf-8'))
    definitions[0]['evidence'] = '仅修改文件说明，不覆盖注册表名称或暂停。'
    new = dict(definitions[0], id='new-college', name='新增官方栏目',
               url='https://college.nju.edu.cn/new-list.htm', identity_url='https://college.nju.edu.cn/new-list.htm')
    definitions.append(new)
    private.write_text(json.dumps(definitions, ensure_ascii=False), encoding='utf-8')
    environment.put(new['url'], listing([11]))
    environment.put('https://college.nju.edu.cn/info/1001/11.htm', article(11, published='2026-10-15'))
    environment.fetches.clear()
    daily.initialize_sources()
    sources = {source['id']: source for source in information.list_sources()}
    assert sources['test-college']['name'] == '注册表操作员名称'
    assert sources['test-college']['enabled'] is False
    assert sources['new-college']['status'] == 'pending_validation' and sources['new-college']['enabled'] is False
    run = daily.run_once(now=NOW + timedelta(days=1), mode='new')
    results = {source['source_id']: source for source in run['sources']}
    assert results['test-college']['status'] == 'disabled'
    assert results['new-college']['extracted'] == 1
    assert ROOT not in environment.fetches
    assert information.list_sources(enabled_only=True)[0]['id'] == 'new-college'


def test_private_disable_remove_and_reenable_preserve_records_queue_and_checkpoint(environment):
    first = source_result(daily.run_once(now=NOW, mode='new'))
    checkpoint = first['last_success_at']
    pending_url = 'https://college.nju.edu.cn/info/1001/3.htm'
    environment.resources[pending_url] = acquisition.AcquisitionError('公开读取暂时失败。', 'source_unavailable')
    environment.put(ROOT, listing([1, 2, 3]))
    second = source_result(daily.run_once(now=NOW + timedelta(days=1), mode='new'))
    assert second['pending'] == 0 and second['fetch_failures'] == 1 and second['last_success_at'] == checkpoint
    private = runtime.settings.data_dir / 'automation_sources.json'
    definitions = json.loads(private.read_text(encoding='utf-8'))
    definitions[0]['enabled'] = False
    private.write_text(json.dumps(definitions), encoding='utf-8')
    environment.fetches.clear()
    posts = len(environment.posts)
    disabled = source_result(daily.run_once(now=NOW + timedelta(days=2), mode='new'))
    assert disabled['status'] == 'disabled' and disabled['pending'] == 0 and disabled['fetch_failures'] == 1
    assert disabled['last_success_at'] == checkpoint
    assert environment.fetches == [] and len(environment.posts) == posts
    private.write_text('[]', encoding='utf-8')
    removed = source_result(daily.run_once(now=NOW + timedelta(days=3), mode='new'))
    assert removed['status'] == 'disabled' and removed['paused_by_configuration'] == 1
    assert environment.fetches == []
    assert information.query_records(information.parse_filters({}))['total'] == 2
    definitions[0]['enabled'] = True
    private.write_text(json.dumps(definitions), encoding='utf-8')
    environment.put(pending_url, article(3))
    daily.initialize_sources()
    assert information.list_sources()[0]['status'] == 'pending_validation'
    assert information.list_sources()[0]['last_success_at'] == checkpoint
    daily.run_once(now=NOW + timedelta(days=4), mode='new')
    resumed = source_result(daily.run_once(now=NOW + timedelta(days=5), mode='new'))
    assert resumed['pending'] == 0
    assert information.query_records(information.parse_filters({}))['total'] == 3


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'https://different.example/notices.htm'])
def test_invalid_private_url_disables_stale_verified_definition_without_network(environment, url):
    daily.run_once(now=NOW, mode='new')
    private = runtime.settings.data_dir / 'automation_sources.json'
    definitions = json.loads(private.read_text(encoding='utf-8'))
    definitions[0]['url'] = url
    private.write_text(json.dumps(definitions), encoding='utf-8')
    environment.fetches.clear()
    daily.initialize_sources()
    source = information.list_sources()[0]
    assert source['status'] == 'failed' and source['enabled'] is False
    assert source['last_success_at'] == NOW.isoformat()
    failed = source_result(daily.run_once(now=NOW + timedelta(days=1), mode='new'))
    assert failed['status'] == 'failed'
    assert environment.fetches == [] and len(environment.posts) == 1


def test_changed_private_endpoint_revalidates_and_does_not_fetch_old_queue_scope(environment):
    daily.run_once(now=NOW, mode='new')
    environment.resources['https://college.nju.edu.cn/info/1001/3.htm'] = acquisition.AcquisitionError('公开读取暂时失败。', 'source_unavailable')
    environment.put(ROOT, listing([1, 2, 3]))
    daily.run_once(now=NOW + timedelta(days=1), mode='new')
    private = runtime.settings.data_dir / 'automation_sources.json'
    definitions = json.loads(private.read_text(encoding='utf-8'))
    new_url = 'https://college.nju.edu.cn/replaced-list.htm'
    definitions[0].update(url=new_url, identity_url=new_url)
    private.write_text(json.dumps(definitions), encoding='utf-8')
    environment.put(new_url, listing([7], identity='错误发布者'))
    daily.initialize_sources()
    source = information.list_sources()[0]
    assert source['url'] == new_url and source['status'] == 'pending_validation' and not source['enabled']
    environment.fetches.clear()
    failed = source_result(daily.run_once(now=NOW + timedelta(days=2), mode='new'))
    assert failed['status'] == 'failed' and ROOT not in environment.fetches
    assert information.list_sources()[0]['status'] == 'failed'
    environment.put(new_url, listing([7]))
    environment.put('https://college.nju.edu.cn/info/1001/7.htm', article(7, published='2026-10-17'))
    environment.fetches.clear()
    recovered = source_result(daily.run_once(now=NOW + timedelta(days=3), mode='new'))
    assert recovered['extracted'] == 1 and recovered['paused_by_configuration'] == 1
    assert recovered['last_success_at'] == NOW.isoformat()
    assert ROOT not in environment.fetches
    assert 'https://college.nju.edu.cn/info/1001/3.htm' not in environment.fetches
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM daily_links').fetchone()[0] == 4
    assert information.query_records(information.parse_filters({}))['total'] == 3


@pytest.mark.parametrize(('start', 'eligible'), [
    ('2026-10-15 09:00:00', True),
    ('2026-10-14T12:00:00Z', True),  # exactly the collection instant
    ('2026-10-14T23:30:00-05:00', True),
    ('2026-10-14', True),  # no invented midnight cutoff for a calendar-only date
    ('2026-10-15T00:30:00+14:00', False),  # still October 14, before collection in Beijing
    ('2026-10-14T11:59:59Z', False),
    ('2026-10-13', False),
    ('2026-02-30 10:00:00', False),
    ('2026-10-15 not-a-timestamp', False),
    ('2026-10-15T09:00:00Z garbage', False),
    ('10月15日', False),
    ('', False),
    (None, False),
    (20261015, False),
    (True, False),
])
def test_continuation_eligibility_uses_valid_explicit_event_precision(job_environment, start, eligible):
    record = {'id': 'event', 'title': '2099年活动标题不能提供日期证据'}
    if start is not None:
        record['startAt'] = start
    put_job_pages(job_environment, [[], [record]])
    result = daily.run_once(now=NOW, mode='new')['sources'][0]
    assert result['extracted'] == int(eligible)
    assert result['history_deferred'] == 0
    if not eligible:
        assert not any('/specifics/event' in url for url in job_environment.fetches)
        daily.run_once(now=NOW, mode='history')
        assert daily.get_status(mode='history')['sources'][0]['history_deferred'] == 1
    assert len(job_environment.posts) == int(eligible)
    with closing(runtime.connect_db()) as db:
        row = db.execute('SELECT eligible,priority_kind FROM daily_links').fetchone()
        assert row[0] == int(eligible)
        assert row[1] == ('event' if daily._api_time(start) is not None else None)
        assert db.execute('SELECT source_published_date FROM source_documents').fetchone()[0] is None
    if eligible:
        source = json.loads(job_environment.posts[0]['input'][0]['content'][0]['text'].split('\n', 1)[1])
        assert source['source_published_date'] is None


@pytest.mark.parametrize(('published', 'eligible'), [
    ('2026-10-13T16:30:00Z', True),  # October 14 in Beijing
    ('2026-10-14T00:30:00+14:00', False),  # October 13 in Beijing
    ('2026-10-14 not-a-timestamp', False),
    (None, False),
])
def test_continuation_publication_uses_validated_publisher_calendar(job_environment, published, eligible):
    put_job_pages(job_environment, [[], [{'id': 'publication', 'publishedAt': published}]])
    daily.run_once(now=NOW, mode='new')
    assert len(job_environment.posts) == int(eligible)
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT eligible FROM daily_links').fetchone()[0] == int(eligible)


@pytest.mark.parametrize('restriction', [
    {'loginRequired': True}, {'enabled': False}, {'status': 'DRAFT'}
])
def test_upcoming_api_events_do_not_override_public_access_gates(job_environment, restriction):
    put_job_pages(job_environment, [[], [dict({'id': 'private', 'startAt': '2026-10-15'}, **restriction)]])
    result = daily.run_once(now=NOW, mode='new')['sources'][0]
    assert result['disallowed'] == 1 and result['extracted'] == 0
    assert not job_environment.posts
    assert not any(url.endswith('/private') for url in job_environment.fetches)
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT state,eligible FROM daily_links').fetchone() == ('disallowed', 1)


def test_rediscovered_upcoming_jobs_reuse_raw_for_first_extraction_without_retrying_attempts(job_environment, monkeypatch):
    identities = ['deferred', 'pending', 'failed', 'uncertain']
    put_job_pages(job_environment, [[], [{'id': identity} for identity in identities]])
    first = daily.run_once(now=NOW, mode='new')
    assert first['sources'][0]['fetched'] == 0
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, collection_page_limit=4))
    history = daily.run_once(now=NOW, mode='history')
    assert history['sources'][0]['history_deferred'] == 4
    assert history['sources'][0]['extracted'] == 0 and not job_environment.posts
    with closing(runtime.connect_db()) as db, db:
        rows = db.execute('SELECT id,url,document_id FROM daily_links ORDER BY id').fetchall()
        document_ids = {url.rsplit('/', 1)[-1]: document_id for _, url, document_id in rows}
        for link_id, url, document_id in rows:
            identity = url.rsplit('/', 1)[-1]
            if identity == 'pending':
                db.execute("UPDATE daily_links SET state='pending',message='' WHERE id=?", (link_id,))
            elif identity in ('failed', 'uncertain'):
                db.execute('UPDATE daily_links SET state=? WHERE id=?',
                           ('model_failed' if identity == 'failed' else 'model_uncertain', link_id))
                db.execute('UPDATE source_documents SET model_status=? WHERE id=?',
                           ('failed' if identity == 'failed' else 'extracting', document_id))
                db.execute('INSERT INTO daily_model_attempts(document_id,run_id,status) VALUES (?,?,?)',
                           (document_id, first['id'], identity))
        attempts = db.execute('SELECT * FROM daily_model_attempts ORDER BY document_id').fetchall()
    put_job_pages(job_environment, [[], [{'id': identity, 'startAt': '2026-10-20 10:00:00'} for identity in identities]])
    job_environment.fetches.clear()
    second = daily.run_once(now=NOW + timedelta(days=1), mode='new')['sources'][0]
    assert second['discovered'] == 0 and second['extracted'] == 2
    assert second['history_deferred'] == 0 and second['model_failures'] == 0
    assert daily.get_status(mode='history')['sources'][0]['model_failures'] == 2
    assert len(job_environment.posts) == 2
    assert not any('/specifics/' in url for url in job_environment.fetches)
    with closing(runtime.connect_db()) as db:
        rows = db.execute('SELECT url,state,eligible,document_id FROM daily_links').fetchall()
        for url, state, eligible, document_id in rows:
            identity = url.rsplit('/', 1)[-1]
            assert eligible == int(identity in ('deferred', 'pending')) and document_id == document_ids[identity]
            assert state == {'failed': 'model_failed', 'uncertain': 'model_uncertain'}.get(identity, 'done')
        for attempt in attempts:
            assert db.execute('SELECT * FROM daily_model_attempts WHERE document_id=?', (attempt[0],)).fetchone() == attempt
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 4
    job_environment.fetches.clear()
    daily.run_once(now=NOW + timedelta(days=2), mode='history')
    assert not any('/specifics/' in url for url in job_environment.fetches)
    assert len(job_environment.posts) == 2


@pytest.mark.parametrize('far_state', ['pending', 'ready'])
@pytest.mark.parametrize('far_start', ['2026-12-01', '2026-10-16T18:00:00+08:00'])
def test_nearest_upcoming_event_precedes_far_future_pending_or_ready_work(job_environment, far_state, far_start):
    put_job_pages(job_environment, [[], [{'id': 'far'}, {'id': 'tomorrow'}]])
    daily.run_once(now=NOW, mode='new')
    if far_state == 'ready':
        daily.run_once(now=NOW, mode='history')  # only far raw, bounded to one original
    with closing(runtime.connect_db()) as db, db:
        db.execute("UPDATE daily_links SET state=?,eligible=1,priority_kind='event',priority_date='2026-12-01' WHERE url LIKE '%/far'", (far_state,))
    put_job_pages(job_environment, [[], [{'id': 'far', 'startAt': far_start},
                                         {'id': 'tomorrow', 'startAt': '2026-10-16T09:00:00+08:00'}]])
    job_environment.fetches.clear()
    result = daily.run_once(now=NOW + timedelta(days=1), mode='new')['sources'][0]
    assert result['extracted'] == 2 and len(job_environment.posts) == 2
    posted_titles = [json.loads(payload['input'][0]['content'][0]['text'].split('\n', 1)[1])['title']
                     for payload in job_environment.posts]
    assert set(posted_titles) == {'公开招聘会tomorrow', '公开招聘会far'}
    if far_state == 'pending':
        assert job_environment.fetches.index('https://job.nju.edu.cn/api/career/job/fair/specifics/tomorrow') < job_environment.fetches.index('https://job.nju.edu.cn/api/career/job/fair/specifics/far')
    with closing(runtime.connect_db()) as db:
        states = dict(db.execute('SELECT url,state FROM daily_links'))
    assert states['https://job.nju.edu.cn/career/specifics/tomorrow'] == 'done'
    assert states['https://job.nju.edu.cn/career/specifics/far'] == 'done'


def test_fresh_eligible_fetch_retry_finishes_before_retained_archive_capture(environment):
    current = 'https://college.nju.edu.cn/info/1001/1.htm'
    environment.resources[current] = acquisition.AcquisitionError('公开读取暂时失败。', 'source_unavailable')
    environment.put(ROOT, listing([1], '/notices/2.htm'))
    environment.put('https://college.nju.edu.cn/notices/2.htm', listing([1], '/notices/3.htm'))
    environment.put('https://college.nju.edu.cn/notices/3.htm', listing([2]))
    environment.put('https://college.nju.edu.cn/info/1001/2.htm', article(2, published='2026-10-13'))
    daily.run_once(now=NOW, mode='new')
    environment.put(current, article(1))
    environment.fetches.clear()
    result = source_result(daily.run_once(now=NOW + timedelta(days=1), mode='new'))
    assert result['extracted'] == 1 and result['pending'] == 0
    assert len(environment.posts) == 1 and result['history_deferred'] == 0
    archive = 'https://college.nju.edu.cn/info/1001/2.htm'
    assert archive not in environment.fetches
    daily.run_once(now=NOW + timedelta(days=1), mode='history')
    daily.run_once(now=NOW + timedelta(days=1), mode='history')
    assert daily.get_status(mode='history')['sources'][0]['history_deferred'] == 1
    assert environment.fetches.index(current) < environment.fetches.index(archive)




def test_newest_explicit_publication_precedes_older_listing_order(job_environment):
    put_job_pages(job_environment, [[], [{'id': 'old', 'publishedAt': '2026-10-14'},
                                         {'id': 'new', 'publishedAt': '2026-10-16'}]])
    result = daily.run_once(now=NOW, mode='new')['sources'][0]
    assert result['extracted'] == 2
    posted_titles = [json.loads(payload['input'][0]['content'][0]['text'].split('\n', 1)[1])['title']
                     for payload in job_environment.posts]
    assert set(posted_titles) == {'公开招聘会new', '公开招聘会old'}
    assert job_environment.fetches.index('https://job.nju.edu.cn/api/career/job/fair/specifics/new') < job_environment.fetches.index('https://job.nju.edu.cn/api/career/job/fair/specifics/old')
    with closing(runtime.connect_db()) as db:
        states = dict(db.execute('SELECT url,state FROM daily_links'))
        assert db.execute("SELECT priority_kind FROM daily_links WHERE url LIKE '%/new'").fetchone()[0] == 'publication'
    assert states['https://job.nju.edu.cn/career/specifics/new'] == 'done'
    assert states['https://job.nju.edu.cn/career/specifics/old'] == 'done'


def test_upcoming_event_priority_does_not_replace_publication_evidence(job_environment):
    put_job_pages(job_environment, [[], [{'id': 'separate', 'publishedAt': '2024-10-01',
                                        'startAt': '2026-10-14T16:30:00Z'}]])
    job_environment.put('https://job.nju.edu.cn/api/career/job/fair/specifics/separate',
                        json.dumps({'id': 'separate', 'theme': '公开招聘会',
                                    'intro': '公开活动正文，来源发布日期与活动开始时间不同。',
                                    'publishedAt': '2024-10-01 09:00:00',
                                    'startAt': '2026-10-14T16:30:00Z', 'status': 'PUBLISHED'}), 'application/json')
    result = daily.run_once(now=NOW, mode='new')['sources'][0]
    assert result['extracted'] == 1
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT priority_kind,priority_date FROM daily_links').fetchone() == (
            'event', '2026-10-15T00:30:00+08:00')
        assert db.execute('SELECT source_published_date FROM source_documents').fetchone()[0] == '2024-10-01'
    source = json.loads(job_environment.posts[0]['input'][0]['content'][0]['text'].split('\n', 1)[1])
    assert source['source_published_date'] == '2024-10-01'
    assert source['raw_date_text'] == '2024-10-01 09:00:00'


def test_queue_priority_schema_migrates_without_changing_historical_work(environment):
    with closing(runtime.connect_db()) as db, db:
        db.execute('DROP TABLE daily_links')
        db.execute('''CREATE TABLE daily_links (
            id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, url TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', eligible INTEGER NOT NULL,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            checked_at TEXT, attempt_run INTEGER, document_id INTEGER,
            message TEXT NOT NULL DEFAULT '', UNIQUE(source_id,url))''')
        db.execute('''INSERT INTO daily_links(source_id,url,state,eligible,first_seen,last_seen,attempt_run,message)
            VALUES ('test-college',?,'model_failed',0,?,?,7,'existing failure')''',
                   ('https://college.nju.edu.cn/info/1001/1.htm', NOW.isoformat(), NOW.isoformat()))
        db.execute('DROP TABLE nannanews_schema')
    migrations.migrate()
    migrations.migrate()
    with closing(runtime.connect_db()) as db:
        assert db.execute('''SELECT state,eligible,attempt_run,message,priority_date,priority_kind
            FROM daily_links''').fetchone() == ('model_failed', 0, 7, 'existing failure', None, None)


@pytest.fixture
def legacy_job_frontier(job_environment, monkeypatch):
    """A pre-upgrade queue has checked near pages and an untouched archive tail."""
    pages = [[], *[[{'id': f'current{page}'}] for page in range(1, 4)],
             *[[{'id': f'archive{page}'}] for page in range(4, 9)]]
    put_job_pages(job_environment, pages)
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, collection_page_limit=3))
    first = daily.run_once(now=NOW, mode='new')['sources'][0]
    assert first['fetched'] == 0 and first['history_deferred'] == 0
    with closing(runtime.connect_db()) as db, db:
        # Legacy rows migrate with frontier=0; do not seed article targets or
        # replace the actual next-page traversal that created this backlog.
        db.execute('UPDATE daily_pages SET frontier=0')
    return job_environment, pages


def test_current_frontier_precedes_untouched_archives_without_inheriting_archive_branch(legacy_job_frontier, monkeypatch):
    boundary, pages = legacy_job_frontier
    for page in (1, 2, 3):
        pages[page][0]['startAt'] = '2026-10-30 14:00:00'
    put_job_pages(boundary, pages)
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, collection_page_limit=4))
    boundary.fetches.clear()
    result = daily.run_once(now=NOW + timedelta(days=1), mode='new')['sources'][0]
    listing_fetches = [url for url in boundary.fetches if url.startswith(JOB_ROOT.split('?', 1)[0] + '?')]
    assert listing_fetches == [JOB_ROOT.replace('page=0', f'page={page}') for page in (0, 1, 2, 4, 3)]
    assert result['extracted'] == 3 and len(boundary.posts) == 3
    with closing(runtime.connect_db()) as db:
        assert db.execute("SELECT COUNT(*) FROM daily_links WHERE url LIKE '%/current%' AND eligible=1 AND state='done'").fetchone()[0] == 3
        # The reserved archive fetch discovers page5, but it cannot hijack page3's
        # current frontier. Page4 resumes next run after both paths meet there.
        assert db.execute('SELECT frontier FROM daily_pages WHERE url=?',
                          (JOB_ROOT.replace('page=0', 'page=4'),)).fetchone()[0] == 1
        assert db.execute('SELECT frontier FROM daily_pages WHERE url=?',
                          (JOB_ROOT.replace('page=0', 'page=5'),)).fetchone()[0] == 0


def test_single_continuation_slot_persists_frontier_and_archive_fairness_within_day(legacy_job_frontier, monkeypatch):
    boundary, pages = legacy_job_frontier
    pages[3][0]['startAt'] = '2026-10-30 14:00:00'
    put_job_pages(boundary, pages)
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, collection_page_limit=1))
    for minute, page in enumerate((1, 2, 4, 3, 4, 1, 5), start=1):
        boundary.fetches.clear()
        daily.run_once(now=NOW + timedelta(minutes=minute), mode='new')
        listing_fetches = [url for url in boundary.fetches if url.startswith(JOB_ROOT.split('?', 1)[0] + '?')]
        assert listing_fetches == [JOB_ROOT, JOB_ROOT.replace('page=0', f'page={page}')]
    assert len(boundary.posts) == 1
    with closing(runtime.connect_db()) as db:
        assert db.execute("SELECT eligible,state FROM daily_links WHERE url LIKE '%/current3'").fetchone() == (1, 'done')
        assert db.execute('SELECT page_fetch_count FROM daily_source_state').fetchone()[0] == 10
        assert db.execute('SELECT frontier FROM daily_pages WHERE url=?',
                          (JOB_ROOT.replace('page=0', 'page=6'),)).fetchone()[0] == 1


def test_new_beijing_day_refreshes_near_front_and_retains_old_frontier_backlog(legacy_job_frontier, monkeypatch):
    boundary, pages = legacy_job_frontier
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, collection_page_limit=1))
    for minute in (1, 2, 3):
        daily.run_once(now=NOW + timedelta(minutes=minute), mode='new')
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT frontier FROM daily_pages WHERE url=?',
                          (JOB_ROOT.replace('page=0', 'page=3'),)).fetchone()[0] == 1
    pages[1][0]['startAt'] = '2026-10-30 14:00:00'
    put_job_pages(boundary, pages)
    boundary.fetches.clear()
    # UTC is still October 14, but the publisher's calendar has crossed midnight.
    next_day = datetime(2026, 10, 14, 16, 1, tzinfo=timezone.utc)
    daily.run_once(now=next_day, mode='new')
    listing_fetches = [url for url in boundary.fetches if url.startswith(JOB_ROOT.split('?', 1)[0] + '?')]
    assert listing_fetches == [JOB_ROOT, JOB_ROOT.replace('page=0', 'page=1')]
    assert len(boundary.posts) == 1
    with closing(runtime.connect_db()) as db:
        assert db.execute("SELECT eligible,state FROM daily_links WHERE url LIKE '%/current1'").fetchone() == (1, 'done')
        assert db.execute('SELECT state,frontier FROM daily_pages WHERE url=?',
                          (JOB_ROOT.replace('page=0', 'page=3'),)).fetchone() == ('pending', 0)
        assert db.execute('SELECT page_fetch_count FROM daily_source_state').fetchone()[0] == 7


def test_daily_frontier_reroot_preserves_deep_progress_with_one_continuation_slot(legacy_job_frontier, monkeypatch):
    boundary, _ = legacy_job_frontier
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, collection_page_limit=1))
    for day, page in enumerate((1, 1, 4, 1, 1, 5), start=1):
        boundary.fetches.clear()
        daily.run_once(now=NOW + timedelta(days=day), mode='new')
        listing_fetches = [url for url in boundary.fetches if url.startswith(JOB_ROOT.split('?', 1)[0] + '?')]
        assert listing_fetches == [JOB_ROOT, JOB_ROOT.replace('page=0', f'page={page}')]
    assert not boundary.posts
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT page_fetch_count FROM daily_source_state').fetchone()[0] == 9
        for page in (4, 5):
            assert db.execute('SELECT state FROM daily_pages WHERE url=?',
                              (JOB_ROOT.replace('page=0', f'page={page}'),)).fetchone()[0] == 'done'


@pytest.mark.parametrize('initial_state', ['pending', 'ready'])
def test_all_current_originals_beyond_previous_cap_finish_without_retrying_failed_version(environment, monkeypatch, initial_state):
    identities = range(1, 32)
    urls = [f'https://college.nju.edu.cn/info/1001/{identity}.htm' for identity in identities]
    environment.put(ROOT, listing(identities))
    for identity, url in zip(identities, urls):
        environment.put(url, article(identity))
    if initial_state == 'ready':
        daily.initialize_sources()
        with closing(runtime.connect_db()) as db:
            key = json.loads(db.execute('SELECT definition_json FROM daily_source_state').fetchone()[0])['definition_key']
        previous = (NOW - timedelta(days=1)).isoformat()
        for url in urls:
            saved = information.save_document(acquisition.fetch_document(url), source_id='test-college', actor='system:daily')
            with closing(runtime.connect_db()) as db, db:
                db.execute('''INSERT INTO daily_links(source_id,url,state,eligible,first_seen,last_seen,document_id,definition_key)
                    VALUES ('test-college',?,'ready',1,?,?,?,?)''', (url, previous, previous, saved['id'], key))
    failed_url = urls[6]

    def selective_failure(payload, *, audit=None):
        source = json.loads(payload['input'][0]['content'][0]['text'].split('\n', 1)[1])
        if source['canonical_url'] == failed_url:
            environment.posts.append(payload)
            raise acquisition.AcquisitionError('已发送的模型请求失败。', 'model_http_error')
        return environment.model(payload)

    monkeypatch.setattr(acquisition, '_model_post', selective_failure)
    environment.fetches.clear()
    first = source_result(daily.run_once(now=NOW, mode='new'))
    assert first['extracted'] == len(urls) - 1 and first['failed'] == 1
    assert first['pending'] == 0 and first['model_failures'] == 1
    posted_urls = [json.loads(payload['input'][0]['content'][0]['text'].split('\n', 1)[1])['canonical_url']
                   for payload in environment.posts]
    assert len(posted_urls) == len(urls) and set(posted_urls) == set(urls)
    assert posted_urls.count(failed_url) == 1
    with closing(runtime.connect_db()) as db:
        attempts = db.execute('SELECT * FROM daily_model_attempts ORDER BY document_id').fetchall()
        assert len(attempts) == len(urls)
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == len(urls)
    environment.fetches.clear()
    second = source_result(daily.run_once(now=NOW + timedelta(days=1), mode='new'))
    assert second['extracted'] == 0 and second['pending'] == 0 and second['model_failures'] == 1
    assert len(environment.posts) == len(urls)
    for url in urls:
        assert environment.fetches.count(url) == 1
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT * FROM daily_model_attempts ORDER BY document_id').fetchall() == attempts


def test_unknown_archive_raw_is_bounded_and_never_captured_by_new_runs(environment):
    environment.put(ROOT, listing([1], '/notices/2.htm'))
    environment.put('https://college.nju.edu.cn/notices/2.htm', listing(range(2, 36)))
    for identity in range(2, 36):
        environment.put(f'https://college.nju.edu.cn/info/1001/{identity}.htm', article(identity, published=None))
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert result['discovered'] == result['fetched'] == result['extracted'] == 1
    assert result['status'] == 'complete' and result['pending'] == 0
    for day in range(1, 4):
        history = source_result(daily.run_once(now=NOW + timedelta(days=day), mode='history'))
        assert history['fetched'] == 1 and history['extracted'] == 0
    assert len(environment.posts) == 1
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 4
        assert db.execute("SELECT COUNT(*) FROM daily_links WHERE eligible=0 AND state='history_deferred' AND document_id IS NOT NULL").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM daily_links WHERE eligible=0 AND state='pending'").fetchone()[0] == 31
    environment.fetches.clear()
    current = source_result(daily.run_once(now=NOW + timedelta(days=4), mode='new'))
    assert current['status'] == 'no_change' and current['pending'] == 0
    assert not any(url.endswith(f'/{identity}.htm') for url in environment.fetches for identity in range(2, 36))
    first_page = daily.list_items(mode='history', page=1, per_page=20)
    second_page = daily.list_items(mode='history', page=2, per_page=20)
    assert first_page['total'] == second_page['total'] == 34
    assert first_page['pages'] == second_page['pages'] == 2
    assert len({row['id'] for row in first_page['items'] + second_page['items']}) == 34
    assert daily.list_items(mode='history', state='history_deferred')['total'] == 3
    assert daily.list_items(mode='history', state='pending')['total'] == 31
    assert daily.list_items(mode='history', source_id='not-configured')['total'] == 0


@pytest.mark.parametrize(('published', 'eligible'), [
    ('2026-10-14', True),
    ('2026-10-15', True),
    ('2026-10-13', False),
    ('2026-02-30', False),
    ('not-a-date', False),
    (None, False),
])
def test_new_continuation_original_qualifies_only_from_valid_fetched_publication(environment, published, eligible):
    environment.put(ROOT, listing([1], '/notices/2.htm'))
    environment.put('https://college.nju.edu.cn/notices/2.htm', listing([1], '/notices/3.htm'))
    environment.put('https://college.nju.edu.cn/notices/3.htm', listing([2]))
    url = 'https://college.nju.edu.cn/info/1001/2.htm'
    environment.put(url, article(2, '正文提到2099年的活动，但它不是来源发布时间', published=published))
    daily.run_once(now=NOW, mode='new')
    assert len(environment.posts) == 1
    assert url not in environment.fetches
    for _ in range(2):
        history = source_result(daily.run_once(now=NOW, mode='history'))
        assert history['extracted'] == 0
    assert len(environment.posts) == 1
    result = source_result(daily.run_once(now=NOW + timedelta(days=1), mode='new'))
    assert result['discovered'] == 0 and result['extracted'] == int(eligible)
    assert len(environment.posts) == 1 + int(eligible)
    assert url in environment.fetches
    with closing(runtime.connect_db()) as db:
        row = db.execute('SELECT eligible,state,document_id,priority_kind,priority_date FROM daily_links WHERE url=?', (url,)).fetchone()
        assert row[0] == int(eligible) and row[1] == ('done' if eligible else 'history_deferred')
        assert row[2] is not None
        valid = published in ('2026-10-13', '2026-10-14', '2026-10-15')
        assert row[3:] == (('publication', published) if valid else (None, None))
        assert db.execute('SELECT source_published_date FROM source_documents WHERE id=?', (row[2],)).fetchone()[0] == (
            published if published in ('2026-10-13', '2026-10-14', '2026-10-15') else None)


def test_job_event_start_in_fetched_original_never_becomes_publication_eligibility(job_environment):
    put_job_pages(job_environment, [[], [{'id': 'detail-only-event'}]])
    job_environment.put('https://job.nju.edu.cn/api/career/job/fair/specifics/detail-only-event',
                        json.dumps({'id': 'detail-only-event', 'theme': '公开招聘会',
                                    'intro': '公开活动正文，无来源发布时间。',
                                    'startAt': '2026-10-30 14:00:00', 'status': 'PUBLISHED'}), 'application/json')
    new = daily.run_once(now=NOW, mode='new')['sources'][0]
    assert new['fetched'] == 0
    result = daily.run_once(now=NOW, mode='history')['sources'][0]
    assert result['fetched'] == 1 and result['history_deferred'] == 1 and result['extracted'] == 0
    assert not job_environment.posts
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT eligible FROM daily_links').fetchone()[0] == 0
        assert db.execute('SELECT source_published_date FROM source_documents').fetchone()[0] is None


@pytest.mark.parametrize(('published', 'eligible'), [('2025-04-01', False), ('2026-10-14', True)])
def test_initial_root_fetched_publication_controls_paid_eligibility(environment, published, eligible):
    url = 'https://college.nju.edu.cn/info/1001/1.htm'
    environment.put(url, article(1, published=published))
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert result['fetched'] == 1 and result['extracted'] == int(eligible)
    assert result['history_deferred'] == 0
    assert daily.get_status(mode='history')['sources'][0]['history_deferred'] == int(not eligible)
    assert len(environment.posts) == int(eligible)
    with closing(runtime.connect_db()) as db:
        row = db.execute('SELECT eligible,state,document_id FROM daily_links').fetchone()
        assert row[:2] == (int(eligible), 'done' if eligible else 'history_deferred')
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == int(eligible)
    stored = information.get_document(row[2])
    assert stored['source_published_date'] == published
    assert '原始正文' in stored['text']


@pytest.mark.parametrize(('published', 'eligible'), [
    ('2025-04-01', False),
    ('2026-10-14', True),
    ('2026-10-13T16:30:00Z', True),
    ('2026-10-14T00:30:00+14:00', False),
])
def test_initial_root_listing_publication_controls_paid_eligibility(job_environment, published, eligible):
    put_job_pages(job_environment, [[{'id': 'root', 'publishedAt': published}]])
    result = daily.run_once(now=NOW, mode='new')['sources'][0]
    assert result['fetched'] == int(eligible) and result['extracted'] == int(eligible)
    assert result['history_deferred'] == 0
    if not eligible:
        daily.run_once(now=NOW, mode='history')
        assert daily.get_status(mode='history')['sources'][0]['history_deferred'] == 1
    assert len(job_environment.posts) == int(eligible)
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT eligible FROM daily_links').fetchone()[0] == int(eligible)
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 1
        assert db.execute('SELECT source_published_date FROM source_documents').fetchone()[0] is None
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == int(eligible)


@pytest.mark.parametrize('initial_state', ['pending', 'ready'])
def test_legacy_eligible_old_work_retains_raw_without_paid_backfill(environment, initial_state):
    url = 'https://college.nju.edu.cn/info/1001/2.htm'
    environment.put(url, article(2, published='2025-04-01'))
    daily.initialize_sources()
    saved = information.save_document(acquisition.fetch_document(url), source_id='test-college', actor='system:daily')
    with closing(runtime.connect_db()) as db, db:
        key = json.loads(db.execute('SELECT definition_json FROM daily_source_state').fetchone()[0])['definition_key']
        db.execute('UPDATE daily_source_state SET bootstrap_at=?,bootstrap_key=?', (NOW.isoformat(), key))
        previous = (NOW - timedelta(days=1)).isoformat()
        db.execute('''INSERT INTO daily_links(source_id,url,state,eligible,first_seen,last_seen,document_id,definition_key)
            VALUES ('test-college',?,?,1,?,?,?,?)''', (url, initial_state, previous, previous, saved['id'], key))
    environment.fetches.clear()
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert result['extracted'] == 1 and result['history_deferred'] == 0
    assert daily.get_status(mode='history')['sources'][0]['history_deferred'] == 1
    assert len(environment.posts) == 1
    assert environment.fetches.count(url) == int(initial_state == 'pending')
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT eligible,state,document_id FROM daily_links WHERE url=?', (url,)).fetchone() == (
            0, 'history_deferred', saved['id'])
        assert db.execute('SELECT 1 FROM daily_model_attempts WHERE document_id=?', (saved['id'],)).fetchone() is None
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 2
    stored = information.get_document(saved['id'])
    assert stored['source_published_date'] == '2025-04-01'
    assert stored['model_status'] == 'not_extracted' and '原始正文' in stored['text']


@pytest.mark.parametrize('model_status', ['completed', 'failed', 'extracting'])
@pytest.mark.parametrize('recorded_attempt', [True, False])
def test_legacy_ready_completed_failed_or_uncertain_version_never_requests_again(environment, model_status, recorded_attempt):
    if model_status == 'failed':
        environment.model_failure = acquisition.AcquisitionError('已发送请求失败。', 'model_http_error')
    elif model_status == 'extracting':
        environment.model_failure = KeyboardInterrupt()
    if model_status == 'extracting':
        with pytest.raises(KeyboardInterrupt):
            daily.run_once(now=NOW, mode='new')
    else:
        daily.run_once(now=NOW, mode='new')
    assert len(environment.posts) == 1
    with closing(runtime.connect_db()) as db, db:
        assert db.execute('SELECT model_status FROM source_documents').fetchone()[0] == model_status
        if not recorded_attempt:
            db.execute('DELETE FROM daily_model_attempts')
        attempts = db.execute('SELECT * FROM daily_model_attempts').fetchall()
        db.execute("UPDATE daily_links SET state='ready'")
    environment.model_failure = None
    environment.fetches.clear()
    result = source_result(daily.run_once(now=NOW + timedelta(days=1), mode='new'))
    assert result['extracted'] == 0 and len(environment.posts) == 1
    assert 'https://college.nju.edu.cn/info/1001/1.htm' not in environment.fetches
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT * FROM daily_model_attempts').fetchall() == attempts
        assert db.execute('SELECT state FROM daily_links').fetchone()[0] == {
            'completed': 'done', 'failed': 'model_failed', 'extracting': 'model_uncertain'}[model_status]


@pytest.mark.parametrize('start', [None, '2026-10-14', '2026-10-15T09:00:00+08:00'])
def test_legacy_ready_old_publication_requires_valid_upcoming_listing_event(job_environment, start):
    record = {'id': 'old-ready', 'publishedAt': '2025-04-01'}
    put_job_pages(job_environment, [[record]])
    job_environment.put('https://job.nju.edu.cn/api/career/job/fair/specifics/old-ready',
                        json.dumps({'id': 'old-ready', 'theme': '公开招聘会',
                                    'intro': '旧来源发布日期与当前活动日期分别保存。',
                                    'publishedAt': '2025-04-01', 'status': 'PUBLISHED'}), 'application/json')
    daily.run_once(now=NOW, mode='new')
    first = daily.run_once(now=NOW, mode='history')['sources'][0]
    assert first['history_deferred'] == 1 and not job_environment.posts
    with closing(runtime.connect_db()) as db, db:
        db.execute("UPDATE daily_links SET state='ready',eligible=1")
        document_id = db.execute('SELECT document_id FROM daily_links').fetchone()[0]
    if start is not None:
        record['startAt'] = start
    job_environment.put(JOB_ROOT, json.dumps({'content': [record], 'page': {'number': 0, 'totalPages': 1}}), 'application/json')
    job_environment.fetches.clear()
    result = daily.run_once(now=NOW + timedelta(minutes=1), mode='new')['sources'][0]
    eligible = start is not None
    assert result['extracted'] == int(eligible) and result['history_deferred'] == 0
    assert daily.get_status(mode='history')['sources'][0]['history_deferred'] == int(not eligible)
    assert len(job_environment.posts) == int(eligible)
    assert not any(url.endswith('/old-ready') for url in job_environment.fetches)
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT eligible,state FROM daily_links').fetchone() == (
            int(eligible), 'done' if eligible else 'history_deferred')
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == int(eligible)
    assert information.get_document(document_id)['source_published_date'] == '2025-04-01'
    if eligible:
        source = json.loads(job_environment.posts[0]['input'][0]['content'][0]['text'].split('\n', 1)[1])
        assert source['source_published_date'] == '2025-04-01'


@contextmanager
def background_collection(*release_events, now=NOW, mode='new'):
    outcome = {}

    def collect():
        try:
            outcome['result'] = daily.run_once(now=now, mode=mode)
        except BaseException as error:
            outcome['error'] = error

    thread = Thread(target=collect, name='test-collector')
    thread.start()
    try:
        yield outcome, thread
    finally:
        for event in release_events:
            event.set()
        thread.join(timeout=10)
        assert not thread.is_alive(), 'collector did not drain its finite workers'


def configure_parallel_sources(boundary, sources):
    definitions = []
    for source_id, host in sources:
        root = f'https://{host}/{source_id}.htm'
        definitions.append({'id': source_id, 'name': source_id, 'url': root, 'kind': 'html',
                            'enabled': True, 'publisher_college': '测试学院', 'identity_url': root,
                            'expected_identity': '南京大学测试学院',
                            'article_pattern': r'^/info/1001/[0-9]+\.htm$'})
        boundary.put(root, listing([1]))
        boundary.put(f'https://{host}/info/1001/1.htm', article(1))
    daily.SEED_PATH.write_text(json.dumps(definitions), encoding='utf-8')


@pytest.mark.parametrize('fetch_workers', [1, 6])
def test_early_source_commits_draft_while_other_host_discovery_is_blocked(environment, monkeypatch, fetch_workers):
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, collection_fetch_workers=fetch_workers))
    # Serial mode must capture before the next discovery; six workers must also
    # avoid letting seven definitions on one blocked host exhaust the web pool.
    sources = ([('a-fast', 'fast.nju.edu.cn'), ('z-slow', 'slow.nju.edu.cn')] if fetch_workers == 1 else
               [(f'a-slow{index}', 'slow.nju.edu.cn') for index in range(7)] + [('z-fast', 'fast.nju.edu.cn')])
    configure_parallel_sources(environment, sources)
    blocked = Event()
    release = Event()
    committed = Event()
    fetch = environment.fetch
    enrich = information.enrich_document

    def public_fetch(url, **kwargs):
        if url.startswith('https://slow.nju.edu.cn/'):
            blocked.set()
            assert release.wait(10)
        return fetch(url, **kwargs)

    def observe_commit(document_id, actor, **kwargs):
        result = enrich(document_id, actor, **kwargs)
        if information.get_document(document_id)['canonical_url'].startswith('https://fast.nju.edu.cn/'):
            committed.set()
        return result

    monkeypatch.setattr(acquisition, 'fetch_public_resource', public_fetch)
    monkeypatch.setattr(information, 'enrich_document', observe_commit)
    with background_collection(release) as (outcome, thread):
        assert blocked.wait(10)
        assert committed.wait(10), 'fast publisher waited for unrelated discovery'
        assert thread.is_alive()
        with closing(runtime.connect_db()) as db:
            assert db.execute("SELECT COUNT(*) FROM entries WHERE status='described' AND publish_date IS NULL").fetchone()[0] == 1
            assert db.execute("SELECT status FROM daily_runs ORDER BY id DESC LIMIT 1").fetchone()[0] == 'running'
    assert 'error' not in outcome
    assert len(environment.posts) == 2  # Seven duplicate-source links share one document.


@pytest.mark.parametrize('fetch_workers', [1, 6])
def test_two_models_overlap_at_most_two_and_raw_capture_keeps_draining(environment, monkeypatch, fetch_workers):
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, collection_fetch_workers=fetch_workers))
    environment.put(ROOT, listing(range(1, 7)))
    both_models = Barrier(3)
    release = Event()
    all_raw = Event()
    lock = Lock()
    counts = {'active': 0, 'peak': 0, 'started': 0}
    save = information.save_document

    def paid_post(payload, *, audit=None):
        with lock:
            counts['active'] += 1
            counts['started'] += 1
            first_pair = counts['started'] <= 2
            counts['peak'] = max(counts['peak'], counts['active'])
        try:
            if first_pair:
                both_models.wait(timeout=10)
                assert release.wait(10)
            return environment.model(payload)
        finally:
            with lock:
                counts['active'] -= 1

    def observe_saved(document, source_id, actor):
        result = save(document, source_id, actor)
        if document['canonical_url'].endswith('/6.htm'):
            all_raw.set()
        return result

    monkeypatch.setattr(acquisition, '_model_post', paid_post)
    monkeypatch.setattr(information, 'save_document', observe_saved)
    with background_collection(release) as (outcome, _):
        both_models.wait(timeout=10)
        assert all_raw.wait(10), 'web capture stalled behind blocked paid calls'
        with lock:
            assert counts['active'] == counts['started'] == counts['peak'] == 2
        with closing(runtime.connect_db()) as db:
            assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 6
    assert 'error' not in outcome
    assert counts == {'active': 0, 'peak': 2, 'started': 6}
    assert source_result(outcome['result'])['extracted'] == 6


def test_serial_transport_prioritizes_persisted_upcoming_job_before_unrelated_discovery(job_environment, monkeypatch):
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, collection_fetch_workers=1))
    job_definition = json.loads(daily.SEED_PATH.read_text(encoding='utf-8'))[0]
    configure_parallel_sources(job_environment, [('a-slow', 'slow.nju.edu.cn')])
    definitions = json.loads(daily.SEED_PATH.read_text(encoding='utf-8'))
    daily.SEED_PATH.write_text(json.dumps(definitions + [job_definition]), encoding='utf-8')
    slow_root = 'https://slow.nju.edu.cn/a-slow.htm'
    job_environment.put(slow_root, listing([]))
    put_job_pages(job_environment, [[{'id': 'upcoming', 'startAt': (NOW + timedelta(hours=1)).isoformat()}]])
    detail = 'https://job.nju.edu.cn/api/career/job/fair/specifics/upcoming'
    response = job_environment.resources[detail]
    job_environment.resources[detail] = acquisition.AcquisitionError('暂时连接失败。', 'connection')
    previous = daily.run_once(now=NOW - timedelta(minutes=1), mode='new')
    assert next(row for row in previous['sources'] if row['source_id'] == 'job')['failed'] == 1
    assert not job_environment.posts
    job_environment.resources[detail] = response
    blocked = Event()
    release = Event()
    committed = Event()
    fetch = job_environment.fetch
    enrich = information.enrich_document

    def public_fetch(url, **kwargs):
        if url == slow_root:
            blocked.set()
            assert release.wait(10)
        return fetch(url, **kwargs)

    def observe_commit(document_id, actor, **kwargs):
        result = enrich(document_id, actor, **kwargs)
        if information.get_document(document_id)['canonical_url'].endswith('/career/specifics/upcoming'):
            committed.set()
        return result

    monkeypatch.setattr(acquisition, 'fetch_public_resource', public_fetch)
    monkeypatch.setattr(information, 'enrich_document', observe_commit)
    with background_collection(release) as (outcome, thread):
        assert blocked.wait(10)
        assert committed.wait(10), 'upcoming queued job waited behind unrelated source discovery'
        assert thread.is_alive()
        with closing(runtime.connect_db()) as db:
            assert db.execute("SELECT COUNT(*) FROM entries WHERE status='described' AND publish_date IS NULL").fetchone()[0] == 1
    assert 'error' not in outcome
    assert len(job_environment.posts) == 1
    with closing(runtime.connect_db()) as db:
        assert db.execute("SELECT state FROM daily_links WHERE source_id='job'").fetchone()[0] == 'done'


@pytest.mark.parametrize('prior_failure', [False, True])
def test_serial_capture_rechecks_nearer_prior_version_before_far_pending_work(job_environment, monkeypatch, prior_failure):
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, collection_fetch_workers=1))
    prior = {'id': 'prior', 'startAt': (NOW + timedelta(minutes=30)).isoformat()}
    put_job_pages(job_environment, [[prior]])
    if prior_failure:
        job_environment.model_failure = acquisition.AcquisitionError('请求已发送但失败。', 'model_http_error')
    daily.run_once(now=NOW - timedelta(minutes=1), mode='new')
    job_environment.model_failure = None
    put_job_pages(job_environment, [[prior,
        {'id': 'near', 'startAt': (NOW + timedelta(hours=1)).isoformat()},
        {'id': 'far', 'startAt': (NOW + timedelta(days=2)).isoformat()}]])
    blocked = Event()
    release = Event()
    committed = Event()
    fetch = job_environment.fetch
    enrich = information.enrich_document

    def public_fetch(url, **kwargs):
        if url.endswith('/api/career/job/fair/specifics/far'):
            blocked.set()
            assert release.wait(10)
        return fetch(url, **kwargs)

    def observe_commit(document_id, actor, **kwargs):
        result = enrich(document_id, actor, **kwargs)
        if information.get_document(document_id)['canonical_url'].endswith('/career/specifics/near'):
            committed.set()
        return result

    monkeypatch.setattr(acquisition, 'fetch_public_resource', public_fetch)
    monkeypatch.setattr(information, 'enrich_document', observe_commit)
    with background_collection(release) as (outcome, _):
        assert blocked.wait(10)
        assert committed.wait(3), 'near-event draft was blocked by conflicting capture/model queue precedence'
        with closing(runtime.connect_db()) as db:
            assert db.execute("SELECT COUNT(*) FROM entries WHERE title='公开招聘会near' AND status='described'").fetchone()[0] == 1
    assert 'error' not in outcome
    posted_titles = [json.loads(payload['input'][0]['content'][0]['text'].split('\n', 1)[1])['title']
                     for payload in job_environment.posts]
    assert posted_titles.count('公开招聘会prior') == 1
    assert set(posted_titles) == {'公开招聘会prior', '公开招聘会near', '公开招聘会far'}


def test_duplicate_sources_share_one_atomic_paid_claim_and_keep_captured_evidence(environment, monkeypatch):
    configure_parallel_sources(environment, [('a', 'college.nju.edu.cn'), ('b', 'college.nju.edu.cn')])
    entered = Event()
    release = Event()
    duplicate_saved = Event()
    save = information.save_document

    def paid_post(payload, *, audit=None):
        entered.set()
        assert release.wait(10)
        return environment.model(payload)

    def observe_saved(document, source_id, actor):
        if source_id == 'b':
            assert entered.wait(10)
        saved = save(document, source_id, actor)
        if source_id == 'b':
            duplicate_saved.set()
        return saved

    monkeypatch.setattr(acquisition, '_model_post', paid_post)
    monkeypatch.setattr(information, 'save_document', observe_saved)
    with background_collection(release) as (outcome, _):
        assert entered.wait(10)
        assert duplicate_saved.wait(10)
    assert 'error' not in outcome
    assert len(environment.posts) == 1
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT state FROM daily_links').fetchall() == [('done',), ('done',)]
        assert db.execute('SELECT status FROM daily_model_attempts').fetchall() == [('done',)]
        captured = json.loads(db.execute('SELECT document_json FROM source_documents').fetchone()[0])
        assert '原始正文' in captured['text'] and 'model_output' not in captured
        assert db.execute('SELECT model_status FROM source_document_model_attempts').fetchone() == ('completed',)


def test_parallel_publication_gate_and_failed_version_guards_preserve_raw(environment, monkeypatch):
    environment.put(ROOT, listing(range(1, 7)))
    for identity in range(3, 7):
        environment.put(f'https://college.nju.edu.cn/info/1001/{identity}.htm', article(identity, published='2025-04-01'))
    overlap = Barrier(2)

    def paid_post(payload, *, audit=None):
        overlap.wait(timeout=10)
        source = json.loads(payload['input'][0]['content'][0]['text'].split('\n', 1)[1])
        if source['canonical_url'].endswith('/1.htm'):
            environment.posts.append(payload)
            raise acquisition.AcquisitionError('请求已发送但失败。', 'model_http_error')
        return environment.model(payload)

    monkeypatch.setattr(acquisition, '_model_post', paid_post)
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert result['extracted'] == result['model_failures'] == 1
    assert result['history_deferred'] == 0 and result['fetched'] == 6
    assert daily.get_status(mode='history')['sources'][0]['history_deferred'] == 4
    assert len(environment.posts) == 2
    monkeypatch.setattr(acquisition, '_model_post', environment.model)
    second = source_result(daily.run_once(now=NOW + timedelta(days=1), mode='new'))
    assert second['extracted'] == 0 and second['model_failures'] == 1
    assert second['history_deferred'] == 0 and len(environment.posts) == 2
    assert daily.get_status(mode='history')['sources'][0]['history_deferred'] == 4
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 6
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 2


@pytest.mark.parametrize('interrupt_at', ['web', 'model'])
def test_interrupt_drains_active_paid_output_before_recovery_and_unlock(environment, monkeypatch, interrupt_at):
    environment.put(ROOT, listing([1, 2]))
    entered = Event()
    interrupted = Event()
    release = Event()
    pair = Barrier(2)
    fetch = environment.fetch

    def public_fetch(url, **kwargs):
        if interrupt_at == 'web' and url.endswith('/2.htm'):
            assert entered.wait(10)
            interrupted.set()
            raise KeyboardInterrupt()
        return fetch(url, **kwargs)

    def paid_post(payload, *, audit=None):
        source = json.loads(payload['input'][0]['content'][0]['text'].split('\n', 1)[1])
        if interrupt_at == 'model':
            pair.wait(timeout=10)
            if source['canonical_url'].endswith('/2.htm'):
                environment.posts.append(payload)
                interrupted.set()
                raise KeyboardInterrupt()
        entered.set()
        assert release.wait(10)
        return environment.model(payload)

    monkeypatch.setattr(acquisition, 'fetch_public_resource', public_fetch)
    monkeypatch.setattr(acquisition, '_model_post', paid_post)
    with background_collection(release) as (outcome, thread):
        assert interrupted.wait(10)
        assert thread.is_alive()
        assert 'error' not in outcome
        with closing(runtime.connect_db()) as db:
            assert db.execute('SELECT status FROM daily_runs').fetchone()[0] == 'running'
        with pytest.raises(RuntimeError, match='already running'):
            with daily._collection_lock():
                pytest.fail('collector released its OS lock before active work drained')
    assert isinstance(outcome.get('error'), KeyboardInterrupt)
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT status FROM daily_runs').fetchone()[0] == 'interrupted'
        assert db.execute('SELECT COUNT(*) FROM entries').fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM source_documents WHERE model_status='completed'").fetchone()[0] == 1
        attempts = db.execute('SELECT status FROM daily_model_attempts ORDER BY document_id').fetchall()
        assert attempts.count(('done',)) == 1
        assert attempts.count(('uncertain',)) == int(interrupt_at == 'model')
        states = db.execute('SELECT id,state,document_id,message FROM daily_links ORDER BY id').fetchall()
    assert not any(thread.name.startswith(('daily-web', 'daily-model')) for thread in running_threads())
    with daily._collection_lock():
        with closing(runtime.connect_db()) as db:
            assert db.execute('SELECT status FROM daily_runs').fetchone()[0] == 'interrupted'
            assert db.execute('SELECT COUNT(*) FROM entries').fetchone()[0] == 1
            assert db.execute('SELECT id,state,document_id,message FROM daily_links ORDER BY id').fetchall() == states
    monkeypatch.setattr(acquisition, 'fetch_public_resource', environment.fetch)
    monkeypatch.setattr(acquisition, '_model_post', environment.model)
    posts = len(environment.posts)
    daily.run_once(now=NOW + timedelta(days=1), mode='new')
    assert len(environment.posts) == posts + int(interrupt_at == 'web')


def test_six_distinct_hosts_discover_concurrently_without_queuing_a_seventh(environment, monkeypatch):
    configure_parallel_sources(environment, [(f'source{index}', f'host{index}.nju.edu.cn') for index in range(7)])
    first_wave = Barrier(7)
    release = Event()
    lock = Lock()
    blocked_roots = set()
    counts = {'active': 0, 'peak': 0, 'started': 0}
    fetch = environment.fetch

    def public_fetch(url, **kwargs):
        with lock:
            first = '/info/' not in url and url not in blocked_roots
            if first:
                blocked_roots.add(url)
                counts['active'] += 1
                counts['started'] += 1
                wave = counts['started'] <= 6
                counts['peak'] = max(counts['peak'], counts['active'])
        if first:
            try:
                if wave:
                    first_wave.wait(timeout=10)
                    assert release.wait(10)
            finally:
                with lock:
                    counts['active'] -= 1
        return fetch(url, **kwargs)

    monkeypatch.setattr(acquisition, 'fetch_public_resource', public_fetch)
    with background_collection(release) as (outcome, _):
        first_wave.wait(timeout=10)
        with lock:
            assert counts['active'] == counts['started'] == counts['peak'] == 6
    assert 'error' not in outcome
    assert counts == {'active': 0, 'peak': 6, 'started': 7}
    assert sum(source['extracted'] for source in outcome['result']['sources']) == 7


def test_history_terminal_raw_outcomes_survive_restart_and_homepage_rediscovery(environment, monkeypatch):
    environment.put(ROOT, listing([1], '/notices/2.htm'))
    environment.put('https://college.nju.edu.cn/notices/2.htm', listing([2, 3]))
    failed_url = 'https://college.nju.edu.cn/info/1001/2.htm'
    raw_url = 'https://college.nju.edu.cn/info/1001/3.htm'
    environment.resources[failed_url] = acquisition.AcquisitionError('公开原文读取失败。', 'source_unavailable')
    environment.put(raw_url, article(3, published='2024-01-01'))
    new = daily.run_once(now=NOW, mode='new')
    daily.run_once(now=NOW, mode='history')
    daily.run_once(now=NOW, mode='history')
    first = {row['url']: row for row in daily.list_items(mode='history')['items']}
    assert first[failed_url]['state'] == 'fetch_failed'
    assert first[failed_url]['processing_label'] == '读取失败'
    assert first[failed_url]['document_id'] is None and first[failed_url]['record_id'] is None
    assert first[raw_url]['state'] == 'history_deferred'
    assert first[raw_url]['processing_label'] == '原文留存成功、未提取'
    assert first[raw_url]['document_id'] and first[raw_url]['record_id']
    assert all(row['processed_at'] for row in first.values())
    clock = daily._now
    monkeypatch.setattr(daily, '_now', lambda value=None: clock(value if value is not None else NOW + timedelta(days=10)))
    environment.put(ROOT, listing([1, 2, 3], '/notices/2.htm'))
    environment.put(failed_url, article(2, published='2024-01-01'))
    migrations.migrate()  # An explicit repeat must not retry processed work.
    for day in (10, 11):
        current = daily.run_once(now=NOW + timedelta(days=day), mode='new')
        history = daily.run_once(now=NOW + timedelta(days=day), mode='history')
        assert current['status'] == 'no_change'
        assert history['sources'][0]['fetched'] == history['sources'][0]['extracted'] == 0
    final = {row['url']: row for row in daily.list_items(mode='history')['items']}
    assert {url: (row['state'], row['processed_at'], row['checked_at']) for url, row in final.items()} == {
        url: (row['state'], row['processed_at'], row['checked_at']) for url, row in first.items()}
    assert environment.fetches.count(failed_url) == environment.fetches.count(raw_url) == 1
    assert len(environment.posts) == 1
    assert daily.get_status(mode='history')['last_runs']['new']['id'] > new['id']


def test_first_seen_old_homepage_date_is_reused_without_any_second_article_read(environment, monkeypatch):
    url = 'https://college.nju.edu.cn/info/1001/1.htm'
    environment.put(url, article(1, published='2024-01-01'))
    first = daily.run_once(now=NOW, mode='new')
    assert first['status'] == 'no_change' and first['sources'][0]['fetched'] == 1
    outcome = daily.list_items(mode='history')['items'][0]
    assert outcome['state'] == 'history_deferred'
    clock = daily._now
    monkeypatch.setattr(daily, '_now', lambda value=None: clock(value if value is not None else NOW + timedelta(days=1)))
    daily.run_once(now=NOW + timedelta(days=1), mode='new')
    daily.run_once(now=NOW + timedelta(days=1), mode='history')
    assert environment.fetches.count(url) == 1 and not environment.posts
    assert daily.list_items(mode='history')['items'][0]['processed_at'] == outcome['processed_at']
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT eligible,priority_date FROM daily_links').fetchone() == (0, '2024-01-01')


@pytest.mark.parametrize('start,extracted', [('2026-10-14T19:00:00+08:00', 0), ('2026-10-14T21:00:00+08:00', 1)])
def test_expired_known_event_does_not_become_fresh_undated_homepage_work(job_environment, start, extracted):
    put_job_pages(job_environment, [[{'id': 'expired', 'startAt': start}]])
    new = daily.run_once(now=NOW, mode='new')
    assert new['sources'][0]['fetched'] == extracted and len(job_environment.posts) == extracted
    daily.run_once(now=NOW, mode='history')
    before = daily.list_items(mode='new' if extracted else 'history')['items'][0]
    put_job_pages(job_environment, [[{'id': 'expired'}]])
    job_environment.fetches.clear()
    daily.run_once(now=NOW + timedelta(days=1), mode='new')
    assert not any('/specifics/expired' in url for url in job_environment.fetches)
    assert daily.list_items(mode='history')['items'][0]['processed_at'] == before['processed_at']
    assert len(job_environment.posts) == extracted


def test_history_run_never_satisfies_daily_four_am_slot_or_advances_new_checkpoint(environment):
    slot = NOW.replace(hour=4)
    previous = slot - timedelta(hours=7)
    environment.put('https://college.nju.edu.cn/info/1001/1.htm', article(1, published=previous.date().isoformat()))
    new = daily.run_once(now=previous, mode='new')
    history = daily.run_once(now=slot + timedelta(hours=1), mode='history')
    assert history['mode'] == 'history'
    assert daily.get_status(mode='new')['last_run']['id'] == new['id']
    assert daily.get_status(mode='history')['last_run']['id'] == history['id']
    assert information.list_sources()[0]['last_success_at'] == previous.isoformat()
    assert daily._worker_due(slot + timedelta(hours=1))
    daily.run_once(now=slot + timedelta(hours=1), mode='new')
    assert not daily._worker_due(slot + timedelta(hours=1, seconds=1))
    with closing(runtime.connect_db()) as db:
        assert [row[0] for row in db.execute('SELECT mode FROM daily_runs ORDER BY id')] == ['new', 'history', 'new']


def test_history_stops_submitting_before_four_am_and_drains_inflight_fetch(environment, monkeypatch):
    environment.put(ROOT, listing([1], '/notices/2.htm'))
    environment.put('https://college.nju.edu.cn/notices/2.htm', listing([2, 3]))
    for identity in (2, 3):
        environment.put(f'https://college.nju.edu.cn/info/1001/{identity}.htm', article(identity, published='2024-01-01'))
    daily.run_once(now=NOW, mode='new')
    slot = NOW.replace(hour=4) + timedelta(days=1)
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, collection_page_limit=2))
    clock = [slot - timedelta(minutes=6)]
    original_now = daily._now
    monkeypatch.setattr(daily, '_now', lambda value=None: original_now(value if value is not None else clock[0]))
    entered, release = Event(), Event()
    fetch = environment.fetch

    def public_fetch(url, **kwargs):
        if url.endswith('/2.htm') and '/info/' in url:
            clock[0] = slot - timedelta(minutes=4)
            entered.set()
            assert release.wait(10)
        return fetch(url, **kwargs)

    monkeypatch.setattr(acquisition, 'fetch_public_resource', public_fetch)
    with background_collection(release, now=clock[0], mode='history') as (outcome, thread):
        assert entered.wait(10)
        assert thread.is_alive()
        with pytest.raises(RuntimeError, match='already running'):
            with daily._collection_lock():
                pytest.fail('history released the lock before its active raw outcome drained')
    assert 'error' not in outcome
    assert outcome['result']['status'] == 'partial'
    assert outcome['result']['sources'][0]['fetched'] == 1
    assert 'https://college.nju.edu.cn/info/1001/3.htm' not in environment.fetches
    assert len(environment.posts) == 1
    assert daily.list_items(mode='history', state='history_deferred')['total'] == 1
    assert daily.list_items(mode='history', state='pending')['total'] == 1


def test_legacy_reads_require_migration_without_writing_the_database(environment):
    initial = daily.run_once(now=NOW, mode='new')
    stamp = NOW.isoformat()
    with closing(runtime.connect_db()) as db, db:
        db.execute('ALTER TABLE daily_runs DROP COLUMN mode')
        db.execute('ALTER TABLE daily_links DROP COLUMN processed_at')
        db.execute('ALTER TABLE daily_source_state DROP COLUMN history_success_at')
        db.execute('''INSERT INTO daily_links(source_id,url,state,eligible,first_seen,last_seen,checked_at,message)
            VALUES ('test-college',?,'pending',0,?,?,?,'已有读取失败')''',
            ('https://college.nju.edu.cn/info/1001/2.htm', stamp, stamp, stamp))
        db.execute('DROP TABLE nannanews_schema')
        schema = db.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall()
    mode_before = runtime.settings.database_path.stat().st_mode
    for read in (lambda: daily.get_status(mode='new'),
                 lambda: daily.list_items(mode='history', state='fetch_failed', per_page=1)):
        with pytest.raises(runtime.ConfigurationError, match='migrate'):
            read()
    assert runtime.settings.database_path.stat().st_mode == mode_before
    with closing(sqlite3.connect(runtime.settings.database_path)) as db:
        assert db.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall() == schema
        assert db.execute("SELECT state FROM daily_links WHERE eligible=0").fetchone()[0] == 'pending'
    migrations.migrate()
    migrations.migrate()
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT mode FROM daily_runs').fetchone()[0] == 'legacy'
        assert db.execute('SELECT state,processed_at FROM daily_links WHERE eligible=0').fetchone() == ('fetch_failed', stamp)
    assert daily.get_status(mode='new')['last_run'] is None
    assert daily.get_status(mode='new')['last_runs']['legacy']['id'] == initial['id']
    failed = daily.list_items(mode='history', state='fetch_failed', per_page=1)
    assert failed['total'] == 1 and failed['items'][0]['processed_at'] == stamp


@pytest.fixture
def legacy_raw_link(environment):
    daily.initialize_sources()
    url = 'https://college.nju.edu.cn/info/1001/2.htm'
    document = acquisition.fetch_document(url)
    document['fetched_at'] = (NOW - timedelta(days=1)).isoformat()
    saved = information.save_document(document, source_id='test-college', actor='system:daily')
    checked = (NOW + timedelta(days=1)).isoformat()
    marker = '启动前历史原文已保存；不自动付费回填。'
    with closing(runtime.connect_db()) as db, db:
        key = json.loads(db.execute('SELECT definition_json FROM daily_source_state').fetchone()[0])['definition_key']
        db.execute('''INSERT INTO daily_runs(id,actor,started_at,finished_at,status,mode)
            VALUES (5,'system:daily',?,?,'partial','legacy')''', (checked, checked))
        db.execute('''INSERT INTO daily_links(id,source_id,url,state,eligible,first_seen,last_seen,
            checked_at,attempt_run,document_id,message,definition_key)
            VALUES (869,'test-college',?,'pending',0,?,?,?,5,?,?,?)''',
            (url, checked, checked, checked, saved['id'], marker, key))
        evidence = {table: db.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
                    for table in ('source_documents', 'information_records', 'entries')}
    environment.fetches.clear()
    return SimpleNamespace(url=url, document_id=saved['id'], checked=checked,
                           marker=marker, evidence=evidence)


@pytest.mark.parametrize('initial_state,old_schema,existing_processed', [
    ('pending', True, None),
    ('ready', False, None),
    ('fetch_failed', False, None),
    ('fetch_failed', False, '2026-10-15T21:17:00+08:00'),
])
def test_legacy_raw_success_protocol_repairs_before_errors_and_preserves_terminal_time(
        environment, legacy_raw_link, initial_state, old_schema, existing_processed):
    legacy = legacy_raw_link
    with closing(runtime.connect_db()) as db, db:
        db.execute('UPDATE daily_links SET state=?,processed_at=?', (initial_state, existing_processed))
        if old_schema:
            db.execute('ALTER TABLE daily_runs DROP COLUMN mode')
            db.execute('ALTER TABLE daily_links DROP COLUMN processed_at')
        db.execute('DROP TABLE nannanews_schema')
    migrations.migrate()
    with closing(runtime.connect_db()) as db:
        assert db.execute('''SELECT id,state,eligible,checked_at,processed_at,attempt_run,document_id,message
            FROM daily_links''').fetchone() == (
                869, 'history_deferred', 0, legacy.checked, existing_processed or legacy.checked,
                5, legacy.document_id, legacy.marker)
        first = db.execute('SELECT * FROM daily_links').fetchall()
        runs = db.execute('SELECT * FROM daily_runs').fetchall()
    migrations.migrate()
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT * FROM daily_links').fetchall() == first
        assert db.execute('SELECT * FROM daily_runs').fetchall() == runs
        for table, rows in legacy.evidence.items():
            assert db.execute(f'SELECT * FROM {table} ORDER BY id').fetchall() == rows
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0
    item = daily.list_items(mode='history')['items'][0]
    assert item['id'] == 869 and item['document_id'] == legacy.document_id
    assert item['processing_label'] == '原文留存成功、未提取'
    assert not environment.fetches and not environment.posts


def test_legacy_image_only_raw_success_retains_its_actual_poster(environment, legacy_raw_link):
    legacy = legacy_raw_link
    image = {'data': 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
             'url': 'https://college.nju.edu.cn/poster.png'}
    saved = information.save_document({'canonical_url': legacy.url, 'text': '', 'media': [image]},
                                      source_id='test-college', actor='system:daily')
    with closing(runtime.connect_db()) as db, db:
        db.execute("UPDATE daily_links SET state='fetch_failed',document_id=?", (saved['id'],))
        db.execute('DROP TABLE nannanews_schema')
    migrations.migrate()
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT state,document_id,processed_at FROM daily_links').fetchone() == (
            'history_deferred', saved['id'], legacy.checked)
    retained = information.get_document(saved['id'])
    assert retained['text'] == '' and retained['media'][0]['data'] == image['data']
    assert not environment.fetches and not environment.posts


@pytest.mark.parametrize('run_mode', ['legacy', 'history'])
def test_capture_failure_retaining_old_raw_is_never_relabelled_success(
        environment, legacy_raw_link, monkeypatch, run_mode):
    legacy = legacy_raw_link
    failure_now = NOW + timedelta(days=2)
    clock = daily._now
    monkeypatch.setattr(daily, '_now', lambda value=None: clock(value if value is not None else failure_now))
    # Even a failure mentioning the old protocol must not be treated as its marker.
    message = legacy.marker + ' 但本次公开原文读取失败。'
    environment.resources[legacy.url] = acquisition.AcquisitionError(message, 'source_unavailable')
    with closing(runtime.connect_db()) as db, db:
        db.execute('''INSERT INTO daily_runs(id,actor,started_at,status,mode)
            VALUES (6,'system:daily',?,'running',?)''', (failure_now.isoformat(), run_mode))
        db.row_factory = sqlite3.Row
        link = db.execute('SELECT * FROM daily_links').fetchone()
    result = {'failed': 0}
    daily._capture(link, {'kind': 'html'}, 'system:daily', failure_now.isoformat(), 6, result)
    assert result['failed'] == 1
    with closing(runtime.connect_db()) as db:
        assert db.execute('''SELECT state,document_id,checked_at,processed_at,message FROM daily_links''').fetchone() == (
            'fetch_failed', legacy.document_id, failure_now.isoformat(), failure_now.isoformat(), message)
        failed = db.execute('SELECT * FROM daily_links').fetchall()
    migrations.migrate()
    migrations.migrate()
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT * FROM daily_links').fetchall() == failed
        for table, rows in legacy.evidence.items():
            assert db.execute(f'SELECT * FROM {table} ORDER BY id').fetchall() == rows
    assert environment.fetches == [legacy.url] and not environment.posts


@pytest.mark.parametrize('initial_state,message_kind,model_status,attempt_table,attempt_status,expected_state', [
    ('pending', 'marker', 'not_extracted', 'daily_model_attempts', 'attempted', 'fetch_failed'),
    ('fetch_failed', 'marker', 'not_extracted', 'daily_model_attempts', 'uncertain', 'fetch_failed'),
    ('pending', 'marker', 'not_extracted', 'source_document_model_attempts', 'extracting', 'fetch_failed'),
    ('fetch_failed', 'marker', 'not_extracted', 'source_document_model_attempts', 'failed', 'fetch_failed'),
    ('fetch_failed', 'marker', 'extracting', None, None, 'fetch_failed'),
    ('fetch_failed', 'marker', 'uncertain', None, None, 'fetch_failed'),
    ('fetch_failed', 'marker', 'failed', None, None, 'fetch_failed'),
    ('fetch_failed', 'marker', 'completed', None, None, 'fetch_failed'),
    ('pending', 'empty', 'not_extracted', 'daily_model_attempts', 'attempted', 'model_uncertain'),
    ('ready', 'empty', 'not_extracted', 'source_document_model_attempts', 'extracting', 'model_uncertain'),
    ('pending', 'empty', 'not_extracted', 'source_document_model_attempts', 'failed', 'model_uncertain'),
    ('ready', 'empty', 'not_extracted', 'source_document_model_attempts', 'completed', 'model_uncertain'),
    ('pending', 'empty', 'extracting', None, None, 'model_uncertain'),
    ('pending', 'empty', 'uncertain', None, None, 'model_uncertain'),
    ('ready', 'empty', 'failed', None, None, 'model_failed'),
    ('pending', 'empty', 'completed', None, None, 'done'),
])
def test_legacy_raw_migration_respects_document_state_and_both_paid_attempt_ledgers(
        environment, legacy_raw_link, initial_state, message_kind, model_status,
        attempt_table, attempt_status, expected_state):
    legacy = legacy_raw_link
    terminal = '2026-10-15T21:17:00+08:00'
    with closing(runtime.connect_db()) as db, db:
        db.execute('UPDATE daily_links SET state=?,message=?,processed_at=?',
                   (initial_state, legacy.marker if message_kind == 'marker' else '', terminal))
        db.execute('UPDATE source_documents SET model_status=? WHERE id=?', (model_status, legacy.document_id))
        if attempt_table == 'daily_model_attempts':
            db.execute('INSERT INTO daily_model_attempts(document_id,run_id,status) VALUES (?,5,?)',
                       (legacy.document_id, attempt_status))
        elif attempt_table == 'source_document_model_attempts':
            db.execute('''INSERT INTO source_document_model_attempts(document_id,actor,started_at,model_status)
                VALUES (?,'system:daily',?,?)''', (legacy.document_id, legacy.checked, attempt_status))
        evidence = {table: db.execute(f'SELECT * FROM {table}').fetchall()
                    for table in ('source_documents', 'daily_model_attempts', 'source_document_model_attempts')}
        db.execute('DROP TABLE nannanews_schema')
    migrations.migrate()
    migrations.migrate()
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT state,checked_at,processed_at,document_id,attempt_run FROM daily_links').fetchone() == (
            expected_state, legacy.checked, terminal, legacy.document_id, 5)
        for table, rows in evidence.items():
            assert db.execute(f'SELECT * FROM {table}').fetchall() == rows
    assert not environment.fetches and not environment.posts


@pytest.mark.parametrize('missing_evidence', [
    'legacy_run', 'attempt_run', 'checked_at', 'document', 'retained_text', 'matching_url', 'historical_scope',
])
def test_legacy_success_marker_requires_matching_retained_raw_and_legacy_provenance(
        environment, legacy_raw_link, missing_evidence):
    legacy = legacy_raw_link
    with closing(runtime.connect_db()) as db, db:
        db.execute("UPDATE daily_links SET state='fetch_failed',processed_at=?", (legacy.checked,))
        if missing_evidence == 'legacy_run':
            db.execute("UPDATE daily_runs SET mode='history'")
        elif missing_evidence == 'attempt_run':
            db.execute('UPDATE daily_links SET attempt_run=NULL')
        elif missing_evidence == 'checked_at':
            db.execute('UPDATE daily_links SET checked_at=NULL')
        elif missing_evidence == 'document':
            db.execute('UPDATE daily_links SET document_id=NULL')
        elif missing_evidence == 'retained_text':
            db.execute("UPDATE source_documents SET content_text='   '")
        elif missing_evidence == 'matching_url':
            db.execute("UPDATE daily_links SET url='https://college.nju.edu.cn/info/1001/3.htm'")
        elif missing_evidence == 'historical_scope':
            db.execute('UPDATE daily_links SET eligible=1')
        before = db.execute('SELECT * FROM daily_links').fetchall()
        db.execute('DROP TABLE nannanews_schema')
    migrations.migrate()
    migrations.migrate()
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT * FROM daily_links').fetchall() == before
    assert not environment.fetches and not environment.posts


@pytest.mark.parametrize('mode', ['', 'legacy', 'mixed', None])
def test_invalid_collection_modes_fail_before_network_effects(environment, mode):
    for call in (daily.run_once, daily.get_status, daily.list_items):
        with pytest.raises(ValueError, match='mode'):
            call(mode=mode)
    assert not environment.fetches and not environment.posts




@pytest.mark.parametrize('failure,state,label', [
    (None, 'done', '提取成功'),
    (acquisition.AcquisitionError('模型请求已发送但失败。', 'model_http_error'), 'model_failed', '提取失败'),
    (KeyboardInterrupt(), 'model_uncertain', '结果未确认'),
])
def test_model_terminal_labels_and_actual_outcome_time_survive_unchanged_refetch(environment, monkeypatch, failure, state, label):
    environment.model_failure = failure
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            daily.run_once(now=NOW, mode='new')
    else:
        daily.run_once(now=NOW, mode='new')
    first = daily.list_items(mode='new')['items'][0]
    assert first['state'] == state and first['processing_label'] == label
    assert first['processed_at'] and first['document_id'] and first['record_id']
    linked = information.get_record(first['record_id'], scope='private')
    assert linked is not None and linked['document_id'] == first['document_id']
    assert (linked['entry_id'] is not None) == (state == 'done')
    clock = daily._now
    monkeypatch.setattr(daily, '_now', lambda value=None: clock(value if value is not None else NOW + timedelta(days=1)))
    environment.model_failure = None
    daily.run_once(now=NOW + timedelta(days=1), mode='new')
    second = daily.list_items(mode='new')['items'][0]
    assert second['state'] == state and second['processing_label'] == label
    assert second['processed_at'] == first['processed_at']
    assert len(environment.posts) == 1


def test_daily_wire_audit_precedes_both_atomic_paid_claims_and_no_transaction_spans_send(environment, monkeypatch):
    from nannanews import model_audit
    events = []
    initialize = model_audit.ModelAudit.__init__

    def prepare(self, *args, **kwargs):
        with closing(runtime.connect_db()) as db:
            assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0
            assert db.execute('SELECT model_status FROM source_documents').fetchone()[0] == 'not_extracted'
        initialize(self, *args, **kwargs)
        with closing(runtime.connect_db()) as db:
            assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0
        events.append('durable_audit')

    def paid(payload, *, audit=None):
        assert audit is not None
        request = (audit.directory / 'request.json').read_bytes()
        assert hashlib.sha256(request).hexdigest() == audit.metadata['request']['sha256']
        assert json.loads(request) == payload
        assert (audit.directory / 'response.bin').stat().st_size == acquisition.MAX_MODEL_BODY + 1
        with closing(runtime.connect_db()) as db:
            db.execute('BEGIN IMMEDIATE')
            assert db.execute('SELECT status FROM daily_model_attempts').fetchall() == [('attempted',)]
            assert db.execute('SELECT model_status FROM source_document_model_attempts').fetchall() == [('extracting',)]
            assert db.execute('SELECT model_status FROM source_documents').fetchall() == [('extracting',)]
            db.rollback()
        events.append('paid_boundary')
        return environment.model(payload)

    monkeypatch.setattr(model_audit.ModelAudit, '__init__', prepare)
    monkeypatch.setattr(acquisition, '_model_post', paid)
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert events == ['durable_audit', 'paid_boundary']
    assert result['extracted'] == 1 and len(environment.posts) == 1
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT status FROM daily_model_attempts').fetchall() == [('done',)]
        assert db.execute('SELECT model_status FROM source_document_model_attempts').fetchall() == [('completed',)]


@pytest.mark.parametrize('failure', ['audit_disk', 'source_claim_rollback'])
def test_daily_unpaid_preparation_and_atomic_claim_failure_leave_no_paid_records(environment, monkeypatch, failure):
    from nannanews import model_audit
    if failure == 'audit_disk':
        def refuse_disk(*args):
            raise OSError('private audit disk full')
        monkeypatch.setattr(model_audit.os, 'posix_fallocate', refuse_disk)
    else:
        with closing(runtime.connect_db()) as db, db:
            db.execute('''CREATE TRIGGER reject_new_source_attempt BEFORE INSERT ON source_document_model_attempts
                BEGIN SELECT RAISE(ABORT,'test admission rollback'); END''')
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert result['failed'] >= 1
    assert environment.posts == []
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0
        assert db.execute('SELECT model_status FROM source_documents').fetchall() == [('not_extracted',)]
        assert db.execute('SELECT state FROM daily_links').fetchall() == [('fetch_failed',)]
    captured = information.get_document(daily.list_items(mode='new')['items'][0]['document_id'])
    assert '原始正文' in captured['text']


def test_concurrent_failed_manual_extraction_cannot_become_automatic_paid_retry(environment, monkeypatch):
    enrich = information.enrich_document
    protected = {}

    def interleave(document_id, actor, **kwargs):
        assert not protected
        # Daily has passed its preliminary check; a separate explicit caller now pays.
        environment.model_failure = acquisition.AcquisitionError('Explicit manual request failed.', 'model_http_error')
        try:
            with pytest.raises(acquisition.AcquisitionError, match='Explicit manual request failed'):
                enrich(document_id, 'manual-regression')
        finally:
            environment.model_failure = None
        with closing(runtime.connect_db()) as db:
            assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0
            assert db.execute('SELECT model_status FROM source_documents WHERE id=?', (document_id,)).fetchone() == ('failed',)
            protected['document'] = db.execute('SELECT * FROM source_documents WHERE id=?', (document_id,)).fetchall()
            protected['attempts'] = db.execute('SELECT * FROM source_document_model_attempts WHERE document_id=?',
                                              (document_id,)).fetchall()
            assert len(protected['attempts']) == 1
        protected['document_id'] = document_id
        return enrich(document_id, actor, **kwargs)

    monkeypatch.setattr(information, 'enrich_document', interleave)
    daily.run_once(now=NOW, mode='new')
    assert len(environment.posts) == 1
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0
        assert db.execute('SELECT * FROM source_documents WHERE id=?', (protected['document_id'],)).fetchall() == protected['document']
        assert db.execute('SELECT * FROM source_document_model_attempts WHERE document_id=?',
                          (protected['document_id'],)).fetchall() == protected['attempts']
        assert db.execute('SELECT state FROM daily_links').fetchall() == [('model_failed',)]


@pytest.mark.parametrize('attempt_status', ['failed', 'extracting', 'completed'])
def test_source_attempt_history_is_rechecked_atomically_even_when_document_state_looks_unpaid(environment, monkeypatch, attempt_status):
    enrich = information.enrich_document
    protected = {}

    def interleave(document_id, actor, **kwargs):
        assert not protected
        with closing(runtime.connect_db()) as db, db:
            assert db.execute('SELECT model_status FROM source_documents WHERE id=?', (document_id,)).fetchone() == ('not_extracted',)
            db.execute('''INSERT INTO source_document_model_attempts(document_id,actor,started_at,model_status)
                VALUES (?,'retained-explicit-attempt',?,?)''', (document_id, NOW.isoformat(), attempt_status))
            protected['document'] = db.execute('SELECT * FROM source_documents WHERE id=?', (document_id,)).fetchall()
            protected['attempts'] = db.execute('SELECT * FROM source_document_model_attempts WHERE document_id=?',
                                              (document_id,)).fetchall()
        protected['document_id'] = document_id
        return enrich(document_id, actor, **kwargs)

    monkeypatch.setattr(information, 'enrich_document', interleave)
    daily.run_once(now=NOW, mode='new')
    assert environment.posts == []
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0
        assert db.execute('SELECT * FROM source_documents WHERE id=?', (protected['document_id'],)).fetchall() == protected['document']
        assert db.execute('SELECT * FROM source_document_model_attempts WHERE document_id=?',
                          (protected['document_id'],)).fetchall() == protected['attempts']
        assert db.execute('SELECT state FROM daily_links').fetchall() == [('model_uncertain',)]


MATH_ROOT = 'https://math.nju.edu.cn/njdx/front/sxxx/reports.do?channelId=16410&pageSize=10&currentPage=1'


@pytest.fixture
def math_environment(environment):
    daily.SEED_PATH.write_text(json.dumps([{
        'id': 'math-reports', 'name': '数学学院近期报告', 'url': MATH_ROOT,
        'identity_url': 'https://math.nju.edu.cn/', 'expected_identity': '南京大学数学学院',
        'publisher_college': '数学学院', 'kind': 'nju_math_reports', 'enabled': True,
        'article_pattern': r'^/xsbg/jrbg/[0-9]{8}/i[0-9]+\.html$'
    }]), encoding='utf-8')
    environment.put('https://math.nju.edu.cn/', '<html><title>南京大学数学学院</title></html>')
    return environment


def put_math_pages(boundary, pages):
    for number, identities in enumerate(pages, start=1):
        records = []
        for identity in identities:
            path = f'/xsbg/jrbg/20261001/i{identity}.html'
            records.append({'iid': identity, 'title': f'公开数学报告{identity}',
                            'url': 'http://math.nju.edu.cn' + path,
                            'releasetime': '2026-10-01 11:06:39',
                            'field_8925': '2026-10-16 14:30:00'})
            boundary.put('https://math.nju.edu.cn' + path, article(identity, published='2026-10-01'))
        boundary.put(MATH_ROOT.replace('currentPage=1', f'currentPage={number}'),
                     json.dumps({'reports': records, 'pages': len(pages), 'pageSize': 10,
                                 'currentPage': number, 'channelId': 16410}),
                     'text/html; charset=utf-8')


def test_math_event_sorted_pages_discover_new_reports_beyond_known_root(math_environment):
    put_math_pages(math_environment, [[423001], [423002]])
    first = daily.run_once(now=NOW, mode='new')['sources'][0]
    assert first['extracted'] == 2
    put_math_pages(math_environment, [[423001], [423003, 423002]])
    second = daily.run_once(now=NOW + timedelta(days=1), mode='new')['sources'][0]
    assert second['discovered'] == second['extracted'] == 1
    assert len(math_environment.posts) == 3
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT source_published_date FROM source_documents ORDER BY id').fetchall() == [
            ('2026-10-01',), ('2026-10-01',), ('2026-10-01',)]
        assert db.execute('SELECT priority_kind,priority_date,eligible,state FROM daily_links ORDER BY url').fetchall() == [
            ('event', '2026-10-16T14:30:00+08:00', 1, 'done'),
            ('event', '2026-10-16T14:30:00+08:00', 1, 'done'),
            ('event', '2026-10-16T14:30:00+08:00', 1, 'done')]
    records = information.query_records({})['records']
    assert {record['source_url'] for record in records} == {
        f'https://math.nju.edu.cn/xsbg/jrbg/20261001/i{identity}.html'
        for identity in (423001, 423002, 423003)}


def test_math_listing_budget_resumes_remaining_pages_without_publication_cutoff(math_environment):
    put_math_pages(math_environment, [[], [], [423009]])
    first = daily.run_once(now=NOW, mode='new')['sources'][0]
    assert first['discovered'] == 0 and first['pending_pages'] == 1
    assert MATH_ROOT.replace('currentPage=1', 'currentPage=3') not in math_environment.fetches
    second = daily.run_once(now=NOW + timedelta(minutes=1), mode='new')['sources'][0]
    assert second['discovered'] == second['extracted'] == 1
    assert information.query_records({})['records'][0]['source_published_date'] == '2026-10-01'


@pytest.mark.parametrize('change', [
    {'channelId': 1}, {'currentPage': 2}, {'pages': -1},
    {'reports': [{'url': 'https://foreign.example/xsbg/jrbg/20261001/i423009.html'}]},
])
def test_math_listing_identity_and_page_failures_never_fetch_articles_or_bill(math_environment, change):
    data = {'reports': [], 'pages': 1, 'pageSize': 10, 'currentPage': 1, 'channelId': 16410}
    math_environment.put(MATH_ROOT, json.dumps(data | change), 'text/html')
    result = daily.run_once(now=NOW, mode='new')['sources'][0]
    assert result['status'] == 'failed' and result['discovered'] == 0
    assert result['last_success_at'] is None
    assert math_environment.posts == []
    assert not any('/xsbg/' in url for url in math_environment.fetches)
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0


WECHAT_REFERENCE = 'https://mp.weixin.qq.com/s/publisher-reference'
WECHAT_ARTICLES = [f'https://mp.weixin.qq.com/s/public-article-{index}' for index in range(1, 4)]
WECHAT_BIZ = 'MzAxODAzMjQ1NQ=='


def wechat_article(version='原始公开正文', published='2026-10-14', biz=WECHAT_BIZ):
    metadata = f'<em id="publish_time">{published}</em>' if published else ''
    return ('<html><head><title>南京大学公开通知</title></head><body>'
            '<h1 id="activity-name">南京大学公开通知</h1>' + metadata +
            '<div id="js_content"><p>' + version + '。公开校园活动的完整正文，'
            '只保留原文已明确的证据，不推测活动日期或发布者。</p></div>'
            '<script>var biz = "' + biz + '";</script></body></html>')


@pytest.fixture
def wechat_environment(environment, monkeypatch):
    from nannanews import wechat_desktop

    daily.SEED_PATH.write_text(json.dumps([{
        'id': 'test-college', 'name': '南京大学公众号', 'url': WECHAT_REFERENCE,
        'identity_url': WECHAT_REFERENCE, 'expected_identity': '南京大学',
        'wechat_biz': WECHAT_BIZ, 'kind': 'wechat_gui', 'enabled': True
    }]), encoding='utf-8')
    environment.put(WECHAT_REFERENCE, wechat_article('发布者身份参考文章', '2025-01-01'))
    for url in WECHAT_ARTICLES:
        environment.put(url, wechat_article())
    snapshot = {'links': [{'url': WECHAT_ARTICLES[0], 'published_date': NOW.date().isoformat(),
                           'disallowed': False}], 'complete': True, 'message': ''}
    calls = []

    def discover(definition, *, page_limit, deadline=None):
        calls.append((dict(definition), page_limit, deadline))
        return snapshot

    monkeypatch.setattr(wechat_desktop, 'discover_articles', discover)
    return SimpleNamespace(boundary=environment, snapshot=snapshot, calls=calls)


@pytest.fixture
def parallel_wechat_environment(environment, monkeypatch):
    from nannanews import wechat_desktop

    configure_parallel_sources(environment, [('z-web', 'ordinary.nju.edu.cn')])
    definitions = json.loads(daily.SEED_PATH.read_text(encoding='utf-8'))
    gui_definitions, snapshots, calls = [], {}, []
    for index, source_id in enumerate(('a-gui', 'b-gui', 'c-gui')):
        reference = f'https://mp.weixin.qq.com/s/{source_id}-reference'
        url = f'https://mp.weixin.qq.com/s/{source_id}-article'
        biz = base64.b64encode(str(3000000000 + index).encode()).decode('ascii')
        definition = {'id': source_id, 'name': source_id, 'url': reference, 'kind': 'wechat_gui',
                      'identity_url': reference, 'expected_identity': '南京大学',
                      'wechat_biz': biz, 'enabled': True}
        gui_definitions.append(definition)
        environment.put(reference, wechat_article('公开发布者参考文章', '2025-01-01', biz))
        environment.put(url, wechat_article(source_id + '的公开通知', biz=biz))
        snapshots[source_id] = {'links': [{'url': url, 'published_date': NOW.date().isoformat(),
                                         'disallowed': False}], 'complete': True, 'message': ''}
    daily.SEED_PATH.write_text(json.dumps(gui_definitions + definitions), encoding='utf-8')

    def discover(definition, *, page_limit, deadline=None):
        calls.append(definition['id'])
        return snapshots[definition['id']]

    monkeypatch.setattr(wechat_desktop, 'discover_articles', discover)
    return SimpleNamespace(boundary=environment, definitions=gui_definitions, snapshots=snapshots, calls=calls)


@pytest.mark.parametrize('fetch_workers', [1, 6])
def test_gui_accounts_serialize_without_blocking_http_or_ordinary_publishers(
        parallel_wechat_environment, monkeypatch, fetch_workers):
    from nannanews import wechat_desktop

    gui = parallel_wechat_environment
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, collection_fetch_workers=fetch_workers))
    first_entered, second_entered = Event(), Event()
    release_first, release_second = Event(), Event()
    web_committed, first_committed = Event(), Event()
    lock = Lock()
    actions = {'active': 0, 'peak': 0}
    submissions = {'peak': 0, 'total': 0}
    gui_futures = []
    enrich = information.enrich_document
    executor_submit = daily.ThreadPoolExecutor.submit

    def observe_submit(executor, function, *args, **kwargs):
        is_gui = (len(args) >= 2 and args[0] == 'discover'
                  and args[1]['definition']['kind'] == 'wechat_gui')
        if is_gui:
            with lock:
                submissions['total'] += 1
                outstanding = sum(not future.done() for future in gui_futures)
                submissions['peak'] = max(submissions['peak'], outstanding + 1)
        future = executor_submit(executor, function, *args, **kwargs)
        if is_gui:
            with lock:
                gui_futures.append(future)
        return future

    def discover(definition, *, page_limit, deadline=None):
        with lock:
            gui.calls.append(definition['id'])
            ordinal = len(gui.calls)
            actions['active'] += 1
            actions['peak'] = max(actions['peak'], actions['active'])
        try:
            if ordinal == 1:
                first_entered.set()
                assert release_first.wait(10)
            elif ordinal == 2:
                second_entered.set()
                assert release_second.wait(10)
            return gui.snapshots[definition['id']]
        finally:
            with lock:
                actions['active'] -= 1

    def observe_commit(document_id, actor, **kwargs):
        result = enrich(document_id, actor, **kwargs)
        source_id = information.get_document(document_id)['source_id']
        if source_id == 'z-web':
            web_committed.set()
        elif source_id == 'a-gui':
            first_committed.set()
        return result

    monkeypatch.setattr(daily.ThreadPoolExecutor, 'submit', observe_submit)
    monkeypatch.setattr(wechat_desktop, 'discover_articles', discover)
    monkeypatch.setattr(information, 'enrich_document', observe_commit)
    with background_collection(release_first, release_second) as (outcome, thread):
        assert first_entered.wait(10)
        assert web_committed.wait(10), 'ordinary publisher waited for native GUI discovery'
        release_first.set()
        assert second_entered.wait(10)
        assert first_committed.wait(10), 'ready WeChat HTTP/model work waited for another account GUI'
        with lock:
            assert actions == {'active': 1, 'peak': 1}
            assert submissions == {'peak': 1, 'total': 2}
            assert sum(not future.done() for future in gui_futures) == 1
            assert gui.calls == ['a-gui', 'b-gui']
        assert gui.definitions[2]['url'] not in gui.boundary.fetches
        assert thread.is_alive()
    assert 'error' not in outcome
    assert actions == {'active': 0, 'peak': 1}
    assert submissions == {'peak': 1, 'total': 3}
    assert all(future.done() for future in gui_futures)
    assert gui.calls == ['a-gui', 'b-gui', 'c-gui']
    assert len(gui.boundary.posts) == 4
    for source in information.list_sources():
        definition = daily._definition(source)
        with closing(runtime.connect_db()) as db:
            rows = db.execute('''SELECT l.definition_key,l.state,d.source_id FROM daily_links l
                JOIN source_documents d ON d.id=l.document_id WHERE l.source_id=?''', (source['id'],)).fetchall()
        assert rows == [(definition['definition_key'], 'done', source['id'])]


@pytest.mark.parametrize('mismatch', ['reference', 'capture'])
def test_gui_lane_publisher_failure_does_not_cross_account_boundaries(
        parallel_wechat_environment, mismatch):
    gui = parallel_wechat_environment
    failed = gui.definitions[1]
    url = failed['url'] if mismatch == 'reference' else gui.snapshots[failed['id']]['links'][0]['url']
    gui.boundary.put(url, wechat_article(biz=gui.definitions[0]['wechat_biz']))
    result = daily.run_once(now=NOW, mode='new')
    sources = {source['source_id']: source for source in result['sources']}
    assert sources[failed['id']]['status'] == ('failed' if mismatch == 'reference' else 'partial')
    assert sources[failed['id']]['extracted'] == 0
    assert sources[failed['id']]['last_success_at'] is None
    assert all(sources[source_id]['extracted'] == 1 for source_id in ('a-gui', 'c-gui', 'z-web'))
    assert (failed['id'] in gui.calls) == (mismatch == 'capture')
    assert len(gui.boundary.posts) == 3
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_documents WHERE source_id=?', (failed['id'],)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 3


@pytest.mark.parametrize('halt_kind', ['partial_snapshot', 'typed_error', 'flagged_error'])
def test_gui_manual_challenge_retains_urls_but_blocks_its_queue_and_later_accounts(
        parallel_wechat_environment, monkeypatch, halt_kind):
    from nannanews import wechat_desktop

    gui = parallel_wechat_environment
    daily.initialize_sources()
    for source in information.list_sources():
        definition = daily._definition(source)
        if definition['kind'] != 'wechat_gui':
            continue
        url = f"https://mp.weixin.qq.com/s/{source['id']}-queued"
        gui.boundary.put(url, wechat_article('之前已经发现的公开原文', biz=definition['wechat_biz']))
        with closing(runtime.connect_db()) as db, db:
            db.execute('''INSERT INTO daily_links(source_id,url,state,eligible,first_seen,last_seen,definition_key)
                VALUES (?,?,'pending',1,?,?,?)''',
                (source['id'], url, NOW.isoformat(), NOW.isoformat(), definition['definition_key']))

    def discover(definition, *, page_limit, deadline=None):
        gui.calls.append(definition['id'])
        if halt_kind == 'partial_snapshot':
            return dict(gui.snapshots[definition['id']], complete=False,
                        verification_required=True, message='公开桌面需要人工验证。')
        error = acquisition.AcquisitionError('公开桌面需要人工验证。',
            'source_verification_required' if halt_kind == 'typed_error' else 'source_blocked')
        if halt_kind == 'flagged_error':
            error.verification_required = True
        raise error

    monkeypatch.setattr(wechat_desktop, 'discover_articles', discover)
    run = daily.run_once(now=NOW, mode='new')
    assert gui.calls == ['a-gui']
    assert all(definition['url'] not in gui.boundary.fetches for definition in gui.definitions[1:])
    sources = {source['source_id']: source for source in run['sources']}
    assert sources['z-web']['extracted'] == 1
    for definition in gui.definitions:
        source = sources[definition['id']]
        assert source['status'] == 'failed' and source['last_success_at'] is None
        assert source['fetched'] == source['extracted'] == 0
    assert sources['a-gui']['discovered'] == int(halt_kind == 'partial_snapshot')
    with closing(runtime.connect_db()) as db:
        rows = db.execute("SELECT state,attempt_run,document_id FROM daily_links WHERE source_id!='z-web'").fetchall()
        assert rows == [('pending', None, None)] * (3 + int(halt_kind == 'partial_snapshot'))
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM source_documents WHERE source_id!='z-web'").fetchone()[0] == 0
        assert db.execute("SELECT state FROM daily_pages WHERE source_id='a-gui'").fetchone()[0] == 'pending'
    assert len(gui.boundary.posts) == 1


@pytest.mark.parametrize('interrupt_at', ['gui', 'web'])
def test_gui_lane_interrupt_drains_native_and_paid_work_before_unlock(
        parallel_wechat_environment, monkeypatch, interrupt_at):
    from nannanews import wechat_desktop

    gui = parallel_wechat_environment
    gui_entered, paid_entered, interrupted = Event(), Event(), Event()
    release_gui, release_paid = Event(), Event()
    fetch = gui.boundary.fetch
    first_url = gui.snapshots['a-gui']['links'][0]['url']

    def discover(definition, *, page_limit, deadline=None):
        gui.calls.append(definition['id'])
        if definition['id'] == 'b-gui':
            gui_entered.set()
            assert paid_entered.wait(10)
            if interrupt_at == 'gui':
                interrupted.set()
                raise KeyboardInterrupt()
            assert release_gui.wait(10)
        return gui.snapshots[definition['id']]

    def public_fetch(url, **kwargs):
        if url == 'https://ordinary.nju.edu.cn/info/1001/1.htm':
            assert gui_entered.wait(10)
            assert paid_entered.wait(10)
            if interrupt_at == 'web':
                interrupted.set()
                raise KeyboardInterrupt()
        return fetch(url, **kwargs)

    def paid_post(payload, *, audit=None):
        source = json.loads(payload['input'][0]['content'][0]['text'].split('\n', 1)[1])
        if source['canonical_url'] == first_url:
            paid_entered.set()
            assert release_paid.wait(10)
        return gui.boundary.model(payload)

    monkeypatch.setattr(wechat_desktop, 'discover_articles', discover)
    monkeypatch.setattr(acquisition, 'fetch_public_resource', public_fetch)
    monkeypatch.setattr(acquisition, '_model_post', paid_post)
    with background_collection(release_gui, release_paid) as (outcome, thread):
        assert interrupted.wait(10)
        assert thread.is_alive() and 'error' not in outcome
        with closing(runtime.connect_db()) as db:
            assert db.execute('SELECT status FROM daily_runs').fetchone()[0] == 'running'
            assert db.execute('''SELECT a.status FROM daily_model_attempts a
                JOIN source_documents d ON d.id=a.document_id WHERE d.source_url=?''', (first_url,)).fetchall() == [('attempted',)]
        with pytest.raises(RuntimeError, match='already running'):
            with daily._collection_lock():
                pytest.fail('collection unlocked while GUI or paid work was still active')
    assert isinstance(outcome.get('error'), KeyboardInterrupt)
    assert gui.calls == ['a-gui', 'b-gui']
    assert gui.definitions[2]['url'] not in gui.boundary.fetches
    assert not any(thread.name.startswith(('daily-web', 'daily-gui', 'daily-model')) for thread in running_threads())
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT status FROM daily_runs').fetchone()[0] == 'interrupted'
        assert db.execute("SELECT model_status FROM source_documents WHERE source_url=?", (first_url,)).fetchone()[0] == 'completed'
        assert db.execute('''SELECT a.status FROM daily_model_attempts a JOIN source_documents d ON d.id=a.document_id
            WHERE d.source_url=?''', (first_url,)).fetchall() == [('done',)]
        assert db.execute("SELECT COUNT(*) FROM daily_links WHERE state IN ('fetching','modelling')").fetchone()[0] == 0
    with daily._collection_lock():
        pass
    monkeypatch.setattr(wechat_desktop, 'discover_articles',
                        lambda definition, **kwargs: gui.snapshots[definition['id']])
    monkeypatch.setattr(acquisition, 'fetch_public_resource', gui.boundary.fetch)
    monkeypatch.setattr(acquisition, '_model_post', gui.boundary.model)
    daily.run_once(now=NOW + timedelta(days=1), mode='new')
    paid_urls = [json.loads(payload['input'][0]['content'][0]['text'].split('\n', 1)[1])['canonical_url']
                 for payload in gui.boundary.posts]
    assert paid_urls.count(first_url) == 1


def test_gui_history_deadline_drains_current_discovery_without_submitting_next_account(
        parallel_wechat_environment, monkeypatch):
    from nannanews import wechat_desktop

    gui = parallel_wechat_environment
    slot = NOW.replace(hour=4) + timedelta(days=1)
    clock = [slot - timedelta(minutes=6)]
    original_now = daily._now
    monkeypatch.setattr(daily, '_now', lambda value=None: original_now(value if value is not None else clock[0]))
    entered, release = Event(), Event()

    def discover(definition, *, page_limit, deadline=None):
        assert deadline == slot - timedelta(minutes=5)
        gui.calls.append(definition['id'])
        clock[0] = slot - timedelta(minutes=4)
        entered.set()
        assert release.wait(10)
        return gui.snapshots[definition['id']]

    monkeypatch.setattr(wechat_desktop, 'discover_articles', discover)
    with background_collection(release, now=clock[0], mode='history') as (outcome, thread):
        assert entered.wait(10)
        assert thread.is_alive()
        with pytest.raises(RuntimeError, match='already running'):
            with daily._collection_lock():
                pytest.fail('history unlocked before its GUI discovery drained')
    assert 'error' not in outcome
    assert outcome['result']['status'] == 'partial'
    assert gui.calls == ['a-gui']
    assert all(definition['url'] not in gui.boundary.fetches for definition in gui.definitions[1:])
    assert gui.boundary.posts == []
    with closing(runtime.connect_db()) as db:
        assert db.execute("SELECT COUNT(*) FROM daily_links WHERE source_id='a-gui' AND document_id IS NULL").fetchone()[0] == 1



@pytest.mark.parametrize('fetch_workers', [1, 6])
def test_gui_reference_and_web_requests_share_global_and_per_host_limits(
        parallel_wechat_environment, monkeypatch, fetch_workers):
    gui = parallel_wechat_environment
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, collection_fetch_workers=fetch_workers))
    configure_parallel_sources(gui.boundary, [(f'web{index}', f'web{index}.nju.edu.cn') for index in range(6)])
    web_definitions = json.loads(daily.SEED_PATH.read_text(encoding='utf-8'))
    daily.SEED_PATH.write_text(json.dumps(gui.definitions + web_definitions), encoding='utf-8')
    release, contended, full = Event(), Event(), Event()
    lock = Lock()
    requests = {'active': 0, 'peak': 0, 'attempted': 0}
    active_hosts, peak_hosts, limiters = {}, {}, set()
    slot = acquisition.PublicFetchLimiter._slot

    @contextmanager
    def observe_slot(limiter, hostname, deadline):
        with lock:
            requests['attempted'] += 1
            limiters.add(limiter)
            if requests['attempted'] >= fetch_workers + 1:
                contended.set()
        with slot(limiter, hostname, deadline):
            yield

    class PublicResponse:
        status = 200

        def __init__(self, url):
            self.body, self.mime, _ = gui.boundary.fetch(url)
            self.offset = 0

        def getheader(self, name, default=None):
            return {'Content-Type': self.mime, 'Content-Length': str(len(self.body))}.get(name, default)

        def read1(self, size):
            chunk = self.body[self.offset:self.offset + size]
            self.offset += len(chunk)
            return chunk

        def close(self):
            pass

    class PublicConnection:
        sock = None

        def __init__(self, host, port, address, timeout):
            self.host, self.closed = host, False
            with lock:
                requests['active'] += 1
                requests['peak'] = max(requests['peak'], requests['active'])
                active_hosts[host] = active_hosts.get(host, 0) + 1
                peak_hosts[host] = max(peak_hosts.get(host, 0), active_hosts[host])
                if requests['active'] >= fetch_workers:
                    full.set()

        def request(self, method, path, headers=None):
            self.url = f'https://{self.host}{path}'
            assert release.wait(10)

        def getresponse(self):
            return PublicResponse(self.url)

        def close(self):
            if not self.closed:
                self.closed = True
                with lock:
                    requests['active'] -= 1
                    active_hosts[self.host] -= 1

    monkeypatch.setattr(acquisition, 'fetch_public_resource', real_public_fetch)
    monkeypatch.setattr(acquisition, 'getproxies_environment', lambda: {})
    monkeypatch.setattr(acquisition, '_public_addresses', lambda *args: ['93.184.216.34'])
    monkeypatch.setattr(acquisition, '_PinnedHTTPS', PublicConnection)
    monkeypatch.setattr(acquisition.PublicFetchLimiter, '_slot', observe_slot)
    with background_collection(release) as (outcome, _):
        assert contended.wait(10), 'GUI and HTTP lanes did not contend for the shared request budget'
        assert full.wait(10)
        with lock:
            assert len(limiters) == 1
            assert requests['active'] == requests['peak'] == fetch_workers
    assert 'error' not in outcome
    assert requests['active'] == 0 and requests['peak'] == fetch_workers
    assert peak_hosts['mp.weixin.qq.com'] == 1
    assert all(peak == 1 for peak in peak_hosts.values())
    assert len(gui.boundary.posts) == 9


def test_gui_bounded_snapshot_keeps_partial_and_real_queue_provenance(wechat_environment):
    gui = wechat_environment
    old = '2025-01-01'
    gui.snapshot.update(complete=False, message='公开时间线仍有未检查内容。', links=[
        {'url': url, 'published_date': published, 'disallowed': False}
        for url, published in zip(WECHAT_ARTICLES, [NOW.date().isoformat(), old, None])])
    gui.boundary.put(WECHAT_ARTICLES[1], wechat_article('历史文章正文', old))
    gui.boundary.put(WECHAT_ARTICLES[2], wechat_article('未说明发布日期的置顶正文', None))
    run = daily.run_once(now=NOW, mode='new')
    result = source_result(run)
    assert run['status'] == result['status'] == 'partial'
    assert result['last_success_at'] is None
    assert result['discovered'] == 3 and result['extracted'] == 1
    assert len(gui.boundary.posts) == 1
    assert result['fetched'] == 2
    with closing(runtime.connect_db()) as db:
        rows = db.execute('''SELECT url,state,eligible,first_seen,last_seen,priority_date,attempt_run
            FROM daily_links ORDER BY url''').fetchall()
        assert rows == [
            (WECHAT_ARTICLES[0], 'done', 1, NOW.isoformat(), NOW.isoformat(), NOW.date().isoformat(), run['id']),
            (WECHAT_ARTICLES[1], 'pending', 0, NOW.isoformat(), NOW.isoformat(), old, None),
            (WECHAT_ARTICLES[2], 'history_deferred', 0, NOW.isoformat(), NOW.isoformat(), None, run['id'])]
        assert db.execute('SELECT url,state,checked_at FROM daily_pages').fetchall() == [
            (WECHAT_REFERENCE, 'done', NOW.isoformat())]
        assert db.execute('SELECT bootstrap_at,last_success_at FROM daily_source_state').fetchone() == (
            NOW.isoformat(), None)
        document_id = db.execute('SELECT id FROM source_documents WHERE source_url=?', (WECHAT_ARTICLES[0],)).fetchone()[0]
    document = information.get_document(document_id)
    assert document['source_type'] == 'wechat' and document['capture_complete'] is True
    assert document['raw_content'] == wechat_article()
    assert document['source_id'] == 'test-college'
    assert all(record['verification_status'] != 'approved' for record in information.query_records({})['records'])

    # A bounded archive pass retains one article per run without paying for old or
    # undated pinned content; an incomplete snapshot never advances success.
    for offset, fetched in ((1, 1), (2, 0)):
        result = source_result(daily.run_once(now=NOW + timedelta(days=offset), mode='history'))
        assert result['status'] == 'partial' and result['last_success_at'] is None
        assert result['fetched'] == fetched and result['extracted'] == 0
    assert len(gui.boundary.posts) == 1
    with closing(runtime.connect_db()) as db:
        retained = db.execute('''SELECT l.url,l.state,l.eligible,l.first_seen,l.document_id,d.source_published_date
            FROM daily_links l JOIN source_documents d ON d.id=l.document_id
            WHERE l.eligible=0 ORDER BY l.url''').fetchall()
        assert [(row[0], row[1], row[2], row[3], row[5]) for row in retained] == [
            (WECHAT_ARTICLES[1], 'history_deferred', 0, NOW.isoformat(), old),
            (WECHAT_ARTICLES[2], 'history_deferred', 0, NOW.isoformat(), None)]
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 1
    daily.run_once(now=NOW + timedelta(days=3), mode='new')
    assert len(gui.boundary.posts) == 1


@pytest.mark.parametrize('mismatch', ['reference', 'capture'])
def test_gui_reference_and_each_http_capture_require_matching_public_biz(wechat_environment, mismatch):
    gui = wechat_environment
    url = WECHAT_REFERENCE if mismatch == 'reference' else WECHAT_ARTICLES[0]
    gui.boundary.put(url, wechat_article(biz='MzAxODAzMjQ1Ng=='))
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert result['status'] == ('failed' if mismatch == 'reference' else 'partial')
    assert result['last_success_at'] is None and result['extracted'] == 0
    assert len(gui.calls) == (0 if mismatch == 'reference' else 1)
    assert gui.boundary.posts == []
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0
        if mismatch == 'capture':
            assert db.execute('SELECT state,document_id FROM daily_links').fetchall() == [('fetch_failed', None)]

@pytest.fixture
def queued_wechat_history(wechat_environment):
    gui = wechat_environment
    gui.snapshot['links'] = [
        {'url': url, 'published_date': '2025-01-01', 'disallowed': False}
        for url in WECHAT_ARTICLES[:2]]
    for url in WECHAT_ARTICLES[:2]:
        gui.boundary.put(url, wechat_article('已发现的历史原文', '2025-01-01'))
    first = daily.run_once(now=NOW, mode='history')
    result = source_result(first)
    assert first['status'] == result['status'] == 'partial'
    assert result['discovered'] == 2 and result['fetched'] == 1
    assert result['extracted'] == 0 and result['pending'] == 1
    with closing(runtime.connect_db()) as db:
        gui.first_document = db.execute('SELECT id FROM source_documents').fetchone()[0]
        gui.queue_provenance = db.execute('''SELECT id,url,definition_key,first_seen,last_seen
            FROM daily_links ORDER BY id''').fetchall()
        gui.pending_url = db.execute("SELECT url FROM daily_links WHERE state='pending'").fetchone()[0]
    gui.first_run = first
    gui.boundary.fetches.clear()
    return gui


def fail_gui_discovery(monkeypatch, error):
    from nannanews import wechat_desktop

    def discover(*args, **kwargs):
        raise error

    monkeypatch.setattr(wechat_desktop, 'discover_articles', discover)


@pytest.mark.parametrize('error_code', ['source_runtime_unavailable', 'source_blocked'])
def test_gui_offline_history_continues_known_raw_queue_without_success_checkpoint(
        queued_wechat_history, monkeypatch, error_code):
    gui = queued_wechat_history
    fail_gui_discovery(monkeypatch, acquisition.AcquisitionError('专用公开桌面暂时不可用。', error_code))
    clock = NOW + timedelta(days=1)
    run = daily.run_once(now=clock, mode='history')
    result = source_result(run)
    assert run['status'] == result['status'] == 'partial'
    assert result['discovered'] == result['extracted'] == result['pending'] == 0
    assert result['fetched'] == result['failed'] == 1 and result['last_success_at'] is None
    assert run['coverage_gaps'] and '_identity_verified' not in json.dumps(run)
    assert gui.boundary.fetches == [WECHAT_REFERENCE, gui.pending_url]
    assert gui.boundary.posts == []
    source = information.list_sources()[0]
    assert source['enabled'] and source['status'] == 'verified' and source['last_success_at'] is None
    with closing(runtime.connect_db()) as db:
        assert db.execute('''SELECT id,url,definition_key,first_seen,last_seen
            FROM daily_links ORDER BY id''').fetchall() == gui.queue_provenance
        assert db.execute('SELECT bootstrap_at,last_success_at,history_success_at FROM daily_source_state').fetchone() == (
            NOW.isoformat(), None, None)
        assert db.execute('SELECT url,state,checked_at,attempt_run FROM daily_pages').fetchone() == (
            WECHAT_REFERENCE, 'pending', NOW.isoformat(), run['id'])
        assert db.execute('SELECT attempt_run FROM daily_links WHERE document_id=?',
                          (gui.first_document,)).fetchone() == (gui.first_run['id'],)
        document_id, attempt_run = db.execute('SELECT document_id,attempt_run FROM daily_links WHERE url=?',
                                              (gui.pending_url,)).fetchone()
        assert attempt_run == run['id']
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0
    document = information.get_document(document_id)
    assert document['source_type'] == 'wechat' and document['source_id'] == 'test-college'
    assert document['canonical_url'] == gui.pending_url and document['capture_complete'] is True
    assert document['raw_content'] == wechat_article('已发现的历史原文', '2025-01-01')
    assert document['model_status'] == 'not_extracted'
    gui.boundary.fetches.clear()
    again = source_result(daily.run_once(now=clock + timedelta(minutes=1), mode='history'))
    assert again['status'] == 'partial' and again['fetched'] == 0 and again['last_success_at'] is None
    assert gui.boundary.fetches == [WECHAT_REFERENCE]


@pytest.mark.parametrize('failure', ['reference_http', 'reference_runtime', 'reference_identity', 'reference_captcha',
                                     'gui_captcha', 'unknown_code', 'unexpected_error', 'verification_flag'])
def test_gui_offline_queue_stays_blocked_without_fresh_identity_or_on_verification(
        queued_wechat_history, monkeypatch, failure):
    gui = queued_wechat_history
    error = acquisition.AcquisitionError('专用公开桌面暂时不可用。', 'source_blocked')
    if failure in ('reference_http', 'reference_runtime', 'reference_captcha'):
        code = {'reference_http': 'source_blocked', 'reference_runtime': 'source_runtime_unavailable',
                'reference_captcha': 'source_verification_required'}[failure]
        gui.boundary.resources[WECHAT_REFERENCE] = acquisition.AcquisitionError('身份参考文章不可验证。', code)
    elif failure == 'reference_identity':
        gui.boundary.put(WECHAT_REFERENCE, wechat_article(biz='MzAxODAzMjQ1Ng=='))
    elif failure == 'gui_captcha':
        error = acquisition.AcquisitionError('请完成人工验证；不自动处理。', 'source_verification_required')
    elif failure == 'unknown_code':
        error = acquisition.AcquisitionError('公开界面发生其他错误。', 'source_empty')
    elif failure == 'unexpected_error':
        error = RuntimeError('unexpected GUI failure')
        error.code = 'source_blocked'
    elif failure == 'verification_flag':
        error.verification_required = True
    fail_gui_discovery(monkeypatch, error)
    run = daily.run_once(now=NOW + timedelta(days=1), mode='history')
    result = source_result(run)
    assert run['status'] == result['status'] == 'failed'
    assert result['discovered'] == result['fetched'] == result['extracted'] == 0
    assert result['pending'] == result['failed'] == 1 and result['last_success_at'] is None
    assert gui.boundary.fetches == [WECHAT_REFERENCE] and gui.boundary.posts == []
    with closing(runtime.connect_db()) as db:
        assert db.execute('''SELECT id,url,definition_key,first_seen,last_seen
            FROM daily_links ORDER BY id''').fetchall() == gui.queue_provenance
        assert db.execute("SELECT attempt_run FROM daily_links WHERE state='pending'").fetchone() == (None,)
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 1
        assert db.execute('SELECT last_success_at,history_success_at FROM daily_source_state').fetchone() == (None, None)
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0


def test_gui_offline_queue_never_crosses_replaced_definition(queued_wechat_history, monkeypatch):
    gui = queued_wechat_history
    private = runtime.settings.data_dir / 'automation_sources.json'
    definitions = json.loads(private.read_text(encoding='utf-8'))
    definitions[0]['wechat_biz'] = 'MzAxODAzMjQ1Ng=='
    private.write_text(json.dumps(definitions), encoding='utf-8')
    gui.boundary.put(WECHAT_REFERENCE, wechat_article(biz=definitions[0]['wechat_biz']))
    fail_gui_discovery(monkeypatch, acquisition.AcquisitionError('专用桌面不可用。', 'source_runtime_unavailable'))
    result = source_result(daily.run_once(now=NOW + timedelta(days=1), mode='history'))
    assert result['status'] == 'partial' and result['paused_by_configuration'] > 0
    assert result['discovered'] == result['fetched'] == result['extracted'] == 0
    assert gui.boundary.fetches == [WECHAT_REFERENCE] and gui.boundary.posts == []
    with closing(runtime.connect_db()) as db:
        assert db.execute('''SELECT id,url,definition_key,first_seen,last_seen
            FROM daily_links ORDER BY id''').fetchall() == gui.queue_provenance


def test_gui_offline_new_pass_keeps_unknown_date_raw_budget_and_no_payment(wechat_environment, monkeypatch):
    gui = wechat_environment
    urls = [f'https://mp.weixin.qq.com/s/offline-queued-{index}' for index in range(9)]
    gui.snapshot['links'] = [{'url': url, 'published_date': None, 'disallowed': False} for url in urls]
    for url in urls:
        gui.boundary.put(url, wechat_article('未说明发布日期的已知原文', None))
    first = daily.run_once(now=NOW, mode='new')
    assert source_result(first)['discovered'] == 9 and source_result(first)['fetched'] == 4
    checkpoint = source_result(first)['last_success_at']
    assert checkpoint == NOW.isoformat()
    fail_gui_discovery(monkeypatch, acquisition.AcquisitionError('专用桌面不可用。', 'source_blocked'))
    gui.boundary.fetches.clear()
    run = daily.run_once(now=NOW + timedelta(days=1), mode='new')
    result = source_result(run)
    assert run['status'] == result['status'] == 'partial' and run['coverage_gaps']
    assert result['discovered'] == result['extracted'] == 0 and result['fetched'] == 4
    assert result['last_success_at'] == checkpoint and gui.boundary.posts == []
    assert gui.boundary.fetches[0] == WECHAT_REFERENCE
    assert set(gui.boundary.fetches[1:]) == set(urls[4:8])
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 8
        assert db.execute("SELECT COUNT(*) FROM daily_links WHERE state='pending' AND attempt_run IS NULL").fetchone()[0] == 1
        assert db.execute('SELECT DISTINCT first_seen,last_seen FROM daily_links').fetchall() == [
            (NOW.isoformat(), NOW.isoformat())]
        assert db.execute('SELECT last_success_at,history_success_at FROM daily_source_state').fetchone() == (checkpoint, None)
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0


def test_gui_offline_queue_still_verifies_each_http_article_before_save(queued_wechat_history, monkeypatch):
    gui = queued_wechat_history
    gui.boundary.put(gui.pending_url, wechat_article(biz='MzAxODAzMjQ1Ng=='))
    fail_gui_discovery(monkeypatch, acquisition.AcquisitionError('专用桌面不可用。', 'source_runtime_unavailable'))
    result = source_result(daily.run_once(now=NOW + timedelta(days=1), mode='history'))
    assert result['status'] == 'partial' and result['last_success_at'] is None
    assert result['discovered'] == result['fetched'] == result['extracted'] == 0
    assert result['fetch_failures'] == 1 and gui.boundary.posts == []
    assert gui.boundary.fetches == [WECHAT_REFERENCE, gui.pending_url]
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT state,document_id FROM daily_links WHERE url=?',
                          (gui.pending_url,)).fetchone() == ('fetch_failed', None)
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 1
        assert db.execute('''SELECT id,url,definition_key,first_seen,last_seen
            FROM daily_links ORDER BY id''').fetchall() == gui.queue_provenance
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0


@pytest.mark.parametrize('error_code', ['source_runtime_unavailable', 'source_blocked'])
def test_non_gui_listing_failure_still_blocks_known_history_queue(environment, error_code):
    identity_url = 'https://college.nju.edu.cn/about.htm'
    definitions = json.loads(daily.SEED_PATH.read_text(encoding='utf-8'))
    definitions[0]['identity_url'] = identity_url
    daily.SEED_PATH.write_text(json.dumps(definitions), encoding='utf-8')
    environment.put(identity_url, '<html><title>南京大学测试学院</title></html>')
    environment.put(ROOT, listing([1], '/notices/2.htm'))
    environment.put('https://college.nju.edu.cn/notices/2.htm', listing([2, 3]))
    for identity in (2, 3):
        environment.put(f'https://college.nju.edu.cn/info/1001/{identity}.htm', article(identity, published='2025-01-01'))
    first = source_result(daily.run_once(now=NOW, mode='history'))
    assert first['discovered'] == 3 and first['fetched'] == first['pending'] == 1
    environment.resources[ROOT] = acquisition.AcquisitionError('列表暂时不可用。', error_code)
    environment.fetches.clear()
    result = source_result(daily.run_once(now=NOW + timedelta(days=1), mode='history'))
    assert result['status'] == 'failed' and result['fetched'] == result['extracted'] == 0
    assert result['pending'] == 1 and result['last_success_at'] is None
    assert environment.fetches == [identity_url, ROOT] and environment.posts == []


WECHAT_PAID_HISTORY = [
    ('completed', None, None), ('failed', None, None), ('extracting', None, None),
    *[('not_extracted', 'source_document_model_attempts', status)
      for status in ('completed', 'failed', 'extracting')],
    *[('not_extracted', 'daily_model_attempts', status)
      for status in ('done', 'failed', 'uncertain', 'attempted')],
]


def retain_wechat_paid_history(document_id, model_status, attempt_table, attempt_status):
    with closing(runtime.connect_db()) as db, db:
        db.execute('UPDATE source_documents SET model_status=? WHERE id=?', (model_status, document_id))
        if attempt_table == 'source_document_model_attempts':
            db.execute('''INSERT INTO source_document_model_attempts(document_id,actor,started_at,model_status)
                VALUES (?,'explicit-prior-attempt',?,?)''', (document_id, NOW.isoformat(), attempt_status))
        elif attempt_table == 'daily_model_attempts':
            db.execute('''INSERT OR IGNORE INTO daily_runs(id,actor,started_at,status,mode)
                VALUES (900,'prior-daily',?,'partial','new')''', (NOW.isoformat(),))
            db.execute('INSERT INTO daily_model_attempts(document_id,run_id,status) VALUES (?,900,?)',
                       (document_id, attempt_status))
        return {table: db.execute(f'SELECT * FROM {table} WHERE ' +
                                 ('id=?' if table == 'source_documents' else 'document_id=?'),
                                 (document_id,)).fetchall()
                for table in ('source_documents', 'source_document_model_attempts', 'daily_model_attempts')}


def assert_wechat_prior_evidence_unchanged(prior, evidence):
    with closing(runtime.connect_db()) as db:
        for table, expected in evidence.items():
            assert db.execute(f'SELECT * FROM {table} WHERE ' +
                              ('id=?' if table == 'source_documents' else 'document_id=?'),
                              (prior,)).fetchall() == expected
        current = db.execute('SELECT id,digest,model_status FROM source_documents WHERE id!=?', (prior,)).fetchall()
        assert len(current) == 1 and current[0][2] == 'not_extracted'
        assert current[0][1] != db.execute('SELECT digest FROM source_documents WHERE id=?', (prior,)).fetchone()[0]
        assert db.execute('SELECT state,document_id FROM daily_links').fetchall() == [('model_uncertain', current[0][0])]
        for table in ('source_document_model_attempts', 'daily_model_attempts'):
            assert db.execute(f'SELECT COUNT(*) FROM {table} WHERE document_id=?', (current[0][0],)).fetchone()[0] == 0


@pytest.mark.parametrize('model_status,attempt_table,attempt_status', WECHAT_PAID_HISTORY)
def test_gui_changed_digest_never_repays_any_same_url_paid_history(
        wechat_environment, monkeypatch, model_status, attempt_table, attempt_status):
    gui = wechat_environment
    from nannanews import wechat_desktop
    url = WECHAT_ARTICLES[0]
    daily.initialize_sources()
    # A pasted/manual capture without this source association still protects its URL.
    prior = information.save_document(acquisition.fetch_document(url), source_id=None, actor='manual')['id']
    evidence = {}
    discover = wechat_desktop.discover_articles

    def prior_before_capture(*args, **kwargs):
        # Install each exact paid state after startup recovery (which legitimately
        # turns interrupted daily attempted records into uncertain), before capture.
        evidence.update(retain_wechat_paid_history(prior, model_status, attempt_table, attempt_status))
        return discover(*args, **kwargs)

    monkeypatch.setattr(wechat_desktop, 'discover_articles', prior_before_capture)
    gui.boundary.put(url, wechat_article('正文和公开页面模板已变化'))
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert result['fetched'] == 1 and result['extracted'] == 0
    assert gui.boundary.posts == []
    assert_wechat_prior_evidence_unchanged(prior, evidence)


@pytest.mark.parametrize('model_status,attempt_table,attempt_status', WECHAT_PAID_HISTORY)
def test_gui_same_url_paid_history_is_rechecked_inside_atomic_reservation(
        wechat_environment, monkeypatch, model_status, attempt_table, attempt_status):
    from nannanews import model_audit

    gui = wechat_environment
    url = WECHAT_ARTICLES[0]
    daily.initialize_sources()
    prior = information.save_document(acquisition.fetch_document(url), source_id=None, actor='manual')['id']
    gui.boundary.put(url, wechat_article('另一版本的完整公开正文'))
    initialize = model_audit.ModelAudit.__init__
    evidence = {}

    def interleave(self, *args, **kwargs):
        initialize(self, *args, **kwargs)
        assert not evidence
        # The new version passed daily's preliminary check. Another caller's old
        # version acquires paid history during audit preparation, before admission.
        evidence.update(retain_wechat_paid_history(prior, model_status, attempt_table, attempt_status))

    monkeypatch.setattr(model_audit.ModelAudit, '__init__', interleave)
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert evidence and result['extracted'] == 0 and gui.boundary.posts == []
    assert_wechat_prior_evidence_unchanged(prior, evidence)


def test_gui_unpaid_prior_version_does_not_block_first_eligible_extraction(wechat_environment):
    gui = wechat_environment
    url = WECHAT_ARTICLES[0]
    daily.initialize_sources()
    prior = information.save_document(acquisition.fetch_document(url), source_id=None, actor='manual')['id']
    gui.boundary.put(url, wechat_article('首次付费前更新的公开正文'))
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert result['extracted'] == 1 and len(gui.boundary.posts) == 1
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT model_status FROM source_documents WHERE id=?', (prior,)).fetchone() == ('not_extracted',)
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 1


def test_gui_comment_publication_date_never_authorizes_undated_article_payment(wechat_environment):
    from nannanews import wechat_public

    gui = wechat_environment
    payload = {'@type': 'NewsArticle', 'comment': {
        '@type': 'Comment', 'datePublished': NOW.date().isoformat()}}
    raw = wechat_article('无发布日期的历史置顶文章', None).replace(
        '</body>', '<script type="application/ld+json">' + json.dumps(payload) + '</script></body>')
    gui.boundary.put(WECHAT_ARTICLES[0], raw)
    document = acquisition.fetch_document(WECHAT_ARTICLES[0])
    gui.snapshot['links'][0]['published_date'] = wechat_public.published_date(document)

    daily.run_once(now=NOW, mode='new')
    daily.run_once(now=NOW + timedelta(days=1), mode='history')

    assert gui.boundary.posts == []
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0
        assert db.execute('''SELECT l.state,l.eligible,d.source_published_date
            FROM daily_links l JOIN source_documents d ON d.id=l.document_id''').fetchone() == (
                'history_deferred', 0, None)


@pytest.mark.parametrize('published,expected_paid', [('2026-10-14', True), ('2025-01-01', False), (None, False)])
def test_gui_url_without_listing_date_is_captured_before_paid_eligibility(wechat_environment, published, expected_paid):
    gui = wechat_environment
    gui.snapshot['links'][0]['published_date'] = None
    gui.boundary.put(WECHAT_ARTICLES[0], wechat_article('原文中的明确发布日期决定是否提取', published))
    daily.run_once(now=NOW, mode='new')
    assert len(gui.boundary.posts) == int(expected_paid)
    with closing(runtime.connect_db()) as db:
        row = db.execute('''SELECT l.state,l.eligible,d.source_published_date,d.model_status
            FROM daily_links l JOIN source_documents d ON d.id=l.document_id''').fetchone()
        assert row == ('done' if expected_paid else 'history_deferred', int(expected_paid), published,
                       'completed' if expected_paid else 'not_extracted')
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == int(expected_paid)


def test_gui_original_failure_keeps_discovered_url_for_unpaid_capture(wechat_environment):
    gui = wechat_environment
    gui.snapshot['links'][0]['published_date'] = None
    gui.boundary.put(WECHAT_ARTICLES[0], wechat_article(published=None))
    original = gui.boundary.resources[WECHAT_ARTICLES[0]]
    gui.boundary.resources[WECHAT_ARTICLES[0]] = acquisition.AcquisitionError('原文连接失败。', 'source_blocked')
    daily.run_once(now=NOW, mode='new')
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT url,state,document_id FROM daily_links').fetchone() == (
            WECHAT_ARTICLES[0], 'fetch_failed', None)
    gui.boundary.resources[WECHAT_ARTICLES[0]] = original
    daily.run_once(now=NOW, mode='new')
    assert gui.boundary.posts == []
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT state,eligible FROM daily_links').fetchone() == ('history_deferred', 0)
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0


@pytest.mark.parametrize('ambiguous', [False, True])
def test_gui_cached_queue_date_cannot_qualify_an_undated_http_version(wechat_environment, ambiguous):
    gui = wechat_environment
    raw = wechat_article('原文没有可核对的发布日期', None)
    if ambiguous:
        raw += '<em id="publish_time">2026-10-14</em><meta property="article:published_time" content="2025-01-01">'
    gui.boundary.put(WECHAT_ARTICLES[0], raw)
    # The listing/queue still has a current priority date; only fresh HTTP
    # publication evidence may admit the newly captured GUI document.
    daily.run_once(now=NOW, mode='new')
    assert gui.boundary.posts == []
    with closing(runtime.connect_db()) as db:
        assert db.execute('''SELECT l.state,l.eligible,d.source_published_date
            FROM daily_links l JOIN source_documents d ON d.id=l.document_id''').fetchone() == (
                'history_deferred', 0, None)
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0


@pytest.mark.parametrize('priority_kind', ['publication', 'event'])
@pytest.mark.parametrize('cached_document_date', [False, True])
def test_gui_retained_ready_row_cannot_pay_using_only_cached_priority(wechat_environment, priority_kind, cached_document_date):
    gui = wechat_environment
    gui.snapshot['links'][0]['published_date'] = None
    gui.boundary.put(WECHAT_ARTICLES[0], wechat_article('没有发布日期的保留原文', None))
    daily.run_once(now=NOW, mode='history')
    with closing(runtime.connect_db()) as db, db:
        priority = (NOW + timedelta(days=1)).date().isoformat() if priority_kind == 'event' else NOW.date().isoformat()
        db.execute("UPDATE daily_links SET state='ready',eligible=1,priority_kind=?,priority_date=?",
                   (priority_kind, priority))
        if cached_document_date:
            db.execute('UPDATE source_documents SET source_published_date=?', (NOW.date().isoformat(),))
    daily.run_once(now=NOW, mode='new')
    assert gui.boundary.posts == []
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT state,eligible FROM daily_links').fetchone() == ('history_deferred', 0)
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0


def test_failed_gui_raw_batch_cannot_starve_untouched_discovered_urls(wechat_environment):
    gui = wechat_environment
    failed = [f'https://mp.weixin.qq.com/s/failed-{i}' for i in range(4)]
    fresh = [f'https://mp.weixin.qq.com/s/untouched-{i}' for i in range(4)]
    gui.snapshot['links'] = [{'url': url, 'published_date': None, 'disallowed': False} for url in failed]
    for url in failed:
        gui.boundary.resources[url] = acquisition.AcquisitionError('原文暂时不可读取。', 'source_blocked')
    first = daily.run_once(now=NOW, mode='new')
    gui.snapshot['links'] = [{'url': url, 'published_date': None, 'disallowed': False} for url in fresh]
    for url in fresh:
        gui.boundary.put(url, wechat_article('新发现且从未抓取的原文', None))
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert result['fetched'] == 4
    assert gui.boundary.posts == []
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 4
        assert db.execute("SELECT COUNT(*) FROM daily_links WHERE state='history_deferred'").fetchone()[0] == 4
        assert {row[0] for row in db.execute("SELECT attempt_run FROM daily_links WHERE state='fetch_failed'")} == {first['id']}
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0


OFFICIAL_WECHAT_ROOT = 'https://college.nju.edu.cn/wechat.htm'


def official_wechat_listing(urls):
    links = ''.join(f'<li><a href="{url}">公众号原文</a><time>{NOW.date().isoformat()}</time></li>'
                    for url in urls)
    return '<html><title>南京大学测试学院公众号动态</title><body><ul>' + links + '</ul></body></html>'


@pytest.fixture
def official_wechat_environment(environment, monkeypatch):
    from nannanews import wechat_desktop

    daily.SEED_PATH.write_text(json.dumps([{
        'id': 'test-college', 'name': '南京大学公众号官网原文链接', 'url': OFFICIAL_WECHAT_ROOT,
        'identity_url': WECHAT_REFERENCE, 'expected_identity': '南京大学',
        'wechat_biz': WECHAT_BIZ, 'listing_identity': '南京大学测试学院',
        'kind': 'wechat_official_links', 'enabled': True
    }]), encoding='utf-8')
    environment.put(WECHAT_REFERENCE, wechat_article('发布者身份参考文章', '2025-01-01'))
    environment.put(OFFICIAL_WECHAT_ROOT, official_wechat_listing(WECHAT_ARTICLES[:1]))
    for url in WECHAT_ARTICLES:
        environment.put(url, wechat_article())

    def forbidden_desktop(*args, **kwargs):
        pytest.fail('Official-site WeChat discovery must not invoke the desktop.')

    monkeypatch.setattr(wechat_desktop, 'discover_articles', forbidden_desktop)
    return environment


def test_official_links_capture_verified_originals_on_http_lane_without_full_account_checkpoint(
        official_wechat_environment, monkeypatch):
    from threading import current_thread

    boundary = official_wechat_environment
    boundary.put(OFFICIAL_WECHAT_ROOT, official_wechat_listing(WECHAT_ARTICLES))
    boundary.put(WECHAT_ARTICLES[1], wechat_article('完整历史原文', '2025-01-01'))
    boundary.put(WECHAT_ARTICLES[2], wechat_article('未说明发布日期的原文', None))
    fetch = boundary.fetch
    listing_threads = []

    def observe_http(url, *args, **kwargs):
        if url == OFFICIAL_WECHAT_ROOT:
            listing_threads.append(current_thread().name)
        return fetch(url, *args, **kwargs)

    monkeypatch.setattr(acquisition, 'fetch_public_resource', observe_http)
    run = daily.run_once(now=NOW, mode='new')
    result = source_result(run)
    assert run['status'] == result['status'] == 'partial'
    assert result['discovered'] == result['fetched'] == 3
    assert result['extracted'] == 1 and len(boundary.posts) == 1
    assert result['last_success_at'] is None
    assert listing_threads and all(name.startswith('daily-web') for name in listing_threads)
    with closing(runtime.connect_db()) as db:
        rows = db.execute('''SELECT l.url,l.state,l.eligible,l.priority_date,l.first_seen,
            d.id,d.source_published_date,d.source_id FROM daily_links l
            JOIN source_documents d ON d.id=l.document_id ORDER BY l.url''').fetchall()
        assert [(row[0], row[1], row[2], row[3], row[4], row[6], row[7]) for row in rows] == [
            (WECHAT_ARTICLES[0], 'done', 1, NOW.date().isoformat(), NOW.isoformat(),
             NOW.date().isoformat(), 'test-college'),
            (WECHAT_ARTICLES[1], 'history_deferred', 0, '2025-01-01', NOW.isoformat(),
             '2025-01-01', 'test-college'),
            (WECHAT_ARTICLES[2], 'history_deferred', 0, None, NOW.isoformat(), None, 'test-college')]
        assert db.execute('SELECT url,state FROM daily_pages').fetchall() == [(OFFICIAL_WECHAT_ROOT, 'done')]
        assert db.execute('SELECT last_success_at,history_success_at FROM daily_source_state').fetchone() == (None, None)
    for row in rows:
        document = information.get_document(row[5])
        assert document['source_type'] == 'wechat' and document['capture_complete'] is True
        assert document['raw_content'] == boundary.resources[row[0]][0].decode()
    assert all(record['verification_status'] != 'approved' for record in information.query_records({})['records'])


def test_official_links_history_retains_complete_raw_without_models_or_success_checkpoint(official_wechat_environment):
    boundary = official_wechat_environment
    boundary.put(OFFICIAL_WECHAT_ROOT, official_wechat_listing(WECHAT_ARTICLES))
    for url in WECHAT_ARTICLES:
        boundary.put(url, wechat_article('保留的完整历史原文', '2025-01-01'))
    for offset in range(4):
        result = source_result(daily.run_once(now=NOW + timedelta(days=offset), mode='history'))
        assert result['status'] == 'partial' and result['last_success_at'] is None
        assert result['fetched'] == int(offset < 3) and result['extracted'] == 0
    assert boundary.posts == []
    with closing(runtime.connect_db()) as db:
        rows = db.execute('''SELECT l.state,l.eligible,d.id,d.source_published_date,d.model_status
            FROM daily_links l JOIN source_documents d ON d.id=l.document_id''').fetchall()
        assert len(rows) == 3
        assert all((row[0], row[1], row[3], row[4]) ==
                   ('history_deferred', 0, '2025-01-01', 'not_extracted') for row in rows)
        assert db.execute('SELECT last_success_at,history_success_at FROM daily_source_state').fetchone() == (None, None)
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0
    assert all(information.get_document(row[2])['capture_complete'] is True for row in rows)


@pytest.mark.parametrize('mismatch', ['reference', 'capture'])
def test_official_links_verify_reference_and_article_biz_before_attribution_or_payment(
        official_wechat_environment, monkeypatch, mismatch):
    boundary = official_wechat_environment
    target = WECHAT_REFERENCE if mismatch == 'reference' else WECHAT_ARTICLES[0]
    discovery_metadata = boundary.resources[target]
    boundary.put(target, wechat_article(biz='MzAxODAzMjQ1Ng=='))
    if mismatch == 'capture':
        fetch = boundary.fetch
        article_reads = 0

        def change_after_discovery(url, *args, **kwargs):
            nonlocal article_reads
            response = fetch(url, *args, **kwargs)
            if url == target:
                article_reads += 1
                if article_reads == 1:
                    # Discovery proves the configured account; fresh capture must
                    # independently reject an article whose publisher changed.
                    return discovery_metadata
            return response

        monkeypatch.setattr(acquisition, 'fetch_public_resource', change_after_discovery)
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert result['status'] == ('failed' if mismatch == 'reference' else 'partial')
    assert result['last_success_at'] is None and result['extracted'] == 0
    assert (OFFICIAL_WECHAT_ROOT in boundary.fetches) is (mismatch == 'capture')
    assert boundary.posts == []
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0
        if mismatch == 'capture':
            assert db.execute('SELECT state,document_id FROM daily_links').fetchall() == [('fetch_failed', None)]


@pytest.mark.parametrize('mode', ['new', 'history'])
@pytest.mark.parametrize('failure', ['source_blocked', 'source_runtime_unavailable', 'publisher_identity'])
def test_official_listing_failure_never_continues_known_queue(official_wechat_environment, mode, failure):
    boundary = official_wechat_environment
    boundary.put(OFFICIAL_WECHAT_ROOT, official_wechat_listing(WECHAT_ARTICLES[:2]))
    for url in WECHAT_ARTICLES[:2]:
        boundary.put(url, wechat_article('已发现的历史原文', '2025-01-01'))
    first = source_result(daily.run_once(now=NOW, mode='history'))
    assert first['discovered'] == 2 and first['fetched'] == 1
    with closing(runtime.connect_db()) as db:
        pending = db.execute("SELECT url FROM daily_links WHERE state='pending'").fetchone()[0]
        provenance = db.execute('SELECT id,url,definition_key,first_seen,last_seen FROM daily_links ORDER BY id').fetchall()
    if failure == 'publisher_identity':
        boundary.put(OFFICIAL_WECHAT_ROOT, '<html><title>其他发布者</title></html>')
    else:
        boundary.resources[OFFICIAL_WECHAT_ROOT] = acquisition.AcquisitionError('官网列表暂时不可用。', failure)
    boundary.fetches.clear()
    result = source_result(daily.run_once(now=NOW + timedelta(days=1), mode=mode))
    assert result['status'] == 'failed' and result['last_success_at'] is None
    assert result['fetched'] == result['extracted'] == 0
    assert boundary.fetches == [WECHAT_REFERENCE, OFFICIAL_WECHAT_ROOT]
    assert pending not in boundary.fetches and boundary.posts == []
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT id,url,definition_key,first_seen,last_seen FROM daily_links ORDER BY id').fetchall() == provenance
        assert db.execute("SELECT attempt_run FROM daily_links WHERE state='pending'").fetchone() == (None,)
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 1
        assert db.execute('SELECT last_success_at,history_success_at FROM daily_source_state').fetchone() == (None, None)


@pytest.mark.parametrize('model_status,attempt_table,attempt_status', WECHAT_PAID_HISTORY)
def test_official_links_changed_digest_never_repays_any_same_url_paid_history(
        official_wechat_environment, monkeypatch, model_status, attempt_table, attempt_status):
    boundary = official_wechat_environment
    url = WECHAT_ARTICLES[0]
    daily.initialize_sources()
    prior = information.save_document(acquisition.fetch_document(url), source_id=None, actor='manual')['id']
    fetch = boundary.fetch
    evidence = {}

    def prior_before_capture(target, *args, **kwargs):
        if target == OFFICIAL_WECHAT_ROOT:
            evidence.update(retain_wechat_paid_history(prior, model_status, attempt_table, attempt_status))
        return fetch(target, *args, **kwargs)

    monkeypatch.setattr(acquisition, 'fetch_public_resource', prior_before_capture)
    boundary.put(url, wechat_article('官网发现的新正文版本'))
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert result['fetched'] == 1 and result['extracted'] == 0
    assert result['status'] == 'partial' and result['last_success_at'] is None
    assert evidence and boundary.posts == []
    assert_wechat_prior_evidence_unchanged(prior, evidence)


@pytest.mark.parametrize('model_status,attempt_table,attempt_status', WECHAT_PAID_HISTORY)
def test_official_links_paid_history_is_rechecked_inside_atomic_reservation(
        official_wechat_environment, monkeypatch, model_status, attempt_table, attempt_status):
    from nannanews import model_audit

    boundary = official_wechat_environment
    url = WECHAT_ARTICLES[0]
    daily.initialize_sources()
    prior = information.save_document(acquisition.fetch_document(url), source_id=None, actor='manual')['id']
    boundary.put(url, wechat_article('通过官网发现的另一完整版本'))
    initialize = model_audit.ModelAudit.__init__
    evidence = {}

    def interleave(self, *args, **kwargs):
        initialize(self, *args, **kwargs)
        assert not evidence
        evidence.update(retain_wechat_paid_history(prior, model_status, attempt_table, attempt_status))

    monkeypatch.setattr(model_audit.ModelAudit, '__init__', interleave)
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert result['status'] == 'partial' and result['last_success_at'] is None
    assert evidence and result['extracted'] == 0 and boundary.posts == []
    assert_wechat_prior_evidence_unchanged(prior, evidence)


def test_official_definition_key_tracks_listing_identity_without_changing_gui_key(
        official_wechat_environment):
    source = next(source for source in daily.initialize_sources() if source['id'] == 'test-college')
    definition = daily._definition(source)
    assert 'configuration_error' not in definition
    key = definition['definition_key']
    assert daily._definition_key(definition | {'listing_identity': '南京大学其他学院'}) != key
    assert daily._definition_key(definition | {'wechat_biz': 'MzAxODAzMjQ1Ng=='}) != key
    gui = definition | {'kind': 'wechat_gui', 'url': WECHAT_REFERENCE}
    assert daily._definition_key(gui | {'listing_identity': '不参与 GUI 队列身份'}) == daily._definition_key(gui)
    assert daily._definition_key(gui | {'wechat_biz': 'MzAxODAzMjQ1Ng=='}) != daily._definition_key(gui)


@pytest.mark.parametrize('priority_kind', ['publication', 'event'])
@pytest.mark.parametrize('cached_document_date', [False, True])
def test_official_retained_ready_row_requires_article_date_not_cached_priority(
        official_wechat_environment, priority_kind, cached_document_date):
    boundary = official_wechat_environment
    boundary.put(WECHAT_ARTICLES[0], wechat_article('未说明发布日期的完整原文', None))
    daily.run_once(now=NOW, mode='history')
    with closing(runtime.connect_db()) as db, db:
        priority = (NOW + timedelta(days=1)).date().isoformat() if priority_kind == 'event' else NOW.date().isoformat()
        db.execute("UPDATE daily_links SET state='ready',eligible=1,priority_kind=?,priority_date=?",
                   (priority_kind, priority))
        if cached_document_date:
            db.execute('UPDATE source_documents SET source_published_date=?', (NOW.date().isoformat(),))
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert result['status'] == 'partial' and result['last_success_at'] is None
    assert boundary.posts == []
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT state,eligible FROM daily_links').fetchone() == ('history_deferred', 0)
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0


def test_official_unknown_date_capture_obeys_bounded_visible_scope(official_wechat_environment):
    boundary = official_wechat_environment
    urls = [f'https://mp.weixin.qq.com/s/official-undated-{index}' for index in range(9)]
    boundary.put(OFFICIAL_WECHAT_ROOT, official_wechat_listing(urls))
    for url in urls:
        boundary.put(url, wechat_article('未说明发布日期的完整原文', None))
    result = source_result(daily.run_once(now=NOW, mode='new'))
    assert result['status'] == 'partial' and result['last_success_at'] is None
    assert result['discovered'] == result['fetched'] == 4
    assert boundary.posts == []
    with closing(runtime.connect_db()) as db:
        assert db.execute('SELECT url,state,eligible FROM daily_links ORDER BY id').fetchall() == [
            (url, 'history_deferred', 0) for url in urls[:4]]
        assert db.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0] == 4
        assert db.execute('SELECT COUNT(*) FROM daily_model_attempts').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM source_document_model_attempts').fetchone()[0] == 0
