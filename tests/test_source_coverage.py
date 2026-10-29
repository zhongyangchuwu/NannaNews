"""Coverage over real migrated metadata; no source fetches, model calls or payload scans."""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3

import pytest

from nannanews import cli, runtime, schema
from nannanews.source_coverage import source_coverage


ES = 'https://es.nju.edu.cn/e8/99/c22458a845977/page.htm'
MOBILE_ES = ES.replace('/page.htm', '/pagem.htm')
STAMP = '2026-10-18T10:00:00+08:00'


@pytest.fixture
def database(tmp_path, monkeypatch):
    private = tmp_path / 'private'
    monkeypatch.setattr(runtime, 'settings', replace(
        runtime.settings, data_dir=private, upload_dir=private / 'uploads',
        database_path=private / 'database.db', model_base_url='', model_api_key='',
    ))
    schema.migrate()
    with closing(sqlite3.connect(runtime.settings.database_path)) as db:
        yield db


def issue(db, number, *, identity=None, date='2026-10-18'):
    identity = identity if identity is not None else number
    issue_id = db.execute('''INSERT INTO bulletin_issues(identity_key,title,issue_date,issue_number)
        VALUES(?,?,?,?)''', ('issue:' + str(identity), 'Retained bulletin', date, number)).lastrowid
    return db.execute('''INSERT INTO bulletin_documents(issue_id,source_sha256,format,source_filename,
        issue_date_evidence,parser_json,parsed_json,text,original_path,normalized_path,imported_at,actor)
        VALUES(?,?,'pdf','original.pdf','printed date','{}','{}','private bulletin',
               'not-opened.pdf','not-opened.json',?,'operator:import')''',
        (issue_id, str(identity), STAMP)).lastrowid


def occurrence(db, document_id, url, *, item_id=None):
    if item_id is None:
        item_id = db.execute('INSERT INTO bulletin_items(identity_key,identity_basis) VALUES(?,?)',
                             ('item:' + str(db.total_changes), 'source')).lastrowid
    occurrence_id = db.execute('''INSERT INTO bulletin_occurrences(item_id,document_id,local_key,kind,
        title,body_text,locator_json,image_refs_json,raw_item_json)
        VALUES(?,?,?,'detail','Activity','Private bulletin body','{}','[]','{}')''',
        (item_id, document_id, 'local:' + str(db.total_changes))).lastrowid
    if url is not None:
        db.execute('INSERT INTO bulletin_sources VALUES(?,?)', (occurrence_id, url))
    return occurrence_id, item_id


def raw_head(db, url, actor, *, model_status='not_extracted', current=True):
    document_id = db.execute('''INSERT INTO source_documents(source_url,digest,document_json,title,
        content_text,fetched_at,captured_by,model_status) VALUES(?,?,'not JSON: never read',
        'Original article','Private raw body',?,?,?)''',
        (url, str(db.total_changes), STAMP, actor, model_status)).lastrowid
    if current:
        db.execute('INSERT OR REPLACE INTO source_document_heads VALUES(?,?)', (url, document_id))
    return document_id


def entry(db, document_id):
    entry_id = db.execute('''INSERT INTO entries(uploader,upload_time,title,status)
        VALUES('operator',?,'Draft activity','described')''', (STAMP,)).lastrowid
    db.execute('UPDATE information_records SET document_id=? WHERE entry_id=?', (document_id, entry_id))
    return entry_id


def queued(db, url, *, actor=None, mode='new', first_seen=STAMP, run_stamp=STAMP,
           source_run=True, document_id=None):
    run_id = None
    if actor is not None:
        run_id = db.execute('''INSERT INTO daily_runs(actor,started_at,finished_at,status,mode)
            VALUES(?,?,?,'complete',?)''', (actor, run_stamp, run_stamp, mode)).lastrowid
        if source_run:
            db.execute('INSERT INTO daily_source_runs VALUES(?,?,?)', (run_id, 'test-source', '{}'))
    return db.execute('''INSERT INTO daily_links(source_id,url,state,eligible,first_seen,last_seen,
        attempt_run,document_id) VALUES('test-source',?,'pending',1,?,?,?,?)''',
        (url, first_seen, first_seen, run_id, document_id)).lastrowid


def test_es_aliases_match_queue_heads_and_entries_without_changing_urls(database):
    document = issue(database, 100)
    occurrence(database, document, MOBILE_ES)
    occurrence(database, document, ES)
    original_id = raw_head(database, ES, 'system:daily', model_status='completed')
    entry_id = entry(database, original_id)
    queued(database, ES, actor='system:daily', document_id=original_id)
    report = source_coverage(database)
    assert report['denominators']['article_urls'] == 1
    assert report['denominators']['effective_source_urls'] == 2
    article = report['articles'][0]
    assert article['source_urls'] == sorted([ES, MOBILE_ES])
    assert article['discovery'] == 'independent_daily'
    assert article['queued'] and article['raw_saved'] and article['extracted'] and article['has_entries']
    assert article['entry_ids'] == [entry_id]
    assert database.execute('SELECT source_url FROM source_documents').fetchone()[0] == ES
    assert {row[0] for row in database.execute('SELECT source_url FROM bulletin_sources')} == {ES, MOBILE_ES}


@pytest.mark.parametrize('other', [
    ES.replace('es.nju.edu.cn', 'life.nju.edu.cn'),
    ES.replace('c22458a845977', 'c22458a845978'),
    ES.replace('/e8/99/', '/aa/bb/'),
    ES.replace('https:', 'http:'),
    ES + '?version=1',
    ES + '?',
    ES + '#',
])
def test_alias_comparison_never_merges_other_publisher_path_scheme_or_query(database, other):
    document = issue(database, 100)
    occurrence(database, document, MOBILE_ES)
    occurrence(database, document, other)
    report = source_coverage(database)
    assert report['denominators']['article_urls'] == 2


def test_other_publishers_mobile_names_are_not_aliases(database):
    document = issue(database, 100)
    url = ES.replace('es.nju.edu.cn', 'life.nju.edu.cn')
    occurrence(database, document, url)
    occurrence(database, document, url.replace('/page.htm', '/pagem.htm'))
    assert source_coverage(database)['denominators']['article_urls'] == 2


def test_one_article_multiple_occurrences_and_activities_counts_one_article(database):
    document = issue(database, 100)
    _, first_item = occurrence(database, document, ES)
    occurrence(database, document, ES, item_id=first_item)
    occurrence(database, document, ES)
    occurrence(database, document, None)
    queued(database, ES)
    report = source_coverage(database)
    assert report['denominators'] == {
        'article_urls': 1, 'effective_source_urls': 1, 'bulletin_occurrences': 4,
        'bulletin_activities': 3, 'occurrences_with_source': 3, 'occurrences_without_source': 1,
    }
    assert report['articles'][0]['occurrence_count'] == 3
    assert report['articles'][0]['activity_count'] == 2
    assert report['article_counts']['queued'] == 1
    assert report['article_fractions']['queued'] == {'numerator': 1, 'denominator': 1, 'fraction': 1.0}
    assert any('not activity recall' in text for text in report['limitations'])


@pytest.mark.parametrize('actor', [
    'system:pdf-coverage-20261018', 'system:pdf-network-recovery-20261018', 'audit:pdf-2026-10-14',
])
def test_pdf_raw_only_is_not_independent_discovery(database, actor):
    occurrence(database, issue(database, 100), ES)
    raw_head(database, ES, actor)
    report = source_coverage(database)
    assert report['article_counts']['pdf_only'] == 1
    assert report['article_counts']['independent_daily'] == 0
    assert report['articles'][0]['raw_saved']
    assert not report['articles'][0]['queued']


def test_independent_listing_evidence_survives_pdf_first_capture(database):
    occurrence(database, issue(database, 100), ES)
    original_id = raw_head(database, ES, 'system:pdf-coverage-20261018')
    queued(database, MOBILE_ES, actor='system:daily', document_id=original_id)
    article = source_coverage(database)['articles'][0]
    assert article['discovery'] == 'independent_daily'
    assert {row['kind'] for row in article['discovery_evidence']} == {'pdf_backfill', 'independent_daily'}


@pytest.mark.parametrize('actor', [
    'system:source-repair-20261018', 'audit:morning-repair-20261016',
    'audit:priority-repair-20261016', 'audit:source-expansion-22dfbd60', 'han',
    'system:pdf-coverage-20261018-extra', 'audit:pdf-2026-10-14-extra',
])
def test_other_actor_evidence_is_not_silently_daily_or_pdf(database, actor):
    occurrence(database, issue(database, 100), ES)
    raw_head(database, ES, actor)
    article = source_coverage(database)['articles'][0]
    assert article['discovery'] == 'other_only'
    assert article['discovery_evidence'][0]['kind'] == 'other'


@pytest.mark.parametrize('source_run,first_seen', [(False, STAMP), (True, '2026-10-17T10:00:00+08:00')])
def test_latest_daily_attempt_does_not_prove_initial_discovery(database, source_run, first_seen):
    occurrence(database, issue(database, 100), ES)
    raw_head(database, ES, 'system:daily')
    queued(database, ES, actor='system:daily', source_run=source_run, first_seen=first_seen)
    report = source_coverage(database)
    assert report['articles'][0]['discovery'] == 'unknown'
    assert report['article_counts']['independent_daily'] == 0


@pytest.mark.parametrize('mode', ['new', 'history', 'legacy'])
def test_independent_run_modes_are_retained_not_collapsed_into_current_collection(database, mode):
    occurrence(database, issue(database, 100), ES)
    queued(database, ES, actor='system:daily', mode=mode)
    article = source_coverage(database)['articles'][0]
    assert article['discovery'] == 'independent_daily'
    assert article['discovery_evidence'][0]['mode'] == mode


def test_current_heads_and_entries_are_separate_stages(database):
    occurrence(database, issue(database, 100), ES)
    old = raw_head(database, ES, 'system:daily', model_status='completed')
    entry(database, old)
    current = raw_head(database, ES, 'han', model_status='completed')
    article = source_coverage(database)['articles'][0]
    assert [head['document_id'] for head in article['raw_heads']] == [current]
    assert article['extracted']
    assert not article['has_entries']
    assert article['entry_ids'] == []


def test_latest_issue_and_numeric_inclusive_bounds(database):
    occurrence(database, issue(database, 9, date='2026-10-19'), 'https://example.org/nine')
    occurrence(database, issue(database, 10, date='2026-10-01'), ES)
    occurrence(database, issue(database, None, identity='unnumbered'), 'https://example.org/unknown')
    latest = source_coverage(database)
    assert latest['scope']['issue_numbers'] == [10]
    assert latest['scope']['issue_from'] == latest['scope']['issue_to'] == 10
    bounded = source_coverage(database, issue_from=9, issue_to=10)
    assert bounded['denominators']['article_urls'] == 2
    assert bounded['scope']['issue_numbers'] == [9, 10]
    assert source_coverage(database, issue_to=9)['scope']['issue_numbers'] == [9]
    assert source_coverage(database, issue_from=10)['scope']['issue_numbers'] == [10]
    empty = source_coverage(database, issue_from=11, issue_to=12)
    assert empty['articles'] == []
    assert empty['article_fractions']['queued']['fraction'] is None


def test_empty_database_has_no_invented_denominator(database):
    report = source_coverage(database)
    assert report['scope']['issue_numbers'] == []
    assert report['scope']['issue_from'] is None
    assert report['denominators']['article_urls'] == 0
    assert report['articles'] == []
    assert all(value == 0 for value in report['article_counts'].values())


@pytest.mark.parametrize('bounds', [
    {'issue_from': 0}, {'issue_to': -1}, {'issue_from': '10'},
    {'issue_to': True}, {'issue_from': 11, 'issue_to': 10},
])
def test_invalid_bounds_are_rejected(database, bounds):
    with pytest.raises(ValueError):
        source_coverage(database, **bounds)


def test_effective_source_replacement_not_obsolete_original_is_reported(database):
    occurrence_id, _ = occurrence(database, issue(database, 100), 'https://example.org/wrong')
    database.execute('''INSERT INTO bulletin_source_revisions(occurrence_id,original_url,corrected_url,
        input_sha256,revision_sha256,origin,actor,reason,review_json,created_at)
        VALUES(?,? ,?,'input','revision','operator_review','operator','reviewed correction','{}',?)''',
        (occurrence_id, 'https://example.org/wrong', ES, STAMP))
    assert source_coverage(database)['articles'][0]['source_urls'] == [ES]


def test_report_uses_only_read_metadata_on_caller_connection(database):
    occurrence(database, issue(database, 100), ES)
    original_id = raw_head(database, ES, 'system:daily')
    queued(database, ES, actor='system:daily', document_id=original_id)
    database.commit()
    with closing(sqlite3.connect(runtime.settings.database_path.as_uri() + '?mode=ro', uri=True)) as db:
        denied_columns = {'document_json', 'content_text', 'raw_item_json', 'parsed_json', 'parser_json',
                          'result_json', 'response_json', 'output_json', 'draft_json', 'source_review_json'}
        def authorize(action, first, second, database_name, trigger):
            if action == sqlite3.SQLITE_READ and second in denied_columns:
                return sqlite3.SQLITE_DENY
            if action not in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorize)
        before = (db.row_factory, db.in_transaction, db.total_changes)
        report = source_coverage(db)
        assert report['article_counts']['independent_daily'] == 1
        assert (db.row_factory, db.in_transaction, db.total_changes) == before


def test_component_rejects_uninitialized_schema_without_ddl():
    with closing(sqlite3.connect(':memory:')) as db:
        with pytest.raises(runtime.ConfigurationError, match='nannanews migrate'):
            source_coverage(db)
        assert db.execute('SELECT count(*) FROM sqlite_master').fetchone()[0] == 0


def test_cli_json_uses_current_schema_and_numeric_issue_bounds(database, capsys, monkeypatch):
    occurrence(database, issue(database, 100), ES)
    database.commit()
    def no_migration(*args, **kwargs):
        pytest.fail('A read command must not migrate or prepare runtime state')
    monkeypatch.setattr(schema, 'migrate', no_migration)
    monkeypatch.setattr(runtime, 'prepare_runtime', no_migration)
    assert cli.main(['source-coverage', '--issue-from', '100', '--issue-to', '100']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['scope']['issue_numbers'] == [100]
    assert report['article_counts']['not_observed'] == 1


def test_cli_help_and_missing_schema_do_not_create_runtime(tmp_path, monkeypatch, capsys):
    private = tmp_path / 'unprepared'
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, data_dir=private,
                       upload_dir=private / 'uploads', database_path=private / 'database.db'))
    with pytest.raises(SystemExit) as help_exit:
        cli.main(['source-coverage', '--help'])
    assert help_exit.value.code == 0
    capsys.readouterr()
    assert not private.exists()
    assert cli.main(['source-coverage']) == 2
    assert 'nannanews migrate' in capsys.readouterr().err
    assert not private.exists()
