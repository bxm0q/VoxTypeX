import logging
import multiprocessing
from threading import Event, Thread
from time import monotonic

from .interfaces import TranscriptionResult
from .whisper_worker import run_worker

logger = logging.getLogger(__name__)


class WorkerFailed(RuntimeError):
    pass


class WorkerTimeout(TimeoutError):
    pass


class FasterWhisperEngine:
    def __init__(self, cache_dir, model="small", model_path=None):
        self.cache_dir = str(cache_dir)
        self.model = model
        self.model_path = model_path
        self._context = multiprocessing.get_context("spawn")
        self._process = None
        self._connection = None
        self._cancelled = Event()
        self._force_cpu = False
        self._device = None
        self.phase = "starting"
        self._deadline = float("inf")
        self._inference_timeout = 60.0

    def _set_phase(self, phase):
        if phase not in ("starting", "downloading", "loading", "transcribing"):
            return
        self.phase = phase
        limit = {
            "starting": 120,
            "downloading": 600,
            "loading": 120,
            "transcribing": self._inference_timeout,
        }[phase]
        self._deadline = monotonic() + limit

    def _check_deadline(self):
        if monotonic() >= self._deadline:
            raise WorkerTimeout(f"STT timeout during {self.phase}")

    def _start(self):
        if self._process is not None and self._process.is_alive():
            return
        self._dispose()
        parent, child = self._context.Pipe()
        self._connection = parent
        self._device = None
        self._process = self._context.Process(
            target=run_worker,
            args=(child, self.cache_dir, self.model, self.model_path, self._force_cpu),
            name="voxtypex-whisper",
            daemon=True,
        )
        try:
            self._process.start()
        finally:
            child.close()

    def _receive(self):
        while not self._cancelled.is_set():
            self._check_deadline()
            try:
                if self._connection.poll(0.1):
                    kind, value = self._connection.recv()
                    if kind == "result":
                        return value
                    if kind == "phase":
                        self._set_phase(value)
                    if kind == "device":
                        self._device = value
                        if value == "cpu":
                            self._force_cpu = True
                    elif kind == "status":
                        logger.info("STT: %s", value)
                    elif kind == "warning":
                        logger.warning("STT: %s", value)
                    elif kind == "error":
                        logger.error("STT worker failed:\n%s", value)
                elif not self._process.is_alive():
                    raise WorkerFailed(f"STT worker exited ({self._process.exitcode})")
            except (EOFError, BrokenPipeError, OSError) as exc:
                raise WorkerFailed(
                    "STT worker disconnected (native runtime error or insufficient memory)"
                ) from exc
        self._dispose()
        return TranscriptionResult(error="Распознавание отменено")

    def transcribe(self, audio, language=None):
        if self._cancelled.is_set():
            return TranscriptionResult(error="Распознавание отменено")
        for attempt in range(2):
            try:
                self._inference_timeout = max(60.0, audio.duration * 5)
                self._set_phase("starting")
                self._start()
                if not self._send_audio(audio, language):
                    return TranscriptionResult(error="Распознавание отменено")
                return self._receive()
            except WorkerTimeout as exc:
                logger.error("%s; terminating worker", exc)
                self._dispose()
                return TranscriptionResult(error=str(exc), error_code="timeout")
            except (WorkerFailed, BrokenPipeError, EOFError, OSError) as exc:
                cuda_failed = self._device == "cuda"
                self._dispose()
                if self._cancelled.is_set():
                    return TranscriptionResult(error="Распознавание отменено")
                if cuda_failed and attempt == 0 and not self._cancelled.is_set():
                    logger.warning("CUDA worker failed; retrying on CPU: %s", exc)
                    self._force_cpu = True
                    continue
                raise

    def _send_audio(self, audio, language):
        # A large clip can fill the pipe while the child is starting or stuck.
        # Keep the owner cancellable even before _receive() is reached.
        done = Event()
        failures = []
        connection = self._connection

        def send():
            try:
                connection.send((audio, language))
            except Exception as exc:
                # Do not retain the sender traceback: it owns Pickler's exported
                # BytesIO view and otherwise forms a cycle with this result list.
                failures.append(WorkerFailed(f"Audio transfer failed ({type(exc).__name__}): {exc}"))
            finally:
                done.set()

        sender = Thread(target=send, name="voxtypex-audio-send", daemon=True)
        sender.start()
        while not done.wait(0.1):
            timed_out = monotonic() >= self._deadline
            if self._cancelled.is_set() or timed_out:
                # Killing the reader unblocks a pending Windows pipe write.
                self._dispose()
                sender.join(timeout=2)
                if sender.is_alive():
                    raise WorkerFailed("Audio pipe writer did not stop after worker termination")
                if timed_out:
                    raise WorkerTimeout("STT timeout sending audio")
                return False
        sender.join()
        if failures:
            raise failures[0]
        return True

    def cancel(self):
        # Called from UI shutdown: the owning worker performs termination/join.
        self._cancelled.set()

    def _dispose(self):
        if self._process is not None:
            if self._process.pid is not None:
                if self._process.is_alive():
                    self._process.terminate()
                self._process.join(timeout=2)
                if self._process.is_alive():
                    self._process.kill()
                    self._process.join(timeout=1)
                self._process.close()
            self._process = None
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def close(self):
        self.cancel()
        self._dispose()
