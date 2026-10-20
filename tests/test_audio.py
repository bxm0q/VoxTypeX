from threading import Event, get_ident
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


def make_recorder(**recorder_options):
    stream = Stream()
    options = {}

    def create(**kwargs):
        options.update(kwargs)
        return stream

    backend = SimpleNamespace(
        query_devices=lambda *args, **_: [{"name": "test", "hostapi": 0, "max_input_channels": 1}],
        query_hostapis=lambda: [{"name": "WASAPI", "default_input_device": 0}],
        RawInputStream=create,
        CallbackAbort=RuntimeError,
    )
    from voxtypex.config import Settings

    return (
        MicrophoneRecorder(backend, settings=Settings(vad_enabled=False), **recorder_options),
        stream,
        options,
    )


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


def test_windows_default_wasapi_uses_shared_rate_conversion_without_device_fallback():
    recorder, _, options = make_recorder()
    recorder.backend.WasapiSettings = lambda **kwargs: kwargs
    held = Event()
    held.set()
    try:
        recorder.start(held)
        assert options['device'] == 0 and options['samplerate'] == 16000
        assert options['extra_settings'] == {'auto_convert': True}
    finally:
        recorder.close()


def test_dsp_preparation_never_opens_or_queries_a_microphone():
    recorder, _, options = make_recorder()
    def forbidden(*args, **kwargs):
        raise RuntimeError('Preparation touched a microphone')
    recorder.backend.query_devices = forbidden
    recorder.backend.RawInputStream = forbidden
    recorder.prepare()
    assert recorder.stream is None and not options and recorder.samples == 0


@pytest.mark.parametrize("test_first", [False, True])
def test_ptt_and_test_share_default_pin_until_explicit_reset(test_first):
    from voxtypex.config import Settings
    from voxtypex.microphones import DefaultInputBinding

    host = {"name": "WASAPI", "default_input_device": 0}
    devices = [{"name": name, "hostapi": 0, "max_input_channels": 1} for name in ("A", "B")]
    opened = []

    def create(**kwargs):
        opened.append(kwargs["device"])
        return Stream()

    backend = SimpleNamespace(
        query_hostapis=lambda: [host], query_devices=lambda: devices, RawInputStream=create
    )
    binding = DefaultInputBinding()
    ptt, test = [MicrophoneRecorder(backend, settings=Settings(vad_enabled=False), default_binding=binding)
                 for _ in range(2)]
    first, second = (test, ptt) if test_first else (ptt, test)
    held = Event()
    held.set()
    try:
        first.start(held)
        first.stop()
        host["default_input_device"] = 1
        second.start(held)
        second.stop()
        assert opened == [0, 0]
        devices[0] = {"name": "Other", "hostapi": 0, "max_input_channels": 1}
        with pytest.raises(RuntimeError, match="недоступен"):
            second.start(held)
        assert opened == [0, 0]
        ptt.reset_default_device()
        second.start(held)
        second.stop()
        assert opened == [0, 0, 1]
    finally:
        ptt.close()
        test.close()


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
        recorder._callback(b"\0\0" * 320, 320, None, failure == "overflow")
    with pytest.raises(RuntimeError):
        recorder.stop()
    assert stream.closed


def test_callback_never_waits_for_dsp_and_bounded_overflow_fails_closed():
    from voxtypex.capture_processing import CaptureProcessor

    entered, release = Event(), Event()
    owner = get_ident()
    threads = []

    class Slow(CaptureProcessor):
        def push(self, pcm):
            threads.append(get_ident())
            entered.set()
            assert release.wait(2)
            super().push(pcm)

    recorder, stream, _ = make_recorder(processor_factory=lambda _: Slow(vad_enabled=False), queue_capacity=4)
    held = Event()
    held.set()
    recorder.start(held)
    try:
        recorder._callback(b"\1\0" * 320, 320, None, False)
        assert entered.wait(1)
        started = monotonic()
        for _ in range(100):
            recorder._callback(b"\2\0" * 320, 320, None, False)
        assert monotonic() - started < 0.1
        assert recorder.diagnostics.queue_overflows > 0
        assert threads and all(thread != owner for thread in threads)
        with pytest.raises(RuntimeError, match="buffer"):
            recorder.check_health()
    finally:
        release.set()
        recorder.close()
    assert stream.closed


def test_status_underflow_and_overflow_are_counted_without_callback_exceptions():
    recorder, _, _ = make_recorder()
    held = Event()
    held.set()
    recorder.start(held)

    class Status:
        input_overflow = True
        input_underflow = True

    recorder._callback(b"\0\0" * 320, 320, None, Status())
    assert recorder.diagnostics.input_overflows == 1
    assert recorder.diagnostics.input_underflows == 1
    with pytest.raises(RuntimeError):
        recorder.stop()


def test_default_device_is_pinned_after_unplug_until_explicit_settings_change():
    from dataclasses import replace

    from voxtypex.config import Settings

    recorder, _, _ = make_recorder()
    inventory = [{"name": "test", "hostapi": 0, "max_input_channels": 1}]
    recorder.backend.query_devices = lambda: inventory
    held = Event()
    held.set()
    recorder.start(held)
    recorder.stop()
    inventory[:] = [{"name": "other", "hostapi": 0, "max_input_channels": 1}]
    with pytest.raises(RuntimeError, match="микрофон"):
        recorder.start(held)
    recorder.configure(replace(Settings(), vad_enabled=False))
    with pytest.raises(RuntimeError, match='микрофон'):
        recorder.start(held)
    recorder.reset_default_device()
    recorder.start(held)
    recorder.stop()


def test_stop_drains_callback_already_in_flight_before_producer_barrier():
    from threading import Thread

    from voxtypex.capture_processing import CaptureProcessor

    copying, resume, finished = Event(), Event(), Event()
    class Processor(CaptureProcessor):
        def finish(self):
            finished.set()
            return super().finish()
    recorder, stream, _ = make_recorder(processor_factory=lambda _: Processor(vad_enabled=False))
    held = Event()
    held.set()
    recorder.start(held)
    class PCM:
        def __bytes__(self):
            copying.set()
            assert resume.wait(2)
            return b'\x0a\x00' * 320
    producer = Thread(target=lambda: recorder._callback(PCM(), 320, None, False))
    producer.start()
    assert copying.wait(1)
    def abort():
        # Expose consumer shutdown before the in-flight callback finishes.
        finished.wait(0.05)
        resume.set()
        producer.join(1)
        assert not producer.is_alive()
        stream.active = False
    stream.abort = abort
    clip = recorder.stop()
    assert clip.pcm == b'\x0a\x00' * 320


def test_processing_failure_is_observable_and_next_capture_works():
    from time import sleep

    from voxtypex.capture_processing import CaptureProcessor

    class Broken(CaptureProcessor):
        def push(self, pcm):
            raise RuntimeError("private PCM should not appear in diagnostics")

    recorder, _, _ = make_recorder(processor_factory=lambda _: Broken(vad_enabled=False))
    held = Event()
    held.set()
    recorder.start(held)
    recorder._callback(b"\1\0" * 320, 320, None, False)
    deadline = monotonic() + 2
    while not recorder.fault:
        assert monotonic() < deadline
        sleep(0.001)
    with pytest.raises(RuntimeError, match="processing"):
        recorder.stop()
    assert "private PCM" not in recorder.fault
    recorder.processor_factory = lambda _: CaptureProcessor(vad_enabled=False)
    recorder.start(held)
    recorder._callback(b"\1\0" * 320, 320, None, False)
    assert recorder.stop().pcm == b"\1\0" * 320
