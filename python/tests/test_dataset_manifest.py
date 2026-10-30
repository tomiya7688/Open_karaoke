import hashlib
import json
from pathlib import Path

import pytest

from open_karaoke_analysis.dataset_manifest import validate_manifest

DURATION = 48_000
STRATA = {
    "language": "ja",
    "vocal_source": "human",
    "audio_condition": "clean",
    "phenomena": ["legato"],
}


def write_file(root: Path, name: str, content: bytes) -> dict:
    (root / name).write_bytes(content)
    return {"path": name, "sha256": hashlib.sha256(content).hexdigest()}


def envelope(identifier, audio_hash, /, kind="lyrics", payload=None, **overrides):
    if payload is None:
        payload = {
            "reference_segments": [
                {"id": "s1", "text": "こんにちは", "start_sample": 0, "end_sample": 24_000}
            ]
        }
    document = {
        "version": 1,
        "sample_id": identifier,
        "audio_sha256": audio_hash,
        "sample_rate": 48_000,
        "duration_samples": DURATION,
        "kind": kind,
        "payload": payload,
    }
    document.update(overrides)
    return document


def sample(root: Path, identifier, split="development", group=None, audio=None, **env):
    audio_entry = write_file(root, f"{identifier}.wav", audio or identifier.encode())
    annotation = json.dumps(envelope(identifier, audio_entry["sha256"], **env)).encode()
    return {
        "id": identifier,
        "split": split,
        "source_group": group or f"group-{identifier}",
        "provenance": "recorded by the project",
        "rights_reference": "consent-001",
        "strata": dict(STRATA),
        "audio": audio_entry,
        "annotation": write_file(root, f"{identifier}.json", annotation),
    }


def manifest(root: Path, samples, version=1) -> Path:
    path = root / "manifest.json"
    path.write_text(json.dumps({"version": version, "samples": samples}), encoding="utf-8")
    return path


def test_valid_manifest_reports_counts_strata_and_verification(tmp_path):
    path = manifest(tmp_path, [sample(tmp_path, "a"), sample(tmp_path, "b", split="holdout")])
    report = validate_manifest(path)
    assert report["samples"] == 2
    assert report["splits"] == {"development": 1, "holdout": 1}
    assert report["hashes_verified"] is True
    assert report["annotation_envelopes_verified"] is True
    assert report["manifest_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert report["strata_counts"]["development"]["language"] == {"ja": 1}
    assert report["strata_counts"]["holdout"]["phenomena"] == {"legato": 1}


def test_empty_phenomena_are_counted_as_none(tmp_path):
    entry = sample(tmp_path, "a")
    entry["strata"]["phenomena"] = []
    report = validate_manifest(manifest(tmp_path, [entry]))
    assert report["strata_counts"]["development"]["phenomena"] == {"none": 1}


@pytest.mark.parametrize("version", [2, True, "1"])
def test_unsupported_version_is_rejected(tmp_path, version):
    with pytest.raises(ValueError, match="version"):
        validate_manifest(manifest(tmp_path, [sample(tmp_path, "a")], version=version))


def test_sample_count_and_keys_are_validated(tmp_path):
    with pytest.raises(ValueError, match="1..10000"):
        validate_manifest(manifest(tmp_path, []))
    extra = sample(tmp_path, "a")
    extra["unexpected"] = "x"
    with pytest.raises(ValueError, match="exactly"):
        validate_manifest(manifest(tmp_path, [extra]))


def test_duplicate_id_and_bad_split_are_rejected(tmp_path):
    first = sample(tmp_path, "a")
    with pytest.raises(ValueError, match="Duplicate sample"):
        validate_manifest(manifest(tmp_path, [first, dict(first, source_group="other")]))
    with pytest.raises(ValueError, match="development or holdout"):
        validate_manifest(manifest(tmp_path, [sample(tmp_path, "b", split="test")]))


def test_text_fields_must_be_nonempty_and_bounded(tmp_path):
    blank = sample(tmp_path, "a")
    blank["provenance"] = "  "
    with pytest.raises(ValueError, match="provenance"):
        validate_manifest(manifest(tmp_path, [blank]))
    long = sample(tmp_path, "b")
    long["rights_reference"] = "x" * 513
    with pytest.raises(ValueError, match="rights_reference"):
        validate_manifest(manifest(tmp_path, [long]))


def test_duplicate_json_fields_and_non_finite_values_are_rejected(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text('{"version": 1, "version": 1, "samples": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate JSON field"):
        validate_manifest(path)
    path.write_text('{"version": NaN, "samples": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="Non-finite"):
        validate_manifest(path)


def test_source_group_and_identical_audio_cannot_leak_across_splits(tmp_path):
    shared_group = [
        sample(tmp_path, "a", group="same"),
        sample(tmp_path, "b", split="holdout", group="same"),
    ]
    with pytest.raises(ValueError, match="source group"):
        validate_manifest(manifest(tmp_path, shared_group))
    same_audio = [
        sample(tmp_path, "c", audio=b"same-audio"),
        sample(tmp_path, "d", split="holdout", audio=b"same-audio"),
    ]
    with pytest.raises(ValueError, match="Identical audio"):
        validate_manifest(manifest(tmp_path, same_audio))


def test_hash_mismatch_and_malformed_digest_are_rejected(tmp_path):
    tampered = sample(tmp_path, "a")
    (tmp_path / "a.wav").write_bytes(b"changed")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        validate_manifest(manifest(tmp_path, [tampered]))
    bad = sample(tmp_path, "b")
    bad["audio"]["sha256"] = bad["audio"]["sha256"].upper()
    with pytest.raises(ValueError, match="lowercase hex"):
        validate_manifest(manifest(tmp_path, [bad]))


@pytest.mark.parametrize(
    "raw", ["../outside.wav", "/abs.wav", "a\\b.wav", "./a.wav", "sub//x", "C:x", "C:/x"]
)
def test_paths_must_stay_below_manifest_directory(tmp_path, raw):
    entry = sample(tmp_path, "a")
    entry["audio"]["path"] = raw
    with pytest.raises(ValueError, match="manifest directory"):
        validate_manifest(manifest(tmp_path, [entry]))


@pytest.mark.parametrize(
    "raw", ["../outside.json", "/abs.json", "a\\b.json", "./a.json", "sub//x", "C:x", "C:/x"]
)
def test_annotation_paths_must_also_stay_below_manifest_directory(tmp_path, raw):
    entry = sample(tmp_path, "a")
    entry["annotation"]["path"] = raw
    with pytest.raises(ValueError, match=r"annotation\.path must .*manifest directory"):
        validate_manifest(manifest(tmp_path, [entry]))


def test_files_in_a_subdirectory_are_accepted(tmp_path):
    entry = sample(tmp_path, "a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.wav").replace(tmp_path / "sub" / "a.wav")
    entry["audio"]["path"] = "sub/a.wav"
    assert validate_manifest(manifest(tmp_path, [entry]))["samples"] == 1


def test_a_symlink_that_escapes_the_manifest_directory_is_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.wav"
    outside.write_bytes(
        b"a"
    )  # sample() の音声内容(識別子と同じ)と一致させ、SHA-256 は合う状態にする
    entry = sample(root, "a")
    (root / "a.wav").unlink()
    try:
        (root / "a.wav").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not permitted in this environment")
    with pytest.raises(ValueError, match="regular file below the manifest directory"):
        validate_manifest(manifest(root, [entry]))


def test_missing_or_directory_target_is_rejected(tmp_path):
    entry = sample(tmp_path, "a")
    (tmp_path / "a.wav").unlink()
    with pytest.raises(ValueError, match="regular file"):
        validate_manifest(manifest(tmp_path, [entry]))
    (tmp_path / "a.wav").mkdir()
    with pytest.raises(ValueError, match="regular file"):
        validate_manifest(manifest(tmp_path, [entry]))


def test_oversized_manifest_is_rejected(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_bytes(b" " * 4_194_305)
    with pytest.raises(ValueError, match="4 MiB"):
        validate_manifest(path)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"version": 2}, "version must be 1"),
        ({"sample_id": "other"}, "does not match"),
        ({"audio_sha256": "0" * 64}, "does not match"),
        ({"sample_rate": 44_100}, "sample_rate must be 48000"),
        ({"duration_samples": 0}, "duration_samples"),
        ({"duration_samples": 48_000 * 60 * 60 * 4 + 1}, "duration_samples"),
        ({"kind": "video"}, "kind is unsupported"),
        ({"payload": {}}, "nonempty object"),
    ],
)
def test_annotation_envelope_must_bind_to_sample_and_audio(tmp_path, override, message):
    entry = sample(tmp_path, "a", **override)
    with pytest.raises(ValueError, match=message):
        validate_manifest(manifest(tmp_path, [entry]))


@pytest.mark.parametrize(
    ("segment", "message"),
    [
        (
            {"id": "s1", "text": "x", "start_sample": 0, "end_sample": DURATION + 1},
            "sample duration",
        ),
        ({"id": "s1", "text": "x", "start_sample": 5, "end_sample": 5}, "sample duration"),
        ({"id": "s1", "text": "x", "start_sample": 10, "end_sample": 5}, "sample duration"),
        ({"id": "s1", "text": "x", "start_sample": -1, "end_sample": 10}, "sample duration"),
        ({"id": "s1", "text": "x", "start_sample": True, "end_sample": 10}, "sample duration"),
        ({"id": "s1", "text": " ", "start_sample": 0, "end_sample": 10}, "nonempty"),
    ],
)
def test_lyric_segments_must_fit_the_audio_duration(tmp_path, segment, message):
    entry = sample(tmp_path, "a", payload={"reference_segments": [segment]})
    with pytest.raises(ValueError, match=message):
        validate_manifest(manifest(tmp_path, [entry]))


def test_lyric_segment_boundaries_and_overlaps_are_accepted(tmp_path):
    # 半開区間 [0, DURATION) 全体と、開始が昇順で重なる区間は有効(仕様は重なりを禁止しない)。
    segments = [
        {"id": "s1", "text": "a", "start_sample": 0, "end_sample": DURATION},
        {"id": "s2", "text": "b", "start_sample": 100, "end_sample": 200},
    ]
    entry = sample(tmp_path, "a", payload={"reference_segments": segments})
    assert validate_manifest(manifest(tmp_path, [entry]))["samples"] == 1


def test_lyric_segments_reject_duplicate_ids_and_unordered_starts(tmp_path):
    first = {"id": "s1", "text": "a", "start_sample": 100, "end_sample": 200}
    duplicate = sample(tmp_path, "a", payload={"reference_segments": [first, dict(first)]})
    with pytest.raises(ValueError, match="duplicate lyric segment ids"):
        validate_manifest(manifest(tmp_path, [duplicate]))
    unordered = [first, dict(first, id="s2", start_sample=50, end_sample=60)]
    entry = sample(tmp_path, "b", payload={"reference_segments": unordered})
    with pytest.raises(ValueError, match="ordered by start_sample"):
        validate_manifest(manifest(tmp_path, [entry]))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("language", "klingon"),
        ("vocal_source", "robot"),
        ("audio_condition", "noisy"),
        ("phenomena", ["vibrato"]),
        ("phenomena", ["legato", "legato"]),
    ],
)
def test_strata_values_are_restricted(tmp_path, field, value):
    entry = sample(tmp_path, "a")
    entry["strata"][field] = value
    with pytest.raises(ValueError, match="strata"):
        validate_manifest(manifest(tmp_path, [entry]))


def test_language_none_cannot_have_reference_lyrics(tmp_path):
    entry = sample(tmp_path, "a")
    entry["strata"]["language"] = "none"
    with pytest.raises(ValueError, match="cannot be none"):
        validate_manifest(manifest(tmp_path, [entry]))
    instrumental = sample(tmp_path, "b", payload={"reference_segments": []})
    instrumental["strata"]["language"] = "none"
    assert validate_manifest(manifest(tmp_path, [instrumental]))["samples"] == 1


def test_numbers_that_overflow_to_infinity_in_an_annotation_are_rejected(tmp_path):
    entry = sample(tmp_path, "a")
    audio_sha = entry["audio"]["sha256"]
    # 1e999 は "Infinity" の綴りを使わずに float('inf') になる。
    raw = (
        '{"version":1,"sample_id":"a","audio_sha256":"%s","sample_rate":48000,'
        '"duration_samples":48000,"kind":"pitch","payload":{"hz":1e999}}' % audio_sha
    ).encode()
    entry["annotation"] = write_file(tmp_path, "a.json", raw)
    with pytest.raises(ValueError, match="Non-finite JSON value"):
        validate_manifest(manifest(tmp_path, [entry]))
