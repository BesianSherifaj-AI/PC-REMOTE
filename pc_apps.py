"""Windows app discovery and activation through a server-owned opaque registry."""

import ctypes
import hashlib
import json
import ntpath
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from ctypes import wintypes


_DISCOVERY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$start = @(Get-StartApps | ForEach-Object { @{name=$_.Name; aumid=$_.AppID} })
$folders = @([Environment]::GetFolderPath('StartMenu'), [Environment]::GetFolderPath('CommonStartMenu'))
$shell = New-Object -ComObject WScript.Shell
$links = @()
try {
  foreach ($folder in $folders) {
    foreach ($file in @(Get-ChildItem -LiteralPath $folder -Filter '*.lnk' -File -Recurse -ErrorAction SilentlyContinue)) {
      $link = $shell.CreateShortcut($file.FullName)
      try {
        $links += @{name=$file.BaseName; path=$file.FullName; target=$link.TargetPath;
                    arguments=$link.Arguments; exists=([bool]($link.TargetPath -and (Test-Path -LiteralPath $link.TargetPath)))}
      } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($link) }
    }
  }
} finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
@{startApps=$start; shortcuts=$links} | ConvertTo-Json -Depth 4 -Compress
"""
_MAINTENANCE = re.compile(
    r"\b(uninstall(?:er)?|install(?:er)?|setup|updat(?:e|er)|repair|remove|reset|"
    r"cleanup|troubleshoot(?:er)?|recovery|configuration|configure)\b", re.I)
_MAINTENANCE_NAMES = re.compile(
    r"\b(?:administrative tools|windows tools|defragment(?: and optimize drives)?|dfrgui|"
    r"application verifier|adobe application manager|iscsi initiator|component services|"
    r"computer management|disk management|event viewer|local security policy|"
    r"odbc data sources|performance monitor|print management|registry editor|"
    r"resource monitor|system information|task scheduler|windows memory diagnostic)\b", re.I)
_SCRIPT_ARGUMENT = re.compile(r"\.(?:ps1|bat|cmd|vbs|js|py|sh)(?:\s|[\"']|$)", re.I)
_INTERPRETERS = {"cmd.exe", "powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe",
                 "mshta.exe", "rundll32.exe", "regsvr32.exe", "python.exe", "pythonw.exe",
                 "node.exe", "bash.exe", "wsl.exe", "msiexec.exe"}
_MAINTENANCE_EXES = {"dfrgui.exe", "appverif.exe", "verifier.exe", "iscsicpl.exe",
                     "cleanmgr.exe", "mmc.exe", "compmgmtlauncher.exe", "regedit.exe",
                     "resmon.exe", "mdsched.exe", "odbcad32.exe", "msconfig.exe",
                     "msinfo32.exe"}
_OPAQUE_ID = re.compile(r"app_[0-9a-f]{24}\Z")


def _name_key(value):
    return re.sub(r"[^\w]", "", value.casefold())


def _safe_name(value):
    return (isinstance(value, str) and 0 < len(value.strip()) <= 160
            and not any(ord(c) < 32 for c in value) and not _MAINTENANCE.search(value)
            and not _MAINTENANCE_NAMES.search(value))


def _excluded_executable(target):
    basename = ntpath.basename(target).casefold()
    return (basename in _INTERPRETERS or basename in _MAINTENANCE_EXES
            or bool(_MAINTENANCE.search(basename))
            or bool(re.match(r"^(?:unins|setup|updat|maintenancetool)", basename, re.I)))


def _path_key(value):
    return ntpath.normcase(ntpath.normpath(value)) if value else ""


def _discover_apps():
    if os.name != "nt":
        return {"startApps": [], "shortcuts": []}
    from pc_controls import _system_directory
    powershell = _system_directory() / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command",
                             _DISCOVERY_SCRIPT], capture_output=True, timeout=25,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("Windows app discovery could not complete.")
    # PowerShell 5 writes UTF-8 when stdout is redirected only after explicitly
    # selecting its encoding. The fixed script below receives that setting.
    text = result.stdout.decode("utf-8-sig", errors="replace")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise RuntimeError("Windows returned an invalid app catalog.")
    return value


def _build_entries(discovery):
    """Prefer packaged identities, then executable shortcuts; dedupe by identity/name."""
    candidates = []
    for row in discovery.get("startApps", []):
        if not isinstance(row, dict) or not _safe_name(row.get("name")):
            continue
        aumid = row.get("aumid", "")
        if (isinstance(aumid, str) and re.fullmatch(r"[\w.-]+![\w.-]+", aumid)
                and len(aumid) <= 256):
            candidates.append({"name": row["name"].strip(), "kind": "packaged",
                               "aumid": aumid, "exe": "", "identity": "aumid:" + aumid.casefold()})
    for row in discovery.get("shortcuts", []):
        if not isinstance(row, dict) or not _safe_name(row.get("name")):
            continue
        path, target, arguments = row.get("path", ""), row.get("target", ""), row.get("arguments", "")
        if (not isinstance(path, str) or not isinstance(target, str) or not isinstance(arguments, str)
                or not ntpath.isabs(path) or not ntpath.isabs(target)
                or ntpath.splitext(path)[1].lower() != ".lnk"
                or ntpath.splitext(target)[1].lower() != ".exe" or row.get("exists") is not True
                or _excluded_executable(target)
                or _SCRIPT_ARGUMENT.search(arguments)
                or any(ord(c) < 32 for c in path + target + arguments)):
            continue
        candidates.append({"name": row["name"].strip(), "kind": "desktop", "path": path,
                           "exe": target, "identity": "exe:" + _path_key(target) + "\0" + arguments})
    # Keep named Start-menu shell entries that have no conventional shortcut.
    # They are launched only through their discovered AppsFolder identity.
    known_names = {_name_key(row["name"]) for row in candidates}
    # A broken or excluded shortcut must not sneak back in via Get-StartApps.
    known_names.update(_name_key(row["name"]) for row in discovery.get("shortcuts", [])
                       if isinstance(row, dict) and isinstance(row.get("name"), str))
    for row in discovery.get("startApps", []):
        if not isinstance(row, dict) or not _safe_name(row.get("name")):
            continue
        name, aumid = row["name"].strip(), row.get("aumid", "")
        if _name_key(name) in known_names or not isinstance(aumid, str) or not aumid or len(aumid) > 512:
            continue
        if any(ord(c) < 32 for c in aumid) or "!" in aumid:
            continue
        # Absolute AppIDs must identify a real executable, never scripts or
        # arbitrary shell expressions. Other IDs come from Get-StartApps.
        exe = ""
        if ntpath.isabs(aumid):
            if (ntpath.splitext(aumid)[1].casefold() != ".exe"
                    or _excluded_executable(aumid) or not Path(aumid).is_file()):
                continue
            exe = aumid
        elif any(c in aumid for c in "\r\n;&|<>\"'`") or _MAINTENANCE.search(aumid):
            continue
        candidates.append({"name": name, "kind": "desktop", "aumid": aumid,
                           "exe": exe, "identity": "aumid:" + aumid.casefold()})
    entries, identities, names = {}, set(), set()
    for entry in candidates:
        identity, name = entry["identity"], _name_key(entry["name"])
        if identity in identities or name in names:
            # An equivalent desktop shortcut can supply an executable mapping
            # to a packaged entry without changing its stable public ID.
            for existing in entries.values():
                if _name_key(existing["name"]) == name and not existing.get("exe"):
                    existing["exe"] = entry.get("exe", "")
            continue
        identifier = "app_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        entry["id"] = identifier
        entries[identifier] = entry
        identities.add(identity)
        names.add(name)
    return entries


def _identity_reader():
    """Cache executable/package identity while querying limited-information handles."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                           ctypes.POINTER(wintypes.DWORD)]
    k.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k.GetApplicationUserModelId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
    k.GetApplicationUserModelId.restype = wintypes.LONG
    identities = {}

    def identity(pid):
        if pid in identities:
            return identities[pid]
        result = {"exe": "", "aumid": ""}
        handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            try:
                size = wintypes.DWORD(32768)
                buffer = ctypes.create_unicode_buffer(size.value)
                if k.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    result["exe"] = buffer.value
                count = wintypes.UINT(1024)
                app_buffer = ctypes.create_unicode_buffer(count.value)
                if k.GetApplicationUserModelId(handle, ctypes.byref(count), app_buffer) == 0:
                    result["aumid"] = app_buffer.value
            finally:
                k.CloseHandle(handle)
        identities[pid] = result
        return result

    return identity


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                ("filename", wintypes.WCHAR * 260)]


def _list_processes():
    """Include background/tray app identities without reading arguments or titles."""
    if os.name != "nt":
        return []
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        getattr(k, name).argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
        getattr(k, name).restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    handle = k.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if not handle or handle == ctypes.c_void_p(-1).value:
        return []
    read_identity, processes = _identity_reader(), []
    entry = _ProcessEntry()
    entry.size = ctypes.sizeof(entry)
    try:
        more = k.Process32FirstW(handle, ctypes.byref(entry))
        while more:
            values = read_identity(entry.pid)
            if values["exe"] or values["aumid"]:
                processes.append({"pid": entry.pid, **values})
            more = k.Process32NextW(handle, ctypes.byref(entry))
    finally:
        k.CloseHandle(handle)
    return processes


def _list_windows():
    """Read only window/process identities; never read window titles or command lines."""
    if os.name != "nt":
        return []
    u = ctypes.WinDLL("user32", use_last_error=True)
    d = ctypes.WinDLL("dwmapi", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    u.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    u.EnumWindows.restype = wintypes.BOOL
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsWindowVisible.restype = wintypes.BOOL
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextLengthW.restype = ctypes.c_int
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.GetForegroundWindow.argtypes = []
    u.GetForegroundWindow.restype = wintypes.HWND
    u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetClassNameW.restype = ctypes.c_int
    d.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
    d.DwmGetWindowAttribute.restype = ctypes.c_int32
    foreground = u.GetForegroundWindow()
    windows, identity = [], _identity_reader()

    @callback_type
    def visitor(hwnd, _):
        if not u.IsWindowVisible(hwnd) or not u.GetWindowTextLengthW(hwnd):
            return True
        cloaked = wintypes.DWORD()
        if d.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value:
            return True
        klass = ctypes.create_unicode_buffer(128)
        u.GetClassNameW(hwnd, klass, 128)
        if klass.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            return True
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        item = {"hwnd": hwnd, "pid": pid.value, "active": hwnd == foreground, **identity(pid.value)}
        windows.append(item)
        return True

    if not u.EnumWindows(visitor, 0):
        raise OSError("Windows app-window discovery failed.")
    return windows


def _launch_entry(entry):
    if os.name != "nt":
        raise RuntimeError("App launching requires Windows.")
    if entry.get("path"):
        if not Path(entry["path"]).is_file():
            raise RuntimeError("This installed app shortcut is no longer available.")
        os.startfile(entry["path"], "open")
    else:
        os.startfile("shell:AppsFolder\\" + entry["aumid"], "open")


def _activate_window(hwnd):
    if os.name != "nt":
        return False
    u = ctypes.WinDLL("user32", use_last_error=True)
    for name in ("IsWindow", "IsIconic", "SetForegroundWindow"):
        getattr(u, name).argtypes = [wintypes.HWND]
        getattr(u, name).restype = wintypes.BOOL
    u.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    u.ShowWindowAsync.restype = wintypes.BOOL
    u.GetForegroundWindow.argtypes = []
    u.GetForegroundWindow.restype = wintypes.HWND
    if not u.IsWindow(hwnd):
        return False
    if u.IsIconic(hwnd):
        u.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
    u.SetForegroundWindow(hwnd)
    return u.GetForegroundWindow() == hwnd


class AppRegistry:
    def __init__(self, root, discovery=None, window_source=None, launcher=None, activator=None, process_source=None):
        self.root = Path(root)
        self._discover = discovery or _discover_apps
        self._window_source = window_source or _list_windows
        self._process_source = process_source or _list_processes
        self._launcher = launcher or _launch_entry
        self._activator = activator or _activate_window
        self._entries = None
        self._catalog_time = -float("inf")
        self._windows = []
        self._processes = []
        self._window_time = -float("inf")
        self._lock = threading.RLock()
        self._favourites_path = self.root / ".runtime" / "favourites.json"
        self._favourites = set()
        try:
            value = json.loads(self._favourites_path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("ids"), list):
                self._favourites = {i for i in value["ids"] if isinstance(i, str) and _OPAQUE_ID.fullmatch(i)}
        except (OSError, ValueError):
            pass

    def _ensure_catalog(self):
        if self._entries is None or time.monotonic() - self._catalog_time >= 60:
            entries = _build_entries(self._discover())
            self._entries = entries
            self._catalog_time = time.monotonic()

    def _refresh_windows(self, force=False):
        if force or time.monotonic() - self._window_time >= 2:
            self._windows = self._window_source()
            self._processes = self._process_source()
            self._window_time = time.monotonic()

    def _matching(self, entry, identities=None):
        aumid, exe = entry.get("aumid", "").casefold(), _path_key(entry.get("exe", ""))
        return [w for w in (self._windows if identities is None else identities)
                if (aumid and w.get("aumid", "").casefold() == aumid)
                or (exe and _path_key(w.get("exe", "")) == exe)]

    def catalog(self):
        with self._lock:
            self._ensure_catalog()
            self._refresh_windows()
            apps = []
            for entry in sorted(self._entries.values(), key=lambda e: e["name"].casefold()):
                windows = self._matching(entry)
                processes = self._matching(entry, self._processes)
                apps.append({"id": entry["id"], "name": entry["name"], "kind": entry["kind"],
                             "running": bool(windows or processes), "hasWindow": bool(windows),
                             "active": any(w.get("active") for w in windows),
                             "favourite": entry["id"] in self._favourites})
            return {"ok": True, "apps": apps, "count": len(apps),
                    "runningCount": sum(a["running"] for a in apps), "supported": os.name == "nt"}

    def state(self):
        return self.catalog()

    def action(self, payload):
        if (not isinstance(payload, dict) or set(payload) != {"action", "id"}
                or payload.get("action") not in ("open", "activate")
                or not isinstance(payload.get("id"), str) or not _OPAQUE_ID.fullmatch(payload["id"])):
            raise ValueError("Select an installed app by its known ID and an open or activate action.")
        with self._lock:
            self._ensure_catalog()
            entry = self._entries.get(payload["id"])
            if entry is None:
                raise ValueError("This app is not in the installed app catalog.")
            if payload["action"] == "open":
                self._launcher(entry)
                self._window_time = -float("inf")
                return {"ok": True, "message": f"Launch requested for {entry['name']} on this PC."}
            self._refresh_windows(force=True)
            windows = self._matching(entry)
            if not windows:
                return {"ok": True, "activated": False, "message": "This app has no open window to activate."}
            window = next((w for w in windows if w.get("active")), windows[0])
            activated = bool(self._activator(window["hwnd"]))
            self._window_time = -float("inf")
            return {"ok": True, "activated": activated,
                    "message": (f"Activated {entry['name']}." if activated
                                else "Windows did not allow foreground switching; select the app on the PC.")}

    def favourites(self, payload):
        if (not isinstance(payload, dict) or set(payload) != {"ids"}
                or not isinstance(payload["ids"], list) or len(payload["ids"]) > 512
                or any(not isinstance(i, str) or not _OPAQUE_ID.fullmatch(i) for i in payload["ids"])):
            raise ValueError("Favourites must be a list of installed app IDs.")
        with self._lock:
            self._ensure_catalog()
            ids = set(payload["ids"])
            if not ids.issubset(self._entries):
                raise ValueError("A favourite app is not in the installed app catalog.")
            self._favourites_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self._favourites_path.parent,
                                                 prefix="favourites-", suffix=".tmp", delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump({"ids": sorted(ids)}, handle)
                temporary.replace(self._favourites_path)
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
            self._favourites = ids
            return self.catalog()
