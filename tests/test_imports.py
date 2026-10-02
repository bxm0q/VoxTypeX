import importlib

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "config",
        "interfaces",
        "state",
        "logging_setup",
        "ui.settings_window",
        "ui.tray",
        "main",
        "audio",
        "hotkeys",
        "recording_service",
        "transcription",
        "whisper_engine",
        "whisper_worker",
    ],
)
def test_module_import(module):
    assert importlib.import_module(f"voxtypex.{module}") is not None
