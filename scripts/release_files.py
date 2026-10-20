import argparse
import hashlib
import importlib.metadata as metadata
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser()
parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
DIST = parser.parse_args().dist_dir.resolve()
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
    "transcribe-cpp",
    "transcribe-cpp-native",
    "pywebrtc-audio",
    "webrtcvad-wheels",
)
notices = [
    "VoxTypeX 0.1.3 — third-party components",
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
for name in (
    "MANUAL_SMOKE_TEST.md",
    "SETTINGS.md",
    "LOCAL_MODELS.md",
    "ASR_BENCHMARK.md",
    "ASR_BENCHMARK_RESULTS.json",
    "AUDIO_PIPELINE.md",
    "TRANSCRIPT_PREVIEW.md",
):
    (APP / "docs" / name).write_bytes((ROOT / "docs" / name).read_bytes())
(APP / "docs/licenses").mkdir(exist_ok=True)
for license_file in (ROOT / "packaging/licenses").glob("*.txt"):
    (APP / "docs/licenses" / license_file.name).write_bytes(license_file.read_bytes())


def archive(path, files, base):
    with ZipFile(path, "w", compression=ZIP_DEFLATED, compresslevel=6) as zip_file:
        for file in sorted(files):
            zip_file.write(file, file.relative_to(base).as_posix())
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")


application_files = [APP / name for name in ("VoxTypeX.exe", "README.md", "THIRD_PARTY_NOTICES.txt")]
application_files += [p for p in (APP / "docs").rglob("*") if p.is_file()]
application_files += [p for p in (APP / "_internal").rglob("*") if p.is_file()]
archive(DIST / "VoxTypeX-0.1.3-win-x64.zip", application_files, DIST)
source_files = [
    ROOT / name
    for name in ("README.md", "pyproject.toml", ".gitignore", ".gitattributes", "requirements-build.lock")
]
source_files += [
    ROOT / "docs" / name
    for name in (
        "MANUAL_SMOKE_TEST.md",
        "DEVELOPMENT.md",
        "SETTINGS.md",
        "ARCHITECTURE.md",
        "LOCAL_MODELS.md",
        "ASR_BENCHMARK.md",
        "ASR_BENCHMARK_RESULTS.json",
        "ITERATION2_REPORT.md",
        "AUDIO_PIPELINE.md",
        "ITERATION3_REPORT.md",
        "TRANSCRIPT_PREVIEW.md",
        "ITERATION4_REPORT.md",
    )
]
source_files += [ROOT / "src/voxtypex/model_catalog.json", ROOT / "packaging/native-runtime.json"]
source_files += [p for p in (ROOT / "packaging/licenses").glob("*") if p.is_file()]
source_files += sorted((ROOT / "tests").glob("test_*.py"))
for directory in ("src/voxtypex", "scripts", "packaging"):
    source_files.extend(
        p
        for p in (ROOT / directory).rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and p.suffix in (".py", ".ps1", ".md", ".html", ".spec", ".iss")
    )
archive(DIST / "VoxTypeX-0.1.3-source.zip", source_files, ROOT)
print("Clean application and source archives created with SHA256")
