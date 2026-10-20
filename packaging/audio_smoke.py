import importlib.metadata
import json
import traceback
import wave
from array import array
from pathlib import Path
from threading import Event
from time import sleep


def run(directory, speech_wav):
    from voxtypex.audio import MicrophoneRecorder
    from voxtypex.capture_processing import CaptureProcessor
    from voxtypex.config import Settings
    from voxtypex.denoising import Denoiser

    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    report = {"checks": [], "errors": [], "metrics": {}}
    stage = "format"
    try:
        with wave.open(str(speech_wav), "rb") as source:
            assert (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (16000, 1, 2)
            pcm = source.readframes(source.getnframes())
        stage = "bundled_audio_runtimes_and_licenses"
        license_text = importlib.metadata.distribution("pywebrtc-audio").read_text("licenses/LICENSE")
        assert license_text and len(license_text) > 15000
        assert importlib.metadata.version("webrtcvad-wheels") == "2.0.14.post1"
        report["checks"].append(stage)
        stage = "portaudio_pcm16_worker_close_and_reopen"
        recorder = MicrophoneRecorder(settings=Settings(vad_enabled=False))
        try:
            counts = []
            for _ in range(2):
                held = Event()
                held.set()
                recorder.start(held)
                sleep(0.4)
                recorder.check_health()
                held.clear()
                clip = recorder.stop()
                assert clip.samples > 3200 and (clip.sample_rate, clip.channels, clip.sample_width) == (
                    16000,
                    1,
                    2,
                )
                assert recorder.diagnostics.queue_overflows == recorder.diagnostics.input_overflows == 0
                counts.append(clip.samples)
            report["metrics"]["microphone_samples"] = counts
        finally:
            recorder.close()
        report["checks"].append(stage)
        stage = "off_is_bit_exact"
        processor = CaptureProcessor(vad_enabled=False, noise_mode="off")
        for offset in range(0, len(pcm), 640):
            processor.push(pcm[offset : offset + 640])
        assert processor.finish().pcm == pcm
        report["checks"].append(stage)
        stage = "real_native_moderate_strong_pcm16_and_tail"
        for mode in ("moderate", "strong"):
            processor = Denoiser(mode)
            output = bytearray()
            try:
                for offset in range(0, len(pcm), 640):
                    output.extend(processor.process(pcm[offset : offset + 640]))
                output.extend(processor.flush())
                assert len(output) == len(pcm)
                report["metrics"][mode] = {"samples": len(output) // 2, "compensated_delay_ms": 6}
            finally:
                processor.close()
        report["checks"].append(stage)
        stage = "real_speech_vad_and_quiet_voice"
        for gain in (1.0, 0.05):
            source = array("h", (round(v * gain) for v in array("h", pcm)))
            processor = CaptureProcessor()
            signal = bytes(16000 * 2 * 3) + source.tobytes() + bytes(16000 * 2 * 3)
            for offset in range(0, len(signal), 640):
                processor.push(signal[offset : offset + 640])
            clip = processor.finish()
            assert clip.speech_detected is True and clip.samples >= len(source) * 0.8
            report["metrics"][f"vad_gain_{gain}"] = {"samples": clip.samples, "source_samples": len(source)}
        report["checks"].append(stage)
        stage = "digital_silence_and_single_impulse_rejected"
        for impulse in (False, True):
            processor = CaptureProcessor()
            signal = bytearray(16000 * 2 * 3)
            if impulse:
                signal[16000:16002] = (30000).to_bytes(2, "little", signed=True)
            for offset in range(0, len(signal), 640):
                processor.push(signal[offset : offset + 640])
            assert not processor.finish().pcm
        report["checks"].append(stage)
    except Exception as exc:
        report["errors"].append(
            {
                "stage": stage,
                "type": type(exc).__name__,
                "locations": [
                    f"{Path(f.filename).name}:{f.lineno}" for f in traceback.extract_tb(exc.__traceback__)
                ],
            }
        )
    report["exit_code"] = int(bool(report["errors"]))
    (directory / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report["exit_code"]
