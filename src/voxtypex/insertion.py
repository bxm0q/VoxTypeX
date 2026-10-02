import logging
import time
from dataclasses import dataclass
from threading import Event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InsertResult:
    success: bool = False
    method: str = ""
    error: str | None = None
    skipped: bool = False
    clipboard_restored: bool | None = None


class TextInsertionService:
    def __init__(self, backend=None, restore_delay=1.0):
        if backend is None:
            from .windows_insertion import WindowsInsertion

            backend = WindowsInsertion()
        self.backend = backend
        self.restore_delay = restore_delay
        self.target = None
        self.cancelled = Event()
        self.changed = Event()

    def capture_target(self):
        """Запоминает активное поле при нажатии PTT, не меняя фокус."""
        return self.backend.capture_target()

    def prepare_target(self, target, cancelled, changed):
        self.cancelled, self.changed = cancelled, changed
        self.target = self.backend.prepare_target(target)

    def _valid(self):
        return (
            not self.cancelled.is_set()
            and not self.changed.is_set()
            and self.target is not None
            and self.backend.target_matches(self.target)
        )

    def insert_text(self, text: str) -> InsertResult:
        if not text or not text.strip():
            return InsertResult(skipped=True)
        lease = None
        method = ""
        result = None
        restored = None
        try:
            if self.target is not None and not self.cancelled.is_set() and not self.changed.is_set():
                self.backend.dismiss_ptt_menu(self.target)
            if not self._valid():
                return InsertResult(skipped=True, error="Insertion cancelled or original field changed")
            if not self.backend.wait_modifiers(self._valid):
                return InsertResult(skipped=True, error="Release modifier keys before insertion")
            lease = self.backend.write_clipboard(text)
            if not self._valid():
                result = InsertResult(skipped=True, error="Original field changed before insertion")
            elif lease is not None:
                method = "clipboard"
                # Повтор через Unicode после Ctrl+V может продублировать текст.
                self.backend.paste()
                result = InsertResult(success=True, method=method)
            else:
                method = "unicode"
                if "\n" in text or "\r" in text:
                    result = InsertResult(
                        skipped=True,
                        method=method,
                        error="Clipboard unavailable; multiline Unicode fallback disabled",
                    )
                else:
                    self.backend.type_unicode(text, self._valid)
                    result = InsertResult(success=True, method=method)
        except Exception as exc:
            logger.exception("Text insertion failed")
            result = InsertResult(method=method, error=str(exc))
        finally:
            if lease is not None:
                # Приложение может прочитать буфер не сразу после SendInput.
                time.sleep(self.restore_delay)
                try:
                    restored = self.backend.restore_clipboard(lease)
                except Exception:
                    restored = False
                    logger.exception("Clipboard restoration failed")
        return InsertResult(result.success, result.method, result.error, result.skipped, restored)

    def close(self):
        self.backend.close()
