import hashlib
import importlib
import importlib.util
import io
import subprocess
import sys
import threading
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest


def module():
    assert importlib.util.find_spec("voxtypex.local_models") is not None, "Local model catalog is missing"
    return importlib.import_module("voxtypex.local_models")


def test_language_quality_selection_has_a_pinned_local_model_for_every_pair():
    registry = module().LanguageModelRegistry()
    assert isinstance(registry.models(), tuple)
    assert len(registry.models()) == 6
    for language in ("ru", "uk", "en"):
        for quality in ("compact", "accurate"):
            model = registry.for_language(language, quality)
            assert (model.language, model.quality, model.backend) == (language, quality, "transcribe-cpp")
            assert registry.get(model.id) is model
            assert model.size_bytes > 0
            assert len(model.revision) == 40
            assert all(f"/resolve/{model.revision}/" in f.url for f in model.files)
    assert registry.for_language("uk", "compact").license == "Moonshine AI Community License"
    assert registry.for_language("uk", "accurate").label.startswith("Parakeet")
    assert registry.for_language("ru", "compact").max_audio_seconds == 25
    with pytest.raises(ValueError):
        registry.for_language("ru", "unknown")
    with pytest.raises(ValueError):
        registry.get("../unknown")


PAYLOAD = b"tiny offline payload"
REVISION = "a" * 40
URL = f"https://huggingface.co/owner/repo/resolve/{REVISION}/test.gguf"


@pytest.fixture
def spec():
    m = module()
    return m.LocalModelSpec(
        id="test-compact",
        label="Test",
        language="en",
        quality="compact",
        revision=REVISION,
        files=(m.ModelFile("test.gguf", URL, len(PAYLOAD), hashlib.sha256(PAYLOAD).hexdigest()),),
        license="MIT",
        license_url="https://example.com/license",
        restrictions=(),
        max_audio_seconds=25,
    )


def manager(cache, spec):
    registry = SimpleNamespace(get=lambda model_id: spec if model_id == spec.id else None)
    return module().LanguageModelManager(cache, registry=registry)


class Response:
    def __init__(self, payload=PAYLOAD, *, status=200, headers=None, url=URL, drop_after=None):
        self.stream = io.BytesIO(payload)
        self.status, self.headers, self.url = status, headers or {}, url
        self.drop_after = drop_after

    def read(self, size):
        if self.drop_after is not None and self.stream.tell() >= self.drop_after:
            raise URLError("private server detail")
        return self.stream.read(min(size, 3))

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stream.close()


def transport(monkeypatch, responses):
    requests = []

    def open_url(request, timeout):
        requests.append((request, timeout))
        return responses.pop(0)

    monkeypatch.setattr(module(), "_open_url", open_url)
    return requests


def artifact(cache, spec, partial=False):
    path = cache / spec.id / (spec.files[0].filename + (".part" if partial else ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_resolve_never_downloads_missing_model(tmp_path, spec, monkeypatch):
    m = module()
    local = manager(tmp_path, spec)
    transport(monkeypatch, [])
    assert local.status(spec.id) == m.LocalModelState.NOT_INSTALLED
    with pytest.raises(m.ModelNotInstalledError):
        local.resolve(spec.id)
    assert not artifact(tmp_path, spec).exists()


def test_resolve_verifies_existing_file_before_readiness(tmp_path, spec):
    m = module()
    local = manager(tmp_path, spec)
    path = artifact(tmp_path, spec)
    path.write_bytes(PAYLOAD)
    assert local.status(spec.id) == m.LocalModelState.INSTALLED
    assert Path(local.resolve(spec.id)) == path
    assert local.status(spec.id) == m.LocalModelState.READY
    path.write_bytes(b"x" * len(PAYLOAD))
    with pytest.raises(m.ModelIntegrityError):
        local.resolve(spec.id)
    assert local.status(spec.id) == m.LocalModelState.BROKEN


def test_download_verifies_then_atomically_installs_and_reports_metadata(tmp_path, spec, monkeypatch):
    m = module()
    local = manager(tmp_path, spec)
    requests = transport(monkeypatch, [Response(headers={"Content-Length": str(len(PAYLOAD))})])
    events = []

    def progress(event):
        events.append(event)
        if event.state == m.LocalModelState.DOWNLOADING:
            assert local.status(spec.id) == m.LocalModelState.DOWNLOADING
            assert not artifact(tmp_path, spec).exists()

    path = Path(local.download(spec.id, on_progress=progress))
    assert path.read_bytes() == PAYLOAD
    assert not artifact(tmp_path, spec, partial=True).exists()
    assert local.status(spec.id) == m.LocalModelState.READY
    assert events[-1].received == events[-1].total == len(PAYLOAD)
    assert events[-1].state == m.LocalModelState.READY
    assert {field for field in events[-1].__dataclass_fields__} == {
        "model_id",
        "received",
        "total",
        "state",
        "message_category",
    }
    with pytest.raises(FrozenInstanceError):
        events[-1].received = 0
    assert requests[0][0].full_url == URL
    assert 0 < requests[0][1] <= 15


def test_cancel_retains_partial_and_validated_range_resumes(tmp_path, spec, monkeypatch):
    from voxtypex.interfaces import OperationCancelled, OperationContext

    local = manager(tmp_path, spec)
    context = OperationContext(1)
    requests = transport(monkeypatch, [Response()])

    def progress(event):
        if event.received >= 3:
            context.cancelled.set()

    with pytest.raises(OperationCancelled):
        local.download(spec.id, context=context, on_progress=progress)
    assert artifact(tmp_path, spec, partial=True).read_bytes() == PAYLOAD[:3]
    resumed = transport(
        monkeypatch,
        [
            Response(
                PAYLOAD[3:],
                status=206,
                headers={
                    "Content-Range": f"bytes 3-{len(PAYLOAD) - 1}/{len(PAYLOAD)}",
                    "Content-Length": str(len(PAYLOAD) - 3),
                },
            )
        ],
    )
    result = local.download(spec.id)
    assert Path(result).read_bytes() == PAYLOAD
    assert requests[0][0].get_header("Range") is None
    assert resumed[0][0].get_header("Range") == "bytes=3-"


def test_disconnect_retains_partial_and_redacts_server_detail(tmp_path, spec, monkeypatch):
    m = module()
    local = manager(tmp_path, spec)
    transport(monkeypatch, [Response(drop_after=3)])
    events = []
    with pytest.raises(m.ModelDownloadError) as error:
        local.download(spec.id, on_progress=events.append)
    assert artifact(tmp_path, spec, partial=True).read_bytes() == PAYLOAD[:3]
    assert "private server detail" not in str(error.value)
    assert events[-1].message_category == "network"
    assert local.status(spec.id) == m.LocalModelState.BROKEN


def test_premature_eof_with_announced_length_retains_resume_data(tmp_path, spec, monkeypatch):
    m = module()
    local = manager(tmp_path, spec)
    transport(monkeypatch, [Response(PAYLOAD[:3], headers={"Content-Length": str(len(PAYLOAD))})])
    with pytest.raises(m.ModelDownloadError) as error:
        local.download(spec.id)
    assert error.value.category == "network"
    assert artifact(tmp_path, spec, partial=True).read_bytes() == PAYLOAD[:3]


def test_fragmented_http_body_allows_cancel_before_buffer_fills(tmp_path, spec, monkeypatch):
    import http.client

    class Fragmented(io.RawIOBase):
        def __init__(self):
            self.data = io.BytesIO(
                b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(PAYLOAD)).encode() + b"\r\n\r\n" + PAYLOAD
            )

        def readable(self):
            return True

        def readinto(self, buffer):
            fragment = self.data.read(1)
            buffer[: len(fragment)] = fragment
            return len(fragment)

    response = http.client.HTTPResponse(SimpleNamespace(makefile=lambda *_: io.BufferedReader(Fragmented())))
    response.begin()
    response.geturl = lambda: URL
    m = module()
    monkeypatch.setattr(m, "_open_url", lambda *_: response)
    context = m.OperationContext(1)

    def progress(event):
        if event.received:
            context.cancelled.set()

    with pytest.raises(m.OperationCancelled):
        manager(tmp_path, spec).download(spec.id, context=context, on_progress=progress)
    assert artifact(tmp_path, spec, partial=True).read_bytes() == PAYLOAD[:1]


def test_disk_filling_during_transfer_reports_disk_space(tmp_path, spec, monkeypatch):
    import errno

    m = module()
    local = manager(tmp_path, spec)

    def full(*args):
        raise OSError(errno.ENOSPC, "disk filled during download")

    monkeypatch.setattr(local, "_receive", full)
    with pytest.raises(m.ModelDownloadError) as error:
        local.download(spec.id)
    assert error.value.category == "disk_space"


def test_resume_200_restarts_instead_of_appending(tmp_path, spec, monkeypatch):
    local = manager(tmp_path, spec)
    artifact(tmp_path, spec, partial=True).write_bytes(b"old")
    requests = transport(monkeypatch, [Response()])
    assert Path(local.download(spec.id)).read_bytes() == PAYLOAD
    assert requests[0][0].get_header("Range") == "bytes=3-"


@pytest.mark.parametrize("content_range", [None, "bytes 4-18/19", "bytes 3-18/*", "bytes 3-18/20"])
def test_bad_resume_range_restarts_with_a_fresh_request(tmp_path, spec, monkeypatch, content_range):
    local = manager(tmp_path, spec)
    artifact(tmp_path, spec, partial=True).write_bytes(PAYLOAD[:3])
    headers = {} if content_range is None else {"Content-Range": content_range}
    requests = transport(monkeypatch, [Response(PAYLOAD[3:], status=206, headers=headers), Response()])
    assert Path(local.download(spec.id)).read_bytes() == PAYLOAD
    assert requests[1][0].get_header("Range") is None


@pytest.mark.parametrize("payload", [b"too short", b"x" * len(PAYLOAD), PAYLOAD + b"extra"])
def test_corrupt_response_is_never_promoted(tmp_path, spec, monkeypatch, payload):
    m = module()
    local = manager(tmp_path, spec)
    transport(monkeypatch, [Response(payload)])
    with pytest.raises(m.ModelIntegrityError):
        local.download(spec.id)
    assert not artifact(tmp_path, spec).exists()
    assert local.status(spec.id) == m.LocalModelState.BROKEN


def test_insufficient_disk_space_stops_before_request(tmp_path, spec, monkeypatch):
    m = module()
    local = manager(tmp_path, spec)
    requests = transport(monkeypatch, [])
    monkeypatch.setattr(m.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(m.ModelDownloadError) as error:
        local.download(spec.id)
    assert error.value.category == "disk_space"
    assert not requests


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "../escape"),
        ("filename", "../escape.gguf"),
        ("filename", "C:escape.gguf"),
        ("filename", "CON.gguf"),
        ("url", "http://huggingface.co/model"),
        ("url", "https://evil.example/model"),
        ("url", URL.replace(REVISION, "main")),
        ("sha256", "not-a-checksum"),
    ],
)
def test_unsafe_catalog_metadata_is_rejected_before_filesystem_changes(tmp_path, spec, field, value):
    m = module()
    unsafe = (
        replace(spec, **{field: value})
        if field == "id"
        else replace(
            spec,
            files=(replace(spec.files[0], **{field: value}),),
        )
    )
    local = manager(tmp_path, unsafe)
    with pytest.raises(m.ModelSecurityError):
        local.download(unsafe.id)
    assert not list(tmp_path.iterdir())


def test_download_rejects_untrusted_final_redirect_url(tmp_path, spec, monkeypatch):
    m = module()
    local = manager(tmp_path, spec)
    transport(monkeypatch, [Response(url="https://evil.example/weights")])
    with pytest.raises(m.ModelSecurityError):
        local.download(spec.id)
    assert not artifact(tmp_path, spec).exists()


def test_same_model_download_lock_is_nonblocking_and_preserves_cancellation(tmp_path, spec, monkeypatch):
    from voxtypex.interfaces import OperationCancelled, OperationContext

    m = module()
    local, second = manager(tmp_path, spec), manager(tmp_path, spec)
    transport(monkeypatch, [Response()])
    attempted = threading.Event()

    def progress(event):
        if event.received and not attempted.is_set():
            attempted.set()
            with pytest.raises(m.ModelBusyError):
                second.download(spec.id)
            cancelled = OperationContext(2)
            cancelled.cancelled.set()
            with pytest.raises(OperationCancelled):
                second.download(spec.id, context=cancelled)

    assert Path(local.download(spec.id, on_progress=progress)).read_bytes() == PAYLOAD
    assert attempted.is_set()


def test_symlink_partial_cannot_write_outside_cache(tmp_path, spec, monkeypatch):
    m = module()
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    part = artifact(tmp_path / "cache", spec, partial=True)
    try:
        part.symlink_to(outside)
    except OSError:
        pytest.skip("Windows does not permit symlink creation for this account")
    local = manager(tmp_path / "cache", spec)
    transport(monkeypatch, [])
    with pytest.raises(m.ModelSecurityError):
        local.download(spec.id)
    assert outside.read_bytes() == b"keep"


def test_current_huggingface_xet_cdn_redirect_is_accepted(tmp_path, spec, monkeypatch):
    local = manager(tmp_path, spec)
    transport(monkeypatch, [Response(url="https://us.aws.cdn.hf.co/xet-bridge-us/weights?signature=opaque")])
    assert Path(local.download(spec.id)).read_bytes() == PAYLOAD


def test_cross_process_download_lock_is_nonblocking_and_released(tmp_path, spec, monkeypatch):
    m = module()
    lock_path = artifact(tmp_path, spec).parent / ".download.lock"
    script = """
import os, sys
handle = open(sys.argv[1], 'w+b')
handle.write(b'1'); handle.flush(); handle.seek(0)
if os.name == 'nt':
    import msvcrt
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
else:
    import fcntl
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
print('locked', flush=True)
sys.stdin.buffer.read()
handle.close()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    local = manager(tmp_path, spec)
    requests = transport(monkeypatch, [Response()])
    try:
        assert process.stdout.readline().strip() == b"locked"
        assert local.status(spec.id) == m.LocalModelState.DOWNLOADING
        with pytest.raises(m.ModelBusyError):
            local.download(spec.id)
        assert not requests
    finally:
        process.communicate(timeout=10)
    assert process.returncode == 0
    assert Path(local.download(spec.id)).read_bytes() == PAYLOAD


def test_pre_cancelled_download_does_not_touch_cache(tmp_path, spec, monkeypatch):
    from voxtypex.interfaces import OperationCancelled, OperationContext

    context = OperationContext(1)
    context.cancelled.set()
    transport(monkeypatch, [])
    with pytest.raises(OperationCancelled):
        manager(tmp_path, spec).download(spec.id, context=context)
    assert not list(tmp_path.iterdir())


def test_complete_partial_is_verified_and_promoted_without_network(tmp_path, spec, monkeypatch):
    local = manager(tmp_path, spec)
    artifact(tmp_path, spec, partial=True).write_bytes(PAYLOAD)
    requests = transport(monkeypatch, [])
    assert Path(local.download(spec.id)).read_bytes() == PAYLOAD
    assert not requests


def test_untrusted_redirect_is_rejected_before_requesting_target():
    from urllib.request import Request

    m = module()
    with pytest.raises(m.ModelSecurityError):
        m._TrustedRedirectHandler().redirect_request(
            Request(URL),
            None,
            302,
            "Found",
            {},
            "http://evil.example/weights",
        )


def test_cache_junction_is_rejected_without_needing_symlink_privileges(tmp_path, spec, monkeypatch):
    m = module()
    cache = tmp_path / "junction"
    cache.mkdir()
    original = Path.lstat

    def lstat(path):
        if path == cache:
            return SimpleNamespace(st_mode=0o40755, st_file_attributes=0x400)
        return original(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(m.ModelSecurityError):
        manager(cache, spec).download(spec.id)
    assert not list(cache.iterdir())
