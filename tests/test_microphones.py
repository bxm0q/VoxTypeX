from types import SimpleNamespace

import pytest

from voxtypex.microphones import input_devices, resolve_input


def devices(names):
    entries = [{"name": name, "hostapi": 0, "max_input_channels": channels} for name, channels in names]
    return SimpleNamespace(query_hostapis=lambda: [{"name": "WASAPI"}], query_devices=lambda: entries)


def test_device_identity_survives_reordering_and_output_is_excluded():
    backend = devices([("Speakers", 0), ("USB mic", 1), ("Built-in", 1)])
    result = input_devices(backend)
    assert len(result) == 2
    identity = result[0][0]
    assert resolve_input(identity, backend) == 1
    assert resolve_input(identity, devices([("USB mic", 1), ("Built-in", 1)])) == 0
    assert resolve_input(None, backend) is None


def test_unplugged_or_ambiguous_microphone_is_not_silently_replaced():
    identity = input_devices(devices([("USB mic", 1)]))[0][0]
    with pytest.raises(RuntimeError):
        resolve_input(identity, devices([("Other", 1)]))
    with pytest.raises(RuntimeError):
        resolve_input(identity, devices([("USB mic", 1), ("USB mic", 1)]))
