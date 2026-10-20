import logging
import unicodedata
from dataclasses import replace
from threading import Lock
from time import perf_counter
from typing import Protocol

from .audio_processing import PCM16Processor
from .interfaces import (
    ASRBackend,
    AudioClip,
    AudioProcessor,
    EngineCapabilities,
    OperationCancelled,
    OperationContext,
    OperationTimeout,
    ProgressEvent,
    ProgressSink,
    ProgressStage,
    TranscriptionResult,
)


class SpeechEngine(Protocol):
    capabilities: EngineCapabilities

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
    """Cancellable speech pipeline with legacy transcribe() support."""

    def __init__(
        self,
        engine: ASRBackend | SpeechEngine,
        language: str | None = None,
        *,
        processor: AudioProcessor | None = None,
    ):
        self.engine = engine
        self.language = language
        self.processor = processor or PCM16Processor()
        self._lock = Lock()

    @property
    def progress(self):
        return getattr(self.engine, "phase", "transcribing")

    @property
    def capabilities(self):
        return getattr(self.engine, "capabilities", EngineCapabilities())

    def transcribe(self, audio: AudioClip) -> TranscriptionResult:
        return self._transcribe(audio, OperationContext(0), None, legacy=True)

    def run(
        self,
        audio: AudioClip,
        context: OperationContext,
        progress: ProgressSink | None = None,
    ) -> TranscriptionResult:
        return self._transcribe(audio, context, progress, legacy=False)

    def _transcribe(self, audio, context, progress, *, legacy):
        def emit(stage):
            if progress is not None:
                progress(ProgressEvent(context.request_id, stage))

        start = perf_counter()
        if not self._lock.acquire(blocking=False):
            return TranscriptionResult(error="Распознавание уже выполняется", error_code="busy")
        duration = 0.0
        error_code = "audio"
        try:
            context.checkpoint()
            emit(ProgressStage.PROCESSING)
            processed = self.processor.process(audio, context)
            duration = audio.duration
            context.checkpoint()
            if processed is None:
                emit(ProgressStage.COMPLETED)
                return TranscriptionResult(duration=duration, processing_time=perf_counter() - start)
            error_code = "backend"
            emit(ProgressStage.TRANSCRIBING)
            if legacy:
                result = self.engine.transcribe(processed, self.language)
            else:
                result = self.engine.transcribe(processed, self.language, context=context, progress=progress)
            context.checkpoint()
            text = "" if result.error else clean_text(result.text)
            stage = {
                "cancelled": ProgressStage.CANCELLED,
                "timeout": ProgressStage.TIMEOUT,
            }.get(result.error_code, ProgressStage.FAILED if result.error else ProgressStage.COMPLETED)
            emit(stage)
            return replace(
                result,
                text=text,
                language=result.language if text else None,
                duration=duration,
                processing_time=perf_counter() - start,
            )
        except (OperationCancelled, OperationTimeout) as exc:
            cancelled = isinstance(exc, OperationCancelled)
            emit(ProgressStage.CANCELLED if cancelled else ProgressStage.TIMEOUT)
            return TranscriptionResult(
                duration=duration,
                processing_time=perf_counter() - start,
                error="Распознавание отменено" if cancelled else "Превышено время ожидания распознавания",
                error_code="cancelled" if cancelled else "timeout",
            )
        except Exception as exc:
            # Provider exceptions may echo private input.
            logging.getLogger(__name__).error("Speech pipeline failed (%s)", type(exc).__name__)
            emit(ProgressStage.FAILED)
            return TranscriptionResult(
                duration=duration,
                processing_time=perf_counter() - start,
                error=f"{type(exc).__name__}: распознавание не удалось",
                error_code=error_code,
            )
        finally:
            self._lock.release()

    def cancel(self):
        self.engine.cancel()

    def release_idle(self):
        release = getattr(self.engine, "release_idle", None)
        if release is not None:
            release()

    def close(self):
        self.engine.close()
