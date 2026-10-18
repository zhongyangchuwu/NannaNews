"""Real SQLite/filesystem imports and queries; only PDF engine boundary is fake."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3


import pytest


from nannanews import bulletin_store as store
from nannanews import runtime
from nannanews import schema as migrations

SOURCE = 'https://mp.weixin.qq.com/s/shared-post'


@pytest.fixture
def environment(tmp_path, monkeypatch):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    uploads = private / 'uploads'
    uploads.mkdir(mode=0o700)
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, data_dir=private,
                        database_path=private / 'database.db', upload_dir=uploads,
                        model_base_url='', model_api_key=''))
    migrations.migrate()
    with closing(runtime.connect_db()) as db, db:
        db.execute("INSERT INTO entries(id,uploader,upload_time,title,status) VALUES(1,'editor','2026-10-01','existing private editorial entry','unverified')")
        db.execute("CREATE VIEW existing_public AS SELECT * FROM entries WHERE status='published'")
    return tmp_path


def row(title, deadline='2026-10-15', source=SOURCE, listed='2026-10-12'):
    anchor = f'<a href="{source}">{title}</a>' if source else title
    return f'<tr><td>{anchor}</td><td>{deadline}</td><td>{listed}</td></tr>'


def issue(tmp_path, name, issue_date='2026-10-13', rows=None, details='', category='讲座'):
    path = tmp_path / name
    title = f'南哪消息 {issue_date}'
    path.write_text(f'''<html><body><h1>{title}</h1><div class="section"><h2>{category}</h2>
        <table><thead><tr><th>活动标题</th><th>截止时间</th><th>刊载时间</th></tr></thead>
        <tbody>{rows if rows is not None else row('活动甲')}</tbody></table>{details}</div></body></html>''', encoding='utf-8')
    return path


def sql(query, params=()):
    with closing(runtime.connect_db()) as db:
        return db.execute(query, params).fetchall()

def apply_test_reviews(decisions):
    from nannanews import bulletin_date_repair
    secondary = {'status': 'secondary_only', 'reason': 'Test review did not fetch primary sources.',
                 'sources': [], 'attempts': []}
    with closing(runtime.connect_db()) as db, db:
        db.row_factory = sqlite3.Row
        rows, originals, _ = bulletin_date_repair.date_snapshot(db)
        for occurrence in rows:
            if occurrence['title'] not in decisions:
                continue
            claims = decisions[occurrence['title']]
            known = any(claim['normalized_date'] for claim in claims)
            bulletin_date_repair.record_revision(db, occurrence, originals[occurrence['id']], claims,
                rules_version='test-ai-review', origin='luna_review', reviewer='luna:test',
                status='resolved' if known else 'no_relevant_date', reason='Explicit test review decision.',
                source_review=secondary)


def reviewed_claim(kind, raw, day, resolution='explicit'):
    basis = '原文刊载完整日期。' if resolution == 'explicit' else '原文月日；依据原刊2026-10-13补年。'
    return {'kind': kind, 'raw_text': raw, 'normalized_date': day, 'resolution': resolution,
            'evidence': raw + '；' + basis, 'locator': None}


def test_additive_schema_and_readonly_views_preserve_editorial_boundary(environment):
    before = sql('SELECT * FROM entries')
    columns = sql('PRAGMA table_info(entries)')
    result = store.import_document(issue(environment, 'one.html'))
    assert result['created']
    migrations.migrate()
    assert sql('SELECT * FROM entries') == before
    assert sql('PRAGMA table_info(entries)') == columns
    assert sql('SELECT * FROM existing_public') == []
    assert len(sql('SELECT * FROM bulletin_items_view')) == 1
    with closing(runtime.connect_db()) as db, db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("UPDATE bulletin_items_view SET title='changed'")
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute("UPDATE bulletin_occurrences SET body_text='changed'")


def test_unconfigured_import_keeps_parser_dates_raw_and_semantically_unresolved(environment):
    store.import_document(issue(environment, 'unreviewed.html', rows=row('尚未复核的活动', '2026-10-15')))
    raw = sql('SELECT normalized_date FROM bulletin_dates')
    effective = store.query_items({})['records'][0]
    assert any(value[0] for value in raw)
    assert effective['dates'] == []
    assert effective['date_review']['status'] == 'unresolved'
    assert effective['date_review']['source_review']['status'] == 'secondary_only'

def test_document_hash_idempotence_source_provenance_and_private_original(environment):
    path = issue(environment, 'one.html')
    first = store.import_document(path, source_url='https://example.edu.cn/issue/1')
    repeated = store.import_document(path, source_url='https://example.edu.cn/issue/1')
    alias = environment / 'different-name.html'
    alias.write_bytes(path.read_bytes())
    third = store.import_document(alias, source_url='https://example.edu.cn/issue/alias')
    assert first['status'] == 'created'
    assert repeated['status'] == third['status'] == 'already_imported'
    assert first['document_id'] == third['document_id']
    assert len(sql('SELECT * FROM bulletin_documents')) == 1
    assert len(sql('SELECT * FROM bulletin_occurrences')) == 1
    assert sql('SELECT status FROM bulletin_imports ORDER BY id') == [('created',), ('already_imported',), ('already_imported',)]
    document = store.get_document(first['document_id'])
    assert document['source_urls'] == ['https://example.edu.cn/issue/1', 'https://example.edu.cn/issue/alias']
    saved = runtime.settings.upload_dir / document['original_path']
    assert saved.read_bytes() == path.read_bytes()
    assert saved.parent.name == hashlib.sha256(path.read_bytes()).hexdigest()
    assert saved.stat().st_mode & 0o777 == 0o600
    assert saved.parent.stat().st_mode & 0o777 == 0o700
    assert (runtime.settings.upload_dir / document['normalized_path']).is_file()
    assert store.query_items({'source':'https://example.edu.cn/issue/alias'})['total'] == 1


def test_repeated_publications_keep_every_summary_and_detail(environment):
    details = f'''<ul><li><h3 id="detail">活动甲</h3>原文：<a href="{SOURCE}">原文</a><br>
        活动时间：2026年10月15日 14:00<br>第一版本原文；面向历史学院学生。</li></ul>'''
    first = store.import_document(issue(environment, 'one.html', details=details))
    updated = details.replace('第一版本原文', '第二版本原文，内容已经修改')
    second = store.import_document(issue(environment, 'two.html', '2026-10-14', details=updated))
    assert first['occurrence_count'] == second['occurrence_count'] == 2
    assert store.query_items({})['total'] == 1
    item = store.get_item(store.query_items({})['records'][0]['id'])
    assert item['occurrence_count'] == 4
    assert {o['document_id'] for o in item['occurrences']} == {first['document_id'],second['document_id']}
    assert any('第一版本原文' in o['body_text'] for o in item['occurrences'])
    assert any('第二版本原文' in o['body_text'] for o in item['occurrences'])
    assert {o['kind'] for o in item['occurrences']} == {'summary','detail'}
    assert item['issue_date'] == '2026-10-14'
    assert item['kind'] == 'detail'
    assert all(d['occurrence_id'] and d['source_sha256'] for d in item['dates'])
    assert all(c['relation'] != 'publisher' for c in item['college_mentions'])
    historical = store.query_items({'issue_date_to':'2026-10-13'})['records'][0]
    assert historical['document_id'] == first['document_id']
    assert historical['issue_date'] == '2026-10-13'


def test_source_uri_title_and_date_identity_are_conservative(environment):
    rows = row('讲座甲') + row('讲座乙') + row('讲座甲','2026-10-16')
    result = store.import_document(issue(environment, 'one.html', rows=rows))
    assert result['item_count'] == 3
    # A missing URL does not become a title-only key; dates/body must match exactly.
    result2 = store.import_document(issue(environment, 'two.html', '2026-10-14', rows=row('讲座甲','2026-10-18',None)))
    assert result2['item_count'] == 1
    assert store.query_items({})['total'] == 4
    assert len(sql('SELECT * FROM bulletin_occurrences')) == 4


def test_exact_content_without_url_links_only_identical_body(environment):
    store.import_document(issue(environment, 'one.html', rows=row('无来源活动',source=None)))
    store.import_document(issue(environment, 'two.html','2026-10-14',rows=row('无来源活动',source=None)))
    store.import_document(issue(environment, 'three.html','2026-10-15',rows=row('无来源活动','2026-10-17',None)))
    assert store.query_items({})['total'] == 2
    repeated = next(r for r in store.query_items({})['records'] if r['occurrence_count']==2)
    assert store.get_item(repeated['id'])['identity_basis'] == 'exact_content'


def test_reviewed_activity_groups_collapse_queries_but_keep_raw_item_ids(environment):
    from nannanews import bulletin_activities
    store.import_document(issue(environment, 'first.html', '2026-10-13',
        rows=row('同一活动', '待定', 'https://example.org/first')))
    store.import_document(issue(environment, 'second.html', '2026-10-14',
        rows=row('同一活动', '待定', 'https://example.org/second')))
    raw_ids = [row[0] for row in sql('SELECT id FROM bulletin_items ORDER BY id')]
    assert len(raw_ids) == 2
    assert store.query_items({})['total'] == 2
    with closing(runtime.connect_db()) as db, db:
        group_id = db.execute('''INSERT INTO bulletin_activity_groups
            (canonical_item_id,decision_sha256,input_sha256,reviewer,reason,evidence_json,created_at)
            VALUES(?,?,?,?,?,?,?)''', (raw_ids[0], 'a' * 64, 'b' * 64, 'luna:test', 'Same event.', '{}',
            '2026-10-14T00:00:00+00:00')).lastrowid
        db.execute('INSERT INTO bulletin_activity_members VALUES(?,?)', (group_id, raw_ids[1]))
        assert bulletin_activities.activity_map(db)[raw_ids[1]] == raw_ids[0]
    from nannanews import bulletin_date_repair
    with closing(runtime.connect_db()) as db:
        db.row_factory = sqlite3.Row
        snapshots, _, _ = bulletin_date_repair.date_snapshot(db)
    assert len({row['related_context_sha256'] for row in snapshots}) == 1
    issue_dates = {issue_date for publication in snapshots[0]['related_occurrences']
                   for issue_date in publication['issue_dates']}
    assert issue_dates == {'2026-10-13', '2026-10-14'}
    result = store.query_items({})
    assert result['total'] == 1
    assert result['records'][0]['id'] == raw_ids[0]
    assert result['records'][0]['occurrence_count'] == 2
    detail = store.get_item(raw_ids[1])
    assert detail['id'] == raw_ids[0]
    assert detail['raw_item_ids'] == raw_ids
    assert detail['occurrence_count'] == 2
    assert len(sql('SELECT id FROM bulletin_items')) == 2

def test_ambiguous_same_title_post_uri_does_not_choose_first_lecture(environment):
    details = f'<ul><li><h3>同名讲座</h3>原文：<a href="{SOURCE}">原文</a><br>未写明日期的实际正文。</li></ul>'
    rows = row('同名讲座','2026-10-15') + row('同名讲座','2026-10-16')
    result = store.import_document(issue(environment,'same-title.html',rows=rows,details=details))
    assert result['item_count'] == result['occurrence_count'] == 3
    assert store.query_items({})['total'] == 3


def test_known_unknown_expiry_and_day_boundaries(environment):
    rows = (row('已过期','2026-10-13') + row('今天截止','2026-10-14') + row('未来截止','2026-10-15')
            + row('缺少年份','10月14日') + row('日期范围','2026-10-14—2026-10-16') + row('无日期','待定'))
    store.import_document(issue(environment,'dates.html',rows=rows))
    apply_test_reviews({
        '已过期': [reviewed_claim('deadline', '2026-10-13', '2026-10-13')],
        '今天截止': [reviewed_claim('deadline', '2026-10-14', '2026-10-14')],
        '未来截止': [reviewed_claim('deadline', '2026-10-15', '2026-10-15')],
        '缺少年份': [reviewed_claim('deadline', '10月14日', '2026-10-14', 'contextual')],
        '日期范围': [reviewed_claim('deadline', '2026-10-14—2026-10-16', '2026-10-14')],
        '无日期': []})
    def titles(filters):
        return {r['title'] for r in store.query_items(dict(filters,as_of='2026-10-14'))['records']}
    assert titles({'freshness':'expired'}) == {'已过期'}
    assert titles({'freshness':'not_expired'}) == {'今天截止','未来截止','缺少年份','日期范围'}
    assert titles({'freshness':'unknown'}) == {'无日期'}
    assert titles({'date_type':'deadline','date_status':'known'}) == {'已过期','今天截止','未来截止','缺少年份','日期范围'}
    assert titles({'date_type':'deadline','date_status':'unknown'}) == {'无日期'}
    assert titles({'date_type':'event','date_status':'known'}) == set()  # The reviewed claims are deadlines.
    assert titles({'date_type':'deadline','date_from':'2026-10-14','date_to':'2026-10-14'}) == {'今天截止','缺少年份','日期范围'}
    assert titles({'date_type':'deadline','date_status':'unknown','date_from':'2026-10-20'}) == set()
    assert titles({'issue_date_from':'2026-10-14'}) == set()  # Publication date is not a deadline.


def test_explicit_event_fallback_and_deadline_precedence(environment):
    details = '''<ul><li><h3 id="one">纯活动</h3>活动时间：2026年10月14日 09:00</li>
        <li><h3 id="two">有截止</h3>截止时间：2026年10月13日<br>活动时间：2026年10月16日</li>
        <li><h3 id="three">未知活动</h3>活动时间：10月15日</li></ul>'''
    store.import_document(issue(environment, 'event.html', rows='', details=details))
    apply_test_reviews({
        '纯活动': [reviewed_claim('event', '2026年10月14日', '2026-10-14')],
        '有截止': [reviewed_claim('deadline', '2026年10月13日', '2026-10-13'),
                   reviewed_claim('event', '2026年10月16日', '2026-10-16')],
        '未知活动': [reviewed_claim('event', '10月15日', '2026-10-15', 'contextual')]})
    expired = store.query_items({'freshness':'expired','as_of':'2026-10-14'})
    upcoming = store.query_items({'freshness':'not_expired','as_of':'2026-10-14'})
    unknown = store.query_items({'freshness':'unknown','as_of':'2026-10-14'})
    assert [r['title'] for r in expired['records']] == ['有截止']
    assert {r['title'] for r in upcoming['records']} == {'纯活动','未知活动'}
    assert unknown['records'] == []


def test_literal_chinese_keyword_and_filter_bounds(environment):
    details = '''<ul><li><h3>专题培训</h3>面向历史学院学生；实际关键词：百分%_字符。</li></ul>'''
    store.import_document(issue(environment,'filters.html',rows=row('普通活动'),details=details,category='院级活动'))
    assert store.query_items({'q':'培训'})['total'] == 1
    assert store.query_items({'q':'%_'})['total'] == 1
    assert store.query_items({'q':'不存在%_'})['total'] == 0
    college = store.query_items({'college':['历史学院'],'category':'院级活动'})
    assert college['total'] == 1
    assert college['records'][0]['college_mentions'][0]['relation'] == 'audience'
    for filters in ({'date_from':'2026-02-30'},{'date_status':'upcoming'},{'date_type':'publication'},
                    {'q':'字'*201},{'date_from':'2026-10-15','date_to':'2026-10-14'}):
        with pytest.raises(ValueError):
            store.query_items(filters)
    with pytest.raises(ValueError):
        store.query_items({},per_page=101)
    with pytest.raises(ValueError):
        store.query_items({},page=0)
    paged = store.query_items({},per_page=1,page=2)
    assert paged['total']==2 and paged['pages']==2 and len(paged['records'])==1


def test_missing_header_date_and_empty_issues_are_visible(environment):
    path = environment / '2026-10-13.html'
    path.write_text('<html><body><h1>南哪消息 10月13日</h1><p>没有活动</p></body></html>',encoding='utf-8')
    result = store.import_document(path)
    assert result['created'] and result['item_count']==0
    assert store.list_issues()[0]['issue_date'] is None
    assert store.list_issues()[0]['document_count']==1
    assert store.get_document(result['document_id'])['issue_date'] is None
    assert store.query_items({})['total']==0


def test_malformed_input_persists_failure_without_partial_rows(environment):
    path = environment / 'bad.html'
    path.write_bytes(b'<html>\xff</html>')
    result = store.import_document(path)
    assert result['status']=='failed'
    assert sql('SELECT status,error_code FROM bulletin_imports') == [('failed','invalid_html')]
    assert sql('SELECT * FROM bulletin_documents') == []
    assert sql('SELECT * FROM bulletin_issues') == []
    assert list((runtime.settings.upload_dir/'bulletins').iterdir()) == []


def test_sqlite_transaction_failure_rolls_back_domain_and_staging(environment):
    with closing(runtime.connect_db()) as db,db:
        db.execute('''CREATE TRIGGER reject_activity BEFORE INSERT ON bulletin_occurrences
            WHEN NEW.title='拒绝活动' BEGIN SELECT RAISE(ABORT,'test transaction failure'); END''')
    result = store.import_document(issue(environment,'failed.html',rows=row('普通活动')+row('拒绝活动')))
    assert result['status']=='failed'
    for table in ('bulletin_documents','bulletin_issues','bulletin_items','bulletin_occurrences','bulletin_dates'):
        assert sql('SELECT * FROM '+table)==[]
    assert sql('SELECT status FROM bulletin_imports') == [('failed',)]
    assert list((runtime.settings.upload_dir/'bulletins').iterdir()) == []


def test_missing_saved_evidence_never_claims_cached_success(environment):
    path = issue(environment,'one.html')
    result = store.import_document(path)
    document = store.get_document(result['document_id'])
    (runtime.settings.upload_dir/document['original_path']).unlink()
    repeated = store.import_document(path)
    assert repeated['status']=='failed'
    assert len(sql('SELECT * FROM bulletin_documents'))==1
    assert sql('SELECT status FROM bulletin_imports ORDER BY id') == [('created',),('failed',)]


def fake_pdf(path, workdir, *, parser_binary=None):
    raw = Path(path).read_bytes()
    (Path(workdir)/'rawparser.json').write_text('{"pages":[]}',encoding='utf-8')
    return {'schema_version':1,'format':'pdf','source_sha256':hashlib.sha256(raw).hexdigest(),
            'source_filename':Path(path).name,'title':'南哪消息 2026-10-13 第451期',
            'issue_date':'2026-10-13','issue_date_evidence':'南哪消息 2026-10-13 第451期','issue_number':451,
            'parser':{'name':'offline-boundary','version':'fixture'},'text':'原始正文','items':[],
            'pages':[{'page':1,'width':100,'height':200,'text':'原始正文'}],
            'assets':[],'warnings':[],'raw_artifact_path':'rawparser.json'}


def test_pdf_engine_boundary_and_raw_artifact_lifetime(environment,monkeypatch):
    from nannanews import pdf_adapter
    monkeypatch.setattr(pdf_adapter,'parse_pdf',fake_pdf)
    path = environment/'one.pdf'
    path.write_bytes(b'%PDF-original-boundary')
    result = store.import_document(path)
    assert result['created']
    document = store.get_document(result['document_id'])
    assert document['issue_number']==451
    assert document['source_filename']=='one.pdf'
    assert (runtime.settings.upload_dir/document['original_path']).read_bytes()==path.read_bytes()
    assert json.loads((runtime.settings.upload_dir/document['raw_artifact_path']).read_text())=={'pages':[]}
    assert not any(p.name.startswith('.import-') for p in (runtime.settings.upload_dir/'bulletins').iterdir())
    (runtime.settings.upload_dir/document['raw_artifact_path']).unlink()
    assert store.import_document(path)['status']=='failed'


@pytest.mark.parametrize('asset_path', ['../escape.png','/tmp/escape.png'])
def test_pdf_asset_path_escape_rejects_atomic_import(environment,monkeypatch,asset_path):
    from nannanews import pdf_adapter
    def unsafe(path,workdir,*,parser_binary=None):
        parsed = fake_pdf(path,workdir,parser_binary=parser_binary)
        parsed['assets'] = [{'id':'bad','path':asset_path,'sha256':'0'*64,'mime':'image/png'}]
        return parsed
    monkeypatch.setattr(pdf_adapter,'parse_pdf',unsafe)
    path = environment/'unsafe.pdf'
    path.write_bytes(b'%PDF-boundary')
    result = store.import_document(path)
    assert result['status']=='failed'
    assert sql('SELECT * FROM bulletin_documents')==[]
    assert list((runtime.settings.upload_dir/'bulletins').iterdir())==[]


def test_html_local_asset_private_copy_hash_and_corruption_detection(environment):
    poster = environment/'poster.png'
    poster.write_bytes(b'actual asset bytes')
    details = '<ul><li><h3>海报</h3>原文海报<img src="poster.png"></li></ul>'
    path = issue(environment,'assets.html',details=details)
    imported = store.import_document(path)
    document = store.get_document(imported['document_id'])
    asset = document['assets'][0]
    stored = runtime.settings.upload_dir/asset['private_path']
    assert stored.read_bytes()==poster.read_bytes()
    assert stored.stat().st_mode & 0o777 == 0o600
    stored.write_bytes(b'corrupted')
    assert store.import_document(path)['status']=='failed'


def test_archive_uses_only_indexed_dated_html_with_verified_hashes(environment):
    root = environment/'archive'
    site = root/'site'
    site.mkdir(parents=True)
    first = issue(site,'2026-10-12.html','2026-10-12')
    second = issue(site,'2026-10-13.html','2026-10-13',rows=row('第二活动'))
    (site/'latest.html').write_bytes(second.read_bytes())
    (site/'legacy.pdf').write_bytes(b'%PDF-not-an-authorized-input')
    records,files = [],{}
    for path in (first,second):
        url = 'https://nik-nul.github.io/news/'+path.name
        representation = {'url':url,'relative_path':'site/'+path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'outcome':'success'}
        records.append({'date':path.stem,'representations':{'html':[representation],'pdf':[]}})
        files[url] = dict(representation,publication_tree={'matches':True})
    (root/'issue-index.json').write_text(json.dumps({'dated_issues':records,'latest_aliases':['latest.html']}),encoding='utf-8')
    (root/'manifest.json').write_text(json.dumps({'files':files}),encoding='utf-8')
    result = store.import_archive(root)
    assert result['inputs']==result['created']==2 and result['failed']==0
    assert len(sql('SELECT * FROM bulletin_documents'))==2
    document = store.get_document(result['results'][0]['document_id'])
    assert document['claimed_issue_date']=='2026-10-12'
    assert document['provenance'][0]['manifest']['publication_tree']['matches'] is True
    assert (runtime.settings.upload_dir/document['original_path']).parent.joinpath('provenance.json').is_file()
    repeated = store.import_archive(root)
    assert repeated['created']==0 and repeated['already_imported']==2
    assert len(sql('SELECT * FROM bulletin_occurrences'))==2
    first.write_text('corrupted',encoding='utf-8')
    failed = store.import_archive(root)
    assert failed['failed']==1 and failed['already_imported']==1
    assert failed['results'][0]['error']['code']=='archive_input_invalid'


def test_archive_claim_is_not_publication_fallback(environment):
    root = environment/'archive'
    site = root/'site'
    site.mkdir(parents=True)
    path = issue(site,'2026-10-13.html',issue_date='10月13日')
    url = 'https://nik-nul.github.io/news/'+path.name
    rep = {'url':url,'relative_path':'site/'+path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'outcome':'success'}
    (root/'issue-index.json').write_text(json.dumps({'dated_issues':[{'date':'2026-10-13','representations':{'html':[rep]}}]}),encoding='utf-8')
    (root/'manifest.json').write_text(json.dumps({'files':{url:rep}}),encoding='utf-8')
    imported = store.import_archive(root)['results'][0]
    assert imported['created']
    assert any(w['code']=='claimed_issue_date_mismatch' for w in imported['warnings'])
    document = store.get_document(imported['document_id'])
    assert document['issue_date'] is None and document['claimed_issue_date']=='2026-10-13'
    assert store.query_items({'issue_date_from':'2026-10-13'})['total']==0
