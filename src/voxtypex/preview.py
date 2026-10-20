import logging
import re
from dataclasses import dataclass
from threading import Event, Lock, Thread
from time import monotonic, perf_counter, process_time

from .interfaces import OperationContext, ProgressStage, TranscriptEvent, TranscriptKind
from .transcription import clean_text

logger = logging.getLogger(__name__)


class TranscriptBuffer:
    """One private UI message; stale decodes never replace a newer generation."""

    def __init__(self):
        self._lock = Lock()
        self._session = 0
        self._generation = 0
        self._sequence = 0
        self._terminal = False
        self._latest = None

    def begin(self, session_id):
        with self._lock:
            self._session = session_id
            self._generation = 1
            self._reset()
            return self._generation

    def advance(self, session_id):
        with self._lock:
            if session_id != self._session:
                return None
            self._generation += 1
            self._reset()
            return self._generation

    def _reset(self):
        self._sequence = 0
        self._terminal = False
        self._latest = TranscriptEvent(self._session, self._generation, 0, TranscriptKind.RESET)

    def emit(self, event):
        with self._lock:
            if (
                (event.session_id, event.generation) != (self._session, self._generation)
                or event.sequence <= self._sequence or self._terminal
            ):
                return
            self._sequence = event.sequence
            self._terminal = event.kind is TranscriptKind.FINAL
            self._latest = event

    def drain(self):
        with self._lock:
            result = () if self._latest is None else (self._latest,)
            self._latest = None
            return result


@dataclass(frozen=True)
class PreviewHypothesis:
    stable_text: str = ""
    unstable_text: str = ""

    @property
    def text(self):
        return " ".join(filter(None, (self.stable_text, self.unstable_text)))


def normalized(words):
    return [re.sub(r"[\W_]+", "", word).casefold() for word in words]


class PreviewText:
    def __init__(self):
        self._committed = []
        self._agreed = []
        self._previous = []
        self._overlap = False

    def begin_window(self, *, overlap):
        self._agreed = []
        self._previous = []
        self._overlap = overlap

    def update(self, text):
        words = clean_text(text).split()
        if self._overlap:
            old, new = normalized(self._committed), normalized(words)
            for count in range(min(24, len(old), len(new)), 0, -1):
                if old[-count:] == new[:count]:
                    words = words[count:]
                    break
        common = 0
        for old, new in zip(normalized(self._previous), normalized(words)):
            if old != new:
                break
            common += 1
        stable = max(len(self._agreed), min(common, max(0, len(words) - 2)))
        self._agreed.extend(words[len(self._agreed):stable])
        self._previous = words
        return PreviewHypothesis(
            " ".join(self._committed + self._agreed), " ".join(words[len(self._agreed):]),
        )

    def commit(self, text):
        result = self.update(text)
        self._committed = result.text.split()
        self.begin_window(overlap=False)
        return PreviewHypothesis(" ".join(self._committed))


@dataclass
class PreviewMetrics:
    calls: int = 0
    fallbacks: int = 0
    first_hypothesis_ms: float | None = None
    first_visible_ms: float | None = None
    inference_seconds: float = 0.0
    parent_cpu_seconds: float = 0.0
    worker_cpu_seconds: float = 0.0
    worker_cpu_measured: bool = False
    unmeasured_calls: int = 0
    elapsed_seconds: float = 0.0
    final_latency_ms: float | None = None

    @property
    def cpu_percent(self):
        if not self.elapsed_seconds:
            return 0.0
        return 100 * (self.parent_cpu_seconds + self.worker_cpu_seconds) / self.elapsed_seconds


class PreviewWorker:
    def __init__(
        self, pipeline, audio_source, session_id, generation, emit, *, interval,
        window_seconds, requested_at, cancelled=None, changed=None,
    ):
        self.pipeline, self.audio_source, self.emit = pipeline, audio_source, emit
        self.session_id, self.generation = session_id, generation
        self.interval, self.maximum_samples = interval, int(window_seconds * 16000)
        self.requested_at = requested_at
        self.cancelled, self.changed = cancelled or Event(), changed or Event()
        self.metrics = PreviewMetrics()
        self.error = ""
        self._stop = Event()
        self._thread = Thread(target=self._run, name="voxtypex-preview", daemon=True)

    def start(self):
        self._thread.start()

    def cancel(self):
        self._stop.set()

    def stop(self, timeout=8):
        self.cancel()
        if self._thread.ident is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise TimeoutError("Preview inference has not stopped; final decode is blocked")

    def _stopped(self):
        return self._stop.is_set() or self.cancelled.is_set() or self.changed.is_set()

    def _run(self):
        started, cpu_started = perf_counter(), process_time()
        text = PreviewText()
        cursor = last_end = sequence = 0
        last_hypothesis = ""
        due = 0.0
        try:
            while not self._stopped():
                if self._stop.wait(0.03):
                    break
                if monotonic() < due:
                    continue
                snapshot = self.audio_source(cursor, self.maximum_samples)
                if snapshot is None or snapshot.end_sample <= last_end:
                    continue
                sealed = snapshot.segment_final and snapshot.end_sample >= snapshot.speech_end_sample
                full = snapshot.audio.samples >= self.maximum_samples
                if snapshot.audio.duration < 0.3:
                    if sealed:
                        cursor, last_end = snapshot.end_sample, 0
                        text.begin_window(overlap=False)
                        last_hypothesis = ""
                    continue
                if snapshot.audio.duration < 1 and not sealed:
                    continue
                if snapshot.audio.speech_detected is not True:
                    continue
                decode_started = monotonic()
                context = OperationContext(self.session_id, self._stop, decode_started + 60)

                def progress(event):
                    if event.stage is ProgressStage.FALLBACK:
                        self.metrics.fallbacks += 1

                result = self.pipeline.run(snapshot.audio, context, progress)
                self.metrics.calls += 1
                self.metrics.inference_seconds += monotonic() - decode_started
                if result.cpu_time is not None:
                    self.metrics.worker_cpu_seconds += result.cpu_time
                else:
                    self.metrics.unmeasured_calls += 1
                self.metrics.worker_cpu_measured = self.metrics.unmeasured_calls == 0
                if self._stopped():
                    break
                if result.error:
                    self.error = result.error_code or "backend"
                    logger.warning("Preview unavailable (%s); final dictation remains enabled", self.error)
                    break
                hypothesis = clean_text(result.text)
                last_end = snapshot.end_sample
                if not hypothesis and not (sealed or full):
                    due = monotonic() + self.interval
                    continue
                last_hypothesis = hypothesis or last_hypothesis
                if last_hypothesis:
                    parts = text.commit(last_hypothesis) if sealed or full else text.update(last_hypothesis)
                    sequence += 1
                    if self.metrics.first_hypothesis_ms is None:
                        self.metrics.first_hypothesis_ms = (perf_counter() - self.requested_at) * 1000
                    self.emit(TranscriptEvent(
                        self.session_id, self.generation, sequence, TranscriptKind.PARTIAL,
                        parts.stable_text, parts.unstable_text,
                    ))
                if sealed or full:
                    cursor = snapshot.end_sample if sealed else snapshot.end_sample - 16000
                    text.begin_window(overlap=not sealed)
                    last_hypothesis = ""
                    last_end = 0
                elapsed = monotonic() - decode_started
                # A slow CPU spends at most about half its time on repeated windows.
                due = monotonic() + max(self.interval - elapsed, elapsed)
        except Exception as exc:
            self.error = type(exc).__name__
            logger.warning("Preview stopped (%s); final dictation remains enabled", self.error)
        finally:
            self.metrics.elapsed_seconds = perf_counter() - started
            self.metrics.parent_cpu_seconds = process_time() - cpu_started
