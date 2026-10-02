import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .key_policy import KEY_CODES


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    schema_version: int = 2
    push_to_talk_key: str = "right_alt"
    debug_wav: bool = False
    whisper_model: str = "small"
    language: str | None = None
    model_path: str | None = None
    microphone: str | None = None
    visual_indication: bool = True
    enabled: bool = True

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
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ConfigError("Unsupported settings version")
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
        try:
            original = self.path.read_bytes()
            raw = json.loads(original.decode("utf-8"))
            if not isinstance(raw, dict):
                raise ConfigError("Settings must be a JSON object")
            version = raw.get("schema_version", 1)
            if type(version) is not int or version not in (1, 2):
                raise ConfigError("Unsupported settings version; use a compatible VoxTypeX version")
            migrated = version == 1
            if migrated:
                raw = dict(raw, schema_version=2)
            settings = Settings(**raw)
            if migrated:
                # Back up the exact original before the first on-disk migration.
                backup = self.path.with_name(self.path.name + ".v1.bak")
                number = 0
                while True:
                    try:
                        with backup.open("xb") as stream:
                            stream.write(original)
                            stream.flush()
                            os.fsync(stream.fileno())
                        break
                    except FileExistsError:
                        if backup.read_bytes() == original:
                            break
                        number += 1
                        backup = self.path.with_name(self.path.name + f".v1.bak.{number}")
                self.save(settings)
            self.current = settings
            return settings
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"Invalid settings file: {self.path}: {exc}") from exc

    def save(self, settings: Settings) -> None:
        settings = Settings(**asdict(settings))
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
