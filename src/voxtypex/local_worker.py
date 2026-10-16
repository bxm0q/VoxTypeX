"""Native SDK, VAD and audio buffers belong exclusively to the spawned worker."""

import os
import sys
import traceback
from pathlib import Path

from .interfaces import TranscriptionResult


def native_directory():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "transcribe_native"
    return Path(__file__).resolve().parents[2] / ".local/native/transcribe-cpp"


def bounded_segments(spans, maximum_samples):
    if maximum_samples <= 0:
        raise ValueError("Invalid segment limit")
    result = []
    previous = 0
    for span in spans:
        start, end = int(span["start"]), int(span["end"])
        if start < previous or end <= start:
            raise ValueError("Invalid or overlapping VAD span")
        while start < end:
            boundary = min(end, start + maximum_samples)
            result.append((start, boundary))
            start = boundary
        previous = end
    return result


class LocalRuntime:
    def __init__(self, cache_dir, model_id, report):
        from .local_models import LanguageModelManager, LanguageModelRegistry

        self.spec = LanguageModelRegistry().get(model_id)
        self.manager = LanguageModelManager(cache_dir)
        self.report = report
        self.model = self.session = None
        self._dll_directory = None
        self.max_seconds = min(20.0, self.spec.max_audio_seconds or 20.0)

    def _load(self):
        source = self.manager.resolve(self.spec.id)
        self.report("phase", "loading")
        directory = native_directory()
        library = directory / "transcribe.dll"
        if not library.is_file():
            raise FileNotFoundError("Build the pinned native CPU runtime first")
        os.environ["TRANSCRIBE_LIBRARY"] = str(library)
        self._dll_directory = os.add_dll_directory(str(directory)) if os.name == "nt" else None
        import transcribe_cpp as tc

        # Native callback strings may contain inference content. Keep native status
        # exceptions and stack locations, never persist arbitrary native messages.
        tc.set_log_callback(None)
        self.model = tc.Model(source, backend="cpu")
        self.session = self.model.session(n_threads=min(4, os.cpu_count() or 1))
        limit = self.session.limits.effective_max_audio_ms
        if limit > 0:
            self.max_seconds = min(self.max_seconds, limit / 1000)
        self.report("device", "cpu")
        self.report("status", f"Local ASR ready: {self.spec.id}, CPU")

    def _chunk(self, samples, language, depth=0):
        import transcribe_cpp as tc

        try:
            return self.session.run(samples, language=language, timestamps="none").text.strip()
        except tc.OutputTruncated:
            # Discard the incomplete hypothesis; retranscribe disjoint halves.
            # No partial prefix can be inserted or duplicated.
            if depth >= 4 or len(samples) < 16000:
                raise
            middle = len(samples) // 2
            return " ".join(
                filter(
                    None,
                    (
                        self._chunk(samples[:middle], language, depth + 1),
                        self._chunk(samples[middle:], language, depth + 1),
                    ),
                )
            )

    def transcribe(self, audio, language):
        if language != self.spec.language:
            raise ValueError("Local model does not match the selected language")
        if (audio.sample_rate, audio.channels, audio.sample_width) != (16000, 1, 2) or len(audio.pcm) % 2:
            raise ValueError("Expected 16 kHz mono PCM16")
        if self.model is None:
            self._load()
        import numpy as np
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        samples = np.frombuffer(audio.pcm, dtype="<i2").astype(np.float32) / 32768
        self.report("phase", "transcribing")
        spans = get_speech_timestamps(
            samples,
            VadOptions(
                min_speech_duration_ms=150,
                min_silence_duration_ms=300,
                max_speech_duration_s=max(0.5, self.max_seconds - 0.4),
                speech_pad_ms=200,
            ),
        )
        chunks = bounded_segments(spans, max(1, int(self.max_seconds * 16000)))
        text = " ".join(filter(None, (self._chunk(samples[start:end], language) for start, end in chunks)))
        return TranscriptionResult(text=text, language=language)

    def close(self):
        if self.session is not None:
            self.session.close()
        if self.model is not None:
            self.model.close()
        if self._dll_directory is not None:
            self._dll_directory.close()


def run_local_worker(connection, cache_dir, model_id):
    request_id = 0
    runtime = None
    try:
        runtime = LocalRuntime(
            cache_dir, model_id, lambda kind, value: connection.send((request_id, kind, value))
        )
        while True:
            request_id, audio, language = connection.recv()
            try:
                result = runtime.transcribe(audio, language)
            except Exception as exc:
                connection.send(
                    (
                        request_id,
                        "error",
                        "".join(traceback.format_tb(exc.__traceback__)) + type(exc).__name__,
                    )
                )
                result = TranscriptionResult(
                    error=f"Local ASR failed ({type(exc).__name__})", error_code="local-asr"
                )
            connection.send((request_id, "result", result))
            del audio, result
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:
        try:
            connection.send(
                (request_id, "error", "".join(traceback.format_tb(exc.__traceback__)) + type(exc).__name__)
            )
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        if runtime is not None:
            runtime.close()
        connection.close()
