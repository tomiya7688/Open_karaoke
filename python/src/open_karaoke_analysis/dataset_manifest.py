import argparse
import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    """
    {
      責務: [_unique_object: JSON objectの重複キーを拒否してdictにする]
      処理: [1: キーと値を順に読む, 2: 既出のキーならエラーにする]
      引数: [pairs: JSON parserが渡すキーと値の組]
      戻り値: [重複のないdict]
      エラー: [ValueError: 重複したキー]
    }
    """
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    """
    {
      責務: [_invalid_constant: NaN・Infinityなど非有限のJSON値を拒否する]
      処理: [1: 受け取った定数名を含むエラーを送出する]
      引数: [value: 定数名]
      戻り値: []
      エラー: [ValueError: 常に送出する]
    }
    """
    raise ValueError(f"Non-finite JSON value: {value}")


def _finite_float(text: str) -> float:
    """
    {
      責務: [_finite_float: JSONの小数を読み、"1e999" のようにinfになる値を拒否する]
      処理: [1: floatへ変換する, 2: 有限でなければエラーにする]
      引数: [text: JSONの数値表記]
      戻り値: [有限のfloat]
      エラー: [ValueError: 非有限の値]
    }
    """
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"Non-finite JSON value: {text}")
    return value


def _load_json(data: bytes) -> object:
    """
    {
      責務: [_load_json: 重複キーと非有限値を拒否してJSONを読む]
      処理: [1: 重複キー検査と非有限値検査を指定してparseする]
      引数: [data: JSONのbytes]
      戻り値: [parse済みの値]
      エラー: [ValueError: JSONが不正、重複キー、または非有限値]
    }
    """
    return json.loads(
        data,
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_constant,
        parse_float=_finite_float,
    )


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


def _strata(value: object, label: str) -> dict:
    """
    {
      責務: [_strata: 評価層(言語・声の由来・音質・歌唱現象)を検証する]
      処理: [
        1: 4項目の過不足を確認する,
        2: 言語・声の由来・音質が許可された値か確認する,
        3: 歌唱現象が許可された値で重複しないか確認する
      ]
      引数: [value: 検証対象, label: エラー表示名]
      戻り値: [検証済みの評価層]
      エラー: [ValueError: 項目または値が不正]
    }
    """
    strata = _object(value, {"language", "vocal_source", "audio_condition", "phenomena"}, label)
    choices = {
        "language": {"ja", "en", "other", "mixed", "none"},
        "vocal_source": {"human", "synthetic"},
        "audio_condition": {"clean", "degraded"},
    }
    for field, allowed in choices.items():
        if not isinstance(strata[field], str) or strata[field] not in allowed:
            raise ValueError(f"{label}.{field} is unsupported")
    phenomena = strata["phenomena"]
    allowed_phenomena = {"long_vowel", "legato", "repetition"}
    if (
        not isinstance(phenomena, list)
        or len(phenomena) > len(allowed_phenomena)
        or any(not isinstance(item, str) or item not in allowed_phenomena for item in phenomena)
        or len(phenomena) != len(set(phenomena))
    ):
        raise ValueError(f"{label}.phenomena contains unsupported or duplicate values")
    return strata


def _verified_file(root: Path, file: object, label: str) -> tuple[Path, str]:
    """
    {
      責務: [_verified_file: manifest配下のファイルを開き、記載されたSHA-256と照合する]
      処理: [
        1: pathとsha256のobjectを検証する,
        2: 絶対パス・ドライブ指定・空や親の参照・バックスラッシュを拒否する,
        3: manifest配下の通常ファイルか確認する,
        4: 1MiB単位でSHA-256を計算して照合する
      ]
      引数: [root: manifestのあるdirectory, file: pathとsha256を持つobject, label: エラー表示名]
      戻り値: [解決済みpathと照合済みのSHA-256(小文字hex)]
      エラー: [ValueError: path・形式・実体・ハッシュのいずれかが不正]
    }
    """
    entry = _object(file, {"path", "sha256"}, label)
    raw_path = _text(entry["path"], f"{label}.path")
    relative = PurePosixPath(raw_path)
    if (
        relative.is_absolute()
        or PureWindowsPath(raw_path).drive
        or "\\" in raw_path
        or any(part in {"", ".", ".."} for part in raw_path.split("/"))
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
    """
    {
      責務: [_lyrics_payload: 歌詞注釈のreference_segmentsを検証する]
      処理: [
        1: segment配列の件数上限を確認する,
        2: id重複とテキスト長を確認する,
        3: 開始・終了sampleが音源長に収まり開始順か確認する
      ]
      引数: [payload: 歌詞注釈のpayload, duration: 音源長(48 kHz sample), label: エラー表示名]
      戻り値: []
      エラー: [ValueError: segmentが契約を満たさない]
    }
    """
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
) -> dict:
    """
    {
      責務: [_annotation_envelope: 注釈fileが音声とsampleに束縛されたenvelopeか検証する]
      処理: [
        1: 4 MiB以下で読み、検証済みのSHA-256から変化していないか確認する,
        2: envelopeのキー・version・sample_id・音声SHA-256を確認する,
        3: 48 kHzと音源長、kind、payloadを確認する,
        4: 歌詞注釈ならreference_segmentsを検証する
      ]
      引数: [
        path: 注釈file,
        annotation_hash: 検証済みのSHA-256,
        sample_id: manifest上のsample id,
        audio_hash: 音声のSHA-256,
        label: エラー表示名
      ]
      戻り値: [検証済みのenvelope]
      エラー: [ValueError: envelopeが契約を満たさない]
    }
    """
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
    return envelope


def validate_manifest(path: Path) -> dict:
    """
    {
      責務: [validate_manifest: 非公開歌唱datasetのmanifestを検証して件数を報告する]
      処理: [
        1: 4 MiB以下で読み、versionとsamples数を確認する,
        2: 各sampleのキー・id重複・split・出典・権利参照を検証する,
        3: source_groupと同一音声がdevelopmentとholdoutにまたがらないか確認する,
        4: 評価層を検証し、split別に集計する,
        5: 音声と注釈のSHA-256、注釈envelopeを検証する
      ]
      引数: [path: manifestのJSON file]
      戻り値: [version・manifestのSHA-256・件数・split別件数・評価層集計・検証結果を持つdict]
      エラー: [ValueError: manifestが契約を満たさない]
    }
    """
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
    strata_counts = {
        split: {
            "language": {},
            "vocal_source": {},
            "audio_condition": {},
            "phenomena": {},
        }
        for split in counts
    }
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
                "strata",
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
        strata = _strata(sample["strata"], f"{label}.strata")
        if group in groups and groups[group] != split:
            raise ValueError("A source group appears in both development and holdout")
        groups[group] = split
        _, audio_hash = _verified_file(root, sample["audio"], f"{label}.audio")
        annotation_path, annotation_hash = _verified_file(
            root, sample["annotation"], f"{label}.annotation"
        )
        envelope = _annotation_envelope(
            annotation_path, annotation_hash, identifier, audio_hash, f"{label}.annotation"
        )
        if (
            strata["language"] == "none"
            and envelope["kind"] == "lyrics"
            and envelope["payload"]["reference_segments"]
        ):
            raise ValueError(f"{label}.strata.language cannot be none with reference lyrics")
        if audio_hash in audio_hashes and audio_hashes[audio_hash] != split:
            raise ValueError("Identical audio appears in both development and holdout")
        audio_hashes[audio_hash] = split
        counts[split] += 1
        split_strata = strata_counts[split]
        for field in ("language", "vocal_source", "audio_condition"):
            value = strata[field]
            split_strata[field][value] = split_strata[field].get(value, 0) + 1
        for value in strata["phenomena"] or ["none"]:
            split_strata["phenomena"][value] = split_strata["phenomena"].get(value, 0) + 1
    return {
        "version": 1,
        "manifest_sha256": hashlib.sha256(data).hexdigest(),
        "samples": len(samples),
        "splits": counts,
        "strata_counts": strata_counts,
        "hashes_verified": True,
        "annotation_envelopes_verified": True,
    }


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
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
