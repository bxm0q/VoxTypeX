"""Small settings dialog; global PTT is gated for the entire visible session."""

import logging
from dataclasses import replace
from threading import Thread

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import Settings, SettingsService
from ..key_policy import KeyCapture, key_name
from ..microphones import input_devices


def native_key(event):
    """Qt/Windows may report generic VK_CONTROL/VK_MENU even for right-hand keys."""
    vk, key, scan = event.nativeVirtualKey(), event.key(), event.nativeScanCode()
    if key == Qt.Key.Key_AltGr:
        return 0xA5
    if vk == 0x12:
        return 0xA5 if scan in (0x138, 0xE038) else 0xA4
    if vk == 0x11:
        return 0xA3 if scan in (0x11D, 0xE01D) else 0xA2
    if vk == 0x10:
        return 0xA1 if scan == 0x36 else 0xA0
    if vk:
        return vk
    if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
        return 0x70 + key - Qt.Key.Key_F1
    return 0x1B if key == Qt.Key.Key_Escape else key


class SettingsWindow(QDialog):
    settings_saved = Signal(object)
    visibility_changed = Signal(bool)
    devices_ready = Signal(int, object, str)

    def __init__(self, store: SettingsService, settings: Settings, microphone_provider=input_devices):
        super().__init__()
        self.store, self.settings = store, settings
        self.microphone_provider = microphone_provider
        self.capture = None
        self._devices_generation = 0
        self.setWindowTitle("VoxTypeX — настройки")
        self.setMinimumWidth(470)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.addWidget(
            QLabel(
                "Зажмите PTT → говорите → отпустите. Esc — отмена.\nПока настройки открыты, запись приостановлена."
            )
        )
        form = QFormLayout()
        form.setSpacing(10)
        self.key_label = QLabel(key_name(settings.push_to_talk_key))
        self.change_key = QPushButton("Изменить клавишу")
        self.change_key.clicked.connect(self.toggle_capture)
        key_row = QHBoxLayout()
        key_row.addWidget(self.key_label)
        key_row.addWidget(self.change_key)
        form.addRow("Push-to-Talk", key_row)
        self.capture_status = QLabel("Одна клавиша. Новое назначение сохраняется сразу.")
        self.capture_status.setWordWrap(True)
        form.addRow(self.capture_status)
        self.microphone = QComboBox()
        self.microphone.setMinimumContentsLength(26)
        self.microphone.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.microphone.addItem("По умолчанию в Windows", None)
        self.refresh_devices = QPushButton("Обновить")
        self.refresh_devices.clicked.connect(self.load_devices)
        mic_row = QHBoxLayout()
        mic_row.addWidget(self.microphone, 1)
        mic_row.addWidget(self.refresh_devices)
        form.addRow("Микрофон", mic_row)
        self.device_status = QLabel("")
        self.device_status.setWordWrap(True)
        form.addRow(self.device_status)
        self.language = QComboBox()
        for title, value in (
            ("Auto — определить автоматически", None),
            ("Russian — русский", "ru"),
            ("Ukrainian — українська", "uk"),
            ("English", "en"),
        ):
            self.language.addItem(title, value)
        form.addRow("Язык", self.language)
        self.model = QComboBox()
        for title, value in (
            ("Tiny — быстрее", "tiny"),
            ("Base — баланс", "base"),
            ("Small — точнее", "small"),
        ):
            self.model.addItem(title, value)
        form.addRow("Модель", self.model)
        hint = QLabel(
            "Новая модель скачается при первом распознавании.\nКлавиши не блокируются в других приложениях: выбирайте свободную."
        )
        hint.setWordWrap(True)
        form.addRow(hint)
        self.visual = QCheckBox("Показывать индикатор записи и обработки")
        form.addRow("Индикация", self.visual)
        layout.addLayout(form)
        self.buttons = QDialogButtonBox()
        self.save_button = self.buttons.addButton("Сохранить", QDialogButtonBox.ButtonRole.ApplyRole)
        self.buttons.addButton("Закрыть", QDialogButtonBox.ButtonRole.RejectRole)
        self.save_button.clicked.connect(self.save)
        self.buttons.rejected.connect(self.close)
        layout.addWidget(self.buttons)
        self.capture_timer = QTimer(self)
        self.capture_timer.setSingleShot(True)
        self.capture_timer.setInterval(15000)
        self.capture_timer.timeout.connect(
            lambda: self.stop_capture("Назначение отменено: время ожидания истекло.")
        )
        self.devices_ready.connect(self._show_devices)

    def open_settings(self):
        if self.isVisible():
            self.raise_()
            self.activateWindow()
            return
        self.settings = self.store.current
        self.key_label.setText(key_name(self.settings.push_to_talk_key))
        self.language.setCurrentIndex(self.language.findData(self.settings.language))
        while self.model.count() > 3:
            self.model.removeItem(3)
        if self.settings.model_path:
            self.model.addItem("Локальная модель из настроек", "local")
            self.model.setCurrentIndex(3)
        else:
            self.model.setCurrentIndex(self.model.findData(self.settings.whisper_model))
        self.visual.setChecked(self.settings.visual_indication)
        self.capture_status.setText("Одна клавиша. Новое назначение сохраняется сразу.")
        self._select_microphone(self.settings.microphone)
        self.visibility_changed.emit(True)
        self.show()
        self.raise_()
        self.activateWindow()
        self.load_devices()

    def _select_microphone(self, identity):
        index = self.microphone.findData(identity)
        if index < 0:
            self.microphone.addItem("Сохранённый микрофон — проверка доступности…", identity)
            index = self.microphone.count() - 1
        self.microphone.setCurrentIndex(index)

    def load_devices(self):
        self._devices_generation += 1
        generation = self._devices_generation
        self.refresh_devices.setEnabled(False)
        self.device_status.setText("Проверяю устройства…")

        def query():
            try:
                devices, error = self.microphone_provider(), ""
            except Exception as exc:
                logging.getLogger(__name__).exception("Could not enumerate microphones")
                devices, error = [], str(exc)
            try:
                self.devices_ready.emit(generation, devices, error)
            except RuntimeError:
                # Qt receiver may have been destroyed while PortAudio enumerated devices.
                pass

        Thread(target=query, name="voxtypex-devices", daemon=True).start()

    def _show_devices(self, generation, devices, error):
        if generation != self._devices_generation:
            return
        selected = self.microphone.currentData()
        self.microphone.clear()
        self.microphone.addItem("По умолчанию в Windows", None)
        for identity, label, _ in devices:
            self.microphone.addItem(label, identity)
        missing = selected is not None and self.microphone.findData(selected) < 0
        if missing:
            self.microphone.addItem("Недоступен — выберите другой микрофон", selected)
        self._select_microphone(selected)
        self.refresh_devices.setEnabled(self.capture is None)
        self.device_status.setText(
            "Не удалось получить список. Нажмите «Обновить»."
            if error
            else "Сохранённый микрофон отключён. Автоматической подмены не будет."
            if missing
            else "Нет доступных микрофонов."
            if not devices
            else ""
        )

    def toggle_capture(self):
        if self.capture is not None:
            self.stop_capture("Назначение отменено.")
            return
        self.capture = KeyCapture()
        self.change_key.setText("Отмена")
        self.capture_status.setText(
            "Нажмите и отпустите Right Alt/Ctrl, F6–F9, F11–F24 или кнопку мыши 4/5. Esc — отмена."
        )
        self._capture_controls(False)
        QApplication.instance().installEventFilter(self)
        self.capture_timer.start()

    def _capture_controls(self, enabled):
        for widget in (
            self.microphone,
            self.refresh_devices,
            self.language,
            self.model,
            self.visual,
            self.save_button,
        ):
            widget.setEnabled(enabled)

    def stop_capture(self, message=""):
        self.capture_timer.stop()
        QApplication.instance().removeEventFilter(self)
        self.capture = None
        self.change_key.setText("Изменить клавишу")
        self._capture_controls(True)
        if message:
            self.capture_status.setText(message)

    def _capture_event(self, vk, pressed, modifiers=(), repeat=False):
        if self.capture is None:
            return
        status, candidate = self.capture.event(vk, pressed, modifiers, repeat)
        if status == "cancel":
            self.stop_capture("Назначение отменено. Клавиша не изменена.")
        elif status == "unsupported":
            self.capture_status.setText(
                "Эта клавиша или комбинация не подходит. Отпустите клавиши и попробуйте Right Alt/Ctrl, F6–F9, F11–F24 или мышь 4/5."
            )
        elif status == "release":
            self.capture_status.setText(f"Отпустите {key_name(candidate)} для сохранения.")
        elif status == "accepted":
            self.stop_capture()
            if candidate == self.store.current.push_to_talk_key:
                self.capture_status.setText("Эта клавиша уже назначена.")
                return
            settings = replace(self.store.current, push_to_talk_key=candidate)
            if self._persist(settings):
                self.key_label.setText(key_name(candidate))
                self.capture_status.setText(
                    "Клавиша сохранена. Проверьте, не занята ли она в вашем приложении."
                )

    def eventFilter(self, watched, event):
        if self.capture is None:
            return False
        kind = event.type()
        if kind == QEvent.Type.WindowDeactivate and watched is self:
            self.stop_capture("Назначение отменено: окно потеряло фокус.")
            return False
        if watched is not self and (not isinstance(watched, QWidget) or not self.isAncestorOf(watched)):
            return False
        if kind == QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        modifiers = set()
        if hasattr(event, "modifiers"):
            for flag, vk in (
                (Qt.KeyboardModifier.ShiftModifier, 0x10),
                (Qt.KeyboardModifier.ControlModifier, 0x11),
                (Qt.KeyboardModifier.AltModifier, 0x12),
                (Qt.KeyboardModifier.MetaModifier, 0x5B),
            ):
                if event.modifiers() & flag:
                    modifiers.add(vk)
        if kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            vk = native_key(event)
            self._capture_event(vk, kind == QEvent.Type.KeyPress, modifiers, event.isAutoRepeat())
            event.accept()
            return True
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            vk = {Qt.MouseButton.XButton1: 0x05, Qt.MouseButton.XButton2: 0x06}.get(event.button())
            if vk is not None:
                self._capture_event(vk, kind == QEvent.Type.MouseButtonPress, modifiers)
                event.accept()
                return True
        return False

    def _persist(self, settings):
        try:
            self.store.save(settings)
        except (OSError, ValueError):
            logging.getLogger(__name__).exception("Could not save settings")
            QMessageBox.warning(
                self,
                "VoxTypeX",
                "Не удалось сохранить настройки. Старые настройки сохранены. Проверьте доступ к папке данных.",
            )
            return False
        self.settings = settings
        self.settings_saved.emit(settings)
        return True

    def save(self):
        if self.capture is not None:
            return
        base = self.store.current
        choice = self.model.currentData()
        model = base.whisper_model if choice == "local" else choice
        settings = replace(
            base,
            microphone=self.microphone.currentData(),
            language=self.language.currentData(),
            whisper_model=model,
            visual_indication=self.visual.isChecked(),
            model_path=base.model_path if choice == "local" else None,
        )
        if self._persist(settings):
            self.close()

    def hideEvent(self, event):
        self.stop_capture()
        self.visibility_changed.emit(False)
        super().hideEvent(event)
