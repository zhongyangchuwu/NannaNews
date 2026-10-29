"""Complete aggregate admission and bounded, inert inline-PNG SVG recovery."""
import base64
from copy import deepcopy
import hashlib
import io
import struct
from types import SimpleNamespace
from unittest.mock import patch
import zlib

import pytest
from PIL import Image

from nannanews import acquisition, runtime


def image_bytes(size=(16, 16), color='red', mode='RGB', **kwargs):
    with Image.new(mode, size, color) as image:
        output = io.BytesIO()
        image.save(output, format='PNG', **kwargs)
        return output.getvalue()


def png_uri(raw):
    return 'data:image/png;base64,' + base64.b64encode(raw).decode('ascii')


def svg_bytes(body, size=(80, 40)):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{size[0]}" height="{size[1]}">{body}</svg>').encode()


def chunk(kind, data):
    return (struct.pack('!I', len(data)) + kind + data +
            struct.pack('!I', zlib.crc32(kind + data)))


def header_png(width, height, compressed=b''):
    return (b'\x89PNG\r\n\x1a\n' +
            chunk(b'IHDR', struct.pack('!IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', compressed) + chunk(b'IEND', b''))


def model_settings(data_dir):
    return SimpleNamespace(model_base_url='https://model.example/v1', model_api_key='test-only',
                           model_name='gpt-6-luna', model_api_style='responses',
                           model_timeout=20, data_dir=data_dir)


@pytest.fixture
def no_resource_fetch():
    # Cairo's ordinary URL loader handles data:, file:, and HTTP alike. Even
    # successful inline PNG rendering must never fall back to that loader.
    with patch('cairosvg.url.urlopen', side_effect=AssertionError('resource I/O forbidden')) as external, \
            patch.object(acquisition, 'fetch_public_resource',
                         side_effect=AssertionError('source I/O forbidden')) as source:
        yield
        external.assert_not_called()
        source.assert_not_called()


@pytest.mark.parametrize('count', [74, 81, 105, 128])
def test_complete_multi_source_aggregate_preserves_last_image_and_all_evidence(count):
    originals = [image_bytes(color=(index, 30, 220 - index)) for index in range(count)]
    documents = []
    for start in range(0, count, 64):
        document = {'canonical_url': f'https://example.org/article/{start}',
                    'title': 'Public announcement', 'text': 'Complete public source',
                    'source_type': 'public_html', 'images': []}
        acquisition._prepare_media(document, originals[start:start + 64])
        assert document['capture_complete'] is True
        assert not document['capture_errors']
        documents.append(document)
    before = deepcopy(documents)
    media = [image for document in documents for image in document['media']]
    previews = acquisition._model_images(media)
    assert len(previews) == count
    assert documents == before
    assert [base64.b64decode(image['data']) for image in media] == originals
    assert [image['digest'] for image in media] == [hashlib.sha256(raw).hexdigest() for raw in originals]
    for index, preview in enumerate(previews):
        assert preview['mime'] == 'image/jpeg'
        with Image.open(io.BytesIO(base64.b64decode(preview['data']))) as rendered:
            rendered.load()
            assert rendered.size == (16, 16)
            pixel = rendered.getpixel((8, 8))
            assert all(abs(actual - expected) <= 4
                       for actual, expected in zip(pixel, (index, 30, 220 - index)))


def test_source_limit_stays_64_and_incomplete_source_cannot_be_preflighted():
    originals = [image_bytes(color=(index, 0, 100)) for index in range(65)]
    document = {'text': 'Public source', 'images': []}
    acquisition._prepare_media(document, originals)
    assert document['capture_complete'] is False
    assert len(document['media']) == 64
    assert [base64.b64decode(image['data']) for image in document['media']] == originals[:64]
    assert document['capture_errors'][0]['image_index'] == 64
    assert document['capture_errors'][0]['code'] == 'source_incomplete'
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition.preflight_document_media(document)
    assert caught.value.code == 'source_incomplete'


def test_129_aggregate_images_reject_before_post_without_truncation(tmp_path):
    raw = image_bytes()
    media = [acquisition._image(raw, f'https://example.org/{index}.png') for index in range(129)]
    before = deepcopy(media)
    with patch.object(runtime, 'settings', model_settings(tmp_path)), \
            patch.object(acquisition, '_model_post') as post:
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition.request_model_json('Instructions', {}, media)
        post.assert_not_called()
    assert caught.value.code == 'image_unsupported'
    assert media == before
    assert base64.b64decode(media[-1]['data']) == raw


@pytest.mark.parametrize('attribute', ['href', 'xlink:href'])
def test_embedded_png_placement_alpha_and_immutable_evidence(attribute, no_resource_fetch):
    raw_png = image_bytes(size=(8, 8), color=(255, 0, 0, 128), mode='RGBA')
    raw_svg = svg_bytes('<rect width="80" height="40" fill="blue"/>'
                        f'<image x="20" y="10" width="20" height="20" '
                        f'{attribute}="{png_uri(raw_png)}"/>')
    original = acquisition._image(raw_svg, 'https://example.org/poster.svg')
    before = deepcopy(original)
    with acquisition._svg_preview(raw_svg, 100)[0] as rendered:
        assert rendered.size == (80, 40)
        assert rendered.getpixel((5, 5)) == (0, 0, 255)
        red, green, blue = rendered.getpixel((30, 20))
        assert 125 <= red <= 130 and green == 0 and 125 <= blue <= 130
        assert rendered.getpixel((60, 20)) == (0, 0, 255)
    preview = acquisition._model_images([original])[0]
    with Image.open(io.BytesIO(base64.b64decode(preview['data']))) as rendered:
        red, green, blue = rendered.getpixel((30, 20))
        assert 115 <= red <= 140 and green < 10 and 115 <= blue <= 140
        assert rendered.getpixel((60, 20))[2] > 240
    assert original == before
    assert base64.b64decode(original['data']) == raw_svg
    assert original['digest'] == hashlib.sha256(raw_svg).hexdigest()


def test_referenced_png_pixels_are_counted_each_time_and_reach_document_guard(monkeypatch, no_resource_fetch):
    uri = png_uri(image_bytes(size=(20, 10), color='lime'))
    raw_svg = svg_bytes(f'<image href="{uri}" width="20" height="10"/>'
                        f'<image href="{uri}" x="40" width="20" height="10"/>')
    rendered, pixels = acquisition._svg_preview(raw_svg, 100)
    with rendered:
        assert rendered.getpixel((10, 5)) == (0, 255, 0)
        assert rendered.getpixel((50, 5)) == (0, 255, 0)
    assert pixels == 80 * 40 + 2 * 20 * 10
    original = acquisition._image(raw_svg)
    before = deepcopy(original)
    monkeypatch.setattr(acquisition, 'MAX_DOCUMENT_FRAME_PIXELS', pixels - 1)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition._model_images([original])
    assert caught.value.code == 'image_unsupported'
    assert original == before
    monkeypatch.setattr(acquisition, 'MAX_DOCUMENT_FRAME_PIXELS', pixels)
    assert len(acquisition._model_images([original])) == 1


def test_embedded_png_at_pixel_boundary_renders(no_resource_fetch):
    uri = png_uri(image_bytes(size=(4096, 4096), color=80, mode='L'))
    raw_svg = svg_bytes(f'<image href="{uri}" width="80" height="40" preserveAspectRatio="none"/>')
    rendered, pixels = acquisition._svg_preview(raw_svg, 100)
    with rendered:
        assert all(abs(channel - 80) <= 1 for channel in rendered.getpixel((40, 20)))
    assert pixels == 80 * 40 + 16_777_216


def test_oversized_embedded_png_is_rejected_before_pixel_decode(no_resource_fetch):
    raw = svg_bytes(f'<image href="{png_uri(header_png(4097, 4096))}" width="80" height="40"/>')
    with patch.object(Image.Image, 'load', side_effect=AssertionError('oversized PNG decoded')) as decode:
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition._model_images([acquisition._image(raw)])
        decode.assert_not_called()
    assert caught.value.code == 'image_unsupported'


def test_aggregate_embedded_pixel_boundary_counts_duplicate_render_cost(no_resource_fetch):
    uri = png_uri(image_bytes(size=(4000, 4000), color=70, mode='L'))
    body = (f'<image href="{uri}" width="40" height="40"/>'
            f'<image href="{uri}" x="40" width="40" height="40"/>')
    rendered, pixels = acquisition._svg_preview(svg_bytes(body), 100)
    with rendered:
        assert rendered.getpixel((20, 20)) == (70, 70, 70)
        assert rendered.getpixel((60, 20)) == (70, 70, 70)
    assert pixels == 80 * 40 + 32_000_000
    extra = png_uri(image_bytes(size=(1, 1)))
    over = svg_bytes(body + f'<image href="{extra}" width="1" height="1"/>')
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition._model_images([acquisition._image(over)])
    assert caught.value.code == 'image_unsupported'


def test_raw_svg_boundary_is_not_widened_by_inline_png_support(no_resource_fetch):
    uri = png_uri(image_bytes())
    raw = svg_bytes(f'<image href="{uri}" width="80" height="40"/><!--PAD-->')
    at_limit = raw.replace(b'PAD', b'x' * (2 * 1024 * 1024 - len(raw) + 3))
    assert len(at_limit) == 2 * 1024 * 1024
    with acquisition._svg_preview(at_limit, 100)[0] as rendered:
        assert rendered.getpixel((40, 20)) == (255, 0, 0)
    over_limit = at_limit.replace(b'<!--', b'<!--x', 1)
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition._model_images([acquisition._image(over_limit)])
    assert caught.value.code == 'image_unsupported'


@pytest.mark.parametrize('resource', [
    'https://example.org/image.png', 'http://127.0.0.1/private', 'file:///etc/passwd',
    '//example.org/image.png', '#local-image',
    'data:image/svg+xml;base64,PHN2Zy8+',
    'data:image/png,%89PNG%0D%0A%1A%0A',
    'data:image/png;base64,%69VBORw0KGgo=',
    'data:image/png;charset=utf-8;base64,aGVsbG8=',
    'data:image/png;base64,not base64',
    png_uri(b'not a PNG'), png_uri(b'\x89PNG\r\n\x1a\nfake'),
])
def test_unapproved_image_resources_remain_blocked_without_resource_io(resource, no_resource_fetch):
    raw = svg_bytes(f'<image href="{resource}" width="80" height="40"/>')
    original = acquisition._image(raw)
    before = deepcopy(original)
    with pytest.raises(acquisition.AcquisitionError):
        acquisition._model_images([original])
    assert original == before


def test_png_with_wrong_mime_or_percent_encoded_base64_is_rejected(no_resource_fetch):
    uri = png_uri(image_bytes())
    for bad in (uri.replace('image/png', 'image/jpeg'),
                uri.replace(';base64,', ';base64,%'), uri + '='):
        with pytest.raises(acquisition.AcquisitionError):
            acquisition._model_images([acquisition._image(svg_bytes(f'<image href="{bad}"/>'))])


def test_apng_even_with_a_static_default_frame_is_rejected(no_resource_fetch):
    frames = [Image.new('RGBA', (8, 8), color) for color in ('red', 'blue', 'lime')]
    try:
        output = io.BytesIO()
        frames[0].save(output, format='PNG', save_all=True, append_images=frames[1:],
                       duration=100, loop=0, default_image=True)
        raw = output.getvalue()
        with Image.open(io.BytesIO(raw)) as image:
            assert image.n_frames == 3
        svg = svg_bytes(f'<image href="{png_uri(raw)}" width="80" height="40"/>')
        with pytest.raises(acquisition.AcquisitionError) as caught:
            acquisition._model_images([acquisition._image(svg)])
        assert caught.value.code == 'image_unsupported'
    finally:
        for frame in frames:
            frame.close()


def test_truncated_corrupt_and_trailing_png_streams_are_rejected(no_resource_fetch):
    raw = image_bytes()
    corrupt = bytearray(raw)
    idat = corrupt.index(b'IDAT')
    corrupt[idat + 4] ^= 1
    bad_streams = [raw[:-1], raw[:-12], raw + b'extra', bytes(corrupt),
                   header_png(8, 8, zlib.compress(b'not enough pixels'))]
    for broken in bad_streams:
        svg = svg_bytes(f'<image href="{png_uri(broken)}" width="80" height="40"/>')
        with pytest.raises(acquisition.AcquisitionError):
            acquisition._model_images([acquisition._image(svg)])


@pytest.mark.parametrize('body', [
    '<image/>', '<image href="#x"/>',
    '<style>image {fill:url(data:image/png;base64,aGVsbG8=)}</style>',
    '<rect style="fill:url(data:image/png;base64,aGVsbG8=)"/>',
    '<script/>', '<foreignObject/>', '<animate/>', '<pattern/>', '<mask/>',
    '<svg width="1000000000" height="1000000000"/>',
    '<rect onload="alert(1)"/>', '<?resource file:///etc/passwd?>',
])
def test_existing_active_and_resource_restrictions_survive_inline_png_support(body, no_resource_fetch):
    with pytest.raises(acquisition.AcquisitionError) as caught:
        acquisition._model_images([acquisition._image(svg_bytes(body))])
    assert caught.value.code == 'image_unsupported'


def test_aggregate_above_source_limit_keeps_every_frame_of_last_image():
    frames = [Image.new('RGB', (60, 60), color) for color in ('red', 'lime', 'blue')]
    try:
        output = io.BytesIO()
        frames[0].save(output, format='PNG', save_all=True, append_images=frames[1:],
                       duration=100, loop=0)
        media = [acquisition._image(image_bytes()) for _ in range(64)]
        media.append(acquisition._image(output.getvalue()))
        before = deepcopy(media)
        previews = acquisition._model_images(media)
        assert len(previews) == 65
        assert media == before
        with Image.open(io.BytesIO(base64.b64decode(previews[-1]['data']))) as rendered:
            rendered.load()
            cell = rendered.width // 2
            samples = [rendered.getpixel((cell // 2, 50)),
                       rendered.getpixel((cell + cell // 2, 50)),
                       rendered.getpixel((cell // 2, cell + 50))]
            assert samples[0][0] > 200 and samples[0][1] < 30
            assert samples[1][1] > 200 and samples[1][0] < 30
            assert samples[2][2] > 200 and samples[2][0] < 30
        assert base64.b64decode(media[-1]['data']) == output.getvalue()
    finally:
        for frame in frames:
            frame.close()


@pytest.mark.parametrize('reference', ['url(#)', "url('#')"])
def test_empty_local_clip_fragment_preserves_pixels_without_resource_access(reference, no_resource_fetch):
    raw = svg_bytes(f'<g style="clip-path: {reference};"><rect width="80" height="40" fill="red"/></g>')
    with acquisition._svg_preview(raw, 100)[0] as rendered:
        assert rendered.getpixel((40, 20)) == (255, 0, 0)
        assert rendered.getpixel((1, 1)) == (255, 0, 0)
