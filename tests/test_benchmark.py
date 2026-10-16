import importlib.util
import struct
import sys
from pathlib import Path

import pytest


def load_script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


benchmark = load_script("benchmark_asr")


def test_normalization_preserves_ukrainian_letters_and_unifies_apostrophes():
    # Removing Unicode letters or treating two common apostrophes differently breaks fair scoring.
    assert benchmark.normalize_text("ПРИВІТ, світе! １２３ М’ЯСО") == "привіт світе 123 мясо"
    assert benchmark.normalize_text("м'ясо") == "мясо"
    assert benchmark.normalize_text("Ёлка — їжак") == "ёлка їжак"


def test_word_and_character_edit_counts_are_hand_checked():
    # A word-level-only metric would miss the difference between one wrong character and one wrong word.
    score = benchmark.error_counts("кот", "кит")
    assert score == {"word_errors": 1, "reference_words": 1, "char_errors": 1, "reference_chars": 3}
    assert benchmark.error_counts("a b c", "a x c d")["word_errors"] == 2


def test_weighted_aggregates_exclude_warmup_and_keep_failures_visible():
    # Averaging per-clip percentages or including cold-start warmup gives misleading WER and RTF.
    rows = [
        dict(
            warmup=True,
            word_errors=100,
            reference_words=100,
            char_errors=100,
            reference_chars=100,
            audio_seconds=10,
            elapsed_seconds=100,
            error_code=None,
        ),
        dict(
            warmup=False,
            word_errors=1,
            reference_words=1,
            char_errors=1,
            reference_chars=3,
            audio_seconds=2,
            elapsed_seconds=1,
            error_code="backend",
        ),
        dict(
            warmup=False,
            word_errors=0,
            reference_words=9,
            char_errors=0,
            reference_chars=27,
            audio_seconds=8,
            elapsed_seconds=1,
            error_code=None,
        ),
    ]
    score = benchmark.aggregate(rows)
    assert score["clips"] == 2
    assert score["failed_clips"] == 1
    assert score["wer"] == pytest.approx(0.1)
    assert score["cer"] == pytest.approx(1 / 30)
    assert score["rtf"] == pytest.approx(0.2)
    assert score["speed_x_realtime"] == pytest.approx(5)


def test_report_csv_has_metrics_and_never_saves_transcripts(tmp_path):
    # Accidentally dumping the internal inference result leaks hypotheses into the default report.
    report = {
        "schema_version": 1,
        "machine": {},
        "runs": [
            {
                "language": "uk",
                "variant": "compact",
                "condition": "read",
                "status": "complete",
                "metrics": {"clips": 1, "wer": 0.1, "cer": 0.05, "rtf": 0.2},
                "observations": [{"hypothesis": "PRIVATE"}],
            }
        ],
    }
    benchmark.write_reports(report, tmp_path)
    assert "PRIVATE" not in (tmp_path / "report.json").read_text(encoding="utf-8")
    header = (tmp_path / "report.csv").read_text(encoding="utf-8").splitlines()[0]
    assert all(name in header for name in ("language", "variant", "condition", "wer", "cer", "rtf"))


def test_corpus_tsv_preserves_literal_quotes_in_reference():
    # FLEURS TSV is tab-delimited text, not CSV-quoted text; CSV parsing can drop fields.
    fetch = load_script("fetch_benchmark_corpus")
    row = '7\ta.wav\t"Hello" world\thello world\th e l l o |\t16000\tFEMALE\n'
    assert fetch.parse_tsv(row)["a.wav"] == {
        "sentence_id": 7,
        "reference": '"Hello" world',
        "normalized_reference": "hello world",
        "num_samples": 16000,
        "gender": "FEMALE",
    }


def test_corpus_float_wav_conversion_produces_valid_pcm16():
    # Original FLEURS WAVs contain IEEE float samples; treating them as int16 corrupts all model inputs.
    fetch = load_script("fetch_benchmark_corpus")
    fmt = struct.pack("<HHIIHH", 3, 1, 16000, 64000, 4, 32)
    samples = struct.pack("<fff", -1.0, 0.0, 1.0)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(samples)) + samples
    original = b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks
    converted, count = fetch.to_pcm16_wav(original)
    assert count == 3
    assert converted[-6:] == struct.pack("<hhh", -32767, 0, 32767)
    assert struct.unpack_from("<H", converted, 20)[0] == 1


def test_noise_variant_is_reproducible_and_preserves_sample_count():
    # Changing RNG state or audio length makes the noisy comparison unfair between models.
    original = struct.pack("<hhhh", 2000, -2000, 1000, -1000) * 100
    first = benchmark.add_noise(original, seed=17, snr_db=20)
    assert len(first) == len(original)
    assert first != original
    assert first == benchmark.add_noise(original, seed=17, snr_db=20)
    assert first != benchmark.add_noise(original, seed=18, snr_db=20)


def test_native_fallback_text_never_receives_native_accuracy_credit():
    from voxtypex.interfaces import AudioClip, ProgressEvent, ProgressStage, TranscriptionResult

    class FallbackService:
        def run(self, audio, context, progress):
            progress(ProgressEvent(context.request_id, ProgressStage.FALLBACK))
            return TranscriptionResult(text="the correct words", language="en")

    # A fallback transcript is from Whisper and must not improve the reported native model score.
    row = benchmark.observe_inference(
        FallbackService(),
        AudioClip(b"\0\0" * 16000, 16000),
        "the correct words",
        native=True,
        request_id=1,
        timeout=60,
    )
    assert row["error_code"] == "native_fallback"
    assert row["word_errors"] == 3
    assert "text" not in row
