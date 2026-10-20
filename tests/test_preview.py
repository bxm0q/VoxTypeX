from threading import Event
from time import monotonic

import pytest
from test_recording_service import FakeInserter, wait_for
from test_recording_service import service as service

from voxtypex.config import ConfigError, Settings, SettingsStore
from voxtypex.interfaces import AudioClip, TranscriptionResult


def test_preview_keeps_agreed_prefix_and_replaces_only_the_tail():
    from voxtypex.preview import PreviewText

    text = PreviewText()
    assert text.update("Привет мир сегодня").text == "Привет мир сегодня"
    result = text.update("Привет мир сегодня вечером")
    assert result.stable_text == "Привет мир"
    assert result.unstable_text == "сегодня вечером"
    result = text.update("Привет мир завтра вечером")
    assert result.stable_text == "Привет мир"
    assert result.text == "Привет мир завтра вечером"
    text.commit("Привет мир завтра вечером")
    text.begin_window(overlap=True)
    assert text.update("завтра вечером мы продолжим").text == "Привет мир завтра вечером мы продолжим"


def test_new_vad_phrase_preserves_intentional_repeated_words():
    from voxtypex.preview import PreviewText

    text = PreviewText()
    text.commit("Да да")
    text.begin_window(overlap=False)
    assert text.update("Да да конечно").text == "Да да Да да конечно"


def test_transcript_mailbox_rejects_stale_generations_and_post_final_partials():
    from voxtypex.interfaces import TranscriptEvent, TranscriptKind
    from voxtypex.preview import TranscriptBuffer

    box = TranscriptBuffer()
    generation = box.begin(10)
    box.emit(TranscriptEvent(10, generation, 2, TranscriptKind.PARTIAL, unstable_text="new"))
    box.emit(TranscriptEvent(10, generation, 1, TranscriptKind.PARTIAL, unstable_text="stale"))
    assert box.drain()[-1].text == "new"
    generation = box.advance(10)
    box.emit(TranscriptEvent(10, generation - 1, 99, TranscriptKind.PARTIAL, unstable_text="late"))
    box.emit(TranscriptEvent(10, generation, 1, TranscriptKind.FINAL, stable_text="final"))
    box.emit(TranscriptEvent(10, generation, 2, TranscriptKind.PARTIAL, unstable_text="wrong"))
    assert box.drain()[-1].text == "final"
    box.begin(11)
    box.emit(TranscriptEvent(10, generation, 100, TranscriptKind.FINAL, stable_text="old session"))
    assert all(not e.text for e in box.drain())


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_preview_migration_preserves_original_settings(tmp_path, version):
    path = tmp_path / "settings.json"
    original = ('{"schema_version":%d,"language":"uk","whisper_model":"tiny"}' % version).encode()
    path.write_bytes(original)
    value = SettingsStore(path).load()
    assert value.preview_mode == "preview"
    assert value.schema_version == 6 and value.language == "uk" and value.whisper_model == "tiny"
    assert path.with_name(f"settings.json.v{version}.bak").read_bytes() == original
    assert SettingsStore(path).load() == value


@pytest.mark.parametrize("fields", [
    {"preview_mode": "live"}, {"preview_mode": "streaming"},
    {"preview_interval_seconds": 0.1}, {"preview_interval_seconds": True},
    {"preview_interval_seconds": float("nan")}, {"preview_window_seconds": 3},
    {"preview_window_seconds": 31},
])
def test_preview_settings_reject_unsafe_modes_and_unbounded_work(fields):
    with pytest.raises(ConfigError):
        Settings(**fields)


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_legacy_preview_fields_are_not_silently_overwritten(tmp_path, version):
    path = tmp_path / "settings.json"
    original = ('{"schema_version":%d,"preview_mode":"off"}' % version).encode()
    path.write_bytes(original)
    with pytest.raises(ConfigError):
        SettingsStore(path).load()
    assert path.read_bytes() == original and list(tmp_path.iterdir()) == [path]


def test_cancelled_preview_cannot_publish_its_late_result():
    from voxtypex.interfaces import AudioPreview, EngineCapabilities
    from voxtypex.preview import PreviewWorker

    entered, release = Event(), Event()
    events = []

    class Pipeline:
        capabilities = EngineCapabilities(batch_preview=True)

        def run(self, audio, context, progress=None):
            entered.set()
            assert release.wait(2)
            return TranscriptionResult(text="private late result")

    snapshot = AudioPreview(AudioClip(b"\x10\x00" * 32000, 16000, speech_detected=True), 0, 32000, 32000, 32000)
    worker = PreviewWorker(Pipeline(), lambda *args: snapshot, 1, 1, events.append,
                           interval=1, window_seconds=8, requested_at=monotonic())
    worker.start()
    try:
        assert entered.wait(1)
        worker.cancel()
        release.set()
        worker.stop()
        assert not any(e.text for e in events)
    finally:
        release.set()
        worker.stop()


def test_capture_preview_is_bounded_and_does_not_finish_the_recording():
    from voxtypex.capture_processing import CaptureProcessor

    class Speech:
        def is_speech(self, pcm, rate):
            return any(pcm)

    processor = CaptureProcessor(speech_detector=Speech(), presence_detector=Speech())
    pcm = b"\x10\x00\xf0\xff" * 16000
    try:
        processor.push(pcm)
        first = processor.preview_audio(0, 16000)
        assert first.audio.pcm == pcm[:32000]
        assert first.end_sample == 16000 and first.captured_samples == 32000
        processor.push(b"\0\0" * 16000)
        second = processor.preview_audio(16000, 16000)
        assert second.audio.pcm == pcm[32000:]
        assert processor.finish().samples > 32000
    finally:
        processor.close()


@pytest.mark.parametrize("first_samples", [3200, 9600])
def test_empty_or_short_closed_vad_span_cannot_block_later_speech(first_samples):
    from voxtypex.interfaces import AudioPreview
    from voxtypex.preview import PreviewWorker

    events = []
    later = Event()

    class Pipeline:
        def run(self, audio, context, progress=None):
            return TranscriptionResult(text="следующая фраза" if audio.samples == 32000 else "")

    def source(cursor, maximum):
        samples = first_samples if cursor == 0 else 32000
        if cursor:
            later.set()
        return AudioPreview(AudioClip(b"\x10\x00" * samples, 16000, speech_detected=True),
                            cursor, cursor + samples, cursor + samples, cursor + samples, True)

    worker = PreviewWorker(Pipeline(), source, 1, 1, events.append, interval=1, window_seconds=8,
                           requested_at=monotonic())
    worker.start()
    try:
        assert later.wait(2)
        wait_for(lambda: bool(events))
        assert events[0].text == "следующая фраза"
    finally:
        worker.stop()


def test_unfinished_subsecond_audio_waits_for_context_instead_of_repeated_batch_calls():
    from voxtypex.interfaces import AudioPreview
    from voxtypex.preview import PreviewWorker

    requested, decoded = Event(), Event()
    samples = [8000]

    class Pipeline:
        def run(self, audio, context, progress=None):
            decoded.set()
            return TranscriptionResult(text="осмысленная фраза")

    def source(cursor, maximum):
        requested.set()
        return AudioPreview(AudioClip(b"\x10\x00" * samples[0], 16000, speech_detected=True),
                            0, samples[0], samples[0], samples[0])

    worker = PreviewWorker(Pipeline(), source, 1, 1, lambda event: None, interval=1, window_seconds=8,
                           requested_at=monotonic())
    worker.start()
    try:
        assert requested.wait(1)
        assert not decoded.wait(0.1)
        samples[0] = 20000
        assert decoded.wait(1)
    finally:
        worker.stop()


def test_empty_segment_ending_preserves_last_hypothesis_and_resets_next_phrase():
    from voxtypex.interfaces import AudioPreview
    from voxtypex.preview import PreviewWorker

    events = []
    frames = iter([(0, 16000, False), (0, 32000, False), (0, 40000, True), (40000, 88000, False)])

    class Pipeline:
        def run(self, audio, context, progress=None):
            return TranscriptionResult(text={16000: "Первая фраза полностью", 32000: "Первая фраза полностью",
                                              40000: "", 48000: "Другая речь"}[audio.samples])

    def source(cursor, maximum):
        try:
            start, end, sealed = next(frames)
        except StopIteration:
            return None
        return AudioPreview(AudioClip(b"\x10\x00" * (end - start), 16000, speech_detected=True),
                            start, end, end, end, sealed)

    worker = PreviewWorker(Pipeline(), source, 1, 1, events.append, interval=0.01, window_seconds=8,
                           requested_at=monotonic())
    worker.start()
    try:
        wait_for(lambda: any("Другая" in e.text for e in events))
        assert events[-1].text == "Первая фраза полностью Другая речь"
        assert worker.metrics.unmeasured_calls > 0
        assert not worker.metrics.worker_cpu_measured
    finally:
        worker.stop()


def preview_pipeline(service):
    from dataclasses import replace

    from voxtypex.interfaces import AudioPreview, EngineCapabilities
    from voxtypex.transcription import TranscriptionService

    class Engine:
        capabilities = EngineCapabilities(batch_preview=True)
        concurrent = peak = partials = finals = 0

        def transcribe(self, audio, language=None, *, context=None, progress=None):
            self.concurrent += 1
            self.peak = max(self.peak, self.concurrent)
            try:
                context.checkpoint()
                if service.recorder.open:
                    self.partials += 1
                    return TranscriptionResult(text="первое слово" if self.partials == 1 else "первое слово изменилось")
                self.finals += 1
                return TranscriptionResult(text="Окончательная фраза.")
            finally:
                self.concurrent -= 1

        def cancel(self):
            pass

        def close(self):
            pass

    engine = Engine()
    service.settings = replace(service.settings, preview_interval_seconds=1)
    service.transcriber = TranscriptionService(engine)
    service.inserter = FakeInserter(service)
    service.recorder.clip = AudioClip(b"\x10\x00" * 48000, 16000, speech_detected=True)
    samples = [16000]

    def snapshot(start, maximum):
        clip = AudioClip(b"\x10\x00" * samples[0], 16000, speech_detected=True)
        return AudioPreview(clip, 0, samples[0], samples[0], samples[0])

    service.recorder.preview_audio = snapshot
    return engine, samples


def test_hypothesis_changes_before_release_and_only_final_is_inserted(service):
    from voxtypex.interfaces import TranscriptKind

    engine, samples = preview_pipeline(service)
    service.pressed()
    wait_for(lambda: engine.partials == 1)
    first = service.drain_transcripts()[-1]
    assert first.text == "первое слово" and first.kind is TranscriptKind.PARTIAL
    assert not service.inserter.calls
    samples[0] = 32000
    wait_for(lambda: engine.partials == 2)
    second = service.drain_transcripts()[-1]
    assert second.text == "первое слово изменилось" and second.sequence > first.sequence
    assert not service.inserter.calls and service.recorder.open
    service.released()
    wait_for(lambda: not service._busy)
    assert engine.peak == 1 and engine.finals == 1
    assert service.inserter.calls == ["Окончательная фраза."]
    event = service.drain_transcripts()[-1]
    assert event.kind is TranscriptKind.FINAL and event.generation > first.generation
    assert service.last_preview_metrics.first_hypothesis_ms is not None
    assert service.last_preview_metrics.final_latency_ms is not None


@pytest.mark.parametrize("action", ["cancel", "changed", "close"])
def test_preview_is_cleared_on_cancel_focus_change_or_shutdown(service, action):
    engine, _ = preview_pipeline(service)
    service.pressed()
    wait_for(lambda: engine.partials == 1)
    assert service.drain_transcripts()[-1].text
    {"cancel": service.cancel_current, "changed": service.context_changed,
     "close": service.request_close}[action]()
    assert not any(e.text for e in service.drain_transcripts())
    service.released()
    wait_for(lambda: not service._busy or service.finished.is_set())
    assert not any(e.text for e in service.drain_transcripts())
    assert not service.last_insert_result or not service.last_insert_result.success
