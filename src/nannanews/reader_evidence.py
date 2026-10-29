"""Read-only navigation of the local foundation database and registered blobs.

Captured sources are never substituted for the exact saved model request.
"""
from contextlib import contextmanager
import base64
import binascii
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from nannanews import runtime
from nannanews.reader_reviews import ReviewConflict, ReviewNotFound, ReviewUnavailable, ReviewValidationError

_SHA = re.compile(r'[0-9a-f]{64}\Z')
_MAX_JSON = 64 * 1024 * 1024
_MAX_IMAGE = 16 * 1024 * 1024
_LEGACY_ARTIFACTS = {
    'legacy-request': ('request_blob_sha', '历史请求元数据（非HTTP实体）'),
    'legacy-result': ('result_blob_sha', '历史解析结果（非提供方原始响应）'),
    'legacy-decision': ('decision_blob_sha', '历史判定记录'),
}


def _object(value, label):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            raise ReviewUnavailable(f'{label} JSON 无效；原始记录仍保留。') from None
    if not isinstance(value, dict):
        raise ReviewUnavailable(f'{label} 不是 JSON 对象。')
    return value


def _json_text(value):
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise ReviewUnavailable('已保存元数据不能安全呈现为 JSON；不替换为空白记录。') from None


def _raster(raw, claimed):
    valid = ((claimed == 'image/png' and raw.startswith(b'\x89PNG\r\n\x1a\n'))
             or (claimed == 'image/jpeg' and raw.startswith(b'\xff\xd8\xff'))
             or (claimed == 'image/gif' and raw.startswith((b'GIF87a', b'GIF89a')))
             or (claimed == 'image/webp' and raw.startswith(b'RIFF') and raw[8:12] == b'WEBP'))
    if not valid:
        raise ReviewUnavailable('图片类型不安全或与实际字节不符；不执行 HTML / SVG。')
    return claimed


class EvidenceStore:
    def __init__(self, settings=None):
        self.settings = settings if settings is not None else runtime.settings

    @property
    def configured(self):
        return self.settings.reader_evidence_dir is not None

    def root(self):
        root = self.settings.reader_evidence_dir
        if root is None:
            raise ReviewUnavailable('未配置 NANNA_READER_EVIDENCE_DIR；无法查询私有核查档案。')
        root = Path(root).expanduser().absolute()
        try:
            from nannanews.data_foundation import _no_symlinks
            _no_symlinks(root)
            _no_symlinks(root / 'foundation.sqlite3')
            root = root.resolve()
            for public in (self.settings.reader_data_dir, self.settings.reader_site_data_dir):
                if public is not None:
                    public = Path(public).expanduser().absolute()
                    _no_symlinks(public)
                    public = public.resolve()
                    if root.is_relative_to(public) or public.is_relative_to(root):
                        raise ReviewUnavailable('私有证据目录不能与公开数据目录重叠。')
            if not (root / 'foundation.sqlite3').is_file():
                raise ReviewUnavailable('私有资料库不存在；不会创建或自动迁移。')
            return root
        except (OSError, ValueError) as exc:
            raise ReviewUnavailable(f'私有资料库不可用：{exc}') from None

    def identity(self, db):
        row = db.execute("SELECT value FROM metadata WHERE key='restic_archive:v1'").fetchone()
        if row:
            try:
                value = json.loads(row[0])['dataset_id']
                if not isinstance(value, str) or not _SHA.fullmatch(value):
                    raise ValueError('invalid dataset identity')
                return value
            except (ValueError, KeyError, TypeError):
                raise ReviewUnavailable('私有资料库身份无效。') from None
        return hashlib.sha256(str(self.root()).encode('utf-8')).hexdigest()

    @contextmanager
    def _db(self, dataset=None):
        db = None
        try:
            from nannanews.data_foundation import _open_existing
            db = _open_existing(self.root())
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA trusted_schema=OFF')
            if dataset is not None and dataset != self.identity(db):
                raise ReviewNotFound('私有证据不属于当前资料库。')
            yield db
        except (OSError, ValueError, sqlite3.Error) as exc:
            if isinstance(exc, (ReviewUnavailable, ReviewNotFound, ReviewValidationError, ReviewConflict)):
                raise
            raise ReviewUnavailable(f'私有资料库查询失败：{exc}') from None
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _activity_id(db, identity):
        row = db.execute('SELECT activity_id FROM activities WHERE activity_id=? UNION ALL '
                         'SELECT activity_id FROM activity_aliases WHERE alias_id=? LIMIT 1',
                         (identity, identity)).fetchone()
        if row is None:
            raise ReviewNotFound('私有目录中没有此活动身份。')
        return row[0]

    def activity_headers(self):
        if not self.configured:
            return []
        with self._db() as db:
            dataset = self.identity(db)
            return [dict(row, dataset_id=dataset) for row in db.execute(
                'SELECT activity_id,title,review_status,record_version FROM activities ORDER BY activity_id')]

    def _record(self, table, identity, dataset=None):
        keys = {'documents': 'document_id', 'activities': 'activity_id',
                'reviews': 'review_id', 'snapshots': 'snapshot_id'}
        if table not in keys:
            raise ValueError('Unsupported evidence table')
        with self._db(dataset) as db:
            row = db.execute(f'SELECT * FROM {table} WHERE {keys[table]}=?', (identity,)).fetchone()
            if row is None:
                raise ReviewNotFound('选中的私有记录不存在。')
            return dict(row)

    def documents(self, *, dataset=None, query='', page=1):
        """Browse original-PDF metadata without reading PDF or model bodies."""
        query = query.strip()[:250]
        with self._db(dataset) as db:
            identity = self.identity(db)
            rows = db.execute("SELECT d.*,b.size FROM documents d JOIN blobs b ON b.sha256=d.sha256 "
                              "WHERE d.document_id=d.sha256 AND d.format='pdf' "
                              "AND b.kind='original' AND b.mime='application/pdf' "
                              "ORDER BY d.issue_date DESC,d.document_id DESC")
            items = [dict(row) for row in rows if not query or query.casefold() in
                     ' '.join(str(row[key] or '') for key in
                              ('document_id', 'filename', 'issue_date', 'issue_number')).casefold()]
        page, per_page = max(1, page), 30
        return {'items': items[(page - 1) * per_page:page * per_page], 'total': len(items),
                'page': page, 'per_page': per_page, 'dataset_id': identity, 'query': query}

    def document_pdf(self, document_id, *, dataset=None):
        if not isinstance(document_id, str) or not _SHA.fullmatch(document_id):
            raise ReviewNotFound('原刊文档标识无效。')
        with self._db(dataset) as db:
            header = db.execute(
                "SELECT b.size FROM documents d JOIN blobs b ON b.sha256=d.sha256 "
                "WHERE d.document_id=? AND d.sha256=d.document_id AND d.format='pdf' "
                "AND b.kind='original' AND b.mime='application/pdf'", (document_id,)).fetchone()
        if header is None:
            raise ReviewNotFound('目录中没有此原刊 PDF；不会读取任意附件。')
        record = self._record('documents', document_id, dataset)
        if (record.get('document_id') != document_id or record.get('sha256') != document_id
                or record.get('format') != 'pdf'):
            raise ReviewUnavailable('原刊文档身份与附件目录不一致。')
        raw = self.blob(document_id, dataset=dataset)
        if not raw.startswith(b'%PDF-'):
            raise ReviewUnavailable('原刊附件不是有效的 PDF 文件头。')
        return raw

    def activity_header(self, identity, *, dataset=None):
        with self._db(dataset) as db:
            activity_id = self._activity_id(db, identity)
            row = db.execute('SELECT activity_id,title,review_status,record_version FROM activities '
                             'WHERE activity_id=?', (activity_id,)).fetchone()
            return dict(row, dataset_id=self.identity(db))
    def activity(self, identity, *, dataset=None):
        header = self.activity_header(identity, dataset=dataset)
        name, activity_id = header['dataset_id'], header['activity_id']
        with self._db(name) as db:
            source_ids = [row[0] for row in db.execute('SELECT source_id FROM source_records '
                                                      'WHERE activity_id=? ORDER BY source_id', (activity_id,))]
            dates = [dict(row) for row in db.execute('SELECT kind,date FROM activity_dates WHERE activity_id=?',
                                                    (activity_id,))]
        return self._record('activities', activity_id, name), source_ids, dates

    def list_reviews(self, identity, *, page=1, per_page=30):
        if type(page) is not int or page < 1 or type(per_page) is not int or not 1 <= per_page <= 100:
            raise ReviewValidationError('核查分页参数无效。')
        with self._db() as db:
            try:
                activity_id = self._activity_id(db, identity)
            except ReviewNotFound:
                return {'activity_id': identity, 'items': [], 'total': 0, 'page': page, 'per_page': per_page}
            dataset = self.identity(db)
            total = db.execute('SELECT count(*) FROM reviews WHERE activity_id=?', (activity_id,)).fetchone()[0]
            rows = [dict(row, dataset_id=dataset) for row in db.execute(
                'SELECT review_id,activity_id,occurrence_id,status,reviewer,reason FROM reviews '
                'WHERE activity_id=? ORDER BY review_id LIMIT ? OFFSET ?',
                (activity_id, per_page, (page - 1) * per_page))]
        return {'activity_id': identity, 'items': rows, 'total': total, 'page': page, 'per_page': per_page}

    def resolve_review(self, review_id, *, dataset=None):
        if not isinstance(review_id, str) or not review_id or len(review_id) > 500:
            raise ReviewValidationError('精确核查身份无效。')
        with self._db(dataset) as db:
            row = db.execute('SELECT * FROM reviews WHERE review_id=?', (review_id,)).fetchone()
            if row is None:
                raise ReviewNotFound('核查记录不存在。')
            return dict(row, dataset_id=self.identity(db))

    def require_review(self, identity, review_id, *, associated=False, dataset=None):
        row = self.resolve_review(review_id, dataset=dataset)
        with self._db(row['dataset_id']) as db:
            try:
                canonical = self._activity_id(db, identity)
            except ReviewNotFound:
                canonical = identity
        if row['activity_id'] is None:
            if not associated:
                raise ReviewNotFound('核查未记录活动身份，须先明确私有关联。')
        elif row['activity_id'] != canonical:
            raise ReviewNotFound('核查已记录的活动身份与选中活动冲突，不能重新归属。')
        return row

    def blob(self, digest, *, maximum=_MAX_JSON, dataset=None):
        if not isinstance(digest, str) or not _SHA.fullmatch(digest):
            raise ReviewUnavailable('记录未保留有效的 SHA-256 附件引用。')
        with self._db(dataset) as db:
            row = db.execute('SELECT size,local_path FROM blobs WHERE sha256=?', (digest,)).fetchone()
            if row is None:
                raise ReviewUnavailable('附件未在资料库中登记；不会猜测或替换。')
            if row['size'] > maximum:
                raise ReviewUnavailable('选中附件超过安全读取上限。')
        try:
            import os
            import stat
            from nannanews.data_foundation import _no_symlinks
            if not row['local_path']:
                raise ReviewUnavailable('选中附件没有本地副本；不会访问云端。')
            path = Path(row['local_path']).absolute()
            _no_symlinks(path)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != row['size'] or info.st_size > maximum:
                    raise ReviewUnavailable('附件长度或文件类型校验失败。')
                raw = stream.read(maximum + 1)
            if len(raw) != row['size'] or hashlib.sha256(raw).hexdigest() != digest:
                raise ReviewUnavailable('附件长度或 SHA-256 校验失败。')
            return raw
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            if isinstance(exc, ReviewUnavailable):
                raise
            raise ReviewUnavailable(f'选中本地附件缺失或校验失败：{exc}') from None

    def _review(self, identity, review_id, *, associated=False, dataset=None):
        header = self.require_review(identity, review_id, associated=associated, dataset=dataset)
        record = self._record('reviews', review_id, header['dataset_id'])
        if record.get('review_id') != review_id or record.get('activity_id') != header['activity_id']:
            raise ReviewUnavailable('选中核查记录身份与目录不一致。')
        record = dict(record, dataset_id=header['dataset_id'])
        fields = _object(record.get('fields_json', '{}'), '核查判断')
        wire = fields.get('_model_wire') or {}
        if not isinstance(wire, dict):
            raise ReviewUnavailable('模型原始收据引用格式无效。')
        return record, fields, wire

    @staticmethod
    def _legacy(fields):
        paid = fields.get('paid_facts')
        if paid is None:
            return None
        if isinstance(paid, dict) and 'artifacts' not in paid:
            return None
        if not isinstance(paid, dict) or not isinstance(paid.get('artifacts'), dict):
            raise ReviewUnavailable('历史付费事实 / 附件引用格式无效；不冒充实际 HTTP 输入。')
        artifacts = paid.get('artifacts', {})
        return {'request_key': paid.get('request_key'), 'source_state': paid.get('state'),
                'artifacts': [{'role': role, 'label': label, 'sha256': artifacts[key]}
                              for role, (key, label) in _LEGACY_ARTIFACTS.items() if artifacts.get(key)]}

    @staticmethod
    def _parts(request):
        texts, images = [], []
        pending = [(request, '$')]
        while pending:
            node, path = pending.pop()
            if isinstance(node, dict):
                children = []
                for key, value in node.items():
                    location = path + '.' + key
                    if key in ('text', 'instructions', 'input', 'prompt', 'content') and isinstance(value, str):
                        children.append(('text', location, value))
                    elif key in ('image_url', 'url') and isinstance(value, str) and (
                            value.startswith('data:image/') or key == 'image_url' or path.endswith('.image_url')):
                        children.append(('image', location, value))
                    else:
                        children.append(('node', location, value))
                for kind, location, value in reversed(children):
                    pending.append(((kind, value), location) if kind != 'node' else (value, location))
            elif isinstance(node, list):
                pending.extend((value, f'{path}[{index}]') for index, value in reversed(list(enumerate(node))))
            elif isinstance(node, tuple):
                kind, value = node
                (texts if kind == 'text' else images).append({'path': path, 'text' if kind == 'text' else 'url': value})
        return texts, images

    def review(self, identity, review_id, *, associated=False, dataset=None):
        record, fields, wire = self._review(identity, review_id, associated=associated, dataset=dataset)
        dataset = record['dataset_id']
        try:
            error_value = record.get('error_json')
            errors = json.loads(error_value) if isinstance(error_value, str) else error_value
        except (ValueError, RecursionError):
            raise ReviewUnavailable('核查错误记录 JSON 无效；不推断为无错误。') from None
        result = {'review_id': review_id, 'activity_id': record['activity_id'], 'status': record.get('status'),
                  'dataset_id': dataset,
                  'reason': record.get('reason'), 'judgment': _json_text(fields),
                  'errors': _json_text(errors) if errors is not None else '未保存错误字段；不能推断为无错误。',
                  'texts': [], 'images': [], 'missing': [], 'response': None, 'receipt': None}
        result['legacy'] = self._legacy(fields)
        for role in ('request', 'response', 'receipt'):
            digest = wire.get(role + '_blob_sha')
            if not digest:
                result['missing'].append({'field': role, 'message': '历史记录没有此原始附件引用；不能证明完整。'})
                continue
            try:
                raw = self.blob(digest, dataset=dataset)
                if role == 'request':
                    texts, images = self._parts(_object(raw.decode('utf-8'), '实际发送请求'))
                    result['texts'] = texts
                    result['images'] = [{'index': i, 'path': image['path'],
                        'available': image['url'].startswith('data:image/'),
                        'message': '请求中保存的 prepared image' if image['url'].startswith('data:image/')
                                   else '请求只保存远程 URL，不会访问该 URL，也不声称保留了图片字节。'}
                                        for i, image in enumerate(images)]
                elif role == 'receipt':
                    result['receipt'] = _json_text(_object(raw.decode('utf-8'), '模型收据'))
                else:
                    result['response'] = raw.decode('utf-8', errors='replace')
            except (ReviewUnavailable, UnicodeError, ValueError) as exc:
                result['missing'].append({'field': role, 'message': str(exc)})
        with self._db(dataset) as db:
            result['sources'] = [dict(row) for row in db.execute('SELECT s.snapshot_id,s.url,s.title,s.status '
                'FROM review_snapshots r JOIN snapshots s USING(snapshot_id) WHERE r.review_id=? ORDER BY s.snapshot_id',
                (review_id,))]
        return result

    def artifact(self, identity, review_id, role, *, associated=False, dataset=None):
        if role not in ('request', 'response', 'receipt') and role not in _LEGACY_ARTIFACTS:
            raise ReviewNotFound('未知原始附件。')
        record, fields, wire = self._review(identity, review_id, associated=associated, dataset=dataset)
        if role in _LEGACY_ARTIFACTS:
            legacy = self._legacy(fields)
            artifact = next((entry for entry in legacy['artifacts'] if entry['role'] == role), None) if legacy else None
            if artifact is None:
                raise ReviewUnavailable('此历史记录没有保留所选元数据附件；不会用实际 HTTP 附件或其他记录替代。')
            return self.blob(artifact['sha256'], dataset=record['dataset_id'])
        return self.blob(wire.get(role + '_blob_sha'), dataset=record['dataset_id'])

    def prepared_image(self, identity, review_id, index, *, associated=False, dataset=None):
        raw = self.artifact(identity, review_id, 'request', associated=associated, dataset=dataset)
        try:
            request_body = _object(raw.decode('utf-8'), '实际发送请求')
        except UnicodeError:
            raise ReviewUnavailable('实际请求不是有效 UTF-8 JSON。') from None
        _, images = self._parts(request_body)
        if type(index) is not int or not 0 <= index < len(images):
            raise ReviewNotFound('实际请求中没有此图片。')
        match = re.fullmatch(r'data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=\r\n]+)', images[index]['url'])
        if not match or len(match[2]) > (_MAX_IMAGE * 4 // 3 + 16):
            raise ReviewUnavailable('图片原始字节缺失、过大或不是可安全显示的 raster 图片。')
        try:
            decoded = base64.b64decode(match[2].replace('\r', '').replace('\n', ''), validate=True)
        except (ValueError, binascii.Error):
            raise ReviewUnavailable('实际请求图片编码无效。') from None
        if len(decoded) > _MAX_IMAGE:
            raise ReviewUnavailable('图片超过安全读取上限。')
        return decoded, _raster(decoded, match[1])

    def source(self, identity, review_id, snapshot_id, *, associated=False, dataset=None):
        header = self.require_review(identity, review_id, associated=associated, dataset=dataset)
        dataset = header['dataset_id']
        with self._db(dataset) as db:
            row = db.execute('SELECT s.* FROM snapshots s JOIN review_snapshots r USING(snapshot_id) '
                             'WHERE r.review_id=? AND s.snapshot_id=?', (review_id, snapshot_id)).fetchone()
            if row is None:
                raise ReviewNotFound('来源快照不属于选中核查记录。')
        record = self._record('snapshots', snapshot_id, dataset)
        text = self.blob(record['text_blob_sha'], dataset=dataset).decode('utf-8', errors='replace') if record.get('text_blob_sha') else None
        return {'snapshot_id': snapshot_id, 'url': record.get('url'), 'status': record.get('status'), 'dataset_id': dataset,
                'text': text, 'missing': None if text is not None else '快照未保存正文附件。',
                'metadata': _json_text(record)}
