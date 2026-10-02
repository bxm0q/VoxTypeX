import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"
import pytest
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from voxtypex.config import Settings, SettingsStore
from voxtypex.ui.tray import TrayApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_tray_settings_and_exit(app, tmp_path, monkeypatch):
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    store = SettingsStore(tmp_path / "settings.json")
    runtime = TrayApplication(app, store, Settings())
    try:
        runtime.start()
        assert runtime.tray.isVisible()
        assert not runtime.tray.icon().isNull()
        assert not app.quitOnLastWindowClosed()
        runtime.settings_action.trigger()
        assert runtime.window.isVisible()
        runtime.window.change_key.click()
        QTest.keyClick(runtime.window, Qt.Key.Key_F8)
        runtime.window.save_button.click()
        assert store.load().push_to_talk_key == "f8"
        assert not runtime.window.isVisible()
        runtime.window.open_settings()
        runtime.window.language.setCurrentIndex(2)
        runtime.window.close()
        runtime.window.open_settings()
        assert runtime.window.key_label.text() == "F8"
        assert runtime.window.language.currentData() is None
        QTimer.singleShot(0, runtime.quit_action.trigger)
        assert app.exec() == 0
        assert runtime.controller.closed
        assert not runtime.tray.isVisible()
    finally:
        runtime.close()


def test_no_tray_fallback(app, tmp_path, monkeypatch):
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    runtime = TrayApplication(app, SettingsStore(tmp_path / "settings.json"), Settings())
    try:
        runtime.start()
        assert runtime.window.isVisible()
        assert app.quitOnLastWindowClosed()
    finally:
        runtime.close()


def test_save_failure_keeps_window_and_previous_settings(app, tmp_path, monkeypatch):
    store = SettingsStore(tmp_path / "settings.json")
    runtime = TrayApplication(app, store, Settings())
    warnings = []

    def fail(settings):
        raise PermissionError("locked")

    monkeypatch.setattr(store, "save", fail)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))
    try:
        runtime.window.open_settings()
        runtime.window.language.setCurrentIndex(2)
        runtime.window.save_button.click()
        assert warnings
        assert runtime.window.isVisible()
        assert runtime.window.settings == Settings()
    finally:
        runtime.close()


def test_quit_waits_asynchronously_for_worker(app, tmp_path, monkeypatch):
    from threading import Event
    from types import SimpleNamespace

    from voxtypex.state import AppController

    requested = []
    service = SimpleNamespace(
        controller=AppController(),
        finished=Event(),
        last_error="",
        set_settings_open=lambda _: None,
        update_settings=lambda _: None,
        close=lambda: None,
        request_close=lambda: requested.append(True),
    )
    runtime = TrayApplication(app, SettingsStore(tmp_path / "settings.json"), Settings(), service)
    try:
        runtime.request_quit()
        assert requested
        assert runtime.quitting
        assert not runtime.controller.closed
        assert not runtime.settings_action.isEnabled()
        quits = []
        monkeypatch.setattr(app, "quit", lambda: quits.append(True))
        runtime._quit_deadline = 0
        runtime.refresh()
        assert quits == [True]
    finally:
        runtime.close()


def test_capture_cancel_repeat_mouse_and_no_recording(app, tmp_path):
    from threading import Event
    from types import SimpleNamespace

    from voxtypex.state import AppController

    gate, updates = [], []
    service = SimpleNamespace(
        controller=AppController(),
        finished=Event(),
        last_error="",
        set_settings_open=gate.append,
        update_settings=updates.append,
        close=lambda: None,
    )
    store = SettingsStore(tmp_path / "settings.json")
    runtime = TrayApplication(app, store, Settings(), service)
    runtime.window.microphone_provider = lambda: []
    try:
        window = runtime.window
        window.open_settings()
        assert gate == [True]
        window.change_key.click()
        QTest.keyClick(window, Qt.Key.Key_Escape)
        assert window.capture is None and window.isVisible()
        assert updates == []
        window.change_key.click()
        QTest.keyClick(window, Qt.Key.Key_F9)
        assert store.load().push_to_talk_key == "f9"
        window.change_key.click()
        QTest.keyClick(window, Qt.Key.Key_F9)
        assert len(updates) == 1
        window.change_key.click()
        QTest.mouseClick(window, Qt.MouseButton.XButton1)
        assert store.load().push_to_talk_key == "mouse4"
        window.close()
        assert gate[-1] is False
    finally:
        runtime.close()


def test_ui_saves_model_language_device_indication_and_toggle(app, tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    runtime = TrayApplication(app, store, Settings())
    runtime.window.microphone_provider = lambda: []
    try:
        window = runtime.window
        window.open_settings()
        window.microphone.addItem("Test microphone", "test device")
        window.microphone.setCurrentIndex(1)
        window.language.setCurrentIndex(window.language.findData("en"))
        window.model.setCurrentIndex(window.model.findData("tiny"))
        window.visual.setChecked(False)
        window.save()
        runtime.enable_action.trigger()
        value = SettingsStore(store.path).load()
        assert (value.language, value.whisper_model, value.microphone) == ("en", "tiny", "test device")
        assert not value.enabled and not value.visual_indication
        assert "Disabled" in runtime.tray.toolTip()
        assert runtime.enable_action.text() == "Включить"
        runtime.enable_action.trigger()
        assert store.current.enabled
    finally:
        runtime.close()


def test_capture_rejects_chord_and_cancels_on_focus_loss(app, tmp_path):
    runtime = TrayApplication(app, SettingsStore(tmp_path / "settings.json"), Settings())
    runtime.window.microphone_provider = lambda: []
    try:
        window = runtime.window
        window.open_settings()
        window.change_key.click()
        QTest.keyClick(window, Qt.Key.Key_F9, Qt.KeyboardModifier.ControlModifier)
        assert window.capture is not None
        assert runtime.store.current.push_to_talk_key == "right_alt"
        QApplication.sendEvent(window, QEvent(QEvent.Type.WindowDeactivate))
        assert window.capture is None
        assert window.save_button.isEnabled()
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "key,vk,scan,expected",
    [
        (Qt.Key.Key_AltGr, 18, 312, 0xA5),
        (Qt.Key.Key_Alt, 18, 0xE038, 0xA5),
        (Qt.Key.Key_Control, 17, 0xE01D, 0xA3),
        (Qt.Key.Key_Alt, 18, 56, 0xA4),
        (Qt.Key.Key_Control, 17, 285, 0xA3),
        (Qt.Key.Key_Control, 17, 29, 0xA2),
    ],
)
def test_native_right_and_left_modifiers_are_distinguished(key, vk, scan, expected):
    from types import SimpleNamespace

    from voxtypex.ui.settings_window import native_key

    event = SimpleNamespace(key=lambda: key, nativeVirtualKey=lambda: vk, nativeScanCode=lambda: scan)
    assert native_key(event) == expected


def test_builtin_model_selection_clears_custom_override(app, tmp_path):
    settings = Settings(model_path="C:/models/custom")
    runtime = TrayApplication(app, SettingsStore(tmp_path / "settings.json"), settings)
    runtime.window.microphone_provider = lambda: []
    try:
        window = runtime.window
        window.open_settings()
        assert window.model.currentData() == "local"
        window.model.setCurrentIndex(window.model.findData("small"))
        window.save()
        assert runtime.store.current.model_path is None
        assert runtime.store.current.whisper_model == "small"
    finally:
        runtime.close()


def test_visual_indication_can_be_disabled_without_losing_status(app, tmp_path):
    from dataclasses import replace

    from voxtypex.state import AppState

    runtime = TrayApplication(app, SettingsStore(tmp_path / "settings.json"), Settings())
    try:
        initial = runtime.tray.icon().cacheKey()
        runtime.controller.transition(AppState.IDLE, AppState.RECORDING)
        runtime.refresh()
        assert runtime.tray.icon().cacheKey() != initial
        runtime.store.save(replace(runtime.store.current, visual_indication=False))
        runtime.refresh()
        static = runtime.tray.icon().cacheKey()
        runtime.controller.transition(AppState.RECORDING, AppState.TRANSCRIBING)
        runtime.refresh()
        assert runtime.tray.icon().cacheKey() == static
        assert "Transcribing" in runtime.tray.toolTip()
    finally:
        runtime.close()
