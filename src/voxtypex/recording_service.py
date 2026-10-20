import logging
import os
import wave
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Lock, Thread
from time import monotonic, perf_counter

from .config import Settings
from .interfaces import (
    MIN_RECORDING_SECONDS,
    AudioRecorder,
    EngineCapabilities,
    HotkeyListener,
    OperationContext,
    OperationTimeout,
    ProgressEvent,
    ProgressStage,
    SpeechPipeline,
    TextInserter,
    Transcriber,
    TranscriptEvent,
    TranscriptKind,
)
from .preview import PreviewMetrics, PreviewWorker, TranscriptBuffer
from .progress import ProgressBuffer
from .state import AppController, AppState

logger = logging.getLogger(__name__)


@dataclass
class Session:
    request_id: int = 0
    requested_at: float = field(default_factory=perf_counter)
    held: Event = field(default_factory=Event)
    cancelled: Event = field(default_factory=Event)
    changed: Event = field(default_factory=Event)
    target: object = None
    capture_error: str | None = None
    generation: int = 1
    released_at: float | None = None


class RecordingService:
    def __init__(
        self,
        controller: AppController,
        recorder: AudioRecorder,
        hotkey: HotkeyListener,
        settings: Settings,
        directory: Path,
        transcriber: SpeechPipeline | Transcriber | None = None,
        inserter: TextInserter | None = None,
        transcriber_factory=None,
    ):
        self.controller = controller
        self.recorder = recorder
        self.hotkey = hotkey
        self.settings = settings
        self.directory = directory
        self.transcriber = transcriber
        self.inserter = inserter
        self.transcriber_factory = transcriber_factory
        self.enabled = settings.enabled
        self.settings_open = False
        self._settings_pending = False
        self._settings_revision = 0
        self._pending_settings = None
        self._microphone_rebind_pending = False
        self.last_insert_result = None
        self.last_result = None
        self.last_error = ""
        self.completed_count = 0
        self.last_duration = 0.0
        self.last_start_latency = 0.0
        self.last_preview_metrics = PreviewMetrics()
        self._preview = None
        self._preview_status = ""
        self._transcripts = TranscriptBuffer()
        self._phase = "hotkey"
        self.finished = Event()
        self._closing = Event()
        self._commands = SimpleQueue()
        self._lock = Lock()
        self._down = False
        self._busy = False
        self._reservation = None
        self._pressed = None
        self._active = None
        self._session_ids = count(1)
        self._progress = ProgressBuffer()
        self._thread = Thread(target=self._run, name="voxtypex-recording", daemon=True)

    def start(self):
        self._thread.start()

    @property
    def progress(self):
        return getattr(self.transcriber, "progress", "transcribing")

    def drain_progress(self):
        return self._progress.drain()

    def drain_transcripts(self):
        return self._transcripts.drain()

    def acknowledge_preview(self, event):
        with self._lock:
            session = self._reservation
            if (
                session is None or self._preview is None or event.kind is not TranscriptKind.PARTIAL
                or (event.session_id, event.generation) != (session.request_id, session.generation)
                or session.cancelled.is_set() or session.changed.is_set()
            ):
                return
            metrics = self._preview.metrics
            if metrics.first_visible_ms is None:
                metrics.first_visible_ms = (perf_counter() - session.requested_at) * 1000

    @property
    def preview_status(self):
        if self._preview is not None and self._preview.error:
            return "Предпросмотр недоступен. Финальное распознавание — после отпускания PTT."
        return self._preview_status

    def _invalidate_preview(self, session):
        if self._preview is not None:
            self._preview.cancel()
        generation = self._transcripts.advance(session.request_id)
        if generation is not None:
            session.generation = generation

    def _stop_preview(self):
        if self._preview is not None:
            self._preview.stop()
            self.last_preview_metrics = self._preview.metrics
            self._preview = None

    def _start_preview(self, session):
        self._preview_status = ""
        if self.settings.preview_mode == "off":
            return
        capabilities = getattr(self.transcriber, "capabilities", EngineCapabilities())
        source = getattr(self.recorder, "preview_audio", None)
        if not self.settings.vad_enabled or not self.settings.visual_indication:
            self._preview_status = "Для предпросмотра включите VAD и индикацию в настройках."
            return
        if not capabilities.batch_preview or source is None:
            self._preview_status = "Предпросмотр для этого движка недоступен."
            return
        if session.cancelled.is_set() or session.changed.is_set():
            return
        self._preview_status = "Предпросмотр · batch-ASR · текст может измениться"
        self._preview = PreviewWorker(
            self.transcriber, source, session.request_id, session.generation, self._transcripts.emit,
            interval=self.settings.preview_interval_seconds,
            window_seconds=self.settings.preview_window_seconds,
            requested_at=session.requested_at, cancelled=session.cancelled, changed=session.changed,
        )
        self._preview.start()

    def _emit(self, session, stage):
        self._progress.emit(ProgressEvent(session.request_id, stage))

    def pressed(self):
        # Windows hook: no I/O or Qt calls.
        with self._lock:
            if self._closing.is_set() or self.finished.is_set() or self._down:
                return
            self._down = True
            if not self.enabled or self.settings_open or self._settings_pending or self._microphone_rebind_pending:
                return
            if self._busy or self.controller.state not in (AppState.IDLE, AppState.ERROR):
                self._commands.put(("ignored", None))
                return
            session = Session(request_id=next(self._session_ids))
            if self.inserter is not None:
                try:
                    session.target = self.inserter.capture_target()
                except Exception as exc:
                    session.capture_error = f"{type(exc).__name__}: {exc}"
                    session.target = None
            session.held.set()
            self._pressed = session
            self._busy = True
            self._reservation = session
            self._commands.put(("press", session))

    def released(self):
        with self._lock:
            self._down = False
            session, self._pressed = self._pressed, None
            if session is not None:
                session.held.clear()
                session.released_at = perf_counter()
                self._commands.put(("release", session))

    def update_settings(self, settings):
        with self._lock:
            self.enabled = settings.enabled
            self._settings_pending = True
            self._settings_revision += 1
            self._commands.put(("settings", (self._settings_revision, settings)))
        if not settings.enabled:
            self.cancel_current()

    def set_settings_open(self, opened):
        with self._lock:
            self.settings_open = opened
        if opened:
            self.cancel_current()

    def request_microphone_rebind(self):
        with self._lock:
            if not self._microphone_rebind_pending and not self._closing.is_set():
                self._microphone_rebind_pending = True
                self._commands.put(('microphone_rebind', None))

    def _apply_settings(self, value, revision):
        try:
            self._configure(value)
        finally:
            with self._lock:
                if revision == self._settings_revision:
                    self._settings_pending = False

    def _configure(self, value):
        if self.transcriber_factory is not None and (
            value.asr_backend,
            value.whisper_model,
            value.model_path,
            value.ru_quality,
            value.uk_quality,
            value.en_quality,
        ) != (
            self.settings.asr_backend,
            self.settings.whisper_model,
            self.settings.model_path,
            self.settings.ru_quality,
            self.settings.uk_quality,
            self.settings.en_quality,
        ):
            replacement = self.transcriber_factory(value)
            try:
                if self.transcriber is not None:
                    self.transcriber.close()
            except Exception:
                try:
                    replacement.close()
                except Exception:
                    logger.exception("Could not discard replacement model after cleanup failure")
                raise
            self.transcriber = replacement
        elif self.transcriber is not None:
            self.transcriber.language = value.language
        configure_recorder = getattr(self.recorder, 'configure', None)
        if configure_recorder is not None:
            configure_recorder(value)
        else:
            self.recorder.device = value.microphone
        self.hotkey.set_key(value.push_to_talk_key)
        self.settings = value
        logger.info(
            "Settings applied: PTT=%s language=%s model=%s enabled=%s",
            value.push_to_talk_key,
            value.language,
            value.whisper_model,
            value.enabled,
        )

    def context_changed(self):
        with self._lock:
            if self._reservation is not None:
                self._reservation.changed.set()
                self._invalidate_preview(self._reservation)

    def cancel_current(self):
        with self._lock:
            session = self._reservation
            if session is not None:
                session.cancelled.set()
                session.held.clear()
                self._invalidate_preview(session)
                self._commands.put(("release", session))

    def request_close(self):
        self._closing.set()
        self.cancel_current()
        if self.transcriber is not None:
            self.transcriber.cancel()
        with self._lock:
            if self._pressed is not None:
                self._pressed.held.clear()
        self._commands.put(("close", None))

    def close(self):
        self.request_close()
        if self._thread.ident is not None:
            self._thread.join(timeout=3)
            if self._thread.is_alive():
                logger.error("Audio shutdown still pending in device driver")

    def _error(self, exc):
        try:
            self._stop_preview()
        except TimeoutError:
            self._closing.set()
            logger.error("Preview worker did not stop; refusing concurrent inference")
        if self._reservation is not None:
            self._invalidate_preview(self._reservation)
        self.last_error = {
            "microphone": "Микрофон недоступен или аудиобуфер переполнен. Проверьте подключение; в настройках можно явно выбрать другое устройство.",
            "model": "Не удалось загрузить модель распознавания. Проверьте свободное место и подключение к интернету.",
            "transcription": "Не удалось распознать речь. Повторите попытку или выберите меньшую модель.",
            "timeout": "Превышено время ожидания распознавания. Проверьте интернет или выберите меньшую модель и повторите запись.",
            "insertion": "Не удалось вставить текст в активное приложение.",
            "settings": "Не удалось применить настройки. Проверьте выбранное устройство и модель.",
            "hotkey": "Горячая клавиша недоступна. Перезапустите VoxTypeX.",
        }[self._phase]
        logger.exception("Audio pipeline error: %s", exc)
        state = self.controller.state
        if state is not AppState.ERROR:
            self.controller.transition(state, AppState.ERROR)
        try:
            self.recorder.close()
        except Exception:
            logger.exception("Could not close microphone")
        self._active = None
        with self._lock:
            self._busy = False
            self._reservation = None

    def _press(self, session):
        logger.info("PTT pressed")
        if session.capture_error:
            logger.warning("Could not capture insertion target: %s", session.capture_error)
        if self.controller.state is AppState.ERROR:
            self.controller.transition(AppState.ERROR, AppState.IDLE)
        if not session.held.is_set() or session.cancelled.is_set() or self._closing.is_set():
            logger.info("PTT released before microphone startup; skipped")
            return
        self._active = session
        self._progress.begin(session.request_id)
        session.generation = self._transcripts.begin(session.request_id)
        self.last_preview_metrics = PreviewMetrics()
        self.last_insert_result = None
        self.last_result = None
        self._phase = "microphone"
        self.recorder.start(session.held)
        self.last_start_latency = perf_counter() - session.requested_at
        self.last_error = ""
        self.controller.transition(AppState.IDLE, AppState.RECORDING)
        self._emit(session, ProgressStage.RECORDING)
        logger.info("Recording started; startup_ms=%.1f", self.last_start_latency * 1000)
        if self.inserter is not None and not self._closing.is_set():
            self._phase = "insertion"
            self.inserter.prepare_target(session.target, session.cancelled, session.changed)
        self._phase = "microphone"
        self._start_preview(session)

    def _release(self, session):
        logger.info("PTT released")
        if session is not self._active:
            # Не сбрасывать резервирование новой сессии.
            with self._lock:
                if self._reservation is session:
                    self._busy = False
                    self._reservation = None
            return
        self._phase = "microphone"
        final_started = session.released_at or perf_counter()
        clip = self.recorder.stop()
        logger.info("Recording stopped; Duration: %.3f s; Samples: %d", clip.duration, clip.samples)
        self.last_duration = clip.duration
        self._active = None
        if self.transcriber is not None and not session.cancelled.is_set():
            self.controller.transition(AppState.RECORDING, AppState.TRANSCRIBING)
        self._invalidate_preview(session)
        self._stop_preview()
        final_generation = session.generation
        if session.cancelled.is_set():
            logger.info("Voice input cancelled")
            self._emit(session, ProgressStage.CANCELLED)
        elif clip.duration < MIN_RECORDING_SECONDS and clip.speech_detected is not True:
            logger.info("Short recording discarded (< %.0f ms)", MIN_RECORDING_SECONDS * 1000)
        else:
            self.completed_count += 1
            logger.info("Audio captured successfully")
            if self.settings.debug_wav:
                self._save_debug(clip)
            if self.transcriber is not None and not self._closing.is_set():
                self._phase = "transcription"
                self.controller.transition(AppState.RECORDING, AppState.TRANSCRIBING)
                context = OperationContext(
                    session.request_id,
                    session.cancelled,
                    monotonic() + self.settings.transcription_timeout_seconds,
                )
                run = getattr(self.transcriber, "run", None)
                result = (
                    run(clip, context, self._progress.emit)
                    if run is not None
                    else self.transcriber.transcribe(clip)
                )
                if not self._closing.is_set() and not session.cancelled.is_set():
                    if monotonic() >= context.deadline:
                        self._phase = "timeout"
                        self._emit(session, ProgressStage.TIMEOUT)
                        raise OperationTimeout("Speech pipeline returned after its deadline")
                    self.last_result = result
                    self.last_preview_metrics.final_latency_ms = (perf_counter() - final_started) * 1000
                    if result.error:
                        if result.error_code == "model":
                            self._phase = "model"
                        elif result.error_code == "timeout":
                            self._phase = "timeout"
                        # Provider errors may contain private input; log only the category.
                        raise RuntimeError(f"STT failed ({self._phase})")
                    if result.text and result.text.strip():
                        if not session.changed.is_set():
                            self._transcripts.emit(TranscriptEvent(
                                session.request_id, final_generation, 1, TranscriptKind.FINAL,
                                stable_text=result.text, mode="final",
                            ))
                        logger.info(
                            "Transcribed in %.2f sec (language=%s, characters=%d)",
                            result.processing_time,
                            result.language,
                            len(result.text),
                        )
                        if self.inserter is not None:
                            self._phase = "insertion"
                            self.controller.transition(AppState.TRANSCRIBING, AppState.INSERTING)
                            self._emit(session, ProgressStage.INSERTING)
                            inserted = self.inserter.insert_text(result.text)
                            self.last_insert_result = inserted
                            logger.info(
                                "Insertion: success=%s method=%s skipped=%s clipboard_restored=%s",
                                inserted.success,
                                inserted.method,
                                inserted.skipped,
                                inserted.clipboard_restored,
                            )
                            if not inserted.success:
                                if inserted.skipped:
                                    self.last_error = "Вставка отменена: активное поле изменилось или нажата другая клавиша."
                                else:
                                    self.last_error = "Не удалось вставить текст в активное приложение."
                                logger.warning("Text not fully inserted: %s", inserted.error)
                                self.controller.transition(AppState.INSERTING, AppState.ERROR)
                    else:
                        logger.info("No speech or valid text detected; nothing to do")
        metrics = self.last_preview_metrics
        logger.info(
            "Preview timing: calls=%d first_ms=%s visible_ms=%s final_ms=%s inference_s=%.3f cpu_pct=%.1f worker_cpu=%s",
            metrics.calls, metrics.first_hypothesis_ms, metrics.first_visible_ms, metrics.final_latency_ms,
            metrics.inference_seconds, metrics.cpu_percent, metrics.worker_cpu_measured,
        )
        if self.controller.state is not AppState.ERROR:
            self.controller.transition(self.controller.state, AppState.IDLE)
        with self._lock:
            self._busy = False
            self._reservation = None

    def _save_debug(self, clip):
        path = self.directory / "last-recording.wav"
        temporary = path.with_suffix(".wav.tmp")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with wave.open(str(temporary), "wb") as output:
                output.setnchannels(clip.channels)
                output.setsampwidth(clip.sample_width)
                output.setframerate(clip.sample_rate)
                output.writeframes(clip.pcm)
            os.replace(temporary, path)
            logger.info("Debug WAV saved: %s", path)
        except OSError:
            logger.exception("Could not save optional debug WAV")
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove temporary debug WAV")

    def _run(self):
        try:
            if self.inserter is not None:
                self.hotkey.set_guard_callbacks(self.context_changed, self.cancel_current)
            prepare = getattr(self.recorder, 'prepare', None)
            if prepare is not None:
                self._phase = 'microphone'
                prepare()
            self._phase = 'hotkey'
            self.hotkey.start(self.settings.push_to_talk_key, self.pressed, self.released)
            while not self._closing.is_set():
                try:
                    command, value = self._commands.get(timeout=0.05 if self._active else 0.5)
                except Empty:
                    command, value = "health", None
                if self._closing.is_set():
                    break
                try:
                    if command == "press":
                        self._press(value)
                    elif command == "release":
                        self._release(value)
                    elif command == "settings":
                        self._pending_settings = value
                    elif command == 'microphone_rebind':
                        self._microphone_rebind_pending = True
                    elif command == "ignored":
                        logger.info("PTT pressed: ignored while busy")
                    if self._active is not None:
                        self._phase = "microphone"
                        self.recorder.check_health()
                        if getattr(self.recorder, 'auto_stopped', False):
                            session = self._active
                            session.held.clear()
                            self._release(session)
                    if self._pending_settings is not None and not self._busy:
                        self._phase = "settings"
                        (revision, value), self._pending_settings = self._pending_settings, None
                        self._apply_settings(value, revision)
                    if self._microphone_rebind_pending and not self._busy:
                        self._phase = 'microphone'
                        reset = getattr(self.recorder, 'reset_default_device', None)
                        if reset is not None:
                            reset()
                        self._microphone_rebind_pending = False
                    if not self._busy and self.transcriber is not None:
                        idle = getattr(self.transcriber, "release_idle", None)
                        if idle is not None:
                            idle()
                except Exception as exc:
                    self._error(exc)
                self._phase = "hotkey"
                self.hotkey.check_health()
        except Exception as exc:
            self._error(exc)
        finally:
            self._closing.set()
            self.cancel_current()
            preview_stopped = True
            try:
                self._stop_preview()
            except TimeoutError:
                preview_stopped = False
                logger.error("Preview shutdown timed out; engine cleanup remains with its owner")
            try:
                if self._active is not None:
                    logger.info("Recording cancelled during shutdown")
                self.recorder.close()
            except Exception:
                logger.exception("Microphone shutdown failed")
            try:
                self.hotkey.close()
            except Exception:
                logger.exception("Hotkey shutdown failed")
            try:
                if self.transcriber is not None and preview_stopped:
                    self.transcriber.close()
            except Exception:
                logger.exception("STT shutdown failed")
            try:
                if self.inserter is not None:
                    self.inserter.close()
            except Exception:
                logger.exception("Insertion shutdown failed")
            self.finished.set()
            logger.info("Recording worker stopped")
