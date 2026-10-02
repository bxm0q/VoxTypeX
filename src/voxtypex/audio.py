import logging
from time import monotonic

from .interfaces import AudioClip
from .microphones import resolve_input

logger = logging.getLogger(__name__)
SAMPLE_RATE = 16_000
MAX_SECONDS = 120


class MicrophoneRecorder:
    def __init__(self, backend=None, device=None):
        if backend is None:
            import sounddevice as backend
        self.backend = backend
        self.device = device
        self.stream = None
        self.chunks = []
        self.held = None
        self.fault = None
        self.last_data = 0.0
        self.samples = 0

    def start(self, held):
        if self.stream is not None:
            raise RuntimeError("Microphone is already open")
        self.chunks = []
        self.samples = 0
        self.fault = None
        self.held = held
        self.last_data = monotonic()
        try:
            index = resolve_input(self.device, self.backend)
            device = (
                self.backend.query_devices(kind="input")
                if index is None
                else self.backend.query_devices(index, "input")
            )
            logger.info("Opening microphone: %s; mono PCM16 %s Hz", device["name"], SAMPLE_RATE)
            self.stream = self.backend.RawInputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=320,
                callback=self._callback,
                device=index,
            )
            self.stream.start()
            self.last_data = monotonic()
        except Exception:
            self.close()
            raise

    def _callback(self, data, frames, timing, status):
        self.last_data = monotonic()
        if not self.held.is_set():
            return
        if status:
            self.fault = f"Microphone stream: {status}"
            raise self.backend.CallbackAbort
        if self.samples + frames > SAMPLE_RATE * MAX_SECONDS:
            self.fault = f"Recording limit reached ({MAX_SECONDS} seconds); release PTT"
            raise self.backend.CallbackAbort
        self.chunks.append(bytes(data))
        self.samples += frames

    def check_health(self):
        if self.fault:
            raise RuntimeError(self.fault)
        if self.stream is not None:
            if not self.stream.active:
                raise RuntimeError("Microphone stream stopped unexpectedly")
            if monotonic() - self.last_data > 2:
                raise RuntimeError("No microphone audio callbacks for 2 seconds; device may be disconnected")

    def stop(self):
        # abort() сбрасывает ожидающие буферы; никогда не продолжать запись после освобождения
        try:
            self.check_health()
            if self.stream is not None:
                self.stream.abort()
            if self.fault:
                raise RuntimeError(self.fault)
            return AudioClip(b"".join(self.chunks), SAMPLE_RATE)
        finally:
            self.close()

    def close(self):
        stream, self.stream = self.stream, None
        try:
            if stream is not None:
                # Закрытие PortAudio также прерывает активный поток
                stream.close()
        finally:
            self.chunks = []
