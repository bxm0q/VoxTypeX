import logging
import os
import wave
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Lock, Thread
from time import perf_counter

from .config import Settings
from .interfaces import MIN_RECORDING_SECONDS, AudioRecorder, HotkeyListener, TextInserter, Transcriber
from .state import AppController, AppState

logger = logging.getLogger(__name__)


@dataclass
class Session:
    requested_at: float = field(default_factory=perf_counter)
    held: Event = field(default_factory=Event)
    cancelled: Event = field(default_factory=Event)
    changed: Event = field(default_factory=Event)
    target: object = None
    capture_error: str | None = None


class RecordingService:
    def __init__(
        self,
        controller: AppController,
        recorder: AudioRecorder,
        hotkey: HotkeyListener,
        settings: Settings,
        directory: Path,
        transcriber: Transcriber | None = None,
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
        self.last_insert_result = None
        self.last_result = None
        self.last_error = ""
        self.completed_count = 0
        self.last_duration = 0.0
        self.last_start_latency = 0.0
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
        self._thread = Thread(target=self._run, name="voxtypex-recording", daemon=True)

    def start(self):
        self._thread.start()

    @property
    def progress(self):
        return getattr(self.transcriber, "progress", "transcribing")

    def pressed(self):
        # Вызывается с помощью хука Windows: без логирования, ввода-вывода или работы с Qt
        with self._lock:
            if self._closing.is_set() or self.finished.is_set() or self._down:
                return
            self._down = True
            if not self.enabled or self.settings_open or self._settings_pending:
                return
            if self._busy or self.controller.state not in (AppState.IDLE, AppState.ERROR):
                self._commands.put(("ignored", None))
                return
            session = Session()
            if self.inserter is not None:
                try:
                    session.target = self.inserter.capture_target()
                except Exception as exc:
                    # Передать диагностику исполнителю; никогда не выполнять вход в хук Windows
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

    def _apply_settings(self, value, revision):
        try:
            self._configure(value)
        finally:
            with self._lock:
                if revision == self._settings_revision:
                    self._settings_pending = False

    def _configure(self, value):
        if self.transcriber_factory is not None and (value.whisper_model, value.model_path) != (
            self.settings.whisper_model,
            self.settings.model_path,
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

    def cancel_current(self):
        with self._lock:
            session = self._reservation
            if session is not None:
                session.cancelled.set()
                session.held.clear()
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
        self.last_error = {
            "microphone": "Микрофон недоступен. Проверьте подключение и выбранное устройство.",
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
        self.last_insert_result = None
        self.last_result = None
        self._phase = "microphone"
        self.recorder.start(session.held)
        self.last_start_latency = perf_counter() - session.requested_at
        self.last_error = ""
        self.controller.transition(AppState.IDLE, AppState.RECORDING)
        logger.info("Recording started; startup_ms=%.1f", self.last_start_latency * 1000)
        if self.inserter is not None and not self._closing.is_set():
            self._phase = "insertion"
            self.inserter.prepare_target(session.target, session.cancelled, session.changed)
        self._phase = "microphone"

    def _release(self, session):
        logger.info("PTT released")
        if session is not self._active:
            # Не отменять резервирование для новой сессии после неудачного запуска
            with self._lock:
                if self._reservation is session:
                    self._busy = False
                    self._reservation = None
            return
        self._phase = "microphone"
        clip = self.recorder.stop()
        logger.info("Recording stopped; Duration: %.3f s; Samples: %d", clip.duration, clip.samples)
        self.last_duration = clip.duration
        self._active = None
        if session.cancelled.is_set():
            logger.info("Voice input cancelled")
        elif clip.duration < MIN_RECORDING_SECONDS:
            logger.info("Short recording discarded (< %.0f ms)", MIN_RECORDING_SECONDS * 1000)
        else:
            self.completed_count += 1
            logger.info("Audio captured successfully")
            if self.settings.debug_wav:
                self._save_debug(clip)
            if self.transcriber is not None and not self._closing.is_set():
                self._phase = "transcription"
                self.controller.transition(AppState.RECORDING, AppState.TRANSCRIBING)
                result = self.transcriber.transcribe(clip)
                if not self._closing.is_set() and not session.cancelled.is_set():
                    self.last_result = result
                    if result.error:
                        if result.error_code == "model":
                            self._phase = "model"
                        elif result.error_code == "timeout":
                            self._phase = "timeout"
                        raise RuntimeError(f"STT: {result.error}")
                    if result.text and result.text.strip():
                        logger.info(
                            "Transcribed in %.2f sec (language=%s, characters=%d)",
                            result.processing_time,
                            result.language,
                            len(result.text),
                        )
                        if self.inserter is not None:
                            self._phase = "insertion"
                            self.controller.transition(AppState.TRANSCRIBING, AppState.INSERTING)
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
                    elif command == "ignored":
                        logger.info("PTT pressed: ignored while busy")
                    if self._active is not None:
                        self._phase = "microphone"
                        self.recorder.check_health()
                    if self._pending_settings is not None and not self._busy:
                        self._phase = "settings"
                        (revision, value), self._pending_settings = self._pending_settings, None
                        self._apply_settings(value, revision)
                except Exception as exc:
                    self._error(exc)
                # Сбой слушания необратим; оставить окно открытым и вывести сообщение об ошибке
                self._phase = "hotkey"
                self.hotkey.check_health()
        except Exception as exc:
            self._error(exc)
        finally:
            self._closing.set()
            self.cancel_current()
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
                if self.transcriber is not None:
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
