"""Lightweight catalogue; the model manager runs inside the ASR worker."""

from pathlib import Path

from .interfaces import ModelSpec, OperationContext, ProgressEvent, ProgressSink, ProgressStage


class WhisperModelRegistry:
    _models = (
        ModelSpec("tiny", "Tiny — быстрее"),
        ModelSpec("base", "Base — баланс"),
        ModelSpec("small", "Small — точнее"),
    )

    def models(self) -> tuple[ModelSpec, ...]:
        return self._models

    def get(self, model_id: str) -> ModelSpec:
        for model in self._models:
            if model.id == model_id:
                return model
        raise ValueError("Unsupported multilingual Whisper model")


class WhisperModelManager:
    def __init__(self, cache_dir, registry=None):
        self.cache_dir = str(cache_dir)
        self.registry = registry or WhisperModelRegistry()

    @staticmethod
    def complete(path: Path) -> bool:
        return all(
            (path / name).is_file() and (path / name).stat().st_size > 0
            for name in ("model.bin", "config.json", "tokenizer.json")
        )

    def resolve(
        self,
        model_id: str,
        local_path: str | None = None,
        *,
        context: OperationContext | None = None,
        progress: ProgressSink | None = None,
    ) -> str:
        context = context or OperationContext(0)
        context.checkpoint()
        self.registry.get(model_id)
        if local_path is not None:
            if not self.complete(Path(local_path)):
                raise FileNotFoundError("Local Whisper model is missing or incomplete")
            return str(Path(local_path))
        # Heavy imports/downloads stay in the spawned worker, never at module import.
        from faster_whisper.utils import download_model

        try:
            source = download_model(model_id, cache_dir=self.cache_dir, local_files_only=True)
            if not self.complete(Path(source)):
                raise FileNotFoundError("Cached model is incomplete")
        except Exception:
            context.checkpoint()
            if progress is not None:
                progress(ProgressEvent(context.request_id, ProgressStage.DOWNLOADING))
            source = download_model(model_id, cache_dir=self.cache_dir)
            if not self.complete(Path(source)):
                raise FileNotFoundError("Downloaded model is incomplete")
        context.checkpoint()
        return str(source)
