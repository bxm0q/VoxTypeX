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


def test_overlay_renders_changing_preview_without_focus_or_stale_updates(app):
    from voxtypex.interfaces import TranscriptEvent, TranscriptKind
    from voxtypex.ui.status_overlay import StatusOverlay

    overlay = StatusOverlay()
    try:
        overlay.update_status("RECORDING", "", True, preview_status="Предпросмотр · batch-ASR")
        overlay.update_transcript(TranscriptEvent(1, 1, 1, TranscriptKind.PARTIAL, unstable_text="Первая версия"))
        assert "Первая версия" in overlay.text() and "Предпросмотр" in overlay.text()
        overlay.update_transcript(TranscriptEvent(1, 1, 2, TranscriptKind.PARTIAL, unstable_text="Версия меняется"))
        assert "Версия меняется" in overlay.text() and "Первая версия" not in overlay.text()
        overlay.update_transcript(TranscriptEvent(1, 1, 1, TranscriptKind.PARTIAL, unstable_text="Устарело"))
        assert "Устарело" not in overlay.text()
        overlay.update_transcript(TranscriptEvent(1, 2, 0, TranscriptKind.RESET))
        overlay.update_transcript(TranscriptEvent(1, 1, 99, TranscriptKind.PARTIAL, unstable_text="Поздний ответ"))
        assert "Версия меняется" not in overlay.text() and "Поздний ответ" not in overlay.text()
        assert overlay.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert overlay.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
        assert overlay.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        overlay.update_status("IDLE", "", True)
        assert not overlay.isVisible()
    finally:
        overlay.close()


def test_first_visible_hypothesis_is_acknowledged_by_gui_paint(app):
    from voxtypex.interfaces import TranscriptEvent, TranscriptKind
    from voxtypex.ui.status_overlay import StatusOverlay

    overlay = StatusOverlay()
    visible = []
    try:
        overlay.hypothesis_visible.connect(lambda event: visible.append(event.sequence))
        overlay.update_status("RECORDING", "", True)
        overlay.update_transcript(TranscriptEvent(1, 1, 1, TranscriptKind.PARTIAL, unstable_text="Текст"))
        app.processEvents()
        overlay.repaint()
        app.processEvents()
        assert visible == [1]
        overlay.update_transcript(TranscriptEvent(1, 2, 0, TranscriptKind.RESET))
        app.processEvents()
        assert visible == [1]
    finally:
        overlay.close()


def test_preview_options_save_and_restore_with_other_settings(app, tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    runtime = TrayApplication(app, store, Settings())
    try:
        runtime.window.open_settings()
        runtime.window.preview_mode.setCurrentIndex(runtime.window.preview_mode.findData("off"))
        runtime.window.preview_interval.setValue(3)
        runtime.window.preview_window.setValue(12)
        runtime.window.save_button.click()
        value = store.load()
        assert value.preview_mode == "off" and value.preview_interval_seconds == 3
        assert value.preview_window_seconds == 12
        runtime.window.open_settings()
        assert runtime.window.preview_mode.currentData() == "off"
        assert runtime.window.preview_interval.value() == 3 and runtime.window.preview_window.value() == 12
        assert "batch" in runtime.window.preview_hint.text().lower()
        assert "live-typing" in runtime.window.preview_hint.text()
    finally:
        runtime.close()


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


def test_worker_progress_is_consumed_by_gui_refresh(app, tmp_path):
    from threading import Event, Thread
    from types import SimpleNamespace

    from voxtypex.interfaces import ProgressEvent, ProgressStage
    from voxtypex.progress import ProgressBuffer
    from voxtypex.state import AppController, AppState

    stream = ProgressBuffer()
    stream.begin(1)
    service = SimpleNamespace(
        controller=AppController(),
        finished=Event(),
        last_error="",
        set_settings_open=lambda _: None,
        close=lambda: None,
        drain_progress=stream.drain,
    )
    runtime = TrayApplication(app, SettingsStore(tmp_path / "settings.json"), Settings(), service)
    try:
        service.controller.transition(AppState.IDLE, AppState.RECORDING)
        service.controller.transition(AppState.RECORDING, AppState.TRANSCRIBING)
        producer = Thread(target=lambda: stream.emit(ProgressEvent(1, ProgressStage.LOADING)))
        producer.start()
        producer.join()
        runtime.refresh()
        assert "Загружаю модель в память" in runtime.overlay.text()
        assert "Загружаю модель в память" in runtime.tray.toolTip()
        assert runtime.overlay.thread() is app.thread()
        assert not stream.drain()
    finally:
        runtime.close()


def test_audio_controls_save_and_test_is_explicit_memory_only(app, tmp_path):
    from voxtypex.ui.microphone_panel import MicrophoneTestSession
    from voxtypex.ui.settings_window import SettingsWindow

    made = []
    store = SettingsStore(tmp_path / "settings.json")
    window = SettingsWindow(store, Settings(), microphone_provider=lambda: [])
    window.audio_panel.session = MicrophoneTestSession(recorder_factory=lambda **kwargs: made.append(kwargs))
    try:
        window.open_settings()
        app.processEvents()
        assert made == [] and not window.audio_panel.session.busy
        assert not window.audio_panel.raw_button.isEnabled()
        panel = window.audio_panel
        panel.noise.setCurrentIndex(panel.noise.findData("strong"))
        panel.vad_mode.setCurrentIndex(panel.vad_mode.findData(2))
        panel.preroll.setValue(400)
        panel.hangover.setValue(900)
        panel.auto_stop.setChecked(True)
        panel.silence.setValue(2.5)
        window.save()
        value = store.load()
        assert value.noise_suppression == "strong" and value.vad_mode == 2
        assert value.vad_preroll_ms == 400 and value.vad_hangover_ms == 900
        assert value.auto_stop_silence_seconds == 2.5
        assert made == []
    finally:
        window.close()


def test_device_refresh_has_one_worker_and_preserves_missing_selection(app, tmp_path):
    from threading import Event

    from voxtypex.ui.settings_window import SettingsWindow

    entered, release = Event(), Event()
    calls = []

    def provider():
        calls.append(True)
        entered.set()
        release.wait(2)
        return []

    store = SettingsStore(tmp_path / "settings.json")
    store.current = Settings(microphone="unplugged")
    window = SettingsWindow(store, store.current, microphone_provider=provider)
    try:
        window.open_settings()
        assert entered.wait(1)
        for _ in range(5):
            window.load_devices()
        assert len(calls) == 1
        release.set()
        for _ in range(50):
            QTest.qWait(10)
            if not window._devices_query_active:
                break
        assert window.microphone.currentData() == "unplugged"
        assert window.fallback_device.isVisible()
        assert "подмены" in window.device_status.text()
    finally:
        release.set()
        window.close()


def test_windows_default_fallback_requires_explicit_consent_and_save(app, tmp_path, monkeypatch):
    from voxtypex.ui.settings_window import SettingsWindow

    store = SettingsStore(tmp_path / "settings.json")
    store.current = Settings(microphone="unplugged")
    window = SettingsWindow(store, store.current, microphone_provider=lambda: [])
    try:
        window.open_settings()
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
        window.use_windows_default()
        assert window.microphone.currentData() == "unplugged" and store.current.microphone == "unplugged"
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
        window.use_windows_default()
        assert window.microphone.currentData() is None and store.current.microphone is None
        assert store.path.exists() and window.isVisible()
    finally:
        window.close()


def test_mic_meter_and_capture_start_only_after_button_and_stop_on_hide(app, tmp_path):
    from threading import Event
    from types import SimpleNamespace

    from voxtypex.ui.microphone_panel import MicrophoneTestSession
    from voxtypex.ui.settings_window import SettingsWindow

    started, closed, made = Event(), Event(), []

    class Recorder:
        diagnostics = SimpleNamespace(
            rms_dbfs=-18.0, peak_dbfs=-1.0, clipped_samples=4, latency_ms=15.0, processing_ms=0.0
        )

        def __init__(self, **kwargs):
            made.append(kwargs)

        def start(self, held):
            self.held = held
            started.set()

        def check_health(self):
            pass

        def close(self):
            closed.set()

    store = SettingsStore(tmp_path / "settings.json")
    window = SettingsWindow(store, Settings(), microphone_provider=lambda: [])
    session = MicrophoneTestSession(recorder_factory=Recorder, duration=3)
    window.audio_panel.session = session
    try:
        window.open_settings()
        QTest.qWait(100)
        assert not started.is_set() and not made
        window.audio_panel.test_button.click()
        assert started.wait(1)
        for _ in range(100):
            QTest.qWait(10)
            if window.audio_panel.meter.value() == 42:
                break
        assert window.audio_panel.meter.value() == 42
        assert "4" in window.audio_panel.metrics.text() and "15.0" in window.audio_panel.metrics.text()
        assert "уменьшите" in window.audio_panel.metrics.text()
        window.close()
        assert closed.wait(1)
        assert not session.has_clip
        assert not window.devices_timer.isActive() and not window.audio_panel.timer.isActive()
    finally:
        window.close()


def test_default_pin_reset_requires_saved_user_activation_or_confirmed_fallback(app, tmp_path, monkeypatch):
    from voxtypex.ui.settings_window import SettingsWindow

    store = SettingsStore(tmp_path / "settings.json")
    window = SettingsWindow(store, Settings(), microphone_provider=lambda: [])
    resets = []
    window.microphone_reset_requested.connect(lambda: resets.append(True))
    try:
        window.open_settings()
        window.audio_panel.noise.setCurrentIndex(1)
        window.save()
        assert resets == []
        window.open_settings()
        window.microphone.setCurrentIndex(0)
        window.save()
        assert resets == []
        window.open_settings()
        window.microphone.activated.emit(0)
        window.save()
        assert resets == [True]
        window.open_settings()
        window.save()
        assert resets == [True]
        window.open_settings()
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
        window.use_windows_default()
        assert resets == [True]
        monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
        window.use_windows_default()
        assert resets == [True, True]
    finally:
        window.close()


def test_tray_shares_ptt_default_binding_with_explicit_mic_test(app, tmp_path):
    from threading import Event
    from types import SimpleNamespace

    from voxtypex.state import AppController

    binding = object()
    service = SimpleNamespace(
        controller=AppController(), recorder=SimpleNamespace(default_binding=binding),
        finished=Event(), last_error="", set_settings_open=lambda _: None, close=lambda: None,
    )
    runtime = TrayApplication(app, SettingsStore(tmp_path / "settings.json"), Settings(), service)
    try:
        assert runtime.window.audio_panel.session.default_binding is binding
    finally:
        runtime.close()
