"""Private append-only corrections, registrations and evidence associations.

Only explicitly configured runtime/public/query directories are opened. Reads
never initialize or migrate, fetch web sources, call models, or publish drafts.
"""
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

from nannanews import runtime, schema
from nannanews.acquisition import AcquisitionError, canonical_url
from nannanews.static_export import (
    StaticExportError, _existing_manifest, _json, _manifest_files, _no_symlinks, _public_url,
)

_HASH = re.compile(r'[0-9a-f]{64}\Z')
_ITEM = re.compile(r'item-[0-9a-f]{24}\Z')
_LIMITS = {'title': 250, 'summary': 1500, 'content': 60000, 'category': 200,
           'time': 1000, 'venue': 500, 'organizer': 500, 'audience': 1500,
           'participation': 3000}
_PROBLEMS = frozenset({'needs_review', 'conflict', 'unconfirmed', 'unreviewed', 'legacy', 'source_corrected', 'unresolved_dates'})
_DATE_KINDS = frozenset({'event', 'deadline', 'registration', 'application'})


class ReviewError(ValueError):
    status_code = 400


class ReviewUnavailable(ReviewError):
    status_code = 503


class ReviewNotFound(ReviewError):
    status_code = 404


class ReviewValidationError(ReviewError):
    status_code = 400


class ReviewConflict(ReviewError):
    status_code = 409


def _migrate_schema(db):
    schema.execute_script(db, '''
        CREATE TABLE IF NOT EXISTS reader_review_revisions (
            id INTEGER PRIMARY KEY,
            item_id TEXT NOT NULL,
            revision TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            actor TEXT NOT NULL,
            base_version TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            previous_head TEXT,
            payload_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS reader_review_item_head
            ON reader_review_revisions(item_id,id);
        CREATE TABLE IF NOT EXISTS reader_review_project (
            id INTEGER PRIMARY KEY CHECK(id=1),
            base_version TEXT NOT NULL,
            base_manifest_json TEXT NOT NULL,
            published_version TEXT);
        CREATE TABLE IF NOT EXISTS reader_review_publications (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            actor TEXT NOT NULL,
            base_version TEXT NOT NULL,
            version TEXT NOT NULL,
            revisions_json TEXT NOT NULL,
            result_json TEXT NOT NULL);
    ''')
    for table in ('reader_review_revisions', 'reader_review_publications'):
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.lower()}
                BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'reader review history is immutable'); END''')

def _migrate_private_schema(db):
    schema.execute_script(db, '''
        CREATE TABLE IF NOT EXISTS reader_private_activities (
            item_id TEXT PRIMARY KEY, activity_id TEXT NOT NULL UNIQUE,
            occurrence_id TEXT NOT NULL UNIQUE, source_id TEXT NOT NULL UNIQUE,
            registration_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
            actor TEXT NOT NULL, payload_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reader_private_evidence_links (
            id INTEGER PRIMARY KEY, item_id TEXT NOT NULL,
            review_id TEXT NOT NULL, dataset_id TEXT NOT NULL,
            actor TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(item_id,review_id,dataset_id));
    ''')
    for table in ('reader_private_activities', 'reader_private_evidence_links'):
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.lower()}
                BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'private registration history is immutable'); END''')



def validate_public_url(value):
    if (not isinstance(value, str) or re.search(r'[\x00-\x20\x7f\\]', value)
            or _public_url(value) is None):
        raise ReviewValidationError('来源链接必须是无账号密码的公开 HTTP(S) URL。')
    try:
        if '%' in (urlsplit(value).hostname or ''):
            raise ReviewValidationError('来源链接不能使用编码的主机名。')
    except ValueError:
        raise ReviewValidationError('来源链接必须是安全的公开 HTTP(S) URL。') from None
    try:
        clean = canonical_url(value)
        host = urlsplit(clean).hostname
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if ('.' not in host or host.endswith('.') or re.fullmatch(
                    r'(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+))*', host)):
                raise ReviewValidationError('来源链接不能指向本地或私有主机。')
        else:
            mapped = getattr(address, 'ipv4_mapped', None)
            if not address.is_global or (mapped is not None and not mapped.is_global):
                raise ReviewValidationError('来源链接不能指向本地或私有主机。')
        if _public_url(clean) is None:
            raise ReviewValidationError('来源链接不能指向本地或私有主机。')
        return clean
    except (AcquisitionError, ValueError) as exc:
        if isinstance(exc, ReviewValidationError):
            raise
        raise ReviewValidationError('来源链接必须是安全的公开 HTTP(S) URL。') from None


def validate_payload(payload):
    """Validate the entire editable contract without a network request."""
    if not isinstance(payload, dict) or set(payload) != {'status', 'reason', 'activity'}:
        raise ReviewValidationError('人工核查内容字段不完整或含有未知字段。')
    if payload['status'] not in ('verified', 'needs_review'):
        raise ReviewValidationError('请选择人工核实或待核对。')
    reason = payload['reason']
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000 or '\x00' in reason:
        raise ReviewValidationError('公开说明不能为空，且不能超过 2000 字符。')
    activity = payload['activity']
    if not isinstance(activity, dict) or set(activity) != set(_LIMITS) | {'source_urls', 'dates'}:
        raise ReviewValidationError('活动字段不完整或含有未知字段。')
    clean = {}
    for field, limit in _LIMITS.items():
        value = activity[field]
        if value is None and field not in {'title', 'summary', 'content'}:
            clean[field] = None
            continue
        if not isinstance(value, str) or len(value) > limit or '\x00' in value:
            raise ReviewValidationError(f'{field} 必须是文本且不能超过 {limit} 字符。')
        if field in {'title', 'content'} and not value.strip():
            raise ReviewValidationError(f'{field} 不能为空。')
        clean[field] = value if value.strip() or field == 'summary' else None
    urls, dates = activity['source_urls'], activity['dates']
    if not isinstance(urls, list) or len(urls) > 100:
        raise ReviewValidationError('来源链接最多 100 条。')
    clean['source_urls'] = list(dict.fromkeys(validate_public_url(value) for value in urls))
    if not isinstance(dates, list) or len(dates) > 100:
        raise ReviewValidationError('日期最多 100 条。')
    clean['dates'] = []
    seen = set()
    for entry in dates:
        if (not isinstance(entry, dict) or set(entry) != {'kind', 'date'}
                or not isinstance(entry['kind'], str) or entry['kind'] not in _DATE_KINDS):
            raise ReviewValidationError('日期字段必须包含有效的类型和 ISO 日期。')
        value = entry['date']
        try:
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError:
            raise ReviewValidationError('日期必须是真实的 YYYY-MM-DD 日期。') from None
        key = (entry['kind'], value)
        if key not in seen:
            seen.add(key)
            clean['dates'].append({'kind': key[0], 'date': key[1]})
    return {'status': payload['status'], 'reason': reason, 'activity': clean}


def _digest(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _actor(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 250 or any(ord(c) < 32 for c in value):
        raise ReviewValidationError('核查人身份无效。')
    return value


def _revision(row):
    if row is None:
        return None
    return {key: row[key] for key in ('item_id', 'revision', 'created_at', 'actor', 'base_version', 'fingerprint')} | {
        'payload': json.loads(row['payload_json'])}


class ReviewStore:
    def __init__(self, settings=None):
        self.settings = settings if settings is not None else runtime.settings

    def evidence(self):
        from nannanews.reader_evidence import EvidenceStore
        return EvidenceStore(self.settings)

    @staticmethod
    def _registrations(db):
        return {row['item_id']: dict(row) for row in db.execute('SELECT * FROM reader_private_activities')}

    def _private_headers(self, db):
        headers = {}
        for row in self.evidence().activity_headers():
            headers[row['activity_id']] = {'title': row['title'], 'content_status': row['review_status']}
        for identity, row in self._registrations(db).items():
            payload = json.loads(row['payload_json'])
            headers[identity] = {'title': payload['activity']['title'], 'content_status': 'needs_review'}
        return headers

    @staticmethod
    def _private_occurrence(identity, payload, occurrence_id):
        activity = payload['activity']
        return {'id': occurrence_id, 'item_id': identity, 'title': activity['title'],
                'preview': activity['summary'], 'body_text': activity['content'],
                'category': activity['category'], 'source_urls': activity['source_urls'],
                'dates': [], 'content_status': 'needs_review', 'images': [], 'primary_images': [],
                'private_registration': True}

    def _private_item(self, db, item_id, dataset=None):
        registered = db.execute('SELECT * FROM reader_private_activities WHERE item_id=?', (item_id,)).fetchone()
        if registered is not None:
            payload = json.loads(registered['payload_json'])
            occurrence_id = registered['occurrence_id']
        elif self.evidence().configured:
            activity, sources, dates = self.evidence().activity(item_id, dataset=dataset)
            fields = {key: activity.get('time_text' if key == 'time' else key) for key in _LIMITS}
            fields.update(title=activity.get('title') or '', summary=activity.get('summary') or '',
                          content='', source_urls=[], dates=[])
            # Canonical facts are not the original source/model body. Humans must
            # explicitly fill the publishable content before saving a new item.
            payload = {'status': 'needs_review', 'reason': '', 'activity': fields}
            occurrence_id = 'occ-' + hashlib.sha256(('private-registration:' + item_id).encode()).hexdigest()[:24]
        else:
            raise ReviewNotFound('活动不存在于公开数据或私有登记。')
        row = self._private_occurrence(item_id, payload, occurrence_id)
        return {'id': item_id, 'latest_occurrence_id': occurrence_id, 'private_payload': payload}, [row], [row]

    def register_activity(self, registration_key, payload, *, actor):
        """Explicit private registration; opaque caller identity, never title/date matching."""
        clean, actor = validate_payload(payload), _actor(actor)
        if (not isinstance(registration_key, str) or not 1 <= len(registration_key) <= 250
                or any(ord(char) < 32 for char in registration_key)):
            raise ReviewValidationError('请提供稳定、不含控制字符的私有登记标识。')
        digest = hashlib.sha256(('private-registration:' + registration_key).encode()).hexdigest()[:24]
        identity, occurrence_id, source_id = 'item-' + digest, 'occ-' + digest, 'source-' + digest
        with self._db(write=True) as db:
            base, _ = self._selection(db)
            items, _ = self._catalog(base)
            if identity in items or db.execute('SELECT 1 FROM reader_private_activities WHERE registration_key=?',
                                               (registration_key,)).fetchone():
                raise ReviewConflict('此登记标识已存在；请编辑原活动，不会新建重复身份。')
            if any(row['activity_id'] == identity for row in self.evidence().activity_headers()):
                raise ReviewConflict('此身份已在私有档案中存在；请编辑原活动。')
            created_at = datetime.now(timezone.utc).isoformat()
            db.execute('INSERT INTO reader_private_activities VALUES(?,?,?,?,?,?,?,?)',
                       (identity, identity, occurrence_id, source_id, registration_key, created_at, actor,
                        _json(clean).decode()))
        return {'item_id': identity, 'activity_id': identity, 'occurrence_id': occurrence_id,
                'source_id': source_id, 'created_at': created_at}

    def associate_review(self, item_id, review_id, *, actor, dataset=None):
        self.get_item(item_id, dataset=dataset)
        actor = _actor(actor)
        header = self.evidence().require_review(item_id, review_id, associated=True, dataset=dataset)
        dataset = header['dataset_id']
        with self._db(write=True) as db:
            created_at = datetime.now(timezone.utc).isoformat()
            db.execute('INSERT OR IGNORE INTO reader_private_evidence_links(item_id,review_id,dataset_id,actor,created_at) '
                       'VALUES(?,?,?,?,?)', (item_id, review_id, dataset, actor, created_at))
        return {'item_id': item_id, 'review_id': review_id, 'dataset_id': dataset}

    def review_associated(self, item_id, review_id, *, dataset=None):
        if dataset is None:
            dataset = self.evidence().resolve_review(review_id)['dataset_id']
        with self._db() as db:
            return db.execute('SELECT 1 FROM reader_private_evidence_links WHERE item_id=? AND review_id=? '
                              'AND dataset_id=?', (item_id, review_id, dataset)).fetchone() is not None

    def review_history_item(self, item_id, *, dataset=None):
        """Resolve history-page context from headers, never archived row bodies."""
        if not isinstance(item_id, str) or not _ITEM.fullmatch(item_id):
            raise ReviewNotFound('活动不存在。')
        with self._db() as db:
            base, _ = self._selection(db)
            items, grouped = self._catalog(base)
            head = db.execute("SELECT json_extract(payload_json,'$.activity.title') FROM reader_review_revisions "
                              'WHERE item_id=? ORDER BY id DESC LIMIT 1', (item_id,)).fetchone()
            registration = db.execute("SELECT json_extract(payload_json,'$.activity.title') "
                                      'FROM reader_private_activities WHERE item_id=?', (item_id,)).fetchone()
            if item_id in items:
                item, rows = items[item_id], grouped[item_id]
                latest = next((row for row in rows if row['id'] == item.get('latest_occurrence_id')), rows[0])
                title = head[0] if head else latest['title']
            elif head is not None or registration is not None:
                title = head[0] if head is not None else registration[0]
            else:
                header = self.evidence().activity_header(item_id, dataset=dataset)
                title, dataset = header['title'], header['dataset_id']
        return {'item_id': item_id, 'title': title, 'dataset_id': dataset}

    def list_reviews(self, item_id, *, page=1, dataset=None):
        activity = self.review_history_item(item_id, dataset=dataset)
        evidence = self.evidence()
        if not evidence.configured:
            raise ReviewUnavailable('未配置 NANNA_READER_EVIDENCE_DIR；不能证明历史核查不存在。')
        try:
            result = evidence.list_reviews(item_id, page=page)
        except ReviewNotFound:
            result = {'activity_id': item_id, 'items': [], 'total': 0, 'page': page, 'per_page': 30}
        with self._db() as db:
            links = [dict(row) for row in db.execute('SELECT review_id,dataset_id FROM reader_private_evidence_links '
                                                    'WHERE item_id=? ORDER BY id', (item_id,))]
        with evidence._db() as foundation:
            current_identity = evidence.identity(foundation)
        result['associated'] = [evidence.require_review(item_id, row['review_id'], associated=True)
                                for row in links if row['dataset_id'] == current_identity]
        result['activity'] = activity
        return result

    def _root(self):
        root = self.settings.reader_data_dir
        if root is None:
            raise ReviewUnavailable('请配置 NANNA_READER_DATA_DIR 指向公开 reader data 目录，然后显式迁移独立控制台运行库。')
        root = Path(root).absolute()
        try:
            _no_symlinks(root)
            _no_symlinks(self.settings.database_path)
            root = root.resolve()
            private = self.settings.data_dir.resolve()
            for public in (root, self.settings.reader_site_data_dir):
                if public is None:
                    continue
                public = Path(public).absolute()
                _no_symlinks(public)
                public = public.resolve()
                if (private.is_relative_to(public) or public.is_relative_to(private)
                        or self.settings.database_path.resolve().is_relative_to(public)):
                    raise ReviewUnavailable('控制台私有运行目录与公开数据目录必须相互独立。')
            if not root.is_dir():
                raise ReviewUnavailable('公开 reader data 目录不存在；请检查 NANNA_READER_DATA_DIR。')
        except StaticExportError:
            raise ReviewUnavailable('控制台与公开数据路径不能包含符号链接。') from None
        return root

    @contextmanager
    def _db(self, *, write=False):
        self._root()
        path = self.settings.database_path
        if not path.is_file():
            raise ReviewUnavailable('控制台数据库未初始化；请显式运行 nannanews migrate。')
        db = None
        try:
            db = sqlite3.connect(path.absolute().as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            schema.require_schema(db)
            if write:
                db.execute('BEGIN IMMEDIATE')
            else:
                db.execute('BEGIN')
            yield db
            if write:
                db.commit()
        except runtime.ConfigurationError as exc:
            raise ReviewUnavailable(str(exc)) from None
        except sqlite3.Error:
            raise ReviewUnavailable('控制台数据库不可用；请检查独立运行库和迁移状态。') from None
        finally:
            if db is not None:
                db.close()

    def _json_file(self, relative, *, maximum=64 * 1024 * 1024):
        root = self._root()
        if (not isinstance(relative, str) or not relative or '\\' in relative or '\x00' in relative
                or Path(relative).is_absolute() or '..' in Path(relative).parts):
            raise ReviewUnavailable('公开数据包含不安全的文件路径。')
        path = root / relative
        try:
            _no_symlinks(path)
            if not path.is_file() or path.stat().st_size > maximum:
                raise ReviewUnavailable('公开数据文件缺失或超过安全读取上限。')
            return json.loads(path.read_bytes())
        except (StaticExportError, OSError, ValueError):
            raise ReviewUnavailable('公开数据文件无效；请检查已发布的静态数据包。') from None

    def _active(self):
        try:
            manifest = _existing_manifest(self._root(), True)
            if not manifest or manifest['schema_version'] != 2:
                raise StaticExportError('Review source must be native v2.')
            return manifest
        except StaticExportError:
            raise ReviewUnavailable('公开数据 manifest 无效；请配置原生静态导出的 data 目录。') from None

    def _selection(self, db):
        active = self._active()
        project = db.execute('SELECT * FROM reader_review_project WHERE id=1').fetchone()
        if project and (active['data_version'] in (project['base_version'], project['published_version'])
                        or self._prepared(db, project['base_version'], active['data_version']) is not None):
            base = json.loads(project['base_manifest_json'])
        else:
            base = active
        return base, active

    @staticmethod
    def _events(db, phase):
        for row in db.execute('SELECT * FROM reader_review_publications ORDER BY id DESC'):
            result = json.loads(row['result_json'])
            if result.get('phase') == phase:
                yield row, result

    @classmethod
    def _prepared(cls, db, base_version, version):
        return next((row for row, _ in cls._events(db, 'prepared')
                     if row['base_version'] == base_version and row['version'] == version), None)

    @classmethod
    def _retirements(cls, db):
        retired = {}
        for row, result in cls._events(db, 'retired'):
            for item_id, revision in json.loads(row['revisions_json']).items():
                retired.setdefault((item_id, revision), {
                    'item_id': item_id, 'revision': revision, 'created_at': row['created_at'],
                    'actor': row['actor'], 'base_version': row['base_version'], 'reason': result['reason']})
        return retired

    @staticmethod
    def _freeze(db, base):
        db.execute('''INSERT INTO reader_review_project(id,base_version,base_manifest_json) VALUES(1,?,?)
            ON CONFLICT(id) DO UPDATE SET base_version=excluded.base_version,
            base_manifest_json=excluded.base_manifest_json,
            published_version=CASE WHEN reader_review_project.base_version=excluded.base_version
                THEN reader_review_project.published_version ELSE NULL END''',
                   (base['data_version'], _json(base).decode()))

    def _catalog(self, manifest):
        catalog = self._json_file(manifest['files']['catalog'])
        if (not isinstance(catalog, dict) or not isinstance(catalog.get('items'), list)
                or not isinstance(catalog.get('occurrences'), list)):
            raise ReviewUnavailable('公开目录格式无效。')
        items, records = {}, {}
        for item in catalog['items']:
            if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                    or not _ITEM.fullmatch(item['id']) or item['id'] in items):
                raise ReviewUnavailable('公开活动身份无效或重复。')
            expected = 'versions/' + manifest['data_version'] + '/items/' + item['id'][5:7] + '.json'
            if item.get('detail_path') != expected:
                raise ReviewUnavailable('公开活动分片路径无效。')
            items[item['id']] = item
            records[item['id']] = []
        occurrence_ids = set()
        for record in catalog['occurrences']:
            if (not isinstance(record, dict) or not isinstance(record.get('item_id'), str)
                    or record['item_id'] not in items or not isinstance(record.get('id'), str)
                    or record['id'] in occurrence_ids
                    or not isinstance(record.get('title'), str)
                    or not isinstance(record.get('content_status', 'unreviewed'), str)
                    or not isinstance(record.get('dates', []), list)
                    or any(not isinstance(entry, dict) or not isinstance(entry.get('kind'), str)
                           or (entry.get('resolution') is not None and not isinstance(entry['resolution'], str))
                           or (entry.get('normalized_date') is not None and not isinstance(entry['normalized_date'], str))
                           for entry in record.get('dates', []))):
                raise ReviewUnavailable('公开活动目录关联无效。')
            records[record['item_id']].append(record)
            occurrence_ids.add(record['id'])
        if any(not rows for rows in records.values()):
            raise ReviewUnavailable('公开活动缺少原始出现记录。')
        return items, records

    @staticmethod
    def _heads(db):
        return {row['item_id']: row for row in db.execute('''SELECT r.* FROM reader_review_revisions r
            JOIN (SELECT item_id,max(id) id FROM reader_review_revisions GROUP BY item_id) h ON r.id=h.id''')}

    @classmethod
    def _published(cls, db):
        row = next((row for row, _ in cls._events(db, 'completed')), None)
        return json.loads(row['revisions_json']) if row else {}

    @staticmethod
    def _flags(item_id, head, published, base_version):
        revision = head['revision'] if head is not None else None
        return {'head_revision': revision, 'published_revision': published.get(item_id),
                'unpublished': revision is not None and revision != published.get(item_id),
                'stale': head is not None and head['base_version'] != base_version}

    def list_items(self, q='', status='problems', page=1, per_page=30):
        if (not isinstance(q, str) or len(q) > 500 or not isinstance(status, str) or status not in _PROBLEMS | {
                'problems', 'edited', 'unpublished', 'stale', 'all', 'removed', 'retired'}):
            raise ReviewValidationError('活动查询或筛选条件无效。')
        if type(page) is not int or page < 1 or type(per_page) is not int or not 1 <= per_page <= 100:
            raise ReviewValidationError('分页参数无效。')
        with self._db() as db:
            base, active = self._selection(db)
            items, grouped = self._catalog(base)
            _, public_grouped = self._catalog(active) if active != base else (items, grouped)
            private_headers = self._private_headers(db)
            heads, published = self._heads(db), self._published(db)
            retirements = self._retirements(db)
            results = []
            query = q.casefold().strip()
            for item_id in dict.fromkeys([*items, *private_headers, *heads]):
                item = items.get(item_id, {})
                rows = grouped.get(item_id, [])
                latest = next((row for row in rows if row.get('id') == item.get('latest_occurrence_id')),
                              rows[0] if rows else private_headers.get(item_id, {}))
                head = heads.get(item_id)
                flags = self._flags(item_id, head, published, base['data_version'])
                removed = item_id not in items and item_id not in private_headers
                retired = head is not None and (item_id, head['revision']) in retirements
                flags.update(removed=removed, retired=retired)
                if retired:
                    flags['unpublished'] = False
                public_rows = public_grouped.get(item_id, [private_headers[item_id]] if item_id in private_headers else [])
                statuses = sorted({row.get('content_status', 'unreviewed') for row in public_rows})
                problems = sorted(set(statuses) & _PROBLEMS)
                if any(entry['kind'] in _DATE_KINDS and (
                        not entry.get('normalized_date') or entry.get('resolution') in {'year_unknown', 'ambiguous', 'invalid'})
                       for row in public_rows for entry in row.get('dates', [])):
                    problems.append('unresolved_dates')
                if removed and not retired:
                    problems.append('removed')
                title = json.loads(head['payload_json'])['activity']['title'] if head else latest.get('title', '')
                searchable = '\n'.join(str(row.get(key) or '') for row in rows
                                       for key in ('title', 'preview', 'category', 'source_urls'))
                if query and query not in (title + '\n' + searchable).casefold():
                    continue
                matches = (status == 'all' or (status == 'problems' and bool(problems))
                           or status in problems or (status == 'edited' and head is not None)
                           or (status == 'removed' and removed and not retired)
                           or (status == 'retired' and retired)
                           or (status in {'unpublished', 'stale'} and flags[status]))
                if matches:
                    results.append({'item_id': item_id, 'title': title, 'occurrence_count': len(rows),
                                    'content_statuses': statuses, 'problem_statuses': problems, **flags})
            total = len(results)
            pages = max(1, math.ceil(total / per_page))
            page = min(page, pages)
            return {'items': results[(page - 1) * per_page:page * per_page], 'total': total,
                    'page': page, 'pages': pages, 'per_page': per_page, 'q': q, 'status': status}

    def _item(self, base, item_id, db=None, dataset=None):
        if not isinstance(item_id, str) or not _ITEM.fullmatch(item_id):
            raise ReviewNotFound('活动不存在。')
        items, records = self._catalog(base)
        if item_id not in items:
            if db is not None:
                return self._private_item(db, item_id, dataset)
            raise ReviewNotFound('活动不存在于当前公开数据包。')
        shard = self._json_file(items[item_id]['detail_path'])
        detail = shard.get(item_id) if isinstance(shard, dict) else None
        if not isinstance(detail, dict) or not isinstance(detail.get('occurrences'), list):
            raise ReviewUnavailable('公开活动详情无效。')
        occurrences = detail['occurrences']
        if (any(not isinstance(row, dict) or row.get('item_id') != item_id
                or not isinstance(row.get('id'), str) or not isinstance(row.get('title'), str)
                or not isinstance(row.get('body_text'), str)
                or not isinstance(row.get('source_urls', []), list)
                or not isinstance(row.get('dates', []), list)
                or any(not isinstance(entry, dict) for entry in row.get('dates', []))
                or (row.get('date_review') is not None and not isinstance(row['date_review'], dict))
                for row in occurrences)
                or {row['id'] for row in occurrences} != {row['id'] for row in records[item_id]}
                or len(occurrences) != len(records[item_id])):
            raise ReviewUnavailable('公开活动详情与完整目录范围不一致。')
        return items[item_id], records[item_id], occurrences

    @staticmethod
    def _initial(item, occurrences, rules_version):
        ordered = sorted(occurrences, key=lambda row: row.get('id') != item.get('latest_occurrence_id'))
        admitted = None
        selected = ordered[0]
        for row in ordered:
            review = row.get('date_review') or {}
            source = review.get('source_review') or {}
            if not isinstance(source, dict):
                raise ReviewUnavailable('公开活动来源核查格式无效。')
            replaced = {entry['original_url'] for entry in row.get('source_corrections', [])}
            if (row.get('content_status') == 'verified' and rules_version
                    and review.get('rules_version') == rules_version
                    and source.get('status') == 'source_verified'
                    and not any(entry.get('url') in replaced for entry in source.get('sources', []))
                    and isinstance(source.get('activity'), dict)):
                admitted, selected = source['activity'], row
                break
        activity = {field: None for field in _LIMITS}
        if admitted is not None:
            for field in _LIMITS:
                node = admitted.get(field)
                activity[field] = node.get('value') if isinstance(node, dict) else None
        else:
            activity.update(title=selected.get('title') or '', summary=selected.get('preview') or '',
                            content=selected.get('body_text') or '')
        activity['category'] = selected.get('category')
        urls = []
        for row in ordered:
            for value in row.get('source_urls') or []:
                try:
                    value = validate_public_url(value)
                except ReviewValidationError:
                    continue
                if value not in urls:
                    urls.append(value)
        activity['source_urls'] = urls
        activity['dates'] = []
        if admitted is not None:
            for entry in selected.get('dates') or []:
                kind, value = entry.get('kind'), entry.get('normalized_date')
                if (kind in _DATE_KINDS and isinstance(value, str) and entry.get('literal_source') == 'primary'
                        and entry.get('resolution') == 'explicit'):
                    candidate = {'kind': kind, 'date': value}
                    if candidate not in activity['dates']:
                        activity['dates'].append(candidate)
        return {'status': 'needs_review', 'reason': '', 'activity': activity}, (
            'admitted_activity' if admitted is not None else 'original_excerpt')

    def get_item(self, item_id, *, dataset=None):
        if not isinstance(item_id, str) or not _ITEM.fullmatch(item_id):
            raise ReviewNotFound('活动不存在。')
        with self._db() as db:
            base, _ = self._selection(db)
            history = [_revision(row) for row in db.execute(
                'SELECT * FROM reader_review_revisions WHERE item_id=? ORDER BY id DESC', (item_id,))]
            head = history[0] if history else None
            removed = False
            original_version = base['data_version']
            try:
                item, records, occurrences = self._item(base, item_id, db, dataset)
            except ReviewNotFound:
                if head is None:
                    raise
                removed = True
                original_version = head['base_version']
                try:
                    item, records, occurrences = self._item({
                        'data_version': original_version, 'files': _manifest_files(original_version)}, item_id)
                except (ReviewNotFound, ReviewUnavailable):
                    item, records, occurrences = {}, [], []
                    original_version = None
            if removed:
                initial, initial_source = head['payload'], 'saved_revision'
            elif 'private_payload' in item:
                initial, initial_source = item['private_payload'], 'private_registration'
            else:
                initial, initial_source = self._initial(item, occurrences, base.get('content_rules_version'))
            retired = self._retirements(db)
            retirement = retired.get((item_id, head['revision'])) if head else None
            flags = self._flags(item_id, head, self._published(db), base['data_version'])
            if retirement:
                flags['unpublished'] = False
            registration = db.execute('SELECT item_id,activity_id,occurrence_id,source_id,created_at '
                                      'FROM reader_private_activities WHERE item_id=?', (item_id,)).fetchone()
            return {'item_id': item_id, 'title': initial['activity']['title'], 'occurrences': occurrences,
                    'dataset_id': dataset, 'registration': dict(registration) if registration is not None else None,
                    'catalog_records': records, 'initial_payload': initial, 'initial_source': initial_source,
                    'head': head, 'history': history, 'base_version': base['data_version'],
                    'removed': removed, 'retired': retirement is not None, 'retirement': retirement,
                    'retirements': [event for (identity, _), event in retired.items() if identity == item_id],
                    'original_available': bool(occurrences), 'original_version': original_version,
                    'fingerprint': _digest({'catalog_records': records, 'occurrences': occurrences}), **flags}

    def retire_removed(self, item_id, *, actor, expected_head, base_version, reason):
        actor = _actor(actor)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000 or '\x00' in reason:
            raise ReviewValidationError('私有退役说明不能为空，且不能超过 2000 字符。')
        with self._db(write=True) as db:
            base, active = self._selection(db)
            if base_version != base['data_version']:
                raise ReviewConflict('原始公开数据已更新，请重新核对后确认退役。')
            items, _ = self._catalog(base)
            head = self._heads(db).get(item_id)
            if head is None:
                raise ReviewNotFound('没有可退役的历史修订。')
            if item_id in items:
                raise ReviewConflict('活动仍存在于当前公开数据包，不能退役。')
            if expected_head != head['revision']:
                raise ReviewConflict('另一位编辑已保存新修订，请查看最新历史后再确认退役。')
            if (item_id, expected_head) in self._retirements(db):
                raise ReviewConflict('此修订已经确认退役。')
            if self._active() != active:
                raise ReviewConflict('公开数据在确认期间发生变化，请重新核对。')
            self._freeze(db, base)
            created_at = datetime.now(timezone.utc).isoformat()
            self._append_event(db, actor, base_version, active['data_version'],
                               {item_id: expected_head}, {'phase': 'retired', 'reason': reason}, created_at)
            return {'item_id': item_id, 'revision': expected_head, 'actor': actor, 'created_at': created_at,
                    'base_version': base_version, 'reason': reason}

    def save(self, item_id, payload, *, actor, expected_head, base_version, dataset=None):
        clean, actor = validate_payload(payload), _actor(actor)
        if expected_head is not None and (not isinstance(expected_head, str) or not _HASH.fullmatch(expected_head)):
            raise ReviewValidationError('上一修订版本无效。')
        with self._db(write=True) as db:
            base, active = self._selection(db)
            if base_version != base['data_version']:
                raise ReviewConflict('原始公开数据已更新，请重新核对后保存。')
            item, records, occurrences = self._item(base, item_id, db, dataset)
            row = db.execute('SELECT revision FROM reader_review_revisions WHERE item_id=? ORDER BY id DESC LIMIT 1',
                             (item_id,)).fetchone()
            if (row[0] if row else None) != expected_head:
                raise ReviewConflict('另一位编辑已保存新修订，请查看最新历史后再保存。')
            if self._active() != active:
                raise ReviewConflict('公开数据在保存期间发生变化，请重新核对。')
            created_at = datetime.now(timezone.utc).isoformat()
            if 'private_payload' in item and item_id not in self._registrations(db):
                identity_sha = hashlib.sha256(('private-registration:' + item_id).encode()).hexdigest()[:24]
                db.execute('INSERT INTO reader_private_activities VALUES(?,?,?,?,?,?,?,?)',
                           (item_id, item_id, occurrences[0]['id'], 'source-' + identity_sha,
                            'archive:' + (dataset or '') + ':' + item_id, created_at, actor, _json(clean).decode()))
            fingerprint = _digest({'catalog_records': records, 'occurrences': occurrences})
            revision = _digest({'item_id': item_id, 'created_at': created_at, 'actor': actor,
                                'base_version': base_version, 'fingerprint': fingerprint,
                                'previous_head': expected_head, 'payload': clean})
            self._freeze(db, base)
            db.execute('''INSERT INTO reader_review_revisions
                (item_id,revision,created_at,actor,base_version,fingerprint,previous_head,payload_json)
                VALUES(?,?,?,?,?,?,?,?)''',
                       (item_id, revision, created_at, actor, base_version, fingerprint, expected_head, _json(clean).decode()))
            return {'item_id': item_id, 'revision': revision, 'created_at': created_at, 'actor': actor,
                    'base_version': base_version, 'fingerprint': fingerprint, 'payload': clean}

    def publication_snapshot(self):
        with self._db(write=True) as db:
            base, active = self._selection(db)
            heads = self._heads(db)
            registrations = self._registrations(db)
            retired = self._retirements(db)
            heads = {item_id: head for item_id, head in heads.items()
                     if (item_id, head['revision']) not in retired}
            project = db.execute('SELECT * FROM reader_review_project WHERE id=1').fetchone()
            prepared = self._prepared(db, base['data_version'], active['data_version'])
            if prepared is not None and project and project['published_version'] != active['data_version']:
                # Finish the already switched snapshot before publishing any later save.
                revisions = [_revision(db.execute(
                    'SELECT * FROM reader_review_revisions WHERE item_id=? AND revision=?', (item_id, revision)).fetchone())
                    for item_id, revision in sorted(json.loads(prepared['revisions_json']).items())]
            else:
                items, _ = self._catalog(base)
                if any(head['base_version'] != base['data_version'] or (item_id not in items and item_id not in registrations)
                       for item_id, head in heads.items()):
                    raise ReviewConflict('原始公开数据已更新，历史草稿须重新核对并保存或明确退役后才能发布。')
                revisions = [_revision(heads[item_id]) for item_id in sorted(heads)]
            self._freeze(db, base)
            for revision in revisions:
                revision['payload'] = validate_payload(revision['payload'])
            items, _ = self._catalog(base)
            new_activities = []
            for revision in revisions:
                identity = revision['item_id']
                if identity not in items:
                    registration = registrations.get(identity)
                    if registration is None:
                        raise ReviewConflict('新增活动缺少明确的私有来源登记。')
                    new_activities.append({key: registration[key] for key in
                        ('item_id', 'activity_id', 'occurrence_id', 'source_id', 'created_at')} | {
                        'revision': revision['revision'], 'payload': revision['payload'],
                        'kind': 'standalone', 'issue_id': None, 'document_id': None,
                        'source_urls': revision['payload']['activity']['source_urls']})
            return {'base_manifest': base, 'base_version': base['data_version'],
                    'active_manifest': active, 'active_version': active['data_version'],
                    'revisions': revisions, 'published_revisions': self._published(db),
                    'new_activities': new_activities}

    @staticmethod
    def _snapshot_map(db, snapshot):
        revisions = {}
        for revision in snapshot['revisions']:
            row = db.execute('SELECT * FROM reader_review_revisions WHERE revision=?', (revision['revision'],)).fetchone()
            if (row is None or _revision(row) != revision or revision['base_version'] != snapshot['base_version']
                    or revision['item_id'] in revisions):
                raise ReviewConflict('发布快照与不可变修订记录不一致。')
            revisions[revision['item_id']] = revision['revision']
        return revisions

    @staticmethod
    def _append_event(db, actor, base_version, version, revisions, result, created_at=None):
        db.execute('''INSERT INTO reader_review_publications
            (created_at,actor,base_version,version,revisions_json,result_json) VALUES(?,?,?,?,?,?)''',
                   (created_at or datetime.now(timezone.utc).isoformat(), actor, base_version, version,
                    _json(revisions).decode(), _json(result).decode()))

    def _record_publish_event(self, *, actor, snapshot, result, phase):
        actor = _actor(actor)
        version = result.get('version') if isinstance(result, dict) else None
        if not isinstance(version, str) or not _HASH.fullmatch(version):
            raise ReviewValidationError('发布结果缺少有效的数据版本。')
        with self._db(write=True) as db:
            project = db.execute('SELECT * FROM reader_review_project WHERE id=1').fetchone()
            if not project or project['base_version'] != snapshot['base_version']:
                raise ReviewConflict('发布期间原始数据基线已改变；未记录为已发布。')
            revisions = self._snapshot_map(db, snapshot)
            active = self._active()
            if phase == 'prepared':
                if active != snapshot['active_manifest']:
                    raise ReviewConflict('准备期间公开数据已变化，未记录准备完成。')
            else:
                prepared = self._prepared(db, snapshot['base_version'], version)
                if (prepared is None or json.loads(prepared['revisions_json']) != revisions
                        or active['data_version'] != version):
                    raise ReviewConflict('公开版本与私有准备快照不同；未记录为已发布。')
            self._append_event(db, actor, snapshot['base_version'], version, revisions, {**result, 'phase': phase})
            if phase == 'completed':
                db.execute('UPDATE reader_review_project SET published_version=? WHERE id=1', (version,))

    def record_preparation(self, *, actor, snapshot, result):
        """Authenticate the exact staged native version before any live switch."""
        self._record_publish_event(actor=actor, snapshot=snapshot, result=result, phase='prepared')

    def record_publication(self, *, actor, snapshot, result):
        """Account only for the exact prepared snapshot after all destinations succeed."""
        self._record_publish_event(actor=actor, snapshot=snapshot, result=result, phase='completed')
