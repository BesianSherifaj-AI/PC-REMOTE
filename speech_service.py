"""Short, local dictation using the best bounded cached Whisper model. No downloads."""
import importlib.util
import io
from pathlib import Path
import threading
import wave

MAX_AUDIO_BYTES = 960044  # 30 seconds of mono PCM16 at 16 kHz, plus WAV header
LANGUAGES = {'auto', 'en', 'el', 'sq', 'de', 'it'}


def decode_audio(data):
    if not isinstance(data, bytes) or not 44 <= len(data) <= MAX_AUDIO_BYTES:
        raise ValueError('Record between one and 30 seconds.')
    try:
        with wave.open(io.BytesIO(data), 'rb') as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (1, 2, 16000, 'NONE'):
                raise ValueError('Use mono 16 kHz PCM audio.')
            frames = source.getnframes()
            if not 1600 <= frames <= 480000:
                raise ValueError('Record between one and 30 seconds.')
            raw = source.readframes(frames)
            if len(raw) != frames * 2:
                raise ValueError('The recording is incomplete.')
            return raw
    except (wave.Error, EOFError) as error:
        raise ValueError('The recording is not a valid WAV file.') from error


class SpeechService:
    def __init__(self, cache=None):
        self.cache = Path(cache) if cache else Path.home() / '.cache/huggingface/hub'
        self._lock = threading.Lock()
        self._model = None
        self._model_location = None

    def model_path(self):
        # Fixed speech model, separate from the user's selected LM Studio model.
        # Small improves multilingual recognition; do not select medium/large
        # models that would compete with the user's LM Studio/Comfy workloads.
        for model in ('small', 'base'):
            for path in sorted((self.cache / ('models--Systran--faster-whisper-' + model) / 'snapshots').glob('*')):
                if all((path / name).is_file() and (path / name).stat().st_size > 0
                       for name in ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt')):
                    return path
        return None

    def status(self):
        path = self.model_path()
        model = 'Whisper small' if path and 'faster-whisper-small' in str(path.parent.parent) else 'Whisper base'
        available = path is not None and importlib.util.find_spec('faster_whisper') is not None
        return {'ok': True, 'available': available, 'model': model, 'maxSeconds': 30,
                'message': f'Dictation runs locally on this PC with {model}, four CPU threads and improved decoding. Nothing is downloaded.' if available else
                'Local dictation needs a cached Whisper small/base model and faster-whisper package.'}

    def transcribe(self, data, language='auto'):
        if language not in LANGUAGES:
            raise ValueError('Choose a supported speech language.')
        pcm = decode_audio(data)
        path = self.model_path()
        if path is None:
            raise RuntimeError('The local speech model is unavailable. Use typed input.')
        if not self._lock.acquire(blocking=False):
            raise RuntimeError('Another recording is being transcribed. Try again shortly.')
        try:
            import numpy as np
            samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
            if np.max(np.abs(samples)) < 0.003:
                return {'ok': True, 'text': '', 'message': 'No speech heard. Check the MAIC microphone.'}
            from faster_whisper import WhisperModel
            if self._model is None or self._model_location != path:
                self._model = WhisperModel(str(path), device='cpu', compute_type='int8',
                                          cpu_threads=4, num_workers=1, local_files_only=True)
                self._model_location = path
            segments, info = self._model.transcribe(samples, language=None if language == 'auto' else language,
                                                   beam_size=5, vad_filter=True,
                                                   condition_on_previous_text=False,
                                                   hallucination_silence_threshold=2.0)
            text = ' '.join(segment.text.strip() for segment in segments).strip()
            return {'ok': True, 'text': text[:16000], 'language': info.language,
                    'message': 'Review the text, then press Send.' if text else 'No speech heard. Try again.'}
        finally:
            self._lock.release()
