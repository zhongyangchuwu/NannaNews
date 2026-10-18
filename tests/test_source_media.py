"""Remote exclusions are explicit; remaining static/PDF evidence stays complete."""
import base64
from copy import deepcopy
import hashlib
import io
from types import SimpleNamespace

from PIL import Image
import pytest

from nannanews import acquisition
from nannanews import bulletin_date_repair as repair
from nannanews import source_media

PARENT = 'https://mp.weixin.qq.com/s/public-source?x=1&x=2'
STATIC = 'https://mmbiz.qpic.cn/static.png'
GIF = 'https://mmbiz.qpic.cn/animated/640?wx_fmt=gif'
VIDEO = 'https://video.example.org/lecture.mp4'


def png_bytes():
    with Image.new('RGB', (32, 32), 'white') as image:
        output = io.BytesIO()
        image.save(output, format='PNG')
        return output.getvalue()


def gif_bytes(frames=65):
    images = [Image.new('RGB', (2, 2), (index % 256, 0, 0)) for index in range(frames)]
    try:
        output = io.BytesIO()
        images[0].save(output, format='GIF', save_all=True, append_images=images[1:],
                       duration=10, loop=0, optimize=False)
        return output.getvalue()
    finally:
        for image in images:
            image.close()


def document(images):
    return {'canonical_url': PARENT, 'images': list(images), 'text': '可复核的公开正文，活动事实仅来自这些文字和实际取得的静态图像。',
            'capture_complete': True, 'capture_errors': []}


def audio_only_mp4_bytes():
    def box(kind, body):
        return (8 + len(body)).to_bytes(4, 'big') + kind + body

    handler = box(b'hdlr', b'\x00' * 8 + b'soun' + b'\x00' * 12 + b'AudioHandler\x00')
    return (box(b'ftyp', b'isom\x00\x00\x00\x00isommp42')
            + box(b'moov', box(b'trak', box(b'mdia', handler))))


@pytest.mark.parametrize('raw', [
    audio_only_mp4_bytes(),
    b'RIFF\x18\x00\x00\x00AVI LIST\x0c\x00\x00\x00strlstrhauds',
    b'FLV\x01\x04\x00\x00\x00\x09',
    b'\x1aE\xdf\xa3\x87\x42\x82\x84webm',
    b'\x00\x00\x01\xba',
])
def test_ambiguous_or_audio_only_containers_are_not_waived_as_video(monkeypatch, raw):
    unknown = 'https://media.example.org/opaque-container'
    monkeypatch.setattr(acquisition, 'fetch_public_resource',
        lambda *args: (raw, 'application/octet-stream', unknown))
    captured = document([unknown])
    media = acquisition._prepare_media(captured)
    assert captured['skipped_media'] == []
    assert captured['images'] == [unknown] and media[0]['url'] == unknown
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.preflight_document_media(captured)


def test_declared_remote_gif_and_video_are_not_downloaded_and_static_poster_is_kept(monkeypatch):
    raw = ('<h1 id="activity-name">公开活动</h1><div id="js_content"><p>活动公开正文。</p>'
           '<img data-type="gif" data-src="' + GIF + '">'
           '<video src="' + VIDEO + '" poster="' + STATIC + '"></video>'
           '<iframe class="video_iframe" data-src="https://video.example.org/embed/123"></iframe>'
           '</div>').encode()
    requested = []

    def fetch(url, *args):
        requested.append(url)
        if url == PARENT:
            return raw, 'text/html; charset=utf-8', PARENT
        assert url == STATIC
        return png_bytes(), 'image/png', STATIC

    monkeypatch.setattr(acquisition, 'fetch_public_resource', fetch)
    captured = acquisition.fetch_document(PARENT)
    media = acquisition._prepare_media(captured)
    assert requested == [PARENT, STATIC]
    assert captured['images'] == [STATIC] and len(media) == 1
    assert captured['capture_complete'] is True and captured['capture_errors'] == []
    assert {item['type'] for item in captured['skipped_media']} == {'gif', 'video'}
    assert all(item['downloaded'] is False and item['digest'] is None and item['bytes'] == 0
               and item['complete'] is False for item in captured['skipped_media'])
    assert captured['media_policy'] == source_media.media_policy()
    source_media.validate_media_policy(captured, media)


@pytest.mark.parametrize('raw,mime,kind', [
    (gif_bytes(), 'application/octet-stream', 'gif'),
    (b'GIF89a' + b'original unexpected GIF', 'image/gif', 'gif'),
    (b'\x00\x00\x01\xb3\x14\x00\xf0\x13', 'application/octet-stream', 'video'),
    (b'FLV\x01\x01\x00\x00\x00\x09', 'application/octet-stream', 'video'),
    (b'original source video response', 'video/mp4', 'video'),
])
def test_unexpected_received_gif_or_video_is_excluded_with_actual_digest_without_blocking_static_media(monkeypatch, raw, mime, kind):
    unknown = 'https://media.example.org/opaque-response'
    calls = []

    def fetch(url, *args):
        calls.append(url)
        return (raw, mime, url) if url == unknown else (png_bytes(), 'image/png', url)

    monkeypatch.setattr(acquisition, 'fetch_public_resource', fetch)
    captured = document([unknown, STATIC])
    media = acquisition._prepare_media(captured)
    assert calls == [unknown, STATIC]
    assert captured['images'] == [STATIC] and [image['url'] for image in media] == [STATIC]
    assert captured['capture_complete'] is True
    item = captured['skipped_media'][0]
    assert item['type'] == kind and item['url'] == unknown
    assert item['downloaded'] is True and item['complete'] is True
    assert item['bytes'] == len(raw) and item['digest'] == hashlib.sha256(raw).hexdigest()
    # The old frame limit is not raised or bypassed for reviewed inputs: only a
    # static PNG reaches normal preflight after the whole GIF was excluded.
    assert len(acquisition.preflight_document_media(captured)) == 1
    source_media.validate_media_policy(captured, media)


def test_bounded_partial_video_response_is_excluded_without_claiming_a_full_file_digest(monkeypatch):
    unknown = 'https://media.example.org/opaque-video'
    prefix = b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42'

    def fetch(url, *args):
        if url == STATIC:
            return png_bytes(), 'image/png', STATIC
        error = acquisition.AcquisitionError('Video response exceeded the source limit.', 'source_too_large')
        error.source_response = {'canonical_url': unknown, 'resolved_url': unknown, 'http_status': 200,
            'content_type': 'video/mp4', 'raw_response_base64': base64.b64encode(prefix).decode(),
            'raw_response_digest': hashlib.sha256(prefix).hexdigest(), 'raw_response_complete': False,
            'fetched_at': '2026-10-17T12:00:00+00:00'}
        raise error

    monkeypatch.setattr(acquisition, 'fetch_public_resource', fetch)
    captured = document([unknown, STATIC])
    media = acquisition._prepare_media(captured)
    assert captured['capture_complete'] is True and captured['capture_errors'] == []
    assert [image['url'] for image in media] == [STATIC]
    skipped = captured['skipped_media'][0]
    assert skipped['complete'] is False and skipped['bytes'] == len(prefix)
    assert skipped['digest'] == hashlib.sha256(prefix).hexdigest()


@pytest.mark.parametrize('fmt', ['PNG', 'WEBP'])
def test_other_image_formats_and_animation_are_not_blanket_excluded(monkeypatch, fmt):
    images = [Image.new('RGB', (8, 8), color) for color in ('red', 'blue')]
    try:
        output = io.BytesIO()
        images[0].save(output, format=fmt, save_all=True, append_images=images[1:], duration=20, loop=0)
        raw = output.getvalue()
    finally:
        for image in images:
            image.close()
    url = 'https://media.example.org/animated-but-not-gif'
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda *args: (raw, 'image/' + fmt.lower(), url))
    captured = document([url])
    media = acquisition._prepare_media(captured)
    assert len(media) == 1 and captured['images'] == [url]
    assert captured['skipped_media'] == [] and captured['capture_complete'] is True
    assert len(acquisition.preflight_document_media(captured)) == 1


def test_actual_static_image_magic_is_not_excluded_by_a_mislabelled_video_mime(monkeypatch):
    raw = png_bytes()
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda *args: (raw, 'video/mp4', STATIC))
    captured = document([STATIC])
    media = acquisition._prepare_media(captured)
    assert media[0]['mime'] == 'image/png'
    assert captured['skipped_media'] == []


def test_explicit_upload_gif_behavior_and_frame_cap_remain_unchanged():
    captured = document([])
    uploaded = gif_bytes()
    media = acquisition._prepare_media(captured, [uploaded])
    assert len(media) == 1 and media[0]['mime'] == 'image/gif'
    assert captured['skipped_media'] == []
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.preflight_document_media(captured)


def test_cached_policy_continuation_is_pure_and_verifies_actual_bytes_before_excluding():
    static = acquisition._image(png_bytes(), STATIC)
    gif = acquisition._image(gif_bytes(), GIF)
    old = document([STATIC, GIF])
    old['media'] = [static, gif]
    old['digest'] = 'old-scope-digest'
    before = deepcopy(old)
    filtered, media = source_media.apply_remote_media_policy(old, old['media'])
    assert old == before
    assert filtered is not old and media == [static]
    assert filtered['images'] == [STATIC]
    assert filtered['digest'] != 'old-scope-digest'
    skipped = filtered['skipped_media'][0]
    assert skipped['downloaded'] is True and skipped['digest'] == gif['digest']
    source_media.validate_media_policy(filtered, media)
    broken = deepcopy(gif)
    broken['digest'] = '0' * 64
    with pytest.raises(ValueError):
        source_media.apply_remote_media_policy(old, [static, broken])
    assert old == before


def test_static_failure_is_not_suppressed_by_an_authorized_gif_exclusion(monkeypatch):
    broken = 'https://media.example.org/unavailable-static.png'

    def fetch(url, *args):
        if url == broken:
            raise acquisition.AcquisitionError('Actual static-image failure.', 'source_unavailable')
        assert url == STATIC
        return png_bytes(), 'image/png', STATIC

    monkeypatch.setattr(acquisition, 'fetch_public_resource', fetch)
    captured = document([GIF, broken, STATIC])
    media = acquisition._prepare_media(captured)
    assert [image['url'] for image in media] == [STATIC]
    assert captured['capture_complete'] is False
    assert captured['capture_errors'][0]['url'] == broken
    assert captured['skipped_media'][0]['url'] == GIF
    with pytest.raises(acquisition.AcquisitionError):
        acquisition.preflight_document_media(captured)


def test_only_excluded_media_has_no_reviewable_primary_evidence_or_model_call(monkeypatch):
    raw = ('<div id="js_content"><img data-src="' + GIF + '"></div>').encode()
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda *args: (raw, 'text/html', PARENT))
    monkeypatch.setattr(acquisition, 'request_model_json', lambda *args, **kwargs: pytest.fail('Excluded-only media must not trigger a model call.'))
    cache = {}
    decision = repair.model_decision({'id': 1, 'source_urls': [PARENT]}, [], source_cache=cache)
    assert decision['_preparation_failed'] is True
    assert decision['source_review']['activity'] is None
    assert cache[PARENT]['document']['capture_complete'] is True
    assert cache[PARENT]['document']['skipped_media'][0]['type'] == 'gif'


@pytest.mark.parametrize('mutation', ['missing_digest', 'pretend_download', 'kept_url', 'kept_gif'])
def test_policy_validator_rejects_false_received_or_reviewed_claims(mutation):
    gif = acquisition._image(gif_bytes(), GIF)
    captured, media = source_media.apply_remote_media_policy(document([GIF]), [gif])
    if mutation == 'missing_digest':
        captured['skipped_media'][0]['digest'] = None
    elif mutation == 'pretend_download':
        captured['skipped_media'][0]['downloaded'] = False
    elif mutation == 'kept_url':
        captured['images'] = [GIF]
    elif mutation == 'kept_gif':
        media = [dict(gif, mime='image/png')]
    with pytest.raises(ValueError):
        source_media.validate_media_policy(captured, media)


@pytest.mark.parametrize('prepared', [False, True])
def test_extraction_model_boundary_receives_policy_and_exclusions_as_unreviewed_input(monkeypatch, prepared):
    old = document([STATIC, GIF])
    captured, media = source_media.apply_remote_media_policy(old,
        [acquisition._image(png_bytes(), STATIC), acquisition._image(gif_bytes(), GIF)])
    seen = {}

    def stop_at_model(instructions, source_data, *args, **kwargs):
        seen.update(instructions=instructions, data=source_data)
        raise RuntimeError('Stopped at the substituted model boundary.')

    monkeypatch.setattr(acquisition, '_model_config', lambda: SimpleNamespace(model_name='test-model'))
    monkeypatch.setattr(acquisition, 'request_model_json', stop_at_model)
    monkeypatch.setattr(acquisition, '_request_prepared_model_json', stop_at_model)
    with pytest.raises(RuntimeError, match='substituted model boundary'):
        acquisition.extract_document(captured, prepared_media=[] if prepared else None)
    metadata = seen['data']['source_media'][0]
    assert metadata['url'] == PARENT
    assert metadata['media_policy']['version'] == source_media.REMOTE_MEDIA_POLICY_VERSION
    assert metadata['skipped_media'][0]['url'] == GIF
    assert metadata['skipped_media'][0]['downloaded'] is True
    assert source_media.MEDIA_POLICY_INSTRUCTIONS in seen['instructions']
    assert len(media) == 1 and media[0]['url'] == STATIC


def test_unexpected_gif_access_refusal_is_not_suppressed_as_an_authorized_exclusion(monkeypatch):
    unknown = 'https://media.example.org/protected-static'
    raw = b'GIF89a' + b'actual HTTP refusal payload'

    def fetch(url, *args):
        if url == STATIC:
            return png_bytes(), 'image/png', STATIC
        error = acquisition.AcquisitionError('Actual HTTP403 source refusal.', 'source_blocked')
        error.source_response = {'canonical_url': unknown, 'resolved_url': unknown, 'http_status': 403,
            'content_type': 'image/gif', 'raw_response_base64': base64.b64encode(raw).decode(),
            'raw_response_digest': hashlib.sha256(raw).hexdigest(), 'raw_response_complete': True,
            'fetched_at': '2026-10-17T12:00:00+00:00'}
        raise error

    monkeypatch.setattr(acquisition, 'fetch_public_resource', fetch)
    captured = document([unknown, STATIC])
    acquisition._prepare_media(captured)
    assert captured['capture_complete'] is False
    assert captured['capture_errors'][0]['code'] == 'source_blocked'
    assert captured['skipped_media'] == []


def test_declared_non_http_video_is_recorded_without_inventing_a_fetchable_url(monkeypatch):
    raw = ('<div id="js_content"><p>保留的公开正文。</p>'
           '<video src="blob:publisher-local-video"></video>'
           '<video src="http://["></video></div>').encode()
    monkeypatch.setattr(acquisition, 'fetch_public_resource', lambda *args: (raw, 'text/html', PARENT))
    captured = acquisition.fetch_document(PARENT)
    assert captured['capture_complete'] is True
    assert captured['skipped_media'][0]['type'] == 'video'
    assert captured['skipped_media'][0]['url'] is None
    assert captured['skipped_media'][0]['downloaded'] is False
    assert '保留的公开正文' in captured['text']


def test_audio_mime_does_not_become_an_unauthorized_video_exclusion():
    mp4_container = b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42'
    assert source_media.excluded_media_type(content_type='audio/mp4', data=mp4_container) is None


@pytest.mark.parametrize('prefix,mime', [
    (b'\x00\x00\x01\xb3\x14\x00\xf0\x13', 'application/octet-stream'),
    (b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42', 'video/mp4'),
])
def test_video_identification_is_not_hidden_by_a_textual_svg_marker_in_container_metadata(prefix, mime):
    assert source_media.excluded_media_type(content_type=mime, data=prefix + b'<svg>metadata</svg>') == 'video'
