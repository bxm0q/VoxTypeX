import json
import subprocess
import sys
from array import array

import pytest

from voxtypex.denoising import Denoiser, DenoisingError


def native_runtime():
    return pytest.importorskip("pywebrtc_audio")


def test_off_is_bit_exact_and_does_not_load_dsp_dependencies():
    probe = """
import json, sys
from voxtypex.denoising import Denoiser
processor = Denoiser('off')
pcm = bytes(range(256)) * 3
output = processor.process(pcm[:42]) + processor.process(pcm[42:]) + processor.flush()
print(json.dumps({'exact': output == pcm, 'loaded': [name for name in ('numpy', 'pywebrtc_audio') if name in sys.modules]}))
"""
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"exact": True, "loaded": []}


@pytest.mark.parametrize("mode", ["moderate", "strong"])
@pytest.mark.parametrize("length", [1, 63, 96, 159, 160, 161, 319, 320, 321, 16037])
def test_flush_preserves_exact_sample_count_with_partial_frames(mode, length):
    native_runtime()
    pcm = array("h", ((index * 137) % 12000 - 6000 for index in range(length))).tobytes()
    processor = Denoiser(mode)
    output = b"".join(processor.process(pcm[start : start + 74]) for start in range(0, len(pcm), 74))
    output += processor.flush()
    assert len(output) == len(pcm)
    assert processor.flush() == b""
    processor.close()
    processor.close()


@pytest.mark.parametrize("mode", ["moderate", "strong"])
def test_arbitrary_chunking_does_not_insert_padding_between_frames(mode):
    native_runtime()
    pcm = array("h", ((index * 137) % 12000 - 6000 for index in range(3501))).tobytes()
    whole = Denoiser(mode)
    expected = whole.process(pcm) + whole.flush()
    chunked = Denoiser(mode)
    actual = b"".join(chunked.process(pcm[start : start + 42]) for start in range(0, len(pcm), 42))
    actual += chunked.flush()
    assert actual == expected


@pytest.mark.parametrize("mode", ["moderate", "strong"])
@pytest.mark.parametrize("position", [0, 1, 158, 159, 160, 319, 998, 999])
def test_first_and_last_impulses_are_aligned_and_retained(mode, position):
    native_runtime()
    pcm = array("h", [0] * 1000)
    pcm[position] = 20000
    processor = Denoiser(mode)
    output = array("h", processor.process(pcm.tobytes()) + processor.flush())
    assert len(output) == len(pcm)
    assert max(range(len(output)), key=lambda index: abs(output[index])) == position
    assert abs(output[position]) > 100


@pytest.mark.parametrize("mode", ["moderate", "strong"])
def test_new_recording_has_fresh_noise_estimator(mode):
    native_runtime()
    pcm = array("h", ((index * 7919) % 16000 - 8000 for index in range(5000))).tobytes()
    first, second = Denoiser(mode), Denoiser(mode)
    assert first.process(pcm) + first.flush() == second.process(pcm) + second.flush()


def test_real_noise_suppression_is_stronger_in_strong_mode():
    native_runtime()
    import numpy as np

    samples = np.random.default_rng(20261016).integers(-1800, 1800, 32000, dtype=np.int16)
    rms = {}
    for mode in ("off", "moderate", "strong"):
        processor = Denoiser(mode)
        output = processor.process(samples.tobytes()) + processor.flush()
        rms[mode] = np.sqrt(np.mean(np.frombuffer(output, dtype="<i2")[8000:].astype(float) ** 2))
    assert rms["moderate"] < rms["off"] * 0.5
    assert rms["strong"] < rms["moderate"] * 0.7


def test_int16_native_output_saturates_instead_of_wrapping():
    runtime = native_runtime()
    import numpy as np

    # Use the float output to observe overshoot before native int16 saturation.
    samples = np.random.default_rng(20261016).choice([-32768, 32767], 3200).astype(np.int16)
    floating = runtime.NoiseSuppressor(level=1).process(samples.astype(np.float32) / 32767)
    expected = np.rint(np.clip(floating * 32767, -32768, 32767)).astype(np.int16)
    direct = runtime.NoiseSuppressor(level=1).process(samples)
    assert np.max(np.abs(expected.astype(int) - direct.astype(int))) <= 1
    processor = Denoiser("moderate")
    output = np.frombuffer(processor.process(samples.tobytes()) + processor.flush(), dtype="<i2")
    assert np.array_equal(output[:-96], direct[96:])


def test_enabled_mode_import_is_lazy_and_missing_runtime_is_explicit():
    probe = """
import importlib.abc, sys
from voxtypex.denoising import Denoiser, DenoisingError
class BlockNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'pywebrtc_audio':
            raise ImportError('native runtime intentionally unavailable')
sys.meta_path.insert(0, BlockNative())
processor = Denoiser('moderate')
assert 'pywebrtc_audio' not in sys.modules
try:
    processor.process(bytes(320))
except DenoisingError as error:
    assert 'pywebrtc-audio' in str(error)
else:
    raise AssertionError('Missing runtime silently bypassed denoising')
try:
    processor.process(b'')
except DenoisingError:
    pass
else:
    raise AssertionError('A failed stream could be reused with lost frames')
"""
    subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)


@pytest.mark.parametrize("mode", ["", "invalid", None])
def test_invalid_mode_is_rejected(mode):
    with pytest.raises(ValueError):
        Denoiser(mode)


def test_invalid_pcm_is_rejected_before_native_loading():
    processor = Denoiser()
    with pytest.raises(ValueError, match="PCM16"):
        processor.process(b"\x01")


def test_closed_or_flushed_stream_cannot_accept_a_new_recording():
    processor = Denoiser("off")
    assert processor.process(b"\x01\x00") == b"\x01\x00"
    processor.flush()
    with pytest.raises(DenoisingError):
        processor.process(b"\x02\x00")
    processor.close()
    with pytest.raises(DenoisingError):
        processor.process(b"\x03\x00")


def test_empty_recording_does_not_load_native_runtime():
    processor = Denoiser()
    assert processor.process(b"") == b""
    assert processor.flush() == b""
