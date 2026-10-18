"""Private, durable wire receipts; never a public response or a retry mechanism."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

VERSION = 'model-wire-audit-v1'
SAFE_HEADERS = ('x-request-id', 'request-id', 'openai-processing-ms', 'retry-after',
                'content-type', 'content-length', 'content-encoding')


class AuditError(Exception):
    pass


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise AuditError('Private model audit directory must not be a symlink.')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise AuditError('Private model audit directory is not a directory.')
    path.chmod(0o700)
    _sync_directory(path.parent)
    return path


def response_metadata(response):
    """Keep only bounded diagnostic scalars and numeric usage, not provider dumps."""
    if not isinstance(response, dict):
        return {}

    def usage(value, depth=0):
        if type(value) in (int, float) and value >= 0:
            return value if value < 10 ** 18 else None
        if isinstance(value, dict) and depth < 3:
            return {key: cleaned for key, item in list(value.items())[:64]
                if isinstance(key, str) and re.fullmatch(r'[a-zA-Z0-9_]{1,80}', key)
                and (cleaned := usage(item, depth + 1)) is not None}
        return None

    result = {key: value for key in ('id', 'model', 'status')
        if isinstance(value := response.get(key), str) and len(value) <= 256
        and not re.search(r'[\x00-\x1f\x7f]', value)}
    if isinstance(response.get('usage'), dict):
        result['usage'] = usage(response['usage'])
    return result


def _save_metadata(directory, metadata):
    temporary = directory / '.receipt.json'
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(metadata, stream, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(directory / 'receipt.json')
    _sync_directory(directory)


def finalize_receipt(identity, *, outcome, failure_code=None):
    """Record downstream admission without touching original request/response."""
    path = Path(identity['receipt'])
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
        metadata = json.load(stream)
    if metadata['request']['sha256'] != identity['request_sha256']:
        raise AuditError('Model receipt identity mismatch.')
    metadata.update(phase='review_finalized', review_outcome=outcome,
        review_failure_code=failure_code, updated_at=datetime.now(timezone.utc).isoformat())
    metadata.setdefault('lifecycle', []).append({'phase': 'review_finalized', 'at': metadata['updated_at']})
    _save_metadata(path.parent, metadata)


class ModelAudit:
    """Serialize once, reserve response disk space, then permit paid admission.

    request.json is the exact HTTP entity (including prepared image data URLs).
    response.bin is the bounded ORIGINAL entity, including non-200/invalid JSON.
    receipt.json is replaceable lifecycle metadata; request/response files are
    never replaced or reconstructed from parsed output. Each receipt is unique.

    Model transport may add upload_complete and network_timing (monotonic elapsed
    seconds, measured phase durations, and an optional timeout_stage). These are
    private observations, not provider claims: upload_complete means all local
    request writes returned, not that the provider acknowledged the request.
    Absent fields in old/prepared receipts mean unknown; immutable request/response
    evidence is never backfilled.
    """
    def __init__(self, directory, payload, *, request_limit, response_limit, context=None):
        self.directory = None
        self.request = None
        self.response = None
        self.response_limit = response_limit
        self.response_size = 0
        self.response_digest = hashlib.sha256()
        try:
            root = _private_directory(directory)
            self.directory = Path(tempfile.mkdtemp(prefix='wire-', dir=root))
            self.request = self._open('request.json')
            digest, size = hashlib.sha256(), 0
            for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(payload):
                data = part.encode('utf-8')
                size += len(data)
                if size > request_limit:
                    raise OverflowError('Complete model request exceeds its byte bound.')
                self.request.write(data)
                digest.update(data)
            self.request.flush()
            os.fsync(self.request.fileno())
            self.request.seek(0)
            self.response = self._open('response.bin')
            # A sparse truncate/free-space check does not reserve disk blocks.
            # Failure (including unsupported fallocate) blocks paid admission.
            os.posix_fallocate(self.response.fileno(), 0, response_limit + 1)
            os.fsync(self.response.fileno())
            self.metadata = {'version': VERSION, 'created_at': self._now(), 'phase': 'prepared',
                'request': {'file': 'request.json', 'bytes': size, 'sha256': digest.hexdigest()},
                'response': {'file': 'response.bin', 'reserved_bytes': response_limit + 1},
                'api_style': context.get('api_style') if context else None,
                'configured_model': payload.get('model'), 'context': context or {}}
            self.save('prepared')
            _sync_directory(root)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def _open(self, name):
        descriptor = os.open(self.directory / name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        return os.fdopen(descriptor, 'w+b')

    def save(self, phase=None, **values):
        now = self._now()
        if phase is not None:
            self.metadata['phase'] = phase
            self.metadata.setdefault('lifecycle', []).append({'phase': phase, 'at': now})
        self.metadata.update(values, updated_at=now)
        _save_metadata(self.directory, self.metadata)

    @property
    def identity(self):
        return {'receipt': str(self.directory / 'receipt.json'),
                'request_sha256': self.metadata['request']['sha256']}

    def headers(self, response):
        headers = {}
        for name in SAFE_HEADERS:
            value = response.getheader(name)
            # Diagnostic allowlist, bounded printable values. Never cookies/auth/location.
            if isinstance(value, str) and len(value) <= 512 and re.fullmatch(r'[\x20-\x7e]*', value):
                headers[name] = value
        self.save('response_headers', http_status=response.status, diagnostic_headers=headers)

    def append(self, chunk):
        if self.response_size + len(chunk) > self.response_limit + 1:
            raise AuditError('Response archive exceeds reserved byte bound.')
        self.response.write(chunk)
        self.response_size += len(chunk)
        self.response_digest.update(chunk)

    def preserve_response(self, *, complete):
        # Persist bytes BEFORE any fallible metadata or downstream validation.
        self.response.truncate(self.response_size)
        self.response.flush()
        os.fsync(self.response.fileno())
        self.metadata['response'] = {'file': 'response.bin', 'bytes': self.response_size,
            'sha256': self.response_digest.hexdigest(), 'complete': complete,
            'bounded_prefix': self.response_size > self.response_limit}
        self.save('response_archived')

    def response_json(self):
        self.response.seek(0)
        return json.load(self.response)

    def close(self):
        for stream in (self.request, self.response):
            if stream is not None:
                stream.close()
