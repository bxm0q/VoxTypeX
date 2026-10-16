"""Explicit download of a small, pinned FLEURS real-speech test sample."""

import argparse
import hashlib
import io
import json
import struct
import tarfile
import urllib.request
import wave
from pathlib import Path, PurePosixPath

REVISION = "70bb2e84b976b7e960aa89f1c648e09c59f894dd"
CONFIGS = {"ru": "ru_ru", "uk": "uk_ua", "en": "en_us"}


def parse_tsv(text):
    rows = {}
    for line in text.split("\n"):
        if not line.strip():
            continue
        fields = line.rstrip("\r").split("\t")
        if len(fields) != 7:
            raise ValueError("Malformed FLEURS TSV row")
        rows[fields[1]] = {
            "sentence_id": int(fields[0]),
            "reference": fields[2],
            "normalized_reference": fields[3],
            "num_samples": int(fields[5]),
            "gender": fields[6],
        }
    return rows


def to_pcm16_wav(original):
    """Convert FLEURS IEEE float32 WAV to a common PCM16 input without resampling."""
    import numpy as np

    if original[:4] != b"RIFF" or original[8:12] != b"WAVE":
        raise ValueError("Expected RIFF WAV")
    position, fmt, data = 12, None, None
    while position + 8 <= len(original):
        name, size = struct.unpack_from("<4sI", original, position)
        payload = original[position + 8 : position + 8 + size]
        if len(payload) != size:
            raise ValueError("Incomplete WAV chunk")
        if name == b"fmt ":
            fmt = struct.unpack_from("<HHIIHH", payload)
        elif name == b"data":
            data = payload
        position += 8 + size + size % 2
    if fmt is None or data is None:
        raise ValueError("Missing WAV format or samples")
    encoding, channels, rate, _, alignment, bits = fmt
    if channels != 1 or rate != 16000:
        raise ValueError("Corpus input must be mono 16 kHz")
    if encoding == 3 and bits == 32 and alignment == 4:
        samples = np.frombuffer(data, dtype="<f4")
        if not np.isfinite(samples).all():
            raise ValueError("Nonfinite corpus samples")
        pcm = (np.clip(samples, -1, 1) * 32767).round().astype("<i2").tobytes()
    elif encoding == 1 and bits == 16 and alignment == 2:
        pcm = data
    else:
        raise ValueError("Unsupported corpus WAV encoding")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return output.getvalue(), len(pcm) // 2


class LimitedStream:
    def __init__(self, source, byte_limit):
        self.source, self.byte_limit, self.bytes_read = source, byte_limit, 0

    def read(self, size=-1):
        if size < 0 or self.bytes_read + size > self.byte_limit:
            raise ValueError("Corpus download byte limit reached")
        data = self.source.read(size)
        self.bytes_read += len(data)
        return data


def fetch(output, languages, count, max_download_mib=256):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    clips, transfers = [], []
    for language in languages:
        config = CONFIGS[language]
        folder = output / config
        folder.mkdir(exist_ok=True)
        base = f"https://huggingface.co/datasets/google/fleurs/resolve/{REVISION}/data/{config}"
        tsv_url, archive_url = base + "/test.tsv", base + "/audio/test.tar.gz"
        with urllib.request.urlopen(tsv_url, timeout=60) as response:
            tsv = response.read(2 * 1024 * 1024)
        references = parse_tsv(tsv.decode("utf-8"))
        (folder / "test.tsv").write_bytes(tsv)
        selected = []
        with urllib.request.urlopen(archive_url, timeout=60) as response:
            limited = LimitedStream(response, max_download_mib * 1024 * 1024)
            with tarfile.open(fileobj=limited, mode="r|gz") as archive:
                for member in archive:
                    name = PurePosixPath(member.name).name
                    if not member.isfile() or not name.endswith(".wav") or name not in references:
                        continue
                    if member.size > 32 * 1024 * 1024:
                        raise ValueError("Unexpectedly large corpus clip")
                    original = archive.extractfile(member).read()
                    converted, samples = to_pcm16_wav(original)
                    reference = references[name]
                    if samples != reference["num_samples"]:
                        raise ValueError("Corpus audio and TSV duration disagree")
                    audio = folder / name
                    audio.write_bytes(converted)
                    selected.append(
                        {
                            "id": f"fleurs-{config}-test-{name[:-4]}",
                            "language": language,
                            "config": config,
                            "split": "test",
                            "audio": str(audio),
                            "path": str(audio),
                            "filename": name,
                            **reference,
                            "duration_seconds": samples / 16000,
                            "sample_rate": 16000,
                            "channels": 1,
                            "sample_width_bytes": 2,
                            "original_audio_sha256": hashlib.sha256(original).hexdigest(),
                            "audio_sha256": hashlib.sha256(converted).hexdigest(),
                            "dataset": "google/fleurs",
                            "dataset_revision": REVISION,
                            "source_archive_url": archive_url,
                            "source_archive_member": member.name,
                            "source_transcript_url": tsv_url,
                            "source_transcript_sha256": hashlib.sha256(tsv).hexdigest(),
                            "license": "CC-BY-4.0",
                            "conversion": "float32 to round(clip(-1,1)*32767) PCM16; no resampling or trimming",
                        }
                    )
                    if len(selected) == count:
                        break
        if len(selected) != count:
            raise ValueError("Corpus has fewer eligible clips than requested")
        transfers.append({"language": language, "downloaded_archive_prefix_bytes": limited.bytes_read})
        clips.extend(selected)
        print(f"{language}: {len(selected)} real clips, {sum(x['duration_seconds'] for x in selected):.2f}s")
    manifest = {
        "schema_version": 1,
        "dataset": "google/fleurs",
        "dataset_revision": REVISION,
        "source_url": "https://huggingface.co/datasets/google/fleurs",
        "license": "CC-BY-4.0",
        "selection": f"First {count} matching regular WAV members in pinned archive order per language",
        "scope": "Small read-speech smoke sample, not full test split or conversational accuracy proof",
        "transfers": transfers,
        "clips": clips,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "manifest.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in clips), encoding="utf-8"
    )
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(".local/iteration2-benchmark"))
    parser.add_argument("--languages", nargs="+", choices=CONFIGS, default=list(CONFIGS))
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--max-download-mib", type=int, default=256)
    args = parser.parse_args()
    if args.count < 1 or args.max_download_mib < 1:
        parser.error("count and max-download-mib must be positive")
    try:
        fetch(args.out, list(dict.fromkeys(args.languages)), args.count, args.max_download_mib)
    except Exception as exc:
        print(f"Corpus fetch failed ({type(exc).__name__}); no benchmark accuracy result was produced")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
