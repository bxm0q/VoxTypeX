from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files, collect_delvewheel_libs_directory, collect_dynamic_libs, copy_metadata,
)

root = Path(SPECPATH).parent
datas = collect_data_files("faster_whisper") + copy_metadata("faster-whisper", recursive=True)
for package in ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6", "sounddevice", "pynput", "comtypes"):
    datas += copy_metadata(package)
binaries = collect_dynamic_libs("ctranslate2")
datas, binaries = collect_delvewheel_libs_directory('pywebrtc_audio', datas=datas, binaries=binaries)
datas += copy_metadata('pywebrtc-audio')
native = root / ".local" / "native" / "transcribe-cpp"
if not (native / "ggml-cpu-x64.dll").is_file():
    raise RuntimeError("Run scripts/build-native.ps1 with the build Python before freezing")
binaries += [(str(path), "transcribe_native") for path in native.glob("*.dll")]
datas += [(str(path), "transcribe_native") for path in native.glob("*.txt")]
datas += [(str(native / "NATIVE_BUILD.json"), "transcribe_native")]
datas += [(str(root / "src" / "voxtypex" / "model_catalog.json"), "voxtypex")]
datas += copy_metadata("transcribe-cpp", recursive=True)

a = Analysis(
    [str(root / "packaging" / "launcher.py")],
    pathex=[str(root / "src"), str(root / "packaging")],
    hookspath=[str(root / "packaging" / "hooks")],
    binaries=binaries,
    datas=datas,
    hiddenimports=["pynput.keyboard._win32", "pynput.mouse._win32", "comtypes.client", "hf_xet", "transcribe_cpp",
                   "pywebrtc_audio", "pywebrtc_audio._webrtc_audio", "webrtcvad", "_webrtcvad"],
    excludes=["pytest", "ruff", "tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets"],
    noarchive=False,
)
# Qt uses Windows ICU exports; a third-party ICU with the same filename is incompatible.
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() not in {"icuuc.dll", "icuin.dll", "icudt.dll"}]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="VoxTypeX",
    console=False,
    debug=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="VoxTypeX")
