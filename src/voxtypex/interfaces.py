from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import Protocol

MIN_RECORDING_SECONDS = 0.2


@dataclass(frozen=True)
class AudioClip:
    pcm: bytes
    sample_rate: int
    channels: int = 1
    sample_width: int = 2

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
