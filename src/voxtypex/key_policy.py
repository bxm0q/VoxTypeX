"""Single-button PTT allowlist shared by settings, capture and Windows hooks."""

# Mark our own injected keystrokes so the input guard does not cancel insertion.
INPUT_TAG = 0x56545801

KEY_CODES = {
    "right_alt": 0xA5,
    "right_ctrl": 0xA3,
    **{f"f{n}": 0x6F + n for n in (*range(6, 10), *range(11, 25))},
    "mouse4": 0x05,
    "mouse5": 0x06,
}
KEY_NAMES = {
    "right_alt": "Right Alt",
    "right_ctrl": "Right Ctrl",
    **{name: name.upper() for name in KEY_CODES if name.startswith("f")},
    "mouse4": "Mouse Button 4",
    "mouse5": "Mouse Button 5",
}
MODIFIERS = {0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C}


def key_name(key):
    return KEY_NAMES.get(key, key)


def from_vk(vk):
    return next((key for key, code in KEY_CODES.items() if code == vk), None)


def modifiers_allowed(vk, held):
    others = set(held) & MODIFIERS
    others.discard(vk)
    # Windows synthesizes left Ctrl for AltGr. A single Right Alt remains usable.
    if vk == 0xA5:
        others -= {0x11, 0x12, 0xA2}
    elif vk == 0xA3:
        others.discard(0x11)
    return not others


class KeyCapture:
    """Commit only after release; repeats cannot assign twice or accept a chord."""

    def __init__(self):
        self.down = set()
        self.candidate = None
        self.released = False

    def event(self, vk, pressed, modifiers=(), repeat=False):
        if repeat:
            return "waiting", None
        if pressed and vk == 0x1B:
            return "cancel", None
        if pressed:
            if vk in self.down:
                return "waiting", None
            others = self.down.copy()
            self.down.add(vk)
            if vk == 0xA5:
                others -= {0x11, 0xA2}
            candidate = from_vk(vk)
            if candidate is None or others or not modifiers_allowed(vk, modifiers):
                self.candidate = None
                self.released = False
                return "unsupported", None
            self.candidate = candidate
            self.released = False
            return "release", candidate
        self.down.discard(vk)
        if self.candidate is not None and KEY_CODES[self.candidate] == vk:
            self.released = True
        if not self.down and self.candidate is not None and self.released:
            candidate, self.candidate = self.candidate, None
            return "accepted", candidate
        return "waiting", None
