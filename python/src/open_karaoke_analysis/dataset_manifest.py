import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath


def _object(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} must contain exactly: {', '.join(sorted(keys))}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"{label} must be a nonempty string of at most 512 characters")
    return value


def _digest(root: Path, file: object, label: str) -> str:
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
    return expected


def validate_manifest(path: Path) -> dict:
    with path.open("rb") as source:
        data = source.read(4_194_305)
    if len(data) > 4_194_304:
        raise ValueError("Dataset manifest exceeds 4 MiB")
    manifest = _object(json.loads(data), {"version", "samples"}, "manifest")
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
        audio_hash = _digest(root, sample["audio"], f"{label}.audio")
        _digest(root, sample["annotation"], f"{label}.annotation")
        if audio_hash in audio_hashes and audio_hashes[audio_hash] != split:
            raise ValueError("Identical audio appears in both development and holdout")
        audio_hashes[audio_hash] = split
        counts[split] += 1
    return {"version": 1, "samples": len(samples), "splits": counts, "hashes_verified": True}


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a private real-singing dataset manifest")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        report = validate_manifest(args.manifest)
    except (OSError, UnicodeError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
