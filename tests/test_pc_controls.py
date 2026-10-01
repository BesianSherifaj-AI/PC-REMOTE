"""Fixed PC launch actions; all operating-system launch calls are mocked."""
from pathlib import Path
import unittest
from unittest.mock import patch

import pc_controls


class QuickActionTests(unittest.TestCase):
    def setUp(self):
        self.windows = patch.object(pc_controls, "_SUPPORTED", True)
        self.windows.start()
        self.addCleanup(self.windows.stop)
        self.startfile = patch.object(pc_controls.os, "startfile", create=True)
        self.open_uri = self.startfile.start()
        self.addCleanup(self.startfile.stop)
        self.popen = patch.object(pc_controls.subprocess, "Popen")
        self.launch = self.popen.start()
        self.addCleanup(self.popen.stop)
        self.system = Path("C:/Windows/System32")
        self.directory = patch.object(pc_controls, "_system_directory", return_value=self.system)
        self.directory.start()
        self.addCleanup(self.directory.stop)

    def action(self, value):
        return pc_controls.perform_action({"action": "open-app", "value": value})

    def test_settings_use_only_fixed_uris(self):
        for action, uri in (
            ("settings", "ms-settings:"),
            ("display-settings", "ms-settings:display"),
            ("sound-settings", "ms-settings:sound"),
            ("bluetooth-settings", "ms-settings:bluetooth"),
            ("network-settings", "ms-settings:network-status"),
        ):
            with self.subTest(action=action):
                result = self.action(action)
                self.assertTrue(result["ok"])
                self.assertIn("on this PC", result["message"])
                self.open_uri.assert_called_once_with(uri)
                self.open_uri.reset_mock()
        self.launch.assert_not_called()

    def test_classic_apps_use_absolute_system_paths_without_shell(self):
        for action, filename in (("calculator", "calc.exe"), ("notepad", "notepad.exe"),
                                 ("task-manager", "Taskmgr.exe")):
            with self.subTest(action=action):
                self.assertTrue(self.action(action)["ok"])
                self.launch.assert_called_once_with([str(self.system / filename)],
                                                    shell=False, cwd=str(self.system))
                self.launch.reset_mock()
        self.open_uri.assert_not_called()

    def test_explorer_resolves_downloads_with_fixed_shell_namespace(self):
        for action, args in (("file-explorer", []), ("downloads", ["shell:Downloads"])):
            with self.subTest(action=action):
                self.assertTrue(self.action(action)["ok"])
                self.launch.assert_called_once_with(
                    [str(self.system.parent / "explorer.exe"), *args],
                    shell=False, cwd=str(self.system))
                self.launch.reset_mock()
        self.open_uri.assert_not_called()

    def test_request_cannot_supply_paths_commands_arguments_or_uris(self):
        for value in ("powershell.exe", "cmd /c calc", "settings;calc", "ms-settings:sound",
                      "shell:Downloads", "C:/Windows/System32/calc.exe", "shutdown", "restart",
                      "../calculator", "downloads /select,secret.txt", None, True, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.action(value)
        for extra in ({"args": []}, {"path": "C:/Windows/System32/calc.exe"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                pc_controls.perform_action({"action": "open-app", "value": "settings", **extra})
        self.open_uri.assert_not_called()
        self.launch.assert_not_called()

    def test_launch_failure_is_not_reported_as_success(self):
        self.open_uri.side_effect = OSError("No settings handler")
        with self.assertRaises(OSError):
            self.action("settings")
        self.launch.side_effect = OSError("Launch failed")
        with self.assertRaises(OSError):
            self.action("task-manager")

    def test_unsupported_platform_does_not_launch(self):
        with patch.object(pc_controls, "_SUPPORTED", False), self.assertRaises(RuntimeError):
            self.action("settings")
        self.open_uri.assert_not_called()
        self.launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
