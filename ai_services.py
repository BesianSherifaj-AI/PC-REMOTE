"""Bounded loopback AI clients. Explicit model loads; no chat storage or Comfy writes."""
import asyncio
import base64
import binascii
import io
import hmac
import json
import os
import queue
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from media_previews import ComfyVideoPreviews

LM_BASE = 'http://127.0.0.1:1234'
COMFY_BASE = 'http://127.0.0.1:8010'
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_RANGE_BYTES = 4 * 1024 * 1024
MAX_STREAM_BYTES = 1024 * 1024
MAX_EVENT_BYTES = 64 * 1024
MAX_CHAT_CHARS = 32768
MAX_CHAT_REQUEST_BYTES = 9 * 1024 * 1024
MAX_CHAT_IMAGES = 3
MAX_CHAT_IMAGE_BYTES = 2 * 1024 * 1024
MAX_CHAT_IMAGE_PIXELS = 16000000
MAX_CHAT_IMAGE_SIDE = 8192
CHAT_IMAGE_PREVIEW_SIDE = 1536
FAST_MODEL_CHOICES = (
    ('qwen3.5-0.8b@q8_0', 'Qwen3.5 0.8B Q8', True,
     'Compact vision model for quick conversations and image questions.'),
    ('qwen_qwen3.5-2b', 'Qwen3.5 2B', True,
     'Small vision model with more capacity for images and short conversations.'),
    ('liquid/lfm2.5-1.2b', 'Liquid LFM2.5 1.2B', False,
     'Compact text-only model designed for fast local replies; images are unavailable.'),
)
MEDIA = {'.png': ('image', 'image/png'), '.jpg': ('image', 'image/jpeg'),
         '.jpeg': ('image', 'image/jpeg'), '.webp': ('image', 'image/webp'),
         '.gif': ('image', 'image/gif'), '.mp4': ('video', 'video/mp4'),
         '.webm': ('video', 'video/webm')}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, 'Local service redirects are disabled.', headers, fp)


class AIService:
    def __init__(self, root):
        self.root = Path(root)
        self._opener = build_opener(ProxyHandler({}), NoRedirect())
        self._outputs = {}
        self._output_key = secrets.token_bytes(32)
        self._output_lock = threading.Lock()
        self._video_previews = ComfyVideoPreviews(self.root, lambda path, headers=None:
                                               self._open(COMFY_BASE, path, headers=headers, timeout=10))

    def _open(self, base, path, data=None, headers=None, timeout=5):
        allowed = (base == LM_BASE and path in ('/api/v1/models', '/api/v1/models/load', '/v1/chat/completions'))
        allowed |= (base == COMFY_BASE and (path in ('/system_stats', '/queue', '/history?max_items=10')
                                           or path.startswith('/view?')))
        if not allowed:
            raise ValueError('Unsupported local service route.')
        request = Request(base + path, data=data, headers=headers or {}, method='POST' if data else 'GET')
        return self._opener.open(request, timeout=timeout)

    def _json(self, base, path, data=None, timeout=5):
        request = self._open(base, path) if data is None else self._open(
            base, path, json.dumps(data).encode('utf-8'), {'Content-Type': 'application/json'}, timeout=timeout)
        with request as response:
            raw = response.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError('Local service metadata exceeds the limit.')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('Invalid local service response.')
        return value

    @staticmethod
    def _unavailable(service, error):
        if isinstance(error, HTTPError) and error.code in (401, 403):
            return service + ' requires authentication. Configure access on the PC.'
        return service + ' is unavailable. Check its local API on the PC.'

    @staticmethod
    def _vision(item):
        capabilities = item.get('capabilities') if isinstance(item, dict) else None
        vision = capabilities.get('vision') if isinstance(capabilities, dict) else None
        return vision if type(vision) is bool else None

    @staticmethod
    def _loaded_models(data):
        catalog = data.get('models') if isinstance(data, dict) else None
        if not isinstance(catalog, list):
            raise ValueError('Invalid model catalog.')
        models, seen = [], set()
        for item in catalog[:500]:
            if not isinstance(item, dict) or item.get('type') != 'llm':
                continue
            instances = item.get('loaded_instances', [])
            if not isinstance(instances, list):
                continue
            for instance in instances[:20]:
                if not isinstance(instance, dict):
                    continue
                identifier = instance.get('id')
                if not isinstance(identifier, str) or not identifier or len(identifier) > 256 or identifier in seen:
                    continue
                if any(ord(char) < 32 for char in identifier):
                    continue
                config = instance.get('config', {})
                context = config.get('context_length') if isinstance(config, dict) else None
                context = context if type(context) is int and context > 0 else None
                name = item.get('display_name')
                name = name[:160] if isinstance(name, str) and name else identifier
                models.append({'id': identifier, 'name': name, 'contextLength': context, 'vision': AIService._vision(item)})
                seen.add(identifier)
                if len(models) >= 50:
                    break
            if len(models) >= 50:
                break
        return models

    def models(self):
        try:
            return self._model_snapshot(self._json(LM_BASE, '/api/v1/models'))
        except (OSError, URLError, ValueError, TypeError) as error:
            return {'ok': True, 'available': False, 'models': [], 'availableModels': [], 'quickModels': [],
                    'message': self._unavailable('LM Studio', error)}

    @classmethod
    def _model_snapshot(cls, data):
        loaded, available, seen = cls._loaded_models(data), [], set()
        for item in data['models'][:500]:
            if not isinstance(item, dict) or item.get('type') != 'llm':
                continue
            identifier = item.get('key')
            if (not isinstance(identifier, str) or not identifier or len(identifier) > 256
                    or identifier in seen or any(ord(char) < 32 for char in identifier)):
                continue
            name = item.get('display_name')
            available.append({'id': identifier, 'name': name[:160] if isinstance(name, str) and name else identifier,
                              'vision': cls._vision(item)})
            seen.add(identifier)
        installed = {item['id']: item for item in available}
        quick = [{'id': identifier, 'name': name, 'vision': vision, 'description': description}
                 for identifier, name, vision, description in FAST_MODEL_CHOICES
                 if identifier in installed and installed[identifier]['vision'] is vision]
        return {'ok': True, 'available': True, 'models': loaded, 'availableModels': available, 'quickModels': quick,
                'message': 'Choose a loaded model.' if loaded else 'No model is loaded. Choose an installed model to load.'}

    def load_lm_model(self, payload):
        """Only the user's explicit selection may load a catalogued LLM; never download or unload."""
        if (not isinstance(payload, dict) or set(payload) != {'model'} or not isinstance(payload['model'], str)
                or not payload['model'] or len(payload['model']) > 256):
            raise ValueError('Choose an installed language model.')
        identifier = payload['model']
        try:
            catalog = self._json(LM_BASE, '/api/v1/models')
            snapshot = self._model_snapshot(catalog)
            if identifier not in {item['id'] for item in snapshot['availableModels']}:
                raise ValueError('That language model is not installed. Refresh models and choose one.')
            selected = next(item for item in catalog['models'][:500]
                            if isinstance(item, dict) and item.get('type') == 'llm' and item.get('key') == identifier)
            instances = selected.get('loaded_instances', [])
            if not isinstance(instances, list):
                raise ValueError('LM Studio returned invalid model state. Refresh models.')
            if instances:
                loaded_ids = {item['id'] for item in snapshot['models']}
                existing = next((item['id'] for item in instances[:20]
                                 if isinstance(item, dict) and item.get('id') in loaded_ids), None)
                return dict(snapshot, ok=existing is not None, selectedInstanceId=existing,
                            message='That model is already loaded. Choose its loaded instance.' if existing
                            else 'LM Studio did not report a usable loaded instance. Refresh models.')
            reply = self._json(LM_BASE, '/api/v1/models/load', {'model': identifier}, timeout=120)
            instance = reply.get('instance_id')
            if (reply.get('type') != 'llm' or reply.get('status') != 'loaded'
                    or not isinstance(instance, str) or not instance or len(instance) > 256):
                return dict(snapshot, ok=False, message='LM Studio did not confirm the model load. Check LM Studio on the PC.')
            refreshed = self._json(LM_BASE, '/api/v1/models')
            current = self._model_snapshot(refreshed)
            selected_ids = {entry.get('id') for item in refreshed['models'][:500]
                            if isinstance(item, dict) and item.get('type') == 'llm' and item.get('key') == identifier
                            and isinstance(item.get('loaded_instances'), list)
                            for entry in item['loaded_instances'][:20] if isinstance(entry, dict)}
            verified = instance in selected_ids and instance in {item['id'] for item in current['models']}
            return dict(current, ok=verified, selectedInstanceId=instance if verified else None,
                        message='The selected model is loaded. Choose it to chat.' if verified
                        else 'The load is not yet confirmed. Refresh models or check LM Studio on the PC.')
        except (OSError, URLError) as error:
            return {'ok': False, 'available': False, 'models': [], 'availableModels': [],
                    'message': self._unavailable('LM Studio', error)}
        except (json.JSONDecodeError, TypeError):
            return {'ok': False, 'available': False, 'models': [], 'availableModels': [],
                    'message': 'LM Studio returned invalid model metadata. Check LM Studio on the PC.'}

    def start_lm_api(self):
        """Call only for the authenticated user's explicit Start API action."""
        current = self.models()
        if current['available']:
            return dict(current, message='LM Studio API is already running.')
        cli = Path.home() / '.lmstudio' / 'bin' / 'lms.exe'
        if not cli.is_file():
            return {'ok': False, 'available': False, 'message': 'LM Studio CLI is not installed on this PC.'}
        try:
            result = subprocess.run([str(cli), 'server', 'start', '--port', '1234', '--bind', '127.0.0.1'],
                                    capture_output=True, timeout=20, check=False,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if result.returncode:
                return {'ok': False, 'available': False, 'message': 'LM Studio API could not start. Check LM Studio on the PC.'}
            current = self.models()
            return dict(current, message='LM Studio API is running on this PC.' if current['available'] else current['message'])
        except (OSError, subprocess.TimeoutExpired):
            return {'ok': False, 'available': False, 'message': 'LM Studio API could not start. Check LM Studio on the PC.'}

    @staticmethod
    def _image_data_url(value):
        if not isinstance(value, str) or len(value) > 40 + 4 * ((MAX_CHAT_IMAGE_BYTES + 2) // 3):
            raise ValueError('Each chat image must be 2 MB or smaller.')
        match = re.fullmatch(r'data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/]*={0,2})', value)
        if not match:
            raise ValueError('Attach a JPEG, PNG or WebP image as inline data. Image URLs and file paths are not accepted.')
        try:
            raw = base64.b64decode(match[2], validate=True)
        except binascii.Error:
            raise ValueError('This image has invalid base64 data.') from None
        if not 1 <= len(raw) <= MAX_CHAT_IMAGE_BYTES:
            raise ValueError('Each chat image must be 2 MB or smaller.')
        try:
            from PIL import Image, ImageOps
        except ImportError:
            raise ValueError('Image chat requires Pillow on the PC.') from None
        try:
            with Image.open(io.BytesIO(raw)) as image:
                expected = {'jpeg': 'JPEG', 'png': 'PNG', 'webp': 'WEBP'}[match[1]]
                if image.format != expected or getattr(image, 'n_frames', 1) != 1:
                    raise ValueError('Attach a still JPEG, PNG or WebP image with its matching image type.')
                if (max(image.size) > MAX_CHAT_IMAGE_SIDE or image.width * image.height > MAX_CHAT_IMAGE_PIXELS):
                    raise ValueError('Chat images must be at most 8192 pixels per side and 16 million pixels.')
                image.load()
                oriented = ImageOps.exif_transpose(image)
                oriented.thumbnail((CHAT_IMAGE_PREVIEW_SIDE, CHAT_IMAGE_PREVIEW_SIDE))
                # Fresh RGB pixels remove EXIF, ICC, text chunks and workflow metadata.
                rgba = oriented.convert('RGBA')
                clean = Image.new('RGB', rgba.size, 'white')
                clean.paste(rgba, mask=rgba.getchannel('A'))
                output = io.BytesIO()
                clean.save(output, 'JPEG', quality=85)
        except (OSError, SyntaxError, ValueError, TypeError, Image.DecompressionBombError):
            raise ValueError('Choose a still JPEG, PNG or WebP image with its matching type, at most 8192 pixels per side and 16 million pixels.') from None
        encoded = output.getvalue()
        if len(encoded) > MAX_CHAT_IMAGE_BYTES:
            raise ValueError('The prepared image exceeds the 2 MB chat limit. Choose a smaller image.')
        return 'data:image/jpeg;base64,' + base64.b64encode(encoded).decode('ascii')

    @classmethod
    def _chat_payload(cls, payload):
        if not isinstance(payload, dict) or set(payload) != {'model', 'messages'}:
            raise ValueError('Choose a loaded model and provide chat messages.')
        model, messages = payload['model'], payload['messages']
        if not isinstance(model, str) or not model or len(model) > 256:
            raise ValueError('Choose a loaded model.')
        if not isinstance(messages, list) or not 1 <= len(messages) <= 32:
            raise ValueError('Chat supports between 1 and 32 messages.')
        clean, total, images = [], 0, 0
        for message in messages:
            if not isinstance(message, dict) or set(message) != {'role', 'content'}:
                raise ValueError('Chat supports role and content fields only.')
            role, content = message['role'], message['content']
            if role not in ('system', 'user', 'assistant'):
                raise ValueError('Chat supports user, assistant and system messages only.')
            if isinstance(content, str) and content.strip():
                length = len(content)
            elif role == 'user' and isinstance(content, list) and 1 <= len(content) <= 16:
                parts, length = [], 0
                for part in content:
                    if not isinstance(part, dict):
                        raise ValueError('Use text and inline image content only.')
                    if part.get('type') == 'text' and set(part) == {'type', 'text'}:
                        value = part['text']
                        if not isinstance(value, str) or not value.strip():
                            raise ValueError('Text content must contain a message.')
                        length += len(value)
                        if length > 16384:
                            raise ValueError('This conversation is too long. Start a new chat.')
                        parts.append({'type': 'text', 'text': value})
                    elif part.get('type') == 'image_url' and set(part) == {'type', 'image_url'}:
                        image = part['image_url']
                        images += 1
                        if images > MAX_CHAT_IMAGES:
                            raise ValueError('A conversation supports at most 3 images. Clear the conversation to send more.')
                        if not isinstance(image, dict) or set(image) != {'url'}:
                            raise ValueError('Use inline image data without extra image options.')
                        parts.append({'type': 'image_url', 'image_url': {'url': cls._image_data_url(image['url'])}})
                    else:
                        raise ValueError('Use text and inline JPEG, PNG or WebP images only.')
                content = parts
            else:
                raise ValueError('Only user messages may contain image content. Other messages must be nonempty text.')
            total += length
            if length > 16384 or total > MAX_CHAT_CHARS:
                raise ValueError('This conversation is too long. Start a new chat.')
            clean.append({'role': role, 'content': content})
        if clean[-1]['role'] != 'user':
            raise ValueError('End the conversation with your message.')
        return {'model': model, 'messages': clean, 'stream': True}

    @staticmethod
    def _check_chat_model(clean, models):
        selected = next((model for model in models if model['id'] == clean['model']), None)
        if selected is None:
            raise ValueError('That model is no longer loaded. Refresh models and choose one.')
        has_images = any(isinstance(message['content'], list)
                         and any(part['type'] == 'image_url' for part in message['content']) for message in clean['messages'])
        if has_images and selected.get('vision') is not True:
            if selected.get('vision') is False:
                raise ValueError('The selected loaded model does not support images. Choose a loaded vision model or remove the images.')
            raise ValueError('LM Studio has not reported image support for this loaded model. Choose a model with reported vision support or remove the images.')

    @staticmethod
    def _event(value):
        return 'data: ' + json.dumps(value, ensure_ascii=True, separators=(',', ':')) + '\n\n'

    def stream_chat(self, payload, cancel_event=None):
        if cancel_event is not None:
            yield from self.stream_chat_cancellable(payload, cancel_event)
            return
        response = None
        try:
            try:
                clean = self._chat_payload(payload)
            except (ValueError, TypeError) as error:
                yield self._event({'error': str(error)})
                return
            snapshot = self.models()
            if not snapshot['available']:
                yield self._event({'error': snapshot['message']})
                return
            try:
                self._check_chat_model(clean, snapshot['models'])
            except ValueError as error:
                yield self._event({'error': str(error)})
                return
            response = self._open(LM_BASE, '/v1/chat/completions',
                                  json.dumps(clean, ensure_ascii=False).encode('utf-8'),
                                  {'Content-Type': 'application/json', 'Accept': 'text/event-stream'}, timeout=20)
            started, total, event_lines, event_size, finished = time.monotonic(), 0, [], 0, False
            while True:
                if time.monotonic() - started > 300:
                    raise ValueError('The response exceeded the time limit.')
                line = response.readline(MAX_EVENT_BYTES + 1)
                if not line:
                    if finished:
                        yield self._event({'done': True})
                    else:
                        yield self._event({'error': 'LM Studio stopped before completing the response.'})
                    return
                total += len(line)
                event_size += len(line)
                if total > MAX_STREAM_BYTES or event_size > MAX_EVENT_BYTES:
                    raise ValueError('The response exceeded the size limit.')
                line = line.decode('utf-8').rstrip('\r\n')
                if line.startswith('data:'):
                    event_lines.append(line[5:].lstrip())
                if line:
                    continue
                event_size = 0
                if not event_lines:
                    continue
                raw, event_lines = '\n'.join(event_lines), []
                if raw == '[DONE]':
                    yield self._event({'done': True})
                    return
                item = json.loads(raw)
                if not isinstance(item, dict) or item.get('error'):
                    raise ValueError('Invalid stream event.')
                choices = item.get('choices')
                if not isinstance(choices, list):
                    raise ValueError('Invalid stream event.')
                if not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict) or not isinstance(choice.get('delta', {}), dict):
                    raise ValueError('Invalid stream event.')
                content = choice.get('delta', {}).get('content')
                if content is not None and not isinstance(content, str):
                    raise ValueError('Invalid stream content.')
                if content:
                    yield self._event({'text': content})
                if choice.get('finish_reason') is not None:
                    finished = True
        except (OSError, URLError, ValueError, TypeError) as error:
            message = self._unavailable('LM Studio', error) if isinstance(error, (OSError, URLError)) else 'LM Studio returned an invalid or oversized response.'
            yield self._event({'error': message})
        finally:
            if response is not None:
                response.close()

    def stream_chat_cancellable(self, payload, cancel_event=None):
        """Same SSE contract; setting the Event interrupts pending HTTP work."""
        cancellation = cancel_event if cancel_event is not None else threading.Event()
        try:
            clean = self._chat_payload(payload)
        except (ValueError, TypeError) as error:
            yield self._event({'error': str(error)})
            return
        if cancellation.is_set():
            return
        try:
            import aiohttp
        except ImportError:
            yield self._event({'error': 'Cancellable chat requires aiohttp on the PC.'})
            return
        chunks, completed = queue.Queue(maxsize=8), threading.Event()

        async def emit(value):
            while not cancellation.is_set():
                try:
                    chunks.put_nowait(self._event(value))
                    return
                except queue.Full:
                    await asyncio.sleep(0.025)

        async def bounded_json(response):
            raw = bytearray()
            async for part in response.content.iter_chunked(MAX_EVENT_BYTES):
                raw.extend(part)
                if len(raw) > MAX_JSON_BYTES:
                    raise ValueError('Oversized catalog.')
            return json.loads(raw)

        async def produce():
            try:
                timeout = aiohttp.ClientTimeout(total=300, connect=5, sock_read=120)
                connector = aiohttp.TCPConnector(limit=2, force_close=True)
                async with aiohttp.ClientSession(timeout=timeout, connector=connector,
                                                  cookie_jar=aiohttp.DummyCookieJar(), trust_env=False) as session:
                    async with session.get(LM_BASE + '/api/v1/models', allow_redirects=False,
                                           timeout=aiohttp.ClientTimeout(total=5)) as response:
                        if response.status != 200:
                            raise HTTPError(LM_BASE, response.status, '', {}, None)
                        models = self._loaded_models(await bounded_json(response))
                    try:
                        self._check_chat_model(clean, models)
                    except ValueError as error:
                        await emit({'error': str(error)})
                        return
                    async with session.post(LM_BASE + '/v1/chat/completions', json=clean,
                                            headers={'Accept': 'text/event-stream'}, allow_redirects=False) as response:
                        if response.status != 200:
                            raise HTTPError(LM_BASE, response.status, '', {}, None)
                        total, event_size, event_lines, finished = 0, 0, [], False
                        while True:
                            line = await response.content.readline()
                            if not line:
                                await emit({'done': True} if finished else {'error': 'LM Studio stopped before completing the response.'})
                                return
                            total += len(line)
                            event_size += len(line)
                            if total > MAX_STREAM_BYTES or event_size > MAX_EVENT_BYTES:
                                raise ValueError('Oversized stream.')
                            line = line.decode('utf-8').rstrip('\r\n')
                            if line.startswith('data:'):
                                event_lines.append(line[5:].lstrip())
                            if line:
                                continue
                            event_size = 0
                            if not event_lines:
                                continue
                            raw, event_lines = '\n'.join(event_lines), []
                            if raw == '[DONE]':
                                await emit({'done': True})
                                return
                            item = json.loads(raw)
                            if not isinstance(item, dict) or item.get('error') or not isinstance(item.get('choices'), list):
                                raise ValueError('Invalid stream event.')
                            if not item['choices']:
                                continue
                            choice = item['choices'][0]
                            if not isinstance(choice, dict) or not isinstance(choice.get('delta', {}), dict):
                                raise ValueError('Invalid stream event.')
                            content = choice.get('delta', {}).get('content')
                            if content is not None and not isinstance(content, str):
                                raise ValueError('Invalid stream content.')
                            if content:
                                await emit({'text': content})
                            if choice.get('finish_reason') is not None:
                                finished = True
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, URLError, ValueError, TypeError) as error:
                message = ('LM Studio returned an invalid or oversized response.' if isinstance(error, (ValueError, TypeError))
                           else self._unavailable('LM Studio', error))
                await emit({'error': message})

        async def supervise():
            task = asyncio.create_task(produce())
            try:
                while not task.done():
                    if cancellation.is_set():
                        task.cancel()
                        break
                    await asyncio.sleep(0.025)
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        def worker():
            try:
                asyncio.run(supervise())
            finally:
                completed.set()

        thread = threading.Thread(target=worker, name='MAIC-AI-stream', daemon=True)
        thread.start()
        try:
            while not cancellation.is_set():
                try:
                    yield chunks.get(timeout=0.05)
                except queue.Empty:
                    if completed.is_set():
                        return
        finally:
            cancellation.set()
            thread.join(timeout=1)

    @staticmethod
    def _media_reference(item):
        if not isinstance(item, dict) or item.get('type') != 'output':
            return None
        filename, subfolder = item.get('filename'), item.get('subfolder', '')
        if not isinstance(filename, str) or not 1 <= len(filename) <= 255 or not isinstance(subfolder, str) or len(subfolder) > 512:
            return None
        if any(char in filename for char in '/\\:%[]') or '..' in filename or any(ord(char) < 32 for char in filename):
            return None
        if any(char in subfolder for char in '\\:%[]') or subfolder.startswith('/') or any(ord(char) < 32 for char in subfolder):
            return None
        if any(part in ('.', '..', '') for part in subfolder.split('/')) and subfolder:
            return None
        media = MEDIA.get(Path(filename).suffix.lower())
        return (filename, subfolder, media[0], media[1]) if media else None

    def comfy_status(self):
        try:
            self._json(COMFY_BASE, '/system_stats')
            queue = self._json(COMFY_BASE, '/queue')
            history = self._json(COMFY_BASE, '/history?max_items=10')
            running, pending = queue.get('queue_running', []), queue.get('queue_pending', [])
            if not isinstance(running, list) or not isinstance(pending, list):
                raise ValueError('Invalid queue metadata.')
            gallery, output_map, seen = [], {}, set()
            for entry in list(history.values())[-10:][::-1]:
                if not isinstance(entry, dict) or not isinstance(entry.get('outputs'), dict):
                    continue
                for node in list(entry['outputs'].values())[:100]:
                    if not isinstance(node, dict):
                        continue
                    for kind in ('images', 'videos', 'gifs'):
                        items = node.get(kind, [])
                        if not isinstance(items, list):
                            continue
                        for item in items[:50]:
                            reference = self._media_reference(item)
                            if not reference or reference in seen or len(gallery) >= 40:
                                continue
                            identifier = hmac.new(self._output_key, json.dumps(reference).encode('utf-8'), 'sha256').hexdigest()[:32]
                            seen.add(reference)
                            output_map[identifier] = reference
                            gallery.append({'id': identifier, 'type': reference[2], 'name': reference[0],
                                            'previewUrl': '/api/comfy/output/' + identifier})
            with self._output_lock:
                self._outputs = output_map
            return {'ok': True, 'available': True, 'queue': {'running': len(running), 'pending': len(pending)},
                    'outputs': gallery, 'message': 'ComfyUI is running. Outputs are read only.'}
        except (OSError, URLError, ValueError, TypeError) as error:
            with self._output_lock:
                self._outputs = {}
            return {'ok': True, 'available': False, 'queue': {'running': 0, 'pending': 0}, 'outputs': [],
                    'message': self._unavailable('ComfyUI', error)}

    @staticmethod
    def _range(value):
        if not isinstance(value, str) or len(value) > 80:
            raise ValueError('Invalid media range.')
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', value.strip())
        if not match or not any(match.groups()):
            raise ValueError('Use a single byte range.')
        first, last = match.groups()
        if first:
            start = int(first)
            end = int(last) if last else start + MAX_RANGE_BYTES - 1
            if end < start or start > 2**63 - 1 or end > 2**63 - 1:
                raise ValueError('Invalid media range.')
            end = min(end, start + MAX_RANGE_BYTES - 1)
            return 'bytes=' + str(start) + '-' + str(end), start, end, None
        suffix = min(int(last), MAX_RANGE_BYTES)
        if suffix < 1:
            raise ValueError('Invalid media range.')
        return 'bytes=-' + str(suffix), None, None, suffix

    @staticmethod
    def _thumbnail(raw):
        try:
            from PIL import Image
        except ImportError:
            raise ValueError('Image previews need Pillow on the PC.') from None
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > 20000000:
                    raise ValueError('The image is too large for a preview.')
                image.thumbnail((960, 960))
                clean = image.convert('RGB')
                clean.info.clear()
                result = io.BytesIO()
                clean.save(result, format='JPEG', quality=85)
                return 'image/jpeg', result.getvalue()
        except (OSError, ValueError, Image.DecompressionBombError):
            raise ValueError('This image could not be previewed safely.') from None

    def output(self, identifier, range_header=None):
        if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{32}', identifier):
            raise ValueError('Unknown ComfyUI output.')
        with self._output_lock:
            reference = self._outputs.get(identifier)
        if not reference:
            raise ValueError('Unknown ComfyUI output. Refresh the gallery.')
        return self.output_reference(reference, range_header)

    def output_reference(self, reference, range_header=None):
        """Preview a validated server-owned output reference, never a client path."""
        filename, subfolder, kind, content_type = reference
        if kind == 'video':
            range_info = self._range(range_header) if range_header is not None else None
            preview = self._video_previews.get(reference)
            try:
                with preview.open('rb') as file:
                    total = os.fstat(file.fileno()).st_size
                    if not 0 < total <= MAX_OUTPUT_BYTES:
                        raise ValueError('This video exceeds the full preview size limit.')
                    if not range_info:
                        raw = file.read(MAX_OUTPUT_BYTES + 1)
                        if len(raw) != total:
                            raise ValueError('The preview changed. Retry playback.')
                        return 'video/mp4', raw
                    _, start, end, suffix = range_info
                    start = max(0, total - suffix) if suffix is not None else start
                    end = total - 1 if suffix is not None else min(end, total - 1)
                    if start >= total:
                        raise ValueError('The requested video range is unavailable.')
                    file.seek(start)
                    raw = file.read(end - start + 1)
                    if len(raw) != end - start + 1:
                        raise ValueError('The preview changed. Retry playback.')
                return 'video/mp4', raw, {'status': 206, 'headers': {'Accept-Ranges': 'bytes',
                                         'Content-Range': f'bytes {start}-{end}/{total}'}}
            except OSError:
                raise ValueError('The cached video preview is unavailable. Retry playback.') from None
        if range_header is not None:
            raise ValueError('Byte ranges are supported for videos only.')
        query = urlencode({'filename': filename, 'subfolder': subfolder, 'type': 'output'})
        try:
            with self._open(COMFY_BASE, '/view?' + query, headers={}, timeout=10) as response:
                status = response.getcode()
                limit = MAX_OUTPUT_BYTES
                length = response.headers.get('Content-Length')
                if length and (not length.isdigit() or int(length) > limit):
                    raise ValueError('This output exceeds the preview size limit.')
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise ValueError('This output exceeds the preview size limit.')
                if status != 200:
                    raise ValueError('Invalid output response.')
                return self._thumbnail(raw)
        except (OSError, URLError):
            raise ValueError('ComfyUI output is unavailable. Refresh the gallery.') from None
