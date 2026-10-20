import json
import math
import wave
from array import array
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from voxtypex.audio import MicrophoneRecorder
from voxtypex.capture_processing import CaptureProcessor
from voxtypex.config import Settings


def test_stop_drains_callback_that_already_passed_the_acceptance_check():
    copying, resume = Event(), Event()

    class Stream:
        active = False

        def start(self):
            self.active = True

        def abort(self):
            self.active = False
            # Let the consumer observe an early stop before this callback completes.
            worker.join(0.1)
            resume.set()
            producer.join(1)

        def close(self):
            self.active = False

    stream = Stream()
    backend = SimpleNamespace(
        query_devices=lambda *args, **kwargs: [{"name": "probe", "hostapi": 0, "max_input_channels": 1}],
        query_hostapis=lambda: [{"name": "WASAPI", "default_input_device": 0}],
        RawInputStream=lambda **kwargs: stream,
    )
    recorder = MicrophoneRecorder(backend, settings=Settings(vad_enabled=False))
    held = Event()
    held.set()
    recorder.start(held)
    worker = recorder._worker

    class InflightBuffer:
        def __bytes__(self):
            copying.set()
            assert resume.wait(2)
            return b"\x01\x00" * 320

    producer = Thread(target=recorder._callback, args=(InflightBuffer(), 320, None, False))
    producer.start()
    try:
        assert copying.wait(1)
        result = recorder.stop()
        assert result.samples == 320
        assert result.pcm == b"\x01\x00" * 320
    finally:
        resume.set()
        producer.join(1)
        recorder.close()


@pytest.mark.parametrize("noise", ["tone", "fan", "white"])
@pytest.mark.parametrize("seconds", [0.4, 0.6, 1.1, 6])
@pytest.mark.parametrize("auto_stop_seconds", [0, 2])
def test_native_vad_does_not_confirm_stationary_noise_at_recording_start(noise, seconds, auto_stop_seconds):
    pytest.importorskip("webrtcvad")
    import random

    rng = random.Random(20261016)
    signal = array(
        "h",
        (
            round((250 * math.sin(2 * math.pi * 120 * index / 16000)
                   + 150 * math.sin(2 * math.pi * 240 * index / 16000) if noise != "white" else 0)
                  + (rng.gauss(0, 120 if noise == "fan" else 300) if noise != "tone" else 0))
            for index in range(round(16000 * seconds))
        ),
    ).tobytes()
    processor = CaptureProcessor(auto_stop_seconds=auto_stop_seconds)
    for start in range(0, len(signal), 640):
        processor.push(signal[start:start + 640])
    assert not processor.auto_stopped
    retained_samples = processor.finish().samples
    assert retained_samples == 0


def existing_english_pcm():
    manifest = Path(__file__).resolve().parents[1] / ".local/asr-benchmark/manifest.json"
    if not manifest.is_file():
        pytest.skip("Existing ignored FLEURS corpus is not available")
    rows = json.loads(manifest.read_text(encoding="utf-8"))["clips"]
    row = next(item for item in rows if item["id"] == "fleurs-en_us-test-1003119935936341070")
    path = Path(row.get("path") or row["audio"])
    if not path.is_file():
        pytest.skip("Existing ignored English speech sample is not available")
    with wave.open(str(path), "rb") as source:
        assert (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (16000, 1, 2)
        return source.readframes(source.getnframes())


@pytest.mark.parametrize("end_sample", [20800, 24000, 32000])
def test_native_vad_keeps_real_short_english_phrase_at_ptt_start(end_sample):
    pytest.importorskip("webrtcvad")
    # The source's first second is background; the following fragments contain speech.
    pcm = existing_english_pcm()[14400 * 2:end_sample * 2]
    processor = CaptureProcessor()
    for start in range(0, len(pcm), 640):
        processor.push(pcm[start:start + 640])
    clip = processor.finish()
    assert clip.speech_detected is True
    assert clip.pcm == pcm


def test_native_vad_keeps_low_level_confirmed_english_ptt_without_context_changes():
    pytest.importorskip("webrtcvad")
    pcm = bytes(16000 * 3 * 2) + existing_english_pcm() + bytes(16000 * 3 * 2)
    processor = CaptureProcessor()
    for start in range(0, len(pcm), 640):
        processor.push(pcm[start:start + 640])
    assert processor.finish().pcm == pcm


@pytest.mark.parametrize("pause_samples", [0, 12800])
def test_native_auto_stop_confirms_short_phrase_after_due_silence(pause_samples):
    pytest.importorskip("webrtcvad")
    phrase = existing_english_pcm()[14400 * 2:20800 * 2]
    spoken = phrase + (bytes(pause_samples * 2) + phrase if pause_samples else b"")
    pcm = spoken + bytes(16000 * 3 * 2)
    processor = CaptureProcessor(auto_stop_seconds=2)
    first_stop = None
    for start in range(0, len(pcm), 640):
        processor.push(pcm[start:start + 640])
        if processor.auto_stopped and first_stop is None:
            first_stop = (start + 640) // 2
    assert first_stop is not None
    assert len(spoken) // 2 + 16000 * 1.8 <= first_stop <= len(spoken) // 2 + 16000 * 2.2
    assert processor.finish().speech_detected is True


def test_native_short_phrase_after_long_pause_keeps_both_phrases_with_default_ptt():
    pytest.importorskip("webrtcvad")
    phrase = existing_english_pcm()[14400 * 2:20800 * 2]
    pcm = phrase + bytes(16000 * 4 * 2) + phrase
    processor = CaptureProcessor()
    for start in range(0, len(pcm), 640):
        processor.push(pcm[start:start + 640])
        assert not processor.auto_stopped
    assert processor.finish().pcm == pcm


@pytest.mark.parametrize("click_sample", [0, 80000])
@pytest.mark.parametrize("auto_stop_seconds", [0, 2])
def test_native_capture_keeps_quiet_english_after_isolated_click(click_sample, auto_stop_seconds):
    pytest.importorskip("webrtcvad")
    quiet = array("h", (round(sample * 0.05) for sample in array("h", existing_english_pcm()))).tobytes()
    click = array("h", [30000] + [0] * 319).tobytes()
    speech = quiet[:click_sample * 2] + click + quiet[click_sample * 2:]
    pcm = speech + bytes(16000 * 3 * 2)
    processor = CaptureProcessor(auto_stop_seconds=auto_stop_seconds)
    first_stop = None
    for start in range(0, len(pcm), 640):
        processor.push(pcm[start:start + 640])
        if processor.auto_stopped and first_stop is None:
            first_stop = (start + 640) // 2
    if auto_stop_seconds:
        assert first_stop is not None
        assert len(speech) // 2 + 16000 * 1.5 <= first_stop <= len(pcm) // 2
    else:
        assert first_stop is None
    assert processor.finish().pcm == pcm


@pytest.mark.parametrize("gain", [1.0, 0.05])
def test_real_english_keeps_first_and_last_phrase_boundaries(gain):
    pytest.importorskip("webrtcvad")
    quiet = array("h", (round(sample * gain) for sample in array("h", existing_english_pcm())))
    pcm = bytes(16000 * 3 * 2) + quiet.tobytes() + bytes(16000 * 3 * 2)
    processor = CaptureProcessor()
    for start in range(0, len(pcm), 640):
        processor.push(pcm[start:start + 640])
    clip = processor.finish()
    start = pcm.find(clip.pcm) // 2 if clip.pcm else -1
    # Bounds cover the first and last words of the cached reference clip.
    assert 0 <= start <= 48000
    assert start + clip.samples >= 188160
