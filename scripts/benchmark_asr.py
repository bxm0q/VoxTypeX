"""Opt-in real-speech evaluation. No downloads, hypotheses, or user audio in reports."""
import argparse
import csv
import hashlib
import importlib.metadata
import json
import logging
import os
import platform
import subprocess
import threading
import time
import traceback
import unicodedata
import wave
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


def normalize_text(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    apostrophes = "'’‘ʼ＇"
    return " ".join(
        "".join(
            char if unicodedata.category(char)[0] in "LNM" else " "
            for char in text
            if char not in apostrophes
        ).split()
    )


def edit_distance(reference, hypothesis):
    previous = list(range(len(hypothesis) + 1))
    for index, expected in enumerate(reference, 1):
        current = [index]
        for column, actual in enumerate(hypothesis, 1):
            current.append(
                min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (expected != actual))
            )
        previous = current
    return previous[-1]


def error_counts(reference, hypothesis):
    reference, hypothesis = normalize_text(reference), normalize_text(hypothesis)
    words = reference.split()
    return {
        "word_errors": edit_distance(words, hypothesis.split()),
        "reference_words": len(words),
        "char_errors": edit_distance(reference, hypothesis),
        "reference_chars": len(reference),
    }


def add_noise(pcm, seed, snr_db):
    import numpy as np

    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    rms = np.sqrt(np.mean(samples**2))
    noise = np.random.default_rng(seed).normal(size=samples.size)
    noise *= rms * 10 ** (-snr_db / 20) / max(np.sqrt(np.mean(noise**2)), 1e-12)
    return np.clip(np.rint(samples + noise), -32768, 32767).astype("<i2").tobytes()


ERROR_CODES = {
    "audio",
    "backend",
    "busy",
    "cancelled",
    "timeout",
    "model",
    "model_missing",
    "runtime",
    "native_fallback",
    "output_truncated",
}


def observe_inference(service, audio, reference, *, native, request_id, timeout, warmup=False):
    from voxtypex.interfaces import OperationContext, ProgressStage

    fell_back = False

    def progress(event):
        nonlocal fell_back
        fell_back |= event.stage == ProgressStage.FALLBACK

    started = time.perf_counter()
    error_code, hypothesis = None, ""
    try:
        result = service.run(
            audio, OperationContext(request_id, deadline=time.monotonic() + timeout), progress
        )
        if native and fell_back:
            error_code = "native_fallback"
        elif result.error:
            error_code = result.error_code if result.error_code in ERROR_CODES else "backend"
        else:
            hypothesis = result.text
    except Exception:
        error_code = "backend"
    elapsed = time.perf_counter() - started
    return {
        **error_counts(reference, hypothesis),
        "audio_seconds": audio.duration,
        "elapsed_seconds": elapsed,
        "error_code": error_code,
        "warmup": warmup,
    }


def aggregate(observations):
    observations = [row for row in observations if not row.get("warmup", False)]
    totals = {
        key: sum(row[key] for row in observations)
        for key in (
            "word_errors",
            "reference_words",
            "char_errors",
            "reference_chars",
            "audio_seconds",
            "elapsed_seconds",
        )
    }
    words, chars = totals["reference_words"], totals["reference_chars"]
    audio, elapsed = totals["audio_seconds"], totals["elapsed_seconds"]
    errors = Counter(row["error_code"] for row in observations if row.get("error_code"))
    return {
        **totals,
        "clips": len(observations),
        "failed_clips": sum(errors.values()),
        "error_categories": dict(errors),
        "wer": totals["word_errors"] / words if words else None,
        "cer": totals["char_errors"] / chars if chars else None,
        "rtf": elapsed / audio if audio else None,
        "speed_x_realtime": audio / elapsed if elapsed else None,
    }


RUN_FIELDS = (
    "language",
    "variant",
    "condition",
    "status",
    "backend",
    "model_id",
    "model_bytes",
    "catalog_bytes",
    "model_sha256",
    "model_revision",
    "license",
    "device",
    "warmup_seconds",
    "worker_peak_rss_bytes",
    "worker_peak_vram_bytes",
    "resource_notes",
    "error_category",
    "decoding",
    "metrics",
)
CSV_FIELDS = (
    "language",
    "variant",
    "condition",
    "status",
    "backend",
    "model_id",
    "model_bytes",
    "catalog_bytes",
    "device",
    "warmup_seconds",
    "worker_peak_rss_bytes",
    "worker_peak_vram_bytes",
    "clips",
    "failed_clips",
    "wer",
    "cer",
    "rtf",
    "speed_x_realtime",
    "audio_seconds",
    "elapsed_seconds",
    "error_category",
)


def write_reports(report, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    safe = {
        key: report[key]
        for key in (
            "schema_version",
            "created_utc",
            "machine",
            "corpus",
            "normalization",
            "conditions",
            "limitations",
        )
        if key in report
    }
    safe["runs"] = [{key: run[key] for key in RUN_FIELDS if key in run} for run in report["runs"]]
    (output / "report.json").write_text(
        json.dumps(safe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (output / "report.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for run in safe["runs"]:
            row = {key: run[key] for key in CSV_FIELDS if key in run}
            row.update({key: value for key, value in run.get("metrics", {}).items() if key in CSV_FIELDS})
            writer.writerow(row)


def read_audio(item, manifest_dir):
    from voxtypex.interfaces import AudioClip

    path = Path(item.get("audio", item.get("path", item.get("file", ""))))
    if not path.is_absolute():
        path = manifest_dir / path
    if item.get("audio_sha256"):
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != item["audio_sha256"]:
                raise ValueError("Corpus audio checksum mismatch")
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
            raise ValueError("Benchmark requires identical mono PCM16 16 kHz WAVs")
        return AudioClip(wav.readframes(wav.getnframes()), 16000)


def worker_rss(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class MemoryCounters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(MemoryCounters),
            wintypes.DWORD,
        ]
        process = kernel.OpenProcess(0x1000, False, pid)
        if not process:
            return None
        try:
            counters = MemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
                return counters.PeakWorkingSetSize
        finally:
            kernel.CloseHandle(process)
    else:
        try:
            import psutil

            return psutil.Process(pid).memory_info().rss
        except (ImportError, OSError):
            return None
    return None


def worker_vram(pid):
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=0x08000000 if os.name == "nt" else 0,
            check=False,
        )
        if output.returncode != 0:
            return None
        total, found = 0, False
        for line in output.stdout.splitlines():
            columns = [x.strip() for x in line.split(",")]
            if len(columns) == 2 and columns[0] == str(pid):
                found = True
                if not columns[1].isdigit():
                    return None
                total += int(columns[1]) * 1024 * 1024
        return total if found else None
    except (OSError, subprocess.TimeoutExpired):
        return None


class ResourceMonitor:
    def __init__(self, service):
        self.service = service
        self.rss = None
        self.vram = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._poll, daemon=True)

    def _poll(self):
        last_gpu = 0.0
        while not self.stop.is_set():
            engine = self.service.engine
            pid = getattr(engine, "worker_pid", None)
            if not pid:
                process = getattr(engine, "_process", None)
                pid = process.pid if process else None
            if pid:
                memory = worker_rss(pid)
                if memory is not None:
                    self.rss = max(memory, self.rss or 0)
                device = getattr(engine, "device", None) or getattr(engine, "_device", None)
                if device == "cuda" and time.monotonic() - last_gpu >= 1:
                    last_gpu = time.monotonic()
                    gpu_memory = worker_vram(pid)
                    if gpu_memory is not None:
                        self.vram = max(gpu_memory, self.vram or 0)
            self.stop.wait(0.05)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=4)


def prepare_model(language, variant, cache_dir):
    from voxtypex.config import Settings

    native = variant in ("compact", "accurate")
    if native:
        from voxtypex.local_models import LanguageModelManager, LanguageModelRegistry

        registry = LanguageModelRegistry()
        spec = registry.for_language(language, variant)
        path = Path(LanguageModelManager(cache_dir, registry).resolve(spec.id))
        settings = Settings(
            asr_backend="transcribe-cpp", language=language, **{f"{language}_quality": variant}
        )
        metadata = {
            "backend": "transcribe-cpp",
            "model_id": spec.id,
            "model_bytes": path.stat().st_size,
            "catalog_bytes": spec.size_bytes,
            "model_sha256": spec.files[0].sha256,
            "model_revision": spec.revision,
            "license": spec.license,
            "decoding": "transcribe.cpp application defaults; CPU",
        }
    else:
        from faster_whisper.utils import download_model

        from voxtypex.models import WhisperModelManager

        path = Path(download_model(variant, cache_dir=str(cache_dir), local_files_only=True))
        if not WhisperModelManager.complete(path):
            raise FileNotFoundError("Cached Whisper model is incomplete")
        settings = Settings(
            asr_backend="faster-whisper", language=language, whisper_model=variant, model_path=str(path)
        )
        with (path / "model.bin").open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        metadata = {
            "backend": "faster-whisper",
            "model_id": variant,
            "model_bytes": sum(x.stat().st_size for x in path.rglob("*") if x.is_file()),
            "model_sha256": digest,
            "model_revision": path.name,
            "license": "MIT",
            "decoding": "VoxTypeX faster-whisper application defaults; auto CUDA/int8 CPU",
        }
    return settings, metadata


def machine_info():
    versions = {}
    for package in (
        "voxtypex",
        "numpy",
        "faster-whisper",
        "ctranslate2",
        "transcribe-cpp",
        "transcribe-cpp-native",
    ):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    gpu = None
    try:
        command = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=0x08000000 if os.name == "nt" else 0,
            check=False,
        )
        if command.returncode == 0:
            gpu = command.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {
        "os": platform.platform(),
        "python": platform.python_version(),
        "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count(),
        "packages": versions,
        "nvidia_gpu_and_driver": gpu,
    }


def run_benchmark(args):
    from voxtypex.asr import make_transcriber
    from voxtypex.interfaces import AudioClip

    document = json.loads(args.manifest.read_text(encoding="utf-8"))
    clips = document["clips"] if isinstance(document, dict) else document
    if not clips:
        raise ValueError("Empty corpus")
    report = {
        "schema_version": 1,
        "created_utc": datetime.now(UTC).isoformat(),
        "machine": machine_info(),
        "corpus": {
            key: document[key]
            for key in ("dataset", "dataset_revision", "license", "selection", "scope")
            if isinstance(document, dict) and key in document
        },
        "normalization": "NFKC + casefold; remove apostrophes; space other punctuation/symbols; preserve diacritics; "
        "no number expansion; CER includes normalized word spaces; sum errors / sum references",
        "conditions": {
            "read": "unchanged public read speech",
            "noise": {
                "kind": "seeded additive Gaussian noise",
                "seed": args.noise_seed,
                "target_snr_db": args.noise_snr_db,
                "note": "Global RMS, then PCM16 saturation; target is before clipping; synthetic noise is not natural noise",
            },
        },
        "limitations": [
            "Small deterministic read-speech sample; no confidence in population-wide ranking",
            "Conversational speech, accents outside the sample, dictation and user microphone are untested",
            "Both backends explicitly use CPU; latency includes application IPC",
            "Warmups excluded from latency/accuracy; worker RAM includes model load and warmup",
            "Failed measured clips count as empty hypotheses; skipped models have no accuracy score",
            "Native fallback aborts remaining native measurements; Whisper text gets no native credit",
        ],
        "runs": [],
    }
    request_id = 0
    for language in args.languages:
        selected = [x for x in clips if x["language"] == language]
        if args.limit_per_language:
            selected = selected[: args.limit_per_language]
        if not selected:
            raise ValueError("Requested language missing from corpus")
        inputs = [(item, read_audio(item, args.manifest.parent)) for item in selected]
        for variant in args.variants:
            base = {"language": language, "variant": variant}
            service = None
            try:
                settings, metadata = prepare_model(language, variant, args.cache_dir)
                base.update(metadata)
                service = make_transcriber(settings, args.cache_dir)
                if variant not in ("compact", "accurate"):
                    service.engine._force_cpu = True
                    base["decoding"] = "VoxTypeX faster-whisper defaults, CPU int8, beam_size=3"
                with ResourceMonitor(service) as monitor:
                    warmups = []
                    for _ in range(args.warmups):
                        request_id += 1
                        warmups.append(
                            observe_inference(
                                service,
                                inputs[0][1],
                                inputs[0][0]["reference"],
                                native=variant in ("compact", "accurate"),
                                request_id=request_id,
                                timeout=args.timeout,
                                warmup=True,
                            )
                        )
                    warmup_error = next((x["error_code"] for x in warmups if x["error_code"]), None)
                    base["warmup_seconds"] = sum(x["elapsed_seconds"] for x in warmups)
                    native_aborted = bool(warmup_error)
                    for condition in args.conditions:
                        if native_aborted:
                            report["runs"].append(
                                {
                                    **base,
                                    "condition": condition,
                                    "status": "failed",
                                    "error_category": warmup_error or "native_fallback",
                                }
                            )
                            continue
                        observations = []
                        for item, original in inputs:
                            audio = original
                            if condition == "noise":
                                clip_seed = args.noise_seed + int.from_bytes(
                                    hashlib.sha256(item["id"].encode("utf-8")).digest()[:4], "little"
                                )
                                audio = AudioClip(
                                    add_noise(original.pcm, clip_seed, args.noise_snr_db), 16000
                                )
                            request_id += 1
                            row = observe_inference(
                                service,
                                audio,
                                item["reference"],
                                native=variant in ("compact", "accurate"),
                                request_id=request_id,
                                timeout=args.timeout,
                            )
                            observations.append(row)
                            if row["error_code"] == "native_fallback":
                                native_aborted = True
                                break
                        metrics = aggregate(observations)
                        report["runs"].append(
                            {
                                **base,
                                "condition": condition,
                                "status": "complete" if not metrics["failed_clips"] else "failed",
                                "metrics": metrics,
                            }
                        )
                        print(
                            f"{language}/{variant}/{condition}: {metrics['clips']} clips, "
                            f"WER {metrics['wer']:.2%}, CER {metrics['cer']:.2%}, RTF {metrics['rtf']:.3f}",
                            flush=True,
                        )
                engine = service.engine
                device = getattr(engine, "device", None) or getattr(engine, "_device", None)
                for row in report["runs"]:
                    if (row["language"], row["variant"]) == (language, variant):
                        row.update(
                            {
                                "device": device,
                                "worker_peak_rss_bytes": monitor.rss,
                                "worker_peak_vram_bytes": 0 if device == "cpu" else monitor.vram,
                                "resource_notes": "Worker peak RSS sampled at 50ms (Windows process high-water); "
                                "VRAM sampled via nvidia-smi about 1s; null means unavailable",
                            }
                        )
            except Exception as exc:
                category = "missing_model" if isinstance(exc, FileNotFoundError) else "unavailable"
                for condition in args.conditions:
                    report["runs"].append(
                        {**base, "condition": condition, "status": "skipped", "error_category": category}
                    )
                print(f"{language}/{variant}: skipped ({category})", flush=True)
            finally:
                if service is not None:
                    service.close()
            write_reports(report, args.out)
    return report


def main():
    from voxtypex.config import model_directory

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path(".local/iteration2-benchmark/manifest.json"))
    parser.add_argument("--out", type=Path, default=Path(".local/iteration2-benchmark/results"))
    parser.add_argument("--cache-dir", type=Path, default=model_directory())
    parser.add_argument("--languages", nargs="+", choices=("ru", "uk", "en"), default=["ru", "uk", "en"])
    parser.add_argument(
        "--variants",
        nargs="+",
        choices=("compact", "accurate", "tiny", "base", "small"),
        default=["compact", "accurate", "tiny", "base", "small"],
    )
    parser.add_argument("--conditions", nargs="+", choices=("read", "noise"), default=["read", "noise"])
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--limit-per-language", type=int)
    parser.add_argument("--noise-seed", type=int, default=17)
    parser.add_argument("--noise-snr-db", type=float, default=20)
    parser.add_argument("--timeout", type=float, default=360)
    args = parser.parse_args()
    if (
        args.warmups < 1
        or args.timeout <= 0
        or (args.limit_per_language is not None and args.limit_per_language < 1)
    ):
        parser.error("Use at least one warmup and positive timeout/clip limit")
    args.languages = list(dict.fromkeys(args.languages))
    args.variants = list(dict.fromkeys(args.variants))
    args.conditions = list(dict.fromkeys(args.conditions))
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    logging.basicConfig(level=logging.ERROR)
    try:
        report = run_benchmark(args)
    except Exception as exc:
        print(f"Benchmark stopped ({type(exc).__name__}); no accuracy claim can be made")
        print("".join(traceback.format_tb(exc.__traceback__)))
        return 1
    return 0 if all(run["status"] == "complete" for run in report["runs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
