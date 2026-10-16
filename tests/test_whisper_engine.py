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
    incoming = [(5, AudioClip(b"", 16000), None)]

    def recv():
        if incoming:
            return incoming.pop()
        raise EOFError

    def fail(*args):
        raise MemoryError("allocation failed")

    monkeypatch.setattr(WhisperRuntime, "transcribe", fail)
    connection = SimpleNamespace(recv=recv, send=messages.append, close=lambda: None)
    run_worker(connection, str(tmp_path), "small", None, True)
    assert messages[-1][0] == 5
    assert "недостаточно памяти" in messages[-1][2].error


def test_worker_progress_reaches_parent_and_resets_phase_deadline(tmp_path):
    engine = FasterWhisperEngine(tmp_path)
    messages = iter(
        [
            (0, "phase", "downloading"),
            (0, "phase", "loading"),
            (0, "phase", "transcribing"),
            (0, "result", TranscriptionResult(text="OK")),
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


@pytest.mark.parametrize("mode", ["shutdown", "request"])
def test_cancel_while_large_clip_is_blocked_in_pipe(tmp_path, monkeypatch, mode):
    from voxtypex.interfaces import OperationContext

    monkeypatch.setattr("voxtypex.whisper_engine.run_worker", nonreading_worker)
    engine = FasterWhisperEngine(tmp_path)
    operation = OperationContext(1) if mode == "request" else None
    results = []
    thread = Thread(
        target=lambda: results.append(
            engine.transcribe(
                AudioClip(b"\0\0" * 16000 * 120, 16000),
                context=operation,
            )
        )
    )
    thread.start()
    try:
        deadline = monotonic() + 4
        while engine._process is None or engine._process.pid is None:
            assert monotonic() < deadline
            sleep(0.01)
        sleep(0.2)
        if operation is not None:
            operation.cancelled.set()
        else:
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


def echo_worker(connection, *args):
    request_id, _, _ = connection.recv()
    connection.send((request_id, "result", TranscriptionResult(text="retry OK")))
    connection.close()


def test_request_cancel_stops_process_but_next_request_can_transcribe(tmp_path, monkeypatch):
    from voxtypex.interfaces import OperationContext

    monkeypatch.setattr("voxtypex.whisper_engine.run_worker", sleeping_worker)
    operation = OperationContext(1)
    engine = FasterWhisperEngine(tmp_path)
    results = []
    thread = Thread(
        target=lambda: results.append(
            engine.transcribe(
                AudioClip(b"\1\0" * 16000, 16000),
                context=operation,
            )
        )
    )
    thread.start()
    try:
        deadline = monotonic() + 4
        while engine._process is None or engine._process.pid is None:
            assert thread.is_alive() and monotonic() < deadline
            sleep(0.01)
        operation.cancelled.set()
        thread.join(4)
        assert not thread.is_alive() and results[0].error_code == "cancelled"
        assert engine._process is None
        monkeypatch.setattr("voxtypex.whisper_engine.run_worker", echo_worker)
        assert (
            engine.transcribe(AudioClip(b"\1\0" * 16000, 16000), context=OperationContext(2)).text
            == "retry OK"
        )
    finally:
        operation.cancelled.set()
        thread.join(4)
        engine.close()


def test_request_deadline_stops_real_worker(tmp_path, monkeypatch):
    from voxtypex.interfaces import OperationContext

    monkeypatch.setattr("voxtypex.whisper_engine.run_worker", sleeping_worker)
    engine = FasterWhisperEngine(tmp_path)
    try:
        result = engine.transcribe(
            AudioClip(b"\1\0" * 16000, 16000),
            context=OperationContext(1, deadline=monotonic() + 0.5),
        )
        assert result.error_code == "timeout" and engine._process is None
    finally:
        engine.close()


def test_stale_process_results_and_progress_are_not_applied_to_next_request(tmp_path):
    from voxtypex.interfaces import OperationContext

    engine = FasterWhisperEngine(tmp_path)
    engine._operation = OperationContext(2)
    engine._ipc_request_id = 2
    events = []
    engine._progress_sink = events.append
    messages = iter(
        [
            (1, "phase", "downloading"),
            (1, "result", TranscriptionResult(text="old text")),
            (2, "phase", "transcribing"),
            (2, "result", TranscriptionResult(text="current text")),
        ]
    )
    engine._connection = SimpleNamespace(poll=lambda _: True, recv=lambda: next(messages))
    assert engine._receive().text == "current text"
    assert [(e.request_id, e.stage.value) for e in events] == [(2, "transcribing")]


def test_request_deadline_is_not_reset_by_worker_progress(tmp_path, monkeypatch):
    from voxtypex import interfaces

    clock = [0.0]
    monkeypatch.setattr(interfaces, "monotonic", lambda: clock[0])
    engine = FasterWhisperEngine(tmp_path)

    def recv():
        clock[0] = 2
        return 1, "phase", "loading"

    engine._connection = SimpleNamespace(
        send=lambda _: None, poll=lambda _: True, recv=recv, close=lambda: None
    )
    monkeypatch.setattr(engine, "_start", lambda: None)
    result = engine.transcribe(
        AudioClip(b"\1\0" * 16000, 16000),
        context=interfaces.OperationContext(1, deadline=1),
    )
    assert engine.phase == "loading" and result.error_code == "timeout"
    assert engine._connection is None


def test_cancel_during_cuda_failure_does_not_start_a_fallback_process(tmp_path, monkeypatch):
    from voxtypex.interfaces import OperationContext

    operation = OperationContext(1)
    engine = FasterWhisperEngine(tmp_path)
    starts = []
    engine._connection = SimpleNamespace(send=lambda _: None)
    monkeypatch.setattr(engine, "_start", lambda: starts.append(True))
    monkeypatch.setattr(engine, "_dispose", lambda: None)

    def crash():
        engine._device = "cuda"
        operation.cancelled.set()
        raise WorkerFailed("CUDA process exited")

    monkeypatch.setattr(engine, "_receive", crash)
    result = engine.transcribe(AudioClip(b"\1\0" * 16000, 16000), context=operation)
    assert result.error_code == "cancelled" and len(starts) == 1
    assert not engine._force_cpu


class SlowExitProcess:
    """Windows termination may return before native I/O cleanup has finished."""

    pid = 123

    def __init__(self, *, can_exit=True):
        self.alive = True
        self.can_exit = can_exit
        self.killed = self.closed = False
        self.waited_after_kill = 0

    def is_alive(self):
        return self.alive

    def terminate(self):
        pass

    def kill(self):
        self.killed = True

    def join(self, timeout):
        if self.killed:
            self.waited_after_kill += timeout
            if self.can_exit and self.waited_after_kill >= 2:
                self.alive = False

    def close(self):
        if self.alive:
            raise ValueError("Cannot close a process while it is still running")
        self.closed = True


def test_slow_forced_exit_is_awaited_before_closing_process(tmp_path):
    engine = FasterWhisperEngine(tmp_path)
    process = SlowExitProcess()
    engine._process = process
    engine._dispose()
    assert process.killed and process.closed and not process.alive
    assert engine._process is None


def test_failed_termination_keeps_process_handle_for_cleanup_retry(tmp_path):
    engine = FasterWhisperEngine(tmp_path)
    process = SlowExitProcess(can_exit=False)
    engine._process = process
    with pytest.raises(WorkerFailed, match="did not exit"):
        engine._dispose()
    assert not process.closed and engine._process is process
    process.can_exit = True
    engine._dispose()
    assert process.closed and engine._process is None
