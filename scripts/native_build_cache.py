"""Build receipts bind a cached wheel to its exact source/options and bytes."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from zipfile import ZipFile


def identity(pin):
    return {key: pin[key] for key in ("revision", "version", "backend", "cmake_options")}


def matches_commit(compiled, revision):
    return bool(re.fullmatch(r"[0-9a-f]{7,40}", compiled) and revision.startswith(compiled))


def receipt_path(wheel):
    return wheel.with_suffix(wheel.suffix + ".build.json")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verified(wheel, pin):
    try:
        receipt = json.loads(receipt_path(wheel).read_text(encoding="utf-8"))
        hashes = receipt["dll_sha256"]
        return (
            receipt["schema_version"] == 1
            and receipt["build"] == identity(pin)
            and isinstance(hashes, dict)
            and bool(hashes)
            and all(re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes.values())
            and receipt["wheel_sha256"] == digest(wheel)
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def record(wheel, pin):
    hashes = {}
    with ZipFile(wheel) as archive:
        for name in archive.namelist():
            if "/_native/" in name and name.endswith(".dll"):
                hashes[Path(name).name] = hashlib.sha256(archive.read(name)).hexdigest()
    receipt = {
        "schema_version": 1,
        "build": identity(pin),
        "wheel_sha256": digest(wheel),
        "dll_sha256": hashes,
    }
    receipt_path(wheel).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "record"))
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    pin = json.loads(
        (Path(__file__).resolve().parents[1] / "packaging/native-runtime.json").read_text(encoding="utf-8")
    )
    if args.action == "check":
        return 0 if verified(args.wheel, pin) else 1
    record(args.wheel, pin)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
