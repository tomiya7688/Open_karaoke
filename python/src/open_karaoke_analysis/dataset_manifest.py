import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON value: {value}")


def _load_json(data: bytes) -> object:
    return json.loads(data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def _object(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} must contain exactly: {', '.join(sorted(keys))}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"{label} must be a nonempty string of at most 512 characters")
    return value


def _verified_file(root: Path, file: object, label: str) -> tuple[Path, str]:
    entry = _object(file, {"path", "sha256"}, label)
    raw_path = _text(entry["path"], f"{label}.path")
    relative = PurePosixPath(raw_path)
    if (
        relative.is_absolute()
        or "\\" in raw_path
        or any(part in {".", ".."} for part in raw_path.split("/"))
        or not relative.parts
    ):
        raise ValueError(f"{label}.path must stay below the manifest directory")
    path = (root / Path(*relative.parts)).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"{label}.path must name a regular file below the manifest directory")
    expected = _text(entry["sha256"], f"{label}.sha256")
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise ValueError(f"{label}.sha256 must be 64 lowercase hex characters")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError(f"{label} SHA-256 mismatch")
    return path, expected


def _lyrics_payload(payload: dict, duration: int, label: str) -> None:
    payload = _object(payload, {"reference_segments"}, label)
    segments = payload["reference_segments"]
    if not isinstance(segments, list) or len(segments) > 10_000:
        raise ValueError(f"{label}.reference_segments must be an array of at most 10000 items")
    ids = set()
    previous_start = -1
    for index, segment in enumerate(segments):
        item_label = f"{label}.reference_segments[{index}]"
        segment = _object(segment, {"id", "text", "start_sample", "end_sample"}, item_label)
        identifier = _text(segment["id"], f"{item_label}.id")
        if identifier in ids:
            raise ValueError(f"{label} has duplicate lyric segment ids")
        ids.add(identifier)
        lyric = segment["text"]
        if not isinstance(lyric, str) or not lyric.strip() or len(lyric) > 20_000:
            raise ValueError(f"{item_label}.text must be nonempty and at most 20000 characters")
        start, end = segment["start_sample"], segment["end_sample"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= duration:
            raise ValueError(f"{item_label} must fit in the 48 kHz sample duration")
        if start < previous_start:
            raise ValueError(f"{label}.reference_segments must be ordered by start_sample")
        previous_start = start


def _annotation_envelope(
    path: Path, annotation_hash: str, sample_id: str, audio_hash: str, label: str
) -> None:
    with path.open("rb") as source:
        data = source.read(4_194_305)
    if len(data) > 4_194_304:
        raise ValueError(f"{label} exceeds 4 MiB")
    if hashlib.sha256(data).hexdigest() != annotation_hash:
        raise ValueError(f"{label} changed after SHA-256 verification")
    envelope = _object(
        _load_json(data),
        {
            "version",
            "sample_id",
            "audio_sha256",
            "sample_rate",
            "duration_samples",
            "kind",
            "payload",
        },
        label,
    )
    if type(envelope["version"]) is not int or envelope["version"] != 1:
        raise ValueError(f"{label}.version must be 1")
    if envelope["sample_id"] != sample_id or envelope["audio_sha256"] != audio_hash:
        raise ValueError(f"{label} does not match its manifest sample and audio")
    if type(envelope["sample_rate"]) is not int or envelope["sample_rate"] != 48_000:
        raise ValueError(f"{label}.sample_rate must be 48000")
    duration = envelope["duration_samples"]
    if type(duration) is not int or not 0 < duration <= 48_000 * 60 * 60 * 4:
        raise ValueError(f"{label}.duration_samples must be 1..691200000")
    if not isinstance(envelope["kind"], str) or envelope["kind"] not in {
        "lyrics",
        "alignment",
        "pitch",
        "events",
        "notes",
        "stems",
    }:
        raise ValueError(f"{label}.kind is unsupported")
    if not isinstance(envelope["payload"], dict) or not envelope["payload"]:
        raise ValueError(f"{label}.payload must be a nonempty object")
    if envelope["kind"] == "lyrics":
        _lyrics_payload(envelope["payload"], duration, f"{label}.payload")


def validate_manifest(path: Path) -> dict:
    with path.open("rb") as source:
        data = source.read(4_194_305)
    if len(data) > 4_194_304:
        raise ValueError("Dataset manifest exceeds 4 MiB")
    manifest = _object(_load_json(data), {"version", "samples"}, "manifest")
    if type(manifest["version"]) is not int or manifest["version"] != 1:
        raise ValueError("Unsupported dataset manifest version")
    samples = manifest["samples"]
    if not isinstance(samples, list) or not 1 <= len(samples) <= 10_000:
        raise ValueError("Dataset manifest needs 1..10000 samples")
    root = path.parent.resolve()
    ids = set()
    groups = {}
    audio_hashes = {}
    counts = {"development": 0, "holdout": 0}
    for index, sample in enumerate(samples):
        label = f"samples[{index}]"
        sample = _object(
            sample,
            {
                "id",
                "split",
                "source_group",
                "provenance",
                "rights_reference",
                "audio",
                "annotation",
            },
            label,
        )
        identifier = _text(sample["id"], f"{label}.id")
        if identifier in ids:
            raise ValueError("Duplicate sample id")
        ids.add(identifier)
        split = sample["split"]
        if not isinstance(split, str) or split not in counts:
            raise ValueError(f"{label}.split must be development or holdout")
        group = _text(sample["source_group"], f"{label}.source_group")
        _text(sample["provenance"], f"{label}.provenance")
        _text(sample["rights_reference"], f"{label}.rights_reference")
        if group in groups and groups[group] != split:
            raise ValueError("A source group appears in both development and holdout")
        groups[group] = split
        _, audio_hash = _verified_file(root, sample["audio"], f"{label}.audio")
        annotation_path, annotation_hash = _verified_file(
            root, sample["annotation"], f"{label}.annotation"
        )
        _annotation_envelope(
            annotation_path, annotation_hash, identifier, audio_hash, f"{label}.annotation"
        )
        if audio_hash in audio_hashes and audio_hashes[audio_hash] != split:
            raise ValueError("Identical audio appears in both development and holdout")
        audio_hashes[audio_hash] = split
        counts[split] += 1
    return {
        "version": 1,
        "samples": len(samples),
        "splits": counts,
        "hashes_verified": True,
        "annotation_envelopes_verified": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a private real-singing dataset manifest")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        report = validate_manifest(args.manifest)
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
