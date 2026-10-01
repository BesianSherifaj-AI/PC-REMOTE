"""Private, CPU-only neural speech. Fixed local assets; no runtime downloads.

The isolated worker keeps speech dependencies out of the dashboard process.
User text/audio exists only in pipes and memory, never in logs or temp files.
"""
import base64
import binascii
import hashlib
import io
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import wave

MAX_CHARS = 3000
MAX_SECONDS = 120
SAMPLE_RATE = 24000
MAX_WAV_BYTES = SAMPLE_RATE * 2 * MAX_SECONDS + 44
MAX_REPLY_BYTES = ((MAX_WAV_BYTES + 2) // 3) * 4 + 1024
DEFAULT_VOICE = 'af_heart'
VOICES = (
    {'id': 'af_heart', 'name': 'Heart · American English', 'language': 'en-US'},
    {'id': 'af_bella', 'name': 'Bella · American English', 'language': 'en-US'},
    {'id': 'am_michael', 'name': 'Michael · American English', 'language': 'en-US'},
    {'id': 'bf_emma', 'name': 'Emma · British English', 'language': 'en-GB'},
    {'id': 'bm_george', 'name': 'George · British English', 'language': 'en-GB'},
    {'id': 'if_sara', 'name': 'Sara · Italian', 'language': 'it'},
    {'id': 'im_nicola', 'name': 'Nicola · Italian', 'language': 'it'},
    {'id': 'ef_dora', 'name': 'Dora · Spanish', 'language': 'es'},
    {'id': 'ff_siwis', 'name': 'Siwis · French', 'language': 'fr-fr'},
    {'id': 'pf_dora', 'name': 'Dora · Brazilian Portuguese', 'language': 'pt-br'},
)
VOICE_LANGUAGES = {voice['id']: voice['language'].lower() for voice in VOICES}
ASSETS = {
    'kokoro-v1.0.fp16.onnx': ('f3a290d384fbb27966d462905c71a46cef9e5fd00516b40df32a0b4afe77ac96', 163527961),
    'voices-v1.0.bin': ('bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d', 28214398),
}


def validate_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError('Choose text and a PC voice.')
    text = payload.get('text')
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_CHARS:
        raise ValueError('Read between one and 3000 characters at a time.')
    voice = payload.get('voice', DEFAULT_VOICE)
    if not isinstance(voice, str) or voice not in VOICE_LANGUAGES:
        raise ValueError('Choose one of the available PC voices.')
    speed = payload.get('speed', 1.0)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or not 0.8 <= speed <= 1.25:
        raise ValueError('Choose a speech speed between 0.8 and 1.25.')
    # Read prose naturally rather than vocalizing Markdown formatting and URLs.
    text = re.sub(r'```[\s\S]*?```', ' Code omitted. ', text)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'https?://\S+', ' link ', text)
    text = re.sub(r'(^|\n)\s{0,3}(?:#{1,6}\s+|[-*+]\s+)', r'\1', text)
    text = re.sub(r'[`*_]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if not text or not any(character.isalnum() for character in text):
        raise ValueError('No readable text was supplied.')
    return {'text': text, 'voice': voice, 'speed': float(speed)}


def validate_wav(data):
    if not isinstance(data, bytes) or not 46 <= len(data) <= MAX_WAV_BYTES:
        raise RuntimeError('The PC voice returned invalid audio.')
    try:
        with wave.open(io.BytesIO(data), 'rb') as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (1, 2, SAMPLE_RATE, 'NONE'):
                raise RuntimeError('The PC voice returned invalid audio.')
            frames = source.getnframes()
            if not 1 <= frames <= SAMPLE_RATE * MAX_SECONDS or len(source.readframes(frames)) != frames * 2:
                raise RuntimeError('The PC voice returned incomplete audio.')
    except (wave.Error, EOFError) as error:
        raise RuntimeError('The PC voice returned invalid audio.') from error
    return data


class TTSService:
    def __init__(self, root=None, timeout=90):
        self.root = Path(root) if root else Path(__file__).resolve().parent
        self.assets = self.root / 'tools' / 'speech'
        self.python = self.assets / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        self.timeout = timeout
        self._lock = threading.Lock()
        self._worker_guard = threading.RLock()
        self._process = None
        self._replies = None
        self._closed = False

    def status(self):
        available = self.python.is_file() and all((self.assets / name).is_file() and
            (self.assets / name).stat().st_size == size for name, (_, size) in ASSETS.items())
        return {'ok': True, 'available': available and not self._closed, 'model': 'Kokoro-82M v1.0 FP16',
                'voices': [dict(voice) for voice in VOICES], 'defaultVoice': DEFAULT_VOICE,
                'maxChars': MAX_CHARS, 'maxSeconds': MAX_SECONDS, 'busy': self._lock.locked(),
                'message': 'Natural PC voices run locally on four CPU threads. Albanian, Greek and German voices are not included.'
                if available else 'The local PC voice model is unavailable. Run the speech setup on this PC.'}

    @staticmethod
    def _read_replies(process, replies):
        try:
            while True:
                line = process.stdout.readline(MAX_REPLY_BYTES + 1)
                if not line or len(line) > MAX_REPLY_BYTES or not line.endswith(b'\n'):
                    break
                replies.put(line)
        finally:
            replies.put(None)

    def _start(self):
        with self._worker_guard:
            if self._closed:
                raise RuntimeError('The PC speech service has stopped.')
            if self._process is not None and self._process.poll() is None:
                return self._process, self._replies
            self._stop_worker()
            if not self.status()['available']:
                raise RuntimeError('The local PC voice model is unavailable.')
            environment = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                               OMP_NUM_THREADS='4', TOKENIZERS_PARALLELISM='false', LOG_LEVEL='CRITICAL')
            process = subprocess.Popen([str(self.python), '-X', 'utf8', str(Path(__file__).resolve()),
                                        '--worker', str(self.assets)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, cwd=str(self.root), env=environment,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            replies = queue.Queue(maxsize=2)
            self._process, self._replies = process, replies
            threading.Thread(target=self._read_replies, args=(process, replies), daemon=True).start()
            return process, replies

    def _stop_worker(self):
        with self._worker_guard:
            process, self._process = self._process, None
            self._replies = None
            if process is not None:
                if process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                for pipe in (process.stdin, process.stdout):
                    if pipe:
                        pipe.close()

    def synthesize(self, payload, cancel_event=None):
        request = validate_payload(payload)
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError('PC speech was cancelled.')
        if not self._lock.acquire(blocking=False):
            raise RuntimeError('Another reply is being spoken. Try again shortly.')
        try:
            process, replies = self._start()
            process.stdin.write((json.dumps(request, ensure_ascii=True) + '\n').encode('utf-8'))
            process.stdin.flush()
            deadline = time.monotonic() + self.timeout
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError('PC speech was cancelled.')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty()
                try:
                    line = replies.get(timeout=min(remaining, 0.05))
                    break
                except queue.Empty:
                    continue
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError('PC speech was cancelled.')
            if line is None:
                raise RuntimeError('The local PC voice could not generate audio. Try again.')
            response = json.loads(line)
            if not isinstance(response, dict) or not response.get('ok'):
                if isinstance(response, dict) and response.get('error') == 'duration':
                    raise ValueError('This reply exceeds two minutes. Read a shorter part at a time.')
                raise RuntimeError('The local PC voice could not generate audio. Try another voice.')
            return validate_wav(base64.b64decode(response['audio'], validate=True))
        except (json.JSONDecodeError, binascii.Error) as error:
            self._stop_worker()
            raise RuntimeError('The local PC voice returned invalid audio. Try again.') from error
        except ValueError:
            raise
        except queue.Empty as error:
            self._stop_worker()
            raise RuntimeError('PC speech took too long. Try a shorter reply.') from error
        except (OSError, KeyError, TypeError) as error:
            self._stop_worker()
            raise RuntimeError('The local PC voice could not generate audio. Try again.') from error
        except RuntimeError:
            self._stop_worker()
            raise
        finally:
            self._lock.release()

    def synthesize_cancellable(self, payload, cancel_event):
        return self.synthesize(payload, cancel_event=cancel_event)

    def close(self):
        with self._worker_guard:
            self._closed = True
            self._stop_worker()


def _load_engine(assets):
    import logging
    logging.disable(logging.CRITICAL)
    for name, (expected, size) in ASSETS.items():
        path = assets / name
        if not path.is_file() or path.stat().st_size != size:
            raise RuntimeError('Speech assets are missing.')
        with path.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != expected:
                raise RuntimeError('Speech asset verification failed.')
    import onnxruntime as ort
    from kokoro_onnx import Kokoro
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session = ort.InferenceSession(str(assets / 'kokoro-v1.0.fp16.onnx'), sess_options=options,
                                  providers=['CPUExecutionProvider'])
    return Kokoro.from_session(session, str(assets / 'voices-v1.0.bin'))


def _render(engine, payload):
    import numpy as np
    request = validate_payload(payload)
    samples, rate = engine.create(request['text'], voice=request['voice'], speed=request['speed'],
                                  lang=VOICE_LANGUAGES[request['voice']])
    samples = np.asarray(samples).reshape(-1)
    if rate != SAMPLE_RATE or not samples.size or not np.isfinite(samples).all():
        raise RuntimeError('Invalid generated speech.')
    if samples.size > SAMPLE_RATE * MAX_SECONDS:
        raise ValueError('duration')
    output = io.BytesIO()
    with wave.open(output, 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes((np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes())
    return validate_wav(output.getvalue())


def _worker(assets):
    engine = None
    while line := sys.stdin.buffer.readline(32000):
        if not line.endswith(b'\n'):
            return
        try:
            payload = json.loads(line)
            validate_payload(payload)
            if engine is None:
                engine = _load_engine(assets)
            audio = _render(engine, payload)
            response = {'ok': True, 'audio': base64.b64encode(audio).decode('ascii')}
        except Exception as error:
            # Some synthesis exceptions embed the original text/phonemes.
            # Never return or log library exceptions or user content.
            response = {'ok': False, 'error': 'duration' if isinstance(error, ValueError) and str(error) == 'duration' else 'synthesis'}
        try:
            sys.stdout.buffer.write((json.dumps(response) + '\n').encode('ascii'))
            sys.stdout.buffer.flush()
        except (BrokenPipeError, OSError):
            return


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == '--worker':
    _worker(Path(sys.argv[2]))
