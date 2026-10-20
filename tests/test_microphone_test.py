from dataclasses import replace
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from voxtypex.config import Settings
from voxtypex.interfaces import AudioClip
from voxtypex.ui.microphone_panel import MicrophoneTestSession


def wait_until(predicate):
    deadline = monotonic() + 3
    while not predicate() and monotonic() < deadline:
        sleep(0.01)
    assert predicate()


class FakeRecorder:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = Event()
        self.closed = Event()
        self.diagnostics = SimpleNamespace(
            rms_dbfs=-24.0, peak_dbfs=-3.0, clipped_samples=2, latency_ms=20.0, processing_ms=1.0
        )
        type(self).instances.append(self)

    def start(self, held):
        self.held = held
        self.started.set()

    def check_health(self):
        pass

    def stop(self):
        self.close()
        return AudioClip(b"\x01\x00" * 320, 16000)

    def close(self):
        self.closed.set()


def test_session_starts_only_on_explicit_action_and_records_raw_for_comparison():
    FakeRecorder.instances = []
    played = []
    session = MicrophoneTestSession(
        recorder_factory=FakeRecorder, player=lambda clip, cancel: played.append(clip), duration=0.08
    )
    assert not session.busy and not FakeRecorder.instances
    settings = Settings(noise_suppression="off", vad_enabled=True)
    assert session.start(settings)
    wait_until(lambda: not session.busy)
    recorder = FakeRecorder.instances[0]
    assert recorder.kwargs["settings"].noise_suppression == "off"
    assert not recorder.kwargs["settings"].vad_enabled
    assert played == [] and session.has_clip
    assert session.play("raw")
    wait_until(lambda: not session.busy)
    assert len(played) == 1 and played[0].pcm == b"\x01\x00" * 320
    session.dismiss()
    assert not session.has_clip


def test_session_passes_shared_default_binding_to_its_recorder():
    binding = object()
    session = MicrophoneTestSession(recorder_factory=FakeRecorder, default_binding=binding, duration=0.05)
    assert session.start(Settings())
    wait_until(lambda: not session.busy)
    assert FakeRecorder.instances[-1].kwargs["default_binding"] is binding
    session.dismiss()


def test_hide_cancels_test_clears_held_and_never_retains_or_plays_audio():
    FakeRecorder.instances = []
    session = MicrophoneTestSession(recorder_factory=FakeRecorder, duration=3)
    assert session.start(Settings())
    wait_until(lambda: bool(FakeRecorder.instances) and FakeRecorder.instances[0].started.is_set())
    assert not session.start(Settings())
    session.dismiss()
    wait_until(lambda: not session.busy)
    recorder = FakeRecorder.instances[0]
    assert recorder.closed.is_set() and not recorder.held.is_set()
    assert not session.has_clip and not session.play("raw")


def test_test_failure_is_metadata_and_closes_recorder():
    class Disconnected(FakeRecorder):
        def check_health(self):
            raise RuntimeError("Микрофон отключён")

    session = MicrophoneTestSession(recorder_factory=Disconnected, duration=0.1)
    assert session.start(Settings())
    wait_until(lambda: not session.busy)
    snapshot = session.poll()
    assert "отключён" in snapshot.message
    assert not session.has_clip and FakeRecorder.instances[-1].closed.is_set()


def test_processed_comparison_uses_same_capture_without_second_recording():
    FakeRecorder.instances = []
    processed = []

    def denoise(clip, mode):
        processed.append((clip, mode))
        return replace(clip, pcm=b"\x02\x00" * clip.samples)

    played = []
    session = MicrophoneTestSession(
        recorder_factory=FakeRecorder,
        processor=denoise,
        player=lambda clip, cancel: played.append(clip),
        duration=0.06,
    )
    assert session.start(Settings(noise_suppression="moderate"))
    wait_until(lambda: not session.busy)
    assert len(FakeRecorder.instances) == 1 and len(processed) == 1
    assert processed[0][1] == "moderate"
    assert session.poll().comparison_ms is not None and session.poll().comparison_ms >= 0
    assert session.play("processed")
    wait_until(lambda: not session.busy)
    assert played[0].pcm == b"\x02\x00" * 320


def test_playback_close_failure_keeps_refresh_guard(monkeypatch):
    import sys

    import voxtypex.microphones as microphones
    from voxtypex.ui.microphone_panel import _play

    monkeypatch.setattr(microphones, "_leases", 0)
    calls = []

    class Output:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

        def start(self):
            pass

        def write(self, _):
            pass

        def abort(self):
            pass

        def stop(self):
            pass

        def close(self):
            raise RuntimeError("Driver still owns stream")

    backend = SimpleNamespace(
        RawOutputStream=lambda **kwargs: Output(),
        _terminate=lambda: calls.append("terminate"),
        _initialize=lambda: calls.append("initialize"),
        query_hostapis=lambda: [],
        query_devices=lambda: [],
    )
    monkeypatch.setitem(sys.modules, "sounddevice", backend)
    with pytest.raises(RuntimeError, match="Driver"):
        _play(AudioClip(b"\0\0" * 320, 16000), Event())
    microphones.input_devices(backend, refresh=True)
    assert calls == []


def test_processed_peak_clipping_and_compensated_latency_are_reported_separately():
    def process(clip, mode):
        return replace(clip, pcm=b"\x00\x80\xff\x7f" + b"\0\0" * (clip.samples - 2))

    session = MicrophoneTestSession(recorder_factory=FakeRecorder, processor=process, duration=0.06)
    assert session.start(Settings(noise_suppression="moderate"))
    wait_until(lambda: not session.busy)
    snapshot = session.poll()
    assert snapshot.processed_peak_dbfs == 0.0
    assert snapshot.processed_clipped_samples == 2
    assert snapshot.comparison_latency_ms == 6.0
    assert snapshot.raw_peak_dbfs == pytest.approx(-90.30899869919436)
    assert snapshot.raw_clipped_samples == 0
    assert snapshot.diagnostics.peak_dbfs == -3.0
