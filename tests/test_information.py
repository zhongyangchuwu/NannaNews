"""Canonical-store boundary tests; only the paid model transport is substituted."""
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import tempfile
import unittest
from unittest.mock import patch

from werkzeug.datastructures import MultiDict


from nannanews import acquisition
from nannanews import information
from nannanews import runtime, schema

TEXT = '信息管理学院举办公开讲座，活动日期为2026年10月20日，报名截止为2026年10月18日，面向计算机学院学生。正文深处包含量子信息和字符串100%_。'


def document(text=TEXT, url='https://example.org/article?a=1'):
    return {'canonical_url': url, 'title': '公开通知', 'text': text, 'images': [],
            'raw_content': '<article>'+text+'</article>', 'raw_date_text': '2026-10-12',
            'source_type': 'public_html', 'fetched_at': '2026-10-13T00:00:00+00:00'}


def candidate():
    return {'title': '公开讲座', 'description': '真实模型描述', 'tag': '院级活动',
            'event_date': '2026-10-20', 'deadline_date': '2026-10-18',
            'event_date_resolution': 'explicit', 'deadline_date_resolution': 'explicit',
            'colleges': [{'name': '信息管理学院', 'relation': 'associated'},
                         {'name': '计算机学院', 'relation': 'audience'}]}


def settings_for(root):
    return runtime.Settings(
        data_dir=root, database_path=root / 'database.db', upload_dir=root / 'uploads',
        publication_dir=root / 'publications', archive_dir=root / 'archives',
        lecture_archive_dir=root / 'lecture_archives', session_secret='fixture-only-secret',
        model_base_url='https://model.example/v1', model_api_key='secret-test-key',
        model_name='gpt-6-luna', model_timeout=20, model_api_style='responses',
    )


def create_legacy_acquisition_tables(conn):
    conn.execute('CREATE TABLE entries (' + ', '.join('"' + name + '" ' + ddl for name, ddl in runtime.ENTRY_COLUMNS) + ')')
    conn.executescript('''CREATE TABLE acquisitions (
        id INTEGER PRIMARY KEY, source_url TEXT NOT NULL, digest TEXT NOT NULL,
        document_json TEXT NOT NULL, model_name TEXT NOT NULL, acquired_by TEXT NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(source_url,digest));
        CREATE TABLE acquisition_candidates (
        acquisition_id INTEGER NOT NULL REFERENCES acquisitions(id), event_key TEXT NOT NULL,
        entry_id INTEGER NOT NULL UNIQUE REFERENCES entries(id), draft_json TEXT NOT NULL,
        PRIMARY KEY(acquisition_id,event_key));''')


class InformationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.settings = settings_for(root)
        settings_patch = patch.object(runtime, 'settings', self.settings)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        schema.migrate()
        with closing(runtime.connect_db()) as conn, conn:
            conn.execute("INSERT INTO users VALUES(1,'private-user','private-password','admin')")
        information.register_source({'id':'official','name':'正式来源','url':'https://example.org/list',
            'publisher_college':'新闻传播学院','enabled':True,'status':'verified'})
        body = {'status':'completed','error':None,'incomplete_details':None,
                'output':[{'type':'message','role':'assistant','status':'completed',
                           'content':[{'type':'output_text','text':json.dumps({'items':[candidate()]},ensure_ascii=False)}]}]}
        model_patch = patch.object(acquisition, '_model_post', return_value=body)
        self.model = model_patch.start()
        self.addCleanup(model_patch.stop)

    @contextmanager
    def alternate_runtime(self, name):
        root = Path(self.directory.name) / name
        with patch.object(runtime, 'settings', settings_for(root)):
            runtime.prepare_runtime()
            yield runtime.settings.database_path

    def enrich(self, doc=None):
        saved = information.save_document(doc or document(), 'official', 'editor')
        result = information.enrich_document(saved['id'], 'editor')
        return saved, result

    def approve(self, entry_id, publication=None):
        publication = publication or (datetime.now(timezone.utc)-timedelta(days=1)).date().isoformat()
        with closing(runtime.connect_db()) as conn, conn:
            conn.execute("UPDATE entries SET status='approved',publish_date=? WHERE id=?",(publication,entry_id))

    def test_joined_dates_source_and_distinct_college_relations(self):
        saved, result = self.enrich()
        row = information.get_record(result['record_ids'][0])
        self.assertEqual((row['source_published_date'],row['event_date'],row['deadline_date']),
                         ('2026-10-12','2026-10-20','2026-10-18'))
        self.assertEqual(row['event_date_resolution'], 'explicit')
        self.assertEqual(row['deadline_date_resolution'], 'explicit')
        self.assertEqual(row['source_id'],'official')
        self.assertEqual(row['source_name'],'正式来源')
        self.assertEqual({(value['name'],value['relation']) for value in row['colleges']},
                         {('新闻传播学院','publisher'),('信息管理学院','associated'),('计算机学院','audience')})
        self.assertEqual(row['content_text'],TEXT)
        self.assertEqual(row['document_id'],saved['id'])

        with closing(runtime.connect_db()) as conn:
            self.assertEqual(conn.execute('SELECT due_time FROM entries').fetchone()[0],'2026-10-18')


    def test_minimal_item_with_colleges_is_private_queryable_and_keeps_early_date(self):
        draft = {'title': '模型提供的活动标题', 'short_title': '',
                 'description': '模型提供的完整活动描述。', 'tag': '院级活动',
                 'event_date': '1969-07-20', 'event_date_resolution': None,
                 'event_date_basis': None, 'deadline_date': None,
                 'deadline_date_resolution': None, 'deadline_date_basis': None,
                 'date_uncertainty': None, 'evidence_quotes': None,
                 'colleges': [{'name': '信息管理学院', 'relation': 'associated', 'evidence': None},
                              {'name': '计算机学院', 'relation': 'audience'}]}
        self.model.return_value = {'status':'completed','error':None,'incomplete_details':None,
            'output':[{'type':'message','role':'assistant','status':'completed',
                       'content':[{'type':'output_text','text':json.dumps({'items':[draft]},ensure_ascii=False)}]}]}
        saved, result = self.enrich(document('来源正文不包含模型输出中的活动事实。'))
        self.assertEqual(result['model_status'], 'completed')
        self.assertEqual(len(result['entry_ids']), 1)
        rows = information.query_records({'college':'计算机学院','college_relation':'audience',
            'date_type':'event','date_from':'1969-07-20','date_to':'1969-07-20'})['records']
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['title'], row['description'], row['event_date']),
                         ('模型提供的活动标题', '模型提供的完整活动描述。', '1969-07-20'))
        self.assertEqual(row['verification_status'], 'described')
        self.assertEqual({(value['name'], value['relation']) for value in row['colleges']},
                         {('新闻传播学院','publisher'), ('信息管理学院','associated'),
                          ('计算机学院','audience')})
        with closing(runtime.connect_db()) as conn:
            stored = conn.execute('SELECT status,publish_date,title,short_title FROM entries WHERE id=?',
                                  (result['entry_ids'][0],)).fetchone()
        self.assertEqual(stored, ('described', None, '模型提供的活动标题', '模型提供的活动标题'))
        self.assertEqual(information.query_records({},scope='public')['total'], 0)
        self.assertEqual(information.export_records({},scope='public'), [])

    def test_supplied_date_resolution_is_stored_without_evidence_basis(self):
        text = '2025年秋季活动安排。活动日期为11月27日。'
        item = candidate()
        item.update(event_date='2025-11-27', deadline_date=None,
                    event_date_resolution='contextual', deadline_date_resolution='unknown')
        self.model.return_value = {'status':'completed','error':None,'incomplete_details':None,
            'output':[{'type':'message','role':'assistant','status':'completed',
                       'content':[{'type':'output_text','text':json.dumps({'items':[item]},ensure_ascii=False)}]}]}
        saved, result = self.enrich(document(text))
        row = information.get_record(result['record_ids'][0])
        self.assertEqual(row['event_date_resolution'], 'contextual')
        self.assertEqual(row['event_date_basis'], '')
        self.assertEqual(row['deadline_date_basis'], '')
        self.assertIsNone(row['deadline_date'])
        with closing(runtime.connect_db()) as conn:
            stored = json.loads(conn.execute('SELECT draft_json FROM acquisition_candidates WHERE entry_id=?',
                                             (result['entry_ids'][0],)).fetchone()[0])
        self.assertEqual(stored['event_date_resolution'], 'contextual')
        self.assertEqual(information.get_document(saved['id'])['source_published_date'], '2026-10-12')


    def test_chinese_short_substrings_quotes_and_sql_wildcards_are_literal(self):
        self.enrich()
        for phrase in ('量子','信息','100%_'):
            self.assertEqual(information.query_records({'q':phrase})['total'],1)
        for phrase in ("' OR 1=1 --",'不存在','100%z'):
            self.assertEqual(information.query_records({'q':phrase})['total'],0)
        self.assertEqual(information.query_records({'q':'%'})['total'],1)

    def test_compound_source_date_relation_and_tag_filters(self):
        self.enrich()
        filters = MultiDict([('q','量子'),('source','official'),('source','other'),
            ('college','计算机学院'),('college_relation','audience'),('tag','院级活动'),
            ('date_type','deadline'),('date_from','2026-10-18'),('date_to','2026-10-18')])
        self.assertEqual(information.query_records(filters)['total'],1)
        for changed in ({'college_relation':'publisher'},{'date_type':'event'},{'sources':['unknown']}):
            normalized = dict(information.parse_filters(filters),**changed)
            self.assertEqual(information.query_records(normalized)['total'],0)

    def test_unknown_dates_never_become_fetched_or_current_dates(self):
        doc = document('仅有10月20日，缺少年份。')
        doc['raw_date_text'] = None
        saved = information.save_document(doc,None,'editor')
        row = information.query_records({})['records'][0]
        self.assertEqual(row['model_status'],'not_extracted')
        self.assertEqual(row['description_kind'],'original_text')
        self.assertEqual(row['description'],doc['text'])
        self.assertIsNone(row['source_published_date'])
        self.assertIsNone(row['event_date'])
        self.assertIsNone(row['deadline_date'])
        self.assertEqual(information.query_records({'date_from':'2026-10-01'})['total'],0)
        self.assertEqual(information.query_records({},scope='public')['total'],0)
        self.assertIsNone(information.get_record(row['id'],scope='public'))
        self.assertEqual(information.get_document(saved['id'])['raw_content'],doc['raw_content'])

    def test_failed_extraction_keeps_honest_raw_record_and_no_fake_entries(self):
        saved = information.save_document(document(),'official','editor')
        self.model.side_effect = acquisition.AcquisitionError('安全错误说明','model_http_error',502)
        with self.assertRaises(acquisition.AcquisitionError):
            information.enrich_document(saved['id'],'editor')
        rows = information.query_records({})['records']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['model_status'],'failed')
        self.assertIsNone(rows[0]['entry_id'])
        self.assertEqual(rows[0]['content_text'],TEXT)
        self.assertEqual(information.get_document(saved['id'])['model_error'],'model_http_error')
        self.assertNotIn('secret-test-key',json.dumps(rows))

    def test_structurally_invalid_items_keep_original_audit_and_create_no_drafts(self):
        missing_title = candidate()
        del missing_title['title']
        invalid_description = candidate()
        invalid_description['description'] = []
        invalid_tag = candidate()
        invalid_tag['tag'] = ['院级活动']
        invalid_calendar = candidate()
        invalid_calendar['event_date'] = '2026-02-30'
        invalid_image_reference = candidate()
        invalid_image_reference['evidence_quotes'] = [{'text':'说明文字','image_index':0}]
        cases = [('missing_title',missing_title,'model_output_invalid'),
                 ('invalid_description',invalid_description,'model_output_invalid'),
                 ('invalid_tag',invalid_tag,'model_output_invalid'),
                 ('invalid_calendar',invalid_calendar,'model_output_invalid'),
                 ('invalid_image_reference',invalid_image_reference,'model_output_invalid')]
        for index,(label,item,code) in enumerate(cases):
            with self.subTest(case=label):
                saved = information.save_document(document(url='https://example.org/'+label),'official','editor')
                with closing(runtime.connect_db()) as conn:
                    original = conn.execute('SELECT document_json FROM source_documents WHERE id=?',(saved['id'],)).fetchone()[0]
                output = {'items':[item]}
                self.model.return_value['output'][0]['content'][0]['text'] = json.dumps(output,ensure_ascii=False)
                with self.assertRaises(acquisition.AcquisitionError) as caught:
                    information.enrich_document(saved['id'],'editor')
                self.assertEqual(caught.exception.code,code)
                self.assertEqual(self.model.call_count,index+1)
                self.assertNotIn('真实模型描述',str(caught.exception))
                with closing(runtime.connect_db()) as conn:
                    row = conn.execute('''SELECT model_status,model_error,model_name,output_json,response_json
                        FROM source_document_model_attempts WHERE document_id=?''',(saved['id'],)).fetchone()
                    self.assertEqual(row[:3],('failed',code,'gpt-6-luna'))
                    self.assertEqual(json.loads(row[3]),output)
                    self.assertIsNone(row[4])
                    self.assertEqual(conn.execute('SELECT document_json FROM source_documents WHERE id=?',
                                                 (saved['id'],)).fetchone()[0],original)
                    for table in ('entries','acquisitions','acquisition_candidates','information_colleges'):
                        self.assertEqual(conn.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
                raw = information.get_document(saved['id'])
                self.assertEqual((raw['model_status'],raw['model_error']),('failed',code))
                self.assertEqual(raw['text'],TEXT)
                self.assertEqual(raw['raw_content'],document()['raw_content'])
                for field in ('model_output','model_response','model_name'):
                    self.assertNotIn(field,raw)
                record = next(row for row in information.query_records({'q':TEXT})['records']
                              if row['document_id']==saved['id'])
                self.assertIsNone(record['entry_id'])
                self.assertEqual(record['model_status'],'failed')
                self.assertEqual(record['content_text'],TEXT)
                self.assertEqual(information.export_records({},scope='public'),[])

    def test_failed_envelopes_are_retained_across_explicit_retry_but_never_exported(self):
        saved = information.save_document(document(),'official','editor')
        valid = deepcopy(self.model.return_value)
        malformed = deepcopy(valid)
        malformed['output'][0]['content'][0]['text'] = '{"items":['
        refused = deepcopy(valid)
        refused['output'][0]['content'] = [{'type':'refusal','refusal':'private-audit-refusal'}]
        incomplete = deepcopy(valid)
        incomplete['status'] = 'incomplete'
        invalid = [malformed,refused,incomplete]
        for index,body in enumerate(invalid):
            with self.subTest(response=body):
                self.model.return_value = body
                with self.assertRaises(acquisition.AcquisitionError) as caught:
                    information.enrich_document(saved['id'],'editor')
                self.assertEqual(caught.exception.code,'model_output_invalid')
                self.assertNotIn('private-audit-refusal',str(caught.exception))
                self.assertEqual(self.model.call_count,index+1)
                self.assertEqual(information.get_document(saved['id'])['model_status'],'failed')
                self.assertEqual(information.query_records({},scope='public')['total'],0)
        with closing(runtime.connect_db()) as conn:
            rows = conn.execute('''SELECT model_status,model_error,model_name,output_json,response_json
                FROM source_document_model_attempts WHERE document_id=? ORDER BY id''',(saved['id'],)).fetchall()
        self.assertEqual(len(rows),len(invalid))
        for row,body in zip(rows,invalid):
            self.assertEqual(row[:3],('failed','model_output_invalid','gpt-6-luna'))
            self.assertIsNone(row[3])
            self.assertEqual(json.loads(row[4]),body)

        # Only this new explicit call retries; reads and failures themselves never do.
        self.model.return_value = valid
        result = information.enrich_document(saved['id'],'editor')
        self.assertEqual(result['model_status'],'completed')
        self.assertEqual(self.model.call_count,len(invalid)+1)
        information.enrich_document(saved['id'],'editor')
        self.assertEqual(self.model.call_count,len(invalid)+1)
        with closing(runtime.connect_db()) as conn:
            history = conn.execute('''SELECT model_status,output_json,response_json
                FROM source_document_model_attempts WHERE document_id=? ORDER BY id''',(saved['id'],)).fetchall()
        self.assertEqual([row[0] for row in history],['failed']*len(invalid)+['completed'])
        self.assertEqual([json.loads(row[2]) for row in history[:-1]],invalid)
        self.assertEqual(json.loads(history[-1][1]),{'items':[candidate()]})
        self.approve(result['entry_ids'][0])
        for scope in ('private','public'):
            records = information.query_records({},scope=scope)['records']
            records += information.export_records({},scope=scope)
            records += [information.get_record(result['record_ids'][0],scope),
                        information.get_entry_record(result['entry_ids'][0],scope)]
            for record in records:
                for field in ('model_output','model_response','output_json','response_json','model_error'):
                    self.assertNotIn(field,record)
                self.assertNotIn('private-audit-refusal',json.dumps(record))
        self.assertNotIn('model_output',information.get_document(saved['id']))
        self.assertNotIn('model_response',information.get_document(saved['id']))

    def test_candidate_storage_failure_rolls_back_drafts_but_retains_answer_and_source(self):
        saved = information.save_document(document(),'official','editor')
        with closing(runtime.connect_db()) as conn, conn:
            original = conn.execute('SELECT document_json FROM source_documents WHERE id=?',(saved['id'],)).fetchone()[0]
            conn.execute('''CREATE TRIGGER reject_candidate_colleges BEFORE INSERT ON information_colleges BEGIN
                SELECT RAISE(ABORT,'candidate indexing failed'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            information.enrich_document(saved['id'],'editor')
        self.assertEqual(self.model.call_count,1)
        with closing(runtime.connect_db()) as conn:
            for table in ('entries','acquisitions','acquisition_candidates','information_colleges'):
                self.assertEqual(conn.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
            source = conn.execute('''SELECT document_json,model_status,model_error,acquisition_id
                FROM source_documents WHERE id=?''',(saved['id'],)).fetchone()
            self.assertEqual(source,(original,'failed','storage_failed',None))
            attempt = conn.execute('''SELECT model_status,model_error,output_json FROM source_document_model_attempts
                WHERE document_id=?''',(saved['id'],)).fetchone()
            self.assertEqual(attempt[:2],('failed','storage_failed'))
            self.assertEqual(json.loads(attempt[2]),{'items':[candidate()]})
        records = information.query_records({})['records']
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['model_status'],'failed')
        self.assertEqual(records[0]['content_text'],TEXT)
        self.assertIsNone(records[0]['entry_id'])
        self.assertIn('失败',records[0]['date_uncertainty'])
        self.assertEqual(information.export_records({},scope='public'),[])

    def test_completed_duplicate_never_calls_paid_model_or_shows_raw_card(self):
        saved, result = self.enrich()
        repeated = information.save_document(document(),'official','other-editor')
        self.assertFalse(repeated['created'])
        self.assertEqual(repeated['id'],saved['id'])
        second = information.enrich_document(saved['id'],'other-editor')
        self.assertFalse(second['created'])
        self.assertEqual(result['entry_ids'],second['entry_ids'])
        self.assertEqual(self.model.call_count,1)
        self.assertEqual(information.query_records({})['total'],1)
        self.assertIsNotNone(information.query_records({})['records'][0]['entry_id'])
        with closing(runtime.connect_db()) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM source_document_model_attempts').fetchone()[0],1)

    def test_updated_url_preserves_evidence_but_lists_current_version_only(self):
        first, result = self.enrich()
        changed = document(TEXT+' 更新：地点改为鼓楼。')
        second = information.save_document(changed,'official','editor')
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(information.get_document(first['id'])['text'],TEXT)
        rows = information.query_records({})['records']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['document_id'],second['id'])
        self.assertEqual(rows[0]['model_status'],'not_extracted')
        self.assertEqual(self.model.call_count,1)

    def test_query_parameter_identity_is_not_collapsed(self):
        first = information.save_document(document(),'official','editor')
        second = information.save_document(document(url='https://example.org/article?a=2'),'official','editor')
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(information.query_records({})['total'],2)

    def test_human_edits_and_delete_immediately_change_all_views(self):
        saved, result = self.enrich()
        entry_id, record_id = result['entry_ids'][0],result['record_ids'][0]
        self.approve(entry_id)
        with closing(runtime.connect_db()) as conn, conn:
            conn.execute("UPDATE entries SET title='人工更正',description='更正后的说明',tag='通知' WHERE id=?",(entry_id,))
        for scope in ('private','public'):
            row = information.get_record(record_id,scope)
            self.assertEqual((row['title'],row['description'],row['tag']),('人工更正','更正后的说明','通知'))
            self.assertEqual(information.get_entry_record(entry_id,scope)['id'],record_id)
        with closing(runtime.connect_db()) as conn, conn:
            conn.execute('DELETE FROM entries WHERE id=?',(entry_id,))
        self.assertIsNone(information.get_record(record_id))
        self.assertEqual(information.query_records({})['total'],0)
        self.assertEqual(information.enrich_document(saved['id'],'editor')['entry_ids'],[])
        self.assertEqual(self.model.call_count,1)
        self.assertIsNotNone(information.get_document(saved['id']))

    def test_public_boundary_excludes_pending_future_invalid_and_operational_fields(self):
        saved, result = self.enrich()
        entry_id = result['entry_ids'][0]
        for value in ('1970-01-01','2026-02-30','not-a-date','2020-01-01T25:00:00','2999-12-31'):
            self.approve(entry_id,value)
            self.assertEqual(information.query_records({},scope='public')['total'],0)
        self.approve(entry_id)
        public = information.query_records({},scope='public')['records'][0]
        for field in ('content_text','evidence_quotes','date_uncertainty','model_error','captured_by','raw_content','document_json'):
            self.assertNotIn(field,public)
        self.assertTrue(all('evidence' not in college for college in public['colleges']))
        with closing(runtime.connect_db()) as conn:
            columns = [column[1] for column in conn.execute('PRAGMA table_info(public_information_records_view)')]
        self.assertNotIn('password',columns)
        self.assertNotIn('content_text',columns)
        self.assertNotIn('captured_by',columns)
        with closing(runtime.connect_db()) as conn, conn:
            conn.execute("UPDATE entries SET status='described' WHERE id=?",(entry_id,))
        self.assertIsNone(information.get_entry_record(entry_id,scope='public'))
        self.assertEqual(information.filter_options('public'),{'sources':[],'colleges':[],'tags':[]})

    def test_repeated_migration_preserves_core_positions_and_live_edits(self):
        saved,result = self.enrich()
        with closing(runtime.connect_db()) as conn:
            before = conn.execute('SELECT * FROM entries').fetchone()
        schema.migrate()
        schema.migrate()
        with closing(runtime.connect_db()) as conn:
            self.assertEqual(conn.execute('SELECT * FROM entries').fetchone(),before)
            self.assertEqual([row[1] for row in conn.execute('PRAGMA table_info(entries)')], [name for name,_ in runtime.ENTRY_COLUMNS])
        self.assertEqual(information.query_records({})['total'],1)
        self.assertEqual(self.model.call_count,1)

    def test_historical_acquisition_backfill_keeps_ambiguous_due_time_unknown(self):
        original = document()
        original['digest'] = acquisition._digest(original)
        old = {'event_key':'historical-key','due_time':'2026-10-20','date_evidence':TEXT,
               'date_uncertainty':'','evidence_quotes':[{'text':TEXT,'image_index':None}]}
        with self.alternate_runtime('legacy-due-time') as database:
            with closing(sqlite3.connect(database)) as conn, conn:
                create_legacy_acquisition_tables(conn)
                entry_id = conn.execute('''INSERT INTO entries(uploader,upload_time,title,description,
                    describer,status,due_time) VALUES(?,?,?,?,?,?,?)''',
                    ('editor','2026-10-13','历史活动','历史正文','model:gpt-6-luna','described','2026-10-20')).lastrowid
                conn.execute('''INSERT INTO acquisitions(id,source_url,digest,document_json,model_name,acquired_by,created_at)
                    VALUES(1,?,?,?,?,?,?)''', (original['canonical_url'],original['digest'],json.dumps(original),
                    'gpt-6-luna','editor',original['fetched_at']))
                conn.execute('INSERT INTO acquisition_candidates VALUES(1,?,?,?)',
                             ('historical-key',entry_id,json.dumps(old)))
            schema.migrate()
            row = information.get_entry_record(entry_id)
            self.assertIsNone(row['event_date'])
            self.assertIsNone(row['deadline_date'])
            self.assertIn('历史 due_time',row['date_uncertainty'])
            self.assertEqual(row['source_published_date'],'2026-10-12')
            self.model.assert_not_called()

    def test_manual_insert_is_indexed_without_reinitialization(self):
        with closing(runtime.connect_db()) as conn, conn:
            entry_id = conn.execute("INSERT INTO entries(uploader,upload_time,title,description,status,due_time) VALUES('editor','2026-10-13','手工新闻','正文','described','2026-10-20')").lastrowid
        row = information.get_entry_record(entry_id)
        self.assertEqual(row['title'],'手工新闻')
        self.assertEqual(row['model_status'],'manual')
        self.assertIsNone(row['document_id'])
        self.assertIsNone(row['event_date'])

    def test_source_validation_and_publisher_are_not_inferred_from_topic(self):
        information.register_source({'id':'unverified','name':'历史账号名','url':'https://example.org/old',
            'publisher_college':'不能推断的学院','enabled':True,'status':'failed'})
        saved = information.save_document(document(url='https://example.org/old'),'unverified','editor')
        row = information.query_records({'source':'unverified'})['records'][0]
        self.assertEqual(row['colleges'],[])
        self.assertEqual([source['id'] for source in information.list_sources(True)],['official'])
        information.update_source('unverified',status='verified',enabled=True,last_checked_at='2026-10-13T00:00:00+00:00')
        self.assertIsNone(information.get_document(saved['id'])['publisher_college'])
        with self.assertRaises(ValueError):
            information.update_source('unverified',status='failed',last_checked_at=123)
        self.assertEqual(information.get_document(saved['id'])['source_status'],'verified')

    def test_filters_reject_invalid_ranges_and_preserve_repeated_values(self):
        parsed = information.parse_filters(MultiDict([('college','甲'),('college','乙'),('source','x'),('source','y'),('tag','新闻'),('date','2026-10-12')]))
        self.assertEqual(parsed['colleges'],['甲','乙'])
        self.assertEqual(parsed['sources'],['x','y'])
        self.assertEqual((parsed['date_from'],parsed['date_to']),('2026-10-12','2026-10-12'))
        for filters in ({'date_from':'2026-02-30'},{'date_type':'due_time'},
                        {'date_from':'2026-10-20','date_to':'2026-10-12'}, {'college_relation':'topic'},
                        {'date_type':['source']}):
            with self.subTest(filters=filters),self.assertRaises(ValueError):
                information.parse_filters(filters)

    def test_export_uses_entire_filter_and_rejects_excess(self):
        for index in range(3):
            information.save_document(document(url='https://example.org/article?id='+str(index)),None,'editor')
        self.assertEqual(len(information.query_records({},per_page=1)['records']),1)
        self.assertEqual(len(information.export_records({})),3)
        with self.assertRaises(ValueError):
            information.export_records({},max_records=2)
        self.assertEqual(information.export_records({},scope='public'),[])

    def test_inflight_claim_prevents_duplicate_model_calls(self):
        saved = information.save_document(document(),'official','editor')
        with closing(runtime.connect_db()) as conn, conn:
            conn.execute("UPDATE source_documents SET model_status='extracting' WHERE id=?",(saved['id'],))
        with self.assertRaises(acquisition.AcquisitionError) as caught:
            information.enrich_document(saved['id'],'editor')
        self.assertEqual(caught.exception.code,'model_in_progress')
        self.model.assert_not_called()

    def test_alternate_current_runtime_root_has_separate_schema_and_sources(self):
        with self.alternate_runtime('alternate'):
            schema.migrate()
            self.assertEqual(information.list_sources(),[])
            self.assertEqual(information.query_records({})['total'],0)
        self.assertEqual(information.list_sources()[0]['id'],'official')

    def test_reverting_content_restores_prior_version_without_model_repeat(self):
        first,result = self.enrich()
        information.save_document(document(TEXT+' 新版本内容。'),'official','editor')
        restored = information.save_document(document(),'official','editor')
        self.assertEqual(restored['id'],first['id'])
        self.assertFalse(restored['created'])
        self.assertEqual(information.query_records({})['records'][0]['id'],result['record_ids'][0])
        information.enrich_document(first['id'],'editor')
        self.assertEqual(self.model.call_count,1)

    def test_unlinked_pasted_documents_are_not_versions_of_empty_url(self):
        first = information.save_document(document('公开正文甲',url=''),None,'editor')
        second = information.save_document(document('公开正文乙',url=''),None,'editor')
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(information.query_records({})['total'],2)

    def test_materialized_image_bytes_control_identity_before_model_call(self):
        import base64
        first = document()
        first['media'] = [{'data':base64.b64encode(b'\x89PNG\r\n\x1a\nfirst').decode(),'url':'https://example.org/poster.png'}]
        saved = information.save_document(first,'official','editor')
        repeated = information.save_document(first,'official','editor')
        self.assertFalse(repeated['created'])
        changed = dict(first,media=[{'data':base64.b64encode(b'\x89PNG\r\n\x1a\nchanged').decode(),'url':'https://example.org/poster.png'}])
        second = information.save_document(changed,'official','editor')
        self.assertNotEqual(saved['id'],second['id'])
        self.model.assert_not_called()

    def test_six_original_images_survive_normalization_and_keep_historical_identity(self):
        import base64
        import hashlib
        import io
        from PIL import Image
        original = document()
        original['media'] = []
        for index in range(6):
            output = io.BytesIO()
            with Image.new('RGB',(20,30),(index*30,20,40)) as image:
                image.save(output,format='PNG')
            original['media'].append(acquisition._image(output.getvalue(),f'https://example.org/{index}.png'))
        historical_digest = acquisition._digest(original)
        first = information.save_document(original,'official','editor')
        captured = information.get_document(first['id'])
        self.assertTrue(first['capture_complete'])
        self.assertIsNone(first['capture_error'])
        self.assertEqual(captured['digest'],historical_digest)
        self.assertEqual(captured['media'],original['media'])
        acquisition._model_images(captured['media'])
        repeated = information.save_document(captured,'official','editor')
        self.assertEqual(repeated['id'],first['id'])
        self.assertFalse(repeated['created'])
        for image in information.get_document(first['id'])['media']:
            self.assertEqual(image['digest'],hashlib.sha256(base64.b64decode(image['data'])).hexdigest())
        self.model.assert_not_called()
        with patch.object(acquisition,'_model_images',wraps=acquisition._model_images) as normalize:
            information.enrich_document(first['id'],'editor')
        normalize.assert_called_once()
        self.model.assert_called_once()
        self.assertEqual(information.get_document(first['id'])['media'],original['media'])

    def test_invalid_preview_retains_raw_evidence_and_honest_model_failure(self):
        import base64
        raw = b'\x89PNG\r\n\x1a\ncorrupt-but-retained-source'
        original = document()
        original['media'] = [acquisition._image(raw,'https://example.org/corrupt.png')]
        saved = information.save_document(original,'official','editor')
        with closing(runtime.connect_db()) as conn:
            before = conn.execute('SELECT document_json FROM source_documents WHERE id=?',(saved['id'],)).fetchone()[0]
        with self.assertRaises(acquisition.AcquisitionError) as caught:
            information.enrich_document(saved['id'],'editor')
        self.assertEqual(caught.exception.code,'invalid_image')
        self.model.assert_not_called()
        captured = information.get_document(saved['id'])
        self.assertEqual(base64.b64decode(captured['media'][0]['data']),raw)
        self.assertEqual(captured['model_status'],'not_extracted')
        self.assertEqual(captured['model_error'],'invalid_image')
        with closing(runtime.connect_db()) as conn:
            self.assertEqual(conn.execute('SELECT document_json FROM source_documents WHERE id=?',(saved['id'],)).fetchone()[0],before)
            self.assertEqual(conn.execute('SELECT count(*) FROM entries').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT count(*) FROM source_document_model_attempts').fetchone()[0],0)

    def test_incomplete_image_capture_is_saved_but_never_claims_model_attempt(self):
        original = document()
        original['images'] = ['https://example.org/missing.png']
        original['raw_response_base64'] = 'cmF3IHJlc3BvbnNl'
        with patch.object(acquisition,'fetch_public_resource',side_effect=acquisition.AcquisitionError('不可访问','source_unavailable')):
            saved = information.save_document(original,'official','editor')
        self.assertFalse(saved['capture_complete'])
        self.assertEqual(saved['capture_error']['code'],'source_incomplete')
        captured = information.get_document(saved['id'])
        self.assertEqual(captured['raw_response_base64'],original['raw_response_base64'])
        self.assertEqual(captured['capture_errors'][0]['code'],'source_unavailable')
        with self.assertRaises(acquisition.AcquisitionError) as caught:
            information.enrich_document(saved['id'],'editor')
        self.assertEqual(caught.exception.code,'source_incomplete')
        self.model.assert_not_called()
        with closing(runtime.connect_db()) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM source_document_model_attempts').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT count(*) FROM entries').fetchone()[0],0)
        self.assertEqual(information.get_document(saved['id'])['model_status'],'not_extracted')

    def test_many_page_tiff_stops_header_traversal_before_paid_attempt_and_retains_original(self):
        import base64
        import io
        from PIL import Image, TiffImagePlugin
        frames = [Image.new('RGB',(2,2),(index*3,10,20)) for index in range(80)]
        try:
            output = io.BytesIO()
            frames[0].save(output,format='TIFF',save_all=True,append_images=frames[1:])
            raw = output.getvalue()
        finally:
            for frame in frames:
                frame.close()
        original = document()
        original['media'] = [acquisition._image(raw,'https://example.org/many-pages.tiff')]
        saved = information.save_document(original,'official','editor')
        seeks = []
        original_seek = TiffImagePlugin.TiffImageFile._seek
        def bounded_seek(image, frame):
            seeks.append(frame)
            self.assertLessEqual(frame,64,'Traversal exceeded the bounded sentinel page.')
            return original_seek(image,frame)
        with patch.object(TiffImagePlugin.TiffImageFile,'_seek',new=bounded_seek):
            with self.assertRaises(acquisition.AcquisitionError) as caught:
                information.enrich_document(saved['id'],'editor')
        self.assertEqual(caught.exception.code,'image_unsupported')
        self.assertTrue(seeks)
        self.assertLessEqual(max(seeks),64)
        self.model.assert_not_called()
        captured = information.get_document(saved['id'])
        self.assertEqual(base64.b64decode(captured['media'][0]['data']),raw)
        self.assertEqual(captured['model_status'],'not_extracted')
        with closing(runtime.connect_db()) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM source_document_model_attempts').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT count(*) FROM entries').fetchone()[0],0)

    def test_empty_incomplete_capture_requires_checkable_raw_response_and_retains_parser_error(self):
        import base64
        import hashlib
        raw = b'<script>var vsb_pdf_image_data=loadDynamicPages();</script>'
        original = document('',url='https://example.org/malformed-viewer')
        parser_error = {'code':'source_incomplete','message':'PDF preview pages were not a supported literal.'}
        original.update(raw_content=raw.decode(),raw_response_base64=base64.b64encode(raw).decode(),
                        raw_response_digest=hashlib.sha256(raw).hexdigest(),
                        capture_complete=False,capture_errors=[parser_error])
        for changes in ({'capture_complete':True,'capture_errors':[]},
                        {'raw_response_base64':None}, {'raw_response_base64':'not base64'},
                        {'raw_response_digest':'0'*64}, {'raw_content':''}):
            with self.subTest(changes=changes), self.assertRaises(acquisition.AcquisitionError) as caught:
                information.save_document(dict(original,**changes),'official','editor')
            self.assertEqual(caught.exception.code,'source_empty')
        saved = information.save_document(original,'official','editor')
        self.assertFalse(saved['capture_complete'])
        captured = information.get_document(saved['id'])
        self.assertEqual(captured['raw_content'],raw.decode())
        self.assertEqual(base64.b64decode(captured['raw_response_base64']),raw)
        self.assertEqual(captured['capture_errors'],[parser_error])
        with self.assertRaises(acquisition.AcquisitionError):
            information.enrich_document(saved['id'],'editor')
        self.model.assert_not_called()
        with closing(runtime.connect_db()) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM source_document_model_attempts').fetchone()[0],0)

    def test_acquire_accepts_real_bmp_over_global_limit_without_relaxing_other_routes(self):
        import base64
        import io
        from flask import Flask, request
        from PIL import Image
        output = io.BytesIO()
        with Image.new('RGB',(2600,2500),'blue') as image:
            image.save(output,format='BMP')
        raw = output.getvalue()
        self.assertGreater(len(raw),16*1024*1024)
        app = Flask(__name__)
        app.secret_key = 'fixture-only-secret'
        app.config['MAX_CONTENT_LENGTH'] = 16*1024*1024
        app.add_url_rule('/describe/<int:entry_id>',endpoint='describe',view_func=lambda entry_id:'editor')
        app.add_url_rule('/review/<int:entry_id>',endpoint='review',view_func=lambda entry_id:'review')
        app.add_url_rule('/ordinary',methods=['POST'],view_func=lambda:str(len(request.files)))
        acquisition.register(app)
        client = app.test_client()
        with client.session_transaction() as state:
            state['username'] = 'editor'
            state['csrf_token'] = 'csrf-fixture'
        with patch.object(runtime,'get_user_role',return_value='editor'):
            response = client.post('/acquire',headers={'Accept':'application/json'},data={
                'csrf_token':'csrf-fixture','public_confirm':'yes','public_text':TEXT,
                'public_images':(io.BytesIO(raw),'poster.bmp')})
        self.addCleanup(response.request.environ['wsgi.input'].close)
        self.addCleanup(response.close)
        self.assertEqual(response.status_code,201,response.get_json())
        with closing(runtime.connect_db()) as conn:
            document_id = conn.execute('SELECT id FROM source_documents').fetchone()[0]
        captured = information.get_document(document_id)
        self.assertEqual(captured['media'][0]['mime'],'image/bmp')
        self.assertEqual(base64.b64decode(captured['media'][0]['data']),raw)
        self.model.assert_called_once()
        rejected = client.post('/ordinary',data={'file':(io.BytesIO(raw),'poster.bmp')})
        self.addCleanup(rejected.request.environ['wsgi.input'].close)
        self.addCleanup(rejected.close)
        self.assertEqual(rejected.status_code,413)
        self.assertEqual(app.config['MAX_CONTENT_LENGTH'],16*1024*1024)

    def test_later_verified_source_and_date_metadata_do_not_duplicate_content(self):
        original = document()
        original['raw_date_text'] = None
        first = information.save_document(original,None,'editor')
        enriched = dict(original,source_published_date='2026-10-12')
        second = information.save_document(enriched,'official','editor')
        self.assertEqual(first['id'],second['id'])
        self.assertFalse(second['created'])
        saved = information.get_document(first['id'])
        self.assertEqual(saved['source_id'],'official')
        self.assertEqual(saved['source_published_date'],'2026-10-12')


    def test_captured_publisher_survives_source_failure_and_definition_edits(self):
        saved, result = self.enrich()
        self.approve(result['entry_ids'][0])
        for fields in ({'status':'failed','message':'短暂不可达'},
                       {'status':'verified','publisher_college':'新来源学院','url':'https://example.org/new-list'}):
            information.update_source('official', **fields)
            schema.migrate()
            captured = information.get_document(saved['id'])
            self.assertEqual(captured['publisher_college'], '新闻传播学院')
            for scope in ('private', 'public'):
                filters = {'college':'新闻传播学院','college_relation':'publisher'}
                rows = information.query_records(filters,scope=scope)['records']
                self.assertEqual(len(rows), 1)
                publisher = next(college for college in rows[0]['colleges'] if college['relation']=='publisher')
                self.assertEqual(publisher['name'], '新闻传播学院')
                if scope=='private':
                    self.assertEqual(publisher['evidence'], document()['canonical_url'])
                self.assertEqual(information.query_records({'college':'新来源学院','college_relation':'publisher'},scope=scope)['total'],0)
        latest = information.save_document(document(url='https://example.org/new-article'),'official','editor')
        self.assertEqual(information.get_document(latest['id'])['publisher_college'], '新来源学院')

    def test_enabled_intent_survives_failure_without_claiming_verified_coverage(self):
        information.update_source('official',status='failed')
        self.assertTrue(information.list_sources()[0]['enabled'])
        self.assertEqual(information.list_sources(enabled_only=True), [])
        information.update_source('official',status='verified')
        self.assertEqual([source['id'] for source in information.list_sources(True)], ['official'])

    def test_pending_source_never_retroactively_labels_captured_documents(self):
        information.register_source({'id':'pending','name':'未核验来源','url':'https://example.org/pending',
                                     'publisher_college':'不能推断的学院','enabled':True,'status':'pending_validation'})
        original = document(url='https://example.org/pending-article')
        saved = information.save_document(original,'pending','editor')
        information.update_source('pending',status='verified')
        repeated = information.save_document(original,'pending','editor')
        schema.migrate()
        self.assertEqual(saved['id'],repeated['id'])
        self.assertIsNone(information.get_document(saved['id'])['publisher_college'])
        self.assertEqual(information.query_records({'source':'pending'})['records'][0]['colleges'], [])
        self.assertEqual(information.query_records({'college':'不能推断的学院','college_relation':'publisher'})['total'],0)

    def test_legacy_publisher_append_migrates_only_currently_verified_definitions_once(self):
        with self.alternate_runtime('legacy-publisher') as database:
            with closing(sqlite3.connect(database)) as conn, conn:
                conn.execute('CREATE TABLE entries ('+', '.join('"'+name+'" '+ddl for name,ddl in runtime.ENTRY_COLUMNS)+')')
                conn.executescript('''CREATE TABLE information_sources (
                    id TEXT PRIMARY KEY,name TEXT NOT NULL,url TEXT NOT NULL,publisher_college TEXT,
                    kind TEXT NOT NULL DEFAULT 'html',enabled INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending_validation',message TEXT NOT NULL DEFAULT '',
                    last_checked_at TEXT,last_success_at TEXT);
                    CREATE TABLE source_documents (
                    id INTEGER PRIMARY KEY,source_id TEXT REFERENCES information_sources(id),
                    source_url TEXT NOT NULL,digest TEXT NOT NULL,document_json TEXT NOT NULL,
                    title TEXT NOT NULL,content_text TEXT NOT NULL,source_published_date TEXT,
                    fetched_at TEXT NOT NULL,captured_by TEXT NOT NULL,
                    model_status TEXT NOT NULL DEFAULT 'not_extracted',model_error TEXT,
                    acquisition_id INTEGER REFERENCES acquisitions(id),UNIQUE(source_url,digest));''')
                for source_id,status in (('proven','verified'),('unproven','pending_validation')):
                    conn.execute('''INSERT INTO information_sources(id,name,url,publisher_college,enabled,status)
                        VALUES(?,?,?,?,1,?)''', (source_id,source_id,'https://example.org/'+source_id,
                        source_id+'学院',status))
                    original = document(url='https://example.org/'+source_id+'/article')
                    conn.execute('''INSERT INTO source_documents(source_id,source_url,digest,document_json,
                        title,content_text,fetched_at,captured_by) VALUES(?,?,?,?,?,?,?,?)''',
                        (source_id,original['canonical_url'],source_id,json.dumps(original),original['title'],
                         original['text'],original['fetched_at'],'editor'))
            schema.migrate()
            with closing(runtime.connect_db()) as conn:
                before = conn.execute('SELECT source_id,publisher_college FROM source_documents ORDER BY id').fetchall()
                columns = [row[1] for row in conn.execute('PRAGMA table_info(source_documents)')]
            self.assertEqual(before,[('proven','proven学院'),('unproven',None)])
            self.assertEqual(columns[-1],'publisher_college')
            information.update_source('proven',status='failed',publisher_college='变更学院')
            information.update_source('unproven',status='verified')
            schema.migrate()
            with closing(runtime.connect_db()) as conn:
                self.assertEqual(conn.execute('SELECT source_id,publisher_college FROM source_documents ORDER BY id').fetchall(),before)
                self.assertEqual([row[1] for row in conn.execute('PRAGMA table_info(entries)')],[name for name,_ in runtime.ENTRY_COLUMNS])

    def test_historical_job_apply_date_is_not_source_publication_metadata(self):
        original = document(url='https://job.nju.edu.cn/career/specifics/123')
        original.update(source_type='nju_job_api',raw_date_text='2026-10-18',
                        raw_content=json.dumps({'data':{'theme':'公开招聘','intro':TEXT,'applyAt':'2026-10-18'}}))
        original['digest'] = acquisition._digest(original)
        original['model_name'] = 'gpt-6-luna'
        with self.alternate_runtime('legacy-job-date') as database:
            with closing(sqlite3.connect(database)) as conn, conn:
                create_legacy_acquisition_tables(conn)
                conn.execute('''INSERT INTO acquisitions(source_url,digest,document_json,model_name,acquired_by,created_at)
                    VALUES(?,?,?,?,?,?)''',(original['canonical_url'],original['digest'],json.dumps(original),
                                         original['model_name'],'editor',original['fetched_at']))
            schema.migrate()
            with closing(runtime.connect_db()) as conn:
                document_id = conn.execute('SELECT id FROM source_documents WHERE source_url=?',(original['canonical_url'],)).fetchone()[0]
            captured = information.get_document(document_id)
            self.assertIsNone(captured['source_published_date'])
            self.assertEqual(captured['raw_date_text'],'2026-10-18')
            self.model.assert_not_called()


    def test_audit_disk_failure_leaves_source_unpaid_and_calls_no_admission_hook(self):
        from nannanews import model_audit
        saved = information.save_document(document(),'official','editor')
        original = information.get_document(saved['id'])['text']
        calls = []
        with patch.object(model_audit.os,'posix_fallocate',side_effect=OSError('disk full')):
            with self.assertRaises(acquisition.AcquisitionError) as caught:
                information.enrich_document(saved['id'],'editor',before_send=lambda conn: calls.append(conn))
        self.assertEqual(caught.exception.code,'model_audit_failed')
        self.assertEqual(calls,[])
        self.model.assert_not_called()
        captured = information.get_document(saved['id'])
        self.assertEqual(captured['model_status'],'not_extracted')
        self.assertEqual(captured['model_error'],'model_audit_failed')
        self.assertEqual(captured['text'],original)
        with closing(runtime.connect_db()) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM source_document_model_attempts').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT count(*) FROM entries').fetchone()[0],0)

    def test_admission_sql_rolls_back_with_rejected_source_attempt(self):
        saved = information.save_document(document(),'official','editor')
        with closing(runtime.connect_db()) as conn:
            original = conn.execute('SELECT date_uncertainty FROM information_records WHERE document_id=?',
                                    (saved['id'],)).fetchone()[0]

        def reject(conn):
            self.assertTrue(conn.in_transaction)
            conn.execute("UPDATE information_records SET date_uncertainty='must roll back' WHERE document_id=?",
                         (saved['id'],))
            raise acquisition.AcquisitionError('Admission refused before payment.','model_admission_blocked')

        with self.assertRaises(acquisition.AcquisitionError):
            information.enrich_document(saved['id'],'editor',before_send=reject)
        self.model.assert_not_called()
        with closing(runtime.connect_db()) as conn:
            self.assertEqual(conn.execute('SELECT date_uncertainty FROM information_records WHERE document_id=?',
                                          (saved['id'],)).fetchone()[0],original)
            self.assertEqual(conn.execute('SELECT count(*) FROM source_document_model_attempts').fetchone()[0],0)
        self.assertEqual(information.get_document(saved['id'])['model_status'],'not_extracted')

    def test_admission_commits_before_paid_boundary_and_completed_reuse_skips_hook(self):
        saved = information.save_document(document(),'official','editor')
        body = self.model.return_value
        calls = []

        def admit(conn):
            self.assertTrue(conn.in_transaction)
            paths = list((self.settings.data_dir/'model-audit').glob('wire-*/receipt.json'))
            self.assertEqual(len(paths),1)
            metadata = json.loads(paths[0].read_text())
            self.assertEqual(metadata['phase'],'prepared')
            self.assertEqual(conn.execute('SELECT count(*) FROM source_document_model_attempts').fetchone()[0],0)
            calls.append('admission')

        def send(payload, *, audit=None):
            with closing(runtime.connect_db()) as conn:
                conn.execute('BEGIN IMMEDIATE')  # No source/admission transaction spans the send.
                self.assertEqual(conn.execute('SELECT model_status FROM source_documents WHERE id=?',
                                              (saved['id'],)).fetchone()[0],'extracting')
                self.assertEqual(conn.execute('SELECT count(*) FROM source_document_model_attempts').fetchone()[0],1)
                conn.rollback()
            calls.append('send')
            return body

        self.model.side_effect = send
        first = information.enrich_document(saved['id'],'editor',before_send=admit)
        second = information.enrich_document(saved['id'],'editor',before_send=admit)
        self.assertEqual(first['model_status'],'completed')
        self.assertFalse(second['created'])
        self.assertEqual(calls,['admission','send'])


if __name__=='__main__':
    unittest.main()
