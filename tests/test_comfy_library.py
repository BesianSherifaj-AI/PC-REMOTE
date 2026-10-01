"""Catalogue boundaries without media reads, queue changes, or real app launches."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from comfy_library import ComfyLibrary


class ComfyLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.output = self.base / 'output'
        self.output.mkdir()
        self.project = self.base / 'project'
        self.runtime = self.project / '.runtime'
        self.runtime.mkdir(parents=True)
        self.config = self.runtime / 'comfy-library.json'
        self.configure(self.output)
        self.library = ComfyLibrary(self.project)

    def configure(self, root):
        self.config.write_text(json.dumps({'outputRoot': str(root)}), encoding='utf-8')

    def media(self, name, modified=100):
        path = self.output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'test')
        os.utime(path, (modified, modified))
        return path

    def test_missing_or_invalid_config_stays_unavailable(self):
        for data in (None, '{}', '[]', '{bad', '{"outputRoot":"relative"}', 'x' * 4097):
            with self.subTest(data=data):
                if data is None:
                    self.config.unlink()
                else:
                    self.config.write_text(data, encoding='utf-8')
                result = self.library.list_outputs()
                self.assertFalse(result['available'])
                self.assertEqual(result['outputs'], [])
                self.assertNotIn(str(self.output), json.dumps(result))

    def test_nested_folder_search_type_and_newest_pagination(self):
        self.media('old.png', 10)
        self.media('scene one/frame.JPG', 20)
        self.media('scene one/takes/clip.mp4', 30)
        self.media('workflow.json', 40)
        result = self.library.list_outputs(limit=2)
        self.assertEqual([item['name'] for item in result['outputs']], ['clip.mp4', 'frame.JPG'])
        self.assertEqual(result['total'], 3)
        self.assertEqual(result['nextOffset'], 2)
        self.assertEqual(result['folders'][0]['name'], 'scene one')
        self.assertEqual(result['breadcrumbs'], [{'id': 'root', 'name': 'All outputs'}])
        self.assertEqual(self.library.list_outputs(offset=2, limit=2)['outputs'][0]['name'], 'old.png')
        folder = result['folders'][0]['id']
        nested = self.library.list_outputs(folder=folder)
        self.assertEqual(nested['total'], 2)
        self.assertEqual(nested['folders'][0]['name'], 'takes')
        self.assertEqual(nested['breadcrumbs'][-1], {'id': folder, 'name': 'scene one'})
        filtered = self.library.list_outputs(folder=folder, search='CLIP', media='video')
        self.assertEqual(filtered['total'], 1)
        self.assertEqual(filtered['outputs'][0]['type'], 'video')
        self.assertNotIn(str(self.output), json.dumps(result))
        self.assertNotIn('relative', result['outputs'][0])

    def test_reference_ids_are_stable_and_only_safe_preview_names_are_listed(self):
        self.media('safe/image & 1.png')
        for name in ('secret..png', 'bad%20.png', 'bad[1].png', 'page.html', 'workflow.json'):
            self.media(name)
        result = self.library.list_outputs()
        self.assertEqual(result['total'], 1)
        item = result['outputs'][0]
        self.assertRegex(item['id'], r'^[0-9a-f]{32}$')
        self.assertEqual(item['previewUrl'], '/api/comfy/library/output/' + item['id'])
        self.assertEqual(self.library.reference(item['id']),
                         ('image & 1.png', 'safe', 'image', 'image/png'))
        self.library._at = -float('inf')
        self.assertEqual(self.library.list_outputs()['outputs'][0]['id'], item['id'])

    def test_invalid_browser_paths_filters_and_large_pages_are_rejected(self):
        for options in ({'folder': '../output'}, {'folder': str(self.output)}, {'folder': '0' * 32},
                        {'media': 'html'}, {'media': []}, {'search': 'x' * 121}, {'search': '\x00'},
                        {'offset': -1}, {'offset': True}, {'offset': 20001},
                        {'limit': 25}, {'limit': 0}, {'limit': True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.library.list_outputs(**options)
        for identifier in ('../secret.png', str(self.output), '0' * 32, None, []):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.library.reference(identifier)

    def test_deleted_files_and_changed_root_revoke_previous_references(self):
        path = self.media('image.png')
        identifier = self.library.list_outputs()['outputs'][0]['id']
        path.unlink()
        with self.assertRaises(ValueError):
            self.library.reference(identifier)
        other = self.base / 'other'
        other.mkdir()
        (other / 'image.png').write_bytes(b'other')
        self.configure(other)
        with self.assertRaises(ValueError):
            self.library.reference(identifier)

    def test_reparse_marked_files_and_directories_are_never_catalogued(self):
        self.media('plain.png')
        self.media('redirected/image.png')
        original = ComfyLibrary._linked

        def reject(info):
            return original(info) or info.st_ino in rejected

        rejected = {(self.output / 'plain.png').stat().st_ino,
                    (self.output / 'redirected').stat().st_ino}
        with patch.object(ComfyLibrary, '_linked', side_effect=reject):
            self.assertEqual(self.library.list_outputs()['outputs'], [])
        self.assertTrue(original(SimpleNamespace(st_mode=0, st_file_attributes=0x400)))

    def test_replaced_link_is_rechecked_before_a_preview_reference_is_returned(self):
        path = self.media('image.png')
        identifier = self.library.list_outputs()['outputs'][0]['id']
        inode = path.stat().st_ino
        original = ComfyLibrary._linked
        with patch.object(ComfyLibrary, '_linked', side_effect=lambda info: original(info) or info.st_ino == inode):
            with self.assertRaises(ValueError):
                self.library.reference(identifier)

    def test_real_external_links_cannot_expose_media(self):
        outside = self.base / 'private'
        outside.mkdir()
        (outside / 'private.png').write_bytes(b'private')
        try:
            (self.output / 'escape').symlink_to(outside, target_is_directory=True)
            (self.output / 'escape.png').symlink_to(outside / 'private.png')
        except OSError:
            self.skipTest('This host does not permit test symlink creation.')
        self.media('allowed.png')
        result = self.library.list_outputs()
        self.assertEqual([item['name'] for item in result['outputs']], ['allowed.png'])
        self.assertEqual(result['folders'], [])
        self.configure(self.output / 'escape')
        self.assertFalse(self.library.list_outputs()['available'])

    def test_scan_and_page_are_bounded(self):
        for index in range(30):
            self.media(f'{index}.png', index)
        result = self.library.list_outputs()
        self.assertEqual(len(result['outputs']), 24)
        self.library._at = -float('inf')
        with patch('comfy_library.MAX_ENTRIES', 5):
            result = self.library.list_outputs()
        self.assertTrue(result['truncated'])
        self.assertLessEqual(result['total'], 5)

    def test_open_folder_accepts_no_browser_paths_or_arguments(self):
        with patch('comfy_library.os.startfile', create=True) as launch:
            for payload in (None, [], {'path': str(self.output)}, {'args': []}):
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    self.library.open_folder(payload)
            launch.assert_not_called()
            if os.name == 'nt':
                self.assertTrue(self.library.open_folder({})['ok'])
                launch.assert_called_once_with(str(self.output.resolve()))
            else:
                with self.assertRaises(RuntimeError):
                    self.library.open_folder({})


if __name__ == '__main__':
    unittest.main()
