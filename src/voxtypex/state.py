import logging
from enum import Enum, auto
from threading import Lock

logger = logging.getLogger(__name__)


class AppState(Enum):
    IDLE = auto()
    RECORDING = auto()
    TRANSCRIBING = auto()
    INSERTING = auto()
    ERROR = auto()


_ALLOWED = {
    AppState.IDLE: {AppState.RECORDING, AppState.ERROR},
    AppState.RECORDING: {AppState.IDLE, AppState.TRANSCRIBING, AppState.ERROR},
    AppState.TRANSCRIBING: {AppState.INSERTING, AppState.IDLE, AppState.ERROR},
    AppState.INSERTING: {AppState.IDLE, AppState.ERROR},
    AppState.ERROR: {AppState.IDLE},
}


class AppController:
    def __init__(self) -> None:
        self._state = AppState.IDLE
        self._closed = False
        self._lock = Lock()

    @property
    def state(self) -> AppState:
        with self._lock:
            return self._state

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def transition(self, expected: AppState, target: AppState) -> bool:
        """Reject stale events and invalid transitions under the same lock."""
        with self._lock:
            if self._closed or self._state is not expected or target not in _ALLOWED[expected]:
                return False
            self._state = target
        logger.info("State: %s -> %s", expected.name, target.name)
        return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        logger.info("Controller closed")
