from array import array

import pytest

from voxtypex.capture_processing import CaptureProcessor


def frame(value=0):
    return array("h", [value, -value] * 160).tobytes()


class MarkedSpeech:
    def is_speech(self, pcm, rate):
        assert rate == 16000 and len(pcm) == 640
        return max(array("h", pcm)) > 100


def feed(processor, frames):
    for item in frames:
        processor.push(item)
    return processor.finish()


def test_no_speech_or_single_impulse_never_reaches_asr():
    for source in ([frame()] * 100, [frame()] * 30 + [frame(1000)] + [frame()] * 50):
        processor = CaptureProcessor(speech_detector=MarkedSpeech(), presence_detector=MarkedSpeech())
        assert feed(processor, source).pcm == b""


def test_preroll_keeps_first_word_and_hangover_keeps_ending():
    source = [frame(10)] * 50 + [frame(800)] * 10 + [frame(20)] * 50
    processor = CaptureProcessor(speech_detector=MarkedSpeech(), presence_detector=MarkedSpeech())
    clip = feed(processor, source)
    assert clip.pcm == b"".join(source[35:85])
    assert clip.sample_rate == 16000 and clip.channels == 1 and clip.sample_width == 2


def test_pause_and_late_speech_are_kept_without_splicing_words():
    source = [frame()] * 150 + [frame(800)] * 5 + [frame()] * 200 + [frame(900)] * 5
    processor = CaptureProcessor(speech_detector=MarkedSpeech(), presence_detector=MarkedSpeech())
    clip = feed(processor, source)
    assert clip.pcm == b"".join(source[135:])


def test_very_short_phrase_at_ptt_start_is_preserved():
    source = [frame(800)] * 3 + [frame()] * 10
    processor = CaptureProcessor(speech_detector=MarkedSpeech(), presence_detector=MarkedSpeech())
    assert feed(processor, source).pcm == b"".join(source)


def test_auto_stop_waits_for_speech_and_ignores_short_pause():
    processor = CaptureProcessor(
        speech_detector=MarkedSpeech(), presence_detector=MarkedSpeech(), auto_stop_seconds=2
    )
    for _ in range(200):
        processor.push(frame())
    assert not processor.auto_stopped
    for _ in range(4):
        processor.push(frame(1000))
    for _ in range(40):
        processor.push(frame())
    assert not processor.auto_stopped
    for _ in range(3):
        processor.push(frame(900))
    for _ in range(100):
        processor.push(frame())
    assert processor.auto_stopped


def test_full_bypass_is_bit_exact_even_for_partial_frames():
    source = array("h", [0, -32768, 32767, 123, -532] * 123).tobytes()
    processor = CaptureProcessor(vad_enabled=False, noise_mode="off")
    for start in range(0, len(source), 14):
        processor.push(source[start : start + 14])
    assert processor.finish().pcm == source


def test_partial_final_frame_does_not_drop_end_speech():
    source = frame(900) * 4 + array("h", [901] * 111).tobytes()
    processor = CaptureProcessor(speech_detector=MarkedSpeech(), presence_detector=MarkedSpeech())
    processor.push(source)
    assert processor.finish().pcm == source


def test_clipping_diagnostics_do_not_wrap_or_change_bypass_audio():
    source = array("h", [32767, -32768, 100, 0] * 80).tobytes()
    processor = CaptureProcessor(vad_enabled=False)
    processor.push(source)
    assert processor.diagnostics.clipped_samples == 160
    assert processor.diagnostics.peak_dbfs == 0
    assert processor.finish().pcm == source


def test_raw_and_processed_stay_on_same_timeline():
    class DelayedDenoiser:
        def __init__(self, mode):
            self.pending = b""

        def process(self, pcm):
            output, self.pending = self.pending, bytes(len(pcm))
            return output

        def flush(self):
            return self.pending

        def close(self):
            pass

    processor = CaptureProcessor(
        speech_detector=MarkedSpeech(), presence_detector=MarkedSpeech(),
        noise_mode="moderate", denoiser_factory=DelayedDenoiser
    )
    source = [frame()] * 50 + [frame(800)] * 4 + [frame()] * 50
    clip = feed(processor, source)
    assert len(clip.pcm) == 640 * 44 and not any(clip.pcm)


@pytest.mark.parametrize("noise", ["silence", "dc", "impulse"])
def test_real_webrtc_rejects_non_speech_signals(noise):
    processor = CaptureProcessor()
    source = [array("h", [30] * 320).tobytes() if noise == "dc" else frame()] * 100
    if noise == "impulse":
        source[40] = array("h", [30000] + [0] * 319).tobytes()
    assert not feed(processor, source).pcm
