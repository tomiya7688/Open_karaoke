import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath


def _object(value: object, keys: set[str], label: str) -> dict:
    """
    {
      責務: [_object: JSON objectのキー集合が過不足ないことを検証する]
      処理: [1: dictか確認する, 2: キー集合が期待と一致するか確認する]
      引数: [value: 検証対象, keys: 必須かつ許可するキー, label: エラー表示名]
      戻り値: [検証済みのdict]
      エラー: [ValueError: objectでない、またはキーが過不足]
    }
    """
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} must contain exactly: {', '.join(sorted(keys))}")
    return value


def _text(value: object, label: str) -> str:
    """
    {
      責務: [_text: 空でない512文字以下の文字列を検証する]
      処理: [1: 文字列か確認する, 2: 空白のみでなく長さが上限内か確認する]
      引数: [value: 検証対象, label: エラー表示名]
      戻り値: [検証済みの文字列]
      エラー: [ValueError: 文字列でない、空、または長すぎる]
    }
    """
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"{label} must be a nonempty string of at most 512 characters")
    return value


def _digest(root: Path, file: object, label: str) -> str:
    """
    {
      責務: [_digest: manifest配下のファイルを開き、記載されたSHA-256と照合する]
      処理: [
        1: pathとsha256のobjectを検証する,
        2: 絶対パス・親参照・バックスラッシュを拒否する,
        3: manifest配下の通常ファイルか確認する,
        4: 1MiB単位でSHA-256を計算して照合する
      ]
      引数: [root: manifestのあるdirectory, file: pathとsha256を持つobject, label: エラー表示名]
      戻り値: [照合済みのSHA-256(小文字hex)]
      エラー: [ValueError: path・形式・実体・ハッシュのいずれかが不正]
    }
    """
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
    """
    {
      責務: [validate_manifest: 非公開歌唱datasetのmanifestを検証して件数を報告する]
      処理: [
        1: 4 MiB以下で読み、versionとsamples数を確認する,
        2: 各sampleのキー・id重複・split・出典・権利参照を検証する,
        3: source_groupと同一音声がdevelopmentとholdoutにまたがらないか確認する,
        4: 音声と注釈のSHA-256を検証する
      ]
      引数: [path: manifestのJSON file]
      戻り値: [version・件数・split別件数・hashes_verifiedを持つdict]
      エラー: [ValueError: manifestが契約を満たさない]
    }
    """
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
    """
    {
      責務: [main: manifest検証のCLI入口]
      処理: [
        1: 引数からmanifestを受け取る,
        2: 検証して結果をJSONで出力する,
        3: 失敗は引数エラーとして終了する
      ]
      引数: []
      戻り値: []
    }
    """
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
