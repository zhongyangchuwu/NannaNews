"""Raw-source immutability and complete, bounded single-request vision input."""
import base64
from copy import deepcopy
import hashlib
import io
import json
import math
from pathlib import Path
import struct

from types import SimpleNamespace
from unittest.mock import patch
import zlib

import pytest
from flask import Flask
from PIL import Image


from nannanews import acquisition
from nannanews import runtime


def image_bytes(fmt='PNG', size=(80, 60), color='red', **kwargs):
    with Image.new('RGB', size, color) as image:
        output = io.BytesIO()
        image.save(output, format=fmt, **kwargs)
        return output.getvalue()


def model_settings(data_dir):
    return SimpleNamespace(model_base_url='https://model.example/v1', model_api_key='test-only',
                           model_name='gpt-6-luna', model_api_style='responses', model_timeout=20, data_dir=data_dir)


def test_six_article_images_are_retained_and_sent_once_with_valid_final_reference(tmp_path):
    url = 'https://example.org/article'
    urls = [f'https://example.org/{index}.png' for index in range(6)]
    html = ('<article><h1>Public article</h1><p>Public announcement with six content-bearing images.</p>' +
            ''.join(f'<img src="{source}">' for source in urls) + '</article>').encode()
    originals = [image_bytes(color=(index * 35, 20, 80)) for index in range(6)]

    def fetch(source, *args):
        return (html, 'text/html', source) if source == url else (originals[urls.index(source)], 'image/png', source)

    item = {'title':'Public event', 'description':'Model-selected draft description',
            'tag':'通知',
            'evidence_quotes':[{'text':'Model-selected image note', 'image_index':5}]}
    response = {'status':'completed', 'error':None, 'incomplete_details':None,
                'output':[{'type':'message', 'role':'assistant', 'status':'completed',
                           'content':[{'type':'output_text', 'text':json.dumps({'items':[item]})}]}]}
    with patch.object(acquisition, 'fetch_public_resource', side_effect=fetch), \
            patch.object(runtime, 'settings', model_settings(tmp_path)), \
            patch.object(acquisition, '_model_post', return_value=response) as post:
        document = acquisition.fetch_document(url)
        acquisition._prepare_media(document)
        before = deepcopy(document)
        result = acquisition.extract_document(document)
    assert document['capture_complete'] is True
    assert len(document['media']) == 6
    assert document['media'] == before['media']
    assert document['digest'] == before['digest']
    assert [base64.b64decode(image['data']) for image in document['media']] == originals
    assert [image['digest'] for image in document['media']] == [hashlib.sha256(raw).hexdigest() for raw in originals]
    assert result[0]['evidence_quotes'][0]['image_index'] == 5
    post.assert_called_once()
    parts = post.call_args.args[0]['input'][0]['content']
    assert len(parts) == 7
    assert all(part['image_url'].startswith('data:image/jpeg;base64,') for part in parts[1:])


@pytest.mark.parametrize('fmt,mime', [('PNG','image/png'), ('JPEG','image/jpeg'), ('WEBP','image/webp'),
                                    ('GIF','image/gif'), ('BMP','image/bmp'), ('TIFF','image/tiff')])
def test_common_formats_normalize_without_rewriting_evidence(fmt, mime):
    raw = image_bytes(fmt)
    original = acquisition._image(raw, 'https://example.org/image')
    before = dict(original)
    preview = acquisition._model_images([original])[0]
    assert original == before
    assert original['mime'] == mime
    assert base64.b64decode(original['data']) == raw
    assert original['digest'] == hashlib.sha256(raw).hexdigest()
    assert preview['mime'] == 'image/jpeg'
    with Image.open(io.BytesIO(base64.b64decode(preview['data']))) as decoded:
        decoded.load()
        assert decoded.size == (80, 60)


def test_large_source_bytes_and_exif_orientation_survive_full_frame_normalization():
    raw = image_bytes('BMP', size=(1300, 1000))
    assert len(raw) > 3 * 1024 * 1024
    retained = acquisition._image(raw)
    assert base64.b64decode(retained['data']) == raw
    acquisition._model_images([retained])
    exif = Image.Exif()
    exif[274] = 6
    oriented = acquisition._image(image_bytes('JPEG', size=(80, 40), exif=exif))
    preview = acquisition._model_images([oriented])[0]
    with Image.open(io.BytesIO(base64.b64decode(preview['data']))) as decoded:
        assert decoded.size == (40, 80)
    assert base64.b64decode(oriented['data']) == image_bytes('JPEG', size=(80, 40), exif=exif)


@pytest.mark.parametrize('fmt', ['GIF', 'TIFF', 'PNG', 'WEBP', 'MPO'])
def test_every_animation_frame_or_tiff_page_appears_in_one_contact_sheet(fmt):
    frames = [Image.new('RGB', (60, 60), color) for color in ('red', 'lime', 'blue')]
    try:
        output = io.BytesIO()
        frames[0].save(output, format=fmt, save_all=True, append_images=frames[1:], duration=100, loop=0)
        original = acquisition._image(output.getvalue())
        before = dict(original)
        preview = acquisition._model_images([original])[0]
        with Image.open(io.BytesIO(base64.b64decode(preview['data']))) as decoded:
            # Small frames need labels, not a vision-budget-sized blank canvas.
            assert decoded.width * decoded.height <= 4 * sum(frame.width * frame.height for frame in frames)
            cell = decoded.width // 2
            samples = [decoded.getpixel((cell // 2, 50)),
                       decoded.getpixel((cell + cell // 2, 50)),
                       decoded.getpixel((cell // 2, cell + 50))]
            assert samples[0][0] > 200 and samples[0][1] < 30
            assert samples[1][1] > 200 and samples[1][0] < 30
            assert samples[2][2] > 200 and samples[2][0] < 30
        assert original == before
    finally:
        for frame in frames:
            frame.close()


def test_frame_limit_rejects_complete_model_input_instead_of_first_frame_success():
    frames = [Image.new('RGB', (2, 2), (index * 3, 10, 20)) for index in range(65)]
    try:
        output = io.BytesIO()
        frames[0].save(output, format='TIFF', save_all=True, append_images=frames[1:])
        original = acquisition._image(output.getvalue())
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition._model_images([original])
        assert caught.value.code == 'image_unsupported'
        assert base64.b64decode(original['data']) == output.getvalue()
    finally:
        for frame in frames:
            frame.close()


def test_invalid_and_bomb_images_fail_before_any_model_post(tmp_path):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    bomb = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', 200000, 200000, 8, 2, 0, 0, 0)) + chunk(b'IDAT', b'') + chunk(b'IEND', b'')
    for raw in (b'not an image', b'\x89PNG\r\n\x1a\ntruncated', bomb):
        original = acquisition._image(raw)
        with patch.object(runtime, 'settings', model_settings(tmp_path)), patch.object(acquisition, '_model_post') as post:
            with pytest.raises(acquisition.AcquisitionError):
                acquisition.request_model_json('Instructions', {}, [original])
            post.assert_not_called()
        assert base64.b64decode(original['data']) == raw


def test_source_over_400m_pixels_is_retained_without_decode_or_send():
    raw = bytearray(image_bytes('JPEG'))
    sof = raw.index(b'\xff\xc0')
    raw[sof + 5:sof + 9] = struct.pack('>HH', 20000, 20001)
    raw = bytes(raw)
    original = acquisition._image(raw, 'https://example.org/poster.jpg')
    document = {'canonical_url': 'https://example.org/article', 'text': 'Public poster',
                'capture_complete': True, 'media': [original]}
    before = deepcopy(document)
    with patch.object(Image.Image, 'load', side_effect=AssertionError('oversized source must not decode')), \
            patch.object(acquisition, '_model_post') as post:
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition.preflight_document_media(document)
    assert caught.value.code == 'invalid_image'
    assert document == before
    assert base64.b64decode(document['media'][0]['data']) == raw
    post.assert_not_called()


@pytest.mark.parametrize('fmt,mode', [('JPEG', 'RGB'), ('JPEG', 'CMYK'), ('PNG', 'RGBA')])
def test_large_single_frame_has_bounded_orientation_copy_and_unchanged_raw(fmt, mode):
    exif = Image.Exif()
    exif[274] = 6
    with Image.new(mode, (2048, 1536), (0, 255, 255, 0) if mode == 'CMYK' else 'red') as image:
        output = io.BytesIO()
        image.save(output, format=fmt, exif=exif)
    raw = output.getvalue()
    original = acquisition._image(raw)
    before = deepcopy(original)
    transpose = acquisition.ImageOps.exif_transpose
    def bounded_transpose(image):
        assert image.width * image.height <= 256 * 1024
        return transpose(image)
    with patch.object(acquisition.ImageOps, 'exif_transpose', side_effect=bounded_transpose):
        with acquisition._raster_preview(raw, 0, 256)[0] as preview:
            assert preview.height > preview.width
            assert math.ceil(preview.width / 32) * math.ceil(preview.height / 32) <= 256
            red, green, blue = preview.getpixel((preview.width // 2, preview.height // 2))
            assert red > 200 and green < 40 and blue < 40
    assert original == before
    assert base64.b64decode(original['data']) == raw


def test_reduced_jpeg_still_rejects_truncated_stream():
    raw = image_bytes('JPEG', size=(2048, 1536))[:-100]
    original = acquisition._image(raw)
    before = deepcopy(original)
    with pytest.raises(OSError):
        acquisition._raster_preview(raw, 0, 256)
    assert original == before


def test_truncated_second_mpo_frame_is_not_omitted():
    frames = [Image.new('RGB', (80, 60), color) for color in ('red', 'blue')]
    try:
        output = io.BytesIO()
        frames[0].save(output, format='MPO', save_all=True, append_images=frames[1:])
        raw = output.getvalue()[:-100]
        with Image.open(io.BytesIO(raw)) as first:
            first.load()
        original = acquisition._image(raw)
        before = deepcopy(original)
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition._model_images([original])
        assert caught.value.code == 'invalid_image'
        assert original == before
    finally:
        for frame in frames:
            frame.close()


def test_all_tiff_pages_count_toward_image_pixel_budget(monkeypatch):
    frames = [Image.new('RGB', (40, 40), color) for color in ('red', 'blue')]
    try:
        output = io.BytesIO()
        frames[0].save(output, format='TIFF', save_all=True, append_images=frames[1:])
        original = acquisition._image(output.getvalue())
        before = deepcopy(original)
        monkeypatch.setattr(acquisition, 'MAX_IMAGE_FRAME_PIXELS', 3199)
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition._model_images([original])
        assert caught.value.code == 'invalid_image'
        assert original == before
    finally:
        for frame in frames:
            frame.close()


def test_missing_middle_image_keeps_available_raw_and_blocks_text_only_success(tmp_path):
    urls = [f'https://example.org/{index}.png' for index in range(3)]
    raw = image_bytes()
    document = {'canonical_url':'https://example.org/article', 'title':'Article', 'text':'Public prose',
                'source_type':'public_html', 'images':urls}
    def fetch(url, *args):
        if url == urls[1]:
            raise acquisition.AcquisitionError('Network unavailable', 'source_unavailable')
        return raw, 'image/png', url
    with patch.object(acquisition, 'fetch_public_resource', side_effect=fetch):
        acquisition._prepare_media(document)
    assert document['capture_complete'] is False
    assert document['capture_errors'][0]['image_index'] == 1
    assert [image['url'] for image in document['media']] == [urls[0], urls[2]]
    assert document['media'][1]['source_image_index'] == 2
    with patch.object(runtime, 'settings', model_settings(tmp_path)), patch.object(acquisition, '_model_post') as post:
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition.extract_document(document)
        assert caught.value.code == 'source_incomplete'
        post.assert_not_called()


def test_model_aggregate_byte_limit_refuses_all_in_one_request_without_partial_post(monkeypatch, tmp_path):
    media = [acquisition._image(image_bytes(color=color)) for color in ('red', 'blue')]
    single = acquisition._model_images(media[:1])[0]
    monkeypatch.setattr(acquisition, 'MAX_MODEL_MEDIA_BYTES', len(base64.b64decode(single['data'])) + 10)
    with patch.object(runtime, 'settings', model_settings(tmp_path)), patch.object(acquisition, '_model_post') as post:
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition.request_model_json('Instructions', {}, media)
        assert caught.value.code == 'image_unsupported'
        post.assert_not_called()


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40"><rect width="80" height="40" fill="red"/></svg>'


def test_svg_original_is_private_immutable_and_preview_is_inert_raster():
    original = acquisition._image(SVG, 'https://example.org/poster.svg')
    before = dict(original)
    preview = acquisition._model_images([original])[0]
    assert preview['mime'] == 'image/jpeg'
    assert original == before
    assert original['mime'] == 'image/svg+xml'
    app = Flask(__name__)
    with app.test_request_context():
        response = acquisition._image_response(original, 3)
    assert response.mimetype == 'image/jpeg'
    assert not response.data.startswith(b'<')
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert 'sandbox' in response.headers['Content-Security-Policy']
    assert base64.b64decode(original['data']) == SVG


@pytest.mark.parametrize('body', [
    '<script>alert(1)</script>', '<foreignObject><div>active</div></foreignObject>',
    '<image href="http://127.0.0.1/private"/>', '<image href="file:///etc/passwd"/>',
    '<style>@import "https://example.org/private.css";</style>',
    '<rect style="fill:url(https://example.org/private.svg)"/>',
    '<rect style="fill:u\\72l(https://example.org/private.svg)"/>',
    '<animate attributeName="x" values="0;100"/>',
    '<pattern width="999999999" height="999999999"/>',
    '<mask width="999999999" height="999999999"/>',
    '<svg width="999999999" height="999999999"/>',
])
def test_unsafe_or_dynamic_svg_never_fetches_or_reaches_renderer(body):
    raw = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40">' + body + '</svg>').encode()
    original = acquisition._image(raw)
    with patch.object(acquisition.PNGSurface, 'convert') as renderer:
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition._model_images([original])
        assert caught.value.code == 'image_unsupported'
        renderer.assert_not_called()
    assert base64.b64decode(original['data']) == raw


def test_svg_xml_entities_and_excessive_dimensions_are_rejected_without_rendering():
    cases = [b'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg" width="1" height="1">&xxe;</svg>',
             SVG.replace(b'width="80"', b'width="999999999999"')]
    with patch.object(acquisition.PNGSurface, 'convert') as renderer:
        for raw in cases:
            with pytest.raises(acquisition.AcquisitionError):
                acquisition._model_images([acquisition._image(raw)])
        renderer.assert_not_called()


def test_six_slower_images_share_generous_capture_deadline_with_per_image_bound():
    clock = [100.0]
    raw = image_bytes()
    urls = [f'https://example.org/{index}.png' for index in range(7)]
    document = {'text':'Public prose', 'images':urls}
    deadlines = []
    def fetch(url, max_bytes, deadline):
        deadlines.append(deadline)
        assert deadline <= 220.0
        assert deadline - clock[0] <= 30.0
        if clock[0] + 20 > deadline:
            raise acquisition.AcquisitionError('Shared deadline expired', 'source_timeout')
        clock[0] += 20
        return raw, 'image/png', url
    with patch.object(acquisition.time, 'monotonic', side_effect=lambda:clock[0]), \
            patch.object(acquisition, 'fetch_public_resource', side_effect=fetch):
        acquisition._prepare_media(document)
    assert len(document['media']) == 6
    assert document['capture_complete'] is False
    assert document['capture_errors'][0]['image_index'] == 6
    assert len(deadlines) == 7


def test_document_image_count_bound_is_explicit_not_silent_truncation():
    raw = image_bytes()
    document = {'text':'Public prose', 'images':[f'https://example.org/{index}.png' for index in range(65)]}
    with patch.object(acquisition,'fetch_public_resource',side_effect=lambda url,*args:(raw,'image/png',url)) as fetch:
        acquisition._prepare_media(document)
    assert len(document['media']) == 64
    assert fetch.call_count == 64
    assert document['capture_complete'] is False
    assert document['capture_errors'][0]['image_index'] == 64
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.preflight_document_media(document)
    assert caught.value.code == 'source_incomplete'


def test_source_aggregate_bound_retains_complete_downloads_and_records_missing_content(monkeypatch):
    raw = image_bytes()
    monkeypatch.setattr(acquisition,'MAX_MEDIA_BYTES',len(raw)*2+1)
    document = {'text':'Public prose', 'images':[f'https://example.org/{index}.png' for index in range(3)]}
    with patch.object(acquisition,'fetch_public_resource',side_effect=lambda url,*args:(raw,'image/png',url)):
        acquisition._prepare_media(document)
    assert len(document['media']) == 2
    assert all(base64.b64decode(image['data']) == raw for image in document['media'])
    assert document['capture_complete'] is False
    assert document['capture_errors'][0]['image_index'] == 2


def test_document_decoded_pixel_bound_rejects_all_input_before_model_post(monkeypatch, tmp_path):
    media = [acquisition._image(image_bytes(size=(40,40))) for _ in range(2)]
    monkeypatch.setattr(acquisition,'MAX_DOCUMENT_FRAME_PIXELS',3000)
    with patch.object(runtime,'settings',model_settings(tmp_path)), patch.object(acquisition,'_model_post') as post:
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition.request_model_json('Instructions',{},media)
        assert caught.value.code == 'image_unsupported'
        post.assert_not_called()


def test_complete_request_byte_bound_includes_source_text_before_paid_post(monkeypatch, tmp_path):
    monkeypatch.setattr(acquisition,'MAX_MODEL_REQUEST_BYTES',1024)
    with patch.object(runtime,'settings',model_settings(tmp_path)), patch.object(acquisition,'_model_post') as post:
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition.request_model_json('Instructions',{'text':'quoted " text '*1000})
        assert caught.value.code == 'model_input_too_large'
        post.assert_not_called()


def test_media_capture_preserves_parser_errors_even_when_remaining_image_downloads_succeed():
    parser_error = {'code':'source_incomplete','message':'PDF viewer page data was not a supported literal.'}
    document = {'text':'Public prose', 'images':['https://example.org/known.png'],
                'capture_complete':False, 'capture_errors':[parser_error]}
    raw = image_bytes()
    with patch.object(acquisition,'fetch_public_resource',return_value=(raw,'image/png',document['images'][0])):
        acquisition._prepare_media(document)
    assert document['capture_complete'] is False
    assert document['capture_errors'] == [parser_error]
    assert base64.b64decode(document['media'][0]['data']) == raw
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.preflight_document_media(document)
    assert caught.value.code == 'source_incomplete'


def test_media_capture_preserves_incomplete_parser_state_without_error_details():
    document = {'text':'', 'images':[], 'capture_complete':False}
    acquisition._prepare_media(document)
    assert document['capture_complete'] is False
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.preflight_document_media(document)
