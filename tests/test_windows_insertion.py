"""Native memory/INPUT layout checks with an isolated simulated clipboard.

Does not replace the user's Windows clipboard or send desktop input.
"""

import ctypes as C

from voxtypex.windows_insertion import INPUT_TAG, Input, WindowsInsertion


class Clipboard:
    def __init__(self, backend, formats):
        self.backend = backend
        self.data = {fmt: backend._allocate(value) for fmt, value in formats.items()}
        self.sequence = 1
        self.owner = 456
        self.empties = 0
        self.busy = False

    def OpenClipboard(self, owner):
        self.open_owner = owner
        return not self.busy

    def CloseClipboard(self):
        return True

    def EnumClipboardFormats(self, previous):
        ordered = list(self.data)
        index = ordered.index(previous) + 1 if previous else 0
        return ordered[index] if index < len(ordered) else 0

    def GetClipboardData(self, fmt):
        return self.data[fmt]

    def GetClipboardFormatNameW(self, fmt, name, count):
        name.value = "unknown OLE format"
        return len(name.value)

    def EmptyClipboard(self):
        for handle in self.data.values():
            self.backend.k.GlobalFree(handle)
        self.data.clear()
        self.sequence += 1
        self.owner = self.open_owner
        self.empties += 1
        return True

    def SetClipboardData(self, fmt, handle):
        self.data[fmt] = handle
        self.sequence += 1
        return handle

    def GetClipboardSequenceNumber(self):
        return self.sequence

    def GetClipboardOwner(self):
        return self.owner

    def bytes(self, fmt):
        handle = self.data[fmt]
        pointer = self.backend.k.GlobalLock(handle)
        try:
            return C.string_at(pointer, self.backend.k.GlobalSize(handle))
        finally:
            self.backend.k.GlobalUnlock(handle)

    def clean(self):
        for handle in self.data.values():
            self.backend.k.GlobalFree(handle)
        self.data.clear()


def clipboard(formats):
    backend = WindowsInsertion()
    backend.window = 123
    simulated = Clipboard(backend, formats)
    backend.u = simulated
    return backend, simulated


def test_native_memory_multiformat_roundtrip():
    original = {13: "Предыдущий текст\0".encode("utf-16-le"), 16: b"\x09\x04\x00\x00"}
    backend, simulated = clipboard(original)
    try:
        lease = backend.write_clipboard("Hello 🙂")
        assert simulated.bytes(13).decode("utf-16-le").rstrip("\0") == "Hello 🙂"
        assert backend.restore_clipboard(lease)
        assert {fmt: simulated.bytes(fmt) for fmt in simulated.data} == original
        assert not lease.handles
    finally:
        simulated.clean()


def test_unknown_format_leaves_clipboard_untouched():
    backend, simulated = clipboard({0xC123: b"preserve this OLE data"})
    try:
        assert backend.write_clipboard("new") is None
        assert simulated.empties == 0
        assert simulated.bytes(0xC123) == b"preserve this OLE data"
    finally:
        simulated.clean()


def test_new_clipboard_owner_or_sequence_wins():
    backend, simulated = clipboard({13: "old\0".encode("utf-16-le")})
    try:
        lease = backend.write_clipboard("dictation")
        simulated.EmptyClipboard()
        simulated.SetClipboardData(13, backend._allocate("new user copy\0".encode("utf-16-le")))
        assert not backend.restore_clipboard(lease)
        assert simulated.bytes(13).decode("utf-16-le").rstrip("\0") == "new user copy"
        assert not lease.handles
    finally:
        simulated.clean()


def test_empty_clipboard_is_restored_to_empty():
    backend, simulated = clipboard({})
    try:
        lease = backend.write_clipboard("new")
        assert backend.restore_clipboard(lease)
        assert simulated.data == {}
    finally:
        simulated.clean()


def test_unicode_surrogates_and_own_event_tag():
    backend = WindowsInsertion()
    events = []
    backend._send = events.extend
    backend.type_unicode("Я🙂", lambda: True)
    assert [event.ki.wScan for event in events[::2]] == [0x42F, 0xD83D, 0xDE42]
    assert all(event.ki.dwExtraInfo == INPUT_TAG for event in events)
    assert [event.ki.dwFlags for event in events] == [4, 6, 4, 6, 4, 6]
    assert C.sizeof(Input) == (40 if C.sizeof(C.c_void_p) == 8 else 28)
