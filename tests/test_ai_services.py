"""Local AI boundaries; mocked inference and no live service starts or queue writes."""
import io
import base64
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import socket
import tempfile
import threading
import time
import struct
import unittest
import zlib
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import ai_services as ai
import media_previews as media


class Response:
    def __init__(self, value, status=200, headers=None):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.body = io.BytesIO(raw)
        self.status, self.headers, self.closed = status, headers or {}, False

    def read(self, limit=-1):
        return self.body.read(limit)

    def readline(self, limit=-1):
        return self.body.readline(limit)

    def getcode(self):
        return self.status

    def close(self):
        self.closed = True
        self.body.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def catalog(loaded=True, vision=None):
    return {'models': [
        {'type': 'llm', 'key': 'catalog-key', 'display_name': 'Loaded model',
         'capabilities': {'vision': vision},
         'loaded_instances': [{'id': 'exact-instance', 'config': {'context_length': 8192}}] if loaded else []},
        {'type': 'embedding', 'loaded_instances': [{'id': 'embedding-instance'}]},
        {'type': 'llm', 'key': 'downloaded-only', 'loaded_instances': []}]}


def image_url(format='PNG', metadata=False, size=(8, 6)):
    from PIL import Image, PngImagePlugin
    output = io.BytesIO()
    options = {}
    if metadata and format == 'PNG':
        info = PngImagePlugin.PngInfo()
        info.add_text('workflow', 'private-image-metadata')
        options['pnginfo'] = info
    Image.new('RGB', size, '#234567').save(output, format, **options)
    mime = {'PNG': 'png', 'JPEG': 'jpeg', 'WEBP': 'webp'}[format]
    return 'data:image/' + mime + ';base64,' + base64.b64encode(output.getvalue()).decode()


def image_chat(url=None):
    return {'model': 'exact-instance', 'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'Describe this image'},
        {'type': 'image_url', 'image_url': {'url': url or image_url()}}]}]}


def events(generator):
    return [json.loads(chunk.removeprefix('data: ').strip()) for chunk in generator]


@contextmanager
def fake_lm(mode):
    """An isolated local HTTP fixture; never connects to LM Studio."""
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def wait_for_close(self):
            self.connection.settimeout(0.05)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not self.server.release.is_set():
                try:
                    if not self.connection.recv(1):
                        self.server.closed.set()
                        return
                except socket.timeout:
                    continue
                except OSError:
                    self.server.closed.set()
                    return
            self.close_connection = True

        def do_GET(self):
            if self.path != '/api/v1/models':
                self.send_error(404)
                return
            if mode == 'catalog-stall':
                self.server.entered.set()
                self.wait_for_close()
                return
            raw = json.dumps(catalog(mode != 'unloaded', mode.startswith('vision-'))).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            if self.path != '/v1/chat/completions':
                self.send_error(404)
                return
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            self.server.received_body = body
            self.server.received_model = body['model']
            self.server.post_count += 1
            if mode == 'headers-stall':
                self.server.entered.set()
                self.wait_for_close()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.flush()
            if mode in ('complete', 'partial', 'vision-complete', 'vision-partial'):
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n')
            if mode in ('complete', 'vision-complete'):
                self.wfile.write(b'data: [DONE]\n\n')
            if mode == 'malformed':
                self.wfile.write(b'data: invalid-private-fixture\n\n')
            self.wfile.flush()
            self.server.entered.set()
            self.wait_for_close()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    server.entered, server.closed, server.release = threading.Event(), threading.Event(), threading.Event()
    server.post_count, server.received_model = 0, None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class AIServicesTests(unittest.TestCase):
    def setUp(self):
        self.service = ai.AIService(Path(__file__).resolve().parent.parent)
        self.chat = {'model': 'exact-instance', 'messages': [{'role': 'user', 'content': 'Test text'}]}

    def test_models_only_expose_loaded_llm_instances(self):
        with patch.object(self.service, '_open', return_value=Response(catalog())) as call:
            data = self.service.models()
        self.assertEqual(data['models'], [{'id': 'exact-instance', 'name': 'Loaded model', 'contextLength': 8192, 'vision': None}])
        self.assertTrue(data['available'])
        self.assertEqual(data['availableModels'], [{'id': 'catalog-key', 'name': 'Loaded model', 'vision': None},
                                                  {'id': 'downloaded-only', 'name': 'downloaded-only', 'vision': None}])
        call.assert_called_once_with(ai.LM_BASE, '/api/v1/models')
        with patch.object(self.service, '_open', return_value=Response(catalog(False))):
            self.assertEqual(self.service.models()['models'], [])

    def test_vision_flag_only_uses_documented_strict_boolean(self):
        for capability, expected in [(True, True), (False, False), (None, None), ('true', None), (1, None), ({}, None)]:
            with self.subTest(capability=capability), patch.object(self.service, '_open', return_value=Response(catalog(vision=capability))):
                self.assertIs(self.service.models()['models'][0]['vision'], expected)
        value = catalog()
        value['models'][0]['display_name'] = 'Vision VL image model'
        value['models'][0]['capabilities'] = 'vision'
        with patch.object(self.service, '_open', return_value=Response(value)):
            self.assertIsNone(self.service.models()['models'][0]['vision'], 'Names and malformed capabilities must not imply vision support')

    def test_image_payload_decodes_reencodes_and_removes_metadata(self):
        from PIL import Image
        for format in ('PNG', 'JPEG', 'WEBP'):
            with self.subTest(format=format):
                clean = self.service._chat_payload(image_chat(image_url(format, metadata=True)))
                value = clean['messages'][0]['content'][1]['image_url']['url']
                self.assertTrue(value.startswith('data:image/jpeg;base64,'))
                raw = base64.b64decode(value.split(',', 1)[1])
                self.assertNotIn(b'private-image-metadata', raw)
                with Image.open(io.BytesIO(raw)) as decoded:
                    self.assertEqual(decoded.format, 'JPEG')
                    self.assertEqual(decoded.size, (8, 6))
                    self.assertEqual(dict(decoded.getexif()), {})
        clean = self.service._chat_payload(image_chat(image_url(size=(2000, 10))))
        with Image.open(io.BytesIO(base64.b64decode(clean['messages'][0]['content'][1]['image_url']['url'].split(',', 1)[1]))) as image:
            self.assertEqual(image.width, ai.CHAT_IMAGE_PREVIEW_SIDE)
        encoded = io.BytesIO(); exif = Image.Exif(); exif[274] = 6; exif[271] = 'private-image-metadata'
        Image.new('RGB', (8, 6)).save(encoded, 'JPEG', exif=exif)
        corrected = self.service._image_data_url('data:image/jpeg;base64,' + base64.b64encode(encoded.getvalue()).decode())
        raw = base64.b64decode(corrected.split(',', 1)[1])
        self.assertNotIn(b'private-image-metadata', raw)
        with Image.open(io.BytesIO(raw)) as image:
            self.assertEqual(image.size, (6, 8), 'Phone EXIF orientation must be applied before removing metadata')

    def test_image_input_rejects_fetches_extra_options_bad_types_and_corrupt_data(self):
        invalid_urls = ['https://private.invalid/image.jpg', 'file:///C:/private.jpg', 'data:image/svg+xml;base64,PHN2Zz4=',
                        'data:image/png;base64,***', 'data:image/png;base64,AAA',
                        'data:image/png;base64,' + base64.b64encode(b'private-corrupt-image').decode(),
                        image_url().replace('image/png', 'image/jpeg'),
                        'data:image/png;base64,' + 'A' * (4 * ((ai.MAX_CHAT_IMAGE_BYTES + 2) // 3) + 4)]
        payloads = [image_chat(url) for url in invalid_urls]
        extra = image_chat(); extra['messages'][0]['content'][1]['image_url']['detail'] = 'high'; payloads.append(extra)
        role = image_chat(); role['messages'][0]['role'] = 'system'; payloads.append(role)
        for payload in payloads:
            with patch.object(self.service, '_open') as call:
                result = events(self.service.stream_chat(payload))
            self.assertIn('error', result[0]); call.assert_not_called()
            self.assertNotIn('private-corrupt-image', result[0]['error'])
        with patch.object(ai, 'MAX_CHAT_IMAGE_BYTES', 8), self.assertRaises(ValueError):
            self.service._chat_payload(image_chat())

    def test_image_dimension_animation_count_and_text_limits(self):
        raw = base64.b64decode(image_url().split(',', 1)[1])
        for width, height in [(8193, 1), (4001, 4000)]:
            header = bytearray(raw); header[16:24] = struct.pack('>II', width, height)
            header[29:33] = struct.pack('>I', zlib.crc32(header[12:29]))
            with self.assertRaises(ValueError):
                self.service._chat_payload(image_chat('data:image/png;base64,' + base64.b64encode(header).decode()))
        from PIL import Image
        animated = io.BytesIO()
        Image.new('RGB', (2, 2), 'red').save(animated, 'WEBP', save_all=True, append_images=[Image.new('RGB', (2, 2), 'blue')])
        with self.assertRaises(ValueError):
            self.service._chat_payload(image_chat('data:image/webp;base64,' + base64.b64encode(animated.getvalue()).decode()))
        payload = image_chat(); part = payload['messages'][0]['content'][1]
        payload['messages'][0]['content'] = [part] * 3
        self.assertEqual(len(self.service._chat_payload(payload)['messages'][0]['content']), 3, 'Image-only messages are supported')
        payload['messages'].append({'role': 'user', 'content': [part]})
        with self.assertRaisesRegex(ValueError, 'at most 3'):
            self.service._chat_payload(payload)
        payload = image_chat(); payload['messages'][0]['content'][0]['text'] = 'x' * 16385
        with self.assertRaisesRegex(ValueError, 'too long'):
            self.service._chat_payload(payload)

    def test_images_only_infer_with_fresh_loaded_confirmed_vision_instance(self):
        for vision in (False, None, 'true', 1):
            with self.subTest(vision=vision), patch.object(self.service, '_open', return_value=Response(catalog(vision=vision))) as call:
                result = events(self.service.stream_chat(image_chat()))
            self.assertEqual(call.call_count, 1)
            self.assertIn('does not support images' if vision is False else 'has not reported image support', result[0]['error'])
        with patch.object(self.service, '_open', return_value=Response(catalog(False, True))) as call:
            self.assertIn('no longer loaded', events(self.service.stream_chat(image_chat()))[0]['error'])
            self.assertEqual(call.call_count, 1)
        stream = Response(b'data: {"choices":[{"delta":{"content":"Image reply"}}]}\n\ndata: [DONE]\n\n')
        with patch.object(self.service, '_open', side_effect=[Response(catalog(vision=True)), stream]) as call:
            self.assertEqual(events(self.service.stream_chat(image_chat())), [{'text': 'Image reply'}, {'done': True}])
        sent = json.loads(call.call_args.args[2])
        self.assertEqual(set(sent), {'model', 'messages', 'stream'})
        self.assertEqual(sent['model'], 'exact-instance')
        self.assertEqual(sent['messages'][0]['content'][1]['type'], 'image_url')
        self.assertTrue(stream.closed)

    def test_explicit_load_uses_only_fresh_installed_llm_key_and_confirms_instance(self):
        reply = {'type': 'llm', 'instance_id': 'exact-instance', 'status': 'loaded'}
        with patch.object(self.service, '_open', side_effect=[Response(catalog(False)), Response(reply), Response(catalog())]) as call:
            result = self.service.load_lm_model({'model': 'catalog-key'})
        self.assertTrue(result['ok'])
        self.assertEqual(result['selectedInstanceId'], 'exact-instance')
        args, kwargs = call.call_args_list[1]
        self.assertEqual(args[:2], (ai.LM_BASE, '/api/v1/models/load'))
        self.assertEqual(json.loads(args[2]), {'model': 'catalog-key'})
        self.assertEqual(kwargs['timeout'], 120)
        with patch.object(self.service, '_open', return_value=Response(catalog())) as call:
            result = self.service.load_lm_model({'model': 'catalog-key'})
            self.assertTrue(result['ok'])
            self.assertEqual(result['selectedInstanceId'], 'exact-instance')
            self.assertEqual(call.call_count, 1)
        for payload in [{'model': 'unknown'}, {'model': 'embedding-instance'}, {'model': 'catalog-key', 'context_length': 8}]:
            with patch.object(self.service, '_open', return_value=Response(catalog(False))) as call, self.assertRaises(ValueError):
                self.service.load_lm_model(payload)
            self.assertLessEqual(call.call_count, 1)
        with patch.object(self.service, '_open', side_effect=[Response(catalog(False)), Response(reply), Response(catalog(False))]):
            self.assertFalse(self.service.load_lm_model({'model': 'catalog-key'})['ok'])
        wrong_owner = catalog(False)
        wrong_owner['models'][2]['loaded_instances'] = [{'id': 'exact-instance'}]
        with patch.object(self.service, '_open', side_effect=[Response(catalog(False)), Response(reply), Response(wrong_owner)]):
            result = self.service.load_lm_model({'model': 'catalog-key'})
            self.assertFalse(result['ok'])
            self.assertIsNone(result['selectedInstanceId'])
        for error in [URLError('private-offline'), HTTPError(ai.LM_BASE, 401, 'private-auth', {}, None)]:
            with patch.object(self.service, '_open', side_effect=error):
                result = self.service.load_lm_model({'model': 'catalog-key'})
            self.assertFalse(result['ok'])
            self.assertNotIn('private-', result['message'])

    def test_quick_models_only_include_approved_installed_capabilities(self):
        installed = {'models': [
            {'type': 'llm', 'key': identifier, 'display_name': name, 'capabilities': {'vision': vision}, 'loaded_instances': []}
            for identifier, name, vision, _ in ai.FAST_MODEL_CHOICES]}
        with patch.object(self.service, '_open', return_value=Response(installed)) as call:
            result = self.service.models()
        self.assertEqual([item['id'] for item in result['quickModels']], [item[0] for item in ai.FAST_MODEL_CHOICES])
        self.assertEqual([item['vision'] for item in result['quickModels']], [True, True, False])
        self.assertEqual(result['models'], [], 'Recommendations must not load or select a model')
        self.assertEqual(call.call_count, 1)
        installed['models'][0]['capabilities']['vision'] = 'true'
        installed['models'][1]['key'] = 'unapproved-vision-finetune'
        with patch.object(self.service, '_open', return_value=Response(installed)):
            self.assertEqual([item['id'] for item in self.service.models()['quickModels']], ['liquid/lfm2.5-1.2b'])
        with patch.object(self.service, '_open', side_effect=URLError('offline')):
            self.assertEqual(self.service.models()['quickModels'], [])

    def test_models_offline_auth_and_malformed_responses_degrade(self):
        failures = [URLError('offline'), HTTPError(ai.LM_BASE, 401, 'private body', {}, None)]
        for error in failures:
            with self.subTest(kind=type(error).__name__), patch.object(self.service, '_open', side_effect=error):
                result = self.service.models()
                self.assertFalse(result['available'])
                self.assertNotIn('private body', result['message'])
        for value in [b'not-json', {'models': {}}, []]:
            with patch.object(self.service, '_open', return_value=Response(value)):
                self.assertFalse(self.service.models()['available'])

    def test_selected_exact_instance_streams_without_model_changes(self):
        stream = Response(b'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}\n\n'
                          b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        with patch.object(self.service, '_open', side_effect=[Response(catalog()), stream]) as call:
            result = events(self.service.stream_chat(self.chat))
        self.assertEqual(result, [{'text': 'Hello'}, {'done': True}])
        args, kwargs = call.call_args
        self.assertEqual(args[:2], (ai.LM_BASE, '/v1/chat/completions'))
        self.assertEqual(json.loads(args[2]), dict(self.chat, stream=True))
        self.assertLessEqual(kwargs['timeout'], 20)
        self.assertTrue(stream.closed)

    def test_unloaded_model_never_posts_inference(self):
        with patch.object(self.service, '_open', return_value=Response(catalog(False))) as call:
            result = events(self.service.stream_chat(self.chat))
        self.assertIn('error', result[0])
        self.assertEqual(call.call_count, 1)

    def test_chat_validation_rejects_urls_extras_multimodal_and_limits(self):
        invalid = [dict(self.chat, baseUrl='http://example.invalid'),
                   {'model': 'exact-instance', 'messages': [{'role': 'tool', 'content': 'text'}]},
                   {'model': 'exact-instance', 'messages': [{'role': 'user', 'content': [{'url': 'file:///private'}]}]},
                   {'model': 'exact-instance', 'messages': self.chat['messages'] * 33},
                   {'model': 'exact-instance', 'messages': [{'role': 'user', 'content': 'x' * 16385}]}]
        for payload in invalid:
            with self.subTest(keys=sorted(payload)), patch.object(self.service, '_open') as call:
                self.assertIn('error', events(self.service.stream_chat(payload))[0])
                call.assert_not_called()

    def test_stream_cancel_closes_upstream_and_malformed_content_is_hidden(self):
        stream = Response(b'data: {"choices":[{"delta":{"content":"First"}}]}\n\n'
                          b'data: {"choices":[{"delta":{"content":"Second"}}]}\n\n')
        with patch.object(self.service, '_open', side_effect=[Response(catalog()), stream]):
            generator = self.service.stream_chat(self.chat)
            next(generator)
            generator.close()
        self.assertTrue(stream.closed)
        for raw in [b'data: private-invalid-value\n\n', b'data: {"choices":[{"delta":{"content":[]}}]}\n\n', b'']:
            stream = Response(raw)
            with patch.object(self.service, '_open', side_effect=[Response(catalog()), stream]):
                result = events(self.service.stream_chat(self.chat))
            self.assertIn('error', result[-1])
            self.assertNotIn('private-invalid-value', json.dumps(result))
            self.assertTrue(stream.closed)

    def test_stream_response_and_metadata_have_byte_limits(self):
        with patch.object(ai, 'MAX_JSON_BYTES', 8), patch.object(self.service, '_open', return_value=Response(catalog())):
            self.assertFalse(self.service.models()['available'])
        stream = Response(b'data: {"choices":[]}\n\n')
        with patch.object(ai, 'MAX_STREAM_BYTES', 8), patch.object(self.service, '_open', side_effect=[Response(catalog()), stream]):
            self.assertIn('error', events(self.service.stream_chat(self.chat))[-1])
        self.assertTrue(stream.closed)

    def test_start_is_fixed_loopback_cli_and_never_loads_a_model(self):
        offline = {'ok': True, 'available': False, 'models': [], 'message': 'offline'}
        online = {'ok': True, 'available': True, 'models': [], 'message': 'online'}
        with patch.object(self.service, 'models', side_effect=[offline, online]), patch.object(Path, 'is_file', return_value=True), \
                patch.object(ai.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'', b'')) as run:
            self.assertTrue(self.service.start_lm_api()['available'])
        self.assertEqual(run.call_args.args[0][1:], ['server', 'start', '--port', '1234', '--bind', '127.0.0.1'])
        self.assertEqual(Path(run.call_args.args[0][0]), Path.home() / '.lmstudio' / 'bin' / 'lms.exe')
        with patch.object(self.service, 'models', return_value=online), patch.object(ai.subprocess, 'run') as run:
            self.service.start_lm_api()
            run.assert_not_called()

    def prime_outputs(self, files):
        history = {'job': {'outputs': {'1': {'images': files}}, 'prompt': {'private-workflow': 'not exposed'}}}
        with patch.object(self.service, '_open', side_effect=[Response({}), Response({'queue_running': [], 'queue_pending': []}), Response(history)]) as call:
            result = self.service.comfy_status()
        self.assertEqual([item.args[:2] for item in call.call_args_list],
                         [(ai.COMFY_BASE, '/system_stats'), (ai.COMFY_BASE, '/queue'), (ai.COMFY_BASE, '/history?max_items=10')])
        return result

    def test_comfy_metadata_is_bounded_safe_and_read_only(self):
        files = [{'filename': 'clip.mp4', 'subfolder': '', 'type': 'output'},
                 {'filename': 'image.png', 'subfolder': 'safe/nested', 'type': 'output'},
                 {'filename': '../private.png', 'subfolder': '', 'type': 'output'},
                 {'filename': 'private.png', 'subfolder': '../private', 'type': 'output'},
                 {'filename': 'private.png', 'subfolder': 'C:\\private', 'type': 'output'},
                 {'filename': '%2e%2e.png', 'subfolder': '', 'type': 'output'},
                 {'filename': 'input.png', 'subfolder': '', 'type': 'input'},
                 {'filename': 'page.html', 'subfolder': '', 'type': 'output'}]
        result = self.prime_outputs(files)
        self.assertTrue(result['available'])
        self.assertEqual([item['type'] for item in result['outputs']], ['video', 'image'])
        self.assertNotIn('private-workflow', json.dumps(result))
        self.assertRegex(result['outputs'][0]['id'], r'^[0-9a-f]{32}$')
        again = self.prime_outputs(files)
        self.assertEqual(result['outputs'][0]['id'], again['outputs'][0]['id'])
        many = self.prime_outputs([{'filename': str(i) + '.png', 'type': 'output'} for i in range(100)])
        self.assertEqual(len(many['outputs']), 40)

    def test_unknown_output_ids_and_redirects_cannot_select_paths_or_hosts(self):
        with patch.object(self.service, '_open') as call:
            for value in ['../private', 'http://example.invalid/x', '0' * 32]:
                with self.assertRaises(ValueError):
                    self.service.output(value)
            call.assert_not_called()
        with self.assertRaises(ValueError):
            self.service._open('http://example.invalid', '/view?filename=x')
        with self.assertRaises(HTTPError):
            ai.NoRedirect().redirect_request(Request(ai.COMFY_BASE + '/queue'), None, 302, 'redirect', {}, 'http://example.invalid')

    def test_image_preview_quotes_trusted_metadata_and_removes_workflow_metadata(self):
        from PIL import Image, PngImagePlugin
        image = io.BytesIO()
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text('workflow', 'private-workflow')
        Image.new('RGB', (4, 4)).save(image, 'PNG', pnginfo=metadata)
        identifier = self.prime_outputs([{'filename': 'image & 1.png', 'subfolder': 'safe folder', 'type': 'output'}])['outputs'][0]['id']
        with patch.object(self.service, '_open', return_value=Response(image.getvalue())) as call:
            content_type, preview = self.service.output(identifier)
        self.assertEqual(content_type, 'image/jpeg')
        self.assertNotIn(b'private-workflow', preview)
        params = parse_qs(urlsplit(call.call_args.args[1]).query)
        self.assertEqual(params, {'filename': ['image & 1.png'], 'subfolder': ['safe folder'], 'type': ['output']})

    def test_video_ranges_are_capped_validated_and_do_not_buffer_whole_files(self):
        identifier = self.prime_outputs([{'filename': 'video.mp4', 'type': 'output'}])['outputs'][0]['id']
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / 'preview.mp4'
            preview.write_bytes(b'a' * (ai.MAX_RANGE_BYTES + 256))
            with patch.object(self.service._video_previews, 'get', return_value=preview), patch.object(self.service, '_open') as call:
                content_type, raw, metadata = self.service.output(identifier, 'bytes=0-')
                call.assert_not_called()
            self.assertEqual(content_type, 'video/mp4')
            self.assertEqual(len(raw), ai.MAX_RANGE_BYTES)
            self.assertEqual(metadata['headers']['Content-Range'], 'bytes 0-4194303/4194560')
        for invalid in ['bytes=0-1,2-3', 'bytes=2-1', 'bytes=-0', 'file=0-1']:
            with patch.object(self.service._video_previews, 'get') as call, self.assertRaises(ValueError):
                self.service.output(identifier, invalid)
            call.assert_not_called()

    def test_range_fallback_suffix_and_offline_are_bounded(self):
        identifier = self.prime_outputs([{'filename': 'video.webm', 'type': 'output'}])['outputs'][0]['id']
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / 'preview.mp4'
            preview.write_bytes(b'abcdefgh')
            with patch.object(self.service._video_previews, 'get', return_value=preview):
                self.assertEqual(self.service.output(identifier, 'bytes=2-4')[1], b'cde')
                self.assertEqual(self.service.output(identifier, 'bytes=-3')[1], b'fgh')
                self.assertEqual(self.service.output(identifier)[0], 'video/mp4')
                with self.assertRaises(ValueError):
                    self.service.output(identifier, 'bytes=8-')
        with patch.object(self.service, '_open', side_effect=URLError('offline')):
            self.assertFalse(self.service.comfy_status()['available'])
        with patch.object(self.service, '_open') as call, self.assertRaises(ValueError):
            self.service.output(identifier)
        call.assert_not_called()


class VideoPreviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.preview = media.ComfyVideoPreviews(self.directory.name, lambda *args, **kwargs: None)
        self.reference = ('video.mp4', 'safe', 'video', 'video/mp4')

    def test_identity_is_bounded_and_build_is_cached(self):
        source = Response(b'x', 206, {'Content-Range': 'bytes 0-0/4096', 'ETag': 'stable'})
        self.preview.fetch = lambda *args, **kwargs: source
        def build(query, size, target, extension):
            self.assertEqual(size, 4096)
            self.assertEqual(extension, '.mp4')
            target.write_bytes(b'x' * 256)
        with patch.object(self.preview, '_build', side_effect=build) as call:
            first = self.preview.get(self.reference)
            self.assertEqual(self.preview.get(self.reference), first)
            self.assertEqual(call.call_count, 1)
        self.assertTrue(first.is_relative_to(Path(self.directory.name).resolve()))
        for headers in [{'Content-Range': 'bytes 1-1/4096'}, {'Content-Range': 'bytes 0-0/' + str(media.MAX_SOURCE_BYTES + 1)}]:
            self.preview.fetch = lambda *args, **kwargs: Response(b'x', 206, headers)
            with self.assertRaises(ValueError):
                self.preview._identity('filename=video.mp4&type=output')

    def test_source_read_bounds_and_malformed_probe_are_sanitized(self):
        self.preview.fetch = lambda *args, **kwargs: Response(b'abc', 200, {'Content-Length': '3'})
        with self.assertRaises(ValueError):
            self.preview._download('filename=x', Path(self.directory.name) / 'source.mp4', 4)
        raw = {'streams': [{'codec_type': 'video', 'width': 'private-invalid', 'height': 1}], 'format': {'duration': 1}}
        with patch.object(self.preview, '_run', return_value=json.dumps(raw).encode()), self.assertRaises(ValueError) as error:
            self.preview._probe('ffprobe', Path('fixture.mp4'), 'mov')
        self.assertNotIn('private-invalid', str(error.exception))

    def test_conversion_is_fixed_bounded_and_rejects_truncated_preview(self):
        self.preview.cache.mkdir(parents=True)
        target = self.preview.cache / ('a' * 64 + '.mp4')
        def run(arguments, timeout):
            self.assertEqual(timeout, 45)
            self.assertIn('yuv420p', arguments)
            self.assertIn('baseline', arguments)
            self.assertIn('+faststart', arguments)
            self.assertEqual(arguments[arguments.index('-map_metadata') + 1], '-1')
            self.assertEqual(arguments[arguments.index('-threads') + 1], '2')
            self.assertNotIn('http', ' '.join(arguments))
            Path(arguments[-1]).write_bytes(b'x' * 256)
            return b''
        with patch.object(media.shutil, 'which', side_effect=lambda name: name), patch.object(self.preview, '_download'), \
                patch.object(self.preview, '_probe', side_effect=[(False, 15), (True, 15)]), patch.object(self.preview, '_run', side_effect=run):
            self.preview._build('filename=x', 4096, target, '.mp4')
        self.assertTrue(target.is_file())
        target.unlink()
        with patch.object(media.shutil, 'which', side_effect=lambda name: name), patch.object(self.preview, '_download'), \
                patch.object(self.preview, '_probe', side_effect=[(False, 15), (True, 10)]), patch.object(self.preview, '_run', side_effect=run), \
                self.assertRaises(ValueError):
            self.preview._build('filename=x', 4096, target, '.mp4')
        self.assertFalse(target.exists())


class CancellableChatTests(unittest.TestCase):
    def setUp(self):
        self.service = ai.AIService(Path(__file__).resolve().parent.parent)
        self.chat = {'model': 'exact-instance', 'messages': [{'role': 'user', 'content': 'Test text'}]}

    def test_cancel_closes_pending_catalog_post_headers_and_first_token_reads(self):
        for mode in ('catalog-stall', 'headers-stall', 'token-stall'):
            with self.subTest(mode=mode), fake_lm(mode) as server:
                base = 'http://127.0.0.1:' + str(server.server_port)
                cancellation, reader_done, received = threading.Event(), threading.Event(), []
                with patch.object(ai, 'LM_BASE', base), patch.object(self.service, '_open', side_effect=AssertionError('Blocking urllib path used.')):
                    stream = self.service.stream_chat_cancellable(self.chat, cancellation)

                    def consume():
                        try:
                            received.extend(events(stream))
                        finally:
                            reader_done.set()

                    reader = threading.Thread(target=consume, daemon=True)
                    reader.start()
                    try:
                        self.assertTrue(server.entered.wait(3), 'Fake upstream did not reach its pending request.')
                        started = time.monotonic()
                        cancellation.set()
                        self.assertTrue(server.closed.wait(1), 'Cancellation did not close the upstream socket promptly.')
                        self.assertTrue(reader_done.wait(1), 'The sync generator did not finish promptly.')
                        self.assertLess(time.monotonic() - started, 1)
                        self.assertEqual(received, [])
                    finally:
                        cancellation.set()
                        reader.join(timeout=2)
                self.assertEqual(server.post_count, 0 if mode == 'catalog-stall' else 1)

    def test_generator_close_at_a_yield_cancels_the_waiting_next_token(self):
        with fake_lm('partial') as server, patch.object(ai, 'LM_BASE', 'http://127.0.0.1:' + str(server.server_port)):
            cancellation = threading.Event()
            stream = self.service.stream_chat_cancellable(self.chat, cancellation)
            self.assertEqual(json.loads(next(stream).removeprefix('data: ').strip()), {'text': 'Hello'})
            started = time.monotonic()
            stream.close()
            self.assertTrue(cancellation.is_set())
            self.assertTrue(server.closed.wait(1))
            self.assertLess(time.monotonic() - started, 1)

    def test_cancellable_stream_keeps_exact_model_and_normalized_sse_contract(self):
        with fake_lm('complete') as server, patch.object(ai, 'LM_BASE', 'http://127.0.0.1:' + str(server.server_port)):
            result = events(self.service.stream_chat(self.chat, threading.Event()))
            self.assertEqual(result, [{'text': 'Hello'}, {'done': True}])
            self.assertEqual(server.received_model, 'exact-instance')
            self.assertTrue(server.closed.wait(1))

    def test_cancellable_vision_payload_is_sanitized_and_upstream_stops_on_close(self):
        for mode in ('vision-complete', 'vision-partial'):
            with self.subTest(mode=mode), fake_lm(mode) as server, patch.object(ai, 'LM_BASE', 'http://127.0.0.1:' + str(server.server_port)):
                cancellation = threading.Event()
                stream = self.service.stream_chat_cancellable(image_chat(image_url(metadata=True)), cancellation)
                self.assertEqual(json.loads(next(stream).removeprefix('data: ').strip()), {'text': 'Hello'})
                if mode == 'vision-partial':
                    stream.close()
                else:
                    self.assertEqual(events(stream), [{'done': True}])
                self.assertTrue(server.closed.wait(1))
                self.assertTrue(cancellation.is_set())
                body = server.received_body
                self.assertEqual(set(body), {'model', 'messages', 'stream'})
                self.assertEqual(body['model'], 'exact-instance')
                raw = base64.b64decode(body['messages'][0]['content'][1]['image_url']['url'].split(',', 1)[1])
                self.assertNotIn(b'private-image-metadata', raw)
        with fake_lm('complete') as server, patch.object(ai, 'LM_BASE', 'http://127.0.0.1:' + str(server.server_port)):
            self.assertIn('does not support images', events(self.service.stream_chat_cancellable(image_chat()))[0]['error'])
            self.assertEqual(server.post_count, 0)

    def test_cancellable_stream_refuses_unloaded_models_and_hides_malformed_data(self):
        for mode in ('unloaded', 'malformed'):
            with self.subTest(mode=mode), fake_lm(mode) as server, patch.object(ai, 'LM_BASE', 'http://127.0.0.1:' + str(server.server_port)):
                result = events(self.service.stream_chat_cancellable(self.chat, threading.Event()))
                self.assertIn('error', result[-1])
                self.assertNotIn('invalid-private-fixture', json.dumps(result))
                if mode == 'unloaded':
                    self.assertEqual(server.post_count, 0)


if __name__ == '__main__':
    unittest.main()
