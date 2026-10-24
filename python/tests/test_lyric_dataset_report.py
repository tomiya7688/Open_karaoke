import hashlib
import json
from pathlib import Path

import pytest
from test_dataset_manifest import manifest, sample

from open_karaoke_analysis.lyric_dataset_report import build_report


def segments(text):
    return {
        "reference_segments": [{"id": "s1", "text": text, "start_sample": 0, "end_sample": 100}]
    }


def predictions(root: Path, records, model=None, version=1) -> Path:
    path = root / "predictions.json"
    document = {
        "version": version,
        "model": model or {"name": "whisper", "version": "large-v3"},
        "predictions": records,
    }
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def dataset(root: Path):
    return manifest(
        root,
        [
            sample(root, "a", payload=segments("abc")),
            sample(root, "b", payload=segments("hello world")),
            sample(root, "h", split="holdout", payload=segments("test")),
            sample(root, "p", payload={"stems": ["vocals"]}, kind="stems"),
        ],
    )


def test_report_aggregates_only_lyric_samples_in_the_selected_split(tmp_path):
    manifest_path = dataset(tmp_path)
    prediction = predictions(
        tmp_path,
        [{"sample_id": "a", "text": "abc"}, {"sample_id": "b", "text": "hello word"}],
    )
    report = build_report(manifest_path, prediction, "development")
    assert report["sample_count"] == 2
    assert report["split"] == "development"
    assert report["model"] == {"name": "whisper", "version": "large-v3"}
    assert 0.0 < report["cer"] < 0.2
    assert report["wer"] == pytest.approx(1 / 3)
    assert (
        report["dataset_manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    )
    assert report["predictions_sha256"] == hashlib.sha256(prediction.read_bytes()).hexdigest()


def test_holdout_report_uses_its_own_reference(tmp_path):
    manifest_path = dataset(tmp_path)
    prediction = predictions(tmp_path, [{"sample_id": "h", "text": "test"}])
    report = build_report(manifest_path, prediction, "holdout")
    assert report["sample_count"] == 1
    assert report["cer"] == 0.0 and report["wer"] == 0.0


def test_predictions_must_match_the_lyric_samples_exactly(tmp_path):
    manifest_path = dataset(tmp_path)
    for records in (
        [{"sample_id": "a", "text": "abc"}],
        [
            {"sample_id": "a", "text": "abc"},
            {"sample_id": "b", "text": "x"},
            {"sample_id": "h", "text": "y"},
        ],
        [{"sample_id": "a", "text": "abc"}, {"sample_id": "p", "text": "z"}],
    ):
        with pytest.raises(ValueError, match="match exactly"):
            build_report(manifest_path, predictions(tmp_path, records), "development")


def test_invalid_prediction_files_are_rejected(tmp_path):
    manifest_path = dataset(tmp_path)
    valid = [{"sample_id": "a", "text": "abc"}, {"sample_id": "b", "text": "x"}]
    with pytest.raises(ValueError, match="Duplicate prediction"):
        build_report(manifest_path, predictions(tmp_path, valid + [valid[0]]), "development")
    with pytest.raises(ValueError, match="Unsupported prediction version"):
        build_report(manifest_path, predictions(tmp_path, valid, version=2), "development")
    with pytest.raises(ValueError, match="1..1000"):
        build_report(manifest_path, predictions(tmp_path, []), "development")
    with pytest.raises(ValueError, match="model.name"):
        build_report(
            manifest_path,
            predictions(tmp_path, valid, model={"name": " ", "version": "1"}),
            "development",
        )
    with pytest.raises(ValueError, match="at most 20000"):
        long = [{"sample_id": "a", "text": "x" * 20_001}, valid[1]]
        build_report(manifest_path, predictions(tmp_path, long), "development")


def test_split_and_missing_lyrics_are_validated(tmp_path):
    manifest_path = dataset(tmp_path)
    prediction = predictions(tmp_path, [{"sample_id": "a", "text": "abc"}])
    with pytest.raises(ValueError, match="development or holdout"):
        build_report(manifest_path, prediction, "test")
    only_stems = manifest(tmp_path, [sample(tmp_path, "p", payload={"stems": ["x"]}, kind="stems")])
    with pytest.raises(ValueError, match="no lyric annotations"):
        build_report(only_stems, prediction, "development")


def test_invalid_manifest_stops_the_report(tmp_path):
    manifest_path = dataset(tmp_path)
    (tmp_path / "a.wav").write_bytes(b"tampered")
    prediction = predictions(tmp_path, [{"sample_id": "a", "text": "abc"}])
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        build_report(manifest_path, prediction, "development")
