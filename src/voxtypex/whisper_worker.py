import gc
import os
import traceback
from dataclasses import replace
from time import process_time

from .interfaces import TranscriptionResult
from .models import WhisperModelManager


class WhisperRuntime:
    def __init__(self, cache_dir, model, model_path, force_cpu, report):
        self.cache_dir = cache_dir
        self.model_name = model
        self.model_path = model_path
        self.force_cpu = force_cpu
        self.report = report
        self.model = None
        self.source = None
        self.device = "cpu"
        self.models = WhisperModelManager(cache_dir)

    def _resolve(self):
        if self.source is not None:
            return self.source
        self.source = self.models.resolve(
            self.model_name,
            self.model_path,
            progress=lambda event: self.report("phase", event.stage.value),
        )
        return self.source

    def _load(self):
        import ctranslate2
        from faster_whisper import WhisperModel

        source = self._resolve()
        self.report("phase", "loading")
        self.device = "cpu"
        compute = "int8" if "int8" in ctranslate2.get_supported_compute_types("cpu") else "float32"
        if not self.force_cpu:
            try:
                if ctranslate2.get_cuda_device_count() > 0:
                    supported = ctranslate2.get_supported_compute_types("cuda")
                    for candidate in ("int8_float16", "float16", "float32"):
                        if candidate in supported:
                            self.device, compute = "cuda", candidate
                            break
            except Exception as exc:
                self.report("warning", f"CUDA unavailable; using CPU ({type(exc).__name__})")
        self.report("device", self.device)
        self.report("status", f"Loading Whisper {self.model_name}: {self.device}/{compute}")
        self.model = WhisperModel(
            source,
            device=self.device,
            compute_type=compute,
            cpu_threads=min(4, os.cpu_count() or 1),
            num_workers=1,
            local_files_only=True,
        )
        self.report("status", "Model ready (reused for subsequent recordings)")

    def _infer(self, audio, language):
        import numpy as np

        if self.model is None:
            self._load()
        self.report("phase", "transcribing")
        samples = np.frombuffer(audio.pcm, dtype="<i2").astype(np.float32) / 32768.0
        segments, info = self.model.transcribe(
            samples,
            language=language,
            task="transcribe",
            beam_size=3,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters={
                "min_speech_duration_ms": 150,
                "min_silence_duration_ms": 500,
                "speech_pad_ms": 200,
            },
        )
        # Генератор тоже может вызвать ошибку CUDA.
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return TranscriptionResult(text=text, language=info.language)

    def transcribe(self, audio, language):
        try:
            return self._infer(audio, language)
        except Exception as exc:
            if self.device != "cuda":
                raise
            self.report("warning", f"CUDA failed; retrying once on CPU ({type(exc).__name__})")
            self.report("fallback", "cpu")
            self.model = None
            gc.collect()
            self.force_cpu = True
            return self._infer(audio, language)


def run_worker(connection, cache_dir, model, model_path, force_cpu):
    os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
    os.environ.setdefault("KMP_BLOCKTIME", "0")
    request_id = 0
    runtime = WhisperRuntime(
        cache_dir,
        model,
        model_path,
        force_cpu,
        lambda kind, message: connection.send((request_id, kind, message)),
    )
    try:
        while True:
            request_id, audio, language = connection.recv()
            cpu_started = process_time()
            try:
                result = runtime.transcribe(audio, language)
            except Exception as exc:
                error_code = "model" if runtime.model is None else "transcription"
                connection.send(
                    (
                        request_id,
                        "error",
                        "".join(traceback.format_tb(exc.__traceback__)) + type(exc).__name__,
                    )
                )
                message = type(exc).__name__
                if isinstance(exc, MemoryError) or any(
                    t in str(exc).lower() for t in ("memory", "bad_alloc", "allocat")
                ):
                    message += "; недостаточно памяти: закройте другие приложения или выберите base/tiny"
                    runtime.model = None
                    gc.collect()
                else:
                    message += (
                        "; проверьте кэш модели, свободное место и доступ к интернету при первом запуске"
                    )
                result = TranscriptionResult(error=message, error_code=error_code)
            connection.send((request_id, "result", replace(result, cpu_time=process_time() - cpu_started)))
            del audio, result
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()
