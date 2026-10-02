from threading import Event, Thread

import numpy as np
import pytest

from voxtypex.interfaces import AudioClip, TranscriptionResult
from voxtypex.transcription import TranscriptionService, clean_text


def audio():
    return AudioClip(np.full(16000, 200, dtype="<i2").tobytes(), 16000)


class Engine:
    def __init__(self, result=None):
        self.result = result or TranscriptionResult(text="  Привет,   мир! \n", language="ru")
        self.calls = []

    def transcribe(self, audio, language):
        self.calls.append((audio, language))
        return self.result

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True


def test_result_and_language_boundary():
    engine = Engine()
    service = TranscriptionService(engine, "uk")
    result = service.transcribe(audio())
    assert result.text == "Привет, мир!" and result.language == "ru"
    assert result.duration == 1 and result.processing_time >= 0
    assert engine.calls[0][1] == "uk"
    service.cancel()
    service.close()
    assert engine.cancelled and engine.closed


@pytest.mark.parametrize("text", ["", " \n ", "...", "\ufffd", "abc\x00", None])
def test_invalid_text(text):
    assert clean_text(text) == ""


@pytest.mark.parametrize("text", ["Привіт, як справи?", "Привет!", "Hello!", "123"])
def test_supported_text_is_preserved(text):
    assert clean_text(text) == text


def test_silence_and_short_audio_do_not_load_engine():
    engine = Engine()
    service = TranscriptionService(engine)
    for clip in (AudioClip(b"\0\0" * 16000, 16000), AudioClip(b"\x10\0" * 100, 16000)):
        assert not service.transcribe(clip).text
    assert not engine.calls


@pytest.mark.parametrize(
    "clip", [AudioClip(b"x", 16000), AudioClip(b"xx", 48000), AudioClip(b"xxxx", 16000, channels=2)]
)
def test_bad_audio_returns_error(clip):
    assert TranscriptionService(Engine()).transcribe(clip).error


@pytest.mark.parametrize(
    "error",
    [MemoryError("RAM"), FileNotFoundError("model"), OSError("download failed"), RuntimeError("inference")],
)
def test_engine_errors_return_result(error):
    engine = Engine()

    def fail(*args):
        raise error

    engine.transcribe = fail
    result = TranscriptionService(engine).transcribe(audio())
    assert result.error and not result.text
    assert type(error).__name__ in result.error


def test_only_one_transcription_at_a_time():
    entered, release = Event(), Event()
    engine = Engine()

    def slow(*args):
        entered.set()
        assert release.wait(2)
        return TranscriptionResult()

    engine.transcribe = slow
    service = TranscriptionService(engine)
    thread = Thread(target=lambda: service.transcribe(audio()))
    thread.start()
    try:
        assert entered.wait(1)
        assert service.transcribe(audio()).error == "Распознавание уже выполняется"
    finally:
        release.set()
        thread.join(2)


def test_error_result_never_leaks_text_and_keeps_error_category():
    engine = Engine(
        TranscriptionResult(
            text="must not be inserted", language="en", error="model unavailable", error_code="model"
        )
    )
    result = TranscriptionService(engine).transcribe(audio())
    assert result.text == "" and result.language is None
    assert result.error_code == "model"


def test_exception_releases_transcription_gate_for_retry():
    engine = Engine()
    original = engine.transcribe

    def fail(*_):
        raise OSError("temporary error")

    engine.transcribe = fail
    service = TranscriptionService(engine)
    assert service.transcribe(audio()).error
    engine.transcribe = original
    assert service.transcribe(audio()).text == "Привет, мир!"
