import math
import sys
from array import array
from dataclasses import dataclass, replace
from threading import Event, Lock, Thread
from time import monotonic, perf_counter

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..microphones import open_input_lease


@dataclass(frozen=True)
class TestSnapshot:
    state: str = "idle"
    message: str = "Тест запускается только кнопкой; запись хранится в памяти до закрытия настроек."
    diagnostics: object = None
    comparison_ms: float | None = None
    processed_peak_dbfs: float | None = None
    processed_clipped_samples: int | None = None
    comparison_latency_ms: float | None = None
    raw_peak_dbfs: float | None = None
    raw_clipped_samples: int | None = None


def _pcm_metrics(clip):
    values = array("h", clip.pcm)
    if sys.byteorder != "little":
        values.byteswap()
    peak = max((abs(value) for value in values), default=0)
    peak_dbfs = 20 * math.log10(peak / 32768) if peak else float("-inf")
    clipped = sum(value in (-32768, 32767) for value in values)
    return peak_dbfs, clipped


def _recorder(**kwargs):
    from ..audio import MicrophoneRecorder

    return MicrophoneRecorder(**kwargs)


def _process(clip, mode):
    from ..denoising import Denoiser

    processor = Denoiser(mode)
    try:
        return replace(clip, pcm=processor.process(clip.pcm) + processor.flush())
    finally:
        processor.close()


def _play(clip, cancelled):
    import sounddevice as backend

    # Playback holds a refresh lease; short writes allow cancellation.
    lease = open_input_lease(backend)
    lease.__enter__()
    stream = None
    try:
        stream = backend.RawOutputStream(
            samplerate=clip.sample_rate, channels=clip.channels, dtype="int16", blocksize=320
        )
        stream.start()
        for offset in range(0, len(clip.pcm), 640):
            if cancelled.is_set():
                break
            stream.write(clip.pcm[offset : offset + 640])
    finally:
        if stream is None:
            lease.__exit__(None, None, None)
        else:
            try:
                stream.abort() if cancelled.is_set() else stream.stop()
            finally:
                # Keep the lease if close fails; the driver may still own the stream.
                stream.close()
                lease.__exit__(None, None, None)


class MicrophoneTestSession:
    """Keep up to three seconds of test audio in RAM; workers publish metadata."""

    def __init__(self, *, recorder_factory=None, processor=None, player=None, duration=3.0,
                 default_binding=None):
        self._recorder_factory = recorder_factory or _recorder
        self._processor = processor or _process
        self._player = player or _play
        self.default_binding = default_binding
        self.duration = min(3.0, max(0.05, duration))
        self._lock = Lock()
        self._busy = False
        self._generation = 0
        self._cancel = Event()
        self._held = Event()
        self._clips = {}
        self._snapshot = TestSnapshot()

    @property
    def busy(self):
        with self._lock:
            return self._busy

    @property
    def has_clip(self):
        with self._lock:
            return bool(self._clips)

    @property
    def has_processed(self):
        with self._lock:
            return "processed" in self._clips

    def poll(self):
        with self._lock:
            return self._snapshot

    def _publish(self, generation, snapshot):
        with self._lock:
            if generation == self._generation:
                self._snapshot = snapshot

    def start(self, settings):
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._generation += 1
            generation = self._generation
            self._clips = {}
            self._cancel, self._held = Event(), Event()
            cancel, held = self._cancel, self._held
            held.set()
            self._snapshot = TestSnapshot("recording", "Идёт явная тестовая запись · до 3 секунд…")
        Thread(
            target=self._capture,
            args=(generation, settings, cancel, held),
            name="voxtypex-mic-test",
            daemon=True,
        ).start()
        return True

    def _capture(self, generation, settings, cancel, held):
        recorder = None
        try:
            raw_settings = replace(
                settings, vad_enabled=False, noise_suppression="off", auto_stop_silence_seconds=0.0
            )
            options = dict(device=settings.microphone, settings=raw_settings)
            if self.default_binding is not None:
                options["default_binding"] = self.default_binding
            recorder = self._recorder_factory(**options)
            if cancel.is_set():
                return
            recorder.start(held)
            deadline = monotonic() + self.duration
            while not cancel.wait(min(0.05, max(0, deadline - monotonic()))):
                recorder.check_health()
                diagnostics = getattr(recorder, "diagnostics", None)
                self._publish(
                    generation,
                    TestSnapshot("recording", "Говорите в микрофон · тест до 3 секунд…", diagnostics),
                )
                if monotonic() >= deadline:
                    break
            held.clear()
            if cancel.is_set():
                return
            raw = recorder.stop()
            raw = replace(
                raw, pcm=raw.pcm[: int(raw.sample_rate * self.duration) * raw.sample_width * raw.channels]
            )
            diagnostics = getattr(recorder, "diagnostics", None)
            self._publish(
                generation, TestSnapshot("processing", "Готовлю сравнение той же записи…", diagnostics)
            )
            processing_started = perf_counter()
            processed = (
                self._processor(raw, settings.noise_suppression)
                if settings.noise_suppression != "off"
                else None
            )
            comparison_ms = (perf_counter() - processing_started) * 1000 if processed is not None else None
            raw_peak, raw_clipped = _pcm_metrics(raw)
            peak, clipped, latency = None, None, None
            if processed is not None:
                from ..denoising import LATENCY_SAMPLES, SAMPLE_RATE

                peak, clipped = _pcm_metrics(processed)
                latency = LATENCY_SAMPLES * 1000 / SAMPLE_RATE
            with self._lock:
                if generation == self._generation and not cancel.is_set():
                    self._clips = {"raw": raw}
                    if processed is not None:
                        self._clips["processed"] = processed
            message = "Тест готов. Прослушивание запускается только кнопкой."
            if settings.noise_suppression == "off":
                message += " Для сравнения выберите шумоподавление и запишите новый тест."
            self._publish(
                generation,
                TestSnapshot(
                    "ready",
                    message,
                    diagnostics,
                    comparison_ms,
                    peak,
                    clipped,
                    latency,
                    raw_peak,
                    raw_clipped,
                ),
            )
        except Exception as exc:
            self._publish(generation, TestSnapshot("error", f"Тест остановлен: {exc}"))
        finally:
            held.clear()
            try:
                if recorder is not None:
                    recorder.close()
            finally:
                with self._lock:
                    self._busy = False

    def play(self, kind):
        with self._lock:
            if self._busy or kind not in self._clips:
                return False
            clip = self._clips[kind]
            self._busy = True
            self._cancel = Event()
            cancel, generation = self._cancel, self._generation
            previous = self._snapshot
            self._snapshot = replace(previous, state="playing", message="Прослушивание теста…")

        def playback():
            try:
                self._player(clip, cancel)
                self._publish(generation, replace(previous, state="ready"))
            except Exception as exc:
                self._publish(
                    generation, replace(previous, state="error", message=f"Не удалось воспроизвести: {exc}")
                )
            finally:
                with self._lock:
                    self._busy = False

        Thread(target=playback, name="voxtypex-test-playback", daemon=True).start()
        return True

    def dismiss(self):
        with self._lock:
            self._generation += 1
            self._cancel.set()
            self._held.clear()
            self._clips = {}
            self._snapshot = TestSnapshot()


class MicrophonePanel(QWidget):
    def __init__(self, settings_provider, *, session=None):
        super().__init__()
        self.settings_provider = settings_provider
        self.session = session or MicrophoneTestSession()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.noise = QComboBox()
        for label, value in (("Выключено", "off"), ("Умеренное", "moderate"), ("Сильное", "strong")):
            self.noise.addItem(label, value)
        form.addRow("Шумоподавление", self.noise)
        self.vad = QCheckBox("Определять речь (VAD)")
        form.addRow(self.vad)
        self.vad_mode = QComboBox()
        for label, value in (
            ("0 — мягкий", 0),
            ("1 — обычный", 1),
            ("2 — строгий", 2),
            ("3 — очень строгий", 3),
        ):
            self.vad_mode.addItem(label, value)
        form.addRow("Чувствительность VAD", self.vad_mode)
        self.preroll = QSpinBox()
        self.preroll.setRange(200, 400)
        self.preroll.setSingleStep(10)
        self.preroll.setSuffix(" мс")
        form.addRow("Сохранить перед речью", self.preroll)
        self.hangover = QSpinBox()
        self.hangover.setRange(300, 1000)
        self.hangover.setSingleStep(10)
        self.hangover.setSuffix(" мс")
        form.addRow("Пауза после речи", self.hangover)
        self.auto_stop = QCheckBox("Завершать после тишины")
        self.silence = QDoubleSpinBox()
        self.silence.setRange(1.5, 10.0)
        self.silence.setSingleStep(0.5)
        self.silence.setSuffix(" с")
        row = QHBoxLayout()
        row.addWidget(self.auto_stop)
        row.addWidget(self.silence)
        form.addRow(row)
        layout.addLayout(form)
        hint = QLabel(
            "VAD сохраняет начало и конец речи. Сильное подавление может менять голос. "
            "Тест сравнивает одну запись с обработкой и без неё; VAD в тесте отключён."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        self.test_button = QPushButton("Записать тест · 3 с")
        self.stop_button = QPushButton("Стоп / очистить")
        self.raw_button = QPushButton("Слушать оригинал")
        self.processed_button = QPushButton("Слушать обработку")
        for button in (self.test_button, self.stop_button, self.raw_button, self.processed_button):
            row.addWidget(button)
        layout.addLayout(row)
        self.meter = QProgressBar()
        self.meter.setRange(0, 60)
        self.meter.setFormat("Уровень — только во время теста")
        layout.addWidget(self.meter)
        self.metrics = QLabel("RMS: — · Пик: — · Клиппинг: — · Задержка DSP: —")
        self.metrics.setWordWrap(True)
        layout.addWidget(self.metrics)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.test_button.clicked.connect(self.start_test)
        self.stop_button.clicked.connect(self.cancel_test)
        self.raw_button.clicked.connect(lambda: self.session.play("raw"))
        self.processed_button.clicked.connect(lambda: self.session.play("processed"))
        self.noise.currentIndexChanged.connect(self.cancel_test)
        self.vad.toggled.connect(self._enable_controls)
        self.auto_stop.toggled.connect(self._enable_controls)
        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.poll)
        self.poll()

    def populate(self, settings):
        self.cancel_test()
        self.noise.setCurrentIndex(self.noise.findData(settings.noise_suppression))
        self.vad.setChecked(settings.vad_enabled)
        self.vad_mode.setCurrentIndex(self.vad_mode.findData(settings.vad_mode))
        self.preroll.setValue(settings.vad_preroll_ms)
        self.hangover.setValue(settings.vad_hangover_ms)
        self.auto_stop.setChecked(settings.auto_stop_silence_seconds > 0)
        self.silence.setValue(settings.auto_stop_silence_seconds or 3.0)
        self._enable_controls()

    def selected_values(self):
        return dict(
            noise_suppression=self.noise.currentData(),
            vad_enabled=self.vad.isChecked(),
            vad_mode=self.vad_mode.currentData(),
            vad_preroll_ms=self.preroll.value(),
            vad_hangover_ms=self.hangover.value(),
            auto_stop_silence_seconds=self.silence.value() if self.auto_stop.isChecked() else 0.0,
        )

    def _enable_controls(self):
        for widget in (self.vad_mode, self.preroll, self.hangover, self.auto_stop):
            widget.setEnabled(self.vad.isChecked())
        self.silence.setEnabled(self.vad.isChecked() and self.auto_stop.isChecked())

    def start_test(self):
        self.session.start(self.settings_provider())
        self.poll()

    def cancel_test(self, *_):
        self.session.dismiss()
        self.poll()

    def poll(self):
        snapshot, busy = self.session.poll(), self.session.busy
        self.status.setText(snapshot.message)
        self.test_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy or self.session.has_clip)
        self.raw_button.setEnabled(not busy and self.session.has_clip)
        self.processed_button.setEnabled(not busy and self.session.has_processed)
        diagnostics = snapshot.diagnostics
        if diagnostics is None:
            self.meter.setValue(0)
            self.meter.setFormat("Уровень — только во время теста")
            self.metrics.setText("RMS: — · Пик: — · Клиппинг: — · Задержка DSP: —")
            return
        rms = diagnostics.rms_dbfs
        peak = snapshot.raw_peak_dbfs if snapshot.raw_peak_dbfs is not None else diagnostics.peak_dbfs
        self.meter.setValue(round(max(0, min(60, rms + 60))) if math.isfinite(rms) else 0)
        self.meter.setFormat(f"RMS {rms:.1f} dBFS")
        latency = (
            snapshot.comparison_latency_ms
            if snapshot.comparison_latency_ms is not None
            else getattr(diagnostics, "latency_ms", None)
        )
        latency_label = f"{latency:.1f} мс" if latency is not None else "—"
        if snapshot.comparison_latency_ms:
            latency_label += " (компенсирована)"
        clipping = (
            snapshot.raw_clipped_samples
            if snapshot.raw_clipped_samples is not None
            else diagnostics.clipped_samples
        )
        timing = (
            f" · Обработка теста: {snapshot.comparison_ms:.1f} мс"
            if snapshot.comparison_ms is not None
            else f" · Обработка кадра: {getattr(diagnostics, 'processing_ms', 0):.2f} мс"
        )
        self.metrics.setText(
            f"{'Исходный пик' if snapshot.raw_peak_dbfs is not None else 'Пик'}: "
            f"{peak:.1f} dBFS · Клиппинг: {clipping} отсч. "
            f"{'— уменьшите усиление' if clipping else ''}"
            + (
                f"\nПосле подавления: пик {snapshot.processed_peak_dbfs:.1f} dBFS · "
                f"Клиппинг: {snapshot.processed_clipped_samples} отсч."
                if snapshot.processed_peak_dbfs is not None
                else ""
            )
            + f" · Задержка DSP: {latency_label}{timing}"
        )

    def showEvent(self, event):
        self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        self.cancel_test()
        super().hideEvent(event)
