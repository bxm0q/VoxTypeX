import importlib
import json
import os
import time
import traceback
import wave
from pathlib import Path
from threading import Event, Thread, Timer


def run(directory, manifest_path):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    os.environ["VOXTYPEX_DATA_DIR"] = str(directory)
    os.environ["HF_HUB_OFFLINE"] = "1"
    document = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    clips = document["clips"]
    report = {"checks": [], "errors": [], "languages": []}
    finished = Event()
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QSystemTrayIcon

    from voxtypex.config import Settings, SettingsService, model_directory
    from voxtypex.interfaces import AudioClip, OperationContext, ProgressStage

    entry = importlib.import_module("voxtypex.main")
    SettingsService(directory / "settings.json").save(
        Settings(asr_backend="transcribe-cpp", language="ru", enabled=False)
    )
    original_start = entry.TrayApplication.start

    def start(runtime):
        original_start(runtime)

        def probe():
            # Use a separate pipeline to avoid racing the service idle cleanup.
            service = None
            try:
                service = entry.build_transcriber(runtime.service.settings, model_directory())
                for request_id, language in enumerate(("ru", "uk", "en"), 1):
                    item = next(row for row in clips if row["language"] == language)
                    source = Path(item.get("audio", item.get("path")))
                    if not source.is_absolute():
                        source = Path(manifest_path).parent / source
                    with wave.open(str(source), "rb") as wav:
                        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
                        clip = AudioClip(wav.readframes(wav.getnframes()), 16000)
                    service.language = language
                    events = []
                    result = service.run(
                        clip, OperationContext(request_id, deadline=time.monotonic() + 300), events.append
                    )
                    assert not result.error and result.text
                    pid = service.engine.worker_pid
                    second = service.run(
                        clip,
                        OperationContext(request_id + 10, deadline=time.monotonic() + 300),
                        events.append,
                    )
                    assert (
                        not second.error and second.text == result.text and service.engine.worker_pid == pid
                    )
                    assert all(event.stage != ProgressStage.FALLBACK for event in events)
                    report["languages"].append(
                        {
                            "language": language,
                            "characters": len(result.text),
                            "device": service.engine.device,
                            "worker_reused": True,
                        }
                    )
                report["checks"].extend(
                    ["real_ru_uk_en_native_asr", "model_reuse", "language_switch_without_restart"]
                )
                events = []
                long_clip = AudioClip(clip.pcm * 4, 16000)
                assert long_clip.duration > 20
                result = service.run(
                    long_clip, OperationContext(30, deadline=time.monotonic() + 300), events.append
                )
                assert not result.error and len(result.text) > len(second.text) * 2
                assert all(event.stage != ProgressStage.FALLBACK for event in events)
                report["checks"].append("long_audio_vad_segments_and_join")
                context = OperationContext(40, deadline=time.monotonic() + 300)
                timer = Timer(0.05, context.cancelled.set)
                timer.start()
                result = service.run(long_clip, context)
                timer.join()
                assert result.error_code == "cancelled" and not result.text
                assert service.engine.worker_pid is None
                events = []
                result = service.run(clip, OperationContext(41, deadline=time.monotonic() + 300), events.append)
                assert not result.error and result.text
                assert all(event.stage != ProgressStage.FALLBACK for event in events)
                report["checks"].append("cancel_active_native_worker_and_retry")
                local_engine = service.engine._local
                service.language = None
                result = service.run(clip, OperationContext(42, deadline=time.monotonic() + 300))
                assert not result.error and result.text
                assert local_engine._process is None and service.engine._local is None
                whisper_engine = service.engine._whisper
                service.language = "en"
                events = []
                result = service.run(clip, OperationContext(43, deadline=time.monotonic() + 300), events.append)
                assert not result.error and result.text
                assert all(event.stage != ProgressStage.FALLBACK for event in events)
                assert whisper_engine._process is None and service.engine._whisper is None
                report["checks"].append("auto_whisper_and_return_release_inactive_model")
                active = service.engine._active
                active._last_used -= 181
                service.release_idle()
                assert active._process is None and not active._cancelled.is_set()
                report["checks"].append("idle_release")
            except Exception as exc:
                report["errors"].append(
                    {"type": type(exc).__name__, "stack": traceback.format_tb(exc.__traceback__)}
                )
            finally:
                try:
                    if service is not None:
                        service.close()
                except Exception as exc:
                    report["errors"].append(
                        {"type": type(exc).__name__, "stack": traceback.format_tb(exc.__traceback__)}
                    )
                finally:
                    finished.set()

        def inspect():
            try:
                assert runtime.tray.isVisible() and QSystemTrayIcon.isSystemTrayAvailable()
                runtime.settings_action.trigger()
                assert runtime.window.asr_backend.currentData() == "transcribe-cpp"
                runtime.window.grab().save(str(directory / "settings.png"))
                report["checks"].append("real_main_tray_and_native_settings")
                Thread(target=probe, name="native-smoke", daemon=True).start()
            except Exception as exc:
                report["errors"].append({"type": type(exc).__name__})
                finished.set()

        def tick():
            if finished.is_set():
                timer.stop()
                runtime.window.close()
                runtime.request_quit()

        timer = QTimer(runtime.app)
        timer.timeout.connect(tick)
        timer.start(100)
        QTimer.singleShot(1000, inspect)
        QTimer.singleShot(720000, runtime.request_quit)

    entry.TrayApplication.start = start
    exit_code = entry.main()
    report["exit_code"] = exit_code
    if finished.is_set() and not report["errors"] and exit_code == 0:
        report["checks"].append("shutdown")
    (directory / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(exit_code != 0 or bool(report["errors"]) or len(report["checks"]) != 9)
