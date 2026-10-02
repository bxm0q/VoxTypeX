import json


def input_devices(backend=None):
    if backend is None:
        import sounddevice as backend
    hosts = backend.query_hostapis()
    found = []
    for index, device in enumerate(backend.query_devices()):
        if device["max_input_channels"] < 1:
            continue
        host = hosts[device["hostapi"]]["name"]
        identity = json.dumps([host, device["name"]], ensure_ascii=False)
        found.append((identity, f"{device['name']} — {host}", index))
    return found


def resolve_input(identity, backend):
    if identity is None:
        return None
    matches = [index for key, _, index in input_devices(backend) if key == identity]
    if len(matches) != 1:
        raise RuntimeError(
            "Выбранный микрофон недоступен или неоднозначен. Выберите устройство в настройках."
        )
    return matches[0]
