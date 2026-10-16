from threading import Event, Thread
from time import monotonic

import pytest

from voxtypex import interfaces
from voxtypex.interfaces import AudioClip, TranscriptionResult
from voxtypex.transcription import TranscriptionService


def context(request_id=7, *, cancelled=None, deadline=None):
    assert hasattr(interfaces, "OperationContext"), "Missing per-request cancellation/deadline contract"
    return interfaces.OperationContext(request_id, cancelled=cancelled or Event(), deadline=deadline)


def clip():
    return AudioClip(b"\xc8\x00" * 16000, 16000)


class Backend:
    def __init__(self):
        self.calls = 0
        self.callback = None

    def transcribe(self, audio, language, *, context=None, progress=None):
        self.calls += 1
        assert audio == clip() and language == "uk"
        if self.callback:
            self.callback(context)
        return TranscriptionResult(text="  Привіт, світ!  ", language="uk")

    def cancel(self):
        pass

    def close(self):
        pass


def test_pipeline_runs_existing_asr_and_emits_request_scoped_metadata():
    events = []
    service = TranscriptionService(Backend(), "uk")
    result = service.run(clip(), context(), events.append)
    assert result.text == "Привіт, світ!" and result.duration == 1
    assert [e.stage.value for e in events] == ["processing", "transcribing", "completed"]
    assert all(e.request_id == 7 for e in events)
    assert not any(hasattr(e, "text") or hasattr(e, "audio") for e in events)


@pytest.mark.parametrize("stopped", ["cancelled", "timeout"])
def test_stopped_request_never_reaches_backend_and_next_request_works(stopped):
    backend = Backend()
    service = TranscriptionService(backend, "uk")
    cancelled = Event()
    if stopped == "cancelled":
        cancelled.set()
    operation = context(cancelled=cancelled, deadline=monotonic() - 1 if stopped == "timeout" else None)
    events = []
    result = service.run(clip(), operation, events.append)
    assert not result.text and result.error_code == stopped and backend.calls == 0
    assert events[-1].stage.value == stopped
    assert service.run(clip(), context(8)).text == "Привіт, світ!"


@pytest.mark.parametrize("stopped", ["cancelled", "timeout"])
def test_late_result_is_discarded_after_stop(stopped, monkeypatch):
    backend = Backend()
    clock = [0.0]
    monkeypatch.setattr(interfaces, "monotonic", lambda: clock[0])

    def stop(operation):
        if stopped == "cancelled":
            operation.cancelled.set()
        else:
            clock[0] = 2.0

    backend.callback = stop
    result = TranscriptionService(backend, "uk").run(clip(), context(deadline=1.0))
    assert not result.text and result.error_code == stopped


def test_processor_is_replaceable_and_silence_never_loads_backend():
    backend = Backend()
    assert hasattr(interfaces, "AudioProcessor")

    class Reject:
        def process(self, audio, context):
            return None

    service = TranscriptionService(backend, "uk", processor=Reject())
    assert not service.run(clip(), context()).text and backend.calls == 0


def test_progress_stream_is_bounded_drains_and_discards_old_request_events():
    from voxtypex.progress import ProgressBuffer

    stream = ProgressBuffer(capacity=4)
    first, second = context(1), context(2)
    stream.begin(first.request_id)

    def publish():
        for _ in range(100):
            stream.emit(interfaces.ProgressEvent(1, interfaces.ProgressStage.TRANSCRIBING))

    worker = Thread(target=publish)
    worker.start()
    worker.join()
    assert len(stream.drain()) == 4
    assert not stream.drain()
    stream.begin(second.request_id)
    stream.emit(interfaces.ProgressEvent(1, interfaces.ProgressStage.COMPLETED))
    stream.emit(interfaces.ProgressEvent(2, interfaces.ProgressStage.LOADING))
    assert [e.stage.value for e in stream.drain()] == ["loading"]


def test_core_pipeline_import_does_not_load_native_asr_or_numpy():
    import subprocess
    import sys

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import voxtypex.transcription, voxtypex.models; "
            "assert not {'numpy', 'faster_whisper', 'ctranslate2', 'onnxruntime'} & sys.modules.keys()",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


def test_backend_exception_does_not_log_private_input(caplog):
    backend = Backend()

    def fail(_):
        raise RuntimeError("private dictated sentence")

    backend.callback = fail
    result = TranscriptionService(backend, "uk").run(clip(), context())
    assert result.error_code == "backend" and "RuntimeError" in result.error
    assert not result.text and "private dictated sentence" not in caplog.text
