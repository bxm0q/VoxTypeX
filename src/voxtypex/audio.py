import json
import logging
from collections import deque
from dataclasses import replace
from threading import Event, Lock, Thread
from time import monotonic

from .capture_processing import AudioDiagnostics, CaptureProcessor
from .config import Settings
from .interfaces import AudioClip
from .microphones import DefaultInputBinding, capture_device, open_input_lease

logger = logging.getLogger(__name__)
SAMPLE_RATE = 16_000
MAX_SECONDS = 120


def make_processor(settings):
    return CaptureProcessor(
        vad_enabled=settings.vad_enabled,
        vad_mode=settings.vad_mode,
        pre_roll_ms=settings.vad_preroll_ms,
        hangover_ms=settings.vad_hangover_ms,
        auto_stop_seconds=settings.auto_stop_silence_seconds,
        noise_mode=settings.noise_suppression,
    )


class MicrophoneRecorder:
    def __init__(
        self, backend=None, device=None, settings=None, *, processor_factory=None, queue_capacity=64,
        default_binding=None,
    ):
        if backend is None:
            import sounddevice as backend
        if not 1 <= queue_capacity <= 256:
            raise ValueError("Invalid audio queue capacity")
        self.backend = backend
        self.settings = settings or Settings()
        self.device = device if device is not None else self.settings.microphone
        self.processor_factory = processor_factory or make_processor
        self.queue_capacity = queue_capacity
        self.stream = None
        self.held = None
        self.fault = None
        self.last_data = 0.0
        self.samples = 0
        self._queue = deque(maxlen=queue_capacity)
        self._worker = None
        self._lease = None
        self.default_binding = default_binding if default_binding is not None else DefaultInputBinding()
        self._accepting = False
        self._stop = Event()
        self._ready = Event()
        self._discard = False
        self._result = AudioClip(b"", SAMPLE_RATE)
        self._diagnostics = AudioDiagnostics()
        self._queue_overflows = self._input_overflows = self._input_underflows = 0
        self.auto_stopped = False
        self._processor = None
        self._processor_lock = Lock()

    def preview_audio(self, start_sample, maximum_samples):
        with self._processor_lock:
            if self._processor is None or self._discard or self.fault:
                return None
            return self._processor.preview_audio(start_sample, maximum_samples)

    @property
    def diagnostics(self):
        return replace(
            self._diagnostics,
            queue_overflows=self._queue_overflows,
            input_overflows=self._input_overflows,
            input_underflows=self._input_underflows,
        )

    def configure(self, settings):
        if self.stream is not None or (self._worker is not None and self._worker.is_alive()):
            raise RuntimeError("Cannot change microphone settings during capture")
        if settings.microphone != self.device:
            self.default_binding.reset()
        self.settings, self.device = settings, settings.microphone

    def prepare(self):
        """Warm DSP imports before PTT without opening the microphone."""
        processor = self.processor_factory(self.settings)
        try:
            processor.push(bytes(640))
        finally:
            processor.close()

    def reset_default_device(self):
        if self.stream is not None or (self._worker is not None and self._worker.is_alive()):
            raise RuntimeError('Cannot rebind an active microphone')
        self.default_binding.reset()

    def start(self, held):
        if self.stream is not None or (self._worker is not None and self._worker.is_alive()):
            raise RuntimeError("Microphone or previous processing is already open")
        self.samples, self.fault, self.held = 0, None, held
        self._queue.clear()
        self._queue_overflows = self._input_overflows = self._input_underflows = 0
        self.auto_stopped = self._discard = False
        self._result = AudioClip(b"", SAMPLE_RATE)
        self._diagnostics = AudioDiagnostics()
        self._stop.clear()
        self._ready.clear()
        self._worker = Thread(target=self._consume, name="voxtypex-capture-dsp", daemon=True)
        self._worker.start()
        try:
            if not self._ready.wait(5):
                raise RuntimeError("Audio processing startup timed out")
            if self.fault:
                raise RuntimeError(self.fault)
            self._lease = open_input_lease(self.backend)
            self._lease.__enter__()
            index, physical, label = (
                self.default_binding.capture(self.backend)
                if self.device is None else capture_device(self.device, self.backend)
            )
            logger.info("Opening microphone: %s; mono PCM16 %s Hz", label, SAMPLE_RATE)
            extra = {}
            wasapi_settings = getattr(self.backend, 'WasapiSettings', None)
            if 'wasapi' in json.loads(physical)[0].lower() and wasapi_settings is not None:
                # Shared-mode conversion retains this endpoint when its mixer runs at 48 kHz.
                extra['extra_settings'] = wasapi_settings(auto_convert=True)
            self.stream = self.backend.RawInputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=320,
                callback=self._callback,
                device=index,
                **extra,
            )
            self._accepting = True
            self.last_data = monotonic()
            self.stream.start()
        except Exception:
            self.close()
            raise

    def _callback(self, data, frames, timing, status):
        # PortAudio callback: copy PCM only; no DSP, I/O or waits.
        self.last_data = monotonic()
        if not self._accepting or not self.held.is_set():
            return
        if status:
            self._input_overflows += int(getattr(status, "input_overflow", True))
            self._input_underflows += int(getattr(status, "input_underflow", False))
            self.fault = "Microphone input overflow/underflow; capture discarded"
            return
        if self.samples + frames > SAMPLE_RATE * MAX_SECONDS:
            self.fault = f"Recording limit reached ({MAX_SECONDS} seconds); release PTT"
            return
        if len(self._queue) >= self.queue_capacity:
            self._queue_overflows += 1
            self.fault = "PCM buffer overflow; capture discarded"
            return
        self._queue.append(bytes(data))
        self.samples += frames

    def _consume(self):
        processor = None
        try:
            processor = self.processor_factory(self.settings)
            with self._processor_lock:
                self._processor = processor
            self._ready.set()
            while not self._stop.is_set() or self._queue:
                if self._discard:
                    self._queue.clear()
                    return
                try:
                    pcm = self._queue.popleft()
                except IndexError:
                    self._stop.wait(0.003)
                    continue
                with self._processor_lock:
                    processor.push(pcm)
                self._diagnostics = processor.diagnostics
                self.auto_stopped = processor.auto_stopped
            if not self._discard:
                with self._processor_lock:
                    self._result = processor.finish()
                self._diagnostics = processor.diagnostics
        except Exception as exc:
            self.fault = f"Audio processing failed ({type(exc).__name__})"
        finally:
            self._ready.set()
            with self._processor_lock:
                self._processor = None
                if processor is not None:
                    try:
                        processor.close()
                    except Exception as exc:
                        self.fault = f"Audio processing cleanup failed ({type(exc).__name__})"

    def check_health(self):
        if self.fault:
            raise RuntimeError(self.fault)
        if self.stream is not None:
            if not self.stream.active:
                raise RuntimeError("Microphone stream stopped; choose a device in settings")
            if monotonic() - self.last_data > 2:
                raise RuntimeError("No microphone callbacks for2seconds; device may be disconnected")

    def stop(self):
        try:
            self.check_health()
        except Exception:
            self.close()
            raise
        self._shutdown(discard=False)
        self.check_health()
        result, self._result = self._result, AudioClip(b"", SAMPLE_RATE)
        return result

    def close(self):
        self._shutdown(discard=True)
        self._result = AudioClip(b"", SAMPLE_RATE)

    def _shutdown(self, *, discard):
        self._accepting = False
        self._discard = discard
        stream = self.stream
        try:
            if stream is not None:
                try:
                    stream.abort()
                finally:
                    stream.close()
                    self.stream = None
        finally:
            # Drain the consumer only after abort/close has finished the last callback.
            self._stop.set()
            if self.stream is None and self._lease is not None:
                lease, self._lease = self._lease, None
                lease.__exit__(None, None, None)
            if self._worker is not None:
                self._worker.join(timeout=5)
                if self._worker.is_alive():
                    # Keep the worker handle until shutdown completes.
                    raise RuntimeError("Audio processing shutdown timed out")
                self._worker = None
            self._queue.clear()
