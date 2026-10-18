"""Persisted source evidence must stay bound, and old PDF shells must not be reused."""
import base64
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from nannanews import bulletin_full_review as full


URL = 'https://campus.example.org/notice'


def _failure():
    raw = '环境异常，完成验证后继续访问。'.encode()
    return {'error': {'code': 'source_blocked', 'reason': 'Verification is required.',
        'source_response': {'canonical_url': URL, 'resolved_url': URL,
            'content_type': 'text/html', 'http_status': 200,
            'fetched_at': '2026-10-17T09:00:00+00:00',
            'raw_response_base64': base64.b64encode(raw).decode(),
            'raw_response_digest': hashlib.sha256(raw).hexdigest(),
            'raw_response_complete': True}}}


@pytest.mark.parametrize('field,value', [
    ('canonical_url', 'https://another.example.org/notice'),
    ('resolved_url', 'http://127.0.0.1/private'),
    ('raw_response_digest', '0' * 64),
    ('raw_response_complete', 'false'),
    ('http_status', True),
    ('fetched_at', '2026-10-17T09:00:00'),
])
def test_failure_diagnostics_cannot_be_corrupted_or_rebound(tmp_path, field, value):
    directory = full._directory(tmp_path / 'cache')
    cache = full.SourceCache(directory, tmp_path / 'main.sqlite3')
    capture = deepcopy(_failure())
    capture['error']['source_response'][field] = value
    with pytest.raises(ValueError):
        cache[URL] = capture
    assert not cache._path(URL).exists()


def test_empty_truncated_http_failure_remains_a_protected_failure_after_restart(tmp_path):
    directory = full._directory(tmp_path / 'cache')
    capture = _failure()
    response = capture['error']['source_response']
    response.update(http_status=503, raw_response_base64='',
                    raw_response_digest=hashlib.sha256(b'').hexdigest(), raw_response_complete=False)
    full.SourceCache(directory, tmp_path / 'main.sqlite3')[URL] = capture
    restored = full.SourceCache(directory, tmp_path / 'main.sqlite3').get(URL)
    assert restored['error']['code'] == 'source_blocked'
    assert restored['error']['source_response']['raw_response_complete'] is False
    assert 'document' not in restored


def test_legacy_header_only_embedded_pdf_is_not_seeded_as_complete_primary(tmp_path):
    raw = b'''<html><h1>Public report</h1><div class="infobox article">
        <div class="wp_articlecontent"><div class="wp_pdf_player"
        pdfsrc="/report.pdf" sudyplayer="wp_pdf_player"></div></div></div></html>'''
    document = {'canonical_url': URL, 'resolved_url': URL, 'title': 'Public report',
        'text': 'Public report', 'images': [], 'media': [], 'source_type': 'public_html',
        'fetched_at': '2026-10-17T09:00:00+00:00', 'content_type': 'text/html',
        'raw_response_base64': base64.b64encode(raw).decode(),
        'raw_response_digest': hashlib.sha256(raw).hexdigest(), 'capture_complete': True}
    database = tmp_path / 'main.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE source_documents(id INTEGER,source_url TEXT,document_json TEXT)')
        db.execute('INSERT INTO source_documents VALUES(1,?,?)', (URL, json.dumps(document)))
    cache = full.SourceCache(full._directory(tmp_path / 'cache'), database)
    assert cache.get(URL) is None
    with sqlite3.connect(database) as db:
        assert json.loads(db.execute('SELECT document_json FROM source_documents').fetchone()[0]) == document
