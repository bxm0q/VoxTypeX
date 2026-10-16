"""Stage the locally compiled CPU provider and license notices for source/frozen use."""

import ctypes
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    from native_build_cache import matches_commit, verified
    from transcribe_cpp_native import descriptor

    pin = json.loads((ROOT / "packaging/native-runtime.json").read_text(encoding="utf-8"))
    source = ROOT / ".local/tools/transcribe-cpp"
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != pin["revision"]:
        raise RuntimeError("Native source revision does not match the checked-in pin")
    contract = descriptor()
    if contract["version"] != pin["version"] or contract["backends"] != ["cpu"]:
        raise RuntimeError("Build the pinned CPU-only native provider")
    if importlib.metadata.version("transcribe-cpp") != pin["version"]:
        raise RuntimeError("Binding version does not match native provider")
    wheel = ROOT / ".local/native-wheels/transcribe_cpp_native-0.4.0-py3-none-win_amd64.whl"
    if not verified(wheel, pin):
        raise RuntimeError("Cached native wheel has no valid source/options/hash build receipt")
    receipt = json.loads(wheel.with_suffix(wheel.suffix + ".build.json").read_text(encoding="utf-8"))
    native = Path(contract["artifact_dir"])
    for name, expected in receipt["dll_sha256"].items():
        if hashlib.sha256((native / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Installed native DLL differs from the verified build wheel")
    with os.add_dll_directory(str(native)):
        library = ctypes.CDLL(str(native / "transcribe.dll"))
        library.transcribe_version_commit.restype = ctypes.c_char_p
        library.transcribe_version_commit.argtypes = []
        compiled_commit = library.transcribe_version_commit().decode("ascii")
    if not matches_commit(compiled_commit, pin["revision"]):
        raise RuntimeError("Compiled DLL commit differs from the pinned source")
    stage = ROOT / ".local/native/transcribe-cpp"
    stage.mkdir(parents=True, exist_ok=True)
    if not stage.resolve().is_relative_to(ROOT.resolve()) or stage.is_symlink():
        raise RuntimeError("Unsafe native output directory")
    # Only generated DLLs in this fixed workspace directory are replaced.
    for previous in stage.glob("*.dll"):
        previous.unlink()
    artifacts = list(native.glob("*.dll"))
    if not (native / "ggml-cpu-x64.dll").is_file():
        raise RuntimeError("Portable x64 CPU baseline is missing")
    hashes = {}
    for artifact in artifacts:
        shutil.copy2(artifact, stage / artifact.name)
        hashes[artifact.name] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    for source_name, output_name in (
        ("LICENSE", "LICENSE-transcribe.txt"),
        ("ggml/LICENSE", "LICENSE-ggml.txt"),
        ("src/third_party/miniz/LICENSE", "LICENSE-miniz.txt"),
        ("src/third_party/silero_vad/LICENSE", "LICENSE-silero.txt"),
    ):
        shutil.copy2(source / source_name, stage / output_name)
    report = {
        **pin,
        "compiled_commit": compiled_commit,
        "wheel_sha256": receipt["wheel_sha256"],
        "header_hash": contract["header_hash"],
        "dll_sha256": hashes,
    }
    (stage / "NATIVE_BUILD.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Staged pinned CPU runtime: {len(artifacts)} DLLs, baseline x64 included")


if __name__ == "__main__":
    main()
