import importlib
from dataclasses import replace
from threading import Event
from time import monotonic
from types import SimpleNamespace

import pytest

from voxtypex.config import Settings, SettingsService
from voxtypex.interfaces import AudioClip, OperationContext, ProgressStage, TranscriptionResult


def new_settings(**kwargs):
    return Settings(**kwargs)


def test_language_profiles_are_validated_and_old_schema_is_preserved(tmp_path):
    path = tmp_path / "settings.json"
    original = b'{"schema_version":3,"language":"uk","whisper_model":"base"}'
    path.write_bytes(original)
    settings = SettingsService(path).load()
    assert settings.schema_version == 6
    assert settings.whisper_model == "base"
    assert settings.uk_quality == "compact"
    assert path.with_name("settings.json.v3.bak").read_bytes() == original
    assert replace(settings, uk_quality="accurate").uk_quality == "accurate"
    with pytest.raises(ValueError):
        replace(settings, en_quality="unknown")


class Backend:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.closed = False
        self.phase = "transcribing"

    def transcribe(self, audio, language, **kwargs):
        self.calls.append(language)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self):
        self.closed = True

    def cancel(self):
        pass


def router(local_result, *, language="uk"):
    module = importlib.import_module("voxtypex.asr")
    local = Backend(local_result)
    whisper = Backend(TranscriptionResult(text="fallback", language=language))
    registry = SimpleNamespace(for_language=lambda lang, quality: SimpleNamespace(id=lang + "-" + quality))
    engine = module.LanguageASRBackend(
        new_settings(asr_backend="transcribe-cpp", language=language),
        "unused",
        local_factory=lambda *args: local,
        whisper_factory=lambda *args: whisper,
        registry=registry,
    )
    return engine, local, whisper


def test_explicit_language_uses_local_and_auto_uses_whisper():
    engine, local, whisper = router(TranscriptionResult(text="local", language="uk"))
    audio = AudioClip(b"\0\0" * 16000, 16000)
    assert engine.transcribe(audio, "uk").text == "local"
    assert engine.transcribe(audio, None).text == "fallback"
    assert local.closed and engine._local is None
    assert local.calls == ["uk"]
    assert whisper.calls == [None]
    engine.close()
    assert local.closed and whisper.closed


def test_auto_to_explicit_language_releases_whisper_before_local_use():
    engine, local, whisper = router(TranscriptionResult(text="local", language="uk"))
    audio = AudioClip(b"\0\0" * 16000, 16000)
    assert engine.transcribe(audio, None).text == "fallback"
    assert not whisper.closed and not local.calls
    assert engine.transcribe(audio, "uk").text == "local"
    assert whisper.closed and engine._whisper is None
    assert not local.closed
    engine.close()


@pytest.mark.parametrize("retiring", ["local", "whisper"])
def test_failed_close_is_retried_without_reusing_terminal_backend(retiring):
    from voxtypex.asr import LanguageASRBackend
    from voxtypex.whisper_engine import WorkerFailed

    class DelayedClose(Backend):
        def __init__(self, text):
            super().__init__(TranscriptionResult(text=text, language="en"))
            self.close_calls = 0

        def close(self):
            self.closed = True
            self.close_calls += 1
            if self.close_calls == 1:
                raise WorkerFailed("STT worker did not exit after forced termination")

        def transcribe(self, audio, language, **kwargs):
            if self.closed:
                return TranscriptionResult(error="stopped", error_code="cancelled")
            return super().transcribe(audio, language, **kwargs)

    old = DelayedClose(retiring)
    replacements = {}

    def factory(kind):
        calls = 0

        def create(*args):
            nonlocal calls
            calls += 1
            if kind == retiring and calls == 1:
                return old
            backend = Backend(TranscriptionResult(text=kind + " replacement", language="en"))
            replacements[kind] = backend
            return backend

        return create

    engine = LanguageASRBackend(
        Settings(asr_backend="transcribe-cpp", language="en"),
        "unused",
        local_factory=factory("local"),
        whisper_factory=factory("whisper"),
    )
    audio = AudioClip(b"\0\0" * 16000, 16000)
    first_language = "en" if retiring == "local" else None
    assert engine.transcribe(audio, first_language).text == retiring
    if retiring == "local":
        with pytest.raises(WorkerFailed):
            engine.transcribe(audio, None)
    else:
        engine.transcribe(audio, "en")
    result = engine.transcribe(audio, first_language)
    assert not result.error and result.text == retiring + " replacement"
    assert old.close_calls >= 2
    assert engine._active is replacements[retiring]
    engine.close()


@pytest.mark.parametrize(
    "failure", [OSError("private content"), TranscriptionResult(text="partial", error="broken")]
)
def test_native_failure_falls_back_once_without_partial_text_or_error_body(failure, caplog):
    engine, local, whisper = router(failure)
    events = []
    result = engine.transcribe(
        AudioClip(b"\0\0" * 16000, 16000), "uk", context=OperationContext(8), progress=events.append
    )
    assert result.text == "fallback"
    assert local.closed
    assert whisper.calls == ["uk"]
    assert [event.stage for event in events] == [ProgressStage.FALLBACK]
    assert "private content" not in caplog.text


def test_cancelled_or_expired_result_never_starts_fallback():
    engine, local, whisper = router(TranscriptionResult(error="broken"))
    for context in (OperationContext(1, Event(), monotonic() - 1), OperationContext(2)):
        if context.request_id == 2:
            context.cancelled.set()
        result = engine.transcribe(AudioClip(b"\0\0" * 16000, 16000), "uk", context=context)
        assert result.error_code in ("cancelled", "timeout")
    assert not local.calls and not whisper.calls


def test_vad_segments_are_bounded_and_non_overlapping():
    segment = importlib.import_module("voxtypex.local_worker").bounded_segments
    spans = segment([{"start": 0, "end": 1100000}, {"start": 1150000, "end": 1300000}], 320000)
    assert spans[0][0] == 0 and spans[-1][1] == 1300000
    assert all(0 < end - start <= 320000 for start, end in spans)
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))
    assert sum(end - start for start, end in spans) == 1250000


def test_native_modules_are_not_loaded_by_main_imports():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import voxtypex.asr, voxtypex.local_worker; "
            'assert "transcribe_cpp" not in sys.modules; assert "onnxruntime" not in sys.modules',
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_idle_worker_is_released_without_terminal_cancellation():
    import multiprocessing
    import time

    from voxtypex.whisper_engine import FasterWhisperEngine

    engine = FasterWhisperEngine("unused")
    process = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(30,), daemon=True)
    process.start()
    engine._process = process
    try:
        engine.release_idle(idle_seconds=3600)
        assert engine._process is process and process.is_alive()
        engine._last_used -= 3601
        engine.release_idle(idle_seconds=3600)
        assert engine._process is None
        assert not engine._cancelled.is_set()
    finally:
        engine.close()


def test_main_builds_and_reconfigures_the_real_transcriber_factory(tmp_path, monkeypatch):
    from voxtypex import main
    from voxtypex.config import SettingsService

    app = SimpleNamespace(
        setApplicationName=lambda *_: None,
        setOrganizationName=lambda *_: None,
        exec=lambda: 0,
        quit=lambda: None,
        exit=lambda _: None,
    )
    calls = []
    monkeypatch.setattr(main, "QApplication", lambda _: app)
    monkeypatch.setattr(
        main,
        "QTimer",
        lambda *_: SimpleNamespace(timeout=SimpleNamespace(connect=lambda _: None), start=lambda _: None),
    )
    monkeypatch.setattr(main, "QMessageBox", SimpleNamespace(critical=lambda *_: None))
    monkeypatch.setattr(main, "configure_logging", lambda *_: None)
    monkeypatch.setattr(main, "data_directory", lambda: tmp_path)
    monkeypatch.setattr(main, "model_directory", lambda: tmp_path / "models")
    factory_name = "build_transcriber" if hasattr(main, "build_transcriber") else "make_transcriber"
    monkeypatch.setattr(
        main, factory_name, lambda settings, directory: calls.append(settings.asr_backend) or object()
    )
    for name in ("MicrophoneRecorder", "GlobalHotkey", "TextInsertionService"):
        monkeypatch.setattr(main, name, lambda *args, **kwargs: object())

    def service(*args, **kwargs):
        kwargs["transcriber_factory"](replace(Settings(), asr_backend="transcribe-cpp"))
        return object()

    monkeypatch.setattr(main, "RecordingService", service)
    monkeypatch.setattr(
        main, "TrayApplication", lambda *args: SimpleNamespace(start=lambda: None, close=lambda: None)
    )
    assert main.main() == 0
    assert calls == ["faster-whisper", "transcribe-cpp"]
    assert SettingsService(tmp_path / "settings.json").load().schema_version == 6
