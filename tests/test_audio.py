from threading import Event
from time import monotonic
from types import SimpleNamespace

import pytest

from voxtypex.audio import MAX_SECONDS, SAMPLE_RATE, MicrophoneRecorder


class Stream:
    def __init__(self):
        self.active = False
        self.closed = False

    def start(self):
        self.active = True

    def abort(self):
        self.active = False

    def close(self):
        self.closed = True
        self.active = False


def make_recorder():
    stream = Stream()
    options = {}

    def create(**kwargs):
        options.update(kwargs)
        return stream

    backend = SimpleNamespace(
        query_devices=lambda **_: {"name": "test"}, RawInputStream=create, CallbackAbort=RuntimeError
    )
    return MicrophoneRecorder(backend), stream, options


def test_pcm_format_release_gate_and_cleanup():
    recorder, stream, options = make_recorder()
    held = Event()
    held.set()
    recorder.start(held)
    assert options["samplerate"] == 16000
    assert options["channels"] == 1
    assert options["dtype"] == "int16"
    recorder._callback(b"\x01\x00" * 320, 320, None, False)
    held.clear()
    recorder._callback(b"\x02\x00" * 320, 320, None, False)
    clip = recorder.stop()
    assert clip.samples == 320 and clip.duration == 0.02
    assert clip.pcm == b"\x01\x00" * 320
    assert stream.closed and recorder.stream is None


@pytest.mark.parametrize("failure", ["permission denied", "device busy", "no input device"])
def test_open_failure_releases_device(failure):
    recorder, stream, _ = make_recorder()

    def fail():
        raise OSError(failure)

    stream.start = fail
    with pytest.raises(OSError, match=failure):
        recorder.start(Event())
    assert stream.closed and recorder.stream is None


@pytest.mark.parametrize("failure", ["inactive", "no_callbacks", "overflow", "limit"])
def test_stream_faults(failure):
    recorder, stream, _ = make_recorder()
    held = Event()
    held.set()
    recorder.start(held)
    if failure == "inactive":
        stream.active = False
    elif failure == "no_callbacks":
        recorder.last_data = monotonic() - 3
    else:
        if failure == "limit":
            recorder.samples = SAMPLE_RATE * MAX_SECONDS
        with pytest.raises(RuntimeError):
            recorder._callback(b"\0\0" * 320, 320, None, failure == "overflow")
    with pytest.raises(RuntimeError):
        recorder.stop()
    assert stream.closed
