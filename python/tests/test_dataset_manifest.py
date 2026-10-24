import hashlib
import json
from pathlib import Path

import pytest

from open_karaoke_analysis.dataset_manifest import validate_manifest


def write_file(root: Path, name: str, content: bytes) -> dict:
    (root / name).write_bytes(content)
    return {"path": name, "sha256": hashlib.sha256(content).hexdigest()}


def sample(root: Path, identifier: str, split="development", group=None, audio=None):
    return {
        "id": identifier,
        "split": split,
        "source_group": group or f"group-{identifier}",
        "provenance": "recorded by the project",
        "rights_reference": "consent-001",
        "audio": write_file(root, f"{identifier}.wav", audio or identifier.encode()),
        "annotation": write_file(root, f"{identifier}.json", b"{}"),
    }


def manifest(root: Path, samples, version=1) -> Path:
    path = root / "manifest.json"
    path.write_text(json.dumps({"version": version, "samples": samples}), encoding="utf-8")
    return path


def test_valid_manifest_reports_counts_and_verified_hashes(tmp_path):
    path = manifest(
        tmp_path,
        [sample(tmp_path, "a"), sample(tmp_path, "b", split="holdout")],
    )
    assert validate_manifest(path) == {
        "version": 1,
        "samples": 2,
        "splits": {"development": 1, "holdout": 1},
        "hashes_verified": True,
    }


@pytest.mark.parametrize("version", [2, True, "1"])
def test_unsupported_version_is_rejected(tmp_path, version):
    path = manifest(tmp_path, [sample(tmp_path, "a")], version=version)
    with pytest.raises(ValueError, match="version"):
        validate_manifest(path)


def test_sample_count_and_keys_are_validated(tmp_path):
    with pytest.raises(ValueError, match="1..10000"):
        validate_manifest(manifest(tmp_path, []))
    extra = sample(tmp_path, "a")
    extra["unexpected"] = "x"
    with pytest.raises(ValueError, match="exactly"):
        validate_manifest(manifest(tmp_path, [extra]))


def test_duplicate_id_and_bad_split_are_rejected(tmp_path):
    first = sample(tmp_path, "a")
    with pytest.raises(ValueError, match="Duplicate"):
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


@pytest.mark.parametrize("raw", ["../outside.wav", "/abs.wav", "a\\b.wav", "./a.wav", "sub//x"])
def test_paths_must_stay_below_manifest_directory(tmp_path, raw):
    entry = sample(tmp_path, "a")
    entry["audio"]["path"] = raw
    with pytest.raises(ValueError, match="manifest directory"):
        validate_manifest(manifest(tmp_path, [entry]))


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
