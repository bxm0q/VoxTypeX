import math
import sys
from array import array
from dataclasses import dataclass, replace
from time import perf_counter

from .interfaces import AudioClip, AudioPreview

RATE = 16000
FRAME_SAMPLES = 320
FRAME_BYTES = FRAME_SAMPLES * 2
MAX_SAMPLES = RATE * 120


@dataclass(frozen=True)
class AudioDiagnostics:
    rms_dbfs: float = -100.0
    peak_dbfs: float = -100.0
    clipped_samples: int = 0
    processing_ms: float = 0.0
    latency_ms: float = 0.0
    speech_detected: bool = False
    queue_overflows: int = 0
    input_overflows: int = 0
    input_underflows: int = 0


def samples16(pcm):
    values = array("h", pcm)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def dbfs(value):
    return max(-100.0, 20 * math.log10(value / 32768)) if value > 0 else -100.0


def detector_levels(values):
    """Estimate detector gain without letting isolated clicks set the level."""
    import numpy as np

    magnitudes = np.abs(values)
    peaks = np.maximum.reduceat(magnitudes, np.arange(0, len(values), FRAME_SAMPLES))
    if len(peaks) >= 3:
        peak = float(np.max(np.minimum(np.minimum(peaks[:-2], peaks[1:-1]), peaks[2:])))
    else:
        peak = float(np.max(peaks))
    # Limit outliers for detector gain only; ASR PCM stays unchanged.
    rms = float(np.sqrt(np.mean(np.minimum(magnitudes, peak) ** 2)))
    return peak, rms, min(1500 / max(rms, 1), 32767 / max(peak, 1))


class SpectralSpeechPresence:
    """Confirm speech separately from VAD boundaries."""

    def __init__(self):
        import numpy as np
        from pywebrtc_audio import VoiceDetector

        self.np = np
        self.detector = VoiceDetector(sample_rate=RATE, num_channels=1)

    def is_speech(self, pcm, rate):
        if rate != RATE:
            raise ValueError("Speech presence requires 16 kHz PCM16")
        return self.detector.process(self.np.frombuffer(pcm, dtype="<i2")) >= 0.5

    def confirms(self, values, gain):
        np = self.np
        run = 0
        for start in range(0, len(values), FRAME_SAMPLES):
            real = values[start : start + FRAME_SAMPLES]
            frame = np.zeros(FRAME_SAMPLES, dtype="<i2")
            frame[: len(real)] = np.rint(np.clip(real * gain, -32768, 32767)).astype("<i2")
            present = self.is_speech(frame.tobytes(), RATE) and float(np.var(real)) > 1
            run = run + 1 if present else 0
            if run >= 3:
                return True
        return False


class CaptureProcessor:
    """Trim outer silence, retain internal pauses, and run VAD on raw PCM."""

    def __init__(
        self,
        *,
        vad_enabled=True,
        vad_mode=1,
        pre_roll_ms=300,
        hangover_ms=500,
        auto_stop_seconds=0,
        noise_mode="off",
        speech_detector=None,
        presence_detector=None,
        denoiser_factory=None,
    ):
        if vad_mode not in range(4) or not 200 <= pre_roll_ms <= 400 or not 300 <= hangover_ms <= 1000:
            raise ValueError("Invalid VAD mode or speech margins")
        if auto_stop_seconds and not 1.5 <= auto_stop_seconds <= 10:
            raise ValueError("Auto-stop must be disabled or between1.5 and10seconds")
        if denoiser_factory is None:
            from .denoising import Denoiser

            denoiser_factory = Denoiser
        self._denoiser = denoiser_factory(noise_mode)
        self._native_vad = vad_enabled and speech_detector is None
        self._vad_mode = vad_mode
        self._vad = speech_detector
        if vad_enabled and self._vad is None:
            import webrtcvad

            self._vad = webrtcvad.Vad(vad_mode)
        self._presence = presence_detector
        if vad_enabled and self._presence is None:
            self._presence = SpectralSpeechPresence()
        self.vad_enabled = vad_enabled
        self.pre_roll_samples = pre_roll_ms * RATE // 1000
        self.hangover_samples = hangover_ms * RATE // 1000
        self.auto_stop_samples = int(auto_stop_seconds * RATE)
        self._pending = bytearray()
        self._output = bytearray()
        self._raw = bytearray()
        self._received = 0
        self._analysed = 0
        self._candidate = 0
        self._run = 0
        self._first_speech = None
        self._last_speech = 0
        self._preview_spans = []
        self._presence_run = 0
        self._presence_confirmed = False
        self._loud_run = 0
        self._loud_seen = False
        self._last_quiet_activity = 0
        self._stop_checked_candidate = -1
        self._finished = False
        self.auto_stopped = False
        self.diagnostics = AudioDiagnostics(latency_ms=0 if noise_mode == "off" else 6)

    def push(self, pcm):
        if self._finished:
            raise RuntimeError("Capture processor is closed")
        if len(pcm) % 2 or self._received + len(pcm) // 2 > MAX_SAMPLES:
            raise ValueError("Invalid or oversized PCM16 capture")
        started = perf_counter()
        self._received += len(pcm) // 2
        if self.vad_enabled:
            self._raw.extend(pcm)
        self._pending.extend(pcm)
        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            self._analyse(frame, FRAME_SAMPLES)
        self._output.extend(self._denoiser.process(pcm))
        self.diagnostics = replace(self.diagnostics, processing_ms=(perf_counter() - started) * 1000)

    def _analyse(self, frame, real_samples):
        values = samples16(frame)[:real_samples]
        energy = sum(v * v for v in values) / real_samples
        mean = sum(values) / real_samples
        peak = max(abs(v) for v in values)
        self._loud_run = self._loud_run + 1 if peak > 256 else 0
        self._loud_seen |= self._loud_run >= 3
        # Reject DC and zero tails left in native VAD hangover.
        active = energy - mean * mean > 1
        voiced = False
        if self.vad_enabled:
            # Feed silence too, or native hangover survives long pauses.
            voiced = self._vad.is_speech(frame, RATE) and active
            present = self._presence.is_speech(frame, RATE) and active
            self._presence_run = self._presence_run + 1 if present else 0
            self._presence_confirmed |= self._presence_run >= 3
        if active and not self._loud_seen:
            # Delay auto-stop while confirmed quiet speech is still active.
            self._last_quiet_activity = self._analysed + real_samples
        if voiced:
            if not self._run:
                self._candidate = self._analysed
            self._run += 1
            if self._run >= 3 or self._first_speech is not None:
                if self._first_speech is None:
                    self._first_speech = self._candidate
                self._last_speech = self._analysed + real_samples
                if not self._preview_spans or self._candidate - self._preview_spans[-1][1] > self.hangover_samples:
                    self._preview_spans.append((self._candidate, self._last_speech))
                else:
                    self._preview_spans[-1] = (self._preview_spans[-1][0], self._last_speech)
        else:
            self._run = 0
        self._analysed += real_samples
        if (
            self._first_speech is not None
            and self.auto_stop_samples
            and self._analysed - max(self._last_speech, self._last_quiet_activity) >= self.auto_stop_samples
        ):
            if (
                not self._presence_confirmed
                and self._native_vad
                and self._stop_checked_candidate != self._last_speech
            ):
                # Retry once per speech ending, after the silence timeout.
                self._stop_checked_candidate = self._last_speech
                self._presence_confirmed = self._confirm_native_stop_candidate()
            if self._presence_confirmed:
                self.auto_stopped = True
        previous = self.diagnostics
        self.diagnostics = AudioDiagnostics(
            rms_dbfs=dbfs(math.sqrt(energy)),
            peak_dbfs=dbfs(peak),
            clipped_samples=previous.clipped_samples + sum(v in (-32768, 32767) for v in values),
            latency_ms=previous.latency_ms,
            processing_ms=previous.processing_ms,
            speech_detected=self._presence_confirmed,
        )

    def _confirm_native_stop_candidate(self):
        import numpy as np

        span = self._last_speech - self._candidate
        if not 3 * FRAME_SAMPLES <= span <= RATE * 2:
            return False
        start = max(0, self._candidate - self.pre_roll_samples)
        # Include the full silence interval so steady noise is not confirmed.
        end = self._analysed
        values = np.frombuffer(bytes(self._raw[start * 2 : end * 2]), dtype="<i2").astype(np.float64)
        _, _, gain = detector_levels(values)
        presence = SpectralSpeechPresence()
        # First pass learns the background; second checks the short phrase.
        return presence.confirms(values, gain) or presence.confirms(values, gain)

    def _refine_native_bounds(self):
        import numpy as np
        import webrtcvad

        values = np.frombuffer(bytes(self._raw), dtype="<i2").astype(np.float64)
        if not len(values):
            return
        peak, rms, gain = detector_levels(values)
        classifier = webrtcvad.Vad(self._vad_mode)
        presence = SpectralSpeechPresence()
        first = None
        last = candidate = run = presence_run = 0
        confirmed = False
        for start in range(0, len(values), FRAME_SAMPLES):
            real = values[start : start + FRAME_SAMPLES]
            active = float(np.var(real)) > 1
            frame = np.zeros(FRAME_SAMPLES, dtype="<i2")
            frame[: len(real)] = np.rint(np.clip(real * gain, -32768, 32767)).astype("<i2")
            pcm = frame.tobytes()
            voiced = classifier.is_speech(pcm, RATE) and active
            present = presence.is_speech(pcm, RATE) and active
            presence_run = presence_run + 1 if present else 0
            confirmed |= presence_run >= 3
            if voiced:
                if not run:
                    candidate = start
                run += 1
                if run >= 3 or first is not None:
                    if first is None:
                        first = candidate
                    last = start + len(real)
            else:
                run = 0
        if not confirmed and first is not None and last - first <= RATE * 2:
            # Recheck short candidates after learning the actual background.
            start_sample = max(0, first - self.pre_roll_samples)
            end_sample = min(len(values), last + self.hangover_samples)
            confirmed = presence.confirms(values[start_sample:end_sample], gain)
        self._presence_confirmed = confirmed
        if not confirmed:
            self._first_speech = None
            self._last_speech = 0
        elif first is None or peak <= 2048 or rms <= 32:
            # Keep the whole PTT clip when quiet speech boundaries are uncertain.
            self._first_speech, self._last_speech = 0, self._received
        else:
            # Use both passes so normalization cannot remove an earlier onset.
            if self._first_speech is not None:
                first = min(first, self._first_speech)
                last = max(last, self._last_speech)
            self._first_speech, self._last_speech = first, last
        self.diagnostics = replace(self.diagnostics, speech_detected=confirmed)

    def preview_audio(self, start_sample, maximum_samples):
        if start_sample < 0 or not 1 <= maximum_samples <= RATE * 20:
            raise ValueError("Invalid preview window")
        if self._finished or not self.vad_enabled or not self._presence_confirmed:
            return None
        available = len(self._output) // 2
        for index, (first, last) in enumerate(self._preview_spans):
            if last <= start_sample:
                continue
            start = max(start_sample, first - self.pre_roll_samples, 0)
            end = min(available, start + maximum_samples, last + self.hangover_samples)
            if end <= start:
                return None
            return AudioPreview(
                AudioClip(bytes(self._output[start * 2 : end * 2]), RATE, speech_detected=True),
                start, end, available, last,
                index < len(self._preview_spans) - 1 or available - last >= self.hangover_samples,
            )
        return None

    def finish(self):
        if self._finished:
            raise RuntimeError("Capture processor already finished")
        try:
            if self._pending:
                self._analyse(bytes(self._pending).ljust(FRAME_BYTES, b"\0"), len(self._pending) // 2)
                self._pending.clear()
            self._output.extend(self._denoiser.flush())
            if len(self._output) != self._received * 2:
                raise RuntimeError("DSP did not preserve the capture timeline")
            if self.vad_enabled:
                if self._native_vad:
                    self._refine_native_bounds()
                if not self._presence_confirmed or self._first_speech is None:
                    return AudioClip(b"", RATE)
                start = max(0, self._first_speech - self.pre_roll_samples)
                end = min(self._received, self._last_speech + self.hangover_samples)
                return AudioClip(bytes(self._output[start * 2 : end * 2]), RATE, speech_detected=True)
            return AudioClip(bytes(self._output), RATE)
        finally:
            self.close()

    def close(self):
        self._finished = True
        self._pending.clear()
        self._output.clear()
        self._raw.clear()
        self._preview_spans.clear()
        self._presence = self._vad = None
        self._denoiser.close()
