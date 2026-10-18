"""Offline public-PDF contracts; only network and OS process boundaries are faked."""
import base64
from copy import deepcopy
import hashlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import time

from PIL import Image
import pytest

from nannanews import acquisition
from nannanews import bulletin_date_repair as repair
from nannanews import public_pdf

PDF_BYTES = b'%PDF-1.7\noriginal public PDF bytes\n%%EOF\n'
PARENT = 'https://life.nju.edu.cn/a5/ac/c12967a828844/page.htm'
PDF_URL = 'https://life.nju.edu.cn/_upload/article/files/original.pdf?x=1&x=2'
HTML_BYTES = ('<html><body><div class="infobox"><div class="article">'
              '<h1>公开讲座</h1><p>发布时间：2026-03-31 浏览次数：10</p>'
              '<div class="wp_articlecontent"><div class="wp_pdf_player" '
              'pdfsrc="/_upload/article/files/original.pdf?x=1&amp;x=2" '
              'swsrc="/never-execute.swf"></div></div></div></div></body></html>').encode()


@pytest.fixture
def local_poppler(monkeypatch):
    state = {'pages': 2, 'encrypted': False, 'failure_page': None,
             'malformed_page': None, 'commands': [], 'dimension': 20, 'directories': []}
    monkeypatch.setattr(public_pdf.shutil, 'which', lambda name: '/private/bin/' + name)
    monkeypatch.setattr(public_pdf.os, 'killpg', lambda *args: None)

    def pipe(content):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, content)
        os.close(write_fd)
        return os.fdopen(read_fd, 'rb')

    def launch(command, **options):
        state['commands'].append(command)
        state['directories'].append(Path(options['cwd']))
        arguments = command[command.index('--') + 1:]
        stdout, stderr, returncode = b'', b'', 0
        if arguments[0].endswith('/pdfinfo'):
            assert Path(arguments[-1]).read_bytes() == PDF_BYTES
            stdout = (f'Pages: {state["pages"]}\nEncrypted: {"yes" if state["encrypted"] else "no"}\n').encode()
        elif '-v' in arguments:
            stderr = b'pdftoppm version 25.04.0\nCopyright Poppler\n'
        else:
            page = int(arguments[arguments.index('-f') + 1])
            assert int(arguments[arguments.index('-l') + 1]) == page
            assert '-singlefile' in arguments and '-png' in arguments
            assert Path(arguments[-2]).read_bytes() == PDF_BYTES
            if state['failure_page'] == page:
                returncode, stderr = 1, b'Syntax Error: broken page\n'
            else:
                path = Path(arguments[-1] + '.png')
                if state['malformed_page'] == page:
                    path.write_bytes(b'not a PNG')
                else:
                    with Image.new('RGB', (state['dimension'], state['dimension']), (page, 20, 30)) as image:
                        image.save(path, format='PNG')
        return SimpleNamespace(stdout=pipe(stdout), stderr=pipe(stderr), pid=123456789,
            returncode=returncode, wait=lambda **kwargs: returncode)

    monkeypatch.setattr(public_pdf.subprocess, 'Popen', launch)
    return state


def render(**limits):
    values = {'deadline': time.monotonic() + 30, 'max_pages': 64,
              'max_image_bytes': 1024 * 1024, 'max_media_bytes': 2 * 1024 * 1024,
              'max_image_pixels': 1000000, 'max_total_pixels': 2000000}
    values.update(limits)
    return public_pdf.render_pdf_pages(PDF_BYTES, **values)


def test_pdf_renderer_returns_every_page_in_order_with_actual_provenance_and_cleans_temporary_files(local_poppler):
    rendered = render()
    assert rendered['page_count'] == 2
    assert rendered['renderer']['name'] == 'pdftoppm'
    assert rendered['renderer']['version'] == '25.04.0'
    assert rendered['renderer']['page_box'] == 'media'
    assert [page['page_number'] for page in rendered['pages']] == [1, 2]
    for page in rendered['pages']:
        with Image.open(io.BytesIO(page['data'])) as image:
            assert image.getpixel((0, 0)) == (page['page_number'], 20, 30)
            assert image.size == (page['width'], page['height'])
    assert all(not path.exists() for path in local_poppler['directories'])
    assert all(command[0].endswith('/prlimit') for command in local_poppler['commands'])
    assert all(any(argument.startswith('--as=') for argument in command) for command in local_poppler['commands'])


@pytest.mark.parametrize('change,limits,code', [
    ({'pages': 65}, {}, 'source_pdf_too_large'),
    ({'encrypted': True}, {}, 'source_pdf_encrypted'),
    ({'failure_page': 2}, {}, 'source_pdf_invalid'),
    ({'malformed_page': 2}, {}, 'source_pdf_invalid'),
    ({}, {'max_media_bytes': 20}, 'source_pdf_too_large'),
    ({'dimension': 100}, {'max_image_pixels': 100}, 'source_pdf_too_large'),
])
def test_pdf_limits_and_bad_later_pages_reject_the_document_not_a_successful_prefix(local_poppler, change, limits, code):
    local_poppler.update(change)
    with pytest.raises(public_pdf.PublicPdfError) as caught:
        render(**limits)
    assert caught.value.code == code
    assert all(not path.exists() for path in local_poppler['directories'])
    if local_poppler['pages'] > 64 or local_poppler['encrypted']:
        assert not any('-png' in command for command in local_poppler['commands'])


def test_pdf_deadline_expires_before_starting_a_local_process(local_poppler):
    with pytest.raises(public_pdf.PublicPdfError) as caught:
        render(deadline=time.monotonic() - 1)
    assert caught.value.code == 'source_pdf_timeout'
    assert local_poppler['commands'] == []


def test_public_pdf_capture_keeps_html_pdf_pages_parent_identity_and_global_indices(monkeypatch, local_poppler):
    requested = []

    def transport(url, *args):
        requested.append(url)
        if url == PARENT:
            return HTML_BYTES, 'text/html; charset=utf-8', PARENT
        assert url == PDF_URL
        return PDF_BYTES, 'application/pdf', PDF_URL

    monkeypatch.setattr(acquisition, 'fetch_public_resource', transport)
    cache = {}
    sources, attempts, media, mismatched, source_media = repair._capture_sources({'source_urls': [PARENT]}, cache)
    assert requested == [PARENT, PDF_URL]
    assert attempts == [] and mismatched is False
    document = cache[PARENT]['document']
    assert document['capture_complete'] is True
    assert document['text'] == '' and document['images'] == []
    assert document['pdf_sources'] == [PDF_URL]
    assert source_media[0]['url'] == PARENT
    assert source_media[0]['skipped_media'] == []
    assert base64.b64decode(document['raw_response_base64']) == HTML_BYTES
    pdf = document['pdf_documents'][0]
    assert base64.b64decode(pdf['raw_response_base64']) == PDF_BYTES
    assert pdf['raw_response_digest'] == hashlib.sha256(PDF_BYTES).hexdigest()
    assert pdf['render_complete'] is True and pdf['page_count'] == 2
    assert [page['page_number'] for page in pdf['pages']] == [1, 2]
    assert [image['source_pdf_page'] for image in media] == [1, 2]
    assert all(image['source_pdf_digest'] == pdf['raw_response_digest'] for image in media)
    assert all(image['source_pdf_url'] == PDF_URL and image['url'] == PARENT for image in media)
    assert [image['digest'] for image in media] == [page['digest'] for page in pdf['pages']]
    assert [image['image_index'] for image in sources[0]['images']] == [0, 1]
    assert all(image['url'] == PARENT for image in sources[0]['images'])
    assert all(set(image) == {'image_index', 'url', 'sha256', 'mime', 'data'} for image in sources[0]['images'])


def test_broken_later_pdf_page_retains_original_pdf_without_admitting_a_prefix(monkeypatch, local_poppler):
    local_poppler['failure_page'] = 2
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda url, *args:
        (HTML_BYTES, 'text/html', PARENT) if url == PARENT else (PDF_BYTES, 'application/pdf', PDF_URL))
    cache = {}
    sources, attempts, media, _, _ = repair._capture_sources({'source_urls': [PARENT]}, cache)
    document = cache[PARENT]['document']
    assert document['capture_complete'] is False
    assert document['capture_errors'][0]['code'] == 'source_pdf_invalid'
    assert base64.b64decode(document['pdf_documents'][0]['raw_response_base64']) == PDF_BYTES
    assert document['pdf_documents'][0]['render_complete'] is False
    assert media == [] and sources[0]['images'] == []
    assert attempts[0]['status'] == 'media_unavailable'


def test_immutable_source_reparse_discovers_pdf_without_refetching_html_or_reusing_old_media(monkeypatch):
    original = {'canonical_url': PARENT, 'resolved_url': PARENT, 'content_type': 'text/html',
        'raw_response_base64': base64.b64encode(HTML_BYTES).decode(),
        'raw_response_digest': hashlib.sha256(HTML_BYTES).hexdigest(),
        'fetched_at': '2026-03-31T03:00:00+00:00', 'text': 'old header-only extraction',
        'media': [{'url': 'https://wrong.example/old-image.png'}]}
    before = dict(original)
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda *args: pytest.fail('Reparse must not fetch.'))
    reparsed = acquisition.reparse_source_document(original)
    assert original == before
    assert reparsed['pdf_sources'] == [PDF_URL]
    assert reparsed['text'] == '' and 'media' not in reparsed
    assert reparsed['fetched_at'] == original['fetched_at']
    assert reparsed['raw_response_digest'] == original['raw_response_digest']
    assert reparsed['source_capture_version'] == acquisition.SOURCE_CAPTURE_VERSION


def test_immutable_source_reparse_rejects_changed_original_bytes_without_network(monkeypatch):
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda *args: pytest.fail('Reparse must not fetch.'))
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.reparse_source_document({'canonical_url': PARENT, 'resolved_url': PARENT,
            'content_type': 'text/html', 'fetched_at': '2026-03-31T03:00:00+00:00',
            'raw_response_base64': base64.b64encode(HTML_BYTES).decode(), 'raw_response_digest': '0' * 64})
    assert caught.value.code == 'source_invalid_capture'


@pytest.fixture
def captured_pdf(monkeypatch, local_poppler):
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda url, *args:
        (HTML_BYTES, 'text/html', PARENT) if url == PARENT else (PDF_BYTES, 'application/pdf', PDF_URL))
    cache = {}
    _, _, media, _, _ = repair._capture_sources({'source_urls': [PARENT]}, cache)
    return cache[PARENT]['document'], media


def test_complete_pdf_capture_validates_without_network_or_subprocesses(monkeypatch, captured_pdf):
    document, media = captured_pdf
    monkeypatch.setattr(public_pdf.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Validation must not render.'))
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda *args: pytest.fail('Validation must not fetch.'))
    public_pdf.validate_pdf_capture(document, media, require_complete=True)


@pytest.mark.parametrize('mutation', ['original_bytes', 'missing_page', 'duplicate_page', 'zero_page',
    'wrong_parent', 'invented_image_url', 'wrong_original_digest', 'wrong_dimensions', 'missing_original',
    'missing_renderer_version', 'bad_resolved_url', 'changed_png_bytes'])
def test_pdf_provenance_validator_rejects_unbound_or_incomplete_evidence(captured_pdf, mutation):
    document, media = deepcopy(captured_pdf)
    original = document['pdf_documents'][0]
    if mutation == 'original_bytes':
        original['raw_response_base64'] = base64.b64encode(PDF_BYTES + b'changed').decode()
    elif mutation == 'missing_page':
        media.pop()
    elif mutation == 'duplicate_page':
        media[1] = dict(media[0])
    elif mutation == 'zero_page':
        media[0]['source_pdf_page'] = 0
    elif mutation == 'wrong_parent':
        media[0]['url'] = PDF_URL
    elif mutation == 'invented_image_url':
        media[0]['url'] = PARENT + '#page-1.png'
    elif mutation == 'wrong_original_digest':
        media[0]['source_pdf_digest'] = '0' * 64
    elif mutation == 'wrong_dimensions':
        original['pages'][0]['width'] += 1
        media[0]['width'] += 1
    elif mutation == 'missing_original':
        document['pdf_documents'] = []
    elif mutation == 'missing_renderer_version':
        original['renderer']['version'] = ''
    elif mutation == 'bad_resolved_url':
        original['resolved_url'] = 'file:///private/source.pdf'
    elif mutation == 'changed_png_bytes':
        raw = b'not actually a PNG'
        media[0]['data'] = base64.b64encode(raw).decode()
        media[0]['digest'] = hashlib.sha256(raw).hexdigest()
        original['pages'][0]['digest'] = media[0]['digest']
    with pytest.raises(ValueError):
        public_pdf.validate_pdf_capture(document, media, require_complete=True)


def test_original_pdf_bytes_share_the_media_budget_and_are_retained_when_rendering_cannot_fit(monkeypatch, local_poppler):
    monkeypatch.setattr(acquisition, 'MAX_MEDIA_BYTES', len(PDF_BYTES) + 1)
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda url, *args:
        (HTML_BYTES, 'text/html', PARENT) if url == PARENT else (PDF_BYTES, 'application/pdf', PDF_URL))
    cache = {}
    _, attempts, media, _, _ = repair._capture_sources({'source_urls': [PARENT]}, cache)
    document = cache[PARENT]['document']
    assert document['capture_complete'] is False
    assert base64.b64decode(document['pdf_documents'][0]['raw_response_base64']) == PDF_BYTES
    assert media == [] and attempts[0]['status'] == 'media_unavailable'
    public_pdf.validate_pdf_capture(document, media, require_complete=False)
    with pytest.raises(ValueError):
        public_pdf.validate_pdf_capture(document, media, require_complete=True)


def test_missing_local_pdf_renderer_is_an_explicit_incomplete_source_not_header_success(monkeypatch, local_poppler):
    monkeypatch.setattr(public_pdf.shutil, 'which', lambda name: None)
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda url, *args:
        (HTML_BYTES, 'text/html', PARENT) if url == PARENT else (PDF_BYTES, 'application/pdf', PDF_URL))
    cache = {}
    _, attempts, media, _, _ = repair._capture_sources({'source_urls': [PARENT]}, cache)
    document = cache[PARENT]['document']
    assert document['capture_complete'] is False
    assert document['capture_errors'][0]['code'] == 'source_pdf_renderer_unavailable'
    assert base64.b64decode(document['pdf_documents'][0]['raw_response_base64']) == PDF_BYTES
    assert media == [] and attempts[0]['status'] == 'media_unavailable'


def test_embedded_pdf_fetch_keeps_existing_private_address_rejection(monkeypatch):
    monkeypatch.setattr(acquisition, '_source_proxy', lambda parts: None)
    monkeypatch.setattr(acquisition, '_resolve_addresses', lambda *args: ['127.0.0.1'])
    monkeypatch.setattr(acquisition, '_PinnedHTTP', lambda *args: pytest.fail('A private PDF origin must never be connected.'))
    document = {'canonical_url': PARENT, 'pdf_sources': ['http://127.0.0.1/private.pdf'], 'images': [],
                'capture_complete': True, 'capture_errors': []}
    assert acquisition._prepare_media(document) == []
    assert document['capture_complete'] is False
    assert document['capture_errors'][0]['code'] == 'unsafe_source'


def test_video_and_gif_exclusions_do_not_exclude_literal_pdf_iframes_or_pdf_pages(monkeypatch, local_poppler):
    raw = ('<div class="wp_articlecontent"><iframe src="' + PDF_URL + '"></iframe>'
           '<object type="application/pdf" data="' + PDF_URL + '"></object>'
           '<img data-src="https://mmbiz.qpic.cn/ignored?wx_fmt=gif">'
           '<iframe class="video_iframe" src="https://video.example.org/embed/123"></iframe></div>').encode()
    requested = []

    def fetch(url, *args):
        requested.append(url)
        if url == PARENT:
            return raw, 'text/html', PARENT
        assert url == PDF_URL
        return PDF_BYTES, 'application/pdf', PDF_URL

    monkeypatch.setattr(acquisition, 'fetch_public_resource', fetch)
    cache = {}
    sources, attempts, media, _, metadata = repair._capture_sources({'source_urls': [PARENT]}, cache)
    assert requested == [PARENT, PDF_URL]
    document = cache[PARENT]['document']
    assert document['pdf_sources'] == [PDF_URL]
    assert document['capture_complete'] is True and attempts == []
    assert {item['type'] for item in document['skipped_media']} == {'gif', 'video'}
    assert all(item['url'] != PDF_URL for item in metadata[0]['skipped_media'])
    assert [image['source_pdf_page'] for image in media] == [1, 2]
    assert [image['image_index'] for image in sources[0]['images']] == [0, 1]
    public_pdf.validate_pdf_capture(document, media, require_complete=True)
