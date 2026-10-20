from types import SimpleNamespace

import pytest

from voxtypex.microphones import capture_device, input_devices, open_input_lease, resolve_input


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


def test_default_capture_uses_windows_wasapi_default_instead_of_legacy_mapper():
    entries = [
        {"name": "Legacy default", "hostapi": 0, "max_input_channels": 1},
        {"name": "USB mic", "hostapi": 1, "max_input_channels": 2},
    ]
    backend = SimpleNamespace(
        query_hostapis=lambda: [
            {"name": "MME", "default_input_device": 0},
            {"name": "Windows WASAPI", "default_input_device": 1},
        ],
        query_devices=lambda: entries,
        default=SimpleNamespace(device=(0, -1)),
    )
    index, identity, label = capture_device(None, backend)
    assert index == 1 and "USB mic" in identity and "WASAPI" in identity
    assert "USB mic" in label and "WASAPI" in label
    assert "Windows" in input_devices(backend)[1][1]
    assert capture_device(identity, backend)[0] == 1


def test_no_default_device_fails_instead_of_selecting_first_input():
    backend = devices([("USB mic", 1)])
    backend.default = SimpleNamespace(device=(-1, -1))
    with pytest.raises(RuntimeError):
        capture_device(None, backend)


def test_missing_wasapi_default_cannot_fall_back_to_a_remapping_legacy_mapper():
    backend = SimpleNamespace(
        query_hostapis=lambda: [
            {"name": "MME", "default_input_device": 0},
            {"name": "Windows WASAPI", "default_input_device": -1},
        ],
        query_devices=lambda: [
            {"name": "Microsoft Sound Mapper - Input", "hostapi": 0, "max_input_channels": 2}
        ],
        default=SimpleNamespace(device=(0, -1)),
    )
    with pytest.raises(RuntimeError):
        capture_device(None, backend)


def test_refresh_is_skipped_while_any_capture_lease_is_open():
    backend = devices([("USB mic", 1)])
    calls = []
    backend._terminate = lambda: calls.append("terminate")
    backend._initialize = lambda: calls.append("initialize")
    with open_input_lease(backend):
        assert input_devices(backend, refresh=True)
        assert calls == []
    assert input_devices(backend, refresh=True)
    assert calls == ["terminate", "initialize"]


def test_nested_and_exceptional_leases_always_release():
    backend = devices([("USB mic", 1)])
    calls = []
    backend._terminate = lambda: calls.append("terminate")
    backend._initialize = lambda: calls.append("initialize")
    with pytest.raises(ValueError), open_input_lease(backend):
        with open_input_lease(backend):
            raise ValueError("closed")
    input_devices(backend, refresh=True)
    assert calls == ["terminate", "initialize"]


def test_lost_owner_cannot_implicitly_release_an_unclosed_stream(monkeypatch):
    import gc

    import voxtypex.microphones as module

    # A failed close must keep the refresh guard even if its owner is discarded.
    monkeypatch.setattr(module, "_leases", 0)
    backend = devices([("USB mic", 1)])
    calls = []
    backend._terminate = lambda: calls.append("terminate")
    backend._initialize = lambda: calls.append("initialize")
    lease = open_input_lease(backend)
    lease.__enter__()
    del lease
    gc.collect()
    input_devices(backend, refresh=True)
    assert calls == []


@pytest.mark.parametrize("recover_with", ["refresh", "lease", "capture"])
def test_failed_refresh_retries_initialize_without_a_second_terminate(recover_with):
    backend = devices([("USB mic", 1)])
    backend.query_hostapis = lambda: [{"name": "WASAPI", "default_input_device": 0}]
    backend.default = SimpleNamespace(device=(0, -1))
    calls = []
    initialized = [True]

    def terminate():
        if not initialized[0]:
            raise RuntimeError("Terminate called on uninitialized backend")
        initialized[0] = False
        calls.append("terminate")

    def initialize():
        calls.append("initialize")
        if calls.count("initialize") == 1:
            raise RuntimeError("Transient initialization failure")
        initialized[0] = True

    backend._terminate, backend._initialize = terminate, initialize
    with pytest.raises(RuntimeError, match="Transient"):
        input_devices(backend, refresh=True)
    assert not initialized[0]
    if recover_with == "refresh":
        assert input_devices(backend, refresh=True)
    elif recover_with == "capture":
        assert capture_device(None, backend)[0] == 0
    else:
        with open_input_lease(backend):
            assert initialized[0]
    assert initialized[0] and calls == ["terminate", "initialize", "initialize"]
