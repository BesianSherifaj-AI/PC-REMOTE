"""Bounded local browser-video derivatives; originals and Comfy queues stay untouched."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlencode

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_PREVIEW_BYTES = 16 * 1024 * 1024
MAX_CACHE_BYTES = 128 * 1024 * 1024
MAX_VIDEO_SECONDS = 120
VERSION = 'android-baseline-720-v1'


class ComfyVideoPreviews:
    def __init__(self, root, fetch):
        self.root = Path(root).resolve()
        self.cache = (self.root / '.runtime' / 'comfy-previews').resolve()
        if not self.cache.is_relative_to(self.root):
            raise ValueError('The media cache must stay inside this workspace.')
        self.fetch = fetch
        self._lock = threading.Lock()
        self._recent = {}

    @staticmethod
    def _run(arguments, timeout):
        result = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=timeout, check=False,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode or len(result.stdout) > 256 * 1024:
            raise ValueError('Video preview conversion failed on the PC.')
        return result.stdout

    def _identity(self, query):
        with self.fetch('/view?' + query, headers={'Range': 'bytes=0-0'}) as response:
            status, length = response.getcode(), response.headers.get('Content-Length', '')
            if status == 206:
                match = re.fullmatch(r'bytes 0-0/(\d+)', response.headers.get('Content-Range', ''))
                if not match or len(response.read(2)) != 1:
                    raise ValueError('Invalid ComfyUI source range.')
                size = int(match[1])
            elif status == 200 and length.isdigit():
                size = int(length)
            else:
                raise ValueError('ComfyUI did not provide bounded video metadata.')
            if not 1 <= size <= MAX_SOURCE_BYTES:
                raise ValueError('This video exceeds the 64 MB source preview limit.')
            validator = response.headers.get('ETag') or response.headers.get('Last-Modified') or ''
            key = hashlib.sha256((VERSION + query + str(size) + validator[:256]).encode('utf-8')).hexdigest()
            return key, size

    def _download(self, query, source, expected_size):
        started, written = time.monotonic(), 0
        with self.fetch('/view?' + query) as response, source.open('xb') as file:
            if response.getcode() != 200:
                raise ValueError('ComfyUI did not provide the complete source video.')
            length = response.headers.get('Content-Length', '')
            if length and (not length.isdigit() or int(length) > MAX_SOURCE_BYTES):
                raise ValueError('This video exceeds the source preview limit.')
            while True:
                if time.monotonic() - started > 30:
                    raise ValueError('The video source took too long to read.')
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_SOURCE_BYTES:
                    raise ValueError('This video exceeds the source preview limit.')
                file.write(chunk)
        if written != expected_size:
            raise ValueError('The ComfyUI video changed while preparing its preview. Retry playback.')

    def _probe(self, ffprobe, source, input_format):
        raw = self._run([ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe', '-f', input_format,
                         '-show_entries', 'stream=codec_type,codec_name,profile,pix_fmt,width,height,level,channels,avg_frame_rate:format=duration',
                         '-of', 'json', str(source)], 10)
        data = json.loads(raw)
        streams = data.get('streams', []) if isinstance(data, dict) else None
        if not isinstance(streams, list) or len(streams) > 64 or any(not isinstance(item, dict) for item in streams):
            raise ValueError('Invalid video stream metadata.')
        video = next((item for item in streams if item.get('codec_type') == 'video'), None)
        if not video:
            raise ValueError('This output has no playable video stream.')
        try:
            duration = float(data.get('format', {}).get('duration', 0))
            width, height, level = int(video.get('width', 0)), int(video.get('height', 0)), int(video.get('level', 999))
            numerator, denominator = (int(value) for value in str(video.get('avg_frame_rate', '0/1')).split('/'))
            fps = numerator / denominator
            channels = [int(item.get('channels', 999)) for item in streams if item.get('codec_type') == 'audio']
        except (ValueError, TypeError, AttributeError, ZeroDivisionError):
            raise ValueError('Invalid video stream metadata.') from None
        pixels = width * height
        if not 0 < duration <= MAX_VIDEO_SECONDS or not 0 < pixels <= 16000000:
            raise ValueError('Video previews support clips up to 120 seconds and 16 million pixels.')
        compatible = (video.get('codec_name') == 'h264' and video.get('pix_fmt') == 'yuv420p'
                      and video.get('profile') in ('Baseline', 'Constrained Baseline', 'Main')
                      and level <= 31 and 0 < fps <= 30 and max(width, height) <= 720
                      and all(item.get('codec_name') == 'aac' and item.get('profile') == 'LC'
                              for item in streams if item.get('codec_type') == 'audio')
                      and all(1 <= count <= 2 for count in channels))
        return compatible, duration

    def _build(self, query, expected_size, target, extension):
        ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        if not ffmpeg or not ffprobe:
            raise ValueError('Video previews need the installed FFmpeg tools on the PC.')
        with tempfile.TemporaryDirectory(prefix='build-', dir=self.cache) as directory:
            work = Path(directory).resolve()
            if not work.is_relative_to(self.cache):
                raise ValueError('Unsafe video work directory.')
            source, result = work / ('source' + extension), work / 'preview.mp4'
            self._download(query, source, expected_size)
            input_format = 'mov' if extension == '.mp4' else 'matroska'
            compatible, duration = self._probe(ffprobe, source, input_format)
            arguments = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                         '-protocol_whitelist', 'file,pipe', '-threads', '2', '-filter_threads', '1',
                         '-f', input_format, '-i', str(source), '-map', '0:v:0', '-map', '0:a:0?',
                         '-map_metadata', '-1', '-map_chapters', '-1', '-sn', '-dn']
            if compatible and expected_size < MAX_PREVIEW_BYTES:
                arguments += ['-c', 'copy']
            else:
                arguments += ['-vf', "scale=w='min(720,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
                              '-c:v', 'libx264', '-profile:v', 'baseline', '-level:v', '3.1', '-pix_fmt', 'yuv420p',
                              '-preset', 'veryfast', '-crf', '23', '-maxrate', '1M', '-bufsize', '2M',
                              '-threads', '2', '-r', '30', '-c:a', 'aac', '-b:a', '96k', '-ar', '48000', '-ac', '2']
            arguments += ['-movflags', '+faststart', '-fs', str(MAX_PREVIEW_BYTES), '-f', 'mp4', str(result)]
            self._run(arguments, 45)
            if not result.is_file() or not 128 <= result.stat().st_size < MAX_PREVIEW_BYTES:
                raise ValueError('This video exceeds the browser preview size limit.')
            result_compatible, result_duration = self._probe(ffprobe, result, 'mov')
            if not result_compatible or not math.isclose(duration, result_duration, abs_tol=0.25, rel_tol=0.01):
                raise ValueError('The PC could not prepare a complete compatible video preview.')
            result.replace(target)

    def _prune(self, keep):
        files = [file for file in self.cache.glob('*.mp4')
                 if re.fullmatch(r'[0-9a-f]{64}\.mp4', file.name) and not file.is_symlink()]
        files.sort(key=lambda file: file.stat().st_mtime, reverse=True)
        total = 0
        for index, file in enumerate(files):
            total += file.stat().st_size
            if file != keep and (index >= 8 or total > MAX_CACHE_BYTES):
                try:
                    file.unlink()
                except OSError:
                    pass

    def get(self, reference):
        filename, subfolder, kind, _ = reference
        extension = Path(filename).suffix.lower()
        if kind != 'video' or extension not in ('.mp4', '.webm'):
            raise ValueError('Unknown source video type.')
        if not self._lock.acquire(timeout=20):
            raise ValueError('A video preview is being prepared. Retry playback shortly.')
        try:
            recent = self._recent.get(reference)
            if (recent and time.monotonic() - recent[1] < 30 and recent[0].is_file()
                    and not recent[0].is_symlink() and 128 <= recent[0].stat().st_size < MAX_PREVIEW_BYTES):
                return recent[0]
            self.cache.mkdir(parents=True, exist_ok=True)
            query = urlencode({'filename': filename, 'subfolder': subfolder, 'type': 'output'})
            key, size = self._identity(query)
            target = self.cache / (key + '.mp4')
            if target.is_symlink():
                raise ValueError('Unsafe media cache entry.')
            if not target.is_file() or not 128 <= target.stat().st_size < MAX_PREVIEW_BYTES:
                self._build(query, size, target, extension)
            target.touch()
            self._recent[reference] = (target, time.monotonic())
            self._prune(target)
            return target
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError):
            raise ValueError('The PC could not prepare this video preview. Retry playback.') from None
        finally:
            self._lock.release()
