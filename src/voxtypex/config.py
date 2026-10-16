import json
import logging
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .key_policy import KEY_CODES


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    schema_version: int = 4
    push_to_talk_key: str = "right_alt"
    debug_wav: bool = False
    whisper_model: str = "small"
    language: str | None = None
    model_path: str | None = None
    microphone: str | None = None
    visual_indication: bool = True
    enabled: bool = True
    asr_backend: str = "faster-whisper"
    transcription_timeout_seconds: float = 900.0
    ru_quality: str = "compact"
    uk_quality: str = "compact"
    en_quality: str = "compact"

    def __post_init__(self) -> None:
        if self.whisper_model not in ("tiny", "base", "small"):
            raise ConfigError("whisper_model must be tiny, base or small (multilingual)")
        if self.language not in (None, "ru", "uk", "en"):
            raise ConfigError("language must be null, ru, uk or en")
        if self.model_path is not None and (
            not isinstance(self.model_path, str) or not self.model_path.strip()
        ):
            raise ConfigError("model_path must be a non-empty local directory or null")
        if any(type(value) is not bool for value in (self.debug_wav, self.visual_indication, self.enabled)):
            raise ConfigError("debug_wav, visual_indication and enabled must be booleans")
        if self.microphone is not None and (
            not isinstance(self.microphone, str) or not self.microphone.strip()
        ):
            raise ConfigError("microphone must be a device identifier or null")
        if type(self.schema_version) is not int or self.schema_version != 4:
            raise ConfigError("Unsupported settings version")
        if self.asr_backend not in ("faster-whisper", "transcribe-cpp"):
            raise ConfigError("Unsupported ASR backend")
        if any(
            value not in ("compact", "accurate")
            for value in (self.ru_quality, self.uk_quality, self.en_quality)
        ):
            raise ConfigError("Language model quality must be compact or accurate")
        if (
            type(self.transcription_timeout_seconds) not in (int, float)
            or not math.isfinite(self.transcription_timeout_seconds)
            or not 1 <= self.transcription_timeout_seconds <= 3600
        ):
            raise ConfigError("transcription_timeout_seconds must be between 1 and 3600")
        if not isinstance(self.push_to_talk_key, str) or self.push_to_talk_key not in KEY_CODES:
            raise ConfigError("Unsupported Push-to-Talk key")


def data_directory() -> Path:
    override = os.environ.get("VOXTYPEX_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "VoxTypeX"


def model_directory() -> Path:
    override = os.environ.get("VOXTYPEX_MODEL_DIR")
    if override:
        return Path(override).expanduser().resolve()
    # Share the model cache across development profiles to avoid duplicate downloads.
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "VoxTypeX" / "models"


class SettingsService:
    """Single persistence boundary: validated defaults, migrations and atomic saves.

    Owned by the UI thread. Workers receive immutable Settings snapshots only.
    A newer or corrupt file is never silently overwritten with defaults.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else data_directory() / "settings.json"
        self.current = Settings()

    def load(self) -> Settings:
        if not self.path.exists():
            self.current = Settings()
            return self.current
        original = self.path.read_bytes()
        settings, version = self._decode(original)
        if version < 4:
            self._backup(original, version)
            self._write(settings, original)
        self.current = settings
        return settings

    def _decode(self, original: bytes) -> tuple[Settings, int]:
        def unique_object(pairs):
            raw = {}
            for key, value in pairs:
                if key in raw:
                    raise ConfigError("Duplicate settings key")
                raw[key] = value
            return raw

        try:
            raw = json.loads(original.decode("utf-8"), object_pairs_hook=unique_object)
            if not isinstance(raw, dict):
                raise ConfigError("Settings must be a JSON object")
            version = raw.get("schema_version", 1)
            if type(version) is not int or version not in (1, 2, 3, 4):
                raise ConfigError("Unsupported settings version; use a compatible VoxTypeX version")
            if version < 4 and any(key in raw for key in ("ru_quality", "uk_quality", "en_quality")):
                raise ConfigError("Unknown language profile fields in legacy settings")
            if version < 3:
                # No invented translation/AI fields: only implemented v3 defaults.
                if "asr_backend" in raw or "transcription_timeout_seconds" in raw:
                    raise ConfigError("Unknown fields in legacy settings")
            if version < 4:
                raw = dict(raw, schema_version=4)
            settings = Settings(**raw)
            return settings, version
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"Invalid settings file: {self.path} ({type(exc).__name__})") from exc

    def _backup(self, original: bytes, version: int) -> None:
        backup = self.path.with_name(self.path.name + f".v{version}.bak")
        number = 0
        while True:
            try:
                with backup.open("xb") as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
                return
            except FileExistsError:
                if backup.read_bytes() == original:
                    return
                number += 1
                backup = self.path.with_name(self.path.name + f".v{version}.bak.{number}")

    def save(self, settings: Settings) -> None:
        settings = Settings(**asdict(settings))
        original = self.path.read_bytes() if self.path.exists() else None
        if original is not None:
            _, version = self._decode(original)
            if version < 4:
                self._backup(original, version)
        self._write(settings, original)

    def _write(self, settings: Settings, original: bytes | None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(asdict(settings), stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            # Do not overwrite a file replaced/edited while this save was prepared.
            current = self.path.read_bytes() if self.path.exists() else None
            if current != original:
                raise ConfigError("Settings changed on disk; reload before saving")
            os.replace(temporary, self.path)
            self.current = settings
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    logging.getLogger(__name__).warning(
                        "Could not remove temporary settings file: %s", temporary, exc_info=True
                    )


# Legacy name.
SettingsStore = SettingsService
