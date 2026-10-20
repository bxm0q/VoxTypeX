import json
from threading import RLock

_lock = RLock()
_leases = 0
_uninitialized_backends = {}


def _backend(backend):
    if backend is None:
        import sounddevice as backend
    return backend


def _ensure_initialized(backend):
    """Retry Pa_Initialize after failure; caller holds _lock."""
    if id(backend) not in _uninitialized_backends:
        return
    if _leases:
        raise RuntimeError("Audio backend recovery must wait until all streams are closed")
    backend._initialize()
    del _uninitialized_backends[id(backend)]


class _InputLease:
    def __init__(self, backend):
        self.backend = backend
        self.entered = False

    def __enter__(self):
        global _leases
        with _lock:
            if self.entered:
                raise RuntimeError("Audio lease is already held")
            _ensure_initialized(self.backend)
            _leases += 1
            self.entered = True
        return self.backend

    def __exit__(self, *_):
        global _leases
        with _lock:
            if self.entered:
                _leases -= 1
                self.entered = False


def open_input_lease(backend=None):
    """Block PortAudio refresh until stream.close() succeeds and the lease is released."""
    return _InputLease(_backend(backend))


def _default_index(backend, hosts, devices):
    wasapi = False
    for host in hosts:
        if "wasapi" in host["name"].lower():
            wasapi = True
            index = host.get("default_input_device", host.get("defaultInputDevice", -1))
            if isinstance(index, int) and 0 <= index < len(devices):
                if devices[index]["max_input_channels"] > 0:
                    return index
    if wasapi:
        # A legacy mapper may silently switch physical inputs.
        return None
    try:
        index = backend.default.device[0]
        if isinstance(index, int) and 0 <= index < len(devices):
            if devices[index]["max_input_channels"] > 0:
                return index
    except (AttributeError, TypeError, IndexError):
        pass
    return None


def _inventory(backend):
    hosts, devices = backend.query_hostapis(), backend.query_devices()
    default = _default_index(backend, hosts, devices)
    found = []
    for index, device in enumerate(devices):
        channels = device["max_input_channels"]
        if channels < 1:
            continue
        host = hosts[device["hostapi"]]["name"]
        identity = json.dumps([host, device["name"]], ensure_ascii=False)
        label = f"{device['name']} — {host} · вход, каналов: {channels}"
        if index == default:
            label += " · по умолчанию Windows"
        found.append((identity, label, index))
    return found, default


def input_devices(backend=None, refresh=False):
    """Return (identity, label, index); refresh only when no streams are open."""
    backend = _backend(backend)
    with _lock:
        recovering = id(backend) in _uninitialized_backends
        _ensure_initialized(backend)
        if refresh and _leases == 0 and not recovering:
            terminate = getattr(backend, "_terminate", None)
            initialize = getattr(backend, "_initialize", None)
            if callable(terminate) and callable(initialize):
                # sounddevice exposes refresh only through private init/terminate calls.
                terminate()
                _uninitialized_backends[id(backend)] = backend
                _ensure_initialized(backend)
        return _inventory(backend)[0]


def resolve_input(identity, backend):
    if identity is None:
        return None
    matches = [index for key, _, index in input_devices(backend) if key == identity]
    if len(matches) != 1:
        raise RuntimeError(
            "Выбранный микрофон недоступен или неоднозначен. Выберите устройство в настройках."
        )
    return matches[0]


def capture_device(identity, backend=None):
    """Resolve one physical input under a lease; fail if missing or ambiguous."""
    backend = _backend(backend)
    with _lock:
        _ensure_initialized(backend)
        devices, default = _inventory(backend)
        if identity is None:
            matches = [item for item in devices if item[2] == default]
            if not matches:
                raise RuntimeError(
                    "Микрофон по умолчанию Windows недоступен. Выберите устройство в настройках."
                )
            identity = matches[0][0]
        matches = [item for item in devices if item[0] == identity]
        if len(matches) != 1:
            raise RuntimeError(
                "Выбранный микрофон недоступен или неоднозначен. Выберите устройство в настройках."
            )
        key, label, index = matches[0]
        return index, key, label


class DefaultInputBinding:
    """Pin one default input for PTT and the microphone test."""

    def __init__(self):
        self._identity = None

    def capture(self, backend):
        with _lock:
            result = capture_device(self._identity, backend)
            self._identity = result[1]
            return result

    def reset(self):
        with _lock:
            self._identity = None
