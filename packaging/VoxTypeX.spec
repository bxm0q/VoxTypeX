from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata

root = Path(SPECPATH).parent
datas = collect_data_files("faster_whisper") + copy_metadata("faster-whisper", recursive=True)
for package in ("PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6", "sounddevice", "pynput", "comtypes"):
    datas += copy_metadata(package)
binaries = collect_dynamic_libs("ctranslate2")

a = Analysis(
    [str(root / "packaging" / "launcher.py")],
    pathex=[str(root / "src"), str(root / "packaging")],
    binaries=binaries,
    datas=datas,
    hiddenimports=["pynput.keyboard._win32", "pynput.mouse._win32", "comtypes.client", "hf_xet"],
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
