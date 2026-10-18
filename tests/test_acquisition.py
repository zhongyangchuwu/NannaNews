"""Behavior tests; external DNS/HTTP are the only faked execution boundaries."""
import base64
from dataclasses import replace
import io
import json
import math
from pathlib import Path
import socket
import sqlite3

import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
from PIL import Image


from nannanews import acquisition, runtime, schema


TEXT = '公开活动甲将于2026年10月20日在仙林举办。\n公开活动乙将于2026年10月21日在鼓楼举办。'


def document(text=TEXT, source='https://example.org/article?a=1'):
    return {'canonical_url': source, 'title': '公开活动通知', 'text': text, 'images': [], 'raw_content': text, 'fetched_at': '2026-10-13T00:00:00+00:00', 'source_type': 'public_html', 'raw_date_text': None}


def item(title='公开活动甲', due='2026-10-20', quote='公开活动甲将于2026年10月20日在仙林举办。'):
    return {'title': title, 'description': quote, 'tag': '校级活动',
            'event_date': due}


def model_body(items):
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'items': items}, ensure_ascii=False)}}]}


def responses_body(items):
    return {'status': 'completed', 'error': None, 'incomplete_details': None, 'output': [{'type': 'message', 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': json.dumps({'items': items}, ensure_ascii=False), 'annotations': []}]}]}


class ModelResponse:
    def __init__(self, body, status=200):
        self.status = status
        self.headers = {}
        self.body = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.offset = 0
        self.closed = False

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read1(self, size):
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class FakeModelConnection:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.sock = None
        self.closed = False
        self.host = 'model.example'

    def bind(self, host, port):
        self.host = host
        return self

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        body = body.read() if hasattr(body, 'read') else body
        self.calls.append(('https://' + self.host + path, {'method': method, 'json': json.loads(body), 'headers': headers}))

    def getresponse(self):
        self.response.offset = 0
        return self.response

    def close(self):
        self.closed = True


class SourceResponse:
    def __init__(self, status=200, headers=None, body=b''):
        self.status = status
        self.headers = headers or {}
        self.body = body
        self.offset = 0

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read1(self, size):
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk

    def close(self):
        pass


class AcquisitionFixture:
    def setUp(self):
        audit_root = tempfile.TemporaryDirectory()
        self.addCleanup(audit_root.cleanup)
        root = Path(audit_root.name)
        settings = runtime.Settings(
            data_dir=root, database_path=root / 'database.db', upload_dir=root / 'uploads',
            publication_dir=root / 'publications', archive_dir=root / 'archives',
            lecture_archive_dir=root / 'lecture_archives', session_secret='fixture-only-secret',
            model_base_url='https://model.example/v1', model_api_key='private-test-key',
            model_name='gpt-6-luna', model_timeout=20, model_api_style='chat_completions',
        )
        self.settings_patch = patch.object(runtime, 'settings', settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        runtime.prepare_runtime()
        self.transport = FakeModelConnection(ModelResponse(model_body([item()])))
        self.transport_patch = patch.object(acquisition, '_PinnedHTTPS', side_effect=lambda host, port, address, timeout: self.transport.bind(host, port))
        self.transport_patch.start()
        self.addCleanup(self.transport_patch.stop)
        self.dns_patch = patch.object(socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))])
        self.dns_patch.start()
        self.addCleanup(self.dns_patch.stop)
        self.proxy_patch = patch.object(acquisition, 'getproxies_environment', return_value={})
        self.proxy_patch.start()
        self.addCleanup(self.proxy_patch.stop)

class AcquisitionTests(AcquisitionFixture, unittest.TestCase):
    def test_wechat_query_identity_preserves_repeated_parameters(self):
        first = acquisition.canonical_url('https://mp.weixin.qq.com/s?__biz=ABC%3D%3D&mid=1&idx=1&sn=a&x=1&x=2#rd')
        second = acquisition.canonical_url('https://mp.weixin.qq.com/s?__biz=ABC%3D%3D&mid=2&idx=1&sn=b')
        self.assertEqual(first, 'https://mp.weixin.qq.com/s?__biz=ABC%3D%3D&mid=1&idx=1&sn=a&x=1&x=2')
        self.assertNotEqual(first, second)

    def test_non_http_credentials_controls_and_ambiguous_urls_rejected(self):
        for url in ('file:///etc/passwd', 'https://user:password@example.org/', 'https://example.org\\@127.0.0.1/', 'http://example.org/\r\nInjected: yes', 'http://[bad-address]/'):
            with self.subTest(url=url), self.assertRaises(acquisition.AcquisitionError):
                acquisition.canonical_url(url)

    def test_dns_rejects_private_and_mixed_answers(self):
        for addresses in (['127.0.0.1'], ['169.254.169.254'], ['::1'], ['::ffff:127.0.0.1'], ['93.184.216.34', '10.1.2.3']):
            answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 80)) for address in addresses]
            with self.subTest(addresses=addresses), patch.object(socket, 'getaddrinfo', return_value=answers):
                with self.assertRaises(acquisition.AcquisitionError) as caught:
                    acquisition.fetch_public_resource('http://example.org/')
                self.assertEqual(caught.exception.code, 'unsafe_source')

    def test_expired_dns_budget_does_not_issue_an_http_request(self):
        clock = [100.0]
        requests = []

        def dns(*args):
            clock[0] = 105.0
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 80))]

        class Connection:
            def __init__(self, *args):
                self.sock = None

            def request(self, method, path, **kwargs):
                requests.append((method, path))

            def getresponse(self):
                return SourceResponse(body=b'public response')

            def close(self):
                pass

        with patch.object(acquisition.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(socket, 'getaddrinfo', side_effect=dns), \
                patch.object(acquisition, '_PinnedHTTP', Connection):
            with self.assertRaises(acquisition.AcquisitionError) as caught:
                acquisition.fetch_public_resource('http://example.org/', deadline=104.0)
        self.assertEqual(caught.exception.code, 'source_unavailable')
        self.assertEqual(requests, [])

    def test_public_redirect_to_private_is_rejected_before_connect(self):
        connections = []
        responses = [SourceResponse(302, {'Location': 'http://internal.example/secret'})]

        class Connection:
            def __init__(self, host, port, address, timeout):
                connections.append((host, address))
                self.sock = None

            def request(self, *args, **kwargs):
                self.headers = kwargs['headers']
                if 'Cookie' in self.headers or 'Authorization' in self.headers:
                    raise AssertionError('source credentials must not be sent')

            def getresponse(self):
                return responses.pop(0)

            def close(self):
                pass

        def dns(host, *args):
            address = '10.0.0.1' if host == 'internal.example' else '93.184.216.34'
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 80))]

        with patch.object(socket, 'getaddrinfo', side_effect=dns), patch.object(acquisition, '_PinnedHTTP', Connection):
            with self.assertRaises(acquisition.AcquisitionError) as caught:
                acquisition.fetch_public_resource('http://example.org/')
        self.assertEqual(caught.exception.code, 'unsafe_source')
        self.assertEqual(connections, [('example.org', '93.184.216.34')])

    def test_source_body_and_redirect_bounds(self):
        class Connection:
            def __init__(self, *args):
                self.sock = None

            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                return SourceResponse(body=b'x' * 101)

            def close(self):
                pass

        dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 80))]
        with patch.object(socket, 'getaddrinfo', return_value=dns), patch.object(acquisition, '_PinnedHTTP', Connection):
            with self.assertRaises(acquisition.AcquisitionError) as caught:
                acquisition.fetch_public_resource('http://example.org/', max_bytes=100)
        self.assertEqual(caught.exception.code, 'source_too_large')

    def test_wechat_readable_body_data_src_and_empty_blocked_states(self):
        html = '<h1 id="activity-name">公开文章</h1><div id="js_content">正文信息<img data-src="https://mmbiz.qpic.cn/image?wx_fmt=png"></div><em id="publish_time">2026-10-12</em>'
        with patch.object(acquisition, 'fetch_public_resource', return_value=(html.encode(), 'text/html', 'https://mp.weixin.qq.com/s?a=1')):
            evidence = acquisition.fetch_document('https://mp.weixin.qq.com/s?a=1')
        self.assertEqual(evidence['text'], '正文信息')
        self.assertEqual(evidence['images'], ['https://mmbiz.qpic.cn/image?wx_fmt=png'])
        self.assertEqual(evidence['raw_date_text'], '2026-10-12')
        self.assertEqual(evidence['raw_content'], html)
        for blocked, code in [('<h1>环境异常，请验证</h1>', 'source_blocked'), ('<div id="js_content"></div>', 'source_empty')]:
            with patch.object(acquisition, 'fetch_public_resource', return_value=(blocked.encode(), 'text/html', 'https://mp.weixin.qq.com/s?a=1')):
                with self.assertRaises(acquisition.AcquisitionError) as caught:
                    acquisition.fetch_document('https://mp.weixin.qq.com/s?a=1')
            self.assertEqual(caught.exception.code, code)

    def test_nju_spa_uses_real_documented_detail_api_without_changing_identity(self):
        cases = [('https://job.nju.edu.cn/career/specifics/123', '/api/career/job/fair/specifics/123'), ('https://job.nju.edu.cn/career/mutual-selections/234', '/api/career/job/fair/mutual-selections/234'), ('https://job.nju.edu.cn/career/info/345?type=NEWS', '/api/career/content/informations/345'), ('https://job.nju.edu.cn/career/jobs-v2?recruitmentId=456', '/api/career/job/recruitments/456')]
        record = json.dumps({'theme': '公开招聘', 'intro': '<p>企业将在2026年10月20日举办招聘会。</p><img src="/images/fair.png">', 'startAt': '2026-10-20 14:00:00', 'venue': '仙林', 'status': 'PUBLISHED'}).encode()
        for original, api_path in cases:
            with self.subTest(original=original), patch.object(acquisition, 'fetch_public_resource', return_value=(record, 'application/json', 'https://job.nju.edu.cn' + api_path)) as transport:
                evidence = acquisition.fetch_document(original)
                transport.assert_called_once_with('https://job.nju.edu.cn' + api_path)
                self.assertEqual(evidence['canonical_url'], original)
                self.assertEqual(evidence['source_type'], 'nju_job_api')
                self.assertIn('2026-10-20', evidence['text'])
                self.assertEqual(evidence['images'], ['https://job.nju.edu.cn/images/fair.png'])
        with self.assertRaises(acquisition.AcquisitionError):
            acquisition.fetch_document('https://job.nju.edu.cn/career/specifics')
        with patch.object(acquisition, 'fetch_public_resource', return_value=(b'{}', 'application/json', 'https://job.nju.edu.cn/api')):
            with self.assertRaises(acquisition.AcquisitionError):
                acquisition.fetch_document(cases[0][0])

    def test_missing_configuration_is_actionable_and_does_not_call_model(self):
        runtime.settings = replace(runtime.settings, model_base_url='')
        with self.assertRaises(acquisition.AcquisitionError) as caught:
            acquisition.extract_document(document())
        self.assertEqual(caught.exception.code, 'model_config_missing')
        self.assertIn('NANNA_MODEL_BASE_URL', str(caught.exception))
        self.assertEqual(self.transport.calls, [])

    def test_real_compatible_request_uses_model_and_isolates_source_data(self):
        drafts = acquisition.extract_document(document())
        self.assertEqual(len(drafts), 1)
        endpoint, options = self.transport.calls[0]
        self.assertEqual(endpoint, 'https://model.example/v1/chat/completions')
        self.assertEqual(options['json']['model'], 'gpt-6-luna')
        self.assertEqual(options['method'], 'POST')
        self.assertEqual(options['headers']['Authorization'], 'Bearer private-test-key')
        self.assertTrue(self.transport.closed)
        self.assertEqual(options['json']['messages'][0]['role'], 'system')
        self.assertEqual(options['json']['messages'][1]['role'], 'user')
        self.assertEqual(drafts[0]['source_url'], document()['canonical_url'])

    def test_shared_model_json_api_returns_configured_model_and_object(self):
        output, model_name = acquisition.request_model_json('Return a JSON object.', {'text': '公开原文'})
        self.assertEqual(output, {'items': [item()]})
        self.assertEqual(model_name, 'gpt-6-luna')
        payload = self.transport.calls[0][1]['json']
        self.assertEqual(payload['messages'][0]['content'], 'Return a JSON object.')
        self.assertIn('公开原文', payload['messages'][1]['content'][0]['text'])

    def test_admin_model_endpoint_may_be_local_but_redirects_are_never_followed(self):
        runtime.settings = replace(runtime.settings, model_base_url='http://127.0.0.1:9999/v1')
        dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 9999))]
        with patch.object(socket, 'getaddrinfo', return_value=dns), patch.object(acquisition, '_PinnedHTTP', return_value=self.transport):
            drafts = acquisition.extract_document(document())
        self.assertEqual(len(drafts), 1)
        self.transport.response = ModelResponse({'error': 'secret-provider-data'}, status=302)
        self.transport.response.headers['Location'] = 'https://other-provider.example/steal-key'
        with patch.object(socket, 'getaddrinfo', return_value=dns), patch.object(acquisition, '_PinnedHTTP', return_value=self.transport):
            with self.assertRaises(acquisition.AcquisitionError) as caught:
                acquisition.extract_document(document())
        self.assertEqual(caught.exception.code, 'model_http_error')
        self.assertEqual(len(self.transport.calls), 2)

    def test_provider_declared_size_and_truncated_body_are_errors(self):
        for length in (str(acquisition.MAX_MODEL_BODY + 1), str(len(self.transport.response.body) + 10)):
            with self.subTest(length=length):
                self.transport.response.headers['Content-Length'] = length
                with self.assertRaises(acquisition.AcquisitionError) as caught:
                    acquisition.extract_document(document())
                self.assertEqual(caught.exception.code, 'model_response_invalid')

    def test_provider_error_dump_and_key_are_not_exposed(self):
        self.transport.response = ModelResponse({'error': 'private-test-key provider debug data'}, status=401)
        with self.assertRaises(acquisition.AcquisitionError) as caught:
            acquisition.extract_document(document())
        self.assertIn('HTTP 401', str(caught.exception))
        self.assertNotIn('private-test-key', str(caught.exception))
        self.assertNotIn('provider debug', str(caught.exception))

    def test_non_json_provider_body_is_retained_privately_without_retry(self):
        raw = b'private-model-body\xff\n'
        self.transport.response = ModelResponse(raw)
        evidence = document()
        with self.assertRaises(acquisition.AcquisitionError) as caught:
            acquisition.extract_document(evidence)
        self.assertEqual(caught.exception.code,'model_response_invalid')
        self.assertNotIn('private-model-body',str(caught.exception))
        self.assertEqual(base64.b64decode(evidence['model_response']['body_base64']),raw)
        self.assertEqual(evidence['model_name'],'gpt-6-luna')
        self.assertNotIn('model_output',evidence)
        self.assertEqual(evidence['text'],TEXT)
        self.assertEqual(evidence['raw_content'],TEXT)
        self.assertEqual(len(self.transport.calls),1)

    def test_malformed_refused_truncated_and_structurally_invalid_models_fail(self):
        invalid = [b'not-json', {'choices': []}, {'choices': ['not-an-object']}, {'choices': [{'finish_reason': 'stop', 'message': []}]}, {'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]}, model_body([]), model_body([dict(item(), title='')]), model_body([dict(item(), tag=['校级活动'])])]
        for output in invalid:
            with self.subTest(output=output):
                self.transport.response = ModelResponse(output)
                with self.assertRaises(acquisition.AcquisitionError):
                    acquisition.extract_document(document())

    def test_optional_date_metadata_preserves_model_values_without_inference(self):
        original = '2025年活动季安排。活动时间：11月27日16:00，地点南雍楼西353。'
        draft = item(due='2025-11-27', quote=original)
        draft.update(event_date_resolution=None, event_date_basis=None, date_uncertainty=None,
                     colleges=None, evidence_quotes=None, lecture_data=None)
        self.transport.response = ModelResponse(model_body([draft]))
        dated = acquisition.extract_document(document(original))[0]
        self.assertEqual(dated['event_date'], '2025-11-27')
        self.assertEqual(dated['event_date_resolution'], 'unknown')
        self.assertEqual(dated['event_date_basis'], '')
        self.assertEqual(dated['date_uncertainty'], '')
        self.assertIsNone(dated['deadline_date'])
        self.assertEqual(dated['colleges'], [])
        self.assertEqual(dated['evidence_quotes'], [])
        self.assertIsNone(dated['lecture_data'])
        self.assertEqual(dated['deadline_date_resolution'], 'unknown')

        draft['event_date_resolution'] = 'contextual'
        self.transport.response = ModelResponse(model_body([draft]))
        contextual = acquisition.extract_document(document(original))[0]
        self.assertEqual(contextual['event_date_resolution'], 'contextual')

    def test_model_title_date_and_quote_are_preserved_despite_source_mismatch(self):
        draft = item(title='模型采用的活动标题', due='2026-12-31', quote='模型撰写的活动说明。')
        draft['evidence_quotes'] = [{'text': '引用的文字并未出现在来源中。', 'image_index': None}]
        output = {'items': [dict(draft, model_note='ignored unknown metadata')]}
        self.transport.response = ModelResponse(model_body(output['items']))
        source = document('来源正文只说了另一件事。')
        result = acquisition.extract_document(source)[0]
        self.assertEqual((result['title'], result['description'], result['event_date']),
                         ('模型采用的活动标题', '模型撰写的活动说明。', '2026-12-31'))
        self.assertEqual(result['evidence_quotes'], draft['evidence_quotes'])
        self.assertNotIn('model_note', result)
        self.assertEqual(source['model_output'], output)

    def test_invalid_dates_and_lecture_schedule_format_are_rejected(self):
        for invalid_date in ('2026-02-30', '2026-2-03'):
            self.transport.response = ModelResponse(model_body([dict(item(), event_date=invalid_date)]))
            with self.subTest(event_date=invalid_date), self.assertRaises(acquisition.AcquisitionError):
                acquisition.extract_document(document())
        draft = item()
        draft['lecture_data'] = {'schedule': {'start_time': '25:61'}}
        self.transport.response = ModelResponse(model_body([draft]))
        with self.assertRaises(acquisition.AcquisitionError) as caught:
            acquisition.extract_document(document())
        self.assertEqual(caught.exception.code, 'model_output_invalid')


    def test_invalid_evidence_image_reference_is_rejected(self):
        draft = item()
        draft['evidence_quotes'] = [{'text': '带有无效图片引用的文字。', 'image_index': 0}]
        self.transport.response = ModelResponse(model_body([draft]))
        with self.assertRaises(acquisition.AcquisitionError) as caught:
            acquisition.extract_document(document())
        self.assertEqual(caught.exception.code, 'model_output_invalid')

    def test_large_poster_fits_vision_limit_without_cropping_or_replacing_evidence(self):
        with Image.new('RGB', (4218, 7499), 'white') as poster:
            poster.paste((0, 220, 0), (0, 0, 100, 100))
            poster.paste((0, 0, 220), (4118, 7399, 4218, 7499))
            stream = io.BytesIO()
            poster.save(stream, format='JPEG', quality=85)
        original = acquisition._image(stream.getvalue())
        before = dict(original)
        prepared = acquisition._model_images([original])[0]
        with Image.open(io.BytesIO(base64.b64decode(prepared['data']))) as resized:
            self.assertLessEqual(math.ceil(resized.width / 32) * math.ceil(resized.height / 32), 30000)
            self.assertAlmostEqual(resized.width / resized.height, 4218 / 7499, places=3)
            self.assertGreater(resized.getpixel((10, 10))[1], 200)
            self.assertGreater(resized.getpixel((resized.width - 11, resized.height - 11))[2], 200)
        self.assertEqual(original, before)

    def test_vision_input_is_real_image_bytes_and_evidence_is_retained(self):
        stream = io.BytesIO()
        Image.new('RGB', (8, 8), (180, 40, 20)).save(stream, format='PNG')
        image = stream.getvalue()
        draft = item(due=None, quote='图片中的公开活动')
        draft['evidence_quotes'] = [{'text': '图片中的公开活动', 'image_index': 0}]
        self.transport.response = ModelResponse(model_body([draft]))
        evidence = document('')
        acquisition.extract_document(evidence, [image])
        parts = self.transport.calls[0][1]['json']['messages'][1]['content']
        self.assertEqual(parts[1]['type'], 'image_url')
        header, encoded = parts[1]['image_url']['url'].split(',', 1)
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as delivered:
            delivered.load()
            self.assertEqual(header, 'data:' + Image.MIME[delivered.format] + ';base64')
            self.assertEqual(delivered.size, (8, 8))
            self.assertLess(sum(abs(a - b) for a, b in zip(delivered.convert('RGB').getpixel((4, 4)), (180, 40, 20))), 20)
        self.assertEqual(base64.b64decode(evidence['media'][0]['data']), image)
        self.assertEqual(len(evidence['media']), 1)
        self.assertTrue(evidence['media'][0]['digest'])
        for invalid in (b'not-an-image', b'x' * (acquisition.MAX_IMAGE + 1)):
            with self.assertRaises(acquisition.AcquisitionError):
                acquisition.extract_document(document(''), [invalid])

    def test_unreadable_remote_images_do_not_silently_become_text_success(self):
        evidence = document()
        evidence['images'] = ['https://mmbiz.qpic.cn/public.png']
        with patch.object(acquisition, 'fetch_public_resource', side_effect=acquisition.AcquisitionError('图片无法公开读取', 'source_blocked')):
            with self.assertRaises(acquisition.AcquisitionError) as caught:
                acquisition.extract_document(evidence)
        self.assertEqual(caught.exception.code, 'source_incomplete')
        self.assertFalse(evidence['capture_complete'])
        self.assertEqual(evidence['capture_errors'][0]['code'], 'source_blocked')
        self.assertEqual(self.transport.calls, [])

    def test_partial_lecture_data_keeps_disagreements_and_fills_missing_fields(self):
        draft = item(title='模型主标题', due='2026-10-20')
        draft['lecture_data'] = {'title': {'primary': '讲座自身标题'},
                                 'schedule': {'date': '2026-10-21', 'start_time': '09:30'}}
        self.transport.response = ModelResponse(model_body([draft]))
        result = acquisition.extract_document(document())[0]
        lecture = result['lecture_data']
        self.assertEqual(lecture['title']['primary'], '讲座自身标题')
        self.assertEqual(lecture['title']['secondary'], '')
        self.assertEqual((lecture['schedule']['date'], lecture['schedule']['start_time'],
                          lecture['schedule']['end_time']), ('2026-10-21', '09:30', ''))
        self.assertEqual(lecture['venue'], '')
        self.assertEqual(lecture['speakers'], [])
        self.assertEqual(lecture['inviter'], {'name': '', 'title': ''})

        del draft['lecture_data']
        draft['tag'] = '讲座'
        self.transport.response = ModelResponse(model_body([draft]))
        self.assertIsNone(acquisition.extract_document(document())[0]['lecture_data'])
        inherited = item(title='模型主标题', due='2026-10-20')
        inherited['lecture_data'] = {'schedule': {'start_time': '10:00'}}
        self.transport.response = ModelResponse(model_body([inherited]))
        lecture = acquisition.extract_document(document())[0]['lecture_data']
        self.assertEqual(lecture['title']['primary'], '模型主标题')
        self.assertEqual(lecture['schedule']['date'], '2026-10-20')
        self.assertEqual(lecture['schedule']['start_time'], '10:00')



    def test_optional_dates_and_college_relations_are_preserved(self):
        original = '信息管理学院举办活动，日期2026年10月20日，报名截止2026年10月18日，面向计算机学院学生。'
        draft = item(quote=original)
        draft.update(deadline_date='2026-10-18',
                     deadline_date_resolution='explicit',
                     colleges=[{'name':'信息管理学院','relation':'associated'},
                               {'name':'计算机学院','relation':'audience'}])
        self.transport.response = ModelResponse(model_body([draft]))
        result = acquisition.extract_document(document(original))[0]
        self.assertEqual(result['event_date'],'2026-10-20')
        self.assertEqual(result['deadline_date'],'2026-10-18')
        self.assertEqual(result['due_time'],'2026-10-18')
        self.assertEqual(result['colleges'][1]['relation'],'audience')
        draft['colleges'] = [{'name':'信息管理学院','relation':'publisher'}]
        self.transport.response = ModelResponse(model_body([draft]))
        with self.assertRaises(acquisition.AcquisitionError):
            acquisition.extract_document(document(original))

    def test_general_notice_without_event_or_deadline_is_supported(self):
        original = '图书馆开放时间调整，请读者留意。'
        draft = item(due=None,quote=original)
        draft['tag'] = '通知'
        self.transport.response = ModelResponse(model_body([draft]))
        result = acquisition.extract_document(document(original))[0]
        self.assertEqual(result['tag'],'通知')
        self.assertIsNone(result['event_date'])
        self.assertIsNone(result['deadline_date'])

    def test_source_publication_comes_from_metadata_not_event_or_apply_date(self):
        article = '<h1>公开活动</h1><article>'+TEXT+'</article>'
        for metadata,expected in [
            ('',None),
            ('<meta property="article:published_time" content="2026-10-12T09:00:00+08:00">','2026-10-12'),
            ('<meta property="article:published_time" content="2026-10-13T20:00:00Z">','2026-10-14'),
            ('<meta property="article:published_time" content="2026-10-13T23:00:00-05:00">','2026-10-14'),
            ('<meta property="article:published_time" content="2026-10-14T01:00:00+14:00">','2026-10-13'),
            ('<meta property="article:published_time" content="2026-10-14T25:00:00Z">',None),
            ('<p>发布时间：2026年10月11日</p>','2026-10-11'),
        ]:
            with self.subTest(metadata=metadata),patch.object(acquisition,'fetch_public_resource',return_value=((metadata+article).encode(),'text/html','https://example.org/article')):
                fetched = acquisition.fetch_document('https://example.org/article')
            self.assertEqual(fetched['source_published_date'],expected)
        record = {'theme':'招聘会','intro':TEXT,'startAt':'2026-10-20','applyAt':'2026-10-18','status':'PUBLISHED'}
        with patch.object(acquisition,'fetch_public_resource',return_value=(json.dumps(record).encode(),'application/json','https://job.nju.edu.cn/api')):
            fetched = acquisition.fetch_document('https://job.nju.edu.cn/career/specifics/123')
        self.assertIsNone(fetched['source_published_date'])
        record['loginRequired'] = True
        with patch.object(acquisition,'fetch_public_resource',return_value=(json.dumps(record).encode(),'application/json','https://job.nju.edu.cn/api')):
            with self.assertRaises(acquisition.AcquisitionError):
                acquisition.fetch_document('https://job.nju.edu.cn/career/specifics/123')



class ResponsesAcquisitionTests(AcquisitionFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        runtime.settings = replace(runtime.settings, model_api_style='responses')
        self.transport.response = ModelResponse(responses_body([item()]))

    def test_responses_request_uses_input_and_json_text_format(self):
        evidence = document()
        drafts = acquisition.extract_document(evidence)
        endpoint, options = self.transport.calls[0]
        self.assertEqual(endpoint, 'https://model.example/v1/responses')
        payload = options['json']
        self.assertEqual(payload['model'], 'gpt-6-luna')
        self.assertEqual(payload['input'][0]['role'], 'user')
        self.assertEqual(payload['input'][0]['content'][0]['type'], 'input_text')
        self.assertEqual(payload['text']['format'], {'type': 'json_object'})
        self.assertFalse(payload['store'])
        self.assertFalse(payload['stream'])
        self.assertNotIn('messages', payload)
        self.assertNotIn('response_format', payload)
        self.assertEqual(drafts[0]['title'], '公开活动甲')
        self.assertEqual(evidence['model_name'], 'gpt-6-luna')

    def test_responses_image_input_is_flat_data_url_and_is_retained(self):
        stream = io.BytesIO()
        Image.new('RGB', (8, 8), (180, 40, 20)).save(stream, format='PNG')
        image = stream.getvalue()
        draft = item(due=None, quote='图片中的公开活动')
        draft['evidence_quotes'] = [{'text': '图片中的公开活动', 'image_index': 0}]
        self.transport.response = ModelResponse(responses_body([draft]))
        evidence = document('')
        result = acquisition.extract_document(evidence, [image])
        parts = self.transport.calls[0][1]['json']['input'][0]['content']
        self.assertEqual(parts[1]['type'], 'input_image')
        header, encoded = parts[1]['image_url'].split(',', 1)
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as delivered:
            delivered.load()
            self.assertEqual(header, 'data:' + Image.MIME[delivered.format] + ';base64')
            self.assertEqual(delivered.size, (8, 8))
            self.assertLess(sum(abs(a - b) for a, b in zip(delivered.convert('RGB').getpixel((4, 4)), (180, 40, 20))), 20)
        self.assertEqual(result[0]['evidence_quotes'][0]['image_index'], 0)
        self.assertEqual(base64.b64decode(evidence['media'][0]['data']), image)

    def test_responses_ignores_reasoning_and_joins_assistant_text_parts(self):
        body = responses_body([item(), item('公开活动乙', '2026-10-21', '公开活动乙将于2026年10月21日在鼓楼举办。')])
        text = body['output'][0]['content'][0]['text']
        split = len(text) // 2
        body['output'][0]['content'] = [{'type': 'output_text', 'text': text[:split]}, {'type': 'output_text', 'text': text[split:]}]
        body['output'].insert(0, {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'This must not become draft JSON.'}]})
        self.transport.response = ModelResponse(body)
        result = acquisition.extract_document(document())
        self.assertEqual(len(result), 2)
        self.assertEqual([entry['due_time'] for entry in result], ['2026-10-20', '2026-10-21'])

    def test_responses_incomplete_failed_and_refused_outputs_are_errors(self):
        invalid = []
        for status in ('incomplete', 'failed', 'cancelled', 'in_progress', 'queued'):
            body = responses_body([item()])
            body['status'] = status
            invalid.append(body)
        for field, value in [('incomplete_details', {'reason': 'max_output_tokens'}), ('error', {'message': 'private-provider-debug private-test-key'})]:
            body = responses_body([item()])
            body[field] = value
            invalid.append(body)
        body = responses_body([item()])
        body['output'][0]['status'] = 'incomplete'
        invalid.append(body)
        body = responses_body([item()])
        body['output'][0]['content'].append({'type': 'refusal', 'refusal': 'private-provider-refusal private-test-key'})
        invalid.append(body)
        for body in invalid:
            with self.subTest(body=body):
                evidence = document()
                self.transport.response = ModelResponse(body)
                with self.assertRaises(acquisition.AcquisitionError) as caught:
                    acquisition.extract_document(evidence)
                self.assertEqual(caught.exception.code, 'model_output_invalid')
                self.assertNotIn('private-provider', str(caught.exception))
                self.assertNotIn('private-test-key', str(caught.exception))
                self.assertNotIn('model_output', evidence)
                self.assertEqual(evidence['model_response'],body)
                self.assertEqual(evidence['model_name'],'gpt-6-luna')

    def test_responses_empty_malformed_nonassistant_and_tool_output_are_errors(self):
        invalid = [[], {}, {'status': 'completed', 'output': []}, {'status': 'completed', 'output': 'not-a-list'}, {'status': 'completed', 'output_text': json.dumps({'items': [item()]})}, model_body([item()])]
        for content in (None, [], ['not-a-part'], [{'type': 'output_text', 'text': ''}], [{'type': 'output_text', 'text': '  '}], [{'type': 'output_text', 'text': 3}], [{'type': 'output_text', 'text': '{invalid-json'}]):
            body = responses_body([item()])
            body['output'][0]['content'] = content
            invalid.append(body)
        body = responses_body([item()])
        body['output'][0]['role'] = 'user'
        invalid.append(body)
        body = responses_body([item()])
        body['output'].insert(0, {'type': 'function_call', 'name': 'publish'})
        invalid.append(body)
        invalid.append({'status': 'completed', 'output': [{'type': 'reasoning', 'summary': []}]})
        for body in invalid:
            with self.subTest(body=body):
                self.transport.response = ModelResponse(body)
                with self.assertRaises(acquisition.AcquisitionError) as caught:
                    acquisition.extract_document(document())
                self.assertEqual(caught.exception.code, 'model_output_invalid')

    def test_responses_protocol_errors_never_retry_chat_or_another_model(self):
        self.transport.response = ModelResponse({'error': 'provider rejected format'}, status=400)
        with self.assertRaises(acquisition.AcquisitionError) as caught:
            acquisition.extract_document(document())
        self.assertEqual(caught.exception.code, 'model_http_error')
        self.assertEqual(len(self.transport.calls), 1)
        self.assertTrue(self.transport.calls[0][0].endswith('/responses'))
        self.assertEqual(self.transport.calls[0][1]['json']['model'], 'gpt-6-luna')




class PersistedAcquisitionTests(AcquisitionFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.database = str(runtime.settings.database_path)
        schema.migrate()

    def extract_two(self):
        self.transport.response = ModelResponse(model_body([item(), item('公开活动乙', '2026-10-21', '公开活动乙将于2026年10月21日在鼓楼举办。')]))
        evidence = document()
        drafts = acquisition.extract_document(evidence)
        return evidence, drafts

    def test_multi_event_insert_is_private_atomic_and_idempotent(self):
        evidence, drafts = self.extract_two()
        ids, created = acquisition.insert_drafts(evidence, drafts, 'editor')
        repeated_ids, repeated = acquisition.insert_drafts(evidence, list(reversed(drafts)), 'other-editor')
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(ids, repeated_ids)
        self.assertEqual(len(ids), 2)
        with sqlite3.connect(self.database) as conn:
            entries = conn.execute('SELECT status,publish_date,describer,reviewer,link FROM entries ORDER BY id').fetchall()
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM acquisitions').fetchone()[0], 1)
        self.assertEqual(entries, [('described', None, 'model:gpt-6-luna', None, evidence['canonical_url'])] * 2)
        saved = acquisition.get_entry_evidence(ids[0])
        self.assertEqual(saved['text'], TEXT)
        self.assertEqual(saved['digest'], evidence['digest'])
        self.assertEqual(saved['model_producer'], 'model:gpt-6-luna')


    def test_shared_and_missing_evidence_keep_distinct_events(self):
        shared_quote = [{'text': '同一段来源文字。', 'image_index': None}]
        first = item(title='共享引用活动甲', due='2026-10-20', quote='活动甲描述，14:00在仙林。')
        first.update(evidence_quotes=shared_quote,
                     lecture_data={'schedule': {'date': '2026-10-20', 'start_time': '14:00'},
                                   'venue': '仙林'})
        second = item(title='共享引用活动乙', due='2026-10-20', quote='活动乙描述，15:00在鼓楼。')
        second.update(evidence_quotes=[dict(shared_quote[0])],
                      lecture_data={'schedule': {'date': '2026-10-22', 'start_time': '15:00'},
                                    'venue': '鼓楼'})
        third = item(title='无引用活动丙', due='2026-10-23', quote='活动丙独立描述。')
        self.transport.response = ModelResponse(model_body([first, second, third]))
        evidence = document('同一段来源文字。')
        drafts = acquisition.extract_document(evidence)
        ids, created = acquisition.insert_drafts(evidence, drafts, 'editor')
        self.assertTrue(created)
        self.assertEqual(len(ids), 3)
        with sqlite3.connect(self.database) as conn:
            entries = conn.execute('SELECT title,description,status,publish_date FROM entries ORDER BY id').fetchall()
        self.assertEqual(entries, [
            ('共享引用活动甲', '活动甲描述，14:00在仙林。', 'described', None),
            ('共享引用活动乙', '活动乙描述，15:00在鼓楼。', 'described', None),
            ('无引用活动丙', '活动丙独立描述。', 'described', None),
        ])

    def test_failure_rolls_back_acquisition_and_every_entry(self):
        evidence, drafts = self.extract_two()
        drafts[1]['event_key'] = drafts[0]['event_key']
        with self.assertRaises(sqlite3.IntegrityError):
            acquisition.insert_drafts(evidence, drafts, 'editor')
        with sqlite3.connect(self.database) as conn:
            for table in ('entries', 'acquisitions', 'acquisition_candidates'):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)

    def test_query_identity_and_changed_content_are_separate_acquisitions(self):
        evidence, drafts = self.extract_two()
        acquisition.insert_drafts(evidence, drafts, 'editor')
        changed = document(source='https://example.org/article?a=2')
        other = acquisition.extract_document(changed)
        ids, created = acquisition.insert_drafts(changed, other, 'editor')
        self.assertTrue(created)
        self.assertEqual(len(ids), 2)
        with sqlite3.connect(self.database) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM entries').fetchone()[0], 4)

    def test_responses_failures_retain_raw_source_then_multi_events_import_once(self):
        runtime.settings = replace(runtime.settings, model_api_style='responses')
        app = Flask(__name__)
        app.secret_key = 'fixture-only-secret'
        app.add_url_rule('/describe/<int:entry_id>', endpoint='describe', view_func=lambda entry_id: 'editor')
        app.add_url_rule('/review/<int:entry_id>', endpoint='review', view_func=lambda entry_id: 'review')
        acquisition.register(app)
        client = app.test_client()
        with client.session_transaction() as state:
            state['username'] = 'editor'
            state['csrf_token'] = 'token'
        form = {'public_text': TEXT, 'public_confirm': 'yes', 'csrf_token': 'token'}
        headers = {'Accept': 'application/json'}
        incomplete = responses_body([item()])
        incomplete['status'] = 'incomplete'
        refused = responses_body([item()])
        refused['output'][0]['content'] = [{'type': 'refusal', 'refusal': 'private-provider-refusal'}]
        with patch.object(acquisition.runtime, 'get_user_role', return_value='editor'):
            for invalid in (incomplete, refused):
                self.transport.response = ModelResponse(invalid)
                response = client.post('/acquire', data=form, headers=headers)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.get_json()['code'], 'model_output_invalid')
                with sqlite3.connect(self.database) as conn:
                    for table in ('entries', 'acquisitions', 'acquisition_candidates'):
                        self.assertEqual(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
                    self.assertEqual(conn.execute('SELECT model_status FROM source_documents').fetchall(), [('failed',)])
                from nannanews import information
                raw = information.query_records({})['records']
                self.assertEqual(len(raw), 1)
                self.assertEqual(raw[0]['content_text'], TEXT)
                self.assertIsNone(raw[0]['entry_id'])
            self.transport.response = ModelResponse(responses_body([item(), item('公开活动乙', '2026-10-21', '公开活动乙将于2026年10月21日在鼓楼举办。')]))
            first = client.post('/acquire', data=form, headers=headers)
            repeated = client.post('/acquire', data=form, headers=headers)
            self.assertEqual(first.status_code, 201)
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(len(first.get_json()['entries']), 2)
            self.assertEqual(first.get_json()['entries'], repeated.get_json()['entries'])
            self.assertEqual(len(self.transport.calls), 3)
            self.assertEqual(information.query_records({})['total'], 2)
            self.assertTrue(all(row['entry_id'] is not None for row in information.query_records({})['records']))
        with sqlite3.connect(self.database) as conn:
            rows = conn.execute('SELECT describer,status,publish_date FROM entries ORDER BY id').fetchall()
        self.assertEqual(rows, [('model:gpt-6-luna', 'described', None)] * 2)

    def test_authenticated_csrf_and_repeat_form_import_without_repeat_model(self):
        app = Flask(__name__)
        app.secret_key = 'fixture-only-secret'
        app.add_url_rule('/describe/<int:entry_id>', endpoint='describe', view_func=lambda entry_id: 'editor')
        app.add_url_rule('/review/<int:entry_id>', endpoint='review', view_func=lambda entry_id: 'review')
        acquisition.register(app)
        client = app.test_client()
        headers = {'Accept': 'application/json'}
        form = {'public_text': TEXT, 'public_confirm': 'yes', 'csrf_token': 'token'}
        with patch.object(acquisition.runtime, 'get_user_role', return_value='editor'):
            self.assertEqual(client.post('/acquire', data=form, headers=headers).status_code, 401)
            with client.session_transaction() as state:
                state['username'] = 'editor'
                state['csrf_token'] = 'token'
            bad = dict(form, csrf_token='wrong')
            self.assertEqual(client.post('/acquire', data=bad, headers=headers).status_code, 400)
            first = client.post('/acquire', data=form, headers=headers)
            second = client.post('/acquire', data=form, headers=headers)
            self.assertEqual(first.status_code, 201)
            self.assertEqual(second.status_code, 200)
            self.assertFalse(second.get_json()['created'])
            self.assertEqual(first.get_json()['entries'], second.get_json()['entries'])
            self.assertEqual(len(self.transport.calls), 1)
            entry_id = first.get_json()['entries'][0]['id']
            evidence_response = client.get(f'/acquire/evidence/{entry_id}', headers=headers)
            self.assertEqual(evidence_response.status_code, 200)
            self.assertEqual(evidence_response.headers['Cache-Control'], 'private, no-store')
            self.assertNotIn('private-test-key', evidence_response.get_data(as_text=True))
            with patch.object(acquisition.runtime, 'get_user_role', return_value='member'):
                self.assertEqual(client.get(f'/acquire/evidence/{entry_id}', headers=headers).status_code, 403)
                self.assertEqual(client.get('/acquire/model-status').status_code, 403)
            with client.session_transaction() as state:
                state.clear()
            self.assertEqual(client.get(f'/acquire/evidence/{entry_id}', headers=headers).status_code, 401)

    def test_job_engagement_and_audit_changes_do_not_repeat_paid_extraction(self):
        from nannanews import information
        source = 'https://job.nju.edu.cn/career/specifics/123'
        record = {'theme':'公开招聘','intro':TEXT,'startAt':'2026-10-20',
                  'deadline':'2026-10-18','publishAt':'2026-10-12','status':'PUBLISHED',
                  'trafficStat':{'view':10,'like':1},'createdBy':'private-admin-id',
                  'updatedBy':'private-updater-id','createdAt':'2026-01-01',
                  'updatedAt':'2026-10-13T00:00:00','sticky':False,
                  'company':{'name':'公开公司','updatedBy':'nested-private-id'}}
        original_bytes = json.dumps({'data':record},ensure_ascii=False).encode()
        with patch.object(acquisition,'fetch_public_resource',return_value=(original_bytes,'application/json','https://job.nju.edu.cn/api')):
            first_document = acquisition.fetch_document(source)
        first = information.save_document(first_document,None,'editor')
        result = information.enrich_document(first['id'],'editor')
        record.update(trafficStat={'view':11,'like':2},updatedBy='different-admin-id',
                      updatedAt='2026-10-14T00:00:00',sticky=True)
        record['company']['updatedBy'] = 'different-nested-admin-id'
        changed_bytes = json.dumps({'data':record},ensure_ascii=False).encode()
        with patch.object(acquisition,'fetch_public_resource',return_value=(changed_bytes,'application/json','https://job.nju.edu.cn/api')):
            revisited = acquisition.fetch_document(source)
        repeated = information.save_document(revisited,None,'editor')
        self.assertFalse(repeated['created'])
        self.assertEqual(repeated['id'],first['id'])
        self.assertEqual(information.enrich_document(first['id'],'editor')['entry_ids'],result['entry_ids'])
        self.assertEqual(len(self.transport.calls),1)
        request_text = self.transport.calls[0][1]['json']['messages'][1]['content'][0]['text']
        for field in ('trafficStat','createdBy','updatedBy','createdAt','updatedAt','sticky','private-admin-id','nested-private-id'):
            self.assertNotIn(field,request_text)
        self.assertIn('publishAt',request_text)
        self.assertIn('startAt',request_text)
        self.assertIn('deadline',request_text)
        captured = information.get_document(first['id'])
        self.assertEqual(base64.b64decode(captured['raw_response_base64']),original_bytes)
        self.assertEqual(json.loads(captured['raw_content'])['data']['trafficStat']['view'],10)
        self.assertEqual(json.loads(revisited['raw_content'])['data']['trafficStat']['view'],11)
        record['intro'] += '\n活动内容发生实际更新。'
        with patch.object(acquisition,'fetch_public_resource',return_value=(json.dumps({'data':record}).encode(),'application/json','https://job.nju.edu.cn/api')):
            changed = information.save_document(acquisition.fetch_document(source),None,'editor')
        self.assertTrue(changed['created'])
        self.assertNotEqual(changed['id'],first['id'])


class PublicCatalogTests(unittest.TestCase):
    def test_catalog_is_explicit_allowlist_never_private_workbook_rows(self):
        from nannanews import source_catalog as importer
        workbooks = [{'file': '社团清单.xlsx', 'sheets': [{'name': 'Sheet1', 'state': 'visible', 'rows': [{'row': 1, 'cells': {'A': '社团', 'E': '公众号', 'F': '群聊', 'G': '其他联系方式'}}, {'row': 2, 'cells': {'A': '公开社团', 'E': '公开账号', 'F': 'private-group-999', 'G': 'private-contact@example.org', 'H': 'private-notes'}}]}]}]
        catalog = importer.build_catalog(workbooks)
        self.assertEqual(catalog[0]['name'], '公开账号')
        self.assertEqual(catalog[0]['unit'], '公开社团')
        serialized = json.dumps(catalog)
        for private in ('private-group-999', 'private-contact', 'private-notes', 'cells'):
            self.assertNotIn(private, serialized)



class ModelAddressRegressionTests(AcquisitionFixture, unittest.TestCase):
    def test_refused_first_address_does_not_prevent_real_response_from_next_address(self):
        class Refused(FakeModelConnection):
            def connect(self):
                raise ConnectionRefusedError('IPv6 listener absent')
            def request(self, *args, **kwargs):
                raise ConnectionRefusedError('IPv6 listener absent')
        refused = Refused(ModelResponse(model_body([item()])))
        reachable = FakeModelConnection(ModelResponse(model_body([item()])))
        with patch.object(acquisition, '_resolve_addresses', return_value=['::1', '127.0.0.1']), patch.object(acquisition, '_PinnedHTTPS', side_effect=[refused, reachable]):
            drafts = acquisition.extract_document(document())
        self.assertEqual(drafts[0]['title'], '公开活动甲')
        self.assertEqual(refused.calls, [])
        self.assertEqual(len(reachable.calls), 1)

    def test_official_calendar_selects_article_and_ignores_template_icons(self):
        template = ''.join(f'<img src="/template/icon{i}.png">' for i in range(5))
        html = ('<html><head><title>模板标题</title><meta name="pubdate" content="2025-11-20"></head><body>' + template +
                '<div class="infobox"><div class="article"><h1>人民科学家程开甲院士先进事迹</h1>'
                '<div class="entry"><span>时间</span>2025-11-27 16:00:00'
                '<span>地点</span>南雍楼西353</div><div class="read1"><img src="/poster.jpg"></div>'
                '</div></div>' + template + '</body></html>')
        with patch.object(acquisition, 'fetch_public_resource', return_value=(html.encode(), 'text/html', 'https://example.org/news')):
            captured = acquisition.fetch_document('https://example.org/news')
        self.assertEqual(captured['title'], '人民科学家程开甲院士先进事迹')
        self.assertEqual(captured['source_published_date'], '2025-11-20')
        self.assertIn('2025-11-27 16:00:00', captured['text'])
        self.assertIn('南雍楼西353', captured['text'])
        self.assertEqual(captured['images'], ['https://example.org/poster.jpg'])

if __name__ == '__main__':
    unittest.main()
