"""Windows SendInput/clipboard adapter. No focus activation, no clipboard content logs."""

import ctypes as C
import logging
import os
import time
from ctypes import wintypes as W
from dataclasses import dataclass

from .key_policy import INPUT_TAG

logger = logging.getLogger(__name__)
MAX_CLIPBOARD_BYTES = 16 * 1024 * 1024
# Only formats whose handles are documented global-memory blocks.
SAFE_FORMATS = {1, 7, 8, 13, 15, 16, 17}
SAFE_REGISTERED = {"HTML Format", "Rich Text Format", "Rich Text Format Without Objects", "UTF8_STRING"}


class GUIInfo(C.Structure):
    _fields_ = [
        ("cbSize", W.DWORD),
        ("flags", W.DWORD),
        ("hwndActive", W.HWND),
        ("hwndFocus", W.HWND),
        ("hwndCapture", W.HWND),
        ("hwndMenuOwner", W.HWND),
        ("hwndMoveSize", W.HWND),
        ("hwndCaret", W.HWND),
        ("rcCaret", W.RECT),
    ]


class KeyboardInput(C.Structure):
    _fields_ = [
        ("wVk", W.WORD),
        ("wScan", W.WORD),
        ("dwFlags", W.DWORD),
        ("time", W.DWORD),
        ("dwExtraInfo", C.c_size_t),
    ]


class MouseInput(C.Structure):
    _fields_ = [
        ("dx", W.LONG),
        ("dy", W.LONG),
        ("mouseData", W.DWORD),
        ("dwFlags", W.DWORD),
        ("time", W.DWORD),
        ("dwExtraInfo", C.c_size_t),
    ]


class InputUnion(C.Union):
    _fields_ = [("ki", KeyboardInput), ("mi", MouseInput)]


class Input(C.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", W.DWORD), ("value", InputUnion)]


@dataclass(frozen=True)
class Target:
    window: int
    process: int
    focus: int
    runtime_id: tuple | None = None


@dataclass
class ClipboardLease:
    sequence: int
    handles: list


class WindowsInsertion:
    def __init__(self):
        self.u = C.WinDLL("user32", use_last_error=True)
        self.k = C.WinDLL("kernel32", use_last_error=True)
        specs = {
            "GetForegroundWindow": ([], W.HWND),
            "GetWindowThreadProcessId": ([W.HWND, C.POINTER(W.DWORD)], W.DWORD),
            "GetGUIThreadInfo": ([W.DWORD, C.POINTER(GUIInfo)], W.BOOL),
            "GetAsyncKeyState": ([C.c_int], C.c_short),
            "SendInput": ([W.UINT, C.POINTER(Input), C.c_int], W.UINT),
            "OpenClipboard": ([W.HWND], W.BOOL),
            "CloseClipboard": ([], W.BOOL),
            "EmptyClipboard": ([], W.BOOL),
            "EnumClipboardFormats": ([W.UINT], W.UINT),
            "GetClipboardData": ([W.UINT], W.HANDLE),
            "SetClipboardData": ([W.UINT, W.HANDLE], W.HANDLE),
            "GetClipboardSequenceNumber": ([], W.DWORD),
            "GetClipboardOwner": ([], W.HWND),
            "GetClipboardFormatNameW": ([W.UINT, W.LPWSTR, C.c_int], C.c_int),
            "CreateWindowExW": (
                [
                    W.DWORD,
                    W.LPCWSTR,
                    W.LPCWSTR,
                    W.DWORD,
                    C.c_int,
                    C.c_int,
                    C.c_int,
                    C.c_int,
                    W.HWND,
                    W.HMENU,
                    W.HINSTANCE,
                    W.LPVOID,
                ],
                W.HWND,
            ),
            "DestroyWindow": ([W.HWND], W.BOOL),
        }
        for name, (args, result) in specs.items():
            fn = getattr(self.u, name)
            fn.argtypes, fn.restype = args, result
        for name, args, result in (
            ("GlobalAlloc", [W.UINT, C.c_size_t], W.HGLOBAL),
            ("GlobalSize", [W.HGLOBAL], C.c_size_t),
            ("GlobalLock", [W.HGLOBAL], W.LPVOID),
            ("GlobalUnlock", [W.HGLOBAL], W.BOOL),
            ("GlobalFree", [W.HGLOBAL], W.HGLOBAL),
        ):
            fn = getattr(self.k, name)
            fn.argtypes, fn.restype = args, result
        self.window = None
        self.automation = None
        self.com_initialized = False

    def capture_target(self):
        hwnd = self.u.GetForegroundWindow()
        pid = W.DWORD()
        thread = self.u.GetWindowThreadProcessId(hwnd, C.byref(pid))
        info = GUIInfo(cbSize=C.sizeof(GUIInfo))
        if not hwnd or pid.value == os.getpid() or not self.u.GetGUIThreadInfo(thread, C.byref(info)):
            return None
        if not info.hwndFocus:
            return None
        return Target(hwnd, pid.value, info.hwndFocus)

    def _focus_id(self):
        if self.automation is None:
            import comtypes
            from comtypes.client import CreateObject, GetModule

            comtypes.CoInitializeEx(2)
            self.com_initialized = True
            GetModule("UIAutomationCore.dll")
            from comtypes.gen.UIAutomationClient import CUIAutomation

            self.automation = CreateObject(CUIAutomation)
        element = self.automation.GetFocusedElement()
        return tuple(element.GetRuntimeId()) if element else None

    def prepare_target(self, target):
        if target is None or self.capture_target() != target:
            return None
        try:
            runtime_id = self._focus_id()
        except Exception:
            logger.warning("UI Automation unavailable; using window/input guards", exc_info=True)
            runtime_id = None
        # Legacy controls may not implement UIA; HWND plus input guard remains available.
        return Target(target.window, target.process, target.focus, runtime_id)

    def target_matches(self, target):
        current = self.capture_target()
        if current is None or (current.window, current.process, current.focus) != (
            target.window,
            target.process,
            target.focus,
        ):
            return False
        if target.runtime_id is not None:
            try:
                return self._focus_id() == target.runtime_id
            except Exception:
                logger.warning("Could not verify UI Automation focus; insertion blocked", exc_info=True)
                return False
        return True

    def dismiss_ptt_menu(self, target):
        """Right Alt may leave a native menu active; dismiss only on the same target.

        Called only with an unchanged input guard. Never activates the window.
        """
        current = self.capture_target()
        if current is None or (current.window, current.process, current.focus) != (
            target.window,
            target.process,
            target.focus,
        ):
            return
        pid = W.DWORD()
        thread = self.u.GetWindowThreadProcessId(target.window, C.byref(pid))
        info = GUIInfo(cbSize=C.sizeof(GUIInfo))
        if self.u.GetGUIThreadInfo(thread, C.byref(info)) and info.flags & 0x4:
            self._send([self._key(0x1B), self._key(0x1B, flags=2)])
            time.sleep(0.05)

    def wait_modifiers(self, valid):
        deadline = time.monotonic() + 1.0
        while any(self.u.GetAsyncKeyState(key) & 0x8000 for key in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
            if not valid() or time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        return valid()

    def _owner(self):
        if not self.window:
            self.window = self.u.CreateWindowExW(
                0, "STATIC", "VoxTypeX clipboard", 0, 0, 0, 0, 0, W.HWND(-3), None, None, None
            )
            if not self.window:
                raise C.WinError(C.get_last_error())
        return self.window

    def _open(self):
        deadline = time.monotonic() + 0.3
        while not self.u.OpenClipboard(self._owner()):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def _allocate(self, data):
        handle = self.k.GlobalAlloc(0x2, len(data))
        if not handle:
            raise MemoryError("Cannot allocate clipboard backup")
        pointer = self.k.GlobalLock(handle)
        if not pointer:
            self.k.GlobalFree(handle)
            raise C.WinError(C.get_last_error())
        C.memmove(pointer, data, len(data))
        self.k.GlobalUnlock(handle)
        return handle

    def _free(self, handles):
        for _, handle in handles:
            if handle:
                self.k.GlobalFree(handle)

    def _put_back(self, handles):
        success = True
        for index, (fmt, handle) in enumerate(handles):
            if self.u.SetClipboardData(fmt, handle):
                handles[index] = (fmt, None)  # Ownership transferred to Windows.
            else:
                success = False
        return success

    def write_clipboard(self, text):
        if not self._open():
            return None
        handles = []
        new_handle = None
        keep = False
        try:
            fmt, total = 0, 0
            while True:
                C.set_last_error(0)
                fmt = self.u.EnumClipboardFormats(fmt)
                if not fmt:
                    if C.get_last_error():
                        return None
                    break
                if fmt not in SAFE_FORMATS:
                    name = C.create_unicode_buffer(256)
                    self.u.GetClipboardFormatNameW(fmt, name, len(name))
                    if fmt < 0xC000 or name.value not in SAFE_REGISTERED:
                        return None
                handle = self.u.GetClipboardData(fmt)
                size = self.k.GlobalSize(handle) if handle else 0
                total += size
                if not size or total > MAX_CLIPBOARD_BYTES:
                    return None
                pointer = self.k.GlobalLock(handle)
                if not pointer:
                    return None
                try:
                    data = C.string_at(pointer, size)
                finally:
                    self.k.GlobalUnlock(handle)
                handles.append((fmt, self._allocate(data)))
            new_handle = self._allocate((text + "\0").encode("utf-16-le"))
            if not self.u.EmptyClipboard():
                return None
            if not self.u.SetClipboardData(13, new_handle):
                self._put_back(handles)
                raise RuntimeError("Could not place text on clipboard; restoration attempted")
            new_handle = None
            keep = True
            return ClipboardLease(self.u.GetClipboardSequenceNumber(), handles)
        finally:
            self.u.CloseClipboard()
            if new_handle:
                self.k.GlobalFree(new_handle)
            if not keep:
                self._free(handles)

    def restore_clipboard(self, lease):
        try:
            if not self._open():
                return False
            try:
                if (
                    self.u.GetClipboardSequenceNumber() != lease.sequence
                    or self.u.GetClipboardOwner() != self.window
                ):
                    return False  # A newer copy always wins.
                if not self.u.EmptyClipboard():
                    return False
                return self._put_back(lease.handles)
            finally:
                self.u.CloseClipboard()
        finally:
            self._free(lease.handles)
            lease.handles.clear()

    @staticmethod
    def _key(vk=0, scan=0, flags=0):
        return Input(type=1, ki=KeyboardInput(vk, scan, flags, 0, INPUT_TAG))

    def _send(self, events):
        inputs = (Input * len(events))(*events)
        if self.u.SendInput(len(inputs), inputs, C.sizeof(Input)) != len(inputs):
            raise RuntimeError("Windows did not accept all input events (possibly elevated target)")

    def paste(self):
        try:
            self._send([self._key(0x11), self._key(0x56), self._key(0x56, flags=2), self._key(0x11, flags=2)])
        except Exception:
            # Release only keys used by our attempted chord, then report failure without retrying.
            try:
                self._send([self._key(0x56, flags=2), self._key(0x11, flags=2)])
            except Exception:
                logger.exception("Could not release keys after failed paste")
            raise

    def type_unicode(self, text, valid):
        # Short chunks limit accidental input after a context change. Surrogate pairs stay together.
        for start in range(0, len(text), 32):
            if not valid():
                raise RuntimeError("Field changed during Unicode input; partial text may remain")
            data = text[start : start + 32].encode("utf-16-le")
            events = []
            for offset in range(0, len(data), 2):
                unit = int.from_bytes(data[offset : offset + 2], "little")
                events.extend((self._key(scan=unit, flags=4), self._key(scan=unit, flags=6)))
            self._send(events)

    def close(self):
        self.automation = None
        if self.com_initialized:
            import comtypes

            comtypes.CoUninitialize()
            self.com_initialized = False
        if self.window:
            self.u.DestroyWindow(self.window)
            self.window = None
