"""Bounded offline speech tests. No actual microphone, network or model loading."""
import base64
import hashlib
import io
import json
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

import numpy as np
import pc_tts as tts


def wav(frames=2400, rate=24000):
    target = io.BytesIO()
    with wave.open(target, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b'\x01\x00' * frames)
    return target.getvalue()


class TTSServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='maic-tts-')
        self.addCleanup(self.temporary.cleanup)
        self.service = tts.TTSService(self.temporary.name, timeout=0.01)
        self.network = patch('socket.create_connection', side_effect=AssertionError('No network in TTS tests'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def response(self, response=None):
        process = SimpleNamespace(stdin=io.BytesIO())
        replies = queue.Queue()
        if response is not None:
            replies.put(response if isinstance(response, bytes) else json.dumps(response).encode('ascii') + b'\n')
        return process, patch.object(self.service, '_start', return_value=(process, replies))

    def test_payload_bounds_and_speed_rejection_before_process(self):
        invalid = [None, [], {}, {'text': ''}, {'text': ' '}, {'text': 3}, {'text': 'x' * 3001},
                   {'text': 'Hello', 'voice': '../voice'}, {'text': 'Hello', 'voice': ['af_heart']},
                   {'text': 'Hello', 'speed': True}, {'text': 'Hello', 'speed': '1'},
                   {'text': 'Hello', 'speed': float('nan')}, {'text': 'Hello', 'speed': float('inf')},
                   {'text': 'Hello', 'speed': 0.79}, {'text': 'Hello', 'speed': 1.26}, {'text': '***'}]
        with patch.object(self.service, '_start') as start:
            for payload in invalid:
                with self.subTest(payload=type(payload).__name__), self.assertRaises(ValueError):
                    self.service.synthesize(payload)
        start.assert_not_called()
        self.assertFalse(self.service._lock.locked())
        self.assertEqual(len(tts.validate_payload({'text': 'x' * 3000})['text']), 3000)

    def test_markdown_is_cleaned_without_reading_url_or_code(self):
        result = tts.validate_payload({'text': '# Hello\n- **world** [Docs](https://example.com) ```secret code```'})
        self.assertEqual(result['text'], 'Hello world Docs Code omitted.')
        self.assertEqual(result['voice'], 'af_heart')
        self.assertEqual(result['speed'], 1.0)

    def test_unavailable_status_does_not_load_model(self):
        with patch('subprocess.Popen') as start:
            result = self.service.status()
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                self.service.synthesize({'text': 'Hello'})
        self.assertFalse(result['available'])
        self.assertEqual(result['maxChars'], 3000)
        self.assertEqual(result['defaultVoice'], 'af_heart')
        self.assertTrue({'sq', 'el', 'de'}.isdisjoint({item['language'] for item in result['voices']}))
        start.assert_not_called()

    def test_busy_does_not_replace_existing_lock_or_start_worker(self):
        self.service._lock.acquire()
        try:
            with patch.object(self.service, '_start') as start, self.assertRaisesRegex(RuntimeError, 'Another reply'):
                self.service.synthesize({'text': 'Hello'})
            start.assert_not_called()
            self.assertTrue(self.service._lock.locked())
        finally:
            self.service._lock.release()

    def test_success_returns_bounded_wav_and_text_is_only_in_pipe(self):
        audio = wav()
        process, start = self.response({'ok': True, 'audio': base64.b64encode(audio).decode('ascii')})
        with start, patch('builtins.print') as output:
            result = self.service.synthesize({'text': 'Private test phrase', 'voice': 'if_sara', 'speed': 0.9})
        self.assertEqual(result, audio)
        request = json.loads(process.stdin.getvalue())
        self.assertEqual(request['voice'], 'if_sara')
        self.assertEqual(request['text'], 'Private test phrase')
        output.assert_not_called()
        self.assertFalse(self.service._lock.locked())

    def test_timeout_stops_owned_worker_and_releases_lock(self):
        process, start = self.response()
        with start, patch.object(self.service, '_stop_worker') as stop, self.assertRaisesRegex(RuntimeError, 'too long'):
            self.service.synthesize({'text': 'Hello'})
        stop.assert_called_once()
        self.assertFalse(self.service._lock.locked())

    def test_pre_cancelled_request_does_not_touch_running_worker(self):
        cancellation = threading.Event()
        cancellation.set()
        with patch.object(self.service, '_start') as start, patch.object(self.service, '_stop_worker') as stop, self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.service.synthesize({'text': 'Hello'}, cancel_event=cancellation)
        start.assert_not_called()
        stop.assert_not_called()

    def test_cancellation_stops_only_lock_owned_worker_and_next_request_works(self):
        self.service.timeout = 2
        _, start = self.response()
        cancellation = threading.Event()
        timer = threading.Timer(0.02, cancellation.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with start, patch.object(self.service, '_stop_worker') as stop, self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.service.synthesize_cancellable({'text': 'Hello'}, cancellation)
        stop.assert_called_once()
        self.assertFalse(self.service._lock.locked())
        _, start = self.response({'ok': True, 'audio': base64.b64encode(wav()).decode('ascii')})
        with start:
            self.assertEqual(self.service.synthesize({'text': 'Fresh request'}), wav())

    def test_cancelled_competing_request_cannot_stop_active_worker(self):
        cancellation = threading.Event()
        self.service._lock.acquire()
        try:
            with patch.object(self.service, '_stop_worker') as stop, self.assertRaisesRegex(RuntimeError, 'Another reply'):
                self.service.synthesize({'text': 'Hello'}, cancel_event=cancellation)
            cancellation.set()
            stop.assert_not_called()
            self.assertTrue(self.service._lock.locked())
        finally:
            self.service._lock.release()

    def test_corrupt_worker_reply_is_sanitized_and_worker_reset(self):
        for response in (b'not json\n', {'ok': True, 'audio': '!!!'}, {'ok': True}, {'ok': True, 'audio': base64.b64encode(b'not wav').decode('ascii')}, None):
            process, start = self.response(response)
            with self.subTest(response=type(response).__name__), start, patch.object(self.service, '_stop_worker') as stop, self.assertRaises(RuntimeError):
                self.service.synthesize({'text': 'Hello'})
            stop.assert_called_once()
            self.assertFalse(self.service._lock.locked())

    def test_duration_error_remains_actionable_and_releases_lock(self):
        _, start = self.response({'ok': False, 'error': 'duration'})
        with start, self.assertRaisesRegex(ValueError, 'two minutes'):
            self.service.synthesize({'text': 'Hello'})
        self.assertFalse(self.service._lock.locked())

    def test_generated_pcm_clips_and_has_correct_mono_header(self):
        engine = Mock()
        engine.create.return_value = (np.array([-2.0, -0.5, 0.0, 0.5, 2.0], dtype=np.float32), 24000)
        data = tts._render(engine, {'text': 'Ciao mondo', 'voice': 'if_sara'})
        with wave.open(io.BytesIO(data), 'rb') as source:
            self.assertEqual((source.getnchannels(), source.getsampwidth(), source.getframerate()), (1, 2, 24000))
            self.assertEqual(np.frombuffer(source.readframes(5), dtype='<i2').tolist(), [-32767, -16383, 0, 16383, 32767])
        self.assertEqual(engine.create.call_args.kwargs['lang'], 'it')

    def test_invalid_generated_audio_and_excess_duration_are_rejected(self):
        engine = Mock()
        for audio, rate in ((np.array([]), 24000), (np.array([np.nan]), 24000), (np.array([0.1]), 16000)):
            engine.create.return_value = (audio, rate)
            with self.assertRaises(RuntimeError):
                tts._render(engine, {'text': 'Hello'})
        engine.create.return_value = (np.zeros(24000 * 120 + 1, dtype=np.float32), 24000)
        with self.assertRaisesRegex(ValueError, 'duration'):
            tts._render(engine, {'text': 'Hello'})

    def test_wav_rejects_wrong_rate_truncation_and_oversize(self):
        for data in (wav(rate=16000), wav()[:-2], b'x' * (tts.MAX_WAV_BYTES + 1), b'not wav'):
            with self.subTest(size=len(data)), self.assertRaises(RuntimeError):
                tts.validate_wav(data)

    def test_checksum_failure_prevents_any_model_loading(self):
        assets = Path(self.temporary.name)
        (assets / 'model.onnx').write_bytes(b'fixture')
        with patch.dict(tts.ASSETS, {'model.onnx': ('0' * 64, 7)}, clear=True), self.assertRaisesRegex(RuntimeError, 'verification'):
            tts._load_engine(assets)

    def test_worker_never_exposes_exception_text(self):
        source = SimpleNamespace(buffer=io.BytesIO(b'{"text":"A private phrase"}\n'))
        target = SimpleNamespace(buffer=io.BytesIO())
        with patch.object(tts.sys, 'stdin', source), patch.object(tts.sys, 'stdout', target), \
             patch.object(tts, '_load_engine', side_effect=RuntimeError('A private phrase embedded by a library')):
            tts._worker(Path(self.temporary.name))
        response = json.loads(target.buffer.getvalue())
        self.assertEqual(response, {'ok': False, 'error': 'synthesis'})
        self.assertNotIn(b'private', target.buffer.getvalue())

    def test_close_marks_unavailable_and_stops_worker(self):
        with patch.object(self.service, '_stop_worker') as stop:
            self.service.close()
        stop.assert_called_once()
        self.assertTrue(self.service._closed)
        self.assertFalse(self.service.status()['available'])


class TTSCancellationHTTPTests(unittest.TestCase):
    def test_browser_disconnect_cancels_synthesis_before_response_headers(self):
        import ipaddress
        import maic_server
        class QuietHandler(maic_server.Handler):
            def log_message(self, *_):
                pass
        class WaitingSpeech:
            def __init__(self):
                self.started = threading.Event()
                self.cancelled = threading.Event()
            def synthesize_cancellable(self, payload, cancellation):
                self.started.set()
                if not cancellation.wait(2):
                    raise AssertionError('Browser disconnect did not cancel speech.')
                self.cancelled.set()
                raise RuntimeError('PC speech was cancelled.')
        service = WaitingSpeech()
        server = maic_server.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        server.allowed_network = ipaddress.ip_network('127.0.0.0/8')
        server.expected_host = '127.0.0.1:' + str(server.server_address[1])
        server.origin = 'http://' + server.expected_host
        server.control_lock = threading.RLock()
        server.control_sessions = {'127.0.0.1': 'test-session'}
        server.tts_service = service
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        connection = None
        try:
            connection = socket.create_connection(server.server_address, timeout=2)
            body = b'{"text":"Synthetic test only"}'
            request = ('POST /api/tts/speak HTTP/1.1\r\nHost: ' + server.expected_host +
                       '\r\nX-MAIC-Control: test-session\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(body)) + '\r\n\r\n').encode('ascii')
            connection.sendall(request + body)
            self.assertTrue(service.started.wait(1))
            connection.close()
            connection = None
            self.assertTrue(service.cancelled.wait(1), 'Closing the browser socket must cancel the active speech task promptly')
        finally:
            if connection:
                connection.close()
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


class DictationSetupTests(unittest.TestCase):
    def test_read_only_check_refuses_missing_or_corrupt_cache_without_network(self):
        from tools import setup_dictation as setup
        with tempfile.TemporaryDirectory(prefix='maic-dictation-check-') as temporary:
            cache = Path(temporary)
            fixtures = {'model.bin': (hashlib.sha256(b'fixture').hexdigest(), 7)}
            with patch.dict(setup.ASSETS, fixtures, clear=True), \
                 patch.object(setup.urllib.request, 'urlopen', side_effect=AssertionError('Check must stay offline')), \
                 patch.object(setup.subprocess, 'run', side_effect=AssertionError('Check cannot install packages')), \
                 patch.object(setup.importlib.util, 'find_spec', return_value=object()):
                self.assertFalse(setup.check(cache)['available'])
                directory = setup.snapshot(cache)
                directory.mkdir(parents=True)
                (directory / 'model.bin').write_bytes(b'fixture')
                self.assertTrue(setup.check(cache)['available'])
                (directory / 'model.bin').write_bytes(b'corrupt')
                self.assertFalse(setup.check(cache)['modelAvailable'])

    def test_dictation_installer_refuses_global_python_before_any_mutation(self):
        from tools import setup_dictation as setup
        with patch.object(setup.sys, 'argv', ['setup_dictation.py']), \
             patch.object(setup.sys, 'prefix', 'global-python'), \
             patch.object(setup.sys, 'base_prefix', 'global-python'), \
             patch.object(setup.subprocess, 'run') as install, \
             patch.object(setup.urllib.request, 'urlopen') as download, \
             self.assertRaisesRegex(RuntimeError, 'Global Python is left unchanged'):
            setup.main()
        install.assert_not_called()
        download.assert_not_called()


if __name__ == '__main__':
    unittest.main()
