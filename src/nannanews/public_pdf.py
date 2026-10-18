"""Render complete unauthenticated source PDFs locally, never executing PDF scripts.

The caller retains the original HTTP response. Poppler renders every page or this
module fails; partial pages are never returned as a complete document.
"""
import base64
import hashlib
import io
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time

from PIL import Image, UnidentifiedImageError

MAX_DIAGNOSTIC_BYTES = 64 * 1024
MAX_PROCESS_BYTES = 1024 * 1024 * 1024
RENDER_DPI = 144
MAX_RENDER_SIDE = 4096


class PublicPdfError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _run(command, directory, deadline, limiter, *, max_file_bytes):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PublicPdfError('source_pdf_timeout', 'Public PDF rendering exceeded its shared deadline.')
    bounded = [limiter, f'--as={MAX_PROCESS_BYTES}', f'--cpu={max(1, math.ceil(remaining))}',
               f'--fsize={max_file_bytes}', '--nofile=64', '--', *command]
    try:
        process = subprocess.Popen(bounded, cwd=directory, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError:
        raise PublicPdfError('source_pdf_renderer_unavailable', 'The local bounded PDF renderer could not be started.') from None
    output = {'stdout': bytearray(), 'stderr': bytearray()}
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PublicPdfError('source_pdf_timeout', 'Public PDF rendering exceeded its shared deadline.')
                for key, _ in selector.select(min(0.1, remaining)):
                    target = output[key.data]
                    chunk = os.read(key.fileobj.fileno(), min(65536, MAX_DIAGNOSTIC_BYTES + 1 - len(target)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        target.extend(chunk)
                        if len(target) > MAX_DIAGNOSTIC_BYTES:
                            raise PublicPdfError('source_pdf_too_large', 'PDF renderer diagnostics exceeded their byte limit.')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PublicPdfError('source_pdf_timeout', 'Public PDF rendering exceeded its shared deadline.')
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise PublicPdfError('source_pdf_timeout', 'Public PDF rendering exceeded its shared deadline.') from None
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise PublicPdfError('source_pdf_stop_failed', 'The owned PDF renderer could not be stopped within its cleanup limit.') from None
        finally:
            process.stdout.close()
            process.stderr.close()
    stdout, stderr = (bytes(output[name]).decode('utf-8', errors='replace') for name in ('stdout', 'stderr'))
    if process.returncode == -signal.SIGXFSZ:
        raise PublicPdfError('source_pdf_too_large', 'A complete PDF page exceeded the bounded renderer output-file limit.')
    if process.returncode:
        code = 'source_pdf_encrypted' if re.search(r'password|encrypted', stderr, re.I) else 'source_pdf_invalid'
        raise PublicPdfError(code, 'The public PDF was rejected by the local renderer; no partial rendering was admitted.')
    if re.search(r'(?:Syntax Error|^Error:)', stderr, re.M):
        raise PublicPdfError('source_pdf_invalid', 'The PDF renderer reported incomplete or invalid page content.')
    return stdout, stderr


def render_pdf_pages(data, *, deadline, max_pages, max_image_bytes, max_media_bytes,
                     max_image_pixels, max_total_pixels):
    """Return all 1-based PNG pages plus actual renderer provenance, or fail.

    Image scaling covers the entire media box, not a crop. Original PDF bytes
    remain the evidence of record; no OCR or guessed text is produced.
    """
    if (not isinstance(data, bytes) or not re.match(rb'%PDF-\d\.\d', data[:16])
            or b'%%EOF' not in data[-2048:]):
        raise PublicPdfError('source_pdf_invalid', 'The source response is not a complete PDF document.')
    if min(max_pages, max_image_bytes, max_media_bytes, max_image_pixels, max_total_pixels) <= 0:
        raise PublicPdfError('source_pdf_too_large', 'The complete PDF does not fit the remaining source evidence limits.')
    tools = {name: shutil.which(name) for name in ('pdfinfo', 'pdftoppm', 'prlimit')}
    if not all(tools.values()):
        raise PublicPdfError('source_pdf_renderer_unavailable', 'Public PDFs require the local pdfinfo, pdftoppm and prlimit tools.')
    with tempfile.TemporaryDirectory(prefix='nanna-public-pdf-') as directory:
        root = Path(directory)
        source = root / 'source.pdf'
        source.write_bytes(data)
        info, info_warnings = _run([tools['pdfinfo'], str(source)], root, deadline,
            tools['prlimit'], max_file_bytes=max_image_bytes)
        count = re.search(r'^Pages:\s+(\d+)\s*$', info, re.M)
        encrypted = re.search(r'^Encrypted:\s+(yes|no)\b', info, re.M | re.I)
        if encrypted is not None and encrypted.group(1).lower() == 'yes':
            raise PublicPdfError('source_pdf_encrypted', 'Encrypted or password-protected public PDFs are not imported.')
        if count is None or encrypted is None:
            raise PublicPdfError('source_pdf_invalid', 'The PDF renderer did not report a complete page count and encryption state.')
        page_count = int(count.group(1))
        if not 1 <= page_count <= max_pages:
            raise PublicPdfError('source_pdf_too_large', 'Every PDF page must fit the remaining captured-image count; no prefix was imported.')
        max_side = min(MAX_RENDER_SIDE, math.isqrt(min(max_image_pixels, max_total_pixels // page_count)))
        if max_side <= 0:
            raise PublicPdfError('source_pdf_too_large', 'Every PDF page must fit the remaining captured-pixel budget.')
        version_out, version_err = _run([tools['pdftoppm'], '-v'], root, deadline,
            tools['prlimit'], max_file_bytes=max_image_bytes)
        version = re.search(r'pdftoppm version\s+(\S+)', version_out + '\n' + version_err)
        if version is None:
            raise PublicPdfError('source_pdf_renderer_unavailable', 'The PDF renderer did not identify its actual version.')
        renderer = {'name': 'pdftoppm', 'version': version.group(1), 'dpi': RENDER_DPI,
                    'max_side': max_side, 'page_box': 'media', 'info_diagnostics': info_warnings}
        pages, total_bytes, total_pixels = [], 0, 0
        for page_number in range(1, page_count + 1):
            page_limit = min(max_image_bytes, max_media_bytes - total_bytes)
            if page_limit <= 0:
                raise PublicPdfError('source_pdf_too_large', 'The complete PDF exceeds the remaining captured-media byte budget.')
            prefix = root / f'page-{page_number}'
            _, warnings = _run([tools['pdftoppm'], '-f', str(page_number), '-l', str(page_number),
                '-singlefile', '-r', str(RENDER_DPI), '-scale-to', str(max_side), '-png',
                str(source), str(prefix)], root, deadline, tools['prlimit'], max_file_bytes=page_limit)
            path = prefix.with_suffix('.png')
            try:
                with path.open('rb') as handle:
                    rendered = handle.read(page_limit + 1)
                if not rendered or len(rendered) > page_limit:
                    raise PublicPdfError('source_pdf_too_large', 'A complete rendered PDF page exceeds its captured-byte budget.')
                with Image.open(io.BytesIO(rendered)) as image:
                    width, height = image.size
                    pixels = width * height
                    if (image.format != 'PNG' or min(width, height) <= 0
                            or max(width, height) > max_side or pixels > max_image_pixels
                            or total_pixels + pixels > max_total_pixels
                            or getattr(image, 'n_frames', 1) != 1):
                        raise PublicPdfError('source_pdf_too_large', 'The PDF renderer exceeded its complete-page pixel budget.')
                    image.load()
            except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as error:
                if isinstance(error, PublicPdfError):
                    raise
                raise PublicPdfError('source_pdf_invalid', 'The PDF renderer did not produce a complete valid PNG for every page.') from None
            total_bytes += len(rendered)
            total_pixels += pixels
            pages.append({'page_number': page_number, 'width': width, 'height': height,
                          'data': rendered, 'diagnostics': warnings})
        return {'page_count': page_count, 'renderer': renderer, 'pages': pages}


def validate_pdf_capture(document, media, *, require_complete=False):
    """Validate retained PDF originals and their 1-based derived-page bindings.

    Generic HTML/media hashes and ordinary-image correspondence remain the
    caller's responsibility. This validator never reparses or fetches a PDF.
    """
    from nannanews import acquisition

    declared = document.get('pdf_sources', [])
    originals = document.get('pdf_documents', [])
    if (not isinstance(declared, list) or len(declared) > acquisition.MAX_IMAGES
            or not isinstance(originals, list) or len(originals) > len(declared)):
        raise ValueError('Invalid retained public PDF source list.')
    if not isinstance(media, list) or len(media) > acquisition.MAX_IMAGES:
        raise ValueError('PDF-derived media exceed the document captured-image count.')
    for url in declared:
        try:
            valid = isinstance(url, str) and acquisition.canonical_url(url) == url
        except acquisition.AcquisitionError:
            valid = False
        if not valid:
            raise ValueError('Invalid exact public PDF identity.')
    if len(set(declared)) != len(declared):
        raise ValueError('Duplicate declared public PDF identity.')
    by_url, expected, total_bytes, total_pixels = {}, {}, 0, 0
    for original in originals:
        if not isinstance(original, dict):
            raise ValueError('Invalid retained public PDF record.')
        url = original.get('canonical_url')
        if not isinstance(url, str) or url not in declared or url in by_url:
            raise ValueError('Retained PDF does not match exactly one declared source.')
        try:
            resolved = acquisition.canonical_url(original.get('resolved_url'))
        except acquisition.AcquisitionError:
            raise ValueError('Retained PDF resolved URL is invalid.') from None
        if (resolved != original.get('resolved_url')
                or type(original.get('http_status')) is not int or original['http_status'] != 200
                or original.get('raw_response_complete') is not True
                or not isinstance(original.get('content_type'), str)
                or not isinstance(original.get('fetched_at'), str) or not original['fetched_at']
                or type(original.get('render_complete')) is not bool):
            raise ValueError('Retained PDF HTTP response metadata is invalid.')
        encoded = original.get('raw_response_base64')
        if not isinstance(encoded, str) or len(encoded) > 4 * math.ceil(acquisition.MAX_IMAGE / 3):
            raise ValueError('Retained original PDF exceeds its source-byte limit.')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ValueError('Retained original PDF bytes are invalid.') from None
        if not raw or len(raw) > acquisition.MAX_IMAGE or hashlib.sha256(raw).hexdigest() != original.get('raw_response_digest'):
            raise ValueError('Retained original PDF response digest is invalid.')
        total_bytes += len(raw)
        if total_bytes > acquisition.MAX_MEDIA_BYTES:
            raise ValueError('Retained PDFs exceed the document source-byte limit.')
        by_url[url] = original
        pages = original.get('pages')
        if not isinstance(pages, list):
            raise ValueError('Retained PDF page catalog is invalid.')
        if not original['render_complete']:
            if require_complete or pages or original.get('page_count') is not None or original.get('renderer') is not None:
                raise ValueError('Incomplete PDF rendering cannot be admitted as complete evidence.')
            continue
        if not re.match(rb'%PDF-\d\.\d', raw[:16]) or b'%%EOF' not in raw[-2048:]:
            raise ValueError('Rendered PDF original is not a complete PDF response.')
        count, renderer = original.get('page_count'), original.get('renderer')
        if type(count) is not int or not 1 <= count <= acquisition.MAX_IMAGES or len(pages) != count:
            raise ValueError('Retained PDF does not include every declared page.')
        if (not isinstance(renderer, dict) or renderer.get('name') != 'pdftoppm'
                or not isinstance(renderer.get('version'), str) or not renderer['version'] or len(renderer['version']) > 100
                or renderer.get('dpi') != RENDER_DPI or renderer.get('page_box') != 'media'
                or type(renderer.get('max_side')) is not int or not 1 <= renderer['max_side'] <= MAX_RENDER_SIDE
                or not isinstance(renderer.get('info_diagnostics'), str)
                or len(renderer['info_diagnostics'].encode()) > MAX_DIAGNOSTIC_BYTES):
            raise ValueError('Retained PDF renderer provenance is invalid.')
        for index, page in enumerate(pages, 1):
            if (not isinstance(page, dict) or type(page.get('page_number')) is not int or page['page_number'] != index
                    or type(page.get('width')) is not int or type(page.get('height')) is not int
                    or min(page['width'], page['height']) <= 0
                    or max(page['width'], page['height']) > renderer['max_side']
                    or page['width'] * page['height'] > acquisition.MAX_IMAGE_PIXELS
                    or not isinstance(page.get('digest'), str) or re.fullmatch('[0-9a-f]{64}', page['digest']) is None
                    or not isinstance(page.get('diagnostics'), str)
                    or len(page['diagnostics'].encode()) > MAX_DIAGNOSTIC_BYTES):
                raise ValueError('Retained PDF page order, dimensions or digest is invalid.')
            expected[(url, index)] = page
            total_pixels += page['width'] * page['height']
        if total_pixels > acquisition.MAX_DOCUMENT_FRAME_PIXELS:
            raise ValueError('Retained PDF pages exceed the document pixel limit.')
    if require_complete and set(by_url) != set(declared):
        raise ValueError('At least one declared PDF original was not captured.')
    seen = set()
    provenance_keys = {'source_pdf_url', 'source_pdf_digest', 'source_pdf_page'}
    for image in media:
        if not isinstance(image, dict):
            raise ValueError('Captured PDF media record is invalid.')
        encoded = image.get('data')
        if not isinstance(encoded, str):
            raise ValueError('Captured media bytes are invalid.')
        # The caller validates ordinary media base64/hashes; avoid decoding it twice.
        total_bytes += len(encoded) * 3 // 4 - (2 if encoded.endswith('==') else 1 if encoded.endswith('=') else 0)
        if total_bytes > acquisition.MAX_MEDIA_BYTES:
            raise ValueError('Original PDFs and captured media exceed the shared source-byte limit.')
        if not provenance_keys.intersection(image):
            continue
        if not provenance_keys.issubset(image):
            raise ValueError('PDF-derived image provenance is incomplete.')
        url, number = image['source_pdf_url'], image['source_pdf_page']
        if not isinstance(url, str) or type(number) is not int:
            raise ValueError('PDF-derived image identity or 1-based page number is invalid.')
        key = (url, number)
        if key not in expected or key in seen:
            raise ValueError('PDF-derived image is duplicated or has no retained page.')
        page, original = expected[key], by_url[url]
        if (image.get('url') != document.get('canonical_url') or image.get('mime') != 'image/png'
                or image.get('source_pdf_digest') != original['raw_response_digest']
                or image.get('digest') != page['digest']
                or image.get('width') != page['width'] or image.get('height') != page['height']):
            raise ValueError('PDF-derived image differs from its original PDF or parent primary source.')
        if len(encoded) > 4 * math.ceil(acquisition.MAX_IMAGE / 3):
            raise ValueError('PDF-derived image exceeds its captured-byte limit.')
        try:
            raw = base64.b64decode(encoded, validate=True)
            if not raw or len(raw) > acquisition.MAX_IMAGE or hashlib.sha256(raw).hexdigest() != page['digest']:
                raise ValueError('PDF-derived image digest does not match its actual bytes.')
            with Image.open(io.BytesIO(raw)) as rendered:
                if rendered.format != 'PNG' or rendered.size != (page['width'], page['height']) or getattr(rendered, 'n_frames', 1) != 1:
                    raise ValueError('PDF-derived image dimensions or format do not match its provenance.')
                rendered.verify()
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
            raise ValueError('PDF-derived page bytes or dimensions are invalid.') from None
        seen.add(key)
    if seen != set(expected):
        raise ValueError('Retained PDF page catalog does not correspond to every captured derived image.')
