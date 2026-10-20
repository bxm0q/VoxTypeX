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
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..config import Settings, SettingsService
from ..key_policy import KeyCapture, key_name
from ..microphones import input_devices
from ..models import WhisperModelRegistry
from .local_models_panel import LocalModelsPanel
from .microphone_panel import MicrophonePanel


def native_key(event):
    """Windows may report generic VK_CONTROL/VK_MENU for right-hand keys."""
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
    microphone_reset_requested = Signal()

    def __init__(self, store: SettingsService, settings: Settings, microphone_provider=input_devices):
        super().__init__()
        self.store, self.settings = store, settings
        self.microphone_provider = microphone_provider
        self.capture = None
        self._devices_generation = 0
        self._devices_query_active = False
        self._microphone_reset_pending = False
        self.setWindowTitle("VoxTypeX — настройки")
        self.setMinimumWidth(740)
        screen = QApplication.primaryScreen()
        height = min(820, screen.availableGeometry().height() - 80) if screen else 820
        self.resize(800, max(420, height))
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.addWidget(
            QLabel(
                "Запись идёт, пока зажата клавиша PTT. Esc — отмена.\nПри открытых настройках запись приостановлена."
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
        self.microphone.currentTextChanged.connect(self.microphone.setToolTip)
        self.microphone.activated.connect(self._microphone_activated)
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
        self.fallback_device = QPushButton("Использовать микрофон по умолчанию Windows…")
        self.fallback_device.clicked.connect(self.use_windows_default)
        form.addRow(self.fallback_device)
        self.audio_panel = MicrophonePanel(self._audio_test_settings)
        self.microphone.currentIndexChanged.connect(self.audio_panel.cancel_test)
        form.addRow(self.audio_panel)
        self.language = QComboBox()
        for title, value in (
            ("Автоматически", None),
            ("Русский", "ru"),
            ("Українська", "uk"),
            ("English", "en"),
        ):
            self.language.addItem(title, value)
        form.addRow("Язык", self.language)
        self.asr_backend = QComboBox()
        self.asr_backend.addItem("Whisper", "faster-whisper")
        self.asr_backend.addItem("Отдельная локальная модель языка", "transcribe-cpp")
        form.addRow("Распознавание", self.asr_backend)
        self.model = QComboBox()
        for model in WhisperModelRegistry().models():
            self.model.addItem(model.label, model.id)
        form.addRow("Whisper / fallback", self.model)
        hint = QLabel(
            "Whisper скачивается при первом распознавании; отдельные модели — кнопкой ниже.\n"
            "Клавиши не блокируются в других приложениях: выбирайте свободную."
        )
        hint.setWordWrap(True)
        form.addRow(hint)
        self.local_models = LocalModelsPanel()
        form.addRow(self.local_models)
        self.language.currentIndexChanged.connect(self._refresh_local_context)
        self.asr_backend.currentIndexChanged.connect(self._refresh_local_context)
        self.visual = QCheckBox("Показывать индикатор записи и обработки")
        form.addRow("Индикация", self.visual)
        self.preview_mode = QComboBox()
        self.preview_mode.addItem("Выключен", "off")
        self.preview_mode.addItem("Предпросмотр в индикаторе", "preview")
        form.addRow("Текст во время записи", self.preview_mode)
        self.preview_interval = QDoubleSpinBox()
        self.preview_interval.setRange(1, 10)
        self.preview_interval.setSingleStep(0.5)
        self.preview_interval.setSuffix(" с")
        form.addRow("Не чаще одного обновления в", self.preview_interval)
        self.preview_window = QDoubleSpinBox()
        self.preview_window.setRange(4, 20)
        self.preview_window.setSuffix(" с")
        form.addRow("Окно предпросмотра", self.preview_window)
        self.preview_hint = QLabel(
            "Whisper tiny/base/small, GigaAM, Moonshine, Canary и Parakeet: batch-предпросмотр, "
            "без потокового decode. Нужны VAD и индикация. На медленном CPU обновления реже.\n"
            "В поле попадёт только один итог после отпускания PTT. live-typing недоступен."
        )
        self.preview_hint.setWordWrap(True)
        form.addRow(self.preview_hint)
        content = QWidget()
        content.setLayout(form)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout.addWidget(scroll)
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
        self.devices_timer = QTimer(self)
        self.devices_timer.setInterval(3000)
        self.devices_timer.timeout.connect(self.load_devices)

    def open_settings(self):
        if self.isVisible():
            self.raise_()
            self.activateWindow()
            return
        self.settings = self.store.current
        self._microphone_reset_pending = False
        self.key_label.setText(key_name(self.settings.push_to_talk_key))
        self.language.setCurrentIndex(self.language.findData(self.settings.language))
        self.asr_backend.setCurrentIndex(self.asr_backend.findData(self.settings.asr_backend))
        self.local_models.populate(self.settings)
        self._refresh_local_context()
        while self.model.count() > 3:
            self.model.removeItem(3)
        if self.settings.model_path:
            self.model.addItem("Локальная модель из настроек", "local")
            self.model.setCurrentIndex(3)
        else:
            self.model.setCurrentIndex(self.model.findData(self.settings.whisper_model))
        self.visual.setChecked(self.settings.visual_indication)
        self.preview_mode.setCurrentIndex(self.preview_mode.findData(self.settings.preview_mode))
        self.preview_interval.setValue(self.settings.preview_interval_seconds)
        self.preview_window.setValue(self.settings.preview_window_seconds)
        self.audio_panel.populate(self.settings)
        self.capture_status.setText("Одна клавиша. Новое назначение сохраняется сразу.")
        self._select_microphone(self.settings.microphone)
        self.visibility_changed.emit(True)
        self.show()
        self.raise_()
        self.activateWindow()
        self.load_devices()
        self.devices_timer.start()

    def _audio_test_settings(self):
        return replace(
            self.store.current, microphone=self.microphone.currentData(), **self.audio_panel.selected_values()
        )

    def use_windows_default(self):
        if (
            QMessageBox.question(
                self,
                "VoxTypeX — сменить микрофон?",
                "Выбрать и сохранить текущий микрофон по умолчанию Windows? "
                "Запись будет использовать другое устройство только после этого подтверждения.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        settings = replace(self.store.current, microphone=None)
        if self._persist(settings):
            self._microphone_reset_pending = False
            self.microphone_reset_requested.emit()
            self.audio_panel.cancel_test()
            self._select_microphone(None)
            self.device_status.setText("Выбран микрофон по умолчанию Windows.")

    def _microphone_activated(self, index):
        # Only user activation permits rebinding the default microphone.
        self._microphone_reset_pending = self.microphone.itemData(index) is None

    def _select_microphone(self, identity):
        index = self.microphone.findData(identity)
        if index < 0:
            self.microphone.addItem("Сохранённый микрофон — проверка доступности…", identity)
            index = self.microphone.count() - 1
        self.microphone.setCurrentIndex(index)

    def _refresh_local_context(self, *_):
        self.local_models.set_context(
            self.language.currentData(),
            self.asr_backend.currentData() == "transcribe-cpp",
        )

    def load_devices(self):
        if self._devices_query_active or self.capture is not None or not self.isVisible():
            return
        self._devices_query_active = True
        self._devices_generation += 1
        generation = self._devices_generation
        self.refresh_devices.setEnabled(False)
        provider = self.microphone_provider

        def query():
            try:
                devices = provider(refresh=True) if provider is input_devices else provider()
                error = ""
            except Exception as exc:
                logging.getLogger(__name__).exception("Could not enumerate microphones")
                devices, error = [], str(exc)
            try:
                self.devices_ready.emit(generation, devices, error)
            except RuntimeError:
                # The dialog may be gone by the time enumeration finishes.
                pass

        Thread(target=query, name="voxtypex-devices", daemon=True).start()

    def _show_devices(self, generation, devices, error):
        self._devices_query_active = False
        if generation != self._devices_generation or not self.isVisible():
            return
        selected = self.microphone.currentData()
        self.microphone.blockSignals(True)
        self.microphone.clear()
        default_label = next((label for _, label, _ in devices if "по умолчанию Windows" in label), "")
        self.microphone.addItem(
            f"По умолчанию Windows: {default_label}" if default_label else "По умолчанию в Windows", None
        )
        for identity, label, _ in devices:
            self.microphone.addItem(label, identity)
            self.microphone.setItemData(self.microphone.count() - 1, label, Qt.ItemDataRole.ToolTipRole)
        missing = selected is not None and self.microphone.findData(selected) < 0
        if missing:
            self.microphone.addItem("Недоступен — выберите другой микрофон", selected)
        self._select_microphone(selected)
        self.microphone.blockSignals(False)
        self.microphone.setToolTip(self.microphone.currentText())
        if missing:
            self.audio_panel.cancel_test()
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
        self.audio_panel.cancel_test()
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
            self.audio_panel,
            self.fallback_device,
            self.preview_mode,
            self.preview_interval,
            self.preview_window,
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
            asr_backend=self.asr_backend.currentData(),
            preview_mode=self.preview_mode.currentData(),
            preview_interval_seconds=self.preview_interval.value(),
            preview_window_seconds=self.preview_window.value(),
            **self.local_models.selected_qualities(),
            **self.audio_panel.selected_values(),
        )
        if self._persist(settings):
            if self._microphone_reset_pending and settings.microphone is None:
                self.microphone_reset_requested.emit()
            self._microphone_reset_pending = False
            self.close()

    def hideEvent(self, event):
        self.devices_timer.stop()
        self._devices_generation += 1
        self.audio_panel.cancel_test()
        self.stop_capture()
        self.visibility_changed.emit(False)
        super().hideEvent(event)
