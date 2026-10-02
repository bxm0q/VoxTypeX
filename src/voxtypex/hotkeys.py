import logging
from threading import Lock

from .key_policy import INPUT_TAG, KEY_CODES, MODIFIERS, modifiers_allowed

logger = logging.getLogger(__name__)


class KeyEdges:
    """Автоповтор фильтра; запоминать нажатую клавишу при изменении настроек"""

    def __init__(self, key, on_press, on_release):
        self.target = KEY_CODES[key]
        self.held = None
        self.on_press = on_press
        self.on_release = on_release
        self.lock = Lock()
        self.down = set()

    def set_key(self, key):
        with self.lock:
            self.target = KEY_CODES[key]

    def event(self, vk, down, allow_press=True):
        with self.lock:
            if down:
                if vk in self.down:
                    return
                self.down.add(vk)
            else:
                self.down.discard(vk)
            if down and vk == self.target and self.held is None and allow_press:
                self.held = vk
                self.on_press()
            elif not down and vk == self.held:
                self.held = None
                self.on_release()


class GlobalHotkey:
    def __init__(self):
        self.listener = None
        self.edges = None
        self.mouse_listener = None
        self.context_changed = None
        self.cancel_current = None

    def set_guard_callbacks(self, context_changed, cancel_current):
        self.context_changed = context_changed
        self.cancel_current = cancel_current

    def start(self, key, on_press, on_release):
        import ctypes

        from pynput.keyboard import Listener

        get_key_state = ctypes.windll.user32.GetAsyncKeyState

        def permitted(vk):
            return modifiers_allowed(vk, {code for code in MODIFIERS if get_key_state(code) & 0x8000})

        self.edges = KeyEdges(key, on_press, on_release)

        def event_filter(message, data):
            if data.dwExtraInfo == INPUT_TAG:
                return True
            if message in (0x100, 0x104, 0x101, 0x105):
                if self.context_changed is not None and message in (0x100, 0x104):
                    if data.vkCode == 0x1B:
                        self.cancel_current()
                    elif data.vkCode not in (
                        self.edges.target,
                        self.edges.held,
                        0x10,
                        0x11,
                        0x12,
                        0xA0,
                        0xA1,
                        0xA2,
                        0xA3,
                        0xA4,
                        0xA5,
                    ):
                        self.context_changed()
                self.edges.event(data.vkCode, message in (0x100, 0x104), permitted(data.vkCode))
            return True

        self.listener = Listener(suppress=False, win32_event_filter=event_filter)
        self.listener.start()
        from pynput.mouse import Button
        from pynput.mouse import Listener as MouseListener

        def clicked(x, y, button, pressed):
            vk = {Button.x1: 0x05, Button.x2: 0x06}.get(button)
            is_ptt = vk is not None and vk in (self.edges.target, self.edges.held)
            if pressed and not is_ptt and self.context_changed is not None:
                self.context_changed()
            if vk is not None:
                self.edges.event(vk, pressed, permitted(vk))

        self.mouse_listener = MouseListener(
            on_click=clicked,
            on_scroll=lambda *args: self.context_changed() if self.context_changed is not None else None,
        )
        self.mouse_listener.start()
        logger.info("Global PTT listener started: %s (non-suppressing)", key)

    def set_key(self, key):
        if self.edges is not None:
            self.edges.set_key(key)
        logger.info("PTT key changed: %s", key)

    def check_health(self):
        if self.listener is None or not self.listener.is_alive():
            raise RuntimeError("Global keyboard listener stopped; restart VoxTypeX")
        if self.mouse_listener is not None and not self.mouse_listener.is_alive():
            raise RuntimeError("Input guard stopped; restart VoxTypeX")

    def close(self):
        for attribute in ("mouse_listener", "listener"):
            listener = getattr(self, attribute)
            if listener is None:
                continue
            try:
                listener.stop()
                listener.join(timeout=2)
                if listener.is_alive():
                    logger.error("%s did not stop within 2 seconds", attribute)
            except Exception:
                logger.exception("Could not shut down %s cleanly", attribute)
            finally:
                setattr(self, attribute, None)
