import logging
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from .audio import MicrophoneRecorder
from .config import SettingsService, data_directory, model_directory
from .hotkeys import GlobalHotkey
from .insertion import TextInsertionService
from .logging_setup import configure_logging
from .recording_service import RecordingService
from .state import AppController
from .transcription import TranscriptionService
from .ui.tray import TrayApplication
from .whisper_engine import FasterWhisperEngine


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("VoxTypeX")
    app.setOrganizationName("VoxTypeX")
    runtime = None
    previous_hook = sys.excepthook
    previous_sigint = signal.getsignal(signal.SIGINT)
    try:
        directory = data_directory()
        configure_logging(directory / "logs")
        store = SettingsService(directory / "settings.json")
        settings = store.load()
        if not store.path.exists():
            store.save(settings)

        def make_transcriber(new_settings):
            return TranscriptionService(
                FasterWhisperEngine(model_directory(), new_settings.whisper_model, new_settings.model_path),
                new_settings.language,
            )

        transcriber = make_transcriber(settings)
        service = RecordingService(
            AppController(),
            MicrophoneRecorder(device=settings.microphone),
            GlobalHotkey(),
            settings,
            directory,
            transcriber=transcriber,
            inserter=TextInsertionService(),
            transcriber_factory=make_transcriber,
        )
        runtime = TrayApplication(app, store, settings, service)

        def unhandled(exc_type, exc_value, traceback):
            logging.getLogger(__name__).critical(
                "Unhandled exception", exc_info=(exc_type, exc_value, traceback)
            )
            app.exit(1)

        sys.excepthook = unhandled
        signal.signal(signal.SIGINT, lambda *_: app.quit())
        heartbeat = QTimer(app)
        heartbeat.timeout.connect(lambda: None)
        heartbeat.start(250)
        runtime.start()
        return app.exec()
    except Exception:
        logging.getLogger(__name__).exception("Startup failed")
        QMessageBox.critical(
            None,
            "VoxTypeX",
            "Не удалось запустить приложение. Проверь настройки и журнал в папке данных VoxTypeX.",
        )
        return 1
    finally:
        if runtime is not None:
            runtime.close()
        sys.excepthook = previous_hook
        signal.signal(signal.SIGINT, previous_sigint)
        logging.shutdown()
