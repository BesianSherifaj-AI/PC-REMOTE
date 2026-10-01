"""Small, fixed Windows audio/app controls for the MAIC panel (stdlib only).

The HTTP server must authenticate requests before calling perform_action.
Core Audio definitions follow Microsoft's WinSDK endpointvolume/mmdeviceapi
headers: https://github.com/microsoft/win32metadata/tree/main/generation/WinSDK/RecompiledIdlHeaders/um
"""

import ctypes
import copy
import csv
import io
import math
import os
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


_SUPPORTED = os.name == "nt"
_AUDIO_LOCK = threading.RLock()
_HRESULT = ctypes.c_int32
_BOOL = ctypes.c_int32
_DWORD = ctypes.c_uint32
_VOID = ctypes.c_void_p
_CLSCTX_ALL = 23
_RPC_E_CHANGED_MODE = 0x80010106
_HARDWARE_LOCK = threading.Lock()
_HARDWARE_CACHE = None
_HARDWARE_TIME = -float("inf")

# All launch targets belong to the server, never to the request payload.
# Settings URIs: https://learn.microsoft.com/windows/apps/develop/launch/launch-settings
_SETTINGS_APPS = {
    "settings": ("ms-settings:", "Windows Settings"),
    "display-settings": ("ms-settings:display", "Windows display settings"),
    "sound-settings": ("ms-settings:sound", "Windows sound settings"),
    "bluetooth-settings": ("ms-settings:bluetooth", "Windows Bluetooth settings"),
    # The general page also works on PCs without a Wi-Fi adapter.
    "network-settings": ("ms-settings:network-status", "Windows network settings"),
}
_SYSTEM_APPS = {
    "calculator": ("calc.exe", "Calculator"),
    "notepad": ("notepad.exe", "Notepad"),
    "task-manager": ("Taskmgr.exe", "Task Manager"),
}
_EXPLORER_APPS = {
    "file-explorer": ((), "File Explorer"),
    # Let Windows resolve the user's actual (possibly redirected) Downloads.
    "downloads": (("shell:Downloads",), "Downloads"),
}


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(value):
    return _GUID.from_buffer_copy(uuid.UUID(value).bytes_le)


_ENUMERATOR_CLSID = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
_ENUMERATOR_IID = _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
_VOLUME_IID = _guid("5CDF2C82-841E-4546-9722-0CF74078229A")

if _SUPPORTED:
    _OLE32 = ctypes.WinDLL("ole32", use_last_error=True)
    _OLE32.CoInitializeEx.argtypes = [_VOID, _DWORD]
    _OLE32.CoInitializeEx.restype = _HRESULT
    _OLE32.CoUninitialize.argtypes = []
    _OLE32.CoUninitialize.restype = None
    _OLE32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID), _VOID, _DWORD,
        ctypes.POINTER(_GUID), ctypes.POINTER(_VOID),
    ]
    _OLE32.CoCreateInstance.restype = _HRESULT


def _check(result, operation):
    if result < 0:
        raise OSError(f"{operation} failed (0x{result & 0xffffffff:08X}).")


def _call(pointer, slot, restype, argtypes, *args):
    # IUnknown-derived interfaces begin with QueryInterface/AddRef/Release.
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(_VOID))).contents
    function = ctypes.WINFUNCTYPE(restype, _VOID, *argtypes)(vtable[slot])
    return function(pointer, *args)


def _release(pointer):
    if pointer and pointer.value:
        _call(pointer, 2, _DWORD, [])


@contextmanager
def _com_initialized():
    if not _SUPPORTED:
        raise RuntimeError("PC controls require Windows.")
    # COM is initialized and balanced on every HTTP worker thread. If a caller
    # already initialized this thread as STA, retain its apartment unchanged.
    result = _OLE32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
    owns_initialization = result >= 0
    if not owns_initialization and (result & 0xffffffff) != _RPC_E_CHANGED_MODE:
        _check(result, "Audio initialization")
    try:
        yield
    finally:
        if owns_initialization:
            _OLE32.CoUninitialize()


@contextmanager
def _endpoint(flow):
    with _com_initialized():
        enumerator, device, volume = _VOID(), _VOID(), _VOID()
        try:
            _check(_OLE32.CoCreateInstance(
                ctypes.byref(_ENUMERATOR_CLSID), None, _CLSCTX_ALL,
                ctypes.byref(_ENUMERATOR_IID), ctypes.byref(enumerator),
            ), "Audio device enumeration")
            _check(_call(
                enumerator, 4, _HRESULT,
                [ctypes.c_int32, ctypes.c_int32, ctypes.POINTER(_VOID)],
                flow, 0, ctypes.byref(device),  # eConsole; eRender=0/eCapture=1
            ), "Default audio device lookup")
            _check(_call(
                device, 3, _HRESULT,
                [ctypes.POINTER(_GUID), _DWORD, _VOID, ctypes.POINTER(_VOID)],
                ctypes.byref(_VOLUME_IID), _CLSCTX_ALL, None, ctypes.byref(volume),
            ), "Audio volume control activation")
            yield volume
        finally:
            # Release all acquired COM references before ending the apartment.
            try:
                _release(volume)
            finally:
                try:
                    _release(device)
                finally:
                    _release(enumerator)


def _get_volume(pointer):
    value = ctypes.c_float()
    _check(_call(pointer, 9, _HRESULT, [ctypes.POINTER(ctypes.c_float)],
                 ctypes.byref(value)), "Read speaker volume")
    return float(value.value)


def _get_mute(pointer):
    value = _BOOL()
    _check(_call(pointer, 15, _HRESULT, [ctypes.POINTER(_BOOL)],
                 ctypes.byref(value)), "Read audio mute")
    return bool(value.value)


def _set_volume(pointer, value):
    _check(_call(pointer, 7, _HRESULT, [ctypes.c_float, _VOID],
                 ctypes.c_float(value), None), "Set speaker volume")


def _set_mute(pointer, value):
    _check(_call(pointer, 14, _HRESULT, [_BOOL, _VOID],
                 int(value), None), "Set audio mute")


def get_audio_state():
    """Read default Windows endpoints; a missing endpoint stays unavailable."""
    state = {
        "supported": _SUPPORTED,
        "speaker": {"available": False, "volume": 0, "muted": False},
        "microphone": {"available": False, "muted": False},
    }
    if not _SUPPORTED:
        return state
    with _AUDIO_LOCK:
        for name, flow in (("speaker", 0), ("microphone", 1)):
            try:
                with _endpoint(flow) as endpoint:
                    muted = _get_mute(endpoint)
                    values = {"available": True, "muted": muted}
                    if name == "speaker":
                        # Retain precision so reading then writing the same
                        # scalar does not change the underlying float value.
                        values["volume"] = max(0.0, min(100.0, _get_volume(endpoint) * 100))
                    state[name] = values
            except (OSError, RuntimeError):
                pass
    return state


def _system_directory():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, _DWORD]
    kernel32.GetSystemDirectoryW.restype = _DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise OSError("Windows system directory is unavailable.")
    return Path(buffer.value)


class _KeyInput(ctypes.Structure):
    _fields_ = [("vk", ctypes.c_uint16), ("scan", ctypes.c_uint16),
                ("flags", _DWORD), ("time", _DWORD), ("extra", ctypes.c_size_t)]


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32),
                ("data", _DWORD), ("flags", _DWORD), ("time", _DWORD),
                ("extra", ctypes.c_size_t)]


class _HardwareInput(ctypes.Structure):
    _fields_ = [("message", _DWORD), ("low", ctypes.c_uint16), ("high", ctypes.c_uint16)]


class _InputUnion(ctypes.Union):
    _fields_ = [("keyboard", _KeyInput), ("mouse", _MouseInput), ("hardware", _HardwareInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", _DWORD), ("input", _InputUnion)]


def _send_media(value):
    keys = {"play-pause": 0xB3, "next": 0xB0, "previous": 0xB1}
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = [_DWORD, ctypes.POINTER(_Input), ctypes.c_int]
    user32.SendInput.restype = _DWORD
    events = (_Input * 2)()
    for index, flags in enumerate((0, 2)):  # key-down/key-up
        events[index].type = 1  # INPUT_KEYBOARD
        events[index].input.keyboard = _KeyInput(keys[value], 0, flags, 0, 0)
    if user32.SendInput(2, events, ctypes.sizeof(_Input)) != 2:
        raise OSError("Windows did not accept this media key.")


def _read_cpu():
    try:
        import psutil
        percent = float(psutil.cpu_percent(interval=0.1))
        if math.isfinite(percent) and 0 <= percent <= 100:
            return {"available": True, "percent": percent}
    except (ImportError, OSError, ValueError):
        pass
    return {"available": False, "percent": None}


def _metric(value, minimum=0, maximum=None):
    try:
        number = float(value.strip())
        if math.isfinite(number) and number >= minimum and (maximum is None or number <= maximum):
            return number
    except (ValueError, AttributeError):
        pass
    return None


def _read_gpu():
    executable = shutil.which("nvidia-smi")
    if not executable:
        return {"available": False, "devices": []}
    try:
        result = subprocess.run([
            executable,
            "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
            "--format=csv,noheader,nounits",
        ], capture_output=True, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            return {"available": False, "devices": []}
        devices = []
        for row in csv.reader(io.StringIO(result.stdout)):
            if len(row) != 5 or not row[0].strip():
                continue
            name = "".join(c for c in row[0].strip() if ord(c) >= 32)[:120]
            devices.append({"name": name, "utilization": _metric(row[1], maximum=100),
                            "memoryUsedMB": _metric(row[2]), "memoryTotalMB": _metric(row[3]),
                            "temperatureC": _metric(row[4], minimum=-50, maximum=200)})
        return {"available": bool(devices), "devices": devices}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"available": False, "devices": []}


def hardware_status():
    """Return CPU/GPU metrics only, cached for five seconds; no process details."""
    global _HARDWARE_CACHE, _HARDWARE_TIME
    with _HARDWARE_LOCK:
        if _HARDWARE_CACHE is None or time.monotonic() - _HARDWARE_TIME >= 5:
            _HARDWARE_CACHE = {"ok": True, "cpu": _read_cpu(), "gpu": _read_gpu()}
            _HARDWARE_TIME = time.monotonic()
        return copy.deepcopy(_HARDWARE_CACHE)


def perform_action(payload):
    """Perform exactly one allowlisted action; never evaluate incoming commands."""
    if not isinstance(payload, dict) or set(payload) != {"action", "value"}:
        raise ValueError("An action must contain exactly action and value.")
    action, value = payload["action"], payload["value"]
    if action == "speaker-volume":
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not 0 <= value <= 100 or not math.isfinite(value)):
            raise ValueError("Speaker volume must be a number from 0 to 100.")
    elif action in ("speaker-mute", "microphone-mute"):
        if not isinstance(value, bool):
            raise ValueError("Mute value must be true or false.")
    elif action == "open-app":
        if (not isinstance(value, str)
                or value not in _SETTINGS_APPS and value not in _SYSTEM_APPS and value not in _EXPLORER_APPS):
            raise ValueError("This application is not allowed.")
    elif action == "media":
        if not isinstance(value, str) or value not in ("play-pause", "next", "previous"):
            raise ValueError("This media key is not allowed.")
    else:
        raise ValueError("This PC action is not allowed.")
    if not _SUPPORTED:
        raise RuntimeError("PC controls require Windows.")

    if action == "media":
        _send_media(value)
        return {"ok": True, "message": "Media key sent to the PC's media player."}

    if action == "open-app":
        if value in _SETTINGS_APPS:
            uri, label = _SETTINGS_APPS[value]
            os.startfile(uri)
        else:
            system_directory = _system_directory()
            if value in _SYSTEM_APPS:
                name, label = _SYSTEM_APPS[value]
                arguments = [str(system_directory / name)]
            else:
                extra_arguments, label = _EXPLORER_APPS[value]
                arguments = [str(system_directory.parent / "explorer.exe"), *extra_arguments]
            subprocess.Popen(arguments,
                             shell=False, cwd=str(system_directory))
        # Launch acceptance does not prove that Windows granted foreground focus.
        return {"ok": True, "message": f"Requested {label} on this PC."}

    with _AUDIO_LOCK:
        flow = 1 if action == "microphone-mute" else 0
        with _endpoint(flow) as endpoint:
            if action == "speaker-volume":
                _set_volume(endpoint, value / 100)
            else:
                _set_mute(endpoint, value)
        return {"ok": True, "state": get_audio_state()}
