from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from voxtypex.interfaces import AudioClip, TranscriptionResult
from voxtypex.whisper_engine import FasterWhisperEngine, WorkerFailed
from voxtypex.whisper_worker import WhisperRuntime, run_worker


def test_lazy_model_reuse_and_generator_consumption(tmp_path, monkeypatch):
    runtime = WhisperRuntime(str(tmp_path), "small", None, True, lambda *_: None)
    loads = []
    calls = []

    def transcribe(samples, **kwargs):
        calls.append(kwargs)
        assert samples.dtype.name == "float32"

        def segments():
            yield SimpleNamespace(text=" Hello ")
            yield SimpleNamespace(text=" world! ")

        return segments(), SimpleNamespace(language="en")

    def load():
        loads.append(True)
        runtime.model = SimpleNamespace(transcribe=transcribe)

    monkeypatch.setattr(runtime, "_load", load)
    assert not loads
    for _ in range(2):
        assert runtime.transcribe(AudioClip(b"\0\0" * 16000, 16000), None).text == "Hello world!"
    assert len(loads) == 1
    assert calls[0]["language"] is None and calls[0]["vad_filter"]


def test_cuda_generator_failure_retries_cpu(tmp_path, monkeypatch):
    runtime = WhisperRuntime(str(tmp_path), "small", None, False, lambda *_: None)
    loads = []

    def load():
        runtime.device = "cpu" if runtime.force_cpu else "cuda"
        loads.append(runtime.device)

        def transcribe(*args, **kwargs):
            def segments():
                if runtime.device == "cuda":
                    raise RuntimeError("cuDNN failure during lazy inference")
                yield SimpleNamespace(text="Привет")

            return segments(), SimpleNamespace(language="ru")

        runtime.model = SimpleNamespace(transcribe=transcribe)

    monkeypatch.setattr(runtime, "_load", load)
    assert runtime.transcribe(AudioClip(b"\0\0" * 16000, 16000), None).text == "Привет"
    assert loads == ["cuda", "cpu"]
    runtime.transcribe(AudioClip(b"\0\0" * 16000, 16000), None)
    assert loads == ["cuda", "cpu"]


def test_missing_local_model(tmp_path):
    runtime = WhisperRuntime(str(tmp_path), "small", str(tmp_path / "missing"), False, lambda *_: None)
    with pytest.raises(FileNotFoundError):
        runtime._resolve()


def test_cuda_initialization_failure_uses_cpu(tmp_path, monkeypatch):
    monkeypatch.setattr("ctranslate2.get_cuda_device_count", lambda: 1)
    monkeypatch.setattr("ctranslate2.get_supported_compute_types", lambda _: {"float16"})
    devices = []

    def model(source, **kwargs):
        devices.append(kwargs["device"])
        if kwargs["device"] == "cuda":
            raise RuntimeError("CUDA DLL missing")
        return SimpleNamespace(transcribe=lambda *a, **k: (iter([]), SimpleNamespace(language="en")))

    monkeypatch.setattr("faster_whisper.WhisperModel", model)
    runtime = WhisperRuntime(str(tmp_path), "small", None, False, lambda *_: None)
    monkeypatch.setattr(runtime, "_resolve", lambda: str(tmp_path))
    assert not runtime.transcribe(AudioClip(b"\0\0" * 16000, 16000), None).error
    assert devices == ["cuda", "cpu"]


def test_missing_cache_and_failed_download(tmp_path, monkeypatch):
    attempts = []

    def download(*args, **kwargs):
        attempts.append(kwargs.get("local_files_only", False))
        raise OSError("network unavailable")

    monkeypatch.setattr("faster_whisper.utils.download_model", download)
    runtime = WhisperRuntime(str(tmp_path), "small", None, False, lambda *_: None)
    with pytest.raises(OSError, match="network unavailable"):
        runtime._resolve()
    assert attempts == [True, False]


def test_partial_cache_resumes_download(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}")
    attempts = []

    def download(*args, **kwargs):
        local = kwargs.get("local_files_only", False)
        attempts.append(local)
        if not local:
            (tmp_path / "model.bin").write_bytes(b"model")
            (tmp_path / "tokenizer.json").write_text("{}")
        return str(tmp_path)

    monkeypatch.setattr("faster_whisper.utils.download_model", download)
    runtime = WhisperRuntime(str(tmp_path), "small", None, True, lambda *_: None)
    assert runtime._resolve() == str(tmp_path)
    assert attempts == [True, False]


def test_native_cuda_crash_retries_cpu(tmp_path, monkeypatch):
    engine = FasterWhisperEngine(tmp_path)
    calls = []
    engine._connection = SimpleNamespace(send=lambda _: None)
    monkeypatch.setattr(engine, "_start", lambda: None)
    monkeypatch.setattr(engine, "_dispose", lambda: None)

    def receive():
        calls.append(engine._force_cpu)
        if len(calls) == 1:
            engine._device = "cuda"
            raise WorkerFailed("native crash")
        return TranscriptionResult(text="OK")

    monkeypatch.setattr(engine, "_receive", receive)
    assert engine.transcribe(AudioClip(b"", 16000)).text == "OK"
    assert calls == [False, True]


def test_worker_reports_memory_error(tmp_path, monkeypatch):
    messages = []
    incoming = [(AudioClip(b"", 16000), None)]

    def recv():
        if incoming:
            return incoming.pop()
        raise EOFError

    def fail(*args):
        raise MemoryError("allocation failed")

    monkeypatch.setattr(WhisperRuntime, "transcribe", fail)
    connection = SimpleNamespace(recv=recv, send=messages.append, close=lambda: None)
    run_worker(connection, str(tmp_path), "small", None, True)
    assert "недостаточно памяти" in messages[-1][1].error


def test_worker_progress_reaches_parent_and_resets_phase_deadline(tmp_path):
    engine = FasterWhisperEngine(tmp_path)
    messages = iter(
        [
            ("phase", "downloading"),
            ("phase", "loading"),
            ("phase", "transcribing"),
            ("result", TranscriptionResult(text="OK")),
        ]
    )
    engine._connection = SimpleNamespace(poll=lambda _: True, recv=lambda: next(messages))
    engine._set_phase("starting")
    assert engine._receive().text == "OK"
    assert engine.phase == "transcribing"
    assert 0 < engine._deadline - monotonic() <= 60


def test_hung_worker_times_out_and_next_request_can_retry(tmp_path, monkeypatch):
    monkeypatch.setattr("voxtypex.whisper_engine.run_worker", sleeping_worker)
    engine = FasterWhisperEngine(tmp_path)
    original = engine._set_phase

    def short_deadline(phase):
        original(phase)
        engine._deadline = monotonic() + 0.5

    monkeypatch.setattr(engine, "_set_phase", short_deadline)
    try:
        result = engine.transcribe(AudioClip(b"\1\0" * 16000, 16000))
        assert result.error_code == "timeout"
        assert engine._process is None and not engine._cancelled.is_set()
        monkeypatch.setattr(engine, "_set_phase", original)
        monkeypatch.setattr(engine, "_receive", lambda: TranscriptionResult(text="retry OK"))
        assert engine.transcribe(AudioClip(b"\1\0" * 16000, 16000)).text == "retry OK"
    finally:
        engine.close()


def sleeping_worker(connection, *args):
    connection.recv()
    sleep(30)


def nonreading_worker(connection, *args):
    sleep(30)


def test_cancel_while_large_clip_is_blocked_in_pipe(tmp_path, monkeypatch):
    monkeypatch.setattr("voxtypex.whisper_engine.run_worker", nonreading_worker)
    engine = FasterWhisperEngine(tmp_path)
    results = []
    thread = Thread(target=lambda: results.append(engine.transcribe(AudioClip(b"\0\0" * 16000 * 120, 16000))))
    thread.start()
    try:
        deadline = monotonic() + 4
        while engine._process is None or engine._process.pid is None:
            assert monotonic() < deadline
            sleep(0.01)
        sleep(0.2)
        engine.cancel()
        thread.join(5)
        assert not thread.is_alive()
        assert results[0].error == "Распознавание отменено"
        assert engine._process is None
        import threading

        assert not any(t.name == "voxtypex-audio-send" for t in threading.enumerate())
    finally:
        engine.close()
        thread.join(4)


def test_cancel_terminates_real_worker_process(tmp_path, monkeypatch):
    monkeypatch.setattr("voxtypex.whisper_engine.run_worker", sleeping_worker)
    engine = FasterWhisperEngine(tmp_path)
    results = []
    thread = Thread(target=lambda: results.append(engine.transcribe(AudioClip(b"\0\0" * 16000, 16000))))
    thread.start()
    try:
        deadline = monotonic() + 3
        while engine._process is None or engine._process.pid is None:
            assert monotonic() < deadline
            sleep(0.01)
        engine.cancel()
        thread.join(4)
        assert not thread.is_alive()
        assert results[0].error == "Распознавание отменено"
        assert engine._process is None
    finally:
        engine.close()
        thread.join(4)
