import argparse
import hashlib
import json
from pathlib import Path

from .dataset_manifest import (
    _annotation_envelope,
    _load_json,
    _object,
    _text,
    _verified_file,
    validate_manifest,
)
from .lyric_evaluator import evaluate


def _read_bounded(path: Path, label: str) -> bytes:
    """
    {
      責務: [_read_bounded: fileを4 MiB以下に制限して読む]
      処理: [1: 上限+1 byteまで読む, 2: 上限を超えたらエラーにする]
      引数: [path: 読むfile, label: エラー表示名]
      戻り値: [fileのbytes]
      エラー: [ValueError: 4 MiBを超える]
    }
    """
    with path.open("rb") as source:
        data = source.read(4_194_305)
    if len(data) > 4_194_304:
        raise ValueError(f"{label} exceeds 4 MiB")
    return data


def build_report(manifest_path: Path, prediction_path: Path, split: str) -> dict:
    """
    {
      責務: [build_report: 非公開datasetの歌詞予測をsplit単位でCER/WER集計する]
      処理: [
        1: manifestを検証し、検証後に変化していないか確認する,
        2: 指定splitの歌詞注釈から参照歌詞を集める,
        3: 予測fileの形式・重複・件数を検証する,
        4: 予測と参照のsample集合が完全一致することを確認する,
        5: 歌詞evaluatorで集計し、manifestと予測のSHA-256付きで返す
      ]
      引数: [manifest_path: manifest, prediction_path: 予測file, split: development/holdout]
      戻り値: [モデル・件数・CER/WER・evaluator情報を持つレポート]
      エラー: [ValueError: splitや入力が契約を満たさない、または予測と参照が一致しない]
    }
    """
    if split not in {"development", "holdout"}:
        raise ValueError("split must be development or holdout")
    validated = validate_manifest(manifest_path)
    manifest_bytes = _read_bounded(manifest_path, "Dataset manifest")
    if hashlib.sha256(manifest_bytes).hexdigest() != validated["manifest_sha256"]:
        raise ValueError("Dataset manifest changed after validation")
    manifest = _load_json(manifest_bytes)
    root = manifest_path.parent.resolve()
    references = {}
    for index, sample in enumerate(manifest["samples"]):
        if sample["split"] != split:
            continue
        label = f"samples[{index}].annotation"
        annotation_path, annotation_hash = _verified_file(root, sample["annotation"], label)
        envelope = _annotation_envelope(
            annotation_path,
            annotation_hash,
            sample["id"],
            sample["audio"]["sha256"],
            label,
        )
        if envelope["kind"] != "lyrics":
            continue
        references[sample["id"]] = " ".join(
            segment["text"] for segment in envelope["payload"]["reference_segments"]
        )
        if len(references) > 1000:
            raise ValueError("Lyric evaluation is limited to 1000 samples per split")
    if not references:
        raise ValueError("The selected split has no lyric annotations")

    prediction_bytes = _read_bounded(prediction_path, "Prediction file")
    predictions = _object(
        _load_json(prediction_bytes), {"version", "model", "predictions"}, "predictions"
    )
    if type(predictions["version"]) is not int or predictions["version"] != 1:
        raise ValueError("Unsupported prediction version")
    model = _object(predictions["model"], {"name", "version"}, "model")
    model = {key: _text(value, f"model.{key}") for key, value in model.items()}
    entries = predictions["predictions"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 1000:
        raise ValueError("Predictions must contain 1..1000 records")
    hypotheses = {}
    for index, entry in enumerate(entries):
        entry = _object(entry, {"sample_id", "text"}, f"predictions[{index}]")
        identifier = _text(entry["sample_id"], f"predictions[{index}].sample_id")
        hypothesis = entry["text"]
        if not isinstance(hypothesis, str) or len(hypothesis) > 20_000:
            raise ValueError(f"predictions[{index}].text must be at most 20000 characters")
        if identifier in hypotheses:
            raise ValueError("Duplicate prediction sample ID")
        hypotheses[identifier] = hypothesis
    if hypotheses.keys() != references.keys():
        raise ValueError("Predictions must match exactly the lyric samples in the selected split")
    pairs = [
        {"id": identifier, "reference": reference, "hypothesis": hypotheses[identifier]}
        for identifier, reference in references.items()
    ]
    metrics = evaluate(pairs)
    return {
        "format_version": 1,
        "dataset_manifest_sha256": validated["manifest_sha256"],
        "predictions_sha256": hashlib.sha256(prediction_bytes).hexdigest(),
        "split": split,
        "model": model,
        "sample_count": len(pairs),
        "evaluator": metrics["evaluator"],
        "evaluator_version": metrics["version"],
        "normalization": metrics["normalization"],
        "cer_whitespace": metrics["cer_whitespace"],
        "wer_tokenizer": metrics["wer_tokenizer"],
        "totals": metrics["totals"],
        "cer": metrics["cer"],
        "wer": metrics["wer"],
        "empty_reference_insertions": metrics["empty_reference_insertions"],
    }


def main() -> None:
    """
    {
      責務: [main: 歌詞dataset集計のCLI入口]
      処理: [
        1: 引数を受け取る,
        2: レポートを作ってJSONで出力する,
        3: 失敗は引数エラーとして終了する
      ]
      引数: []
      戻り値: []
    }
    """
    parser = argparse.ArgumentParser(description="Aggregate private lyric dataset CER/WER")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("predictions", type=Path)
    parser.add_argument("--split", required=True, choices=["development", "holdout"])
    args = parser.parse_args()
    try:
        report = build_report(args.manifest, args.predictions, args.split)
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
