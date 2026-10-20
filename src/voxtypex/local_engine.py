from .local_worker import run_local_worker
from .whisper_engine import FasterWhisperEngine


class TranscribeCppEngine(FasterWhisperEngine):
    def __init__(self, cache_dir, model_id):
        super().__init__(cache_dir, model_id)
        self._force_cpu = True

    def _start(self):
        if self._process is not None and self._process.is_alive():
            return
        self._dispose()
        parent, child = self._context.Pipe()
        self._connection = parent
        self._device = "cpu"
        self._process = self._context.Process(
            target=run_local_worker,
            args=(child, self.cache_dir, self.model),
            name="voxtypex-local-asr",
            daemon=True,
        )
        try:
            self._process.start()
        finally:
            child.close()
