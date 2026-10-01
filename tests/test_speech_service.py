"""Bounded WAV and offline dictation tests; no microphone, network or real model."""
import io
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave
import numpy as np

import speech_service as speech


def wav(frames=16000, value=1000, channels=1, width=2, rate=16000):
    output = io.BytesIO()
    with wave.open(output, 'wb') as target:
        target.setnchannels(channels)
        target.setsampwidth(width)
        target.setframerate(rate)
        target.writeframes(struct.pack('<h', value) * frames * channels)
    return output.getvalue()


class SpeechServiceTests(unittest.TestCase):
    def setUp(self):
        runtime = (Path(__file__).resolve().parents[1] / '.runtime').resolve()
        runtime.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='speech-test-', dir=runtime)
        self.cache = Path(self.temporary.name)
        self.service = speech.SpeechService(self.cache)
        self.network = patch('socket.create_connection', side_effect=AssertionError('No network in dictation tests'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(self.temporary.cleanup)

    def cached(self, model='base'):
        snapshot = self.cache / ('models--Systran--faster-whisper-' + model) / 'snapshots' / 'test-snapshot'
        snapshot.mkdir(parents=True)
        for name in ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt'):
            (snapshot / name).write_bytes(b'local-test-fixture')
        return snapshot

    def whisper(self, model=None):
        model = model or Mock()
        model.transcribe.return_value = (iter([SimpleNamespace(text=' Local speech ')]), SimpleNamespace(language='en'))
        constructor = Mock(return_value=model)
        return constructor, patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=constructor)})

    def test_wav_duration_and_size_bounds(self):
        self.assertEqual(len(speech.decode_audio(wav(frames=1600))), 3200)
        self.assertEqual(int(np.frombuffer(speech.decode_audio(wav(frames=1600)), dtype='<i2')[0]), 1000)
        self.assertEqual(len(speech.decode_audio(wav(frames=480000))), 960000)
        self.assertEqual(len(wav(frames=480000)), speech.MAX_AUDIO_BYTES)
        for data in (wav(frames=1599), wav(frames=480001), b'not-a-wav', bytearray(wav())):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                speech.decode_audio(data)

    def test_rejects_incorrect_pcm_format(self):
        for kwargs in ({'channels': 2}, {'width': 1}, {'rate': 48000}):
            with self.subTest(format=kwargs), self.assertRaisesRegex(ValueError, 'mono 16 kHz PCM'):
                speech.decode_audio(wav(**kwargs))

    def test_truncated_payload_and_headers(self):
        data = wav()
        for cut in (1, 1000, len(data) - 44):
            with self.subTest(cut=cut), self.assertRaises(ValueError):
                speech.decode_audio(data[:-cut])
        corrupt = bytearray(data)
        corrupt[:4] = b'FAIL'
        with self.assertRaises(ValueError):
            speech.decode_audio(bytes(corrupt))

    def test_missing_model_does_not_construct_or_download(self):
        constructor, module = self.whisper()
        with module, self.assertRaisesRegex(RuntimeError, 'unavailable'):
            self.service.transcribe(wav())
        constructor.assert_not_called()

    def test_partial_cache_is_unavailable(self):
        snapshot = self.cached()
        (snapshot / 'tokenizer.json').write_bytes(b'')
        self.assertIsNone(self.service.model_path())
        self.assertFalse(self.service.status()['available'])

    def test_busy_preserves_existing_lock_and_does_not_load(self):
        self.cached()
        constructor, module = self.whisper()
        self.assertTrue(self.service._lock.acquire(False))
        try:
            with module, self.assertRaisesRegex(RuntimeError, 'Another recording'):
                self.service.transcribe(wav())
            constructor.assert_not_called()
            self.assertTrue(self.service._lock.locked())
        finally:
            self.service._lock.release()

    def test_silence_does_not_initialize_model(self):
        self.cached()
        constructor, module = self.whisper()
        with module:
            result = self.service.transcribe(wav(value=0))
        self.assertTrue(result['ok'])
        self.assertEqual(result['text'], '')
        constructor.assert_not_called()
        self.assertFalse(self.service._lock.locked())

    def test_constructor_is_offline_and_model_is_reused(self):
        snapshot = self.cached()
        constructor, module = self.whisper()
        with module:
            result = self.service.transcribe(wav(), 'en')
            self.service.transcribe(wav(), 'en')
        constructor.assert_called_once_with(str(snapshot), device='cpu', compute_type='int8', cpu_threads=4,
                                            num_workers=1, local_files_only=True)
        self.assertEqual(result['text'], 'Local speech')
        self.assertEqual(result['language'], 'en')
        kwargs = constructor.return_value.transcribe.call_args.kwargs
        self.assertTrue(kwargs['vad_filter'])
        self.assertFalse(kwargs['condition_on_previous_text'])
        self.assertEqual(kwargs['beam_size'], 5)
        self.assertEqual(kwargs['hallucination_silence_threshold'], 2.0)

    def test_small_is_preferred_over_base_without_loading_medium(self):
        self.cached('base')
        small = self.cached('small')
        self.cached('medium')
        self.assertEqual(self.service.model_path(), small)
        self.assertEqual(self.service.status()['model'], 'Whisper small')
        constructor, module = self.whisper()
        with module:
            self.service.transcribe(wav(), 'sq')
        self.assertEqual(constructor.call_args.args[0], str(small))
        self.assertEqual(constructor.return_value.transcribe.call_args.kwargs['language'], 'sq')

    def test_incomplete_small_falls_back_to_complete_base(self):
        base = self.cached('base')
        small = self.cached('small')
        (small / 'model.bin').write_bytes(b'')
        self.assertEqual(self.service.model_path(), base)
        self.assertEqual(self.service.status()['model'], 'Whisper base')

    def test_newly_cached_small_replaces_loaded_base_once(self):
        self.cached('base')
        constructor, module = self.whisper()
        with module:
            self.service.transcribe(wav())
            small = self.cached('small')
            self.service.transcribe(wav())
            self.service.transcribe(wav())
        self.assertEqual(constructor.call_count, 2)
        self.assertEqual(constructor.call_args.args[0], str(small))

    def test_invalid_language_before_loading(self):
        constructor, module = self.whisper()
        with module, self.assertRaises(ValueError):
            self.service.transcribe(wav(), 'invalid')
        constructor.assert_not_called()

    def test_model_failure_releases_lock_for_retry(self):
        self.cached()
        constructor, module = self.whisper()
        constructor.side_effect = RuntimeError('fixture initialization failed')
        with module, self.assertRaises(RuntimeError):
            self.service.transcribe(wav())
        self.assertFalse(self.service._lock.locked())

    def test_iteration_failure_releases_lock(self):
        self.cached()
        constructor, module = self.whisper()
        def failing_segments():
            raise RuntimeError('fixture inference failed')
            yield
        constructor.return_value.transcribe.return_value = (failing_segments(), SimpleNamespace(language='en'))
        with module, self.assertRaises(RuntimeError):
            self.service.transcribe(wav())
        self.assertFalse(self.service._lock.locked())


if __name__ == '__main__':
    unittest.main()
