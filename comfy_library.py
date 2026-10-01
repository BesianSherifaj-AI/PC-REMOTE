"""Read-only catalogue of a PC-configured ComfyUI output tree.

Requests select opaque IDs, never paths. Media bytes still go through the
existing ComfyUI preview/transcoding client after reference() revalidates them.
"""
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from ai_services import AIService

MAX_ENTRIES = 20000
MAX_SCAN_SECONDS = 3
CACHE_SECONDS = 20
MAX_PAGE = 24


class ComfyLibrary:
    def __init__(self, project_root):
        self.config = Path(project_root) / '.runtime' / 'comfy-library.json'
        self._key = secrets.token_bytes(32)
        self._lock = threading.RLock()
        self._root = None
        self._at = -float('inf')
        self._files = {}
        self._folders = {'root': ''}
        self._truncated = False

    @staticmethod
    def _linked(info):
        return (stat.S_ISLNK(info.st_mode)
                or bool(getattr(info, 'st_file_attributes', 0) & 0x400))

    def _configured_root(self):
        try:
            with self.config.open('rb') as file:
                raw = file.read(4097)
            if len(raw) > 4096:
                return None
            settings = json.loads(raw)
            value = settings.get('outputRoot') if isinstance(settings, dict) else None
            if not isinstance(value, str) or not value or len(value) > 1024:
                return None
            root = Path(value)
            if not root.is_absolute() or self._linked(root.lstat()):
                return None
            root = root.resolve(strict=True)
            return root if root.is_dir() else None
        except (OSError, ValueError, RuntimeError):
            return None

    def _identifier(self, kind, relative):
        value = f'{self._root}\0{kind}\0{relative}'.encode('utf-8')
        return hmac.new(self._key, value, 'sha256').hexdigest()[:32]

    def _safe_file(self, relative):
        """Reject links/junctions at every component, including changed entries."""
        candidate = self._root
        for part in Path(relative).parts:
            if part in ('', '.', '..'):
                raise ValueError('Unknown ComfyUI library output.')
            candidate = candidate / part
            if self._linked(candidate.lstat()):
                raise ValueError('Linked ComfyUI outputs are unavailable.')
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(self._root) or not resolved.is_file():
            raise ValueError('Unknown ComfyUI library output.')
        return resolved

    def _refresh(self):
        root = self._configured_root()
        now = time.monotonic()
        if root is None:
            self._root, self._files, self._folders = None, {}, {'root': ''}
            self._truncated = False
            return False
        if root == self._root and now - self._at < CACHE_SECONDS:
            return True
        self._root, self._files, self._folders = root, {}, {'root': ''}
        self._truncated = False
        stack, visited = [root], 0
        while stack and not self._truncated:
            directory = stack.pop()
            try:
                # Recheck queued directories so a replaced junction is never followed.
                if self._linked(directory.lstat()) or not directory.resolve().is_relative_to(root):
                    continue
                with os.scandir(directory) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > MAX_ENTRIES or time.monotonic() - now > MAX_SCAN_SECONDS:
                            self._truncated = True
                            break
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if self._linked(info):
                                continue
                            path = Path(entry.path)
                            if stat.S_ISDIR(info.st_mode):
                                stack.append(path)
                                continue
                            if not stat.S_ISREG(info.st_mode):
                                continue
                            relative = path.relative_to(root).as_posix()
                            folder = path.parent.relative_to(root).as_posix()
                            folder = '' if folder == '.' else folder
                            reference = AIService._media_reference({
                                'filename': path.name, 'subfolder': folder, 'type': 'output'})
                            if not reference:
                                continue
                            self._safe_file(relative)
                            identifier = self._identifier('file', relative)
                            self._files[identifier] = {
                                'relative': relative, 'folder': folder, 'reference': reference,
                                'id': identifier, 'type': reference[2], 'name': path.name,
                                'sizeBytes': info.st_size, 'modified': info.st_mtime,
                                'previewUrl': '/api/comfy/library/output/' + identifier,
                            }
                            parent = Path(folder)
                            while str(parent) != '.':
                                name = parent.as_posix()
                                self._folders[self._identifier('folder', name)] = name
                                parent = parent.parent
                        except (OSError, ValueError, RuntimeError):
                            continue
            except OSError:
                continue
        self._at = time.monotonic()
        return True

    def list_outputs(self, folder='root', search='', media='all', offset=0, limit=MAX_PAGE):
        if (not isinstance(folder, str) or (folder != 'root' and not re.fullmatch(r'[0-9a-f]{32}', folder))
                or not isinstance(search, str) or len(search) > 120
                or any(ord(char) < 32 for char in search)
                or not isinstance(media, str) or media not in ('all', 'image', 'video')
                or type(offset) is not int or not 0 <= offset <= MAX_ENTRIES
                or type(limit) is not int or not 1 <= limit <= MAX_PAGE):
            raise ValueError('Invalid ComfyUI library filters.')
        with self._lock:
            if not self._refresh():
                return {'ok': True, 'available': False, 'folder': 'root', 'folders': [],
                        'breadcrumbs': [], 'outputs': [], 'total': 0, 'offset': 0,
                        'limit': limit, 'nextOffset': None, 'truncated': False,
                        'message': 'Set the ComfyUI output folder in the PC library configuration.'}
            if folder not in self._folders:
                raise ValueError('Unknown ComfyUI library folder. Refresh the library.')
            relative = self._folders[folder]
            prefix = relative + '/' if relative else ''
            query = search.casefold().strip()
            selected = [item for item in self._files.values()
                        if item['relative'].startswith(prefix)
                        and (media == 'all' or item['type'] == media)
                        and (not query or query in item['relative'].casefold())]
            selected.sort(key=lambda item: (-item['modified'], item['relative'].casefold()))
            outputs = [{key: item[key] for key in
                        ('id', 'type', 'name', 'folder', 'sizeBytes', 'modified', 'previewUrl')}
                       for item in selected[offset:offset + limit]]
            folders = [{'id': identifier, 'name': name[len(prefix):]}
                       for identifier, name in self._folders.items()
                       if name.startswith(prefix) and name != relative
                       and '/' not in name[len(prefix):]]
            folders.sort(key=lambda item: item['name'].casefold())
            folder_limit_reached = len(folders) > 200
            folders = folders[:200]
            breadcrumbs = [{'id': 'root', 'name': 'All outputs'}]
            for index in range(1, len(Path(relative).parts) + 1):
                name = Path(*Path(relative).parts[:index]).as_posix()
                breadcrumbs.append({'id': self._identifier('folder', name), 'name': Path(name).name})
            next_offset = offset + limit if offset + limit < len(selected) else None
            return {'ok': True, 'available': True, 'folder': folder, 'folders': folders,
                    'breadcrumbs': breadcrumbs, 'outputs': outputs, 'total': len(selected),
                    'offset': offset, 'limit': limit, 'nextOffset': next_offset,
                    'truncated': self._truncated or folder_limit_reached,
                    'message': ('Showing a bounded scan. Use a smaller configured output folder.'
                                if self._truncated or folder_limit_reached
                                else 'Saved outputs, newest first. Originals stay unchanged.')}

    def reference(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{32}', identifier):
            raise ValueError('Unknown ComfyUI library output.')
        with self._lock:
            if not self._refresh() or identifier not in self._files:
                raise ValueError('Unknown ComfyUI library output. Refresh the library.')
            item = self._files[identifier]
            try:
                self._safe_file(item['relative'])
            except (OSError, RuntimeError):
                raise ValueError('This saved output is no longer available.') from None
            return item['reference']

    def open_folder(self, payload):
        if not isinstance(payload, dict) or payload:
            raise ValueError('Opening the output folder takes no arguments.')
        if os.name != 'nt':
            raise RuntimeError('Opening the output folder requires Windows.')
        with self._lock:
            root = self._configured_root()
            if root is None:
                raise ValueError('The PC ComfyUI output folder is not configured or available.')
            os.startfile(str(root))
        return {'ok': True, 'message': 'Requested the ComfyUI output folder on this PC.'}
