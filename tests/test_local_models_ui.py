from threading import Event
from time import monotonic

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QMessageBox

from voxtypex.config import Settings
from voxtypex.interfaces import OperationCancelled
from voxtypex.local_models import DownloadProgress, LanguageModelRegistry, LocalModelState


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_auto_language_keeps_whisper_and_exact_language_has_real_profiles(app, tmp_path):
    from voxtypex.ui.local_models_panel import LocalModelsPanel

    panel = LocalModelsPanel(cache_dir=tmp_path)
    panel.populate(Settings())
    panel.set_context(None, True)
    assert not panel.download_button.isEnabled()
    assert "Whisper" in panel.model_info.text()
    panel.set_context("uk", True)
    assert "77" in panel.model_info.text()
    assert "Powered by Moonshine AI" in panel.model_info.text()
    panel.quality_boxes["uk"].setCurrentIndex(1)
    assert "Parakeet" in panel.model_info.text()
    assert panel.selected_qualities()["uk_quality"] == "accurate"
    panel.shutdown()


def test_download_progress_and_cancel_are_drained_on_gui_thread(app, tmp_path, monkeypatch):
    from voxtypex.ui.local_models_panel import LocalModelsPanel

    started, finished = Event(), Event()
    registry = LanguageModelRegistry()

    class Manager:
        def status(self, model_id):
            return LocalModelState.NOT_INSTALLED

        def download(self, model_id, *, context, on_progress):
            assert QThread.currentThread() != app.thread()
            started.set()
            spec = registry.get(model_id)
            on_progress(
                DownloadProgress(model_id, 1000, spec.size_bytes, LocalModelState.DOWNLOADING, "downloading")
            )
            try:
                while not context.cancelled.wait(0.01):
                    context.checkpoint()
                raise OperationCancelled()
            finally:
                finished.set()

    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    panel = LocalModelsPanel(cache_dir=tmp_path, manager=Manager())
    panel.set_context("en", True)
    panel.start_download()
    assert started.wait(1)
    panel.poll_download()
    assert panel.progress.value() > 0
    assert panel.cancel_button.isEnabled()
    panel.cancel_download()
    assert finished.wait(1)
    deadline = monotonic() + 1
    while panel.downloading and monotonic() < deadline:
        app.processEvents()
        panel.poll_download()
    assert not panel.downloading
    assert "отмен" in panel.download_status.text().lower()
    assert panel.download_button.isEnabled()
    panel.shutdown()
