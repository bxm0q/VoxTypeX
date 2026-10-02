import logging
from dataclasses import replace
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit
from test_recording_service import FakeInserter, FakeTranscriber, wait_for
from test_recording_service import service as service

from voxtypex.config import Settings, SettingsService
from voxtypex.insertion import InsertResult
from voxtypex.interfaces import TranscriptionResult
from voxtypex.state import AppState
from voxtypex.ui.tray import TrayApplication


def test_insertion_failure_is_error_without_technical_ui_text(service, caplog):
    service.transcriber = stt = FakeTranscriber(service)
    stt.release.set()
    service.inserter = inserter = FakeInserter(service)
    inserter.insert_text = lambda _: InsertResult(error="OSError: SendInput failed 5")
    service.pressed()
    wait_for(lambda: service.controller.state is AppState.RECORDING)
    service.released()
    wait_for(lambda: not service._busy)
    assert service.controller.state is AppState.ERROR
    assert service.last_error == "Не удалось вставить текст в активное приложение."
    assert "SendInput failed 5" in caplog.text


def test_model_error_has_specific_message(service, caplog):
    service.transcriber = stt = FakeTranscriber(service)
    stt.result = TranscriptionResult(error="FileNotFoundError: private/model/path", error_code="model")
    stt.release.set()
    service.pressed()
    wait_for(lambda: service.controller.state is AppState.RECORDING)
    service.released()
    wait_for(lambda: not service._busy)
    assert "Не удалось загрузить модель" in service.last_error
    assert "private/model/path" not in service.last_error
    assert "private/model/path" in caplog.text


def test_repeat_cancel_and_rapid_edges_leave_no_stream(service):
    for _ in range(30):
        service.pressed()
        service.released()
        service.cancel_current()
        wait_for(lambda: not service._busy)
    service.pressed()
    wait_for(lambda: service.controller.state is AppState.RECORDING)
    service.request_close()
    assert service.finished.wait(2)
    assert not service.recorder.open
    service.pressed()
    assert service._commands.qsize() < 5


def test_overlay_status_focus_and_disable(tmp_path):
    app = QApplication.instance() or QApplication([])
    target = QLineEdit()
    target.show()
    target.setFocus()
    app.processEvents()
    runtime = TrayApplication(app, SettingsService(tmp_path / "settings.json"), Settings())
    try:
        focus = app.focusWidget()
        assert "Ready" in runtime.tray.toolTip()
        assert not runtime.overlay.isVisible()
        for old, new, text in [
            (AppState.IDLE, AppState.RECORDING, "Запись"),
            (AppState.RECORDING, AppState.TRANSCRIBING, "Распознаю"),
        ]:
            runtime.controller.transition(old, new)
            runtime.refresh()
            app.processEvents()
            assert runtime.overlay.isVisible()
            assert text in runtime.overlay.text()
            assert app.focusWidget() is focus
        assert runtime.overlay.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
        assert runtime.overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput
        runtime.overlay.update_status("TRANSCRIBING", "", True, "downloading")
        assert "Скачиваю модель" in runtime.overlay.text()
        runtime.overlay.update_status("TRANSCRIBING", "", True, "loading")
        assert "в память" in runtime.overlay.text()
        assert app.focusWidget() is focus
        runtime.store.current = replace(runtime.store.current, visual_indication=False)
        runtime.refresh()
        assert not runtime.overlay.isVisible()
        runtime.store.current = replace(runtime.store.current, enabled=False)
        runtime.refresh()
        assert "Disabled" in runtime.tray.toolTip()
        runtime.store.current = replace(runtime.store.current, enabled=True, visual_indication=True)
        runtime.controller.transition(AppState.TRANSCRIBING, AppState.ERROR)
        runtime.refresh()
        assert runtime.overlay.isVisible()
        runtime.overlay._expiry.timeout.emit()
        runtime.refresh()
        assert not runtime.overlay.isVisible()  # polling must not resurrect an expired error
    finally:
        runtime.close()
        target.close()


def test_production_log_rotation_is_bounded(tmp_path, monkeypatch):
    from voxtypex.logging_setup import configure_logging

    configured = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: configured.update(kwargs))
    configure_logging(tmp_path)
    handler = configured["handlers"][0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes == 1_000_000 and handler.backupCount == 3
    try:
        for _ in range(55):
            handler.handle(
                logging.LogRecord("rotation-test", logging.INFO, __file__, 0, "x" * 100_000, (), None)
            )
    finally:
        handler.close()
    logs = list(tmp_path.glob("voxtypex.log*"))
    assert len(logs) == 4
    assert all(path.stat().st_size <= 1_000_000 for path in logs)
    assert sum(path.stat().st_size for path in logs) <= 4_000_000
