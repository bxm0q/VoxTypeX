import wave
from dataclasses import replace
from threading import Event, get_ident
from time import monotonic, sleep

import pytest

from voxtypex.config import Settings
from voxtypex.interfaces import AudioClip, TranscriptionResult
from voxtypex.recording_service import RecordingService
from voxtypex.state import AppController, AppState


def wait_for(predicate):
    deadline = monotonic() + 3
    while not predicate():
        assert monotonic() < deadline, "Worker timeout"
        sleep(0.005)


class FakeHotkey:
    def set_guard_callbacks(self, changed, cancel):
        self.changed, self.cancel = changed, cancel

    def start(self, key, down, up):
        self.key, self.down, self.up = key, down, up

    def set_key(self, key):
        self.key = key

    def check_health(self):
        pass

    def close(self):
        self.closed = True


class FakeRecorder:
    def __init__(self):
        self.calls = []
        self.error = None
        self.health_error = None
        self.opening = Event()
        self.finish_open = Event()
        self.finish_open.set()
        self.stopping = Event()
        self.finish_stop = Event()
        self.finish_stop.set()
        self.clip = AudioClip(b"\0\0" * 16000, 16000)
        self.open = False

    def start(self, held):
        self.calls.append(("start", get_ident()))
        self.held = held
        self.opening.set()
        assert self.finish_open.wait(2)
        if self.error:
            raise self.error
        self.open = True

    def check_health(self):
        if self.health_error:
            raise self.health_error

    def stop(self):
        self.calls.append(("stop", get_ident()))
        self.stopping.set()
        assert self.finish_stop.wait(2)
        self.open = False
        return self.clip

    def close(self):
        self.calls.append(("close", get_ident()))
        self.open = False


@pytest.fixture
def service(tmp_path):
    recorder = FakeRecorder()
    service = RecordingService(AppController(), recorder, FakeHotkey(), Settings(), tmp_path)
    service.start()
    yield service
    recorder.finish_open.set()
    recorder.finish_stop.set()
    service.close()
    assert service.finished.is_set()
    assert not recorder.open


def test_normal_repeat_and_worker_isolation(service, caplog):
    caplog.set_level("INFO")
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    for _ in range(20):
        service.pressed()
    service.released()
    wait_for(lambda: service.completed_count == 1 and service.controller.state == AppState.IDLE)
    assert [name for name, _ in service.recorder.calls].count("start") == 1
    assert all(thread != get_ident() for _, thread in service.recorder.calls)
    assert "Duration: 1.000 s; Samples: 16000" in caplog.text
    assert not (service.directory / "last-recording.wav").exists()


def test_release_while_device_opens_and_busy_press(service):
    service.recorder.finish_open.clear()
    service.recorder.clip = AudioClip(b"", 16000)
    service.pressed()
    assert service.recorder.opening.wait(1)
    service.released()
    assert not service.recorder.held.is_set()
    service.pressed()  # rejected while opening/closing the previous recording
    service.released()
    service.recorder.finish_open.set()
    wait_for(lambda: service.recorder.stopping.is_set() and not service._busy)
    assert service.completed_count == 0
    assert [name for name, _ in service.recorder.calls].count("start") == 1


@pytest.mark.parametrize("samples, accepted", [(0, False), (3199, False), (3200, True)])
def test_short_threshold(service, samples, accepted):
    service.recorder.clip = AudioClip(b"\0\0" * samples, 16000)
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    service.released()
    wait_for(lambda: not service._busy)
    assert service.completed_count == int(accepted)


def test_busy_during_stop_ignores_press_until_next_edge(service):
    service.recorder.finish_stop.clear()
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    service.released()
    assert service.recorder.stopping.wait(1)
    service.pressed()
    service.recorder.finish_stop.set()
    wait_for(lambda: not service._busy)
    service.pressed()  # auto-repeat of the ignored press must not start a stream
    assert [n for n, _ in service.recorder.calls].count("start") == 1
    service.released()
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    service.released()
    wait_for(lambda: service.completed_count == 2)


def test_error_recovery_after_release(service):
    service.recorder.error = OSError("device busy")
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.ERROR and not service._busy)
    service.pressed()
    assert [n for n, _ in service.recorder.calls].count("start") == 1
    service.released()
    service.recorder.error = None
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    service.released()
    wait_for(lambda: service.completed_count == 1)


def test_disconnected_microphone_and_shutdown(service):
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    service.recorder.health_error = RuntimeError("disconnected")
    wait_for(lambda: service.controller.state == AppState.ERROR)
    wait_for(lambda: not service.recorder.open)
    assert "Микрофон недоступен" in service.last_error
    assert "disconnected" not in service.last_error


def test_shutdown_during_recording(service):
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    service.request_close()
    assert not service.recorder.held.is_set()
    assert service.finished.wait(2)
    assert not service.recorder.open


def test_settings_and_debug_wav(service):
    service.update_settings(replace(service.settings, push_to_talk_key="f8", debug_wav=True))
    wait_for(lambda: service.hotkey.key == "f8")
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    service.released()
    wait_for(lambda: not service._busy)
    with wave.open(str(service.directory / "last-recording.wav"), "rb") as wav:
        assert (wav.getnchannels(), wav.getframerate(), wav.getsampwidth(), wav.getnframes()) == (
            1,
            16000,
            2,
            16000,
        )


def test_fast_tap_before_worker_starts(tmp_path):
    recorder = FakeRecorder()
    service = RecordingService(AppController(), recorder, FakeHotkey(), Settings(), tmp_path)
    service.pressed()
    service.released()
    service.start()
    try:
        wait_for(lambda: not service._busy)
        assert not recorder.opening.is_set()
        assert service.controller.state == AppState.IDLE
    finally:
        service.close()


def test_press_during_transcription_is_ignored(service):
    service.controller.transition(AppState.IDLE, AppState.RECORDING)
    service.controller.transition(AppState.RECORDING, AppState.TRANSCRIBING)
    service.pressed()
    assert not service._busy
    assert not service.recorder.opening.is_set()
    service.released()


def test_listener_failure_cleans_up_without_crashing(tmp_path):
    hotkey = FakeHotkey()

    def fail(*args):
        raise RuntimeError("hook unavailable")

    hotkey.start = fail
    recorder = FakeRecorder()
    service = RecordingService(AppController(), recorder, hotkey, Settings(), tmp_path)
    service.start()
    assert service.finished.wait(2)
    assert service.controller.state == AppState.ERROR
    assert "Горячая клавиша недоступна" in service.last_error
    service.pressed()
    assert not service._busy
    assert hotkey.closed
    assert not recorder.open
    service.close()


def test_shutdown_while_opening_is_nonblocking(service):
    service.recorder.finish_open.clear()
    service.pressed()
    assert service.recorder.opening.wait(1)
    service.request_close()
    assert not service.recorder.held.is_set()
    assert not service.finished.is_set()
    service.recorder.finish_open.set()
    assert service.finished.wait(2)
    assert not service.recorder.open


class FakeTranscriber:
    def __init__(self, service):
        self.service = service
        self.entered = Event()
        self.release = Event()
        self.result = TranscriptionResult(text="Привіт!", language="uk", processing_time=0.1)
        self.calls = 0

    def transcribe(self, audio):
        assert not self.service.recorder.open
        assert self.service.controller.state == AppState.TRANSCRIBING
        self.calls += 1
        self.entered.set()
        assert self.release.wait(2)
        return self.result

    def cancel(self):
        self.release.set()

    def close(self):
        self.cancel()


def test_recording_to_stt_and_busy_guard(service, caplog):
    caplog.set_level("INFO")
    stt = FakeTranscriber(service)
    service.transcriber = stt
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    service.released()
    assert stt.entered.wait(1)
    service.pressed()
    service.released()
    stt.release.set()
    wait_for(lambda: not service._busy)
    assert service.last_result.text == "Привіт!"
    assert service.controller.state == AppState.IDLE
    assert stt.calls == 1
    assert "Transcribed in 0.10 sec (language=uk, characters=7)" in caplog.text
    assert stt.result.text not in caplog.text


def test_stt_error_returns_error_state_and_can_retry(service):
    stt = FakeTranscriber(service)
    stt.result = TranscriptionResult(error="model missing")
    stt.release.set()
    service.transcriber = stt
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    service.released()
    wait_for(lambda: service.controller.state == AppState.ERROR and not service._busy)
    assert "Не удалось распознать речь" in service.last_error
    assert "model missing" not in service.last_error
    stt.result = TranscriptionResult()
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    service.released()
    wait_for(lambda: not service._busy)
    assert service.controller.state == AppState.IDLE


def test_exit_cancels_stt_and_does_not_log_late_text(service, caplog):
    caplog.set_level("INFO")
    stt = FakeTranscriber(service)
    service.transcriber = stt
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    service.released()
    assert stt.entered.wait(1)
    service.request_close()
    assert service.finished.wait(2)
    assert "Transcribed in" not in caplog.text


def test_short_recording_not_sent_to_stt(service):
    stt = FakeTranscriber(service)
    service.transcriber = stt
    service.recorder.clip = AudioClip(b"\0\0" * 100, 16000)
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    service.released()
    wait_for(lambda: not service._busy)
    assert stt.calls == 0


class FakeInserter:
    def __init__(self, service):
        self.service = service
        self.calls = []

    def capture_target(self):
        return "original field"

    def prepare_target(self, target, cancelled, changed):
        self.target, self.cancelled, self.changed = target, cancelled, changed

    def insert_text(self, text):
        from voxtypex.insertion import InsertResult

        assert self.service.controller.state == AppState.INSERTING
        assert self.target == "original field"
        self.calls.append(text)
        return InsertResult(success=not self.changed.is_set(), skipped=self.changed.is_set())

    def close(self):
        pass


@pytest.mark.parametrize("case", ["normal", "empty", "error", "short", "cancel", "changed", "exit"])
def test_complete_insertion_pipeline(service, case):
    stt = FakeTranscriber(service)
    service.transcriber = stt
    inserter = FakeInserter(service)
    service.inserter = inserter
    if case == "empty":
        stt.result = TranscriptionResult(text=" \n ")
    if case == "error":
        stt.result = TranscriptionResult(error="failed")
    if case == "short":
        service.recorder.clip = AudioClip(b"\0\0" * 100, 16000)
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    service.released()
    if case != "short":
        assert stt.entered.wait(1)
    if case == "cancel":
        service.cancel_current()
    if case == "changed":
        service.context_changed()
    if case == "exit":
        service.request_close()
    stt.release.set()
    wait_for(lambda: not service._busy or service.finished.is_set())
    assert len(inserter.calls) == int(case in ("normal", "changed"))
    if case == "normal":
        assert service.last_insert_result.success
    if case == "changed":
        assert service.last_insert_result.skipped


def test_escape_during_recording_discards_and_allows_next_press(service):
    stt = FakeTranscriber(service)
    stt.release.set()
    service.transcriber = stt
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    service.cancel_current()
    wait_for(lambda: not service._busy)
    assert stt.calls == 0 and service.controller.state == AppState.IDLE
    service.released()
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    service.released()
    wait_for(lambda: not service._busy)
    assert stt.calls == 1


def test_disabled_and_settings_capture_gate_block_recording(service):
    service.update_settings(replace(service.settings, enabled=False))
    wait_for(lambda: not service._settings_pending)
    service.pressed()
    service.released()
    assert not service.recorder.opening.is_set()
    service.update_settings(replace(service.settings, enabled=True))
    wait_for(lambda: not service._settings_pending)
    service.set_settings_open(True)
    service.pressed()
    service.set_settings_open(False)
    service.pressed()  # A held assignment key must not become a fresh PTT.
    assert not service.recorder.opening.is_set()
    service.released()
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    service.released()
    wait_for(lambda: not service._busy)


def test_disable_during_stt_discards_result(service):
    stt = FakeTranscriber(service)
    service.transcriber = stt
    service.inserter = FakeInserter(service)
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    service.released()
    assert stt.entered.wait(1)
    service.update_settings(replace(service.settings, enabled=False))
    stt.release.set()
    wait_for(lambda: not service._busy and not service._settings_pending)
    assert not service.inserter.calls
    assert service.last_result is None
    assert not service.enabled


def test_settings_apply_after_active_recording_and_rebuild_model(service):
    stt = FakeTranscriber(service)
    stt.release.set()
    service.transcriber = stt
    created = []

    def factory(settings):
        new = FakeTranscriber(service)
        new.language = settings.language
        created.append(new)
        return new

    service.transcriber_factory = factory
    service.pressed()
    wait_for(lambda: service.controller.state == AppState.RECORDING)
    settings = replace(service.settings, language="uk", microphone="USB", whisper_model="tiny")
    service.update_settings(settings)
    wait_for(lambda: service._pending_settings is not None)
    assert service.settings.whisper_model == "small" and not created
    service.released()
    wait_for(lambda: not service._busy and not service._settings_pending)
    assert service.settings == settings and service.transcriber is created[0]
    assert service.recorder.device == "USB" and service.transcriber.language == "uk"


def test_language_only_change_reuses_model(service):
    stt = FakeTranscriber(service)
    service.transcriber = stt
    service.transcriber_factory = lambda settings: (_ for _ in ()).throw(
        AssertionError("unnecessary model reload")
    )
    service.update_settings(replace(service.settings, language="en"))
    wait_for(lambda: not service._settings_pending)
    assert service.transcriber is stt and stt.language == "en"


def test_newer_settings_keep_ptt_gated_until_their_own_apply_finishes(service):
    first_entered, second_entered = Event(), Event()
    finish_first, finish_second = Event(), Event()
    original = service.hotkey.set_key

    def blocked(key):
        if key == "f8":
            first_entered.set()
            assert finish_first.wait(2)
        elif key == "f9":
            second_entered.set()
            assert finish_second.wait(2)
        original(key)

    service.hotkey.set_key = blocked
    try:
        service.update_settings(replace(service.settings, push_to_talk_key="f8"))
        assert first_entered.wait(1)
        service.update_settings(replace(service.settings, push_to_talk_key="f9"))
        finish_first.set()
        assert second_entered.wait(1)
        service.pressed()
        service.released()
        assert service._settings_pending
        assert not service.recorder.opening.is_set()
        finish_second.set()
        wait_for(lambda: not service._settings_pending)
        assert service.hotkey.key == "f9"
    finally:
        finish_first.set()
        finish_second.set()


def test_capture_failure_is_logged_off_hook_without_crashing(service, caplog):
    service.inserter = FakeInserter(service)

    def fail():
        raise OSError("target lookup failed")

    service.inserter.capture_target = fail
    service.pressed()
    wait_for(lambda: service.controller.state is AppState.RECORDING)
    service.released()
    wait_for(lambda: not service._busy)
    assert "target lookup failed" in caplog.text


def test_model_replacement_is_closed_if_old_model_cleanup_fails(service):
    class Old:
        language = None

        def close(self):
            raise OSError("old model cleanup failed")

        def cancel(self):
            pass

    replacement = FakeTranscriber(service)
    service.transcriber = Old()
    service.transcriber_factory = lambda _: replacement
    service.update_settings(replace(service.settings, whisper_model="tiny"))
    wait_for(lambda: service.controller.state is AppState.ERROR and not service._settings_pending)
    assert replacement.release.is_set()
