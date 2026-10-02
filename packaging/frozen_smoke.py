"""Explicit release check using the real application and bundled native libraries."""

import importlib
import json
import os
import time
import wave
from pathlib import Path
from threading import Event, Thread


def run(directory, speech_wav):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    os.environ["VOXTYPEX_DATA_DIR"] = str(directory)
    report = {"checks": [], "errors": [], "phases": []}
    with wave.open(speech_wav, "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
            raise ValueError("Smoke fixture must be mono PCM16 / 16000 Hz")
        pcm = wav.readframes(wav.getnframes())

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QSystemTrayIcon

    from voxtypex.audio import MicrophoneRecorder
    from voxtypex.config import Settings, SettingsService
    from voxtypex.interfaces import AudioClip

    entry = importlib.import_module("voxtypex.main")
    store = SettingsService(directory / "settings.json")
    store.save(Settings(whisper_model=os.environ.get("VOXTYPEX_SMOKE_MODEL", "small")))
    original_start = entry.TrayApplication.start
    finished = Event()

    def start(runtime):
        original_start(runtime)

        def inspect():
            try:
                assert QSystemTrayIcon.isSystemTrayAvailable() and runtime.tray.isVisible()
                report["checks"].append("tray")
                runtime.settings_action.trigger()
                assert runtime.window.isVisible()
                runtime.window.grab().save(str(directory / "settings.png"))
                report["checks"].append("settings")
                Thread(target=probe, daemon=True).start()
            except Exception as exc:
                report["errors"].append(repr(exc))
                finished.set()

        def probe():
            recorder = MicrophoneRecorder()
            try:
                held = Event()
                held.set()
                recorder.start(held)
                time.sleep(0.6)
                recorder.check_health()
                held.clear()
                clip = recorder.stop()
                assert clip.duration > 0.2
                report["checks"].append("microphone_callbacks")
                result = runtime.service.transcriber.transcribe(AudioClip(pcm, 16000))
                assert not result.error and result.text, result.error or "Empty transcription"
                report.update(text=result.text, processing_seconds=result.processing_time)
                report["checks"].append("frozen_whisper_process_and_vad")
            except Exception as exc:
                report["errors"].append(repr(exc))
            finally:
                recorder.close()
                finished.set()

        def tick():
            phase = runtime.service.transcriber.engine.phase
            if phase not in report["phases"]:
                report["phases"].append(phase)
            if finished.is_set():
                timer.stop()
                runtime.window.close()
                runtime.request_quit()

        timer = QTimer(runtime.app)
        timer.timeout.connect(tick)
        timer.start(100)
        runtime.app.aboutToQuit.connect(
            lambda: report["checks"].append("shutdown" if runtime.controller.closed else "shutdown_failed")
        )
        QTimer.singleShot(1500, inspect)
        QTimer.singleShot(720000, runtime.request_quit)

    entry.TrayApplication.start = start
    exit_code = entry.main()
    report["exit_code"] = exit_code
    (directory / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return int(exit_code != 0 or bool(report["errors"]) or len(report["checks"]) != 5)
