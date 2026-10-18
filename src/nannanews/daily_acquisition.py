"""Public listing collection; no scheduler starts on import and no publication side effects.

The bootstrap front page is a finite automatic extraction cohort. Older pagination
is raw history unless explicit publication or upcoming public-event metadata qualifies it.
Failed/uncertain model calls require an explicit editorial retry, not a daily retry.
"""
import errno
import hashlib
import fcntl
import json
import os
import re
import signal
import sqlite3
import threading
import tempfile
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from nannanews import acquisition
from nannanews import information
from nannanews import runtime
from nannanews import schema

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    BEIJING = ZoneInfo('Asia/Shanghai')
except ZoneInfoNotFoundError:
    BEIJING = timezone(timedelta(hours=8), 'Asia/Shanghai')

SEED_PATH = Path(__file__).resolve().parent / 'data' / 'automation_sources.json'
CATALOG_PATH = SEED_PATH.with_name('source_catalog.json')
COUNTS = ('discovered', 'fetched', 'extracted', 'skipped', 'failed')


def _validate_mode(mode):
    if mode not in ('new', 'history'):
        raise ValueError('Collection mode must be new or history.')


def _now(value=None):
    value = value if value is not None else datetime.now(BEIJING)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Collection clock must be timezone-aware.')
    return value.astimezone(BEIJING)


@contextmanager
def _db():
    with closing(runtime.connect_db()) as connection, connection:
        connection.row_factory = sqlite3.Row
        yield connection


def _migrate_schema(db):
    """Apply legacy schema/backfills only inside the explicit migration."""
    db.row_factory = sqlite3.Row
    schema.execute_script(db, '''
        CREATE TABLE IF NOT EXISTS daily_runs (
            id INTEGER PRIMARY KEY, actor TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT,
            status TEXT NOT NULL, result_json TEXT
        );
        CREATE TABLE IF NOT EXISTS daily_source_state (
            source_id TEXT PRIMARY KEY, definition_json TEXT NOT NULL,
            bootstrap_at TEXT, last_success_at TEXT,
            fetch_count INTEGER NOT NULL DEFAULT 0,
            config_json TEXT, bootstrap_key TEXT
        );
        CREATE TABLE IF NOT EXISTS daily_source_runs (
            run_id INTEGER NOT NULL REFERENCES daily_runs(id),
            source_id TEXT NOT NULL, result_json TEXT NOT NULL,
            PRIMARY KEY(run_id, source_id)
        );
        CREATE TABLE IF NOT EXISTS daily_pages (
            source_id TEXT NOT NULL, url TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', history INTEGER NOT NULL,
            first_seen TEXT NOT NULL, checked_at TEXT, attempt_run INTEGER,
            PRIMARY KEY(source_id, url)
        );
        CREATE TABLE IF NOT EXISTS daily_links (
            id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, url TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', eligible INTEGER NOT NULL,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            checked_at TEXT, attempt_run INTEGER, document_id INTEGER,
            message TEXT NOT NULL DEFAULT '', UNIQUE(source_id, url)
        );
        CREATE TABLE IF NOT EXISTS daily_model_attempts (
            document_id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_cursor (
            id INTEGER PRIMARY KEY CHECK(id=1), last_source_id TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS daily_links_work
            ON daily_links(state, attempt_run, first_seen);
        CREATE INDEX IF NOT EXISTS daily_pages_work
            ON daily_pages(source_id, state, first_seen);
    ''')
    if 'fetch_count' not in {row[1] for row in db.execute('PRAGMA table_info(daily_source_state)')}:
        db.execute('ALTER TABLE daily_source_state ADD COLUMN fetch_count INTEGER NOT NULL DEFAULT 0')
    for table, fields in (
        ('daily_source_state', {'config_json': 'TEXT', 'bootstrap_key': 'TEXT',
                                'page_fetch_count': 'INTEGER NOT NULL DEFAULT 0'}),
        ('daily_pages', {'definition_key': "TEXT NOT NULL DEFAULT ''", 'frontier': 'INTEGER NOT NULL DEFAULT 0'}),
        ('daily_links', {'definition_key': "TEXT NOT NULL DEFAULT ''", 'priority_date': 'TEXT', 'priority_kind': 'TEXT', 'processed_at': 'TEXT'}),
        ('daily_runs', {'mode': "TEXT NOT NULL DEFAULT 'legacy'"}),
        ('daily_source_state', {'history_success_at': 'TEXT'}),
    ):
        present = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        for name, ddl in fields.items():
            if name not in present:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')
    for row in db.execute('SELECT source_id,definition_json,bootstrap_at,bootstrap_key FROM daily_source_state').fetchall():
        key = _definition_key(json.loads(row['definition_json']))
        for table in ('daily_pages', 'daily_links'):
            db.execute(f"UPDATE {table} SET definition_key=? WHERE source_id=? AND definition_key=''", (key, row['source_id']))
        if row['bootstrap_at'] and not row['bootstrap_key']:
            db.execute('UPDATE daily_source_state SET bootstrap_key=? WHERE source_id=?', (key, row['source_id']))
    # Legacy raw retention used a pending row plus this exact success marker.
    # Repair it before classifying errors, including rows an earlier migration
    # already misclassified. Retained documents alone never reverse a failure.
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'source_documents' in tables:
        paid = '''EXISTS(SELECT 1 FROM daily_model_attempts a
            WHERE a.document_id=daily_links.document_id)'''
        if 'source_document_model_attempts' in tables:
            paid += ''' OR EXISTS(SELECT 1 FROM source_document_model_attempts a
                WHERE a.document_id=daily_links.document_id)'''
        retained_raw = '''EXISTS(SELECT 1 FROM source_documents d
            WHERE d.id=daily_links.document_id AND d.source_url=daily_links.url
                AND d.model_status='not_extracted' AND (TRIM(d.content_text)!='' OR
                    CASE WHEN json_valid(d.document_json) THEN
                        json_type(d.document_json,'$.media')='array'
                        AND json_array_length(d.document_json,'$.media')>0
                    ELSE 0 END))'''
        db.execute(f'''UPDATE daily_links SET state='history_deferred',
            processed_at=COALESCE(processed_at,checked_at)
            WHERE eligible=0 AND state IN ('pending','ready','fetch_failed')
                AND checked_at IS NOT NULL AND message=?
                AND EXISTS(SELECT 1 FROM daily_runs r
                    WHERE r.id=daily_links.attempt_run AND r.mode='legacy')
                AND {retained_raw} AND NOT ({paid})''',
            ('启动前历史原文已保存；不自动付费回填。',))
        db.execute(f'''UPDATE daily_links SET state=CASE
            (SELECT model_status FROM source_documents d WHERE d.id=daily_links.document_id)
            WHEN 'completed' THEN 'done' WHEN 'failed' THEN 'model_failed'
            WHEN 'not_extracted' THEN
                CASE WHEN {paid} THEN 'model_uncertain'
                    WHEN {retained_raw} THEN 'history_deferred' ELSE state END
            ELSE 'model_uncertain' END
            WHERE eligible=0 AND state IN ('pending','ready') AND document_id IS NOT NULL
                AND message='' AND EXISTS(SELECT 1 FROM source_documents d WHERE d.id=daily_links.document_id)''')
    db.execute("UPDATE daily_links SET state='fetch_failed' WHERE state='pending' AND checked_at IS NOT NULL AND message!=''")
    attempted = '''(SELECT MAX(finished_at) FROM source_document_model_attempts a
        WHERE a.document_id=daily_links.document_id AND a.finished_at IS NOT NULL)''' if 'source_document_model_attempts' in tables else 'NULL'
    captured = '''CASE WHEN state='history_deferred' THEN
        (SELECT fetched_at FROM source_documents d WHERE d.id=daily_links.document_id) END''' if 'source_documents' in tables else 'NULL'
    db.execute(f'''UPDATE daily_links SET processed_at=CASE
        WHEN state='fetch_failed' THEN checked_at
        WHEN state='history_deferred' THEN COALESCE({captured},checked_at)
        ELSE COALESCE({attempted},checked_at) END
        WHERE processed_at IS NULL AND state IN
            ('done','history_deferred','model_failed','model_uncertain','fetch_failed')''')


def _definition_key(definition):
    identity = {key: definition.get(key) for key in ('url', 'kind', 'publisher_college',
        'identity_url', 'expected_identity', 'article_pattern', 'detail_path', 'scan_all_pages')}
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _private_definitions():
    target = runtime.settings.data_dir / 'automation_sources.json'
    if target.is_symlink():
        raise ValueError('automation_sources.json must not be a symlink.')
    if not target.exists():
        descriptor, temporary = tempfile.mkstemp(prefix='.automation-sources-', dir=runtime.settings.data_dir)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(SEED_PATH.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass  # An operator/concurrent initializer supplied the canonical file.
        finally:
            Path(temporary).unlink(missing_ok=True)
    with os.fdopen(os.open(target, os.O_RDONLY | os.O_NOFOLLOW), encoding='utf-8') as stream:
        definitions = json.load(stream)
    if not isinstance(definitions, list):
        raise ValueError('automation_sources.json must contain an array of definitions.')
    identities = set()
    for definition in definitions:
        if not isinstance(definition, dict) or not isinstance(definition.get('id'), str) or not definition['id'].strip():
            raise ValueError('Every source definition requires a nonempty id.')
        identity = definition['id'].strip()
        if identity in identities:
            raise ValueError('Source definition ids must be unique.')
        identities.add(identity)
    return definitions


def _configuration(definition):
    value = {key: item for key, item in definition.items() if key not in
        ('activate_on_validation', 'definition_key', 'configuration_error', 'configuration_removed')}
    value['id'] = value['id'].strip()
    value.setdefault('kind', 'html')
    value.setdefault('enabled', False)
    value.setdefault('publisher_college', None)
    if isinstance(value['publisher_college'], str):
        value['publisher_college'] = value['publisher_college'].strip() or None
    return value


def _validate_definition(definition):
    if not isinstance(definition.get('name'), str) or not definition['name'].strip():
        raise ValueError('Source definition requires a name.')
    if type(definition['enabled']) is not bool or definition['kind'] not in ('html', 'nju_job'):
        raise ValueError('Source definition enabled/kind is invalid.')
    definition['url'] = acquisition.canonical_url(definition.get('url'))
    definition['identity_url'] = acquisition.canonical_url(definition.get('identity_url'))
    if urlsplit(definition['url']).hostname != urlsplit(definition['identity_url']).hostname:
        raise ValueError('Publisher identity and listing hosts must match.')
    if not isinstance(definition.get('expected_identity'), str) or not definition['expected_identity'].strip():
        raise ValueError('Source definition requires an expected publisher identity.')
    college = definition['publisher_college']
    if college is not None and (not isinstance(college, str) or college not in definition['expected_identity']):
        raise ValueError('Publisher college must match the expected identity.')
    if definition['kind'] == 'html':
        if not isinstance(definition.get('article_pattern'), str) or not definition['article_pattern']:
            raise ValueError('HTML source definition requires an article pattern.')
        re.compile(definition['article_pattern'])
    else:
        endpoints = {'/api/career/content/informations': '/career/info/',
                     '/api/career/job/fair/specifics': '/career/specifics/'}
        parts = urlsplit(definition['url'])
        if parts.hostname != 'job.nju.edu.cn' or endpoints.get(parts.path) != definition.get('detail_path'):
            raise ValueError('Job source endpoint/detail mapping is not supported.')
    return definition


def initialize_sources():
    """Copy the seed once, then reconcile the canonical private operator file.

    Unchanged file fields never overwrite registry edits. Explicit file disable,
    removal and edits take effect; identity changes require fresh public validation.
    Documents, queues and success history are retained, including replaced scopes.
    """
    schema.require_schema()
    definitions = _private_definitions()
    existing = {source['id']: source for source in information.list_sources()}
    with _db() as db:
        states = {row['source_id']: dict(row) for row in db.execute('SELECT * FROM daily_source_state')}
    configured = set()
    fields = ('name', 'url', 'publisher_college', 'kind', 'enabled')
    for raw_definition in definitions:
        config = _configuration(raw_definition)
        identity = config['id']
        configured.add(identity)
        source = existing.get(identity)
        state = states.get(identity)
        old_definition = json.loads(state['definition_json']) if state else {}
        previous = json.loads(state['config_json']) if state and state['config_json'] else _configuration(old_definition) if old_definition else {}
        definition = dict(config)
        try:
            _validate_definition(definition)
            error_message = None
        except (ValueError, acquisition.AcquisitionError, re.error) as error:
            error_message = _safe_message(error) if isinstance(error, acquisition.AcquisitionError) else '来源配置无效：' + str(error)
            definition['configuration_error'] = error_message
        changed = config != previous
        identity_changed = bool(state) and _definition_key(definition) != _definition_key(old_definition)
        activation = bool(definition['enabled']) and (source is None or
            (changed and config['enabled'] != previous.get('enabled')) or
            bool(source and source['enabled']) or
            bool(changed and source and source['status'] in ('pending_validation', 'failed', 'unsupported')) or
            bool(old_definition.get('activate_on_validation') and source and source['status'] in ('pending_validation', 'failed')))
        if source is None:
            # Invalid URLs cannot be entered into the shared canonical registry.
            # Existing invalid definitions below are disabled and visibly failed.
            source = information.register_source({key: definition.get(key) for key in fields if key != 'enabled'} |
                {'id': identity, 'enabled': False, 'status': 'failed' if error_message else 'pending_validation',
                 'message': error_message or '公开列表身份和文章链接尚待运行时验证。'})
            existing[identity] = source
        else:
            updates = {key: definition.get(key) for key in fields if key != 'enabled' and
                       changed and config.get(key) != previous.get(key)}
            if error_message:
                # Preserve the last valid registry URL/metadata; retain the invalid
                # edited definition in collector state, without fetching its old URL.
                updates = {'enabled': False, 'status': 'failed', 'message': error_message}
                activation = False
            elif not definition['enabled']:
                updates.update(enabled=False, message='私有来源配置已禁用自动采集。')
                activation = False
            elif identity_changed or (changed and config['enabled'] != previous.get('enabled')):
                updates.update(enabled=False, status='pending_validation', message='私有来源定义已变更，需重新验证发布者与文章列表。')
            if updates:
                source = information.update_source(identity, **updates)
                existing[identity] = source
        if not error_message and (source['url'] != definition['url'] or source['kind'] != definition['kind'] or
                source.get('publisher_college') != definition.get('publisher_college')):
            source = information.update_source(identity, enabled=False, status='unsupported', message='注册来源身份已变更，未匹配私有来源定义；未验证，停止自动采集。')
            existing[identity] = source
            activation = False
        definition['activate_on_validation'] = activation and not error_message
        definition['definition_key'] = _definition_key(definition)
        with _db() as db:
            db.execute('''INSERT INTO daily_source_state(source_id,definition_json,config_json) VALUES (?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET definition_json=excluded.definition_json,config_json=excluded.config_json''',
                (identity, json.dumps(definition, ensure_ascii=False), json.dumps(config, ensure_ascii=False)))
    for identity, state in states.items():
        if identity in configured or identity not in existing:
            continue
        definition = json.loads(state['definition_json'])
        definition.update(enabled=False, activate_on_validation=False, configuration_removed=True)
        previous = json.loads(state['config_json']) if state['config_json'] else _configuration(definition)
        previous['enabled'] = False
        information.update_source(identity, enabled=False, status='pending_validation', message='来源已从私有配置移除；保留历史与队列，停止自动采集。')
        with _db() as db:
            db.execute('UPDATE daily_source_state SET definition_json=?,config_json=? WHERE source_id=?',
                       (json.dumps(definition, ensure_ascii=False), json.dumps(previous, ensure_ascii=False), identity))
    catalog_path = runtime.settings.data_dir / 'source_catalog.json'
    if not catalog_path.exists():
        catalog_path = CATALOG_PATH
    if catalog_path.exists():
        for item in json.loads(catalog_path.read_text(encoding='utf-8')):
            for index, url in enumerate(item.get('source_urls') or []):
                if not isinstance(url, str):
                    continue
                identity = f"historical-{item['id']}-{index}"
                if identity in existing:
                    continue
                existing[identity] = information.register_source({'id': identity, 'name': '历史参考：' + item['name'],
                    'url': url, 'kind': 'html', 'enabled': False, 'status': 'unsupported',
                    'message': '历史文章/PDF/个人参考链接不是已验证发布者列表；未自动采集，不按目录名推定发布者。'})
    return information.list_sources()


@contextmanager
def _collection_lock():
    """An OS lock prevents concurrent CLI/workers and dies with the process."""
    path = runtime.settings.data_dir / '.daily-acquisition.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EACCES):
                raise RuntimeError('A daily collection is already running.') from None
            raise
        yield
    finally:
        os.close(fd)


def _safe_message(error):
    # Acquisition errors are deliberately safe for operators; unexpected exception
    # payloads can contain provider credentials/configuration and are never returned.
    if isinstance(error, acquisition.AcquisitionError):
        return str(error)[:500]
    return '采集失败：' + type(error).__name__ + '；请检查私有运行日志。'


def _recover(stamp):
    with _db() as db:
        db.execute("UPDATE daily_runs SET status='interrupted', finished_at=? WHERE status='running'", (stamp,))
        db.execute("INSERT OR IGNORE INTO daily_model_attempts(document_id,run_id,status) SELECT document_id,attempt_run,'uncertain' FROM daily_links WHERE state='modelling' AND document_id IS NOT NULL AND attempt_run IS NOT NULL")
        db.execute("UPDATE daily_model_attempts SET status='uncertain' WHERE status='attempted'")
        db.execute("UPDATE daily_links SET state='pending', message='上次抓取中断，等待恢复。' WHERE state='fetching'")
        db.execute("UPDATE daily_links SET state='model_uncertain',processed_at=COALESCE(processed_at,?), message='模型请求可能已发送；禁止自动重试，请人工检查。' WHERE state='modelling'", (stamp,))
        db.execute("UPDATE daily_pages SET state='pending' WHERE state='fetching'")


def _text(body, content_type):
    charset = re.search(r'charset=([\w-]+)', content_type, re.I)
    try:
        return body.decode(charset.group(1) if charset else 'utf-8', errors='strict')
    except (LookupError, UnicodeError):
        raise acquisition.AcquisitionError('来源列表编码不可读取。', 'source_blocked') from None


def _soup(url, expected_host, *, preserve_scripts=False):
    body, mime, resolved = acquisition.fetch_public_resource(url)
    if urlsplit(resolved).hostname != expected_host or 'html' not in mime.lower():
        raise acquisition.AcquisitionError('发布者列表重定向到不同主机或不是 HTML。', 'source_identity')
    soup = BeautifulSoup(_text(body, mime), 'html.parser')
    for node in soup(['style', 'noscript'] if preserve_scripts else ['script', 'style', 'noscript']):
        node.decompose()
    if re.search(r'环境异常|请完成.{0,8}验证|仅限校园网|当前IP.*非校园|访问过于频繁', soup.get_text(' ', strip=True)):
        raise acquisition.AcquisitionError('公开列表返回访问限制，未覆盖该来源。', 'source_blocked')
    return soup, resolved


def _validate_identity(definition):
    identity_url = acquisition.canonical_url(definition['identity_url'])
    host = urlsplit(definition['url']).hostname
    if urlsplit(identity_url).hostname != host:
        raise acquisition.AcquisitionError('发布者身份页与列表主机不一致。', 'source_identity')
    soup, _ = _soup(identity_url, host)
    identity = ' '.join([soup.title.get_text(' ', strip=True) if soup.title else ''] +
        [str(node.get('content', '')) for node in soup.select('meta[name="keywords"], meta[name="description"]')] +
        [str(node.get('title', '')) for node in soup.select('a[title]')])
    if definition['expected_identity'] not in identity:
        raise acquisition.AcquisitionError('公开页面不符合预期发布者身份，未启用覆盖。', 'source_identity')


def _html_listing(definition, page_url):
    soup, resolved = _soup(page_url, urlsplit(definition['url']).hostname, preserve_scripts=True)
    pattern = re.compile(definition['article_pattern'])
    links, pages = {}, []
    for anchor in soup.select('a[href]'):
        href = anchor.get('href', '').strip()
        if not href or href.startswith(('#', 'javascript:', 'java:', 'mailto:')):
            continue
        try:
            url = acquisition.canonical_url(urljoin(resolved, href))
        except acquisition.AcquisitionError:
            continue
        if urlsplit(url).hostname != urlsplit(definition['url']).hostname:
            continue
        if pattern.fullmatch(urlsplit(url).path):
            links[url] = {'url': url, 'published_date': None, 'disallowed': False}
        label = anchor.get_text('', strip=True).replace(' ', '').lower()
        if ('next' in (anchor.get('rel') or []) or re.search(r'下[一]?页|next', label)) and url != page_url:
            pages.append(url)
    # Boshan publishes all listing pages as literal JSON alongside its template.
    # Decode the data only; never execute the template, scripts or pagination JS.
    if soup.select_one('#ajaxpage-list') is not None and soup.select_one('#ajaxpage-template') is not None:
        payloads = []
        try:
            for script in soup.find_all('script'):
                raw = script.string or ''
                assignment = re.search(r'\bvar\s+dataList\s*=\s*', raw)
                if assignment:
                    start = assignment.end()
                    data, end = json.JSONDecoder().raw_decode(raw[start:])
                    tail = raw[start + end:].lstrip()
                    if (tail and not tail.startswith(';')) or not isinstance(data, list):
                        raise ValueError
                    payloads.append(data)
            if len(payloads) != 1:
                raise ValueError
            for page in payloads[0]:
                if not isinstance(page, dict) or not isinstance(page.get('infolist'), list):
                    raise ValueError
                for record in page['infolist']:
                    if not isinstance(record, dict) or not isinstance(record.get('url'), str):
                        raise ValueError
                    try:
                        url = acquisition.canonical_url(urljoin(resolved, record['url']))
                    except acquisition.AcquisitionError:
                        continue
                    if (urlsplit(url).hostname != urlsplit(definition['url']).hostname
                            or not pattern.fullmatch(urlsplit(url).path)):
                        continue
                    # Only a matching publisher article needs publication metadata;
                    # unrelated navigation/external links cannot block this source.
                    published = record.get('releasetime')
                    if type(published) is not int:
                        raise ValueError
                    published = datetime.fromtimestamp(published / 1000, BEIJING).date().isoformat()
                    links[url] = {'url': url, 'published_date': published, 'disallowed': False}
        except (ValueError, TypeError, OverflowError, OSError):
            raise acquisition.AcquisitionError('公开页面内嵌文章列表/日期结构不可读取，未执行来源脚本。', 'source_empty') from None
    # Only sequential next links are followed: tail/numeric links need not enqueue
    # thousands of archives at startup; the next page itself is persisted immediately.
    if not links:
        raise acquisition.AcquisitionError('公开页面没有符合已验证栏目模式的文章列表。', 'source_empty')
    return list(links.values()), list(dict.fromkeys(pages))


def _api_time(value):
    """Validate explicit ISO API metadata, using the publisher's local timezone.

    Date-only values retain calendar precision; timestamps retain time precision.
    Invalid optional metadata supplies no eligibility or priority evidence.
    """
    if not isinstance(value, str):
        return None
    try:
        if len(value) == 10:
            parsed = date.fromisoformat(value)
            return parsed if parsed.isoformat() == value else None
        if len(value) < 16 or value[10] not in ('T', ' '):
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.date().isoformat() != value[:10]:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BEIJING)
        return parsed.astimezone(BEIJING)
    except (ValueError, OverflowError):
        return None


def _eligible(default, published, bootstrap, clock, event_start=None):
    """Explicit publisher dates override cohort eligibility, except upcoming events."""
    if isinstance(event_start, str):
        event_start = _api_time(event_start)
    if (event_start >= clock if isinstance(event_start, datetime) else
            event_start >= clock.date() if event_start is not None else False):
        return True
    published = _api_time(published)
    if isinstance(published, datetime):
        published = published.date()
    if published is not None and bootstrap:
        return published >= _now(datetime.fromisoformat(bootstrap)).date()
    # A known expired event cannot fall back to an undated homepage cohort.
    return bool(default) if event_start is None else False


def _job_listing(definition, page_url):
    body, mime, resolved = acquisition.fetch_public_resource(page_url)
    if urlsplit(resolved).hostname != 'job.nju.edu.cn' or 'json' not in mime.lower():
        raise acquisition.AcquisitionError('就业门户列表不是预期公开 JSON。', 'source_identity')
    try:
        data = json.loads(_text(body, mime))
        content, page = data['content'], data['page']
        number, total = page['number'], page['totalPages']
        if not isinstance(content, list) or type(number) is not int or type(total) is not int or number < 0 or total < 0:
            raise ValueError
        links = []
        for record in content:
            identity = record['id']
            if not isinstance(identity, (str, int)) or isinstance(identity, bool):
                raise ValueError
            identity = str(identity)
            if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]*', identity):
                raise ValueError
            published = _api_time(record.get('publishedAt') or record.get('publishAt'))
            if isinstance(published, datetime):
                published = published.date()
            event_start = _api_time(record.get('startAt'))
            disallowed = (record.get('loginRequired') is True or record.get('enabled') is False or
                          record.get('status', 'PUBLISHED') != 'PUBLISHED')
            links.append({'url': 'https://job.nju.edu.cn' + definition['detail_path'] + identity,
                          'published_date': published.isoformat() if published else None,
                          'event_start': event_start, 'disallowed': disallowed})
        pages = []
        if number + 1 < total:
            parts = urlsplit(page_url)
            query = [(key, str(number + 1) if key == 'page' else value) for key, value in parse_qsl(parts.query, keep_blank_values=True)]
            pages.append(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), '')))
        return links, pages
    except (ValueError, KeyError, TypeError):
        raise acquisition.AcquisitionError('就业门户公开列表/分页结构不符合已验证 API。', 'source_empty') from None


def _definition(source):
    with _db() as db:
        row = db.execute('SELECT definition_json FROM daily_source_state WHERE source_id=?', (source['id'],)).fetchone()
    if row is None:
        return None
    definition = json.loads(row[0])
    if definition.get('configuration_error') or definition.get('configuration_removed'):
        return definition
    # Never apply a seed's verified publisher to an operator-replaced endpoint.
    if (acquisition.canonical_url(source['url']) != acquisition.canonical_url(definition['url']) or
            source['kind'] != definition['kind'] or source.get('publisher_college') != definition.get('publisher_college')):
        return None
    return definition


def _enqueue_page(db, source_id, url, stamp, history, definition_key, refresh=False):
    db.execute('''INSERT INTO daily_pages(source_id,url,history,first_seen,definition_key) VALUES (?,?,?,?,?)
        ON CONFLICT(source_id,url) DO UPDATE SET
        state=CASE WHEN daily_pages.definition_key!=excluded.definition_key THEN 'pending' ELSE daily_pages.state END,
        history=CASE WHEN daily_pages.definition_key!=excluded.definition_key THEN excluded.history ELSE daily_pages.history END,
        frontier=CASE WHEN daily_pages.definition_key!=excluded.definition_key THEN 0 ELSE daily_pages.frontier END,
        definition_key=excluded.definition_key''', (source_id, url, int(history), stamp, definition_key))
    if refresh:
        db.execute("UPDATE daily_pages SET state='pending', history=MIN(history,?) WHERE source_id=? AND url=? AND state!='fetching'",
                   (int(history), source_id, url))


def _discover(source, definition, run_id, stamp, page_limit, result, mode='new', deadline=None):
    source_id = source['id']
    clock = _now(datetime.fromisoformat(stamp))
    _validate_identity(definition)
    with _db() as db:
        state = db.execute('SELECT bootstrap_at,bootstrap_key,page_fetch_count FROM daily_source_state WHERE source_id=?', (source_id,)).fetchone()
        key = definition['definition_key']
        bootstrap = state[0] if state['bootstrap_key'] == key else None
        continuation_count = state['page_fetch_count']
        root = db.execute('SELECT checked_at FROM daily_pages WHERE source_id=? AND url=? AND definition_key=?',
                          (source_id, definition['url'], key)).fetchone()
        if root and root['checked_at'] and _now(datetime.fromisoformat(root['checked_at'])).date() != clock.date():
            # Tomorrow's entries may be inserted just beyond an unchanged root.
            # A new local day reroots current discovery, retaining the old frontier
            # as ordinary backlog and keeping the cumulative archive reservation.
            db.execute('UPDATE daily_pages SET frontier=0 WHERE source_id=? AND definition_key=?', (source_id, key))
        _enqueue_page(db, source_id, definition['url'], stamp, False, key, refresh=True)
    # The root is mandatory. A persisted current frontier advances within a day,
    # even with page_limit=1; every third continuation reserves the older backlog.
    # Only current-frontier descendants inherit priority, never archive-slot links.
    root_verified = False
    for _ in range(page_limit + 1):
        if deadline is not None and _now() >= deadline:
            result.update(status='partial', message='临近每日采集时段，历史任务停止提交并保留待处理队列。')
            break
        with _db() as db:
            page_scope = '' if mode == 'history' or definition.get('scan_all_pages') else 'AND history=0'
            frontier_order = 'frontier,' if continuation_count % 3 == 2 else 'frontier DESC,'
            page = db.execute(f"SELECT * FROM daily_pages WHERE source_id=? AND definition_key=? AND state='pending' {page_scope} AND (attempt_run IS NULL OR attempt_run!=?) ORDER BY url!=?,{frontier_order}history,checked_at IS NOT NULL,checked_at,first_seen,url LIMIT 1", (source_id, key, run_id, definition['url'])).fetchone()
            if page is None:
                break
            db.execute("UPDATE daily_pages SET state='fetching', attempt_run=? WHERE source_id=? AND url=?", (run_id, source_id, page['url']))
        try:
            listing = _job_listing if definition['kind'] == 'nju_job' else _html_listing
            links, next_pages = listing(definition, page['url'])
        except Exception as error:
            with _db() as db:
                db.execute("UPDATE daily_pages SET state='pending' WHERE source_id=? AND url=?", (source_id, page['url']))
            if not root_verified:
                raise
            result['failed'] += 1
            result.update(status='partial', message='后续分页读取失败；已验证首页和已发现原文继续处理。 ' + _safe_message(error))
            break
        with _db() as db:
            advance_frontier = bool(page['frontier']) or (page['url'] == definition['url'] and
                db.execute("SELECT 1 FROM daily_pages WHERE source_id=? AND definition_key=? AND frontier=1 AND state='pending' LIMIT 1", (source_id, key)).fetchone() is None)
            new_links = 0
            boundary = False
            for link in links:
                prior = db.execute('''SELECT l.*,d.source_published_date,d.model_status,
                    EXISTS(SELECT 1 FROM daily_model_attempts a WHERE a.document_id=l.document_id)
                    OR EXISTS(SELECT 1 FROM source_document_model_attempts a WHERE a.document_id=l.document_id) AS paid,
                    CASE WHEN l.state='pending' AND l.message='' AND d.source_url=l.url THEN
                        TRIM(d.content_text)!='' OR CASE WHEN json_valid(d.document_json) THEN
                            json_type(d.document_json,'$.media')='array'
                            AND json_array_length(d.document_json,'$.media')>0
                        ELSE 0 END
                    ELSE 0 END AS retained_pending
                    FROM daily_links l LEFT JOIN source_documents d ON d.id=l.document_id
                    WHERE l.source_id=? AND l.url=?''', (source_id, link['url'])).fetchone()
                # A known URL is the incremental pagination frontier, including
                # previously raw-only history; only explicit metadata qualifies it.
                if prior and prior['definition_key'] == key:
                    boundary = True
                cohort = not page['history'] and (page['url'] == definition['url'] or not boundary)
                if prior and prior['definition_key'] == key:
                    cohort = bool(prior['eligible'])
                event_start = link.get('event_start')
                if event_start is None and prior and prior['priority_kind'] == 'event':
                    event_start = _api_time(prior['priority_date'])
                published = link['published_date'] or (prior['source_published_date'] if prior else None)
                if not published and prior and prior['priority_kind'] == 'publication':
                    published = prior['priority_date']
                eligible = _eligible(cohort, published, bootstrap or stamp, clock, event_start)
                # Explicit listing evidence can qualify retained unpaid raw for its
                # first extraction, even if its queue row still says pending. Reuse
                # that document directly; startup no longer normalizes these rows.
                promote = bool(mode == 'new' and prior and eligible
                    and (prior['state'] == 'history_deferred' or prior['retained_pending'])
                    and (link.get('event_start') is not None or link['published_date'])
                    and prior['document_id'] and not prior['paid']
                    and prior['model_status'] == 'not_extracted')
                if prior and not prior['eligible'] and prior['state'] in (
                        'done', 'history_deferred', 'model_failed', 'model_uncertain', 'fetch_failed') and not promote:
                    eligible = False
                priority_date = event_start.isoformat() if event_start is not None else published
                priority_kind = 'event' if event_start is not None else 'publication' if priority_date else None
                if prior is None:
                    new_links += 1
                    state_name = 'disallowed' if link['disallowed'] else 'pending'
                    message = 'API 标为非公开/需登录，未读取详情。' if link['disallowed'] else ''
                    db.execute('INSERT INTO daily_links(source_id,url,state,eligible,first_seen,last_seen,message,definition_key,priority_date,priority_kind) VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (source_id, link['url'], state_name, int(eligible), stamp, stamp, message, key, priority_date, priority_kind))
                    result['discovered'] += 1
                else:
                    if link['disallowed']:
                        db.execute("UPDATE daily_links SET state='disallowed',eligible=?,last_seen=?,definition_key=?,priority_date=COALESCE(?,priority_date),priority_kind=COALESCE(?,priority_kind),message='API 标为非公开/需登录，未读取详情。' WHERE id=?", (int(eligible), stamp, key, priority_date, priority_kind, prior[0]))
                    else:
                        db.execute("UPDATE daily_links SET last_seen=?,definition_key=?,priority_date=COALESCE(?,priority_date),priority_kind=COALESCE(?,priority_kind),eligible=?,state=CASE WHEN ? THEN 'ready' WHEN state='disallowed' THEN 'pending' ELSE state END,processed_at=CASE WHEN ? THEN NULL ELSE processed_at END WHERE id=?", (stamp, key, priority_date, priority_kind, int(eligible), int(promote), int(promote), prior[0]))
            # A known incremental page is the discovery frontier. Bootstrap/archive
            # pagination is always resumed, bounded per run and visible as backlog.
            if bootstrap is None or page['history'] or new_links or definition.get('scan_all_pages'):
                for url in next_pages:
                    history = bool(definition.get('scan_all_pages')) or bootstrap is None or bool(page['history']) or boundary
                    _enqueue_page(db, source_id, url, stamp, history, key,
                                  refresh=bool(definition.get('scan_all_pages')) or (bootstrap is not None and not history))
                    if advance_frontier and url != definition['url']:
                        db.execute("UPDATE daily_pages SET frontier=1 WHERE source_id=? AND url=? AND definition_key=? AND state='pending'", (source_id, url, key))
            db.execute("UPDATE daily_pages SET state='done',frontier=0,checked_at=? WHERE source_id=? AND url=?", (stamp, source_id, page['url']))
            if page['url'] != definition['url']:
                continuation_count += 1
                db.execute('UPDATE daily_source_state SET page_fetch_count=? WHERE source_id=?', (continuation_count, source_id))
            if bootstrap is None:
                bootstrap = stamp
                db.execute('UPDATE daily_source_state SET bootstrap_at=?,bootstrap_key=? WHERE source_id=?', (stamp, key, source_id))
        if page['url'] == definition['url']:
            root_verified = True
    information.update_source(source_id, enabled=True, status='verified', message='公开发布者身份与文章列表已验证。', last_checked_at=stamp)


def _capture(link, actor, stamp, run_id, result):
    with _db() as db:
        db.execute("UPDATE daily_links SET state='fetching',attempt_run=? WHERE id=?", (run_id, link['id']))
    try:
        document = acquisition.fetch_document(link['url'])
        # A safe public redirect does not establish publisher ownership.
        if urlsplit(document['resolved_url']).hostname != urlsplit(link['url']).hostname:
            raise acquisition.AcquisitionError('文章跳转到其他发布者，未归属当前来源。', 'source_identity')
        saved = information.save_document(document, source_id=link['source_id'], actor=actor)
        result['fetched'] += 1
        published = document.get('source_published_date')
        if not published and link['priority_kind'] == 'publication':
            published = link['priority_date']
        with _db() as db:
            db.execute('BEGIN IMMEDIATE')
            status = db.execute('SELECT model_status FROM source_documents WHERE id=?', (saved['id'],)).fetchone()[0]
            prior_attempt = db.execute('SELECT status FROM daily_model_attempts WHERE document_id=?', (saved['id'],)).fetchone()
            scope = db.execute('SELECT bootstrap_at FROM daily_source_state WHERE source_id=? AND bootstrap_key=?',
                               (link['source_id'], link['definition_key'])).fetchone()
            eligible = _eligible(link['eligible'], published, scope['bootstrap_at'] if scope else None,
                                 _now(datetime.fromisoformat(stamp)),
                                 link['priority_date'] if link['priority_kind'] == 'event' else None)
            priority_date = None
            if link['priority_kind'] != 'event':
                parsed = _api_time(published)
                if parsed is not None:
                    priority_date = parsed.date().isoformat() if isinstance(parsed, datetime) else parsed.isoformat()
            if not saved.get('capture_complete', True):
                capture_error = saved.get('capture_error') or {}
                state = 'fetch_failed'
                message = capture_error.get('message') or '网页原文已留存，但图片证据未完整读取；未调用模型。'
                result['failed'] += 1
            elif status == 'completed':
                state, message = 'done', ''
                result['skipped'] += 1
            elif status in ('failed', 'extracting') or prior_attempt is not None:
                state, message = ('model_failed' if status == 'failed' else 'model_uncertain'), '模型失败或请求结果未确认，需人工处理，未自动重试。'
                result['skipped'] += 1
            elif not eligible:
                state, message = 'history_deferred', '启动前历史原文已保存；不自动付费回填。'
            else:
                state, message = 'ready', ''
            terminal_at = _now().isoformat() if state != 'ready' else None
            db.execute('''UPDATE daily_links SET state=?,checked_at=?,message=?,eligible=?,
                processed_at=CASE WHEN document_id=? THEN COALESCE(processed_at,?) ELSE ? END,
                document_id=?,priority_date=COALESCE(?,priority_date),
                priority_kind=CASE WHEN ? IS NOT NULL THEN 'publication' ELSE priority_kind END WHERE id=?''',
                (state, stamp, message, int(eligible), saved['id'], terminal_at, terminal_at,
                 saved['id'], priority_date, priority_date, link['id']))
    except Exception as error:
        result['failed'] += 1
        with _db() as db:
            db.execute("UPDATE daily_links SET state='fetch_failed',checked_at=?,processed_at=COALESCE(processed_at,?),message=? WHERE id=?",
                       (stamp, _now().isoformat(), _safe_message(error), link['id']))


def _enrich(link_id, document_id, actor, run_id, result):
    document = information.get_document(document_id)
    prepared_media = None
    if document is not None and document['model_status'] == 'not_extracted':
        try:
            prepared_media = acquisition.preflight_document_media(document)
        except acquisition.AcquisitionError as error:
            # Preparing an unsupported image is not a paid model attempt.
            message = '原文已留存，模型图片准备失败；未发送模型请求。 ' + _safe_message(error)
            with _db() as db:
                changed = db.execute("""UPDATE daily_links SET state='fetch_failed',attempt_run=?,
                    processed_at=COALESCE(processed_at,?),message=?
                    WHERE id=? AND document_id=? AND state='ready'""",
                    (run_id, _now().isoformat(), message, link_id, document_id)).rowcount
            result['failed'] += changed
            return
    with _db() as db:
        db.execute('BEGIN IMMEDIATE')
        stored = db.execute('''SELECT l.*,d.source_published_date,d.model_status,s.bootstrap_at,r.started_at
            FROM daily_links l JOIN source_documents d ON d.id=l.document_id
            JOIN daily_runs r ON r.id=? LEFT JOIN daily_source_state s
                ON s.source_id=l.source_id AND s.bootstrap_key=l.definition_key
            WHERE l.id=? AND d.id=?''', (run_id, link_id, document_id)).fetchone()
        prior_attempt = db.execute('SELECT status FROM daily_model_attempts WHERE document_id=?', (document_id,)).fetchone()
        if stored['model_status'] == 'completed':
            db.execute("UPDATE daily_links SET state='done',attempt_run=?,processed_at=COALESCE(processed_at,?),message='' WHERE id=?", (run_id, _now().isoformat(), link_id))
            result['skipped'] += 1
            return
        if stored['model_status'] in ('failed', 'extracting') or prior_attempt is not None:
            state = 'model_failed' if stored['model_status'] == 'failed' else 'model_uncertain'
            db.execute("UPDATE daily_links SET state=?,attempt_run=?,processed_at=COALESCE(processed_at,?),message='模型失败或请求结果未确认，需人工处理，未自动重试。' WHERE id=?",
                       (state, run_id, _now().isoformat(), link_id))
            result['skipped'] += 1
            return
        published = stored['source_published_date']
        if not published and stored['priority_kind'] == 'publication':
            published = stored['priority_date']
        eligible = _eligible(stored['eligible'], published, stored['bootstrap_at'],
                             _now(datetime.fromisoformat(stored['started_at'])),
                             stored['priority_date'] if stored['priority_kind'] == 'event' else None)
        db.execute('UPDATE daily_links SET eligible=? WHERE id=?', (int(eligible), link_id))
        if not eligible:
            db.execute("UPDATE daily_links SET state='history_deferred',attempt_run=?,processed_at=COALESCE(processed_at,?),message='启动前历史原文已保存；不自动付费回填。' WHERE id=?", (run_id, _now().isoformat(), link_id))
            return
    claimed = False

    def reserve_before_send(conn):
        nonlocal claimed
        # Join information's admission transaction AFTER durable audit preparation.
        # It commits both attempt ledgers before returning to the network transport.
        source = conn.execute('SELECT model_status FROM source_documents WHERE id=?',
                              (document_id,)).fetchone()
        source_attempt = conn.execute('SELECT 1 FROM source_document_model_attempts WHERE document_id=? LIMIT 1',
                                      (document_id,)).fetchone()
        if source is None or source[0] != 'not_extracted' or source_attempt is not None:
            # Manual enrichment may have paid after our preliminary eligibility read.
            # Its failed/uncertain history never authorizes an automatic retry.
            raise acquisition.AcquisitionError('原文已有模型尝试或不再适合自动提取；未重复发送请求。', 'model_in_progress', 409)
        inserted = conn.execute("INSERT OR IGNORE INTO daily_model_attempts(document_id,run_id,status) VALUES (?,?,'attempted')",
                                (document_id, run_id)).rowcount
        if not inserted:
            raise acquisition.AcquisitionError('该原文已有模型请求记录；不自动重复请求。', 'model_in_progress', 409)
        conn.execute("UPDATE daily_links SET state='modelling',attempt_run=? WHERE id=?", (run_id, link_id))
        claimed = True

    try:
        enriched = information.enrich_document(document_id, actor=actor, prepared_media=prepared_media,
                                               before_send=reserve_before_send)
        if enriched['model_status'] != 'completed':
            raise acquisition.AcquisitionError('模型未完成，原文已保留且不自动重试。', 'model_incomplete')
        result['extracted' if enriched['created'] else 'skipped'] += 1
        with _db() as db:
            db.execute("UPDATE daily_links SET state='done',attempt_run=?,processed_at=?,message='' WHERE document_id=? AND (attempt_run=? OR id=?) AND state IN ('ready','modelling','model_uncertain')", (run_id, _now().isoformat(), document_id, run_id, link_id))
            if claimed:
                db.execute("UPDATE daily_model_attempts SET status='done' WHERE document_id=? AND run_id=?", (document_id, run_id))
    except Exception as error:
        with _db() as db:
            attempt = db.execute('SELECT run_id FROM daily_model_attempts WHERE document_id=?',
                                 (document_id,)).fetchone()
            if claimed and attempt is not None and attempt['run_id'] == run_id:
                result['failed'] += 1
                db.execute("UPDATE daily_links SET state='model_failed',processed_at=?,message=? WHERE document_id=? AND attempt_run=? AND state IN ('ready','modelling','model_uncertain')", (_now().isoformat(), _safe_message(error), document_id, run_id))
                db.execute("UPDATE daily_model_attempts SET status='failed' WHERE document_id=?", (document_id,))
            else:
                status = db.execute('SELECT model_status FROM source_documents WHERE id=?',
                                    (document_id,)).fetchone()['model_status']
                guarded = (attempt is not None or status in {'completed', 'failed', 'extracting'}
                           or getattr(error, 'code', None) == 'model_in_progress')
                if guarded:
                    state = 'done' if status == 'completed' else 'model_failed' if status == 'failed' else 'model_uncertain'
                    result['skipped'] += 1
                    message = '' if state == 'done' else '模型已有请求或结果未确认；未重复发送请求。'
                else:
                    state = 'fetch_failed'
                    result['failed'] += 1
                    message = '原文已留存，模型请求准备失败；未发送请求。 ' + _safe_message(error)
                db.execute("UPDATE daily_links SET state=?,attempt_run=?,processed_at=COALESCE(processed_at,?),message=? WHERE id=? AND document_id=?",
                           (state, run_id, _now().isoformat(), message, link_id, document_id))


def _backlog(db, source_id, mode='new'):
    link_scope = '' if mode == 'legacy' else ' AND eligible=' + ('0' if mode == 'history' else '1')
    states = {row[0]: row[1] for row in db.execute(f'''SELECT
        CASE WHEN state='pending' AND checked_at IS NOT NULL AND message!=''
            THEN 'fetch_failed' ELSE state END AS outcome,COUNT(*)
        FROM daily_links WHERE source_id=? {link_scope} GROUP BY outcome''', (source_id,))}
    source_state = db.execute('SELECT definition_json FROM daily_source_state WHERE source_id=?', (source_id,)).fetchone()
    definition = json.loads(source_state[0]) if source_state else {}
    page_scope = '' if mode == 'legacy' or definition.get('scan_all_pages') else ' AND history=' + ('1' if mode == 'history' else '0')
    paused = 0
    columns = {row[1] for row in db.execute('PRAGMA table_info(daily_links)')}
    if definition.get('definition_key') and 'definition_key' in columns:
        key = definition['definition_key']
        removed = int(bool(definition.get('configuration_removed')))
        paused = db.execute(f"SELECT COUNT(*) FROM daily_links WHERE source_id=? {link_scope} AND state IN ('pending','fetch_failed','fetching','ready','modelling') AND (definition_key!=? OR ?)", (source_id, key, removed)).fetchone()[0]
        paused += db.execute(f"SELECT COUNT(*) FROM daily_pages WHERE source_id=? {page_scope} AND state!='done' AND (definition_key!=? OR ?)", (source_id, key, removed)).fetchone()[0]
    return {
        'pending': sum(states.get(state, 0) for state in ('pending', 'fetching', 'ready', 'modelling')),
        'pending_pages': db.execute(f"SELECT COUNT(*) FROM daily_pages WHERE source_id=? {page_scope} AND state!='done'", (source_id,)).fetchone()[0],
        'history_deferred': states.get('history_deferred', 0),
        'model_failures': states.get('model_failed', 0) + states.get('model_uncertain', 0),
        'fetch_failures': states.get('fetch_failed', 0),
        'disallowed': states.get('disallowed', 0),
        'paused_by_configuration': paused,
    }


def _source_result(source):
    return {'source_id': source['id'], 'status': 'pending', 'message': '',
            **{key: 0 for key in COUNTS}, 'pending': 0, 'pending_pages': 0,
            'last_success_at': source.get('last_success_at')}


def _finish_source(source, run_id, stamp, result, mode='new'):
    checkpoint = 'history_success_at' if mode == 'history' else 'last_success_at'
    with _db() as db:
        result.update(_backlog(db, source['id'], mode))
        state = db.execute(f'SELECT {checkpoint} FROM daily_source_state WHERE source_id=?', (source['id'],)).fetchone()
        if state:
            result['last_success_at'] = state[0]
        if result['status'] == 'pending':
            if result['failed'] or result['model_failures'] or result['fetch_failures'] or result['disallowed']:
                result['status'] = 'partial'
                result['message'] = '存在抓取失败、模型失败/未确认或禁止访问的记录；未推进成功检查点。'
            elif result['pending'] or result['pending_pages']:
                result['status'] = 'partial'
                result['message'] = '当前模式仍有分页/抓取待处理；历史原文不自动付费回填。'
            else:
                result['status'] = 'complete' if result['extracted'] or (mode == 'history' and result['fetched']) else 'no_change'
                result['message'] = '本轮发现范围已处理。' if result['status'] == 'complete' else '本轮未发现新内容；不代表全部校园来源完整覆盖。'
                db.execute(f'UPDATE daily_source_state SET {checkpoint}=? WHERE source_id=?', (stamp, source['id']))
                result['last_success_at'] = stamp
        if result['status'] == 'partial' and result['paused_by_configuration']:
            result['message'] += ' 配置替换前的未完成队列已保留并暂停，不以新发布者抓取旧范围。'
        db.execute('INSERT OR REPLACE INTO daily_source_runs(run_id,source_id,result_json) VALUES (?,?,?)',
                   (run_id, source['id'], json.dumps(result, ensure_ascii=False)))
    if mode == 'new' and result['status'] in ('complete', 'no_change'):
        information.update_source(source['id'], last_success_at=stamp)


def _run_result(run_id, stamp, results, finished=None, interrupted=False, mode='new'):
    gaps = [row['source_id'] + ': ' + row['message'] for row in results if row['status'] not in ('complete', 'no_change')]
    if not results:
        gaps.append('没有配置可用来源；未完成校园信息覆盖。')
    if interrupted:
        status = 'interrupted'
    elif not any(row['status'] in ('complete', 'no_change', 'partial') for row in results):
        status = 'failed'
    elif any(row['status'] not in ('complete', 'no_change') for row in results):
        status = 'partial'
    elif not any(row['extracted'] or (mode == 'history' and row['fetched']) for row in results):
        status = 'no_change'
    else:
        status = 'complete'
    return {'id': run_id, 'mode': mode, 'status': status, 'started_at': stamp,
            'finished_at': finished, 'sources': results, 'coverage_gaps': gaps}


def _pipeline(contexts, actor, run_id, stamp, page_limit, mode):
    """Keep queues in SQLite; bound web futures independently of two model workers."""
    contexts = deque(contexts)
    today = _now(datetime.fromisoformat(stamp)).date().isoformat()
    deadline = None
    if mode == 'history':
        clock = _now()
        hour, minute = map(int, runtime.settings.collection_time.split(':'))
        slot = clock.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if clock > slot + timedelta(minutes=5):
            slot += timedelta(days=1)
        deadline = slot - timedelta(minutes=5)
    with _db() as db:
        cursor = db.execute('SELECT last_source_id FROM daily_cursor WHERE id=1').fetchone()
        upcoming = {(row['source_id'], row['definition_key']): row['priority_date']
                    for row in db.execute("""SELECT source_id,definition_key,MIN(priority_date) AS priority_date
                        FROM daily_links WHERE eligible=1 AND state IN ('pending','fetch_failed','ready')
                        AND priority_kind='event' AND (priority_date>=? OR priority_date=?)
                        GROUP BY source_id,definition_key""", (stamp, today))}
    for context in contexts:
        # A persisted event only orders discovery; fresh identity/root checks still gate it.
        context['event_hint'] = upcoming.get((context['source']['id'], context['definition']['definition_key']))
    if cursor:
        for index, context in enumerate(contexts):
            if context['source']['id'] == cursor[0]:
                contexts.rotate(-index - 1)
                break
    priority_order = """CASE WHEN priority_kind='event' AND (priority_date>=? OR priority_date=?) THEN 0
        WHEN priority_kind='event' THEN 2 ELSE 1 END,
        CASE WHEN priority_kind='event' AND (priority_date>=? OR priority_date=?) THEN priority_date END,
        CASE WHEN priority_kind IS NULL OR priority_kind!='event'
            THEN COALESCE(priority_date,substr(first_seen,1,10)) END DESC"""
    priority_args = (stamp, today, stamp, today)
    fetch_workers = runtime.settings.collection_fetch_workers
    limiter = acquisition.PublicFetchLimiter(max_concurrent=fetch_workers)
    web = ThreadPoolExecutor(max_workers=fetch_workers, thread_name_prefix='daily-web')
    models = ThreadPoolExecutor(max_workers=2, thread_name_prefix='daily-model')
    futures = {}
    hosts = set()
    active_links = set()
    prefer_capture = True

    def task(kind, context, link):
        delta = {key: 0 for key in COUNTS}
        error = None
        try:
            if kind == 'model':
                _enrich(link['id'], link['document_id'], actor, run_id, delta)
            else:
                with limiter.scope():
                    if kind == 'discover':
                        _discover(context['source'], context['definition'], run_id, stamp, page_limit, delta, mode, deadline)
                    else:
                        _capture(link, actor, stamp, run_id, delta)
        except BaseException as caught:
            # A worker interrupt must reach the coordinator, including its counters.
            error = caught
        return delta, error

    def complete(future):
        kind, context, link, host = futures.pop(future)
        if host is not None:
            hosts.remove(host)
        if link is not None:
            active_links.remove(link['id'])
        if future.cancelled():
            return None
        delta, error = future.result()
        for key in COUNTS:
            context['result'][key] += delta[key]
        if kind == 'discover':
            if isinstance(error, Exception):
                result = context['result']
                result.update(status='failed', message=_safe_message(error))
                result['failed'] += 1
                information.update_source(context['source']['id'], status='failed',
                                          message=result['message'], last_checked_at=stamp)
                context['phase'] = 'failed'
                return None
            if error is None:
                for key in ('status', 'message'):
                    if key in delta:
                        context['result'][key] = delta[key]
                # Each source freezes independently after its bounded discovery.
                with _db() as db:
                    context['end'] = db.execute('SELECT COALESCE(MAX(id),0) FROM daily_links WHERE source_id=?',
                                                (context['source']['id'],)).fetchone()[0]
                context['phase'] = 'ready'
        return error

    def candidate(context, model=False):
        if context['phase'] != 'ready':
            return None
        excluded = ','.join('?' for _ in active_links) or '0'
        scope = (context['source']['id'], context['definition']['definition_key'], context['end'])
        with _db() as db:
            if mode == 'history':
                if model or context['history'] >= page_limit:
                    return None
                return db.execute("""SELECT * FROM daily_links WHERE source_id=? AND definition_key=?
                    AND id<=? AND state='pending' AND eligible=0 AND document_id IS NULL
                    AND checked_at IS NULL AND (attempt_run IS NULL OR attempt_run!=?)
                    ORDER BY first_seen,id LIMIT 1""", (*scope, run_id)).fetchone()
            if model:
                capturing = tuple(link['id'] for kind, _, link, _ in futures.values() if kind == 'capture')
                reserved = ','.join('?' for _ in capturing) or '0'
                link = db.execute(f"""SELECT * FROM daily_links WHERE source_id=? AND definition_key=?
                    AND id<=? AND ((state='ready' AND id NOT IN ({excluded})) OR
                        (id IN ({reserved}) AND state IN ('pending','fetch_failed','fetching','ready')) OR
                        ((attempt_run IS NULL OR attempt_run!=?) AND eligible=1 AND
                         (state IN ('pending','fetch_failed') OR
                           (last_seen=? AND state IN ('done','model_failed','model_uncertain')))))
                    ORDER BY {priority_order},id LIMIT 1""",
                    (*scope, *active_links, *capturing, run_id, stamp, *priority_args)).fetchone()
                return link if link is not None and link['state'] == 'ready' and link['id'] not in active_links else None
            # Current refetches detect genuinely changed content, but history is never
            # admitted just because its row was inserted during this run.
            return db.execute(f"""SELECT * FROM daily_links WHERE source_id=? AND definition_key=?
                AND id<=? AND (attempt_run IS NULL OR attempt_run!=?) AND eligible=1
                AND (state IN ('pending','fetch_failed') OR
                    (last_seen=? AND state IN ('done','model_failed','model_uncertain')))
                ORDER BY {priority_order},id LIMIT 1""",
                (*scope, run_id, stamp, *priority_args)).fetchone()

    def submit(kind, context, link=None):
        host = None if kind == 'model' else urlsplit(
            context['definition']['url'] if link is None else link['url']).hostname
        if link is not None:
            with _db() as db:
                db.execute('UPDATE daily_links SET attempt_run=? WHERE id=?', (run_id, link['id']))
                db.execute('INSERT INTO daily_cursor(id,last_source_id) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET last_source_id=excluded.last_source_id',
                           (context['source']['id'],))
                if mode == 'history' and kind == 'capture':
                    db.execute('UPDATE daily_source_state SET fetch_count=fetch_count+1 WHERE source_id=?',
                               (context['source']['id'],))
                    context['history'] += 1
            active_links.add(link['id'])
        if host is not None:
            hosts.add(host)
        if kind == 'discover':
            context['phase'] = 'discovering'
        future = (models if kind == 'model' else web).submit(task, kind, context, link)
        futures[future] = kind, context, link, host
        while contexts[0] is not context:
            contexts.rotate(-1)
        contexts.rotate(-1)

    try:
        while True:
            # Harvest before submitting, so worker BaseExceptions stop new work.
            for future in list(futures):
                if future.done():
                    error = complete(future)
                    if error is not None:
                        raise error
            if deadline is not None and _now() >= deadline:
                for context in contexts:
                    if context['result']['status'] == 'pending':
                        context['result'].update(status='partial', message='临近每日采集时段，历史任务停止提交并排空在途请求。')
                break
            model_count = sum(kind == 'model' for kind, _, _, _ in futures.values())
            while model_count < 2:
                ready = next(((context, link) for context in contexts
                              if (link := candidate(context, model=True)) is not None), None)
                if ready is None:
                    break
                submit('model', *ready)
                model_count += 1
            web_count = len(futures) - model_count
            while web_count < fetch_workers:
                if deadline is not None and _now() >= deadline:
                    for context in contexts:
                        if context['result']['status'] == 'pending':
                            context['result'].update(status='partial', message='临近每日采集时段，历史任务停止提交并排空在途请求。')
                    break
                choices = []
                for context in contexts:
                    if context['phase'] == 'pending':
                        choices.append(('discover', context, None, urlsplit(context['definition']['url']).hostname))
                    elif (link := candidate(context)) is not None:
                        choices.append(('capture', context, link, urlsplit(link['url']).hostname))
                eligible = any(link is not None and link['eligible'] for _, _, link, _ in choices)
                available = [choice for choice in choices
                             if choice[3] not in hosts and (not eligible or choice[2] is None or choice[2]['eligible'])]
                discovery = min((choice for choice in available if choice[0] == 'discover'),
                                key=lambda choice: (choice[1]['event_hint'] is None, choice[1]['event_hint'] or ''),
                                default=None)
                capture = next((choice for choice in available if choice[0] == 'capture'), None)
                choice = (capture or discovery) if prefer_capture else (discovery or capture)
                if choice is None:
                    break
                submit(*choice[:3])
                prefer_capture = choice[0] == 'discover'
                web_count += 1
            if futures:
                wait(futures, return_when=FIRST_COMPLETED)
            else:
                break
    finally:
        # Keep the OS collection lock and running status until every active task has
        # persisted its outcome. In particular, never recover a still-live paid call.
        for future in futures:
            future.cancel()
        while futures:
            try:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
            except BaseException:
                continue
            for future in done:
                complete(future)
        web.shutdown(wait=True, cancel_futures=True)
        models.shutdown(wait=True, cancel_futures=True)


def run_once(*, actor='system:daily', now=None, mode='new'):
    """Run current discovery/extraction, or a separate bounded raw-history pass.

    Historical passes never submit a model call or revisit a terminal raw outcome.
    Current passes retain first-extraction and changed-version paid-claim guards.
    """
    _validate_mode(mode)
    clock = _now(now)
    stamp = clock.isoformat()
    page_limit = runtime.settings.collection_page_limit
    if page_limit < 1:
        raise ValueError('Collection page limit must be positive.')
    with _collection_lock():
        initialize_sources()
        _recover(stamp)
        with _db() as db:
            run_id = db.execute("INSERT INTO daily_runs(actor,started_at,status,mode) VALUES (?,?,'running',?)", (actor, stamp, mode)).lastrowid
        sources = information.list_sources()
        results = [_source_result(source) for source in sources]
        interrupted = True
        try:
            contexts = []
            for source, result in zip(sources, results):
                try:
                    definition = _definition(source)
                    activation = (definition and definition.get('activate_on_validation') and
                                  source.get('status') in ('pending_validation', 'failed'))
                    if source.get('status') == 'unsupported':
                        result.update(status='unsupported', message=source.get('message') or '没有支持的公开列表。')
                    elif definition and definition.get('configuration_error'):
                        result.update(status='failed', message=definition['configuration_error'])
                    elif definition and (definition.get('configuration_removed') or not definition['enabled']):
                        result.update(status='disabled', message=source.get('message') or '私有来源配置已禁用自动采集。')
                    elif not source['enabled'] and not activation:
                        result.update(status='disabled', message='操作员已禁用自动采集。')
                    elif definition is None:
                        result.update(status='unsupported', message='缺少与当前 URL 匹配的发布者身份/列表定义，未进行网络或模型调用。')
                    else:
                        contexts.append({'source': source, 'result': result, 'definition': definition,
                                         'phase': 'pending', 'history': 0})
                except Exception as error:
                    result.update(status='failed', message=_safe_message(error))
                    result['failed'] += 1
                    information.update_source(source['id'], status='failed', message=result['message'], last_checked_at=stamp)
            _pipeline(contexts, actor, run_id, stamp, page_limit, mode)
            for source, result in zip(sources, results):
                _finish_source(source, run_id, stamp, result, mode)
            interrupted = False
        finally:
            if interrupted:
                # KeyboardInterrupt/SystemExit and SIGTERM retain exactly which paid
                # calls are uncertain; safe fetch stages can resume on the next run.
                _recover(stamp)
                for source, result in zip(sources, results):
                    if result['status'] == 'pending':
                        result.update(status='partial', message='进程中断；队列已保存，未确认模型请求不会自动重试。')
                    _finish_source(source, run_id, stamp, result, mode)
            finished = _now().isoformat()
            result = _run_result(run_id, stamp, results, finished, interrupted, mode)
            with _db() as db:
                db.execute('UPDATE daily_runs SET status=?,finished_at=?,result_json=? WHERE id=?',
                           (result['status'], finished, json.dumps(result, ensure_ascii=False), run_id))
        return result


@contextmanager
def _readonly_db():
    # Accessors must not create a database or apply even an otherwise safe migration.
    if runtime.settings.database_path.is_symlink():
        raise runtime.ConfigurationError('database.db must not be a symlink')
    with closing(schema.open_database(readonly=True)) as db:
        schema.require_schema(db)
        db.row_factory = sqlite3.Row
        yield db


def get_status(mode='new'):
    """Read only the selected queue/run; old mixed runs remain visibly legacy."""
    _validate_mode(mode)
    empty = {'mode': mode, 'last_run': None,
             'last_runs': {'new': None, 'history': None, 'legacy': None},
             'sources': [], 'pending': 0, 'coverage_gaps': ['尚未运行当前模式采集。']}
    with _readonly_db() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_runs'").fetchone() is None:
            return empty
        columns = {row[1] for row in db.execute('PRAGMA table_info(daily_runs)')}
        runs = {}
        for run_mode in empty['last_runs']:
            if 'mode' in columns:
                row = db.execute('SELECT * FROM daily_runs WHERE mode=? ORDER BY id DESC LIMIT 1', (run_mode,)).fetchone()
            else:
                row = db.execute('SELECT * FROM daily_runs ORDER BY id DESC LIMIT 1').fetchone() if run_mode == 'legacy' else None
            if row:
                value = json.loads(row['result_json']) if row['result_json'] else {
                    'id': row['id'], 'started_at': row['started_at'], 'finished_at': row['finished_at'],
                    'sources': [], 'coverage_gaps': ['运行中或进程中断，队列保留。']}
                value.update(mode=run_mode, status=row['status'])
                runs[run_mode] = value
        last_run = runs.get(mode)
        source_rows = db.execute('SELECT result_json FROM daily_source_runs WHERE run_id=? ORDER BY source_id', (last_run['id'],)).fetchall() if last_run else []
        sources = [json.loads(row[0]) for row in source_rows]
        known = {row['source_id'] for row in sources}
        state_columns = {row[1] for row in db.execute('PRAGMA table_info(daily_source_state)')}
        checkpoint = 'last_success_at' if mode == 'new' else 'history_success_at'
        field = checkpoint if checkpoint in state_columns else 'NULL'
        for state in db.execute(f'SELECT source_id,{field} AS checkpoint FROM daily_source_state ORDER BY source_id'):
            if state['source_id'] not in known:
                sources.append({'source_id': state['source_id'], 'status': 'pending', 'message': '当前模式尚未采集。',
                    **{key: 0 for key in COUNTS}, 'last_success_at': state['checkpoint']})
        for source in sources:
            source.update(_backlog(db, source['source_id'], mode))
        if last_run:
            last_run['sources'] = sources
    gaps = [row['source_id'] + ': ' + row['message'] for row in sources if row['status'] not in ('complete', 'no_change')]
    return {**empty, 'last_run': last_run, 'last_runs': {**empty['last_runs'], **runs}, 'sources': sources,
            'pending': sum(row['pending'] + row['pending_pages'] for row in sources),
            'coverage_gaps': gaps if sources else empty['coverage_gaps']}


def list_items(mode='history', source_id=None, state=None, page=1, per_page=20):
    """Paginate safe source-bound ledger fields without initializing schema."""
    _validate_mode(mode)
    page, per_page = max(1, int(page)), min(100, max(1, int(per_page)))
    empty = {'items': [], 'total': 0, 'page': page, 'pages': 0, 'per_page': per_page}
    with _readonly_db() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_links'").fetchone() is None:
            return empty
        columns = {row[1] for row in db.execute('PRAGMA table_info(daily_links)')}
        processed = 'l.processed_at' if 'processed_at' in columns else 'NULL'
        normalized = """CASE
            WHEN l.state='pending' AND l.checked_at IS NOT NULL AND l.message!='' THEN 'fetch_failed'
            WHEN l.state='done' AND COALESCE(d.model_status,'')!='completed' THEN 'model_uncertain'
            ELSE l.state END"""
        where = ['l.eligible=?']
        args = [0 if mode == 'history' else 1]
        if source_id:
            where.append('l.source_id=?')
            args.append(source_id)
        if state:
            where.append(f'({normalized})=?')
            args.append(state)
        joined = 'FROM daily_links l LEFT JOIN source_documents d ON d.id=l.document_id LEFT JOIN information_sources s ON s.id=l.source_id'
        predicate = ' AND '.join(where)
        total = db.execute(f'SELECT COUNT(*) {joined} WHERE {predicate}', args).fetchone()[0]
        rows = db.execute(f'''SELECT l.id,l.source_id,COALESCE(s.name,l.source_id) AS source_name,
            COALESCE(d.title,'') AS title,l.url,l.document_id,
            (SELECT MIN(r.id) FROM information_records_view r WHERE r.document_id=l.document_id) AS record_id,
            {normalized} AS state,{processed} AS processed_at,l.checked_at,l.message,
            l.eligible,d.fetched_at {joined} WHERE {predicate}
            ORDER BY l.id DESC LIMIT ? OFFSET ?''', (*args, per_page, (page - 1) * per_page)).fetchall()
    labels = {'done': ('completed', '提取成功'),
              'history_deferred': ('history_deferred', '原文留存成功、未提取'),
              'fetch_failed': ('fetch_failed', '读取失败'),
              'model_failed': ('model_failed', '提取失败'),
              'model_uncertain': ('model_uncertain', '结果未确认'),
              'pending': ('pending', '待处理'), 'ready': ('pending', '待提取'),
              'fetching': ('running', '读取中'), 'modelling': ('running', '提取中'),
              'disallowed': ('disallowed', '禁止读取')}
    items = []
    for row in rows:
        item = dict(row)
        item['is_history'] = not bool(item.pop('eligible'))
        fetched_at = item.pop('fetched_at')
        item['processing_status'], item['processing_label'] = labels.get(item['state'], ('model_uncertain', '结果未确认'))
        if not item['processed_at'] and item['state'] in ('done', 'history_deferred', 'fetch_failed', 'model_failed', 'model_uncertain'):
            item['processed_at'] = item['checked_at'] or (fetched_at if item['state'] == 'history_deferred' else None)
        items.append(item)
    return {**empty, 'items': items, 'total': total, 'pages': (total + per_page - 1) // per_page}


def next_run_at(now=None):
    """Next configured wall-clock slot, always aware Beijing time."""
    clock = _now(now)
    hour, minute = map(int, runtime.settings.collection_time.split(':'))
    slot = clock.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return slot if slot >= clock else slot + timedelta(days=1)


def _worker_due(clock):
    next_slot = next_run_at(clock)
    latest_slot = next_slot if next_slot <= clock else next_slot - timedelta(days=1)
    with _db() as db:
        last = db.execute("SELECT started_at FROM daily_runs WHERE mode='new' AND status NOT IN ('running','interrupted') ORDER BY id DESC LIMIT 1").fetchone()
        newest = db.execute("SELECT status FROM daily_runs WHERE mode='new' ORDER BY id DESC LIMIT 1").fetchone()
    if newest and newest[0] in ('running', 'interrupted'):
        return True
    if last:
        return _now(datetime.fromisoformat(last[0])) < latest_slot
    # A brand-new deployment before today's time waits; never replays yesterday
    # simply because the registry was initialized on startup.
    return latest_slot.date() == clock.date()


def run_worker():
    """Foreground once/day worker with SIGTERM wakeup and persistent catch-up."""
    if not runtime.settings.collection_enabled:
        raise RuntimeError('Daily scheduler is disabled (NANNA_COLLECTION_ENABLED).')
    schema.require_schema()
    condition = threading.Condition()
    stopped = False

    def stop(signum, frame):
        nonlocal stopped
        stopped = True
        with condition:
            condition.notify_all()
        raise KeyboardInterrupt

    previous = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, stop)
    print('daily acquisition scheduler ready', flush=True)
    try:
        while not stopped:
            clock = _now()
            if _worker_due(clock):
                try:
                    result = run_once(now=clock, mode='new')
                    print(json.dumps({'run_id': result['id'], 'mode': result['mode'], 'status': result['status']}, ensure_ascii=False), flush=True)
                except RuntimeError as error:
                    # A concurrent manual run owns the OS lock; do not spin.
                    print(_safe_message(error), flush=True)
                    with condition:
                        condition.wait(timeout=60)
                    continue
            delay = max(1, (next_run_at(_now()) - _now()).total_seconds())
            with condition:
                condition.wait(timeout=min(delay, 3600))
    except KeyboardInterrupt:
        pass
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
