from threading import Event

import pytest

from voxtypex.insertion import TextInsertionService


class Backend:
    def __init__(self):
        self.matches = True
        self.clipboard = True
        self.modifiers = True
        self.events = []
        self.fail_paste = False
        self.restored = True

    def capture_target(self):
        return "field"

    def prepare_target(self, target):
        return target

    def target_matches(self, target):
        return self.matches

    def dismiss_ptt_menu(self, target):
        pass

    def wait_modifiers(self, valid):
        return self.modifiers and valid()

    def write_clipboard(self, text):
        self.events.append(("clipboard", text))
        return "lease" if self.clipboard else None

    def paste(self):
        self.events.append("paste")
        if self.fail_paste:
            raise RuntimeError("partial SendInput")

    def type_unicode(self, text, valid):
        assert valid()
        self.events.append(("unicode", text))

    def restore_clipboard(self, lease):
        self.events.append("restore")
        return self.restored

    def close(self):
        pass


def ready():
    backend = Backend()
    service = TextInsertionService(backend, restore_delay=0)
    service.prepare_target(service.capture_target(), Event(), Event())
    return service, backend


@pytest.mark.parametrize("reason", ["empty", "space", "cancel", "changed", "focus", "modifier", "missing"])
def test_unsafe_or_empty_does_not_touch_clipboard(reason):
    service, backend = ready()
    text = "Привіт!"
    if reason == "empty":
        text = ""
    if reason == "space":
        text = " \n "
    if reason == "cancel":
        service.cancelled.set()
    if reason == "changed":
        service.changed.set()
    if reason == "focus":
        backend.matches = False
    if reason == "modifier":
        backend.modifiers = False
    if reason == "missing":
        service.target = None
    assert service.insert_text(text).skipped
    assert backend.events == []


def test_clipboard_delay_and_order(monkeypatch):
    service, backend = ready()
    service.restore_delay = 1
    monkeypatch.setattr(
        "voxtypex.insertion.time.sleep", lambda delay: backend.events.append(("delay", delay))
    )
    result = service.insert_text("Hello — Привет — Привіт 🙂")
    assert result.success and result.clipboard_restored
    assert backend.events[1:] == ["paste", ("delay", 1), "restore"]


def test_no_retry_after_partial_paste():
    service, backend = ready()
    backend.fail_paste = True
    result = service.insert_text("hello")
    assert not result.success and result.error
    assert backend.events == [("clipboard", "hello"), "paste", "restore"]


def test_clipboard_cannot_be_saved_uses_unicode():
    service, backend = ready()
    backend.clipboard = False
    result = service.insert_text("🙂 Привет")
    assert result.success and result.method == "unicode" and result.clipboard_restored is None
    assert backend.events[-1] == ("unicode", "🙂 Привет")


def test_no_enter_in_unicode_fallback():
    service, backend = ready()
    backend.clipboard = False
    result = service.insert_text("hello\nworld")
    assert result.skipped and not result.success
    assert backend.events == [("clipboard", "hello\nworld")]


def test_new_copy_is_respected():
    service, backend = ready()
    backend.restored = False
    result = service.insert_text("hello")
    assert result.success and result.clipboard_restored is False


def test_focus_changed_after_clipboard_write_restores_without_paste():
    service, backend = ready()
    original = backend.write_clipboard

    def change(text):
        lease = original(text)
        backend.matches = False
        return lease

    backend.write_clipboard = change
    result = service.insert_text("hello")
    assert result.skipped and result.clipboard_restored
    assert backend.events == [("clipboard", "hello"), "restore"]


def test_restore_failure_is_reported_without_retrying_insert(caplog):
    service, backend = ready()

    def fail(_):
        raise OSError("restore locked")

    backend.restore_clipboard = fail
    result = service.insert_text("hello")
    assert result.success and result.clipboard_restored is False
    assert backend.events.count("paste") == 1
    assert "restore locked" in caplog.text


def test_cancellation_during_modifier_wait_never_writes_clipboard():
    service, backend = ready()

    def cancel(valid):
        service.cancelled.set()
        return valid()

    backend.wait_modifiers = cancel
    assert service.insert_text("hello").skipped
    assert backend.events == []
