import pytest

from voxtypex.config import ConfigError, Settings, SettingsStore


def test_roundtrip(tmp_path):
    store = SettingsStore(tmp_path / "nested" / "settings.json")
    assert store.load() == Settings()
    settings = Settings(push_to_talk_key="f8")
    store.save(settings)
    assert store.load() == settings
    assert list(store.path.parent.iterdir()) == [store.path]


@pytest.mark.parametrize(
    "content",
    ["{broken", "[]", '{"schema_version": 99}', '{"push_to_talk_key": "unknown"}', '{"extra": true}'],
)
def test_invalid_config_preserved(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        SettingsStore(path).load()
    assert path.read_text(encoding="utf-8") == content


def test_failed_replace_preserves_settings(tmp_path, monkeypatch):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings())

    def fail(*args):
        raise PermissionError("locked")

    monkeypatch.setattr("voxtypex.config.os.replace", fail)
    with pytest.raises(PermissionError):
        store.save(Settings(push_to_talk_key="f8"))
    assert store.load() == Settings()
    assert len(list(tmp_path.iterdir())) == 1


def test_v1_migration_preserves_values_and_exact_backup(tmp_path):
    path = tmp_path / "settings.json"
    original = (
        '{"schema_version":1,"push_to_talk_key":"f8","language":"uk","whisper_model":"base","debug_wav":true}'
    )
    path.write_text(original, encoding="utf-8")
    store = SettingsStore(path)
    value = store.load()
    assert value.schema_version == 2 and value.language == "uk" and value.debug_wav
    assert value.enabled and value.visual_indication and value.microphone is None
    assert path.with_name("settings.json.v1.bak").read_text(encoding="utf-8") == original
    assert store.load() == value


def test_new_settings_persist_across_service_instances(tmp_path):
    path = tmp_path / "settings.json"
    value = Settings(
        push_to_talk_key="mouse4",
        microphone='["WASAPI", "USB microphone"]',
        language="en",
        whisper_model="tiny",
        visual_indication=False,
        enabled=False,
    )
    SettingsStore(path).save(value)
    assert SettingsStore(path).load() == value


def test_failed_migration_keeps_original_and_backup(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"schema_version":1}', encoding="utf-8")
    original = path.read_bytes()
    monkeypatch.setattr(
        "voxtypex.config.os.replace", lambda *args: (_ for _ in ()).throw(PermissionError("locked"))
    )
    with pytest.raises(PermissionError):
        SettingsStore(path).load()
    assert path.read_bytes() == original
    assert path.with_name("settings.json.v1.bak").read_bytes() == original


@pytest.mark.parametrize(
    "fields",
    [
        {"enabled": 1},
        {"visual_indication": "yes"},
        {"microphone": 3},
        {"push_to_talk_key": "ctrl+alt+delete"},
        {"push_to_talk_key": "f4"},
    ],
)
def test_new_fields_are_validated(fields):
    with pytest.raises(ConfigError):
        Settings(**fields)


def test_migration_never_overwrites_different_existing_backup(tmp_path):
    path = tmp_path / "settings.json"
    original = '{"schema_version":1,"language":"en"}'
    path.write_text(original, encoding="utf-8")
    backup = path.with_name("settings.json.v1.bak")
    backup.write_text("older backup", encoding="utf-8")
    assert SettingsStore(path).load().language == "en"
    assert backup.read_text(encoding="utf-8") == "older backup"
    assert path.with_name("settings.json.v1.bak.1").read_text(encoding="utf-8") == original


def test_cleanup_failure_does_not_hide_original_write_failure(tmp_path, monkeypatch, caplog):
    from pathlib import Path

    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings())
    original = store.path.read_bytes()

    def replace_failed(*_):
        raise PermissionError("replace locked")

    def cleanup_failed(*_, **__):
        raise OSError("cleanup locked")

    with monkeypatch.context() as patch:
        patch.setattr("voxtypex.config.os.replace", replace_failed)
        patch.setattr(Path, "unlink", cleanup_failed)
        with pytest.raises(PermissionError, match="replace locked"):
            store.save(Settings(language="uk"))
    assert store.current == Settings()
    assert store.path.read_bytes() == original
    assert "Could not remove temporary settings" in caplog.text


def test_invalid_utf8_preserves_disk_and_current_value(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(language="uk"))
    store.path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ConfigError):
        store.load()
    assert store.current.language == "uk"
    assert store.path.read_bytes() == b"\xff\xfe\x00"
