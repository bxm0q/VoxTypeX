import logging
import multiprocessing
from itertools import count
from threading import Event, Thread
from time import monotonic

from .interfaces import (
    EngineCapabilities,
    OperationCancelled,
    OperationContext,
    OperationTimeout,
    ProgressEvent,
    ProgressSink,
    ProgressStage,
    TranscriptionResult,
)
from .whisper_worker import run_worker

logger = logging.getLogger(__name__)


class WorkerFailed(RuntimeError):
    pass


class WorkerTimeout(TimeoutError):
    pass


class FasterWhisperEngine:
    capabilities = EngineCapabilities(batch_preview=True)

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
        self._operation = None
        self._progress_sink = None
        self._request_ids = count(1)
        self._ipc_request_id = 0
        self._last_used = monotonic()

    def _emit(self, stage):
        if self._progress_sink is not None and self._operation is not None:
            self._progress_sink(ProgressEvent(self._operation.request_id, stage))

    def _checkpoint(self):
        if self._cancelled.is_set():
            raise OperationCancelled("ASR shutdown requested")
        if self._operation is not None:
            self._operation.checkpoint()

    def _set_phase(self, phase):
        if phase not in ("starting", "downloading", "loading", "transcribing"):
            return
        self.phase = phase
        self._emit(ProgressStage(phase))
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
        while True:
            self._checkpoint()
            self._check_deadline()
            try:
                if self._connection.poll(0.1):
                    message = self._connection.recv()
                    if not isinstance(message, tuple) or len(message) != 3:
                        raise WorkerFailed("Invalid STT worker message")
                    request_id, kind, value = message
                    if request_id != self._ipc_request_id:
                        continue  # Ignore stale worker messages.
                    if kind == "result":
                        if not isinstance(value, TranscriptionResult):
                            raise WorkerFailed("Invalid STT worker result")
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
                    elif kind == "fallback":
                        self._emit(ProgressStage.FALLBACK)
                    elif kind == "error":
                        logger.error("STT worker failed:\n%s", value)
                elif not self._process.is_alive():
                    raise WorkerFailed(f"STT worker exited ({self._process.exitcode})")
            except (EOFError, BrokenPipeError, OSError) as exc:
                raise WorkerFailed(
                    "STT worker disconnected (native runtime error or insufficient memory)"
                ) from exc

    def transcribe(
        self,
        audio,
        language=None,
        *,
        context: OperationContext | None = None,
        progress: ProgressSink | None = None,
    ):
        self._operation, self._progress_sink = context, progress
        self._ipc_request_id = next(self._request_ids)
        try:
            self._checkpoint()
            result = self._transcribe(audio, language)
            self._checkpoint()
            return result
        except (OperationCancelled, OperationTimeout) as exc:
            self._dispose()
            cancelled = isinstance(exc, OperationCancelled)
            return TranscriptionResult(
                error="Распознавание отменено" if cancelled else "STT request deadline exceeded",
                error_code="cancelled" if cancelled else "timeout",
            )
        finally:
            self._operation, self._progress_sink = None, None
            self._last_used = monotonic()

    def _transcribe(self, audio, language):
        if self._cancelled.is_set():
            return TranscriptionResult(error="Распознавание отменено")
        for attempt in range(2):
            try:
                self._checkpoint()
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
                try:
                    self._checkpoint()
                except (OperationCancelled, OperationTimeout):
                    logger.warning("ASR worker failure during stop (%s)", type(exc).__name__)
                    raise
                if cuda_failed and attempt == 0 and not self._cancelled.is_set():
                    logger.warning("CUDA worker failed; retrying on CPU: %s", exc)
                    self._force_cpu = True
                    self._emit(ProgressStage.FALLBACK)
                    continue
                raise

    def _send_audio(self, audio, language):
        # Use a sender thread so a full pipe cannot block cancellation.
        done = Event()
        failures = []
        connection = self._connection
        request_id = self._ipc_request_id

        def send():
            try:
                connection.send((request_id, audio, language))
            except Exception as exc:
                # Sender tracebacks retain Pickler's BytesIO view; store only the error type.
                failures.append(WorkerFailed(f"Audio transfer failed ({type(exc).__name__}): {exc}"))
            finally:
                done.set()

        sender = Thread(target=send, name="voxtypex-audio-send", daemon=True)
        sender.start()
        while not done.wait(0.1):
            timed_out = monotonic() >= self._deadline
            try:
                self._checkpoint()
                if timed_out:
                    raise WorkerTimeout("STT timeout sending audio")
            except (OperationCancelled, OperationTimeout, WorkerTimeout):
                # Stop the reader first to unblock a pending Windows pipe write.
                self._dispose()
                sender.join(timeout=2)
                if sender.is_alive():
                    raise WorkerFailed("Audio pipe writer did not stop after worker termination")
                raise
        sender.join()
        if failures:
            raise failures[0]
        return True

    def cancel(self):
        self._cancelled.set()

    def release_idle(self, idle_seconds=180.0):
        if self._process is not None and monotonic() - self._last_used >= idle_seconds:
            self._dispose()

    def _dispose(self):
        if self._process is not None:
            if self._process.pid is not None:
                if self._process.is_alive():
                    self._process.terminate()
                self._process.join(timeout=2)
                if self._process.is_alive():
                    self._process.kill()
                    # TerminateProcess may return before native I/O cleanup finishes.
                    self._process.join(timeout=5)
                if self._process.is_alive():
                    # Keep the handle so cleanup can be retried.
                    logger.error("STT worker did not exit after forced termination")
                    raise WorkerFailed("STT worker did not exit after forced termination")
                self._process.close()
            self._process = None
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def close(self):
        self.cancel()
        self._dispose()
