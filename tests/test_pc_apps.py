import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pc_apps
import pc_controls


def shortcut(name, target, **changes):
    row = {"name": name, "path": "C:\\StartMenu\\" + name + ".lnk",
           "target": target, "arguments": "", "exists": True}
    row.update(changes)
    return row


def fixture():
    return {
        "startApps": [
            {"name": "Notepad", "aumid": "Microsoft.Notepad_abc!App"},
            {"name": "Good App", "aumid": "Vendor.GoodApp"},
            {"name": "Broken App", "aumid": "Vendor.BrokenApp"},
            {"name": "Script App", "aumid": "Vendor.ScriptApp"},
            {"name": "Updater", "aumid": "Vendor.Updater"},
        ],
        "shortcuts": [
            shortcut("Notepad", r"C:\Windows\notepad.exe"),
            shortcut("Good App", r"C:\Program Files\Good\good.exe"),
            shortcut("Good App duplicate", r"C:\Program Files\Good\good.exe"),
            shortcut("Broken App", r"C:\Missing\broken.exe", exists=False),
            shortcut("Script App", r"C:\Windows\System32\cmd.exe", arguments="/c run.cmd"),
            shortcut("Uninstall Good App", r"C:\Good\unins000.exe"),
            shortcut("Maintenance Tool", r"C:\Good\updater.exe"),
        ],
    }


class AppRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.launcher, self.activator = Mock(), Mock(return_value=False)
        self.windows = [{"hwnd": 123, "pid": 5, "exe": r"C:\Program Files\Good\good.exe",
                         "aumid": "", "active": True}]
        self.registry = pc_apps.AppRegistry(
            self.root, discovery=fixture, window_source=lambda: self.windows,
            launcher=self.launcher, activator=self.activator, process_source=lambda: [])

    def test_filter_deduplicate_stable_ids_and_no_private_fields(self):
        apps = self.registry.catalog()["apps"]
        self.assertEqual({a["name"] for a in apps}, {"Good App", "Notepad"})
        reordered = fixture()
        reordered["shortcuts"].reverse()
        other = pc_apps._build_entries(reordered)
        self.assertEqual({a["id"] for a in apps}, set(other))
        for app in apps:
            self.assertRegex(app["id"], r"^app_[0-9a-f]{24}$")
            self.assertEqual(set(app), {"id", "name", "kind", "running", "hasWindow", "active", "favourite"})
        good = next(a for a in apps if a["name"] == "Good App")
        self.assertTrue(good["running"] and good["active"])

    def test_commands_paths_unknown_ids_and_extra_arguments_rejected(self):
        known = self.registry.catalog()["apps"][0]["id"]
        invalid = [
            {"action": "open", "id": "C:\\Windows\\cmd.exe"},
            {"action": "open", "id": "app_" + "0" * 24},
            {"action": "shell", "id": known},
            {"action": "open", "id": known, "arguments": "& calc"},
            {"action": "open", "id": [known]},
            {"action": "open", "id": "app_" + "1" * 24 + ";calc"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.registry.action(payload)
        self.launcher.assert_not_called()
        self.activator.assert_not_called()

    def test_chat_apps_remain_ordinary_catalog_entries_without_a_special_workspace(self):
        discovery = {"startApps": [
            {"name": "ChatGPT", "aumid": "OpenAI.Codex_2p2nqsd0c76g0!App"},
            {"name": "ChatGPT (Beta)", "aumid": "OpenAI.CodexBeta_2p2nqsd0c76g0!App"},
            {"name": "Codex", "aumid": "OtherVendor.Codex_123!App"},
        ], "shortcuts": []}
        registry = pc_apps.AppRegistry(self.root, discovery=lambda: discovery,
                                       window_source=lambda: [], process_source=lambda: [],
                                       launcher=self.launcher)
        catalog = registry.catalog()
        self.assertNotIn("integrations", catalog)
        self.assertEqual({a["name"] for a in catalog["apps"]}, {"ChatGPT", "ChatGPT (Beta)", "Codex"})
        app_id = next(a["id"] for a in catalog["apps"] if a["name"] == "ChatGPT")
        self.assertNotIn("aumid", json.dumps(catalog))
        self.launcher.assert_not_called()
        registry.action({"action": "open", "id": app_id})
        self.assertEqual(self.launcher.call_args.args[0]["aumid"], "OpenAI.Codex_2p2nqsd0c76g0!App")

    def test_admin_and_maintenance_tools_are_excluded_without_losing_normal_launchers(self):
        maintenance_names = ('Administrative Tools', 'Windows Tools', 'Defragment and Optimize Drives',
                             'dfrgui', 'Application Verifier', 'Adobe Application Manager',
                             'iSCSI Initiator', 'Event Viewer', 'Task Scheduler')
        discovery = {'startApps': [{'name': name, 'aumid': 'Vendor.Tool' + str(index)}
                                  for index, name in enumerate(maintenance_names)],
                     'shortcuts': [shortcut('Utility ' + str(index), 'C:/Windows/' + target)
                                   for index, target in enumerate(('dfrgui.exe', 'appverif.exe',
                                                                  'iscsicpl.exe', 'mmc.exe', 'regedit.exe'))]}
        launchers = {'Adobe Creative Cloud': 'CreativeCloud.exe', 'Epic Games Launcher': 'EpicGamesLauncher.exe',
                     'Steam': 'steam.exe', 'LM Studio': 'LM Studio.exe', 'Comfy Desktop': 'Comfy Desktop.exe',
                     'Task Manager': 'Taskmgr.exe', 'Adobe Photoshop': 'Photoshop.exe'}
        discovery['shortcuts'] += [shortcut(name, 'C:/Apps/' + exe) for name, exe in launchers.items()]
        entries = pc_apps._build_entries(discovery)
        self.assertEqual({entry['name'] for entry in entries.values()}, set(launchers))

    def test_known_app_launch_uses_internal_record_only(self):
        app = next(a for a in self.registry.catalog()["apps"] if a["name"] == "Good App")
        result = self.registry.action({"action": "open", "id": app["id"]})
        self.assertTrue(result["ok"])
        self.assertEqual(self.launcher.call_args.args[0]["exe"], r"C:\Program Files\Good\good.exe")
        self.assertNotIn("exe", result)

    def test_foreground_denial_and_no_open_window_are_reported(self):
        app = next(a for a in self.registry.catalog()["apps"] if a["name"] == "Good App")
        result = self.registry.action({"action": "activate", "id": app["id"]})
        self.assertTrue(result["ok"])
        self.assertFalse(result["activated"])
        self.activator.assert_called_once_with(123)
        self.windows = []
        self.assertFalse(self.registry.action({"action": "activate", "id": app["id"]})["activated"])
        self.assertEqual(self.activator.call_count, 1)

    def test_favourites_validate_and_survive_restart(self):
        identifier = self.registry.catalog()["apps"][0]["id"]
        response = self.registry.favourites({"ids": [identifier, identifier]})
        self.assertEqual(sum(a["favourite"] for a in response["apps"]), 1)
        saved = json.loads((self.root / ".runtime" / "favourites.json").read_text())
        self.assertEqual(saved, {"ids": [identifier]})
        again = pc_apps.AppRegistry(self.root, discovery=fixture, window_source=lambda: [], process_source=lambda: [])
        self.assertEqual(sum(a["favourite"] for a in again.catalog()["apps"]), 1)
        for invalid in ({"ids": ["C:/evil"]}, {"ids": ["app_" + "0" * 24]}, {"ids": [], "cmd": "calc"}):
            with self.assertRaises(ValueError):
                self.registry.favourites(invalid)
        self.assertEqual(json.loads((self.root / ".runtime" / "favourites.json").read_text()), saved)

    def test_packaged_aumid_running_match_and_window_cache(self):
        source = Mock(return_value=[{"hwnd": 4, "aumid": "Microsoft.Notepad_abc!App", "exe": "", "active": False}])
        process_source = Mock(return_value=[])
        registry = pc_apps.AppRegistry(self.root, discovery=fixture, window_source=source, process_source=process_source)
        registry.state()
        apps = registry.state()["apps"]
        source.assert_called_once()
        process_source.assert_called_once()
        self.assertTrue(next(a for a in apps if a["name"] == "Notepad")["running"])

    def test_background_app_is_running_without_an_activatable_window(self):
        processes = Mock(return_value=[{'pid': 42, 'exe': r'C:\Program Files\Good\good.exe', 'aumid': ''},
                                       {'pid': 43, 'exe': '', 'aumid': 'Microsoft.Notepad_abc!App'}])
        registry = pc_apps.AppRegistry(self.root, discovery=fixture, window_source=lambda: [],
                                       process_source=processes, activator=self.activator)
        apps = registry.state()['apps']
        good = next(a for a in apps if a['name'] == 'Good App')
        self.assertTrue(good['running'])
        self.assertFalse(good['hasWindow'])
        self.assertFalse(good['active'])
        notepad = next(a for a in apps if a['name'] == 'Notepad')
        self.assertTrue(notepad['running'])
        self.assertFalse(notepad['hasWindow'])
        registry.state()
        processes.assert_called_once()
        result = registry.action({'action': 'activate', 'id': good['id']})
        self.assertFalse(result['activated'])
        self.activator.assert_not_called()


class HardwareAndMediaTests(unittest.TestCase):
    def test_missing_gpu_and_driver_failure_are_unavailable(self):
        with patch.object(pc_controls.shutil, "which", return_value=None), patch.object(pc_controls.subprocess, "run") as run:
            self.assertEqual(pc_controls._read_gpu(), {"available": False, "devices": []})
            run.assert_not_called()
        with patch.object(pc_controls.shutil, "which", return_value="nvidia-smi"), patch.object(pc_controls.subprocess, "run", return_value=Mock(returncode=1)):
            self.assertFalse(pc_controls._read_gpu()["available"])

    def test_gpu_projection_excludes_other_information_and_preserves_unknown_metrics(self):
        sample = Mock(returncode=0, stdout="NVIDIA Example, 12, 1024, 8192, 45\nNVIDIA Other, N/A, 0, 8192, N/A\n")
        with patch.object(pc_controls.shutil, "which", return_value="nvidia-smi"), patch.object(pc_controls.subprocess, "run", return_value=sample) as run:
            gpu = pc_controls._read_gpu()
            self.assertEqual(len(gpu["devices"]), 2)
            self.assertEqual(gpu["devices"][0]["utilization"], 12)
            self.assertIsNone(gpu["devices"][1]["utilization"])
            self.assertNotIn("serial", str(run.call_args))
            self.assertNotIn("process", str(run.call_args))

    def test_hardware_cache_is_bounded_and_copies_result(self):
        with patch.object(pc_controls, "_HARDWARE_CACHE", None), patch.object(pc_controls, "_read_cpu", return_value={"available": False, "percent": None}) as cpu, patch.object(pc_controls, "_read_gpu", return_value={"available": False, "devices": []}) as gpu:
            first = pc_controls.hardware_status()
            first["gpu"]["devices"].append({"name": "changed"})
            self.assertEqual(pc_controls.hardware_status()["gpu"]["devices"], [])
            cpu.assert_called_once()
            gpu.assert_called_once()

    def test_media_allowlist_is_dispatched_without_real_key_events(self):
        with patch.object(pc_controls, "_SUPPORTED", True), patch.object(pc_controls, "_send_media") as send:
            for value in ("play-pause", "next", "previous"):
                self.assertTrue(pc_controls.perform_action({"action": "media", "value": value})["ok"])
            self.assertEqual(send.call_count, 3)
            for value in ("cmd", "next;calc", "volume-up", True, []):
                with self.assertRaises(ValueError):
                    pc_controls.perform_action({"action": "media", "value": value})
            self.assertEqual(send.call_count, 3)


if __name__ == "__main__":
    unittest.main()
