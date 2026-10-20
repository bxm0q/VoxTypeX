import json
import os
import traceback
import wave
from dataclasses import asdict
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, sleep


class ReplayRecorder:
    def __init__(self, pcm, settings):
        from voxtypex.audio import make_processor

        self.pcm, self.settings = pcm, settings
        self.processor_factory = make_processor
        self.device = None
        self.done = Event()
        self._stop = Event()
        self._lock = Lock()
        self._thread = None
        self._processor = None
        self.error = None

    def start(self, held):
        self.done.clear()
        self._stop.clear()
        self._processor = self.processor_factory(self.settings)

        def feed():
            try:
                started = monotonic()
                for offset in range(0, len(self.pcm), 640):
                    if self._stop.is_set() or not held.is_set():
                        break
                    with self._lock:
                        self._processor.push(self.pcm[offset:offset + 640])
                    sleep(max(0, started + (offset + 640) / 32000 - monotonic()))
            except Exception as exc:
                self.error = type(exc).__name__
            finally:
                self.done.set()

        self._thread = Thread(target=feed, daemon=True)
        self._thread.start()

    def preview_audio(self, start, maximum):
        with self._lock:
            return self._processor.preview_audio(start, maximum) if self._processor else None

    def check_health(self):
        if self.error:
            raise RuntimeError(self.error)

    def stop(self):
        self._stop.set()
        self._thread.join(3)
        with self._lock:
            result = self._processor.finish()
            self._processor = None
            return result

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(3)
        with self._lock:
            if self._processor:
                self._processor.close()
                self._processor = None


class ProbeHotkey:
    def set_guard_callbacks(self, changed, cancel):
        self.changed, self.cancel = changed, cancel

    def start(self, *args):
        pass

    def check_health(self):
        pass

    def close(self):
        pass


def target_window(connection):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget

    app = QApplication([])
    window = QWidget()
    window.setWindowTitle("VoxTypeX — отдельные поля для smoke-теста")
    layout = QVBoxLayout(window)
    fields = [QLineEdit(), QLineEdit()]
    for field in fields:
        layout.addWidget(field)
    window.resize(720, 160)
    window.show()
    window.raise_()
    window.activateWindow()
    fields[0].setFocus()
    previous = None

    def tick():
        nonlocal previous
        while connection.poll():
            command, value = connection.recv()
            if command == "close":
                app.quit()
                return
            if command == "focus":
                import ctypes

                window.raise_()
                window.activateWindow()
                ctypes.windll.user32.SetForegroundWindow(int(window.winId()))
                fields[value].setFocus()
        snapshot = ([f.text() for f in fields], next((i for i, f in enumerate(fields) if f.hasFocus()), -1))
        if snapshot != previous:
            connection.send(("target", os.getpid(), int(window.winId()), *snapshot))
            previous = snapshot

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(50)
    app.exec()
    connection.close()


def run(directory, speech_wav):
    from multiprocessing import get_context

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from voxtypex.asr import make_transcriber
    from voxtypex.config import Settings, SettingsService, model_directory
    from voxtypex.insertion import TextInsertionService
    from voxtypex.interfaces import AudioClip, OperationContext, ProgressStage, TranscriptKind
    from voxtypex.recording_service import RecordingService
    from voxtypex.state import AppController, AppState
    from voxtypex.ui.tray import TrayApplication

    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    os.environ["VOXTYPEX_DATA_DIR"] = str(directory)
    with wave.open(str(speech_wav), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
            raise ValueError("Expected 16 kHz mono PCM16 WAV")
        pcm = wav.readframes(wav.getnframes())
    settings = Settings(
        whisper_model=os.environ.get("VOXTYPEX_SMOKE_MODEL", "tiny"),
        asr_backend=os.environ.get("VOXTYPEX_PREVIEW_BACKEND", "faster-whisper"),
        language=os.environ.get("VOXTYPEX_PREVIEW_LANGUAGE", "ru"), preview_interval_seconds=1,
    )
    case = os.environ.get("VOXTYPEX_PREVIEW_CASE", "normal")
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    context = get_context("spawn")
    target_pipe, child_pipe = context.Pipe()
    target_process = context.Process(target=target_window, args=(child_pipe,), daemon=True)
    target_process.start()
    child_pipe.close()
    target_values = ["", ""]
    target_info = None
    pipeline = make_transcriber(settings, model_directory())
    if settings.asr_backend == "faster-whisper":
        pipeline.engine._force_cpu = True
    recorder = ReplayRecorder(pcm, settings)
    inserter = TextInsertionService(restore_delay=0.15)
    report = {"backend": settings.asr_backend, "language": settings.language, "case": case,
              "checks": [], "errors": [], "partial_updates": 0, "distinct_hypotheses": 0,
              "final_events": 0, "insert_attempts": 0, "audio_seconds": len(pcm) / 32000}
    original_insert = inserter.insert_text

    def insert(text):
        report["insert_attempts"] += 1
        return original_insert(text)

    inserter.insert_text = insert
    service = RecordingService(AppController(), recorder, ProbeHotkey(), settings, directory,
                               transcriber=pipeline, inserter=inserter)
    store = SettingsService(directory / "settings.json")
    store.save(settings)
    runtime = TrayApplication(app, store, settings, service)
    runtime.window.microphone_provider = lambda: []
    warmed, warming_failed = Event(), Event()

    def warmup():
        try:
            events = []
            result = pipeline.run(AudioClip(pcm, 16000), OperationContext(0, deadline=monotonic() + 180),
                                  events.append)
            report["warmup_fallbacks"] = sum(event.stage is ProgressStage.FALLBACK for event in events)
            if result.error or not result.text:
                raise RuntimeError("Warmup failed")
        except Exception as exc:
            report["errors"].append({"stage": "warmup", "type": type(exc).__name__})
            warming_failed.set()
        finally:
            warmed.set()

    Thread(target=warmup, daemon=True).start()
    original_drain = service.drain_transcripts
    previous = ""
    action_done = pressed = released = finished = False
    runtime_started = False
    target_deadline = None
    finish_after = None

    def drain():
        nonlocal previous
        events = original_drain()
        for event in events:
            if event.kind is TranscriptKind.PARTIAL and event.text:
                assert service.controller.state is AppState.RECORDING
                assert not any(target_values)
                report["partial_updates"] += 1
                if event.text != previous:
                    report["distinct_hypotheses"] += 1
                    previous = event.text
            elif event.kind is TranscriptKind.FINAL:
                report["final_events"] += 1
        return events

    service.drain_transcripts = drain

    def finish():
        nonlocal finished
        finished = True
        metrics = service.last_preview_metrics
        report["metrics"] = {**asdict(metrics), "cpu_percent_one_core": metrics.cpu_percent}
        report["target_characters"] = [len(value) for value in target_values]
        report["expected_characters"] = len(service.last_result.text) if service.last_result else 0
        inserted = service.last_insert_result
        report["insert_result"] = ({"success": inserted.success, "method": inserted.method,
                                    "skipped": inserted.skipped} if inserted else None)
        if service.last_error and case == "normal":
            report["errors"].append({"stage": "service", "type": "PipelineError"})
        if case == "normal":
            if (report["distinct_hypotheses"] < 2 or report["final_events"] != 1
                    or metrics.first_visible_ms is None
                    or report["insert_attempts"] != 1 or service.last_result is None
                    or target_values[0] != service.last_result.text or target_values[1]):
                report["errors"].append({"stage": "final", "type": "PreviewOrInsertionMismatch"})
            else:
                report["checks"].extend(["real_asr_changed_while_recording", "no_partial_insertion",
                                         "one_final_windows_insertion", "overlay_kept_target_focus"])
        elif any(target_values) or report["final_events"]:
            report["errors"].append({"stage": case, "type": "UnexpectedText"})
        else:
            report["checks"].append(case + "_cleared_preview_and_kept_fields_empty")
        runtime.request_quit()

    deadline = monotonic() + 240

    def tick():
        nonlocal pressed, released, action_done, target_info, target_values
        nonlocal runtime_started, target_deadline, finish_after
        try:
            if finished:
                return
            if monotonic() > deadline or warming_failed.is_set():
                raise TimeoutError("Preview smoke did not finish")
            while target_pipe.poll():
                _, pid, window, target_values, focus = target_pipe.recv()
                target_info = (pid, window, focus)
            if not warmed.is_set() or target_info is None:
                return
            if not pressed:
                if not runtime_started:
                    runtime.start()
                    runtime_started = True
                    target_deadline = monotonic() + 5
                    target_pipe.send(("focus", 0))
                    return
                current = inserter.capture_target()
                if current is None or (current.process, current.window) != target_info[:2]:
                    if monotonic() > target_deadline:
                        raise RuntimeError("Test target is not the foreground window")
                    return
                service.pressed()
                pressed = True
            if report["partial_updates"] and not action_done:
                action_done = True
                current = inserter.capture_target()
                assert runtime.overlay.isVisible() and current is not None
                assert (current.process, current.window) == target_info[:2] and target_info[2] == 0
                runtime.overlay.grab().save(str(directory / "overlay.png"))
                if case == "changed":
                    target_pipe.send(("focus", 1))
                    service.context_changed()
                elif case == "cancel":
                    service.cancel_current()
                elif case == "close":
                    service.request_close()
            if recorder.done.is_set() and not released:
                service.released()
                released = True
            if pressed and released and not service._busy:
                if finish_after is None:
                    finish_after = monotonic() + 0.3
                elif monotonic() >= finish_after:
                    runtime.refresh()
                    finish()
            elif case == "close" and service.finished.is_set():
                finish()
        except Exception as exc:
            report["errors"].append({"stage": "tick", "type": type(exc).__name__,
                                     "locations": [f"{Path(f.filename).name}:{f.lineno}"
                                                   for f in traceback.extract_tb(exc.__traceback__)]})
            finish()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(50)
    exit_code = app.exec()
    runtime.close()
    target_pipe.send(("close", None))
    target_process.join(5)
    if target_process.is_alive():
        target_process.terminate()
        target_process.join(3)
    target_process.close()
    target_pipe.close()
    report["exit_code"] = int(exit_code != 0 or bool(report["errors"]))
    (directory / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report["exit_code"]


if __name__ == "__main__":
    import argparse
    from multiprocessing import freeze_support

    freeze_support()
    parser = argparse.ArgumentParser(description="Replay a public WAV in real time; test actual local ASR and Windows insertion")
    parser.add_argument("directory")
    parser.add_argument("speech_wav")
    args = parser.parse_args()
    raise SystemExit(run(args.directory, args.speech_wav))
