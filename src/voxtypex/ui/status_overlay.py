from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QLabel

from ..interfaces import TranscriptKind

PROGRESS_TEXT = {
    "starting": "Запускаю распознавание…",
    "downloading": "Скачиваю модель… Первый запуск может занять несколько минут.",
    "loading": "Загружаю модель в память…",
    "transcribing": "Распознаю речь…",
    "fallback": "Перехожу на Whisper…",
}


class StatusOverlay(QLabel):
    hypothesis_visible = Signal(object)

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
        self._transcript = None
        self._transcript_order = (0, 0, -1)
        self._painted_order = None
        self._status = ("IDLE", "", False, "transcribing", "")
        self._expiry = QTimer(self)
        self._expiry.setSingleShot(True)
        self._expiry.timeout.connect(self.hide)

    def paintEvent(self, event):
        super().paintEvent(event)
        if (
            self._transcript is not None and self._transcript.kind is TranscriptKind.PARTIAL
            and self._status[0] == "RECORDING" and self._painted_order != self._transcript_order
        ):
            self._painted_order = self._transcript_order
            self.hypothesis_visible.emit(self._transcript)

    def update_transcript(self, event):
        order = (event.session_id, event.generation, event.sequence)
        if order <= self._transcript_order:
            return
        self._transcript_order = order
        self._transcript = None if event.kind is TranscriptKind.RESET else event
        self.update_status(*self._status)

    def update_status(self, state, error, enabled, progress="transcribing", preview_status=""):
        self._status = (state, error, enabled, progress, preview_status)
        snapshot = (*self._status, self._transcript)
        if snapshot == self._snapshot:
            return
        self._snapshot = snapshot
        self._expiry.stop()
        if not enabled or state not in ("RECORDING", "TRANSCRIBING", "INSERTING", "ERROR"):
            self._transcript = None
            self.clear()
            self.hide()
            return
        text, color = {
            "RECORDING": ("●  Запись — отпустите PTT для обработки", "#b32632"),
            "TRANSCRIBING": ("◌  " + PROGRESS_TEXT.get(progress, PROGRESS_TEXT["transcribing"]), "#835900"),
            "INSERTING": ("◌  Вставляю текст…", "#246344"),
            "ERROR": (error or "Ошибка. Подробности в журнале.", "#9c2942"),
        }[state]
        if state == "RECORDING" and preview_status:
            text += "\n" + preview_status
        if state in ("RECORDING", "TRANSCRIBING", "INSERTING") and self._transcript is not None:
            stable = self._transcript.stable_text
            tail = self._transcript.unstable_text
            if self._transcript.kind is TranscriptKind.FINAL:
                text += "\nИтог: " + stable[-700:]
            else:
                visible = " ".join(filter(None, (stable, tail)))[-700:]
                text += "\n" + visible
        if state == "ERROR":
            self._transcript = None
        self.setText(text)
        self.setStyleSheet(
            f"QLabel {{ background: {color}; color: white; border-radius: 8px; font-size: 13px; }}"
        )
        self.setWordWrap(True)
        self.setFixedWidth(460 if self._transcript is not None else 370)
        self.setMaximumHeight(340)
        self.adjustSize()
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.right() - self.width() - 20, area.bottom() - self.height() - 20)
        self.show()
        if state == "ERROR":
            self._expiry.start(6000)
