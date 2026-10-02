import logging
from dataclasses import replace
from time import monotonic

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from ..config import Settings, SettingsService
from ..state import AppController
from .settings_window import SettingsWindow
from .status_overlay import PROGRESS_TEXT, StatusOverlay


def application_icon(color="#2463a0") -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor(color))
    painter = QPainter(pixmap)
    painter.setPen(QColor("white"))
    font = painter.font()
    font.setPixelSize(23)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(7, 25, "V")
    painter.end()
    return QIcon(pixmap)


class TrayApplication:
    def __init__(self, app: QApplication, store: SettingsService, settings: Settings, service=None) -> None:
        self.app = app
        self.service = service
        self.store = store
        self.store.current = settings
        self.controller = service.controller if service is not None else AppController()
        self.quitting = False
        self._quit_deadline = None
        self._reported_error = ""
        self.overlay = StatusOverlay()
        self.window = SettingsWindow(store, settings)
        self.window.settings_saved.connect(self.apply_settings)
        if service is not None:
            self.window.visibility_changed.connect(service.set_settings_open)
        self.icon = application_icon()
        self.window.setWindowIcon(self.icon)
        self.tray = QSystemTrayIcon(self.icon, app)
        self.tray.setToolTip("VoxTypeX — IDLE")
        self.menu = QMenu()
        self.status_action = self.menu.addAction("IDLE — удерживайте PTT для записи")
        self.status_action.setEnabled(False)
        self.settings_action = self.menu.addAction("Настройки…")
        self.settings_action.triggered.connect(self.window.open_settings)
        self.enable_action = self.menu.addAction("Отключить" if settings.enabled else "Включить")
        self.enable_action.triggered.connect(self.toggle_enabled)
        self.menu.addSeparator()
        self.quit_action = self.menu.addAction("Выход")
        self.quit_action.triggered.connect(self.request_quit)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.on_activation)
        app.aboutToQuit.connect(self.close)
        self.poll = QTimer(self.window)
        self.poll.setInterval(100)
        self.poll.timeout.connect(self.refresh)
        self._icon_color = "#2463a0"
        self.refresh()

    def apply_settings(self, settings):
        if self.service is not None:
            self.service.update_settings(settings)
        self.refresh()

    def toggle_enabled(self):
        settings = replace(self.store.current, enabled=not self.store.current.enabled)
        try:
            self.store.save(settings)
        except (OSError, ValueError):
            logging.getLogger(__name__).exception("Could not save enabled state")
            QMessageBox.warning(
                self.window, "VoxTypeX", "Не удалось сохранить состояние. Настройки не изменены."
            )
            return
        self.apply_settings(settings)

    def start(self) -> None:
        available = QSystemTrayIcon.isSystemTrayAvailable()
        self.app.setQuitOnLastWindowClosed(not available)
        self.tray.show()
        if self.service is not None:
            self.service.start()
            self.poll.start()
        if not available:
            logging.getLogger(__name__).warning("System tray unavailable; showing settings")
            self.window.open_settings()
        logging.getLogger(__name__).info("Application started; tray_available=%s", available)

    def refresh(self):
        state = self.controller.state.name
        settings = self.store.current
        enable_label = "Отключить" if settings.enabled else "Включить"
        if self.enable_action.text() != enable_label:
            self.enable_action.setText(enable_label)
        if not settings.enabled:
            state = "DISABLED"
        description = {
            "IDLE": "Ready — готово, удерживайте PTT",
            "RECORDING": "Recording — запись",
            "TRANSCRIBING": "Transcribing — распознавание",
            "INSERTING": "Вставка текста",
            "ERROR": "Error — ошибка",
            "DISABLED": "Disabled — выключено",
        }[state]
        progress = getattr(self.service, "progress", "transcribing")
        if state == "TRANSCRIBING":
            description = "Transcribing — " + PROGRESS_TEXT.get(progress, PROGRESS_TEXT["transcribing"])
        label = f"VoxTypeX — {description}"
        if self.quitting:
            label = "VoxTypeX — завершение работы…"
        if self.tray.toolTip() != label:
            self.tray.setToolTip(label)
            self.status_action.setText(label)
        error = self.service.last_error if self.service is not None else ""
        self.overlay.update_status(state, error, settings.visual_indication and not self.quitting, progress)
        color = "#777777" if not settings.enabled else "#2463a0"
        if settings.enabled and settings.visual_indication:
            color = {
                "RECORDING": "#d83b3b",
                "TRANSCRIBING": "#bb8100",
                "INSERTING": "#25864a",
                "ERROR": "#a92d45",
            }.get(state, color)
        if color != self._icon_color:
            self.tray.setIcon(application_icon(color))
            self._icon_color = color
        if self.service is None:
            return
        if not self.service.last_error:
            self._reported_error = ""
        if not self.quitting and self.service.last_error and self.service.last_error != self._reported_error:
            self._reported_error = self.service.last_error
            self.tray.showMessage(
                "VoxTypeX — ошибка",
                self.service.last_error[:400] + "\nПодробности в журнале. Отпустите PTT и повторите попытку.",
                QSystemTrayIcon.MessageIcon.Warning,
            )
        if self.quitting and self.service.finished.is_set():
            self.app.quit()
        elif self.quitting and monotonic() >= self._quit_deadline:
            logging.getLogger(__name__).error(
                "Shutdown deadline exceeded; closing UI despite blocked native driver"
            )
            self.app.quit()

    def request_quit(self):
        if self.quitting:
            return
        if self.service is None:
            self.app.quit()
            return
        self.quitting = True
        self._quit_deadline = monotonic() + 5
        self.settings_action.setEnabled(False)
        self.enable_action.setEnabled(False)
        self.service.request_close()
        self.refresh()

    def on_activation(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.window.open_settings()

    def close(self) -> None:
        if self.controller.closed:
            return
        self.poll.stop()
        if self.service is not None:
            self.service.close()
        self.controller.close()
        self.tray.hide()
        self.overlay.close()
        self.window.close()
        self.menu.close()
        logging.getLogger(__name__).info("Application stopped")
