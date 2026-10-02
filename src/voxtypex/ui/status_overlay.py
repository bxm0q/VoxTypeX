"""Small click-through status badge; never activates another window."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QLabel

PROGRESS_TEXT = {
    "starting": "Запускаю распознавание…",
    "downloading": "Скачиваю модель… Первый запуск может занять несколько минут.",
    "loading": "Загружаю модель в память…",
    "transcribing": "Распознаю речь…",
}


class StatusOverlay(QLabel):
    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWindowTitle("VoxTypeX status")
        self.setMargin(12)
        self._snapshot = None
        self._expiry = QTimer(self)
        self._expiry.setSingleShot(True)
        self._expiry.timeout.connect(self.hide)

    def update_status(self, state, error, enabled, progress="transcribing"):
        snapshot = (state, error, enabled, progress)
        if snapshot == self._snapshot:
            return
        self._snapshot = snapshot
        self._expiry.stop()
        if not enabled or state not in ("RECORDING", "TRANSCRIBING", "INSERTING", "ERROR"):
            self.hide()
            return
        text, color = {
            "RECORDING": ("●  Запись — отпустите PTT для обработки", "#b32632"),
            "TRANSCRIBING": ("◌  " + PROGRESS_TEXT.get(progress, PROGRESS_TEXT["transcribing"]), "#835900"),
            "INSERTING": ("◌  Вставляю текст…", "#246344"),
            "ERROR": (error or "Ошибка. Подробности в журнале.", "#9c2942"),
        }[state]
        self.setText(text)
        self.setStyleSheet(
            f"QLabel {{ background: {color}; color: white; border-radius: 8px; font-size: 13px; }}"
        )
        self.setWordWrap(True)
        self.setFixedWidth(370)
        self.adjustSize()
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.right() - self.width() - 20, area.bottom() - self.height() - 20)
        self.show()
        if state == "ERROR":
            self._expiry.start(6000)
