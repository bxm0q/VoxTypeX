SAMPLE_RATE = 16000
FRAME_SAMPLES = 160
FRAME_BYTES = FRAME_SAMPLES * 2
# FFT overlap: 256 - 160 = 96 samples (6 ms).
LATENCY_SAMPLES = 96


class DenoisingError(RuntimeError):
    """Noise suppression failed."""


class Denoiser:
    """Process mono PCM16 at 16 kHz; flush() returns the delayed tail."""

    def __init__(self, mode="moderate"):
        if mode not in ("off", "moderate", "strong"):
            raise ValueError("Denoising mode must be off, moderate, or strong")
        self.mode = mode
        self.latency_samples = 0 if mode == "off" else LATENCY_SAMPLES
        self._pending = bytearray()
        self._skip_bytes = self.latency_samples * 2
        self._input_bytes = 0
        self._output_bytes = 0
        self._finished = False
        self._backend = None
        self._numpy = None

    def process(self, pcm: bytes) -> bytes:
        if self._finished:
            raise DenoisingError("Denoising stream is closed; create one per recording")
        if len(pcm) % 2:
            raise ValueError("Denoising requires complete PCM16 samples")
        if not pcm:
            return b""
        self._input_bytes += len(pcm)
        if self.mode == "off":
            self._output_bytes += len(pcm)
            return bytes(pcm)
        self._pending.extend(pcm)
        size = len(self._pending) // FRAME_BYTES * FRAME_BYTES
        if not size:
            return b""
        frames = bytes(self._pending[:size])
        del self._pending[:size]
        try:
            return self._aligned(self._process_frames(frames))
        except DenoisingError:
            self.close()
            raise

    def flush(self) -> bytes:
        if self._finished:
            return b""
        self._finished = True
        try:
            if self.mode == "off" or not self._input_bytes:
                return b""
            # Drain the delayed 96 samples and any partial frame.
            size = len(self._pending) + self.latency_samples * 2
            size = (size + FRAME_BYTES - 1) // FRAME_BYTES * FRAME_BYTES
            frames = bytes(self._pending).ljust(size, b"\0")
            output = self._aligned(self._process_frames(frames))
            if self._output_bytes != self._input_bytes:
                raise DenoisingError("Noise suppressor returned an incomplete recording")
            return output
        finally:
            self.close()

    def close(self):
        self._finished = True
        self._pending.clear()
        self._backend = None
        self._numpy = None

    def _load_backend(self):
        try:
            import numpy as np
            from pywebrtc_audio import NoiseSuppressor

            self._numpy = np
            self._backend = NoiseSuppressor(
                sample_rate=SAMPLE_RATE, num_channels=1, level=1 if self.mode == "moderate" else 3
            )
        except (ImportError, OSError, RuntimeError) as error:
            raise DenoisingError("Noise suppression requires the pywebrtc-audio CPU runtime") from error

    def _process_frames(self, frames: bytes) -> bytes:
        if self._backend is None:
            self._load_backend()
        np = self._numpy
        samples = np.frombuffer(frames, dtype="<i2")
        try:
            output = self._backend.process(samples)
        except Exception as error:
            raise DenoisingError("WebRTC noise suppression failed") from error
        if output.dtype != np.dtype("int16") or output.shape != samples.shape:
            raise DenoisingError("Noise suppressor returned invalid PCM16")
        # Keep native saturation; narrowing floats to int16 would wrap.
        return output.astype("<i2", copy=False).tobytes()

    def _aligned(self, pcm: bytes) -> bytes:
        skipped = min(self._skip_bytes, len(pcm))
        self._skip_bytes -= skipped
        available = self._input_bytes - self._output_bytes
        output = pcm[skipped : skipped + available]
        self._output_bytes += len(output)
        return output
