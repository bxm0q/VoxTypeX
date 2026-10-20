from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from threading import Event
from time import monotonic
from typing import Protocol

MIN_RECORDING_SECONDS = 0.2


@dataclass(frozen=True)
class AudioClip:
    pcm: bytes
    sample_rate: int
    channels: int = 1
    sample_width: int = 2
    # None when VAD is bypassed.
    speech_detected: bool | None = None

    @property
    def samples(self) -> int:
        return len(self.pcm) // (self.channels * self.sample_width)

    @property
    def duration(self) -> float:
        return self.samples / self.sample_rate


class HotkeyListener(Protocol):
    def set_guard_callbacks(
        self, context_changed: Callable[[], None], cancel_current: Callable[[], None]
    ) -> None: ...
    def start(self, key: str, on_press: Callable[[], None], on_release: Callable[[], None]) -> None: ...
    def set_key(self, key: str) -> None: ...
    def check_health(self) -> None: ...
    def close(self) -> None: ...


class AudioRecorder(Protocol):
    device: str | None

    def start(self, held: Event) -> None: ...
    def stop(self) -> AudioClip: ...
    def check_health(self) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class TranscriptionResult:
    text: str = ""
    language: str | None = None
    duration: float = 0.0
    processing_time: float = 0.0
    error: str | None = None
    error_code: str | None = None
    cpu_time: float | None = None


@dataclass(frozen=True)
class EngineCapabilities:
    partial_results: bool = False
    streaming: bool = False
    batch_preview: bool = False


@dataclass(frozen=True)
class AudioPreview:
    audio: AudioClip
    start_sample: int
    end_sample: int
    captured_samples: int
    speech_end_sample: int
    segment_final: bool = False


class TranscriptKind(StrEnum):
    PARTIAL = "partial"
    FINAL = "final"
    RESET = "reset"


@dataclass(frozen=True)
class TranscriptEvent:
    session_id: int
    generation: int
    sequence: int
    kind: TranscriptKind
    stable_text: str = ""
    unstable_text: str = ""
    mode: str = "preview"

    @property
    def text(self) -> str:
        return " ".join(filter(None, (self.stable_text, self.unstable_text)))


class OperationCancelled(RuntimeError):
    pass


class OperationTimeout(TimeoutError):
    pass


@dataclass(frozen=True)
class OperationContext:
    """Shared cancellation and a monotonic deadline; stays in the owner process."""

    request_id: int
    cancelled: Event = field(default_factory=Event)
    deadline: float | None = None

    def checkpoint(self) -> None:
        if self.cancelled.is_set():
            raise OperationCancelled("Operation cancelled")
        if self.deadline is not None and monotonic() >= self.deadline:
            raise OperationTimeout("Operation deadline exceeded")


class ProgressStage(StrEnum):
    RECORDING = "recording"
    PROCESSING = "processing"
    STARTING = "starting"
    DOWNLOADING = "downloading"
    LOADING = "loading"
    TRANSCRIBING = "transcribing"
    FALLBACK = "fallback"
    INSERTING = "inserting"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    FAILED = "failed"


@dataclass(frozen=True)
class ProgressEvent:
    # Keep audio, transcript and selected text out of progress events.
    request_id: int
    stage: ProgressStage


ProgressSink = Callable[[ProgressEvent], None]


class ASRBackend(Protocol):
    capabilities: EngineCapabilities

    def transcribe(
        self,
        audio: AudioClip,
        language: str | None = None,
        *,
        context: OperationContext | None = None,
        progress: ProgressSink | None = None,
    ) -> TranscriptionResult: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    backend: str = "faster-whisper"


class ModelRegistry(Protocol):
    def models(self) -> tuple[ModelSpec, ...]: ...
    def get(self, model_id: str) -> ModelSpec: ...


class ModelManager(Protocol):
    def resolve(
        self,
        model_id: str,
        local_path: str | None = None,
        *,
        context: OperationContext | None = None,
        progress: ProgressSink | None = None,
    ) -> str: ...


class AudioProcessor(Protocol):
    def process(self, audio: AudioClip, context: OperationContext) -> AudioClip | None: ...


class SpeechPipeline(Protocol):
    language: str | None

    def run(
        self,
        audio: AudioClip,
        context: OperationContext,
        progress: ProgressSink | None = None,
    ) -> TranscriptionResult: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...


class TranslationProvider(Protocol):
    def translate(
        self,
        result: TranscriptionResult,
        target_language: str,
        context: OperationContext,
        progress: ProgressSink | None = None,
    ) -> TranscriptionResult: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...


class AIProvider(Protocol):
    def transform(
        self,
        result: TranscriptionResult,
        instruction: str,
        context: OperationContext,
        progress: ProgressSink | None = None,
    ) -> TranscriptionResult: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...


class Transcriber(Protocol):
    language: str | None

    def transcribe(self, audio: AudioClip) -> TranscriptionResult: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...


class TextInserter(Protocol):
    def capture_target(self): ...
    def prepare_target(self, target, cancelled: Event, changed: Event) -> None: ...
    def insert_text(self, text: str): ...
    def close(self) -> None: ...
