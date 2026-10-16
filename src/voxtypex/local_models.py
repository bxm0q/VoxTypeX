"""Pinned local speech model catalog. Importing it never loads an ASR runtime."""

import errno
import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from enum import StrEnum
from http.client import IncompleteRead
from importlib.resources import files
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .interfaces import OperationCancelled, OperationContext, OperationTimeout

_CHUNK_SIZE = 256 * 1024
_NETWORK_TIMEOUT = 15
_DISK_RESERVE = 1024 * 1024
_LOCKS_GUARD = Lock()
_DOWNLOAD_LOCKS: dict[str, Lock] = {}
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(10)),
    *(f"LPT{i}" for i in range(10)),
}


class LocalModelState(StrEnum):
    NOT_INSTALLED = "not_installed"
    INSTALLED = "installed"
    DOWNLOADING = "downloading"
    READY = "ready"
    BROKEN = "broken"


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    model_id: str
    received: int
    total: int
    state: LocalModelState
    message_category: str


class LocalModelError(RuntimeError):
    def __init__(self, message: str, category: str = "model"):
        super().__init__(message)
        self.category = category


class ModelNotInstalledError(LocalModelError, FileNotFoundError):
    pass


class ModelDownloadError(LocalModelError):
    pass


class ModelIntegrityError(LocalModelError):
    pass


class ModelSecurityError(LocalModelError):
    pass


class ModelBusyError(ModelDownloadError):
    pass


def _trusted_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise ModelSecurityError("Invalid model URL", "security") from None
    trusted = host == "huggingface.co" or host in {"cdn-lfs.huggingface.co", "cas-bridge.xethub.hf.co"}
    trusted = trusted or (host.startswith("cdn-lfs-") and host.endswith(".hf.co"))
    trusted = trusted or bool(re.fullmatch(r"[a-z0-9-]+\.aws\.cdn\.hf\.co", host))
    if (
        not trusted
        or parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        raise ModelSecurityError("Model download requires a trusted HTTPS host", "security")
    if parsed.fragment or any(ord(character) < 32 for character in url):
        raise ModelSecurityError("Invalid model URL", "security")


class _TrustedRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        _trusted_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _open_url(request: Request, timeout: float):
    _trusted_url(request.full_url)
    return build_opener(_TrustedRedirectHandler()).open(request, timeout=timeout)


def _safe_name(value: str) -> bool:
    return (
        isinstance(value, str)
        and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value))
        and value not in (".", "..")
        and not value.endswith((".", " "))
        and value.split(".")[0].upper() not in _WINDOWS_RESERVED
    )


def _safe_path(path: Path) -> None:
    if ".." in path.parts:
        raise ModelSecurityError("Unsafe model cache path", "security")
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ModelSecurityError("Model cache cannot contain links or reparse points", "security")


def _fingerprint(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino


@dataclass(frozen=True, slots=True)
class ModelFile:
    filename: str
    url: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class LocalModelSpec:
    id: str
    label: str
    language: str
    quality: str
    revision: str
    files: tuple[ModelFile, ...]
    license: str
    license_url: str
    restrictions: tuple[str, ...]
    max_audio_seconds: float | None
    backend: str = "transcribe-cpp"

    @property
    def size_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)


class LanguageModelRegistry:
    def __init__(self):
        document = json.loads(files("voxtypex").joinpath("model_catalog.json").read_text(encoding="utf-8"))
        self._models = tuple(
            LocalModelSpec(
                **{key: value for key, value in item.items() if key not in ("files", "restrictions")},
                files=tuple(ModelFile(**entry) for entry in item["files"]),
                restrictions=tuple(item["restrictions"]),
            )
            for item in document["models"]
        )

    def models(self) -> tuple[LocalModelSpec, ...]:
        return self._models

    def get(self, model_id: str) -> LocalModelSpec:
        for model in self._models:
            if model.id == model_id:
                return model
        raise ValueError("Unsupported local speech model")

    def for_language(self, language: str, quality: str = "compact") -> LocalModelSpec:
        for model in self._models:
            if (model.language, model.quality) == (language, quality):
                return model
        raise ValueError("Unsupported language or model quality")


class LanguageModelManager:
    """Synchronous downloads for a background caller; resolve is always offline.

    INSTALLED means size-complete, READY means this manager verified the SHA256.
    A cache fingerprint invalidates readiness whenever the artifact changes.
    """

    def __init__(self, cache_dir, registry=None):
        self.cache_dir = Path(cache_dir).absolute()
        self.registry = registry or LanguageModelRegistry()
        self._verified: dict[str, tuple[int, int, int, int]] = {}
        self._broken: set[str] = set()

    def _spec(self, model_id: str) -> LocalModelSpec:
        spec = self.registry.get(model_id)
        if not isinstance(spec, LocalModelSpec) or not _safe_name(spec.id) or spec.id != model_id:
            raise ModelSecurityError("Invalid local model metadata", "security")
        if not re.fullmatch(r"[0-9a-f]{40}", spec.revision) or len(spec.files) != 1:
            raise ModelSecurityError("Model requires one pinned GGUF file", "security")
        entry = spec.files[0]
        _trusted_url(entry.url)
        parsed = urlsplit(entry.url)
        path = unquote(parsed.path).split("/")
        if (
            not _safe_name(entry.filename)
            or not entry.filename.endswith(".gguf")
            or not isinstance(entry.size_bytes, int)
            or isinstance(entry.size_bytes, bool)
            or entry.size_bytes <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", entry.sha256)
            or parsed.hostname != "huggingface.co"
            or parsed.query
            or len(path) != 6
            or path[3:] != ["resolve", spec.revision, entry.filename]
            or not all(_safe_name(value) for value in path[1:3])
        ):
            raise ModelSecurityError("Invalid pinned model file metadata", "security")
        return spec

    def _paths(self, spec: LocalModelSpec) -> tuple[Path, Path]:
        directory = self.cache_dir / spec.id
        final = directory / spec.files[0].filename
        partial = directory / (spec.files[0].filename + ".part")
        _safe_path(final)
        _safe_path(partial)
        return final, partial

    def _lock(self, spec: LocalModelSpec) -> Lock:
        key = os.path.normcase(str(self.cache_dir / spec.id))
        with _LOCKS_GUARD:
            return _DOWNLOAD_LOCKS.setdefault(key, Lock())

    def _lease(self, spec, *, create):
        path = self.cache_dir / spec.id / ".download.lock"
        _safe_path(path)
        if not create and not path.exists():
            return None
        stream = path.open("a+b" if create else "r+b")
        try:
            if path.stat().st_size == 0:
                stream.write(b"1")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return stream
        except OSError:
            stream.close()
            raise ModelBusyError("Another process is installing this model", "busy") from None

    @staticmethod
    def _release_lease(stream):
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def status(self, model_id: str) -> LocalModelState:
        try:
            spec = self._spec(model_id)
            final, _ = self._paths(spec)
            if self._lock(spec).locked():
                return LocalModelState.DOWNLOADING
            try:
                lease = self._lease(spec, create=False)
            except ModelBusyError:
                return LocalModelState.DOWNLOADING
            self._release_lease(lease)
            if model_id in self._broken:
                return LocalModelState.BROKEN
            if not final.exists():
                return LocalModelState.NOT_INSTALLED
            if not final.is_file() or final.stat().st_size != spec.size_bytes:
                return LocalModelState.BROKEN
            if self._verified.get(model_id) == _fingerprint(final):
                return LocalModelState.READY
            return LocalModelState.INSTALLED
        except (ModelSecurityError, OSError):
            return LocalModelState.BROKEN

    @staticmethod
    def _emit(callback, spec, received, state, category):
        if callback is not None:
            callback(DownloadProgress(spec.id, received, spec.size_bytes, state, category))

    @staticmethod
    def _verify(path: Path, entry: ModelFile, context: OperationContext) -> None:
        _safe_path(path)
        context.checkpoint()
        if not path.is_file() or path.stat().st_size != entry.size_bytes:
            raise ModelIntegrityError("Model file has an incorrect size", "integrity")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while block := source.read(_CHUNK_SIZE):
                context.checkpoint()
                digest.update(block)
        context.checkpoint()
        if digest.hexdigest() != entry.sha256:
            raise ModelIntegrityError("Model checksum verification failed", "integrity")

    def resolve(self, model_id: str, *, context=None, progress=None) -> str:
        context = context or OperationContext(0)
        context.checkpoint()
        spec = self._spec(model_id)
        final, _ = self._paths(spec)
        if not final.exists():
            raise ModelNotInstalledError("Install the selected speech model before using it", "not_installed")
        self._emit(progress, spec, spec.size_bytes, LocalModelState.INSTALLED, "verifying")
        try:
            self._verify(final, spec.files[0], context)
        except ModelIntegrityError:
            self._broken.add(model_id)
            self._verified.pop(model_id, None)
            raise
        self._broken.discard(model_id)
        self._verified[model_id] = _fingerprint(final)
        self._emit(progress, spec, spec.size_bytes, LocalModelState.READY, "ready")
        return str(final)

    @staticmethod
    def _valid_range(response, offset: int, entry: ModelFile) -> bool:
        value = response.headers.get("Content-Range", "")
        match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", value)
        return bool(
            match and tuple(map(int, match.groups())) == (offset, entry.size_bytes - 1, entry.size_bytes)
        )

    def _receive(self, spec, partial, context, callback):
        entry = spec.files[0]
        offset = partial.stat().st_size if partial.exists() else 0
        for attempt in range(2):
            context.checkpoint()
            headers = {"User-Agent": "VoxTypeX/0.1", "Accept-Encoding": "identity"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            request = Request(entry.url, headers=headers)
            timeout = _NETWORK_TIMEOUT
            if context.deadline is not None:
                from time import monotonic

                timeout = min(timeout, max(0.1, context.deadline - monotonic()))
            try:
                response = _open_url(request, timeout)
            except HTTPError as error:
                if error.code == 416 and offset and attempt == 0:
                    _safe_path(partial)
                    partial.write_bytes(b"")
                    offset = 0
                    continue
                raise
            with response:
                context.checkpoint()
                _trusted_url(response.geturl())
                code = response.status
                if code == 206 and not self._valid_range(response, offset, entry):
                    if attempt == 0:
                        _safe_path(partial)
                        partial.write_bytes(b"")
                        offset = 0
                        continue
                    raise ModelDownloadError("Server returned an invalid model range", "range")
                if code not in (200, 206):
                    raise ModelDownloadError("Server did not return model data", "network")
                if code == 200:
                    offset = 0
                length = response.headers.get("Content-Length")
                if length is not None and (
                    not str(length).isdigit() or int(length) != entry.size_bytes - offset
                ):
                    raise ModelIntegrityError("Server returned an incorrect model size", "integrity")
                encoding = response.headers.get("Content-Encoding", "identity")
                if encoding.lower() not in ("identity", ""):
                    raise ModelIntegrityError("Encoded model transfers cannot be verified", "integrity")
                _safe_path(partial)
                flags = os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                flags |= os.O_APPEND if offset else os.O_TRUNC
                with os.fdopen(os.open(partial, flags, 0o600), "ab" if offset else "wb") as target:
                    received = offset
                    self._emit(callback, spec, received, LocalModelState.DOWNLOADING, "downloading")
                    while True:
                        context.checkpoint()
                        # Buffered read(amt) may accumulate many receives on an
                        # active slow stream. read1 yields each network fragment.
                        read_fragment = getattr(response, "read1", None)
                        block = read_fragment(_CHUNK_SIZE) if read_fragment else response.read(1)
                        context.checkpoint()
                        if not block:
                            break
                        if received + len(block) > entry.size_bytes:
                            raise ModelIntegrityError("Server exceeded the expected model size", "integrity")
                        target.write(block)
                        received += len(block)
                        self._emit(callback, spec, received, LocalModelState.DOWNLOADING, "downloading")
                    target.flush()
                    os.fsync(target.fileno())
                if length is not None and received < entry.size_bytes:
                    raise ModelDownloadError("Model transfer ended before the announced size", "network")
                return

    def download(self, model_id: str, *, context=None, on_progress=None) -> str:
        context = context or OperationContext(0)
        context.checkpoint()
        spec = self._spec(model_id)
        final, partial = self._paths(spec)
        lock = self._lock(spec)
        if not lock.acquire(blocking=False):
            raise ModelBusyError("This model is already being downloaded", "busy")
        lease = None
        try:
            context.checkpoint()
            final.parent.mkdir(parents=True, exist_ok=True)
            self._paths(spec)
            lease = self._lease(spec, create=True)
            if final.exists():
                try:
                    self._verify(final, spec.files[0], context)
                except ModelIntegrityError:
                    pass
                else:
                    self._verified[model_id] = _fingerprint(final)
                    self._broken.discard(model_id)
                    self._emit(on_progress, spec, spec.size_bytes, LocalModelState.READY, "ready")
                    return str(final)
            offset = partial.stat().st_size if partial.exists() else 0
            if offset >= spec.size_bytes:
                try:
                    self._verify(partial, spec.files[0], context)
                except ModelIntegrityError:
                    partial.unlink()
                    offset = 0
            if shutil.disk_usage(final.parent).free < spec.size_bytes - offset + _DISK_RESERVE:
                raise ModelDownloadError("Not enough free disk space for this model", "disk_space")
            self._broken.discard(model_id)
            if offset < spec.size_bytes:
                self._receive(spec, partial, context, on_progress)
            self._emit(on_progress, spec, spec.size_bytes, LocalModelState.DOWNLOADING, "verifying")
            self._verify(partial, spec.files[0], context)
            context.checkpoint()
            self._paths(spec)
            os.replace(partial, final)
            self._verified[model_id] = _fingerprint(final)
            self._emit(on_progress, spec, spec.size_bytes, LocalModelState.READY, "ready")
            return str(final)
        except (OperationCancelled, OperationTimeout):
            self._emit(
                on_progress,
                spec,
                partial.stat().st_size if partial.exists() else 0,
                LocalModelState.NOT_INSTALLED,
                "cancelled",
            )
            raise
        except ModelBusyError:
            raise
        except LocalModelError as error:
            self._broken.add(model_id)
            self._verified.pop(model_id, None)
            if isinstance(error, ModelIntegrityError) and partial.exists():
                _safe_path(partial)
                partial.unlink()
            self._emit(on_progress, spec, 0, LocalModelState.BROKEN, error.category)
            raise
        except (OSError, URLError, IncompleteRead) as error:
            context.checkpoint()
            self._broken.add(model_id)
            category = (
                "network"
                if isinstance(error, (URLError, TimeoutError, ConnectionError, IncompleteRead))
                else "io"
            )
            if getattr(error, "errno", None) == errno.ENOSPC or getattr(error, "winerror", None) == 112:
                category = "disk_space"
            self._emit(
                on_progress,
                spec,
                partial.stat().st_size if partial.exists() else 0,
                LocalModelState.BROKEN,
                category,
            )
            raise ModelDownloadError("Model download failed; retry to resume", category) from None
        finally:
            self._release_lease(lease)
            lock.release()
