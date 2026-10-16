"""Language routing and a single fallback; no native imports in this process."""

import logging

from .interfaces import (
    OperationCancelled,
    OperationContext,
    OperationTimeout,
    ProgressEvent,
    ProgressStage,
    TranscriptionResult,
)
from .local_engine import TranscribeCppEngine
from .transcription import TranscriptionService
from .whisper_engine import FasterWhisperEngine

logger = logging.getLogger(__name__)


class LanguageASRBackend:
    def __init__(self, settings, cache_dir, *, local_factory=None, whisper_factory=None, registry=None):
        if registry is None:
            from .local_models import LanguageModelRegistry

            registry = LanguageModelRegistry()
        self.settings, self.cache_dir, self.registry = settings, cache_dir, registry
        self._local_factory = local_factory or TranscribeCppEngine
        self._whisper_factory = whisper_factory or FasterWhisperEngine
        self._local = self._whisper = self._active = None
        self._local_id = None
        self._whisper_needs_cleanup = False
        self._closed = False

    @property
    def phase(self):
        return getattr(self._active, "phase", "starting")

    @property
    def _process(self):
        return getattr(self._active, "_process", None)

    @property
    def worker_pid(self):
        process = self._process
        return process.pid if process is not None else None

    @property
    def _device(self):
        return getattr(self._active, "_device", None)

    @property
    def device(self):
        return self._device

    def _release_local(self):
        # close() is terminal even when process cleanup fails. Invalidate the
        # model ID first, retaining the backend only for a cleanup retry.
        self._local_id = None
        if self._local is not None:
            self._local.close()
            self._local = None

    def _release_whisper(self):
        self._whisper_needs_cleanup = True
        if self._whisper is not None:
            self._whisper.close()
            self._whisper = None
        self._whisper_needs_cleanup = False

    def _fallback(self):
        self._release_local()
        if self._whisper_needs_cleanup:
            self._release_whisper()
        if self._whisper is None:
            self._whisper = self._whisper_factory(
                self.cache_dir,
                self.settings.whisper_model,
                self.settings.model_path,
            )
        self._active = self._whisper
        return self._whisper

    def transcribe(self, audio, language=None, *, context=None, progress=None):
        context = context or OperationContext(0)
        try:
            context.checkpoint()
            if self._closed:
                raise OperationCancelled("Backend closed")
            if language is None or self.settings.asr_backend == "faster-whisper":
                return self._fallback().transcribe(audio, language, context=context, progress=progress)
            spec = self.registry.for_language(language, getattr(self.settings, f"{language}_quality"))
            try:
                if self._whisper is not None:
                    self._release_whisper()
                if self._local_id != spec.id:
                    self._release_local()
                    self._local = self._local_factory(self.cache_dir, spec.id)
                    self._local_id = spec.id
                self._active = self._local
                result = self._local.transcribe(audio, language, context=context, progress=progress)
                context.checkpoint()
                if not result.error or result.error_code in ("cancelled", "timeout"):
                    return result
                logger.warning("Local ASR failed (%s); using Whisper", result.error_code or "backend")
            except (OperationCancelled, OperationTimeout):
                raise
            except Exception as exc:
                logger.warning("Local ASR failed (%s); using Whisper", type(exc).__name__)
            context.checkpoint()
            self._release_local()
            if progress is not None:
                progress(ProgressEvent(context.request_id, ProgressStage.FALLBACK))
            return self._fallback().transcribe(audio, language, context=context, progress=progress)
        except (OperationCancelled, OperationTimeout) as exc:
            return TranscriptionResult(
                error="Operation stopped",
                error_code="cancelled" if isinstance(exc, OperationCancelled) else "timeout",
            )

    def release_idle(self):
        for backend in (self._local, self._whisper):
            if backend is not None and hasattr(backend, "release_idle"):
                backend.release_idle()

    def cancel(self):
        self._closed = True
        for backend in (self._local, self._whisper):
            if backend is not None:
                backend.cancel()

    def close(self):
        self._closed = True
        for backend in (self._local, self._whisper):
            if backend is not None:
                backend.close()


def make_transcriber(settings, cache_dir):
    engine = (
        LanguageASRBackend(settings, cache_dir)
        if settings.asr_backend == "transcribe-cpp"
        else FasterWhisperEngine(cache_dir, settings.whisper_model, settings.model_path)
    )
    return TranscriptionService(engine, settings.language)
