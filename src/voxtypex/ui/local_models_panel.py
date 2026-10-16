"""Explicit model installation. Worker threads publish metadata through a queue."""

import logging
from collections import deque
from threading import Event, Lock, Thread
from time import monotonic

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import model_directory
from ..interfaces import OperationCancelled, OperationContext, OperationTimeout
from ..local_models import LanguageModelManager, LanguageModelRegistry, LocalModelState


class LocalModelsPanel(QWidget):
    def __init__(self, *, cache_dir=None, manager=None):
        super().__init__()
        self.registry = LanguageModelRegistry()
        self.manager = manager or LanguageModelManager(
            cache_dir if cache_dir is not None else model_directory()
        )
        self.language = None
        self.local_enabled = False
        self.downloading = False
        self._cancel = Event()
        self._events = deque(maxlen=8)
        self._events_lock = Lock()
        self._thread = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        profiles = QFormLayout()
        self.quality_boxes = {}
        for language, title in (("ru", "Русский"), ("uk", "Українська"), ("en", "English")):
            box = QComboBox()
            for quality, label in (("compact", "Компактная"), ("accurate", "Точная")):
                spec = self.registry.for_language(language, quality)
                box.addItem(f"{label} · {spec.size_bytes / 1_000_000:.1f} МБ", quality)
            box.currentIndexChanged.connect(self.refresh)
            self.quality_boxes[language] = box
            profiles.addRow(title, box)
        layout.addLayout(profiles)
        self.model_info = QLabel()
        self.model_info.setWordWrap(True)
        self.model_info.setOpenExternalLinks(True)
        layout.addWidget(self.model_info)
        self.progress = QProgressBar()
        self.progress.setRange(0, 10000)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.download_status = QLabel()
        self.download_status.setWordWrap(True)
        layout.addWidget(self.download_status)
        row = QHBoxLayout()
        self.download_button = QPushButton("Скачать / проверить модель")
        self.cancel_button = QPushButton("Отменить скачивание")
        self.download_button.clicked.connect(self.start_download)
        self.cancel_button.clicked.connect(self.cancel_download)
        row.addWidget(self.download_button)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll_download)
        self.timer.start()
        self.refresh()

    def populate(self, settings):
        for language, box in self.quality_boxes.items():
            box.setCurrentIndex(box.findData(getattr(settings, f"{language}_quality")))

    def selected_qualities(self):
        return {f"{language}_quality": box.currentData() for language, box in self.quality_boxes.items()}

    def set_context(self, language, local_enabled):
        self.language, self.local_enabled = language, local_enabled
        self.refresh()

    def selected_spec(self):
        if self.language is None:
            return None
        return self.registry.for_language(self.language, self.quality_boxes[self.language].currentData())

    def refresh(self, *_):
        spec = self.selected_spec()
        self.download_button.setEnabled(self.local_enabled and spec is not None and not self.downloading)
        self.cancel_button.setEnabled(self.downloading)
        for box in self.quality_boxes.values():
            box.setEnabled(self.local_enabled and not self.downloading)
        if not self.local_enabled or spec is None:
            self.model_info.setText("Автоматическое определение языка и обычный режим используют Whisper.")
            return
        status = self.manager.status(spec.id)
        states = {
            LocalModelState.NOT_INSTALLED: "не установлена",
            LocalModelState.INSTALLED: "установлена; SHA256 проверится перед запуском",
            LocalModelState.DOWNLOADING: "скачивается",
            LocalModelState.READY: "готова, SHA256 проверен",
            LocalModelState.BROKEN: "повреждена или скачивание прервано; повторите проверку",
        }
        attribution = "<br>Powered by Moonshine AI" if spec.id == "uk-compact" else ""
        self.model_info.setText(
            f"<b>{spec.label}</b> — {spec.size_bytes:,} байт · CPU<br>"
            f'Состояние: {states[status]}. <a href="{spec.license_url}">{spec.license}</a>{attribution}<br>'
            "Новый выбор заменит модель языка после сохранения. Старые файлы останутся в кэше. "
            "«Точная» — более тяжёлый профиль; меньший WER не гарантируется."
        )

    def _publish(self, event):
        with self._events_lock:
            self._events.append(event)

    def start_download(self):
        spec = self.selected_spec()
        if self.downloading or not self.local_enabled or spec is None:
            return
        terms = ""
        if spec.id == "uk-compact":
            terms = (
                "\nMoonshine AI Community License: личное использование и оценка разрешены; "
                "коммерческое использование требует регистрации и соблюдения условий. "
                "Ссылка на полный текст лицензии показана над кнопкой."
            )
        answer = QMessageBox.question(
            self,
            "Загрузка модели",
            f"Скачать или проверить {spec.label}?\nРазмер: {spec.size_bytes:,} байт. "
            f"Лицензия: {spec.license}.\nФайлы будут в пользовательском кэше VoxTypeX. "
            f"Предыдущая модель не удаляется.{terms}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._cancel = Event()
        context = OperationContext(0, self._cancel, monotonic() + 3600)
        self.downloading = True
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.download_status.setText("Подключение к серверу / проверка кэша…")
        self.refresh()

        def download():
            category = "ready"
            try:
                self.manager.download(
                    spec.id, context=context, on_progress=lambda event: self._publish(("progress", event))
                )
            except (OperationCancelled, OperationTimeout):
                category = "cancelled"
            except Exception as exc:
                category = getattr(exc, "category", "io")
                logging.getLogger(__name__).error(
                    "Model installation failed (%s, %s)", category, type(exc).__name__
                )
            finally:
                self._publish(("finished", category))

        self._thread = Thread(target=download, name="voxtypex-model-download", daemon=True)
        self._thread.start()

    def poll_download(self):
        with self._events_lock:
            events = tuple(self._events)
            self._events.clear()
        for kind, event in events:
            if kind == "progress":
                self.progress.setValue(max(1, int(event.received * 10000 / max(1, event.total))))
                self.download_status.setText(
                    "Проверка SHA256…"
                    if event.message_category == "verifying"
                    else f"{event.received / 1_000_000:.1f} / {event.total / 1_000_000:.1f} МБ"
                )
            else:
                self.downloading = False
                self.download_status.setText(
                    {
                        "ready": "Модель готова. Нажмите «Сохранить», чтобы применить выбор.",
                        "cancelled": "Скачивание отменено. Частичный файл сохранён для докачки.",
                        "disk_space": "Недостаточно свободного места на диске.",
                        "network": "Сеть недоступна. Повторите скачивание для докачки.",
                        "integrity": "Размер или SHA256 не совпал. Файл не установлен; повторите загрузку.",
                    }.get(
                        event,
                        "Не удалось установить модель. Проверьте сеть, права доступа и свободное место.",
                    )
                )
                self.refresh()

    def cancel_download(self):
        self._cancel.set()
        if self.downloading:
            self.download_status.setText("Отмена… ожидаю завершения сетевого чтения.")

    def shutdown(self):
        self.cancel_download()
        self.timer.stop()
