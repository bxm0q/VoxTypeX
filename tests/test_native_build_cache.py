import importlib.util
import json
import sys
from pathlib import Path
from zipfile import ZipFile


def module():
    path = Path(__file__).resolve().parents[1] / "scripts/native_build_cache.py"
    spec = importlib.util.spec_from_file_location("native_build_cache", path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


PIN = {
    "revision": "a" * 40,
    "version": "0.4.0",
    "backend": "cpu",
    "cmake_options": {"TRANSCRIBE_GGML_BACKEND_DL": "ON", "GGML_CPU_ALL_VARIANTS": "ON"},
}


def wheel(path, data=b"compiled binary fixture"):
    with ZipFile(path, "w") as archive:
        archive.writestr("transcribe_cpp_native/_native/ggml-cpu-x64.dll", data)


def test_same_filename_without_source_receipt_is_never_reused(tmp_path):
    cache = module()
    path = tmp_path / "native-0.4.0.whl"
    wheel(path)
    assert not cache.verified(path, PIN)
    cache.record(path, PIN)
    assert cache.verified(path, PIN)


def test_stale_source_changed_options_or_changed_bytes_invalidate_cache(tmp_path):
    cache = module()
    path = tmp_path / "native-0.4.0.whl"
    wheel(path)
    cache.record(path, PIN)
    assert not cache.verified(path, dict(PIN, revision="b" * 40))
    assert not cache.verified(path, dict(PIN, cmake_options={"GGML_CPU_ALL_VARIANTS": "OFF"}))
    wheel(path, b"different binary from another build")
    assert not cache.verified(path, PIN)


def test_receipt_records_actual_dll_hashes_and_invalid_receipts_fail_closed(tmp_path):
    cache = module()
    path = tmp_path / "native-0.4.0.whl"
    wheel(path)
    cache.record(path, PIN)
    receipt = path.with_suffix(path.suffix + ".build.json")
    data = json.loads(receipt.read_text())
    assert len(data["dll_sha256"]["ggml-cpu-x64.dll"]) == 64
    receipt.write_text("{broken")
    assert not cache.verified(path, PIN)


def test_dll_commit_must_be_real_prefix_of_the_pinned_source():
    cache = module()
    assert cache.matches_commit("a" * 7, "a" * 40)
    assert not cache.matches_commit("unknown", "a" * 40)
    assert not cache.matches_commit("b" * 7, "a" * 40)
    assert not cache.matches_commit("a", "a" * 40)
