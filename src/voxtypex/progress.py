from collections import deque
from threading import Lock

from .interfaces import ProgressEvent


class ProgressBuffer:
    def __init__(self, capacity: int = 64):
        if capacity < 1:
            raise ValueError("Progress capacity must be positive")
        self._events = deque(maxlen=capacity)
        self._lock = Lock()
        self._request_id = None

    def begin(self, request_id: int) -> None:
        with self._lock:
            self._request_id = request_id
            self._events.clear()

    def emit(self, event: ProgressEvent) -> None:
        with self._lock:
            if event.request_id == self._request_id:
                self._events.append(event)

    def drain(self) -> tuple[ProgressEvent, ...]:
        with self._lock:
            events = tuple(self._events)
            self._events.clear()
            return events
