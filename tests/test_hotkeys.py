from voxtypex.hotkeys import KEY_CODES, KeyEdges


def test_repeat_unrelated_and_key_change_while_held():
    events = []
    gate = KeyEdges("right_alt", lambda: events.append("down"), lambda: events.append("up"))
    gate.event(65, True)
    for _ in range(30):
        gate.event(KEY_CODES["right_alt"], True)
    gate.set_key("f8")
    gate.event(KEY_CODES["f8"], True)
    gate.event(KEY_CODES["right_alt"], False)
    gate.event(KEY_CODES["right_alt"], True)
    gate.event(KEY_CODES["f8"], False)  # A key held across reassignment needs a fresh edge.
    gate.event(KEY_CODES["f8"], True)
    gate.event(KEY_CODES["f8"], False)
    gate.event(KEY_CODES["f8"], False)
    assert events == ["down", "up", "down", "up"]


def test_blocked_combo_does_not_start_from_autorepeat():
    events = []
    gate = KeyEdges("f9", lambda: events.append("down"), lambda: events.append("up"))
    gate.event(KEY_CODES["f9"], True, allow_press=False)
    gate.event(KEY_CODES["f9"], True)
    assert events == []
    gate.event(KEY_CODES["f9"], False)
    gate.event(KEY_CODES["f9"], True)
    gate.event(KEY_CODES["f9"], False)
    assert events == ["down", "up"]


def test_mouse_edges_and_key_change_held():
    events = []
    gate = KeyEdges("mouse4", lambda: events.append("down"), lambda: events.append("up"))
    gate.event(5, True)
    gate.event(5, True)
    gate.set_key("f9")
    gate.event(5, False)
    assert events == ["down", "up"]


def test_keyboard_hook_still_closes_when_mouse_join_reraises(caplog):
    from voxtypex.hotkeys import GlobalHotkey

    class Listener:
        def __init__(self, fail=False):
            self.stopped, self.fail = False, fail

        def stop(self):
            self.stopped = True

        def join(self, timeout):
            if self.fail:
                raise RuntimeError("mouse callback crashed")

        def is_alive(self):
            return False

    hotkey = GlobalHotkey()
    mouse, keyboard = Listener(True), Listener()
    hotkey.mouse_listener, hotkey.listener = mouse, keyboard
    hotkey.close()
    hotkey.close()
    assert mouse.stopped and keyboard.stopped
    assert hotkey.listener is None and hotkey.mouse_listener is None
    assert "mouse callback crashed" in caplog.text
