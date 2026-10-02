import logging
import unicodedata
from dataclasses import replace
from threading import Lock
from time import perf_counter
from typing import Protocol

import numpy as np

from .interfaces import MIN_RECORDING_SECONDS, AudioClip, TranscriptionResult


class SpeechEngine(Protocol):
    def transcribe(self, audio: AudioClip, language: str | None) -> TranscriptionResult: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...


def clean_text(text) -> str:
    if not isinstance(text, str) or "\ufffd" in text:
        return ""
    if any(unicodedata.category(c).startswith("C") and not c.isspace() for c in text):
        return ""
    text = " ".join(text.split())
    return text if any(c.isalnum() for c in text) else ""


class TranscriptionService:
    def __init__(self, engine: SpeechEngine, language: str | None = None):
        self.engine = engine
        self.language = language
        self._lock = Lock()

    @property
    def progress(self):
        return getattr(self.engine, "phase", "transcribing")

    def transcribe(self, audio: AudioClip) -> TranscriptionResult:
        start = perf_counter()
        if not self._lock.acquire(blocking=False):
            return TranscriptionResult(error="Распознавание уже выполняется")
        duration = 0.0
        try:
            if (
                audio.channels != 1
                or audio.sample_width != 2
                or audio.sample_rate != 16000
                or len(audio.pcm) % 2
            ):
                raise ValueError("STT requires mono PCM16 at 16000 Hz")
            duration = audio.duration
            if duration < MIN_RECORDING_SECONDS:
                return TranscriptionResult(duration=duration, processing_time=perf_counter() - start)
            # Здесь эффективно подавляется только цифровая тишина; VAD справляется с обычным фоновым шумом
            samples = np.frombuffer(audio.pcm, dtype="<i2").astype(np.float32) / 32768.0
            if float(np.sqrt(np.mean(samples * samples))) < 0.0001:
                return TranscriptionResult(duration=duration, processing_time=perf_counter() - start)
            result = self.engine.transcribe(audio, self.language)
            text = "" if result.error else clean_text(result.text)
            return replace(
                result,
                text=text,
                language=result.language if text else None,
                duration=duration,
                processing_time=perf_counter() - start,
            )
        except Exception as exc:
            logging.getLogger(__name__).exception("Speech engine failed")
            return TranscriptionResult(
                duration=duration,
                processing_time=perf_counter() - start,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            self._lock.release()

    def cancel(self):
        self.engine.cancel()

    def close(self):
        self.engine.close()
