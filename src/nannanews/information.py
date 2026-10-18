"""Canonical captured sources and entry-linked campus information.

Initialize after runtime's core migration. All connections resolve current settings;
source evidence is immutable, while editorial content is always joined from entries.
No network or model work occurs during schema migration/backfill.
"""
from contextlib import closing
from datetime import date, datetime, timezone
import base64
import hashlib
import json
import re
import sqlite3

from nannanews import acquisition
from nannanews import runtime
from nannanews import schema

DATE_COLUMNS = {'source': 'source_published_date', 'event': 'event_date',
                'deadline': 'deadline_date', 'publication': 'publication_date'}
RELATIONS = ('any', 'publisher', 'associated', 'audience')
SOURCE_STATUSES = ('verified', 'failed', 'unsupported', 'pending_validation')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False)


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat()


def _migrate_schema(conn):
    """Apply legacy schema/backfills only inside the explicit migration."""
    schema.execute_script(conn, '''
        CREATE TABLE IF NOT EXISTS information_sources (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
            publisher_college TEXT, kind TEXT NOT NULL DEFAULT 'html',
            enabled INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending_validation',
            message TEXT NOT NULL DEFAULT '', last_checked_at TEXT, last_success_at TEXT);
        CREATE TABLE IF NOT EXISTS source_documents (
            id INTEGER PRIMARY KEY, source_id TEXT REFERENCES information_sources(id),
            source_url TEXT NOT NULL, digest TEXT NOT NULL, document_json TEXT NOT NULL,
            title TEXT NOT NULL, content_text TEXT NOT NULL, source_published_date TEXT,
            fetched_at TEXT NOT NULL, captured_by TEXT NOT NULL,
            model_status TEXT NOT NULL DEFAULT 'not_extracted', model_error TEXT,
            acquisition_id INTEGER REFERENCES acquisitions(id),
            publisher_college TEXT,
            UNIQUE(source_url,digest));
        CREATE TABLE IF NOT EXISTS source_document_model_attempts (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES source_documents(id),
            actor TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
            model_status TEXT NOT NULL DEFAULT 'extracting', model_error TEXT,
            model_name TEXT, output_json TEXT, response_json TEXT);
        CREATE TABLE IF NOT EXISTS source_document_heads (
            source_url TEXT PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS information_records (
            id INTEGER PRIMARY KEY, entry_id INTEGER UNIQUE REFERENCES entries(id) ON DELETE CASCADE,
            document_id INTEGER REFERENCES source_documents(id), event_key TEXT,
            event_date TEXT, deadline_date TEXT,
            event_date_resolution TEXT NOT NULL DEFAULT 'unknown',
            deadline_date_resolution TEXT NOT NULL DEFAULT 'unknown',
            event_date_basis TEXT NOT NULL DEFAULT '', deadline_date_basis TEXT NOT NULL DEFAULT '',
            model_status TEXT NOT NULL DEFAULT 'manual',
            date_uncertainty TEXT NOT NULL DEFAULT '', evidence_quotes_json TEXT NOT NULL DEFAULT '[]',
            UNIQUE(document_id,event_key));
        CREATE UNIQUE INDEX IF NOT EXISTS information_raw_document ON information_records(document_id)
            WHERE entry_id IS NULL;
        CREATE INDEX IF NOT EXISTS information_document_source_date ON source_documents(source_id,source_published_date);
        CREATE INDEX IF NOT EXISTS information_document_url ON source_documents(source_url,id);
        CREATE INDEX IF NOT EXISTS information_event_date ON information_records(event_date);
        CREATE INDEX IF NOT EXISTS information_deadline_date ON information_records(deadline_date);
        CREATE TABLE IF NOT EXISTS information_colleges (
            record_id INTEGER NOT NULL REFERENCES information_records(id) ON DELETE CASCADE,
            name TEXT NOT NULL, relation TEXT NOT NULL CHECK(relation IN ('associated','audience')),
            evidence TEXT NOT NULL, PRIMARY KEY(record_id,name,relation));
        CREATE INDEX IF NOT EXISTS information_college_filter ON information_colleges(name,relation,record_id);
        CREATE TRIGGER IF NOT EXISTS information_entry_insert AFTER INSERT ON entries BEGIN
            INSERT OR IGNORE INTO information_records(entry_id,model_status,date_uncertainty)
            VALUES(NEW.id,CASE WHEN NEW.describer LIKE 'model:%' THEN 'legacy_model' ELSE 'manual' END,
                   '历史编辑日期未区分活动日期与截止日期；来源发布日期未知。');
        END;
        CREATE TRIGGER IF NOT EXISTS information_entry_delete BEFORE DELETE ON entries BEGIN
            DELETE FROM acquisition_candidates WHERE entry_id=OLD.id;
            DELETE FROM information_colleges WHERE record_id IN
                (SELECT id FROM information_records WHERE entry_id=OLD.id);
            DELETE FROM information_records WHERE entry_id=OLD.id;
        END;
        DROP VIEW IF EXISTS public_information_records_view;
        DROP VIEW IF EXISTS information_records_view;
        DROP VIEW IF EXISTS information_colleges_view;
    ''')
    document_columns = {row[1] for row in conn.execute('PRAGMA table_info(source_documents)')}
    if 'publisher_college' not in document_columns:
        conn.execute('ALTER TABLE source_documents ADD COLUMN publisher_college TEXT')
        # One-time legacy migration: only the currently proven definition is evidence.
        # Subsequent initialization must not retroactively label pending captures.
        conn.execute('''UPDATE source_documents SET publisher_college=(
            SELECT s.publisher_college FROM information_sources s
            WHERE s.id=source_documents.source_id AND s.status='verified')''')
    record_columns = {row[1] for row in conn.execute('PRAGMA table_info(information_records)')}
    if 'event_date_resolution' not in record_columns:
        conn.execute("ALTER TABLE information_records ADD COLUMN event_date_resolution TEXT NOT NULL DEFAULT 'unknown'")
    if 'deadline_date_resolution' not in record_columns:
        conn.execute("ALTER TABLE information_records ADD COLUMN deadline_date_resolution TEXT NOT NULL DEFAULT 'unknown'")
    if 'event_date_basis' not in record_columns:
        conn.execute("ALTER TABLE information_records ADD COLUMN event_date_basis TEXT NOT NULL DEFAULT ''")
    if 'deadline_date_basis' not in record_columns:
        conn.execute("ALTER TABLE information_records ADD COLUMN deadline_date_basis TEXT NOT NULL DEFAULT ''")
    conn.execute('''
        CREATE VIEW information_colleges_view AS
            SELECT record_id,name,relation,evidence FROM information_colleges
            UNION ALL
            SELECT r.id,d.publisher_college,'publisher',d.source_url
            FROM information_records r JOIN source_documents d ON d.id=r.document_id
            WHERE d.publisher_college IS NOT NULL AND d.publisher_college<>''
    ''')
    conn.execute('''CREATE VIEW information_records_view AS
            SELECT r.id,r.entry_id,r.document_id,
                CASE WHEN r.entry_id IS NULL THEN d.title ELSE e.title END AS title,
                CASE WHEN r.entry_id IS NULL THEN substr(d.content_text,1,600) ELSE coalesce(e.description,'') END AS description,
                coalesce(d.content_text,'') AS content_text,d.source_id,s.name AS source_name,
                coalesce(d.source_url,CASE WHEN e.link LIKE 'http://%' OR e.link LIKE 'https://%' THEN e.link END) AS source_url,
                d.source_published_date,r.event_date,r.deadline_date,
                CASE WHEN e.publish_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
                     AND datetime(e.publish_date) IS NOT NULL
                     AND substr(e.publish_date,1,4) BETWEEN '0001' AND '9999'
                     AND date(substr(e.publish_date,1,10),'+0 days')=substr(e.publish_date,1,10)
                     AND substr(e.publish_date,1,10)<>'1970-01-01'
                     THEN substr(e.publish_date,1,10) END AS publication_date,
                e.tag,coalesce(e.status,'unverified') AS verification_status,
                CASE WHEN r.entry_id IS NULL THEN d.model_status ELSE r.model_status END AS model_status,
                r.event_date_resolution,r.deadline_date_resolution,
                r.event_date_basis,r.deadline_date_basis,r.date_uncertainty,r.evidence_quotes_json,
                CASE WHEN r.entry_id IS NULL THEN 'original_text' ELSE 'editorial_description' END AS description_kind
            FROM information_records r LEFT JOIN entries e ON e.id=r.entry_id
            LEFT JOIN source_documents d ON d.id=r.document_id
            LEFT JOIN information_sources s ON s.id=d.source_id
            WHERE (r.entry_id IS NULL OR e.id IS NOT NULL)
              AND (d.id IS NULL OR d.source_url='' OR EXISTS (
                  SELECT 1 FROM source_document_heads h WHERE h.source_url=d.source_url AND h.document_id=d.id))
              AND (r.entry_id IS NOT NULL OR d.model_status<>'completed')
    ''')
    conn.execute('''CREATE VIEW public_information_records_view AS
            SELECT id,entry_id,document_id,title,description,source_id,source_name,source_url,
                   source_published_date,event_date,deadline_date,publication_date,tag,
                   verification_status,model_status,description_kind
            FROM information_records_view
            WHERE entry_id IS NOT NULL AND verification_status='approved'
              AND publication_date IS NOT NULL
              AND publication_date<=date('now','+8 hours')
    ''')
    conn.execute('''INSERT OR IGNORE INTO information_records(entry_id,model_status,date_uncertainty)
        SELECT id,CASE WHEN describer LIKE 'model:%' THEN 'legacy_model' ELSE 'manual' END,
        '历史编辑日期未区分活动日期与截止日期；来源发布日期未知。' FROM entries''')
    _backfill(conn)
    conn.execute('''INSERT OR IGNORE INTO source_document_heads(source_url,document_id)
        SELECT source_url,max(id) FROM source_documents WHERE source_url<>'' GROUP BY source_url''')


def _backfill(conn):
    rows = conn.execute('''SELECT a.id,a.document_json,a.acquired_by FROM acquisitions a
        WHERE NOT EXISTS(SELECT 1 FROM source_documents d WHERE d.acquisition_id=a.id) ORDER BY a.id''')
    for acquisition_id, raw, actor in rows:
        document = json.loads(raw)
        document['source_published_date'] = acquisition._source_publication_date(document)
        # Stored historic media is already materialized; migration never fetches it.
        digest = acquisition._digest(document)
        conn.execute('''INSERT OR IGNORE INTO source_documents
            (source_url,digest,document_json,title,content_text,source_published_date,fetched_at,captured_by,model_status,acquisition_id)
            VALUES(?,?,?,?,?,?,?,?,'completed',?)''',
            (document.get('canonical_url',''), digest, _json(document), document.get('title',''),
             document.get('text',''), document['source_published_date'], document.get('fetched_at',_now()), actor, acquisition_id))
        document_id = conn.execute('SELECT id FROM source_documents WHERE source_url=? AND digest=?',
                                   (document.get('canonical_url',''),digest)).fetchone()[0]
        conn.execute('INSERT OR IGNORE INTO information_records(document_id,model_status) VALUES(?,?)', (document_id,'completed'))
        candidates = conn.execute('''SELECT c.entry_id,c.draft_json FROM acquisition_candidates c
            JOIN entries e ON e.id=c.entry_id WHERE c.acquisition_id=?''', (acquisition_id,)).fetchall()
        for entry_id, draft_json in candidates:
            _index_candidate(conn, document_id, entry_id, json.loads(draft_json), historical=True)


def _persist_source(conn, source):
    if not isinstance(source, dict):
        raise ValueError('来源定义必须为对象。')
    for key in ('id', 'name', 'url'):
        if not isinstance(source.get(key), str) or not source[key].strip():
            raise ValueError('来源必须提供 id、name 和 url。')
    source_id = source['id'].strip()
    url = acquisition.canonical_url(source['url'])
    kind, status = source.get('kind', 'html'), source.get('status', 'pending_validation')
    if kind not in ('html', 'nju_job') or status not in SOURCE_STATUSES:
        raise ValueError('来源类型或状态不受支持。')
    enabled = source.get('enabled', False)
    if type(enabled) is not bool:
        raise ValueError('enabled 必须为布尔值。')
    college, message = source.get('publisher_college'), source.get('message', '')
    if college is not None and (not isinstance(college, str) or len(college) > 200):
        raise ValueError('来源学院格式错误。')
    if not isinstance(message, str):
        raise ValueError('来源状态说明格式错误。')
    times = [source.get(key) for key in ('last_checked_at', 'last_success_at')]
    if any(value is not None and not isinstance(value, str) for value in times):
        raise ValueError('来源检查时间格式错误。')
    conn.execute('''INSERT INTO information_sources
        (id,name,url,publisher_college,kind,enabled,status,message,last_checked_at,last_success_at)
        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url,
        publisher_college=excluded.publisher_college,kind=excluded.kind,enabled=excluded.enabled,
        status=excluded.status,message=excluded.message,last_checked_at=excluded.last_checked_at,
        last_success_at=excluded.last_success_at''',
        (source_id,source['name'].strip(),url,college.strip() or None if college else None,
         kind,int(enabled),status,message,*times))
    return source_id


def register_source(source):
    with closing(runtime.connect_db()) as conn, conn:
        source_id = _persist_source(conn, source)
    return next(value for value in list_sources() if value['id'] == source_id)


def list_sources(enabled_only=False):
    with closing(runtime.connect_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM information_sources' + (' WHERE enabled=1 AND status=\'verified\'' if enabled_only else '') + ' ORDER BY name,id').fetchall()
    return [dict(row, enabled=bool(row['enabled'])) for row in rows]


def update_source(source_id, **fields):
    allowed = {'name','url','publisher_college','kind','enabled','status','message','last_checked_at','last_success_at'}
    if not fields or set(fields) - allowed:
        raise ValueError('来源更新字段不受支持。')
    with closing(runtime.connect_db()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM information_sources WHERE id=?', (source_id,)).fetchone()
        if row is None:
            raise ValueError('来源不存在。')
        existing = dict(row, enabled=bool(row['enabled']))
        _persist_source(conn, dict(existing, **fields))
    return next(source for source in list_sources() if source['id'] == source_id)


def save_document(document, source_id, actor):
    if not isinstance(document,dict) or not isinstance(document.get('text'),str) or len(document['text'])>acquisition.MAX_TEXT:
        raise acquisition.AcquisitionError('公开正文格式错误或过长。','invalid_content')
    captured = dict(document)
    source_url = captured.get('canonical_url','')
    captured['canonical_url'] = acquisition.canonical_url(source_url) if source_url else ''
    if 'media' not in captured:
        acquisition._prepare_media(captured)
    else:
        media = captured['media']
        if not isinstance(media,list) or len(media)>acquisition.MAX_IMAGES:
            raise acquisition.AcquisitionError('公开图片格式错误。','invalid_image')
        validated, total = [], 0
        try:
            for image in media:
                encoded = image['data']
                if not isinstance(encoded,str) or len(encoded)>4*((acquisition.MAX_IMAGE+2)//3):
                    raise ValueError
                raw = base64.b64decode(encoded,validate=True)
                total += len(raw)
                if total>acquisition.MAX_MEDIA_BYTES:
                    raise ValueError
                retained = acquisition._image(raw,image.get('url',''))
                if 'source_image_index' in image:
                    retained['source_image_index'] = image['source_image_index']
                validated.append(retained)
        except (KeyError,TypeError,ValueError):
            raise acquisition.AcquisitionError('公开图片证据格式错误或超过总量上限。','invalid_image') from None
        captured['media'] = validated
    errors = list(captured.get('capture_errors') or [])
    retained_urls = {image.get('url','') for image in captured['media']}
    if any(url not in retained_urls for url in captured.get('images',[])) and not errors:
        errors.append({'code':'source_incomplete','message':'来源图片未全部保留。'})
    captured['capture_complete'] = captured.get('capture_complete',True) and not errors
    captured['capture_errors'] = errors
    capture_result = {'capture_complete':captured['capture_complete'],
                      'capture_error':({'code':'source_incomplete','message':'来源图片未完整采集；已取得的原始证据已保留。'}
                                       if not captured['capture_complete'] else None)}
    if not captured['text'].strip() and not captured['media']:
        retained_raw = False
        if not captured['capture_complete']:
            try:
                encoded = captured.get('raw_response_base64')
                raw_content = captured.get('raw_content')
                if (isinstance(encoded,str) and len(encoded)<=4*((acquisition.MAX_BODY+2)//3)
                        and isinstance(raw_content,str) and raw_content.strip()):
                    response = base64.b64decode(encoded,validate=True)
                    retained_raw = (0<len(response)<=acquisition.MAX_BODY
                                    and hashlib.sha256(response).hexdigest() == captured.get('raw_response_digest'))
            except (ValueError,TypeError):
                pass
        if not retained_raw:
            raise acquisition.AcquisitionError('没有可保存的公开正文或图片，且缺少完整可核对的原始响应。','source_empty')
    captured['source_published_date'] = acquisition._source_publication_date(captured)
    captured['fetched_at'] = captured.get('fetched_at') or _now()
    captured['digest'] = acquisition._digest(captured)
    # Model answers are private attempt evidence, never part of the immutable fetched source.
    captured.pop('model_output',None)
    captured.pop('model_response',None)
    captured.pop('model_name',None)
    with closing(runtime.connect_db()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        publisher = None
        if source_id is not None:
            source = conn.execute('SELECT status,publisher_college FROM information_sources WHERE id=?', (source_id,)).fetchone()
            if source is None:
                raise ValueError('来源尚未注册。')
            if source[0] == 'verified':
                publisher = source[1]
        row = conn.execute('SELECT id,model_status FROM source_documents WHERE source_url=? AND digest=?',
                           (captured['canonical_url'],captured['digest'])).fetchone()
        if row:
            if source_id is not None:
                conn.execute('UPDATE source_documents SET source_id=? WHERE id=? AND source_id IS NULL', (source_id,row[0]))
            conn.execute('UPDATE source_documents SET source_published_date=coalesce(source_published_date,?) WHERE id=?',
                         (captured['source_published_date'],row[0]))
            _set_current_document(conn, captured['canonical_url'], row[0])
            return dict(id=row[0],created=False,model_status=row[1],**capture_result)
        cursor = conn.execute('''INSERT INTO source_documents(source_id,source_url,digest,document_json,title,content_text,
            source_published_date,fetched_at,captured_by,publisher_college) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (source_id,captured['canonical_url'],captured['digest'],_json(captured),captured.get('title',''),captured['text'],
             captured['source_published_date'],captured['fetched_at'],actor,publisher))
        document_id = cursor.lastrowid
        conn.execute('INSERT INTO information_records(document_id,model_status,date_uncertainty) VALUES(?,?,?)',
                     (document_id,'not_extracted','尚未提取；未确认活动日期与截止日期。'))
        _set_current_document(conn, captured['canonical_url'], document_id)
    return dict(id=document_id,created=True,model_status='not_extracted',**capture_result)


def _set_current_document(conn, source_url, document_id):
    # Pasted documents with no URL are independent, not successive URL versions.
    if source_url:
        conn.execute('''INSERT INTO source_document_heads(source_url,document_id) VALUES(?,?)
            ON CONFLICT(source_url) DO UPDATE SET document_id=excluded.document_id''',
            (source_url,document_id))


def get_document(document_id):
    with closing(runtime.connect_db()) as conn:
        row = conn.execute('''SELECT d.document_json,d.id,d.source_id,d.model_status,d.model_error,
            s.name,s.url,d.publisher_college,s.status,d.source_published_date FROM source_documents d
            LEFT JOIN information_sources s ON s.id=d.source_id WHERE d.id=?''',(document_id,)).fetchone()
    if row is None:
        return None
    value = json.loads(row[0])
    value.update(id=row[1],source_id=row[2],model_status=row[3],model_error=row[4],source_name=row[5],
                 registry_url=row[6],publisher_college=row[7],source_status=row[8])
    value['source_published_date'] = row[9]
    return value


def _index_candidate(conn, document_id, entry_id, item, historical=False):
    uncertainty = item.get('date_uncertainty','')
    if historical and 'event_date' not in item:
        uncertainty = (uncertainty+' 历史 due_time 未区分活动与截止日期，不转换为已知日期。').strip()
    context = []
    for kind, label in (('event', '活动'), ('deadline', '截止')):
        basis = item.get(kind + '_date_basis', '')
        if item.get(kind + '_date_resolution', 'unknown') == 'contextual' and basis:
            context.append(f'{label}日期按上下文判断：{basis}')
    uncertainty = ' '.join(part for part in (uncertainty, *context) if part)
    conn.execute('''INSERT INTO information_records(entry_id,document_id,event_key,event_date,deadline_date,
        event_date_resolution,deadline_date_resolution,event_date_basis,deadline_date_basis,
        model_status,date_uncertainty,evidence_quotes_json) VALUES(?,?,?,?,?,?,?,?,?,'completed',?,?)
        ON CONFLICT(entry_id) DO UPDATE SET document_id=excluded.document_id,event_key=excluded.event_key,
        event_date=excluded.event_date,deadline_date=excluded.deadline_date,
        event_date_resolution=excluded.event_date_resolution,deadline_date_resolution=excluded.deadline_date_resolution,
        event_date_basis=excluded.event_date_basis,deadline_date_basis=excluded.deadline_date_basis,
        model_status=excluded.model_status,date_uncertainty=excluded.date_uncertainty,
        evidence_quotes_json=excluded.evidence_quotes_json''',
        (entry_id,document_id,item.get('event_key'),_date(item.get('event_date')),_date(item.get('deadline_date')),
         item.get('event_date_resolution','unknown'),item.get('deadline_date_resolution','unknown'),
         item.get('event_date_basis',''),item.get('deadline_date_basis',''),
         uncertainty,_json(item.get('evidence_quotes',[]))))
    record_id = conn.execute('SELECT id FROM information_records WHERE entry_id=?',(entry_id,)).fetchone()[0]
    conn.execute('DELETE FROM information_colleges WHERE record_id=?',(record_id,))
    for college in item.get('colleges',[]):
        if college.get('relation') in ('associated','audience') and college.get('name'):
            conn.execute('INSERT OR IGNORE INTO information_colleges VALUES(?,?,?,?)',
                         (record_id,college['name'].strip(),college['relation'],college.get('evidence', '')))
    return record_id


def _enrichment_result(conn, document_id, created):
    rows = conn.execute('''SELECT r.entry_id,r.id FROM information_records r JOIN entries e ON e.id=r.entry_id
        WHERE r.document_id=? ORDER BY r.id''',(document_id,)).fetchall()
    status = conn.execute('SELECT model_status FROM source_documents WHERE id=?',(document_id,)).fetchone()[0]
    return {'created':created,'entry_ids':[row[0] for row in rows],'record_ids':[row[1] for row in rows],'model_status':status}


def _finish_model_attempt(conn, attempt_id, document, status, error=None):
    # Keep rejected answers out of source JSON and all record/DTO projections.
    conn.execute('''UPDATE source_document_model_attempts SET finished_at=?,model_status=?,model_error=?,
        model_name=?,output_json=?,response_json=? WHERE id=?''',
        (_now(),status,error,document.get('model_name'),
         _json(document['model_output']) if 'model_output' in document else None,
         _json(document['model_response']) if 'model_response' in document else None,attempt_id))


def enrich_document(document_id, actor, *, prepared_media=None, before_send=None):
    """Enrich once admitted; before_send(conn) may enlist caller SQL atomically.

    The callback runs only after durable wire-audit preparation, in the short
    source-attempt transaction. It must not perform network or open a competing
    write transaction. Transport starts only after this transaction commits.
    """
    document = get_document(document_id)
    if document is None:
        raise ValueError('来源原文不存在。')
    if not document.get('capture_complete',True):
        raise acquisition.AcquisitionError('来源图片未完整采集；原始证据已保留，未创建模型尝试。','source_incomplete')
    if document['model_status'] not in {'completed','extracting'} and prepared_media is None:
        try:
            prepared_media = acquisition.preflight_document_media(document)
        except acquisition.AcquisitionError as error:
            with closing(runtime.connect_db()) as conn, conn:
                conn.execute("UPDATE source_documents SET model_error=? WHERE id=? AND model_status NOT IN ('completed','extracting')",
                             (error.code,document_id))
                conn.execute('UPDATE information_records SET date_uncertainty=? WHERE document_id=? AND entry_id IS NULL',
                             ('图片无法安全完整提取；保留原始证据，未调用模型，活动日期与截止日期未知。',document_id))
            raise
    with closing(runtime.connect_db()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT model_status,document_json FROM source_documents WHERE id=?',(document_id,)).fetchone()
        if row is None:
            raise ValueError('来源原文不存在。')
        if not json.loads(row[1]).get('capture_complete',True):
            raise acquisition.AcquisitionError('来源图片未完整采集；原始证据已保留，未创建模型尝试。','source_incomplete')
        if row[0]=='completed':
            return _enrichment_result(conn,document_id,False)
        if row[0]=='extracting':
            raise acquisition.AcquisitionError('同一原文正在提取或上次提取中断；不会重复调用模型。','model_in_progress',409)
    attempt_id = None
    completed_result = None

    def claim_before_send():
        nonlocal attempt_id, completed_result
        # Called only after exact request durability and response disk reservation.
        # The optional caller admission joins this transaction; no network may run
        # inside it. Both source and daily claims commit before transport starts.
        with closing(runtime.connect_db()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT model_status,document_json FROM source_documents WHERE id=?',
                               (document_id,)).fetchone()
            if row is None:
                raise ValueError('来源原文不存在。')
            if not json.loads(row[1]).get('capture_complete',True):
                raise acquisition.AcquisitionError('来源图片未完整采集；未创建模型尝试。','source_incomplete')
            if row[0] == 'completed':
                completed_result = _enrichment_result(conn,document_id,False)
            elif row[0] == 'extracting':
                raise acquisition.AcquisitionError('同一原文正在提取或上次提取中断；不会重复调用模型。','model_in_progress',409)
            else:
                if before_send is not None:
                    before_send(conn)
                conn.execute("UPDATE source_documents SET model_status='extracting',model_error=NULL WHERE id=?",(document_id,))
                pending_attempt_id = conn.execute('''INSERT INTO source_document_model_attempts(document_id,actor,started_at)
                    VALUES(?,?,?)''',(document_id,actor,_now())).lastrowid
        if completed_result is not None:
            raise acquisition.AcquisitionError('同一原文已完成提取；不会重复调用模型。','model_in_progress',409)
        attempt_id = pending_attempt_id

    try:
        items = acquisition.extract_document(document,prepared_media=prepared_media,
                                             before_send=claim_before_send)
        with closing(runtime.connect_db()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            ids, created, acquisition_id = acquisition._insert_drafts(conn,document,items,actor)
            for entry_id,item in zip(ids,items):
                _index_candidate(conn,document_id,entry_id,item)
            _finish_model_attempt(conn,attempt_id,document,'completed')
            conn.execute("UPDATE source_documents SET model_status='completed',model_error=NULL,acquisition_id=? WHERE id=?",
                         (acquisition_id,document_id))
            return _enrichment_result(conn,document_id,created)
    except Exception as error:
        if completed_result is not None:
            return completed_result
        code = error.code if isinstance(error, acquisition.AcquisitionError) else 'storage_failed'
        if attempt_id is None and code == 'model_in_progress':
            raise
        with closing(runtime.connect_db()) as conn, conn:
            if attempt_id is None:
                # Unpaid audit/admission failure is not a failed paid model attempt.
                conn.execute("UPDATE source_documents SET model_error=? WHERE id=? AND model_status NOT IN ('completed','extracting')",
                             (code,document_id))
            else:
                # Candidate writes rolled back; preserve the answer and failure together separately.
                _finish_model_attempt(conn,attempt_id,document,'failed',code)
                conn.execute("UPDATE source_documents SET model_status='failed',model_error=? WHERE id=?",(code,document_id))
                conn.execute('UPDATE information_records SET date_uncertainty=? WHERE document_id=? AND entry_id IS NULL',
                             ('模型提取失败；保留原文，活动日期与截止日期未知。',document_id))
        raise


def _values(mapping, singular, plural):
    if hasattr(mapping,'getlist'):
        values = mapping.getlist(singular) or mapping.getlist(plural)
    else:
        values = mapping.get(plural,mapping.get(singular,[]))
        if isinstance(values,str):
            values = [values]
    if not isinstance(values,(list,tuple)) or any(not isinstance(value,str) for value in values):
        raise ValueError('筛选列表格式错误。')
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def parse_filters(mapping):
    q = mapping.get('q','')
    if not isinstance(q,str) or len(q)>500:
        raise ValueError('关键词过长或格式错误。')
    date_type = mapping.get('date_type','source') or 'source'
    relation = mapping.get('college_relation','any') or 'any'
    if not isinstance(date_type,str) or date_type not in DATE_COLUMNS or relation not in RELATIONS:
        raise ValueError('日期类型或学院关系不受支持。')
    result = {'q':q.strip(),'date_type':date_type,'college_relation':relation,
              'colleges':_values(mapping,'college','colleges'),'sources':_values(mapping,'source','sources'),
              'tags':_values(mapping,'tag','tags')}
    exact = mapping.get('date','')
    for key in ('date_from','date_to'):
        value = mapping.get(key,'') or exact or None
        if value is not None and not _date(value):
            raise ValueError('日期必须为有效 YYYY-MM-DD，且不能使用未知日期占位值。')
        result[key] = value
    if result['date_from'] and result['date_to'] and result['date_from']>result['date_to']:
        raise ValueError('日期范围起点不能晚于终点。')
    return result


def _query(filters,scope):
    if scope not in ('private','public'):
        raise ValueError('查询范围不受支持。')
    filters = parse_filters(filters)
    view = 'public_information_records_view' if scope=='public' else 'information_records_view'
    clauses,params = [],[]
    if filters['q']:
        escaped = filters['q'].replace('!','!!').replace('%','!%').replace('_','!_')
        fields = ['v.title', 'v.description']
        if scope == 'private':
            fields.append('v.content_text')
        expressions = [field + " LIKE ? ESCAPE '!'" for field in fields]
        clauses.append('(' + ' OR '.join(expressions) + ')')
        params.extend(['%' + escaped + '%'] * len(expressions))
    column = DATE_COLUMNS[filters['date_type']]
    for key,op in (('date_from','>='),('date_to','<=')):
        if filters[key]:
            clauses.append('v.'+column+op+'?')
            params.append(filters[key])
    for key,column_name in (('sources','source_id'),('tags','tag')):
        if filters[key]:
            clauses.append('v.'+column_name+' IN ('+','.join('?' for _ in filters[key])+')')
            params.extend(filters[key])
    if filters['colleges']:
        clause = 'EXISTS(SELECT 1 FROM information_colleges_view c WHERE c.record_id=v.id AND c.name IN ('+','.join('?' for _ in filters['colleges'])+')'
        params.extend(filters['colleges'])
        if filters['college_relation']!='any':
            clause += ' AND c.relation=?'
            params.append(filters['college_relation'])
        clauses.append(clause+')')
    return view,(' AND '.join(clauses) or '1=1'),params,filters


def _dtos(conn, rows, scope):
    if not rows:
        return []
    ids = [row['id'] for row in rows]
    colleges = {}
    for row in conn.execute('SELECT record_id,name,relation,evidence FROM information_colleges_view WHERE record_id IN ('+','.join('?' for _ in ids)+') ORDER BY name,relation',ids):
        value = {'name':row['name'],'relation':row['relation']}
        if scope=='private':
            value['evidence'] = row['evidence']
        colleges.setdefault(row['record_id'],[]).append(value)
    records = []
    for row in rows:
        record = dict(row)
        record['colleges'] = colleges.get(record['id'],[])
        if scope=='private':
            record['evidence_quotes'] = json.loads(record.pop('evidence_quotes_json'))
        records.append(record)
    return records


def query_records(filters,scope='private',page=1,per_page=20):
    if type(page) is not int or type(per_page) is not int or not 1<=per_page<=100:
        raise ValueError('分页参数不正确。')
    view,where,params,normalized = _query(filters,scope)
    column = DATE_COLUMNS[normalized['date_type']]
    with closing(runtime.connect_db()) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute('SELECT count(*) FROM '+view+' v WHERE '+where,params).fetchone()[0]
        pages = (total+per_page-1)//per_page
        page = min(max(1,page),max(1,pages))
        rows = conn.execute('SELECT v.* FROM '+view+' v WHERE '+where+' ORDER BY v.'+column+' IS NULL,v.'+column+' DESC,v.id DESC LIMIT ? OFFSET ?',
                            [*params,per_page,(page-1)*per_page]).fetchall()
        records = _dtos(conn,rows,scope)
    return {'records':records,'total':total,'page':page,'pages':pages,'per_page':per_page}


def _get(key,value,scope):
    view,_,_,_ = _query({},scope)
    with closing(runtime.connect_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM '+view+' WHERE '+key+'=?',(value,)).fetchall()
        records = _dtos(conn,rows,scope)
    return records[0] if records else None


def get_record(record_id,scope='private'):
    return _get('id',record_id,scope)


def get_entry_record(entry_id,scope='private'):
    """Map stable legacy entry URLs without confusing them with record IDs."""
    return _get('entry_id',entry_id,scope)


def filter_options(scope='private'):
    view,_,_,_ = _query({},scope)
    with closing(runtime.connect_db()) as conn:
        conn.row_factory = sqlite3.Row
        if scope=='private':
            sources = list_sources()
        else:
            sources = [dict(row) for row in conn.execute('''SELECT DISTINCT s.id,s.name,s.url,s.publisher_college
                FROM information_sources s JOIN '''+view+' v ON v.source_id=s.id ORDER BY s.name')]
        colleges = [row[0] for row in conn.execute('SELECT DISTINCT c.name FROM information_colleges_view c JOIN '+view+' v ON v.id=c.record_id ORDER BY c.name')]
        tags = [row[0] for row in conn.execute('SELECT DISTINCT tag FROM '+view+" WHERE tag IS NOT NULL AND tag<>'' ORDER BY tag")]
    return {'sources':sources,'colleges':colleges,'tags':tags}


def export_records(filters,scope='private',max_records=500):
    if type(max_records) is not int or not 1<=max_records<=500:
        raise ValueError('导出上限必须在 1–500 之间。')
    view,where,params,normalized = _query(filters,scope)
    column = DATE_COLUMNS[normalized['date_type']]
    with closing(runtime.connect_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT v.* FROM '+view+' v WHERE '+where+' ORDER BY v.'+column+' IS NULL,v.'+column+' DESC,v.id DESC LIMIT ?',
                            [*params,max_records+1]).fetchall()
        if len(rows)>max_records:
            raise ValueError('匹配记录超过导出上限，请缩小筛选范围。')
        return _dtos(conn,rows,scope)
