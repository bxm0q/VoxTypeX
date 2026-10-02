"""Produce clean release archives and third-party notices from explicit input paths."""

import hashlib
import importlib.metadata as metadata
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
APP = DIST / "VoxTypeX"

runtime_packages = (
    "PySide6",
    "PySide6_Essentials",
    "PySide6_Addons",
    "shiboken6",
    "faster-whisper",
    "ctranslate2",
    "onnxruntime",
    "av",
    "numpy",
    "sounddevice",
    "pynput",
    "comtypes",
    "huggingface_hub",
    "tokenizers",
    "hf-xet",
    "certifi",
    "cffi",
)
notices = [
    "VoxTypeX 0.1.0 — third-party components",
    "License texts and metadata are preserved in _internal/*.dist-info.",
    "Qt/PySide DLLs are dynamically linked and remain separate in this onedir distribution.",
    "Upstream Qt/PySide source: https://code.qt.io/cgit/pyside/pyside-setup.git/",
    "Python source and license: https://www.python.org/downloads/source/",
    "PyInstaller bootloader: GPL with exception for distributed applications.",
    "https://pyinstaller.org/en/stable/license.html",
    "",
]
for package in runtime_packages:
    info = metadata.metadata(package)
    license_name = info.get("License-Expression") or info.get("License") or "See upstream license"
    notices.append(f"{package} {metadata.version(package)}: {license_name}")
(APP / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(notices), encoding="utf-8")
(APP / "README.md").write_bytes((ROOT / "README.md").read_bytes())
(APP / "docs").mkdir(exist_ok=True)
for name in ("MANUAL_SMOKE_TEST.md", "SETTINGS.md"):
    (APP / "docs" / name).write_bytes((ROOT / "docs" / name).read_bytes())


def archive(path, files, base):
    with ZipFile(path, "w", compression=ZIP_DEFLATED, compresslevel=6) as zip_file:
        for file in sorted(files):
            zip_file.write(file, file.relative_to(base).as_posix())
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")


archive(DIST / "VoxTypeX-0.1.0-win-x64.zip", [p for p in APP.rglob("*") if p.is_file()], DIST)
source_files = [
    ROOT / name for name in ("README.md", "pyproject.toml", ".gitignore", ".gitattributes", "requirements-build.lock")
]
source_files += [ROOT / "docs" / name for name in ("MANUAL_SMOKE_TEST.md", "SETTINGS.md")]
source_files += sorted((ROOT / "tests").glob("test_*.py"))
for directory in ("src/voxtypex", "scripts", "packaging"):
    source_files.extend(
        p
        for p in (ROOT / directory).rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and p.suffix in (".py", ".ps1", ".md", ".html", ".spec")
    )
archive(DIST / "VoxTypeX-0.1.0-source.zip", source_files, ROOT)
print("Clean application and source archives created with SHA256")
